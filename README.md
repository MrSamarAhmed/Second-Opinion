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

## When the evidence is thin, it looks once more

Two very different situations produce the same payload, and the record in hand
cannot tell them apart: either the two artists genuinely share nothing in a
dimension, or the copy that arrived inside the recommendation list simply says
nothing there. A dimension whose candidate side is empty while the seed side is
not is therefore *unproven*, not *empty* — and an `AFFINITY_ONLY` or
`UNVERIFIABLE` verdict resting on such a dimension is a verdict with a named hole
in it.

So the agent takes one bounded second look. It picks the first candidate whose
verdict is one of those two states, that carries an affinity number, and whose
audit is not void, and re-reads **that entity's own record, requested by entity
id** — the same documented lookup the seed record is read with, not a new
endpoint, header or parameter — then re-runs the comparison and re-derives the
receipts against the same rules.

What is bounded, and how it behaves when it fails:

- **One lookup per audit**, enforced in code by `MAX_FOLLOW_UP_QUERIES`, on the
  path that succeeds and the paths that do not. A second candidate with the same
  gap is reported as sharing the gap, not investigated.
- **The budget is spent before the request is made**, so a timeout, an
  authentication failure or an empty result cannot be retried into a fourth and
  fifth upstream call.
- **The verdict may improve, hold, or move in the cautious direction.** An entity's
  own record can carry *less* than the list copy did; when it does, the state is
  lowered rather than left alone.
- **Nothing is invented to fill a gap.** If the re-read fails, returns nothing,
  or fails receipt verification, the first-pass verdict stands and the trace says
  which of those happened.
- **The affinity number is never re-read as evidence of overlap.** The follow-up
  supplies metadata only; direct term evidence and Qloo's affinity signal stay in
  separate columns.

The trace carries the whole decision — `identify_gap`, `plan_follow_up`,
`follow_up_query`, `verify_receipts`, `investigation_outcome` — and the interface
turns it into a card that states the gap, the reason one more query could reduce
it, the action taken, the result and the final call. The second look is auditable
rather than implied.

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
`/api/audit` returns HTTP 503. Fixture mode opens no socket and builds no API
client — see [Audit endpoint limits](#audit-endpoint-limits), where it also has
its own request budget.

## Demonstrations with no key and no captures

`GET /api/demo` runs the **same agent** over cultural records written by hand
into `second_opinion/demo.py` — invented artists, invented terms, invented
affinity numbers — through a `SyntheticAdapter` that opens no socket, reads no
`data/fixtures`, and never looks at `QLOO_API_KEY`. It exists because the
interesting behaviour above only appears when the agent runs, and a clean
checkout has no captures while a judge's laptop has no credential.

```bash
curl 'http://localhost:8000/api/demo?scenario=improves'   # the re-read closes the gap
curl 'http://localhost:8000/api/demo?scenario=confirms'   # the re-read holds the verdict
```

The response is an ordinary audit report, so it carries the same receipts, the
same state rule and the same trace, plus `mode: "synthetic"` and a `demo` block
reporting the scenario, where the evidence came from, and the adapter's own call
counts (one seed query, two entity reads — the seed's record and the followed-up
candidate's). An unknown scenario is `400` with `use improves or confirms`.

The page lists both under a heading that says *Demonstrations · synthetic*, and
the evidence banner above their report reads "Synthetic demonstration — the
artists, terms and affinities in this run are invented for the demonstration.
They are not Qloo data and this is not a live API result." The banner is chosen
from the response's own `mode`, and the demonstrations draw on neither audit
budget, so a synthetic run can never be rendered as live evidence and can never
consume the quota a real audit needs.

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

The follow-up uses that same first line of the record-reading path one more time
— `filter.results.entities` set to the candidate's id, `take=1`, the returned
`entity_id` checked against the request — so the fourth request in an audit is a
repeat of a call this project already makes, not new API surface.

## Audit endpoint limits

One live audit costs **three Qloo requests** — search, seed record, candidate
set — **four when the agent's single follow-up re-read fires**, and the endpoint
is open to any visitor. Without a bound, a single page left refreshing in a loop
spends the operator's quota. So `/api/audit` carries in-process limits from
`second_opinion/throttle.py`, and every check runs *before* the API client is
built, which means a refused request costs nothing. The follow-up is charged to
the audit that is already inside this budget: it draws no token of its own and
cannot become a second metered request.

There is **one budget per mode**, because the two modes do not cost the same
thing. A fixture audit reads `data/fixtures/**` from local disk and sends no
request anywhere; charging it to the quota guard both blocked local work and
told the visitor their Qloo budget was spent when nothing had been spent:

| Budget | Default | What it protects |
| --- | --- | --- |
| Live, per client | 6 audits/minute, burst 3 | The operator's Qloo quota. Past the burst the response is `429` with `Retry-After` and a body naming the budget. |
| Live, in flight | 2 | Caps simultaneous upstream calls at roughly `2 × 4` in the worst case (three reads plus one follow-up re-read), so slow Qloo responses cannot pile threads onto a small instance. Busy is `503` with `Retry-After: 2`. |
| Fixture, per client | 30 audits/minute, burst 10 | Local work only: the JSON parse and report build. It spends no quota, so it is sized generously and refused requests still cost nothing upstream. |
| Fixture, in flight | 4 | A loop cannot occupy the instance with more than four local audits. |
| Seed length | 120 characters | Longer input is `400` in either mode and never reaches an adapter. |
| Follow-up lookups | 1 per audit | `MAX_FOLLOW_UP_QUERIES` in `agent.py`. It is not an environment variable, and the constructor clamps any request to it, so no caller can raise it; the budget is charged before the lookup is made, so failure paths spend it too. |

Exhausting one budget leaves the other alone: after the live quota guard has
refused a client, that client can still browse fixtures, and a page running
fixture audits all day never empties the live bucket. Nothing falls open either:
`/api/audit` rejects an unrecognised `mode` with `400` before spending anything,
and the lookup that chooses a budget, `throttle_for_mode()`, answers an unknown
mode with the *live* bucket rather than the generous one — so a future caller
that passes a mode the throttle does not know does not inherit a free pass.

`/api/seeds`, `/api/rules`, `/api/health`, `/api/demo` and the page itself are
hand-authored, static, or synthetic, cost the operator nothing, and are
deliberately not throttled — a judge can reload them freely and run the
demonstrations as often as they like. `/api/demo` is the one that needs the
judgement stated: it is not metered because it reaches no network and reads no
file, but it does run the real agent over a small fixed payload, so it is CPU
work rather than free. Drawing on neither audit budget is what matters for
quota, and that is asserted in tests rather than assumed. The CLI is a
single-user local tool and is not throttled either. A refused audit is not a
dead end in the interface: the page says which budget ran out, whether it spends
Qloo quota, and counts down to the next accepted audit. The countdown only writes
text — the page sends no request on a timer, so a limit can never turn into the
retry loop it exists to prevent.

Defaults are settable without touching code: `SO_AUDIT_PER_MINUTE`,
`SO_AUDIT_BURST` and `SO_MAX_CONCURRENT_AUDITS` for live, and
`SO_FIXTURE_AUDIT_PER_MINUTE`, `SO_FIXTURE_AUDIT_BURST` and
`SO_FIXTURE_MAX_CONCURRENT_AUDITS` for fixture. A missing, non-numeric or
zero/negative value keeps the default, and an oversized value is clamped (a
burst above the steady rate is reduced to it, so neither limit can be switched
off by configuration).

These limitations are stated plainly because they matter for a hosted demo:
the buckets are **in memory**, so a restart, a redeploy or an idle spin-down
hands every client a fresh burst; they are **per instance**, so more instances
multiply the allowance; they are **per mode, not per seed**, so two different
seeds from one client draw on the same bucket; and the client is identified by
the **rightmost** entry of `X-Forwarded-For`, because that entry alone is the
proxy's own record — anything to its left is text the visitor typed. A proxy
that replaces the header and a proxy that appends to it both put the real client
last, so the last entry is right under either behavior. No IP is stored: buckets
are keyed by a truncated digest.

## Failure behavior

| Condition | Behavior |
| --- | --- |
| No `QLOO_API_KEY` | HTTP 503; the response names `QLOO_API_KEY` but never a credential value |
| Credential rejected | refusal code `authentication failed` |
| Request timeout | refusal code `upstream timeout`; a timeout on the follow-up lookup costs the audit its one re-read and keeps the first-pass verdict |
| Non-JSON response | `MalformedResponse`, surfaced as a refusal or HTTP 502 |
| No search match | refusal code `seed resolution failed` |
| Seed has no usable scored metadata | refusal code `insufficient seed metadata` |
| Receipt cannot be re-derived | candidate state becomes `UNVERIFIABLE` and the audit is void for that candidate |
| Follow-up lookup returns no record | first-pass verdict stands; the outcome says the gap is real rather than a fetching artifact |
| Re-read makes the pair look comparable but its receipts fail re-derivation | the forged record is discarded and the first-pass verdict stands |
| Seed longer than 120 characters, or blank, or an unknown `mode`, or a non-numeric threshold | HTTP 400, no Qloo request, no limit token spent |
| That mode's per-client budget exhausted | HTTP 429 with `Retry-After`, naming the mode and whether it spends quota; nothing is sent to Qloo, in either mode |
| Too many audits of that mode in flight | HTTP 503 with `Retry-After: 2`; nothing is sent to Qloo |
| An unexpected error inside the audit path | HTTP 500 `internal audit error`; the traceback goes to the server log, never to the client |
| An unexpected error inside the demo path | HTTP 500 `internal demo error`; same rule — the log line is run through the credential scan before it is written |
| Unknown `scenario` on `/api/demo` | HTTP 400 naming the two valid scenarios |

Every JSON response is scanned once for the credential value before it is
written, and so is the traceback that a 500 leaves behind in the log. The reason
is the last line of a traceback: it is the exception's own message, and an
adapter error that quotes the request it was making can carry the credential into
it. Redacting the rendered stack costs the operator nothing — the file and line
information survives, only the value does not.
Adapter messages are built from text that never includes the key, so
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
| Clean publication copy — no `data/`, no `.env`, no captured responses | 270 tests: 213 passed, 0 failed, 0 errors, 57 skipped, exit code 0 |
| The same copy with a local `data/fixtures/` directory temporarily available | 270 tests OK, 0 skips, exit code 0 |

The 57 skips are the fixture-dependent tests: they compare real payloads field by
field, and the payloads they read are Qloo API responses that stay private and are
not published with this repository. A public checkout therefore runs 213 tests and
skips those 57 with the reason

```text
captured Qloo fixtures are not present in this checkout
```

The skips are a missing-input condition, not a failure: the same 270 tests run
with 0 skips wherever the captured responses are available.

Coverage includes receipt re-derivation, fabricated shared terms, normalization,
duplicate collapse, audit states and boundaries, live adapter error mapping,
key-header behavior, secret non-disclosure, static seed catalogue behavior,
mode handling, `.env` loader precedence, clean-checkout HTTP routes, and the audit
throttle: bucket arithmetic and refill under an injected clock, per-client
isolation, the `X-Forwarded-For` rule that ignores client-supplied prefixes,
refused requests making zero upstream calls, the in-flight gate, `Retry-After`,
oversized-input rejection, and the redaction scan over error and refusal bodies.
`tests/test_mode_budgets.py` covers the split specifically, on synthetic
fixtures it writes itself so it runs on a clean checkout: a fixture audit that
produces a full report without opening a socket or building an API client,
fixture traffic that cannot draw on the live bucket, an exhausted live guard that
still serves fixture requests, refusal text that names the mode that actually
ran out, and a source check that the page's only timer is the countdown and
that the countdown never fetches. It also checks that every click takes a new
ticket, so a slow audit cannot replace the report of the seed the visitor
chose last. `tests/test_clean_checkout.py` drives the shipped server over a copy
with no `data/` and no key, and asserts there that a fixture request refuses with
`503` in fixture mode and without naming the directory the server runs from, that
the synthetic demonstration still runs in that state, and that twenty
demonstrations leave the live audit failing for the real reason — no credential —
rather than as a rate limit.

`tests/test_investigation.py` (42 tests) covers the second look, on invented
records only. Its load-bearing assertions are **adapter call counts**, so an
agent that never re-reads fails them and an agent that re-reads twice fails them
too: no gap means no lookup; two gaps still mean one lookup; only an entity id
already present in the candidate set is ever requested, in the recorded order
`resolve_seed → candidates → seed_record → entity_record`; the ceiling cannot be
raised by a caller, can be lowered to zero, and is spent by a lookup that fails.
Verdict changes are checked against hand-computed overlaps and an independent
state oracle rather than a recorded expected value, in all three directions: a
re-read that legitimately populates the missing dimensions raises the state, one
that adds disjoint evidence holds it, and one whose own record carries *less*
than the list copy did lowers it. The failure paths — timeout, authentication
error, no record, unreadable record — must each return the first-pass report
byte-for-byte what a zero-budget run produced, with the candidate's whole payload
compared, not just its state. A re-read that would manufacture support is
detected by the receipt check and discarded. Provenance is verified by resolving
every receipt's source path against the payload the report claims it came from,
and the trace must name the gap, the reason, the action, the result and the final
decision. A separate class asserts that nothing in the whole report — including a
crash and its log line — can carry the sentinel credential, a traceback, or a
local path.

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
                    agent, adapters, synthetic demo records, CLI, environment
                    loader, audit throttle, HTTP server
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

That is not a guess about what is allowed. The official hackathon developer guide
says, verbatim: *"Do not store Qloo response data in a public repository — that
makes the data publicly accessible outside the terms of your API key."* The same
page permits the other half explicitly: *"Private server-side caching is fine and
there is no time limit on how long you can keep cached responses."* So the split
this repository observes is the split the rules draw — code that *calls* the API
is public, what the API *returned* stays private. Fixture mode exists for exactly
that private local copy, and `data/fixtures/` is absent here on purpose rather
than by omission. Quote: docs.qloo.com/reference/qloo-llm-hackathon-developer-guide,
read 2026-10-29.

The credential rules are followed to the letter in the same source: *"Never share
a credential between teams or put one in source control, a browser app, a public
MCP configuration, or a demo recording."* The key is read from the process
environment only, sent only as the `X-Api-Key` header, never placed in the page
or a URL, and scrubbed from any response body or traceback the server writes.
No recording, screenshot or captured payload is published from this copy.

The presentation rule is the one this tool is built around — *"Present results
with their source and limits. Distinguish a Qloo result from your own product
interpretation, and do not describe an affinity as a causal claim or a
probability about an individual."* Every number carries the field path it came
from, every run is labelled live, saved-capture or synthetic, and the affinity
column is Qloo's own value, displayed separately from the term evidence and never
recomputed, blended into a score, or read as a claim about a listener.

## Limitations

- The tool audits metadata overlap only. It does not audit audience overlap,
  demand, commercial performance, or recommendation quality.
- Metadata coverage is uneven; where a dimension is missing on either side, the
  receipt is `unverifiable` and the limitation names the dimension.
- Affinity values are displayed as returned by Qloo; the auditor does not
  recompute or reinterpret them.
- A live audit depends on an operator-provided `QLOO_API_KEY` and on the current
  Qloo API behavior.
- One live audit costs three Qloo requests, four when the follow-up re-read
  fires, so a hosted demo spends the operator's quota on other people's clicks.
  The live budget is what bounds that, not what authorizes it. Fixture audits
  spend no quota and draw on their own budget, so local testing never eats into
  the live allowance.
- The follow-up is **gap-triggered, not routine**, and on the operator's own
  captured audits it has never fired: across the seven seeds those responses
  cover, every run reported `follow_up_queries_used: 0` and
  `candidates_with_gap: 0`, and stayed at three requests. `AFFINITY_ONLY`
  verdicts do occur in that data — the gap condition is simply stricter, because
  a dimension has to be empty on the candidate's side while the seed carries
  terms there. So the second look is demonstrated honestly, by the two synthetic
  scenarios and by call-count tests against the real agent, and a live audit
  today will usually show it declining to run. It has not yet been observed
  firing against Qloo itself.
- The audit budget is in-memory, per-instance, per-mode state: a restart, a
  redeploy or a free-tier idle spin-down refills every bucket, and running two
  instances doubles the allowance. It is a quota guard, not access control — the
  demo has no sign-in, so anyone within budget can run an audit.
- No hosted demo URL exists yet, while the event's submission rules ask for a link
  to a functional, publicly published demo application. Until it is deployed, the
  live path is verified locally: against a stubbed adapter in the suite and
  against one deliberate credentialed audit through this copy's own server.
- The clean-copy audit verifies local no-key behavior, not a hosted deployment.

## License

MIT. See `LICENSE`.
