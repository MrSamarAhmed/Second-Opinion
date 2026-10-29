"""Stdlib HTTP server: runs the agent per request and serves the audit page.

    python3 -m second_opinion.server            # http://localhost:8000
    PORT=9000 python3 -m second_opinion.server

Endpoints
    GET /                     the interface
    GET /api/seeds            seed list + environment readiness
    GET /api/audit?seed=...   run the agent for one seed
    GET /api/rules            frozen thresholds and state definitions
    GET /api/health           liveness

The key is never echoed: `/api/seeds` reports only whether one is present.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import AdapterError, FixtureAdapter, LiveAdapter, describe_environment
from .agent import SecondOpinionAgent
from .audit import rules_payload
from .catalog import seed_list
from .config import DEFAULT_CONFIG, AuditConfig
from .env import load_env_file

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


MODES = ("live", "fixture")


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
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    def log_message(self, fmt, *args):  # keep logs free of query strings
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

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
            return self._handle_audit(query)
        return self._send_json({"error": "not found"}, 404)

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
        seed = (query.get("seed") or [""])[0]
        mode = (query.get("mode") or [server_mode()])[0].strip().lower()
        if not seed:
            return self._send_json({"error": "missing seed parameter"}, 400)
        if mode not in MODES:
            return self._send_json({"error": f"unknown mode {mode!r}"}, 400)
        try:
            threshold = float((query.get("threshold") or [DEFAULT_CONFIG.strong_threshold])[0])
            min_corr = int((query.get("min_corroborated") or [DEFAULT_CONFIG.min_dimensions_corroborated])[0])
            config = AuditConfig(strong_threshold=threshold, min_dimensions_corroborated=min_corr)
        except (TypeError, ValueError):
            return self._send_json({"error": "invalid threshold or min_corroborated"}, 400)

        try:
            adapter = _adapter(mode)
        except AdapterError as exc:
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
    ready = "key present" if env["live_ready"] else "no QLOO_API_KEY: audits will refuse"
    print(f"Second Opinion listening on http://{host}:{port}  (mode={mode}, {ready})", flush=True)
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
