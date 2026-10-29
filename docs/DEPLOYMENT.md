# Deployment notes

Status: **not deployed.** No public URL exists, no hosting account or credential
was used for this clean-copy preparation, and no push or deployment was
performed.

The application defaults to live mode and its seed list is hand-authored, so a
deployment target with a runtime `QLOO_API_KEY` can run without captured Qloo
response files.

## Application profile

- One long-running Python process, standard library only, Python 3.9+.
- No build step, no database, no external assets; `web/index.html` is served by
  the same process.
- Reads `HOST` (default `0.0.0.0`) and `PORT` (default `8000`).
- Reads `SO_MODE` (default `live`; set `fixture` only for local testing).
- `GET /api/health` returns `{"ok": true}` for platform health checks.
- Stateless per request; nothing is written to disk.

This fits any container host or a plain VM. A serverless/edge platform does not
fit without a rewrite because the server is `ThreadingHTTPServer`, not a request
handler.

## Environment variables

| Variable | Required | Default | Notes |
| --- | --- | --- | --- |
| `QLOO_API_KEY` | yes, for a working live demo | none | Secret. Set it in the platform's secret store, never in the image, repo, logs or frontend. The server reports only whether a key is present. |
| `QLOO_API_HOST` | no | `https://hackathon.api.qloo.com` | Hackathon keys are documented to work against this host. |
| `SO_MODE` | no | `live` | `live` calls Qloo. `fixture` reads local captures and is for development only. There is no automatic fallback: with `live` and no key, audits return `503`. |
| `PORT` | no | `8000` | Most platforms inject this. |
| `HOST` | no | `0.0.0.0` | Keep the default in containers. |

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

The clean copy was verified without a real API key and without captured
fixtures:

| Request | Observed |
| --- | --- |
| `GET /api/health` | `200 {"ok": true}` |
| `GET /api/seeds` | `200`, `mode: "live"`, `live_ready: false`, 8 static seeds |
| `GET /api/audit?seed=Laufey` | `503`, error names `QLOO_API_KEY` |
| `GET /api/audit?seed=Laufey&mode=live` | `503`, no fixture fallback |

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
Never bake the key into the image or pass it as a build argument.

## Minimum steps to a public demo

1. Create a service from the published repository using the included
   `Dockerfile`, or any host that runs a long-lived process with a `PORT`.
2. Set `QLOO_API_KEY` as a runtime secret.
3. Configure the health check as `GET /api/health`.
4. Expose the platform's `PORT`.
5. Confirm from outside: `GET /api/seeds` returns 8 seeds with
   `"live_ready": true`, then `GET /api/audit?seed=Laufey&mode=live` returns
   `ok: true`.
6. Open the page, pick a seed, expand two candidates, and check that receipts
   say "Every shared term above was re-derived from the source payload."
7. Only then call the URL a working live demo.

Nothing here has been executed against a hosting provider in this clean-copy
preparation, so no deployment claim is made.

## Remaining deployment blockers

- No GitHub repository or hosting service has been created from this copy.
- No push, deployment, or public URL has been performed.
- A real-key live audit still needs operator review of the Qloo-derived outputs
  before publishing them in documentation.

## Why fixtures and reports are not shipped

Captured Qloo responses, derived reports, stale clean-checkout exports, entity
IDs, and detailed live audit outputs are excluded from this copy. Their
redistribution permission has not been confirmed. The conservative publication
rule used here is: ship the application code and tests with synthetic identifiers,
not saved API responses or derived analyses.
