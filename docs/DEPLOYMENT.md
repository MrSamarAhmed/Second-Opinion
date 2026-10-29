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
- `GET /api/demo` runs the same agent over records written by hand into
  `second_opinion/demo.py`. It opens no socket, reads no `data/`, never looks at
  `QLOO_API_KEY`, draws on neither audit budget, and answers `mode: "synthetic"` —
  which is what makes it safe to leave reachable on a deployed instance and the
  reason a demonstration does not depend on a credential or on captured Qloo data.
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
| `QLOO_API_HOST` | no | `https://hackathon.api.qloo.com` | The official developer guide states that "Hackathon API keys only work against `https://hackathon.api.qloo.com`", so the default is the event host. Set it to whichever host your credential was issued for; the client reads this variable and no other endpoint is assumed. |
| `SO_MODE` | no | `live` | `live` calls Qloo. `fixture` reads local captures and is for development only. There is no automatic fallback: with `live` and no key, audits return `503`. |
| `PORT` | no | `8000` | Injected by the platform. Render's default is `10000`; the server binds whatever `PORT` says (verified in-container), so no code change is needed. |
| `HOST` | no | `0.0.0.0` | Keep the default in containers; binding `127.0.0.1` makes the service unreachable from outside. |
| `SO_AUDIT_PER_MINUTE` | no | `6` | **Live audits only** — this is the quota guard. Sustained audits per client per minute. One live audit costs three Qloo requests, four when the agent's single follow-up re-read fires, so `6` means up to 18–24 requests per client-minute. A non-numeric, zero or negative value keeps the default; above 600 it is clamped. |
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

Verified on 2026-10-29 in this publication copy. Every row below runs with no
captured responses present and, unless the row says otherwise, with
`QLOO_API_KEY` unset; the browser rows and the probe sweep answer from synthetic
payloads written by the harness, not from Qloo.

| Check | Observed |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | 270 tests: 213 passed, 0 failed, 0 errors, 57 skipped (missing captured responses), exit 0 |
| Same suite with `data/fixtures/` temporarily made available | 270 tests OK, 0 skips — neither audit budget interferes with fixture audits |
| `GET /api/health` | `200 {"ok": true}` |
| `GET /api/seeds` | `200`, `mode: "live"`, `live_ready: false`, 8 static seeds |
| `GET /` | `200`, `text/html`, the single-page frontend |
| `GET /api/demo?scenario=improves` with no key and no `data/` | `200`, `ok: true`, `mode: "synthetic"`, `demo.entity_lookups: 2`, `demo.seed_queries: 1`, `investigation.follow_up_queries_used: 1`, outcome direction `improved` |
| `GET /api/demo?scenario=nope` | `400` naming the two valid scenarios |
| Three `GET /api/demo` calls, then a live audit | the live audit still fails with `QLOO_API_KEY is not set`, not as a rate limit — demonstrations charge neither budget |
| `GET /api/audit?seed=Laufey` | `503`, error names `QLOO_API_KEY` |
| `GET /api/audit?seed=Laufey&mode=live` | `503`, no fixture fallback |
| Four live audits from one client with `SO_AUDIT_PER_MINUTE=2 SO_AUDIT_BURST=2 SO_MAX_CONCURRENT_AUDITS=1` | `503`, `503` (no key), then `429` twice with `Retry-After: 30`, `error: "live audit rate limit reached; try again later"` and a `limit` block naming `mode: "live"` and `spends_quota: true` |
| A **fixture** audit from the same client whose live budget is spent | `503`, `{"ok": false, "error": "no fixture data: a fixture audit needs a search/ and a pair/ directory inside the fixture root (default: data/fixtures)", "mode": "fixture"}` — never `429`, so the live guard cannot starve local reads. The message names what is missing, not the absolute directory the server runs from |
| A live audit from a different `X-Forwarded-For` while the first client is rate-limited | `503` (no key), not `429` — the bucket is per client, not global |
| Ten `GET /api/seeds` while the audit budget is exhausted | all `200` — only the endpoint that spends quota is limited |
| `GET /api/audit` with a 200-character seed, three times | `400`, `seed is too long; the limit is 120 characters` — and the two valid audits right after it still returned `503` (no key) with the third returning `429`, so a rejected request spent no budget at all |
| Container run with `PORT=10000 SO_AUDIT_PER_MINUTE=2 SO_AUDIT_BURST=2` | inside the image, for one client: `503`, `503`, then `429` twice with `Retry-After: 30` and `code: "rate_limited"`; a 200-character seed returns `400`; `/api/seeds` keeps answering `200`; and a `mode=fixture` audit from that same rate-limited client returns `503 {"ok": false, "error": "no fixture data: a fixture audit needs a search/ and a pair/ directory inside the fixture root (default: data/fixtures)", "mode": "fixture"}` — not `429`, and no container path disclosed |
| The page in a browser, fixture mode, `SO_FIXTURE_AUDIT_PER_MINUTE=1 SO_FIXTURE_AUDIT_BURST=1` | the first seed renders a full report (candidates, receipts verified, blanket flag, agent trace); once the fixture budget is spent the next click renders "Audit could not run — Little Simz — fixture audit budget reached; try again shortly (no Qloo request is made in fixture mode)" with the facts `mode = fixture`, `http status = 429`, `fixture budget = 1/min, burst 1`, `spends Qloo quota = no, local read`, and a countdown that ticks in text ("Next fixture audit from this client accepted in about 48 seconds." → "…about 32 seconds." fifteen seconds later) while the page's request total stays constant at 3 — it never retries on a timer |
| Server started with `SO_AUDIT_PER_MINUTE=0 SO_FIXTURE_AUDIT_PER_MINUTE=-5 SO_FIXTURE_AUDIT_BURST=999` | banner reports the live defaults `6/minute per client, burst 3, 2 in flight` and the fixture defaults `30/minute … 4 in flight`, with the oversized fixture burst clamped down to its own rate (`burst 30`) — a zero or a negative value cannot switch a budget off, and a huge one cannot switch it open |
| Startup banner | `live audit limits:    6/minute per client, burst 3, 2 in flight — three Qloo requests each, four when the follow-up fires (in-memory, reset on restart)` and `fixture audit limits: 30/minute per client, burst 10, 4 in flight — local reads, no Qloo request (in-memory, reset on restart)`, above `Second Opinion listening on http://0.0.0.0:<port>  (mode=live, no QLOO_API_KEY: audits will refuse)` — re-read from a real start on 2026-10-29 |
| `docker build` | succeeds; `/app` contains only `second_opinion/` and `web/`, no `.env`, no data files, runs as uid 10001 |
| `docker run -e PORT=10000` | the app answers `/api/health`, `/api/seeds` and `/` on 10000 and still returns `503` for a live audit with no key |
| 80 HTTP probes against the real server with a stubbed live adapter and synthetic payloads (swept before the follow-up landed, so the upstream counts there are the three-read, no-gap case; the fourth request is covered by `tests/test_investigation.py` instead) | `200` for a well-formed audit (three upstream reads, receipts re-derived), `400` for each of 14 malformed or absent inputs before any budget is spent, `503` naming `QLOO_API_KEY` with no key, `200` refusals carrying `code` `authentication failed` / `upstream timeout` / `no results` / `upstream error` with `candidates: []` and a terminating `refuse` trace step, `502` for an unmapped adapter failure, `503 audit_busy` with `Retry-After: 2` when the in-flight gate is full, `429` with the right mode once each budget is spent, and `500 {"ok": false, "error": "internal audit error"}` for an unexpected exception while the 933-character traceback stayed on stderr |
| Sentinel sweep over every probe response body | zero hits for a synthetic credential, `Traceback`, `'File "` and `password` — an upstream message that quoted the credential arrived as `the key was [credential redacted]` |
| `GET /api/audit?seed=Laufey&mode=fixture` in this checkout (no `data/`) | `503 {"ok": false, "mode": "fixture", "error": "no fixture data: …"}` — the same request before the fix also carried the server's absolute working directory, which is what the added regression test now forbids |
| The page in a browser, slow first audit then a fast second click | before the fix the panel re-rendered the earlier seed while the sidebar highlighted the later one; with the per-click ticket in place the stale response is dropped and the panel agrees with the highlighted seed, and a normal click still renders its own report and trace |
| The page in a browser, synthetic demonstration clicked (keyboard: Tab to the entry, Enter) | the report renders with the banner "Synthetic demonstration — the artists, terms and affinities in this run are invented for the demonstration. They are not Qloo data and this is not a live API result.", a card titled "What the agent did after the first pass" carrying Budget / Gap found / Why / Action / Result / Decision, and a `follow-up · raised` chip on the candidate the agent re-read. `improves` shows `AFFINITY_ONLY → CORROBORATED`; `confirms` holds at `AFFINITY_ONLY` with the decision text "the doubt is settled in the conservative direction" and a `follow-up · held` chip |
| Evidence labels, checked in the DOM rather than by eye | exactly one banner node is ever present, and its label is taken from the response's own `mode`: `synthetic` after a demonstration, `Saved Qloo captures` after a fixture click. No synthetic run is rendered as live evidence, and no live claim appears while `QLOO_API_KEY` is absent |
| The page in a browser after the frontend changes: stale-response guard and countdown re-checked | an artificially delayed first audit was discarded and the panel still agreed with the last seed clicked (request sequence 7 dropped, 8 rendered); a drained fixture budget rendered "Next fixture audit from this client accepted in about N seconds." — labelled **fixture**, never live — ticking down to "Budget refilled — pick a seed to try again." with no further request from the page, and the browser console stayed empty |
| One deliberate live audit through this copy's server, key loaded into the process environment only | `200` after about 4.1 s, `ok: true`, `mode: live`, every candidate's receipts verified, and the body free of the credential, any traceback and any local path. The Qloo-derived content of that report is withheld from this copy, as the note below and the README's Qloo data-use note require. **That audit predates the follow-up**, so the four-request path has never been exercised against Qloo itself — what follows is the closest verified evidence |
| The gap condition measured against the operator's own captured responses, in fixture mode, no key in the environment | across the seven seeds those captures cover (the eighth has no capture there and refuses), every audit reported `investigation.triggered: false` and `candidates_with_gap: 0` — no candidate met "empty on the candidate's side while the seed is not", so those audits stay at three requests and the follow-up never fires. Notably `AFFINITY_ONLY` verdicts do occur in that data; the gap condition is simply rarer than that state, because an unsupported dimension has to be empty on one side and populated on the other. The second look is therefore demonstrated by the synthetic scenarios and by call-count tests, and an honest live demo should expect it to decline |

The refused-request counts are also asserted in `tests/test_rate_limit.py` against
a stubbed adapter, where the stub records every call it would have made to Qloo.
`tests/test_mode_budgets.py` covers the split itself: a fixture audit that
produces a full report while no socket is opened and no API client is built,
fixture traffic that leaves the live bucket and the live bucket table untouched,
an exhausted live guard that still answers fixture requests, refusal text that
names the right mode, a static check that the page's only timer is the
countdown and that the countdown sends nothing, and — since the browser pass —
that each click takes a fresh ticket and that both the response path and the
failure path refuse to render a superseded audit. `tests/test_clean_checkout.py`
additionally drives the shipped server over a copy with no `data/` and asserts
that a fixture request in that state answers `503` in fixture mode without
naming the directory the server runs from, and that the failure costs the live
budget nothing; it also asserts there that `GET /api/demo` still returns a full
report with no key and no captures, that its `mode` is `synthetic`, and that
twenty demonstrations afterwards still leave the live audit failing for the real
reason — a missing credential — rather than as a rate limit.
`tests/test_investigation.py` covers the second look with the adapter's recorded
call sequence as the load-bearing assertion, so an agent that never re-reads and
an agent that re-reads twice both fail it: no gap means no lookup, two gaps still
mean one lookup, only an id already in the candidate set is requested, the
ceiling is clamped and is spent even when the lookup fails, every failure path
returns byte-for-byte what a zero-budget run returned, a re-read that would
manufacture support is discarded by the receipt check, and no part of a report,
a refusal, a crash or its log line can carry the sentinel credential.

A real-key live audit has now been driven through this copy's own server, and it
behaved as the no-key paths say it should. Its Qloo-derived results are still not
reproduced in this clean copy; the applicable Qloo data-use terms have to be
reviewed before per-artist output from a credentialed run is published here.

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

The unit to think in is Qloo requests, not page views: one audit is three of them
(`/search`, the seed record, the candidate set) and **four when the agent's
single follow-up re-read fires**, so the default 6/minute/client is 18 requests
per client-minute in the ordinary case and up to 24 in the worst one, and a page
left open in a loop cannot exceed that. The follow-up is not an extra meter: it
is charged to the audit that already took the token, it happens only when a
named evidence gap exists, and its ceiling is clamped in code at one — a deployed
instance cannot be talked into a second re-read by configuration.

Each of those requests carries a 12-second timeout (`LiveAdapter(timeout=...)`),
so a single audit can take up to about 36 seconds when Qloo is slow, or about 48
when the re-read also waits, and `SO_MAX_CONCURRENT_AUDITS=2` is what keeps
three stalled visitors from becoming nine or twelve open connections on a small
host — the third one is refused with `503 audit_busy` instead of adding sockets.
Raising the in-flight cap raises both the concurrent Qloo calls and the memory
pressure of the reports; on a free tier, leave it at 2.

For a review panel, count the people who will click at once and give each of
them a few audits per minute — `SO_AUDIT_PER_MINUTE=10 SO_AUDIT_BURST=5` for
five judges is up to 150 Qloo requests per minute in the ordinary case and 200 if
every audit takes the follow-up branch, which is a number worth checking against
your key's plan before setting it, not after. For a public demo where strangers
may arrive, keep the defaults or go lower.

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
7. Confirm the demonstration path works on the deployed instance too:
   `GET /api/demo` returns `ok: true` with `mode: "synthetic"`. It needs no key
   and no `data/`, so it is the half of the product a judge can always run — and
   if it does not work, the deployment has lost the agentic demonstration even
   though audits still pass.
8. Confirm the protection is live: repeat that audit until the budget runs out
   and check that you get `429` with a `Retry-After` header and a `limit` block,
   and that `/api/seeds` keeps answering `200` throughout.
9. Open the page, pick a seed, expand two candidates, and check that receipts
   say "Every shared term above was re-derived from the source payload." Then run
   a demonstration and check the card that says what the agent did after the
   first pass, so the live report and the synthetic one are visibly different
   kinds of evidence.
10. Only then call the URL a working live demo.

Nothing here has been executed against a hosting provider in this clean-copy
preparation, so no deployment claim is made.

## Remaining deployment blockers

- No GitHub repository or hosting service has been created from this copy, and no
  push has been performed; the operator must authorize and run those steps.
- No public demo URL exists.
- One credentialed live audit has been driven through this copy's own server and
  returned `ok: true` with every receipt verified, so the live path is no longer
  an untested claim. What is still untested against Qloo itself is the **fourth
  request**: no run available to the authors has met the gap condition, so the
  follow-up branch has been exercised only against stubs and the synthetic
  demonstrations.
- Qloo-derived outputs from a credentialed run (per-pair states, affinities,
  calibration numbers) stay out of the repository, as the official guide requires.

## What this copy excludes, and why

No captured Qloo API responses, no API-returned entity identifiers, no affinity
values or per-pair results, and no derived reports are shipped here. The official
developer guide states the rule directly — "Do not store Qloo response data in a
public repository — that makes the data publicly accessible outside the terms of
your API key" — while allowing private server-side caching with no time limit. So
the publication rule applied here is the rule the event states: ship the code that
*calls* the API, not what the API *returned*. Tests that need captured responses
skip with a stated reason instead of failing, and the synthetic demonstrations
exist so that a clean checkout can still show the agent working.
