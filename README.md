# Second Opinion — a Qloo recommendation evidence auditor

Qloo returns a ranked list of artists for a seed artist, each with an affinity
score. This tool answers a narrower question about each of those
recommendations: **is the recommendation backed by descriptive metadata that can
be pointed at, or is affinity the only thing there?**

For every candidate it shows four dimension receipts — genres, lyrical themes,
audience, style — each with the exact source field in the payload returned for
that audit, the shared terms, and an overlap coefficient. It then emits one
discrete state:

| State | Meaning |
| --- | --- |
| `CORROBORATED` | At least 2 of 4 dimensions meet the 0.30 threshold. |
| `AFFINITY_ONLY` | Affinity exists, but fewer than 2 dimensions meet the threshold. |
| `UNVERIFIABLE` | A precondition is missing: no seed/candidate metadata, no affinity, fewer than 2 comparable dimensions, or a receipt that failed re-derivation. |

Before any result is shown, the agent re-derives every receipt from the payload
returned for that audit. A receipt that cannot be reproduced voids that
candidate's audit.

**What this does not do.** It does not score recommendation quality, does not
produce a composite number, and does not claim that shared tags mean shared
fans, demand, attendance, ticket sales or revenue. It audits whether Qloo's own
descriptive metadata supports a pair, nothing more.

## Quick start

Python 3.9+, standard library only. No installation step.

```bash
python3 -m second_opinion.server          # http://localhost:8000
PORT=9000 python3 -m second_opinion.server
```

Command line:

```bash
python3 -m second_opinion.cli seeds
python3 -m second_opinion.cli audit "Fontaines D.C."
python3 -m second_opinion.cli audit "Bad Bunny" --json --threshold 0.30
```

## Modes

**Live mode** is the default. It calls the Qloo API using `QLOO_API_KEY`. A
directly launched server or CLI also loads the project-root `.env`, but a real
environment variable always takes precedence over the file value. The loader never
prints or logs a value and returns only the count of variables it set; what it
does is copy missing entries into the process environment, which is the only place
the API client reads the key from.

```bash
python3 -m second_opinion.server                       # reads ./.env, http://localhost:8000
python3 -m second_opinion.cli audit "Fontaines D.C."   # live by default
export QLOO_API_KEY=...                                # or export it; overrides ./.env
```

Docker images do not contain `.env`; a container receives the key at runtime,
either from the platform's secret store or from a local `--env-file` command.
The key must never be baked into an image or passed as a build argument.

**Fixture mode** (`SO_MODE=fixture`, or `--mode fixture` on the CLI) reads saved
responses from a local `data/fixtures/` directory. That directory is intentionally
not present in this clean publication copy. The fixture tests skip when it is
absent, and live mode never silently falls back to fixtures. With no key,
`/api/audit` returns HTTP 503.

## Seed catalogue

`second_opinion/catalog.py` contains eight hand-written artist search names and
a short editorial note for each. The public seed list contains no entity IDs,
tag counts, affinities, or captured payloads. Selecting a seed sends the name to
`/api/audit`, which resolves it through `GET /search` and audits the live
recommendation set through `GET /v2/insights`.

The live request path is:

```text
GET /search           query, types=urn:entity:artist, take
GET /v2/insights      filter.type, filter.results.entities for the seed record
GET /v2/insights      filter.type, signal.interests.entities, take for candidates
X-Api-Key             credential header
```

The seed record is fetched by `filter.results.entities` and checked against the
resolved entity ID. This is necessary because `signal.interests.entities`
returns recommendations, not the seed itself.

## Audit endpoint limits

One live audit costs **three Qloo requests** — search, seed record, candidate
set — and the endpoint is open to any visitor. Without a bound, a single page
left refreshing in a loop spends the operator's quota. So `/api/audit` carries
two in-process limits from `second_opinion/throttle.py`, and every check runs
*before* the API client is built, which means a refused request costs nothing:

| Limit | Default | What it does |
| --- | --- | --- |
| Per-client token bucket | 6 audits/minute, burst 3 | Past the burst, the response is `429` with `Retry-After` and a JSON body explaining the budget. |
| Audits in flight | 2 | One per client is not enough: this caps simultaneous upstream calls at roughly `2 × 3` so a few slow Qloo responses cannot pile threads onto a small instance. Busy responses are `503` with `Retry-After: 2`. |
| Seed length | 120 characters | Longer input is `400` and never reaches Qloo. Real artist names are far shorter; this only bounds what one request can put on the wire. |

`/api/seeds`, `/api/rules`, `/api/health` and the page itself are
hand-authored or static, cost nothing, and are deliberately not throttled — a
judge can reload them freely. The CLI is a single-user local tool and is not
throttled either. A refused audit is not a dead end in the interface: the page
shows the status, the budget in force and the wait before the next one is
accepted, rather than a spinner that never finishes.

Defaults are settable without touching code, through `SO_AUDIT_PER_MINUTE`,
`SO_AUDIT_BURST` and `SO_MAX_CONCURRENT_AUDITS`; a missing, non-numeric or
zero/negative value keeps the default, and an oversized value is clamped
(a burst above the steady rate is reduced to it, so the limit cannot be
switched off by configuration).

Three limitations are stated plainly because they matter for a hosted demo:
the buckets are **in memory**, so a restart, a redeploy or an idle spin-down
hands every client a fresh burst; they are **per instance**, so more instances
multiply the allowance; and the client is identified by the **rightmost** entry
of `X-Forwarded-For`, because that entry alone is the proxy's own record —
anything to its left is text the visitor typed. A proxy that replaces the header
and a proxy that appends to it both put the real client last, so the last entry
is right under either behavior. No IP is stored: buckets are keyed by a
truncated digest.

## Failure behavior

| Condition | Behavior |
| --- | --- |
| No `QLOO_API_KEY` | HTTP 503; the response names `QLOO_API_KEY` but never a credential value |
| Credential rejected | refusal code `authentication failed` |
| Request timeout | refusal code `upstream timeout` |
| Non-JSON response | `MalformedResponse`, surfaced as a refusal or HTTP 502 |
| No search match | refusal code `seed resolution failed` |
| Seed has no usable scored metadata | refusal code `insufficient seed metadata` |
| Receipt cannot be re-derived | candidate state becomes `UNVERIFIABLE` and the audit is void for that candidate |
| Seed longer than 120 characters, or blank, or an unknown `mode`, or a non-numeric threshold | HTTP 400, no Qloo request, no limit token spent |
| Per-client budget exhausted | HTTP 429 with `Retry-After`; nothing is sent to Qloo |
| Too many audits in flight | HTTP 503 with `Retry-After: 2`; nothing is sent to Qloo |
| An unexpected error inside the audit path | HTTP 500 `internal audit error`; the traceback goes to the server log, never to the client |

Every JSON response is scanned once for the credential value before it is
written. Adapter messages are built from text that never includes the key, so
the scan should never fire; it is the net under that guarantee rather than a
substitute for it. An error response carries a code and a short message, not an
upstream body. The key itself travels only in the `X-Api-Key` header, so it can
never appear in a URL or in an access log — those lines are additionally capped
at 300 characters, because the request target is client text.

## Method

The four scored dimensions are:

| Dimension | Source fields |
| --- | --- |
| genres | `properties.primary_genres[]` + `properties.secondary_genres[]` |
| themes | `properties.lyrical_themes[]`, falling back to `tags[type=urn:tag:theme:qloo].name` |
| audience | `tags[type=urn:tag:audience:qloo].name` |
| style | `tags[type=urn:tag:style:qloo].name` |

`properties.instruments` and `properties.influenced_by` are display-only. They
are not scored because they can be non-discriminative or too sparse to compare.

Normalization is deterministic and spelling-only: Unicode NFKD with combining
marks removed, lowercase, `&` → `and`, `/`, `-`, `_` → space, drop remaining
punctuation, collapse whitespace. No stemming, no stopwords, no synonyms.

Comparison is the overlap coefficient:

```text
|A ∩ B| / min(|A|, |B|)
```

over normalized, de-duplicated sets. Jaccard was rejected because it penalizes
an entity for having a richer metadata entry rather than measuring the
relationship itself.

The decision rule is a pure function over dimension statuses. The frozen
thresholds are in `second_opinion/config.py`: strong overlap 0.30, at least two
evaluable dimensions, and at least two corroborated dimensions for
`CORROBORATED`.

## Blanket dimensions

A dimension that corroborates for every candidate in a seed result is not
discriminating within that result set. `/api/audit` reports this with
`dimension_summary.<dimension>.is_blanket`, and the UI exposes it instead of
hiding it. This rule is retained even when live results look favorable.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Verified suite, both runs with `QLOO_API_KEY` and `SO_MODE` unset:

| Input present | Result |
| --- | --- |
| Clean publication copy — no `data/`, no `.env`, no captured responses | 192 tests: 135 passed, 0 failed, 0 errors, 57 skipped, exit code 0 |
| The same copy with a local `data/fixtures/` directory temporarily available | 192 tests OK, 0 skips, exit code 0 |

The 57 skips are the fixture-dependent tests: they compare real payloads field by
field, and the payloads they read are Qloo API responses that stay private and are
not published with this repository. A public checkout therefore runs 135 tests and
skips those 57 with the reason

```text
captured Qloo fixtures are not present in this checkout
```

The skips are a missing-input condition, not a failure: the same 192 tests run
with 0 skips wherever the captured responses are available.

Coverage includes receipt re-derivation, fabricated shared terms, normalization,
duplicate collapse, audit states and boundaries, live adapter error mapping,
key-header behavior, secret non-disclosure, static seed catalogue behavior,
mode handling, `.env` loader precedence, clean-checkout HTTP routes, and the audit
throttle: bucket arithmetic and refill under an injected clock, per-client
isolation, the `X-Forwarded-For` rule that ignores client-supplied prefixes,
refused requests making zero upstream calls, the in-flight gate, `Retry-After`,
oversized-input rejection, and the redaction scan over error and refusal bodies.

## Deployment

**Not deployed — no public demo URL exists.** Nothing from this copy has been
pushed to a hosting provider and no live instance is running. The `Dockerfile`
builds a code-only image that runs locally; a public live demo additionally needs
a container host and `QLOO_API_KEY` set in that host's environment settings, never
in the image. Build and start configuration, port handling and the exact dashboard
steps are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Repository layout

```text
second_opinion/     product code: config, normalization, extraction, audit,
                    agent, adapters, CLI, environment loader, audit throttle,
                    HTTP server
web/index.html      single-page interface, no build step
tests/              test suite above
docs/DEPLOYMENT.md  hosting and verification notes
citytaste/          retained metrics utilities used by the test suite
Dockerfile          code-only container image
LICENSE             MIT license
```

## Qloo data-use note

This repository ships application code, tests, documentation and the frontend
only. It contains no captured Qloo API responses, no entity identifiers returned
by the API, no affinity values or per-pair results, and no audit output from a
credentialed run; tests address entities through synthetic placeholder ids.

What is documented here is the interface, not the data: the request parameters,
the response field names the auditor reads, and the rules the auditor applies to
them. The seed names in `second_opinion/catalog.py` are hand-written artist
search queries, not published Qloo entities.

Permission to redistribute Qloo response data has **not** been established, and
nothing in this repository claims otherwise: the terms that would settle it could
not be verified from the public documentation available when this copy was
prepared. The conservative rule applied here is therefore — publish code that
*calls* the API, keep anything the API *returned* out of the repository. Whoever
runs this with their own key is responsible for the terms attached to it.

## Limitations

- The tool audits metadata overlap only. It does not audit audience overlap,
  demand, commercial performance, or recommendation quality.
- Metadata coverage is uneven; where a dimension is missing on either side, the
  receipt is `unverifiable` and the limitation names the dimension.
- Affinity values are displayed as returned by Qloo; the auditor does not
  recompute or reinterpret them.
- A live audit depends on an operator-provided `QLOO_API_KEY` and on the current
  Qloo API behavior.
- One live audit costs three Qloo requests, so a hosted demo spends the
  operator's quota on other people's clicks. The audit budget is what bounds
  that, not what authorizes it.
- The audit budget is in-memory, per-instance state: a restart, a redeploy or a
  free-tier idle spin-down refills every bucket, and running two instances
  doubles the allowance. It is a quota guard, not access control — the demo has
  no sign-in, so anyone within budget can run an audit.
- The clean-copy audit verifies local no-key behavior, not a hosted deployment.

## License

MIT. See `LICENSE`.
