"""Stdlib HTTP server: runs the agent per request and serves the audit page.

    python3 -m second_opinion.server            # http://localhost:8000
    PORT=9000 python3 -m second_opinion.server

Endpoints
    GET /                     the interface
    GET /api/seeds            seed list + environment readiness
    GET /api/audit?seed=...   run the agent for one seed (throttled)
    GET /api/demo             run the agent over invented evidence (no network, no key)
    GET /api/rules            frozen thresholds and state definitions
    GET /api/health           liveness

The key is never echoed: `/api/seeds` reports only whether one is present.

`/api/audit` is the only endpoint that can spend anything, and a live audit costs
three Qloo calls — four when the agent's single follow-up lookup fires. It
therefore carries the in-memory limits in `second_opinion.throttle`: a per-client
token bucket, a cap on audits in flight, and a bound on the seed string. Those
limits are separate per mode — fixture audits read local files and spend no quota,
so they draw on their own larger budget instead of the quota guard. `/api/demo`
reaches neither: it runs the same agent over records written into the repository
by hand, so it is free in quota and reports `mode: "synthetic"` so that no one,
frontend or test, can read it as a live result. Everything else is static or
hand-authored and stays free.
"""

from __future__ import annotations

import io
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import AdapterError, FixtureAdapter, LiveAdapter, describe_environment
from .agent import SecondOpinionAgent
from .audit import rules_payload
from .catalog import seed_list
from .config import DEFAULT_CONFIG, AuditConfig
from .demo import DEMO_SEED, SCENARIOS, SyntheticAdapter
from .env import load_env_file
from .throttle import (
    MAX_SEED_LENGTH,
    MODES,
    active_throttles,
    rightmost_forwarded_client,
    throttle_for_mode,
)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


# Characters of an access-log line kept before truncation. Long enough to hold a
# whole request target for a real artist name, short enough that a hostile URL
# cannot bloat the log.
LOG_LINE_LIMIT = 300

REDACTED = b"[credential redacted]"


def _redact_credential(body: bytes) -> bytes:
    """Last line of defence: no response ever carries the live credential.

    Adapter messages name `QLOO_API_KEY` but are built from text that never
    includes its value, so this should never fire. It is here because one future
    message that formatted a captured exception would otherwise be a silent
    disclosure bug rather than a visible failure, and a substitution costs one
    byte scan.

    Limitation: it matches the literal key bytes, so a key containing characters
    JSON escapes would not be caught by the escaped form. Real Qloo keys are
    ASCII; the message construction is the real guarantee, this is the net.
    """
    key = os.environ.get("QLOO_API_KEY") or ""
    if len(key) < 4:
        return body
    return body.replace(key.encode("utf-8", "ignore"), REDACTED)


def _format_exception() -> str:
    """The traceback for the operator's log, with the same credential net applied.

    `traceback.print_exc()` would write straight to stderr, and on a hosted
    service that log is readable by whoever runs the host. An exception message
    that ever formatted a captured value — `QLOO_API_KEY` included — would then be
    recorded there in the clear. One byte scan on the way to the log closes that,
    and the operator still gets the full frames.
    """
    buffer = io.StringIO()
    traceback.print_exc(file=buffer)
    return _redact_credential(buffer.getvalue().encode("utf-8", "ignore")).decode(
        "utf-8", "replace"
    )


def _rate_limit_reason(mode: str) -> str:
    """Why a refusal happened, in terms of what that mode actually costs.

    Telling a fixture-mode visitor that their "Qloo quota" is spent is both wrong
    and confusing: a local read spent nothing. The reader should learn which
    budget ran out and that the other one is untouched.
    """
    if mode == "fixture":
        return "fixture audit budget reached; try again shortly (no Qloo request is made in fixture mode)"
    return "live audit rate limit reached; try again later"


def _adapter(mode: str):
    return LiveAdapter() if mode == "live" else FixtureAdapter()


def server_mode() -> str:
    """Deployment mode: `live` unless SO_MODE=fixture is set explicitly.

    There is no automatic fallback. If live mode has no credential the audit
    endpoint returns 503; it never quietly serves fixture data instead.
    """
    mode = (os.environ.get("SO_MODE") or "live").strip().lower()
    return mode if mode in MODES else "live"


class Handler(BaseHTTPRequestHandler):
    server_version = "SecondOpinion/1.0"

    # -- helpers ---------------------------------------------------------
    def _send_json(self, payload, status=200, extra_headers=None):
        body = _redact_credential(json.dumps(payload).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path):
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_scope(self) -> str:
        """Address used only to bucket the audit limit.

        Behind a proxy the peer address is the proxy, so without the forwarded
        header every visitor would share one bucket. Entries to the left of the
        last one are client-supplied and are ignored for that reason.
        """
        forwarded = rightmost_forwarded_client(self.headers.get("X-Forwarded-For"))
        if forwarded:
            return forwarded
        return self.client_address[0] if self.client_address else ""

    def log_message(self, fmt, *args):
        # The formatted message is the access line, and it contains the request
        # target — whatever a client typed into the URL. `http.server` caps a
        # request line near 64 KB, so bound it here rather than letting one rude
        # URL become one rude log line. The credential is not in any URL (it
        # travels in the `X-Api-Key` header), so this is log volume, not
        # disclosure.
        line = fmt % args
        if len(line) > LOG_LINE_LIMIT:
            line = line[:LOG_LINE_LIMIT] + " ... (truncated)"
        sys.stderr.write("%s - %s\n" % (self.address_string(), line))

    # -- routes ----------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._send_file(WEB_DIR / "index.html")
        if route.startswith("/static/"):
            candidate = (WEB_DIR / route[len("/static/"):]).resolve()
            if WEB_DIR.resolve() not in candidate.parents:
                return self._send_json({"error": "invalid path"}, 400)
            return self._send_file(candidate)
        if route == "/api/health":
            return self._send_json({"ok": True})
        if route == "/api/rules":
            return self._send_json(rules_payload())
        if route == "/api/seeds":
            return self._handle_seeds()
        if route == "/api/audit":
            return self._audit_guarded(query)
        if route == "/api/demo":
            return self._demo_guarded(query)
        return self._send_json({"error": "not found"}, 404)

    def _guarded(self, handler, query, message):
        """Run a request handler that must never close the socket mid-response."""
        try:
            return handler(query)
        except Exception:
            sys.stderr.write(_format_exception())
            return self._send_json({"ok": False, "error": message}, 500)

    def _audit_guarded(self, query):
        """One guard for the whole audit path.

        An uncaught error inside `BaseHTTPRequestHandler` closes the socket
        without a response, which the frontend can only report as a network
        failure — and a bug in the limiter itself would look exactly like that.
        The traceback goes to stderr for the operator; the client gets a plain
        500 with no internals.
        """
        return self._guarded(self._handle_audit, query, "internal audit error")

    def _demo_guarded(self, query):
        return self._guarded(self._handle_demo, query, "internal demo error")

    def _handle_demo(self, query):
        """The scripted demonstration: the real agent over evidence written by hand.

        It exists because a clean checkout has no `data/` and a judge's laptop has
        no credential, while the interesting behaviour — a gap spotted, one re-read
        issued, a verdict revised or declined — only shows up when the agent runs.
        Nothing here touches the network or the key, so no budget is charged and the
        response says `mode: "synthetic"` for exactly that reason.
        """
        scenario = (query.get("scenario") or ["improves"])[0].strip().lower()
        if scenario not in SCENARIOS:
            return self._send_json(
                {"ok": False, "error": "unknown demo scenario: use improves or confirms"}, 400
            )
        agent = SecondOpinionAgent(SyntheticAdapter(scenario), config=DEFAULT_CONFIG)
        report = agent.audit(DEMO_SEED)
        report["demo"] = {
            "scenario": scenario,
            "evidence_origin": "synthetic: these artists, terms and affinity numbers are "
                               "invented for this demonstration and were not returned by Qloo",
            "seed_queries": agent.adapter.queries.count("resolve_seed"),
            "entity_lookups": agent.adapter.queries.count("entity_record"),
        }
        return self._send_json(report)

    def _handle_seeds(self):
        """A hand-authored seed list: no fixtures, no cached metadata."""
        env = describe_environment()
        return self._send_json({
            "seeds": seed_list(),
            "catalog": "static",
            "environment": {"live_ready": env["live_ready"], "host": env["host"]},
            "rules": rules_payload(),
            "mode": server_mode(),
        })

    def _handle_audit(self, query):
        seed = (query.get("seed") or [""])[0].strip()
        mode = (query.get("mode") or [server_mode()])[0].strip().lower()
        if not seed:
            return self._send_json({"ok": False, "error": "missing seed parameter"}, 400)
        if len(seed) > MAX_SEED_LENGTH:
            return self._send_json(
                {"ok": False, "error": f"seed is too long; the limit is {MAX_SEED_LENGTH} characters"},
                400,
            )
        if mode not in MODES:
            # The client's value is not echoed: it is arbitrary text and it buys
            # the reader nothing that "live or fixture" does not already say.
            return self._send_json({"ok": False, "error": "unknown mode: use live or fixture"}, 400)
        try:
            threshold = float((query.get("threshold") or [DEFAULT_CONFIG.strong_threshold])[0])
            min_corr = int((query.get("min_corroborated") or [DEFAULT_CONFIG.min_dimensions_corroborated])[0])
            config = AuditConfig(strong_threshold=threshold, min_dimensions_corroborated=min_corr)
        except (TypeError, ValueError):
            return self._send_json({"error": "invalid threshold or min_corroborated"}, 400)

        # Every limit is checked before the adapter is constructed, so a refused
        # request costs nothing at all. The budget is chosen by mode: only a live
        # audit spends Qloo quota, so a fixture audit draws on a separate, larger
        # bucket and can never empty the quota guard.
        limits = throttle_for_mode(mode)
        allowed, retry_after = limits.budget(self._client_scope())
        if not allowed:
            wait = max(1, int(round(retry_after)))
            return self._send_json(
                {
                    "ok": False,
                    "code": "rate_limited",
                    "mode": mode,
                    "error": _rate_limit_reason(mode),
                    "retry_after_seconds": wait,
                    "limit": limits.as_dict(),
                },
                429,
                {"Retry-After": str(wait)},
            )

        if not limits.gate.try_acquire():
            return self._send_json(
                {
                    "ok": False,
                    "code": "audit_busy",
                    "mode": mode,
                    "error": "too many audits are running right now; try again shortly",
                    "retry_after_seconds": 2,
                    "limit": limits.as_dict(),
                },
                503,
                {"Retry-After": "2"},
            )
        try:
            return self._run_audit(seed, mode, config)
        finally:
            limits.gate.release()

    def _run_audit(self, seed, mode, config):
        try:
            adapter = _adapter(mode)
        except AdapterError as exc:
            # A deployment that cannot even build an adapter — no credential in
            # live mode, no fixture directory in fixture mode — is a server-side
            # configuration problem, not the client's.
            return self._send_json({"ok": False, "error": str(exc), "mode": mode}, 503)

        agent = SecondOpinionAgent(adapter, config=config)
        try:
            report = agent.audit(seed)
        except AdapterError as exc:
            return self._send_json({"ok": False, "error": str(exc), "mode": mode}, 502)
        return self._send_json(report)


def serve(host="0.0.0.0", port=8000):
    httpd = ThreadingHTTPServer((host, port), Handler)
    mode = server_mode()
    env = describe_environment()
    budgets = {mode: throttle.as_dict() for mode, throttle in active_throttles().items()}
    live = budgets["live"]
    fixture = budgets["fixture"]
    ready = "key present" if env["live_ready"] else "no QLOO_API_KEY: audits will refuse"
    print(f"Second Opinion listening on http://{host}:{port}  (mode={mode}, {ready})", flush=True)
    print(
        f"live audit limits:    {live['per_minute']}/minute per client, "
        f"burst {live['burst']}, {live['concurrent']} in flight "
        f"— three Qloo requests each, four when the follow-up fires "
        f"({live['storage']}, reset on restart)",
        flush=True,
    )
    print(
        f"fixture audit limits: {fixture['per_minute']}/minute per client, "
        f"burst {fixture['burst']}, {fixture['concurrent']} in flight "
        f"— local reads, no Qloo request ({fixture['storage']}, reset on restart)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()


def main(argv=None):
    # A directly launched server reads the project-root `.env`; variables
    # already in the real environment (Docker, a platform, the shell) win.
    load_env_file()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    serve(host, port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
