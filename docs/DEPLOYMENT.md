# Deployment notes

Status: **not deployed — no public demo URL exists.** No GitHub repository was
created from this copy, nothing was pushed, and no hosting provider was contacted.
This file records what has been verified locally and the exact steps an operator
performs.

The application defaults to live mode and its seed list is hand-authored, so a
deployment target with a runtime `QLOO_API_KEY` can run without any captured Qloo
response files.

## Application profile

- One long-running Python process, standard library only, Python 3.9+.
- No build step, no database, no external assets; `web/index.html` is served by
  the same process.
- Reads `HOST` (default `0.0.0.0`) and `PORT` (default `8000`).
- Reads `SO_MODE` (default `live`; set `fixture` only for local testing).
- `GET /api/health` returns `{"ok": true}` for platform health checks.
- Stateless per request except one thing: the audit budgets in
  `second_opinion/throttle.py` — one per audit mode — live in process memory.
  Nothing is written to disk, and that memory is gone after a restart, redeploy
  or idle spin-down.

This fits any container host or a plain VM. A serverless/edge platform does not
fit without a rewrite because the server is `ThreadingHTTPServer`, not a request
handler.

## Environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `QLOO_API_KEY` | yes, for a working live demo | none | Secret. Set it in the platform's secret store, never in the image, repo, logs or frontend. The server reports only whether a key is present. |
| `QLOO_API_HOST` | no | `https://hackathon.api.qloo.com` | Set it to whichever host your credential was issued for; the client reads this variable and no other endpoint is assumed. |
| `SO_MODE` | no | `live` | `live` calls Qloo. `fixture` reads local captures and is for development only. There is no automatic fallback: with `live` and no key, audits return `503`. |
| `PORT` | no | `8000` | Injected by the platform. Render's default is `10000`; the server binds whatever `PORT` says (verified in-container), so no code change is needed. |
| `HOST` | no | `0.0.0.0` | Keep the default in containers; binding `127.0.0.1` makes the service unreachable from outside. |
| `SO_AUDIT_PER_MINUTE` | no | `6` | **Live audits only** — this is the quota guard. Sustained audits per client per minute. One live audit costs three Qloo requests, so `6` means up to 18 requests per client-minute. A non-numeric, zero or negative value keeps the default; above 600 it is clamped. |
| `SO_AUDIT_BURST` | no | `3` | Live: how many audits one client may fire at once before the per-minute refill binds. Never larger than `SO_AUDIT_PER_MINUTE`. |
| `SO_MAX_CONCURRENT_AUDITS` | no | `2` | Live: audits in flight across the instance, so a few slow Qloo responses cannot pile threads onto a small host. Ceiling of 16; a busy instance answers `503` with `Retry-After: 2`. |
| `SO_FIXTURE_AUDIT_PER_MINUTE` | no | `30` | Fixture audits per client per minute. Fixture mode reads local files and spends no Qloo quota, so this bound exists only to limit local work — and fixture traffic never draws on the live budget. Same clamping rules. |
| `SO_FIXTURE_AUDIT_BURST` | no | `10` | Fixture: audits one client may fire at once. Never larger than `SO_FIXTURE_AUDIT_PER_MINUTE`. |
| `SO_FIXTURE_MAX_CONCURRENT_AUDITS` | no | `4` | Fixture: local audits in flight, so a loop cannot occupy the instance with more than four parses and report builds. |

A directly launched `python3 -m second_opinion.server` (or `.cli`) also reads
the project-root `.env` through `second_opinion/env.py`. Real environment
variables take precedence, and the image never contains `.env`
(`.dockerignore` excludes it), so containers and platforms must still receive
the key at runtime.

## Seed catalogue

`second_opinion/catalog.py` holds eight hand-written artist names and a short
editorial note each. It contains no entity IDs, tag counts, affinities, or
captured payloads. `/api/seeds` serves that list plus the frozen rules and a
`live_ready` flag; selecting a seed sends its name to `/api/audit`, which
resolves it through the live Qloo search API and then audits the live
recommendation set.

## Local no-key verification

Verified on 2026-10-29 in this publication copy, with `QLOO_API_KEY` unset and no
captured responses present:

| Check | Observed |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | 219 tests: 162 passed, 0 failed, 0 errors, 57 skipped (missing captured responses), exit 0 |
| Same suite with `data/fixtures/` temporarily made available | 219 tests OK, 0 skips — neither audit budget interferes with fixture audits |
| `GET /api/health` | `200 {"ok": true}` |
| `GET /api/seeds` | `200`, `mode: "live"`, `live_ready: false`, 8 static seeds |
| `GET /` | `200`, `text/html`, the single-page frontend |
| `GET /api/audit?seed=Laufey` | `503`, error names `QLOO_API_KEY` |
| `GET /api/audit?seed=Laufey&mode=live` | `503`, no fixture fallback |
| Four live audits from one client with `SO_AUDIT_PER_MINUTE=2 SO_AUDIT_BURST=2 SO_MAX_CONCURRENT_AUDITS=1` | `503`, `503` (no key), then `429` twice with `Retry-After: 30`, `error: "live audit rate limit reached; try again later"` and a `limit` block naming `mode: "live"` and `spends_quota: true` |
| A **fixture** audit from the same client whose live budget is spent | `503`, `{"ok": false, "error": "fixture directories not found under …/data/fixtures", "mode": "fixture"}` — never `429`, so the live guard cannot starve local reads |
| A live audit from a different `X-Forwarded-For` while the first client is rate-limited | `503` (no key), not `429` — the bucket is per client, not global |
| Ten `GET /api/seeds` while the audit budget is exhausted | all `200` — only the endpoint that spends quota is limited |
| `GET /api/audit` with a 200-character seed, three times | `400`, `seed is too long; the limit is 120 characters` — and the two valid audits right after it still returned `503` (no key) with the third returning `429`, so a rejected request spent no budget at all |
| Container run with `PORT=10000 SO_AUDIT_PER_MINUTE=2 SO_AUDIT_BURST=2` | inside the image, for one client: `503`, `503`, then `429` twice with `Retry-After: 30` and `code: "rate_limited"`; a 200-character seed returns `400`; `/api/seeds` keeps answering `200`; and a `mode=fixture` audit from that same rate-limited client returns `503 {"ok": false, "error": "fixture directories not found under /app/data/fixtures", "mode": "fixture"}` — not `429` |
| The page in a browser, fixture mode, `SO_FIXTURE_AUDIT_PER_MINUTE=1 SO_FIXTURE_AUDIT_BURST=1` | the first seed renders a full report (candidates, receipts verified, blanket flag, agent trace); once the fixture budget is spent the next click renders "Audit could not run — Little Simz — fixture audit budget reached; try again shortly (no Qloo request is made in fixture mode)" with the facts `mode = fixture`, `http status = 429`, `fixture budget = 1/min, burst 1`, `spends Qloo quota = no, local read`, and a countdown that ticks in text ("Next fixture audit from this client accepted in about 48 seconds." → "…about 32 seconds." fifteen seconds later) while the page's request total stays constant at 3 — it never retries on a timer |
| Server started with `SO_AUDIT_PER_MINUTE=0 SO_FIXTURE_AUDIT_PER_MINUTE=-5 SO_FIXTURE_AUDIT_BURST=999` | banner reports the live defaults `6/minute per client, burst 3, 2 in flight` and the fixture defaults `30/minute … 4 in flight`, with the oversized fixture burst clamped down to its own rate (`burst 30`) — a zero or a negative value cannot switch a budget off, and a huge one cannot switch it open |
| Startup banner | `live audit limits: 6/minute per client, burst 3, 2 in flight — three Qloo requests each (in-memory, reset on restart)` and `fixture audit limits: 30/minute per client, burst 10, 4 in flight — local reads, no Qloo request (in-memory, reset on restart)` |
| `docker build` | succeeds; `/app` contains only `second_opinion/` and `web/`, no `.env`, no data files, runs as uid 10001 |
| `docker run -e PORT=10000` | the app answers `/api/health`, `/api/seeds` and `/` on 10000 and still returns `503` for a live audit with no key |

The refused-request counts are also asserted in `tests/test_rate_limit.py` against
a stubbed adapter, where the stub records every call it would have made to Qloo.
`tests/test_mode_budgets.py` covers the split itself: a fixture audit that
produces a full report while no socket is opened and no API client is built,
fixture traffic that leaves the live bucket and the live bucket table untouched,
an exhausted live guard that still answers fixture requests, refusal text that
names the right mode, and a static check that the page's only timer is the
countdown and that the countdown sends nothing.

A real-key live audit was verified in the separate local development workspace,
but its detailed Qloo-derived results are not included in this clean copy until
the applicable Qloo data-use terms are reviewed.

## Recommended path: the included Dockerfile

A container host that runs a long-lived process is the simplest route. The
`Dockerfile` copies only `second_opinion/` and `web/`; it does not include
`data/`, `.env`, tests, reports, or local artifacts.

```bash
docker build -t second-opinion .
docker run --rm -p 8000:8000 --env-file .env second-opinion
```

or, on a platform with secret storage, set `QLOO_API_KEY` as a runtime secret.
Never bake the key into the image or pass it as a build argument, and prefer the
platform's environment/secrets UI over a shell argument so the value does not land
in shell history.

`--env-file` in the example above reads a local untracked `.env`; it is a local
convenience only. In a container or on a host, the key comes from the platform.

### Free-tier container host: what to set (Render as the example)

Verified against Render's documentation while preparing this copy; **no account was
created and nothing was deployed.**

| Setting | Value | Why |
| --- | --- | --- |
| Source | the published repository, build with the included `Dockerfile` | Render builds Dockerfiles for web services; no build command to invent. |
| Runtime port | the platform's `PORT`; Render's default is `10000` | `server.py` reads `PORT` at startup. Confirmed in a local container: with `PORT=10000` the app answered on 10000. |
| Bind address | `HOST` default `0.0.0.0` | Already the image default. |
| Health check path | `/api/health` | Returns `200 {"ok": true}` with no key and no data, which is what a platform probe needs. |
| Environment | `QLOO_API_KEY` as a **secret** in the service's Environment Variables section (Advanced, or Settings after creation) | Values are read from the environment only; nothing is committed. `SO_MODE` can be left unset — live is the default. |
| Audit budget | `SO_AUDIT_PER_MINUTE`, `SO_AUDIT_BURST`, `SO_MAX_CONCURRENT_AUDITS` as **non-secret** environment variables, or leave them unset for 6/minute, burst 3, 2 in flight | These are what stop one visitor from spending your Qloo quota, and they are the **live** budget: only live audits reach Qloo. A live deployment needs no `SO_FIXTURE_AUDIT_*` setting — a `?mode=fixture` request there draws on its own separate budget and fails on the missing `data/fixtures`, so it can waste local work but never quota. The defaults are a demo budget, not a limit tuned for a panel of judges — see sizing below. |
| Client identity | nothing to set | The limit keys on the **rightmost** `X-Forwarded-For` entry, falling back to the peer address. A proxy that replaces the header and a proxy that appends to it both put the real client last, so taking the last entry is right under either behavior and a client-supplied prefix buys nothing. Run it exposed directly, with no proxy, and every visitor shares one bucket — that fails closed, not open. |
| Expect on free | idle instances spin down after roughly 15 minutes without traffic, and the first request after that waits for a cold start | Render documents free-tier spin-down; a cold start plus a live Qloo call is the slowest path in the demo. The same spin-down **resets the audit budget** for everyone, because the buckets live in process memory. |

### Sizing the audit budget against your quota

The unit to think in is Qloo requests, not page views: one audit is three of
them (`/search`, the seed record, the candidate set), so the default
6/minute/client is up to 18 Qloo requests per client-minute, and a page left
open in a loop cannot exceed that.

Each of those requests carries a 12-second timeout (`LiveAdapter(timeout=...)`),
so a single audit can take up to about 36 seconds when Qloo is slow, and
`SO_MAX_CONCURRENT_AUDITS=2` is what keeps three stalled visitors from becoming
nine open connections on a small host. Raising the in-flight cap raises both the
concurrent Qloo calls and the memory pressure of the reports; on a free tier,
leave it at 2.

For a review panel, count the people who will click at once and give each of
them a few audits per minute — `SO_AUDIT_PER_MINUTE=10 SO_AUDIT_BURST=5` for
five judges is up to 150 Qloo requests per minute in the worst case, which is a
number worth checking against your key's plan before setting it, not after. For
a public demo where strangers may arrive, keep the defaults or go lower.

Only the live budget enters that arithmetic. A fixture audit opens no socket and
builds no API client, so `SO_FIXTURE_AUDIT_PER_MINUTE`, `SO_FIXTURE_AUDIT_BURST`
and `SO_FIXTURE_MAX_CONCURRENT_AUDITS` are sized against CPU and disk reads, not
against quota: on a deployed instance with no `data/fixtures/` those requests
stop at the missing directory, and on a local fixture demo they cost a parse and
a report build. The default 30/minute per client, burst 10, 4 in flight exists to
stop one loop from occupying a small box; nothing about a live deployment's quota
depends on changing it.

Neither `SO_AUDIT_PER_MINUTE` nor the gate is a substitute for knowing your
quota: the limit bounds a single client's rate and a single instance's
concurrency, and the ceiling on total spend is time × clients × rate. What your
key is actually allowed is a fact only Qloo can give you — this repository does
not assert a request limit or a usage dashboard exists. Set the budget from the
terms attached to your credential, and remember that revoking or rotating the
key takes effect on the next request: the app reads it from the environment and
caches nothing on disk.

Not verified here, and the operator should check the current plan pages before
relying on it: free-tier RAM/CPU, monthly hours, bandwidth limits, and whether
free web services are still offered in your region. None of them change the
configuration above.

## Minimum steps to a public demo

1. Publish this branch to the GitHub repository, then create a web service from it
   using the included `Dockerfile` (or any host that runs a long-lived process with
   a `PORT`).
2. Set `QLOO_API_KEY` as a runtime secret in the platform's environment settings.
3. Decide the audit budget for the audience you expect — see "Sizing the audit
   budget" above — and set `SO_AUDIT_PER_MINUTE`, `SO_AUDIT_BURST` and
   `SO_MAX_CONCURRENT_AUDITS` as non-secret variables, or accept the defaults.
   These are the live knobs, and live is the only mode that spends anything; the
   `SO_FIXTURE_AUDIT_*` family bounds local fixture reads and needs no setting on
   a live deployment.
4. Configure the health check path as `/api/health`.
5. Let the platform assign the port; the app binds `$PORT`.
6. Confirm from outside: `GET /api/seeds` returns 8 seeds with
   `"live_ready": true`, then `GET /api/audit?seed=Laufey&mode=live` returns
   `ok: true`.
7. Confirm the protection is live: repeat that audit until the budget runs out
   and check that you get `429` with a `Retry-After` header and a `limit` block,
   and that `/api/seeds` keeps answering `200` throughout.
8. Open the page, pick a seed, expand two candidates, and check that receipts
   say "Every shared term above was re-derived from the source payload."
9. Only then call the URL a working live demo.

Nothing here has been executed against a hosting provider in this clean-copy
preparation, so no deployment claim is made.

## Remaining deployment blockers

- No GitHub repository or hosting service has been created from this copy, and no
  push has been performed; the operator must authorize and run those steps.
- No public demo URL exists.
- This copy has never been run with a real credential: its live path is exercised
  against stubs and its no-key behavior is verified, so a hosted `ok: true` audit
  is still an untested claim for this tree.
- Qloo-derived outputs from a credentialed run (per-pair states, affinities,
  calibration numbers) stay out of the documentation until the applicable terms
  are reviewed.

## What this copy excludes, and why

No captured Qloo API responses, no API-returned entity identifiers, no affinity
values or per-pair results, and no derived reports are shipped here. Permission to
redistribute that material has not been confirmed, so the publication rule applied
is: ship the code that *calls* the API, not what the API *returned*. Tests that
need captured responses skip with a stated reason instead of failing.
