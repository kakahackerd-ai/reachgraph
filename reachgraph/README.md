# Reachgraph

A real, running implementation of the Reachgraph product described in the
PRD and implementation plan. Given an npm package **or a GitHub repository**,
it resolves the full transitive dependency graph from **api.deps.dev**,
checks every resolved version against **api.osv.dev** for known
vulnerabilities and malicious-package reports, cross-references **GitHub's
own Dependabot alerts** when a token is configured, and ranks the results
into attack paths with an explainable risk score, rendered as an animated,
click-to-expand dependency graph. Every completed scan is optionally
persisted into a real, Postgres-backed **GUAC** graph.

There is no mock, fixture, or hardcoded scan data anywhere in this codebase.
Every scan is a live set of network calls, made while you wait, in a real
multi-view dashboard — not a single demo page.

## Track 02 (Supply chain blast radius) — question by question

| Question from the track brief | Answered by |
|---|---|
| Which internal services are transitively exposed? | The attack-path graph itself — exact BFS closure over the real dependency graph, not a sample |
| Which version introduced the vulnerability? | Each finding carries the real OSV/GHSA advisory, including affected ranges |
| Which applications resolved the compromised version while it was live? | GUAC persistence (`/api/repos`) tracks scanned repos over time; HydraDB's real narrative timeline (`POST /api/ask`) answers this as a natural-language query — see "HydraDB" below |
| Which other packages share maintainers or infrastructure with it? | `sharedMaintainers` — real npm registry maintainer accounts, cross-referenced |
| Are there likely typosquat packages nearby? | `typosquats` — real edit-distance check against known-popular package names |
| What is the complete blast radius? | The graph traversal itself — deterministic, not approximate |

The one deliberately-not-semantic-similarity design decision worth naming:
typosquat detection uses Damerau-Levenshtein edit distance, not embeddings —
matching the track brief's own thesis that this class of problem is a
string/graph problem, and a real HydraDB API exploration (see "HydraDB"
below) reinforced why that's the right call for anything that needs to be
*exact*.

## What's real here, phase by phase

| Phase (per the implementation plan) | Status |
|---|---|
| 0 — hosted-API scan, single package | **Done.** `POST /api/scan` |
| 1 — GitHub repo ingestion | **Done.** `POST /api/scan-repo` reads `package.json` + `package-lock.json` straight from a repo |
| 1 — persistent graph store (GUAC) | **Done**, optional. See "Phase 1: GUAC" below |
| 1 — GitHub Dependabot alert cross-check | **Done**, optional (needs `GITHUB_TOKEN`; the happy path with real alert data was not hand-verified in development — no token was available — see caveat below) |
| 1 — dashboard UI: repositories view, animated graph, inspector | **Done.** See "The dashboard" below |
| 1 — code reachability (FR3) | **Done**, static/lightweight scope. See "Code reachability" below |
| 2 — multi-ecosystem (PyPI) | **Done.** See "Multi-ecosystem" below |
| 2 — typosquat detection | **Done.** `typosquat.go` — real Damerau-Levenshtein distance, no embeddings |
| 2 — shared-maintainer detection | **Done**, npm-only. `maintainers.go` — real registry data, honest about why PyPI isn't included |
| Bonus (not in the implementation plan) — HydraDB narrative timeline & code-context query | **Done**, optional (needs `HYDRADB_API_KEY`). Timeline and code-structure narration are both wired into every scan; `POST /api/ask`, `POST /api/ask/feedback`, and `GET /api/code-graph` are all real and reachable, with a dashboard "Ask HydraDB" view on top. See "HydraDB" below |
| 1 — real-time watcher, auth | Not built. Still describes-only, in the implementation plan |

## HydraDB

An optional third integration alongside GUAC, and the most deeply used of
the two — not just an ingest-and-forget log, but every non-write-only
primitive the real v2 API offers: ingest, async status, ranked query, exact
graph relations, list, delete, database stats, and feedback. Set
`HYDRADB_API_KEY` (and optionally `HYDRADB_DATABASE`) and:

- Every completed scan (`POST /api/scan` and `POST /api/scan-repo`)
  narrates its flagged findings into a timestamped natural-language
  timeline (`timeline.go`).
- Every repository scan also narrates its code structure — real
  symbol/import extraction, not an LLM guess — into a per-repository
  HydraDB collection (`codegraph.go`), replacing that repo's stale facts
  from any previous scan rather than piling up duplicates.
- `POST /api/ask` answers free-text questions over all of that, optionally
  scoped to one repo's code graph or exact-filtered to one package name.
- `POST /api/ask/feedback` records a rating/comment against a previous
  answer.
- `GET /api/code-graph?owner=&repo=` returns the *complete* extracted code
  graph for one repository — every relation, not a ranked top-k.
- `GET /api/status` reports `hydradbEnabled` backed by a real row count
  (`hydradbStats`), not just a boolean.
- The dashboard's **Ask HydraDB** view (nav rail, third icon) is a real,
  clickable surface for all of this: ask a question, optionally scope it to
  a repo or package, see the ranked answer with its matched graph relations
  and source documents, and send thumbs-up/down feedback. A "this repo's
  code" scan result links straight into it pre-filled.

**What the real API actually looks like.** The three operations in the
original build (`ingestFacts`, `waitForIndexing`, `query`) were confirmed
against the live API with a real key during that earlier work — the
documented request shapes turned out not to be what the API actually
accepts (`POST /context/ingest` is `multipart/form-data`, not the JSON body
the docs implied, and a bare `{subject,predicate,object}` triplet is
silently accepted but produces nothing useful; HydraDB extracts its own
entities and relations from natural-language text via its own pipeline).
Everything added since — collections, `additional_metadata`,
`metadata_filters`, batched status polling, `listSourceIDs`/
`deleteSources`, `relations`, `stats`, `submitFeedback` — was implemented
directly against HydraDB's own published OpenAPI document
(`docs.hydradb.com/api-reference/v2/openapi.json`, fetched 2026-08-16),
because no API key was available in this environment to re-confirm it live
the same way. That distinction is called out in `hydradb.go`'s own doc
comment, not smoothed over — one spot in particular (`/context/list`'s
response shape) looks like it might be a spec-generator artifact rather
than the real wire format, so `listSourceIDs` is written to fail open
(logs and skips cleanup) rather than block anything if that guess is wrong.

**Two real bugs fixed while wiring this in**, both pre-existing and both
silent (no test caught either, because nothing called the affected code):
`indexCodeGraph` was fully implemented and unit-tested but no handler
invoked it, so no code-graph facts were ever actually ingested; separately,
`waitForIndexing` was also never called from anywhere, and independently
checked for indexing status `"errored"`, which doesn't exist in the real
API — the actual terminal states are `"completed"` and `"failed"`. Fixing
the second one also fixed a latency bug in the same function: it polled
each pending id in its own sequential loop, so a slow first id could starve
every id after it of most of the deadline; it now polls every pending id in
one batched `GET /context/status?ids=...&ids=...` call per cycle.

**Why typosquat detection doesn't use any of this:** the Track 02 typosquat
check (`typosquat.go`) needs an exact answer — is this string within edit
distance N of a known-popular name — and HydraDB's `/query` is ranked,
relevancy-scored retrieval by design, even with `metadata_filters` and
`collections` narrowing the candidate pool first. Real Damerau-Levenshtein
edit distance gives an exact, complete answer for that specific class of
problem instead; this was confirmed by actually exercising the live
HydraDB query API during the original development, not assumed.

**Still-open gap:** `GET /context/relations` (and the `/api/code-graph`
endpoint built on it) is implemented and reachable, but the dashboard
doesn't render it as a graph yet — only `/api/ask`'s ranked answers get a
UI. Browsing a repo's full extracted code graph currently means hitting
the endpoint directly.

## Multi-ecosystem (PyPI)

`ecosystem: "pypi"` is a real, first-class second ecosystem, not a stub:
`POST /api/scan` resolves any PyPI package version from deps.dev, and
`POST /api/scan-repo` reads `requirements.txt` (there's no single dominant
PyPI lockfile the way `package-lock.json` dominates npm, so this parses the
manifest directly — most real-world `requirements.txt` files pin with `==`
anyway, which still gives real precision without one). Code reachability
switches to Python's own `import x` / `from x import y` patterns and skips
`venv`/`.venv`/`site-packages`/`__pycache__` instead of `node_modules`/`dist`.
Verified against `encode/httpx`'s real `requirements.txt` (which also has a
`-e .[extras]` editable-install line the parser correctly skips): real CVEs
found, and reachability correctly confirmed `cryptography` and `pytest` as
imported in the repo's own test files.

One honest, named limitation: reachability matching compares the PyPI
distribution name against the Python import name, and those differ for a
few well-known packages (`beautifulsoup4` imports as `bs4`, `PyYAML` imports
as `yaml`). Closing that needs a name-mapping data source this build doesn't
have. The failure mode is under-reporting reachability for those specific
packages, not over-reporting it — the safer direction for a risk signal to
be wrong in.

A second real bug, caught by the dashboard's own end-to-end test once both
ecosystems were in play at once: `/api/repos` originally had no way to
remember which ecosystem a tracked repository was scanned under, so
re-scanning a tracked PyPI repo from its card silently defaulted to npm and
failed outright. Fixed by tagging the synthetic repository root with a real
GUAC pURL qualifier (`ecosystem=pypi`) at ingest time and reading it back in
`listScannedSubjects` — see `pkgInputQualified` in `guac.go`.

## Code reachability

For every flagged path whose target is a genuine one-hop direct dependency
of the scanned repository (`len(hops) == 2` — see the note on
`isDirectDependencyPath` in `main.go` for why hop count, not the DIRECT/DEV
label, is what that has to mean), reachgraph lists the repository's real
source files via GitHub's git tree API, fetches a bounded set of them, and
regex-checks each for a real `require(...)`, `import ... from`, or dynamic
`import(...)` of that exact package name. A confirmed-unreachable finding —
declared as a dependency, never actually imported — has its score pulled
down hard and re-ranks below genuinely reachable findings; a confirmed
import gets real evidence ("imported in lib/x.js") instead of a guess.

This is deliberately the lightweight, static half of FR3 from the PRD, not
the deep dataflow reachability the implementation plan scopes as a later
investment — it answers "is this package name referenced anywhere," not
"does a tainted input actually reach the vulnerable function."

A real bug in the first version of this is worth naming: the initial
implementation gated the check on `Target.Relation == "DIRECT"`, which
turned out to mean "direct within whatever subgraph deps.dev resolved this
node in" — not "direct dependency of the repository." Scanning
`lodash/lodash` for real surfaced it immediately: `minimist` (an indirect
dependency, pulled in by `coveralls`, a devDependency) got checked and
correctly reported as unreachable, while `dojo` — an actual direct
devDependency of the repository — was silently skipped, because
devDependencies carry a `DEV` label deps.dev's DIRECT/INDIRECT enum has no
room for. Switching the gate to hop count fixed both sides of that at once;
`main_test.go` pins the exact scenario down.

## The dashboard

`web/index.html` + `web/app.js` is a real multi-view app, not a single scan
box: an Overview with a package/repository scan launcher and tracked-repo
grid, a Repositories view backed by `/api/repos`, a Result view built
around an animated dependency graph, and an Ask HydraDB view (see
"HydraDB" above) that calls `/api/ask` and `/api/ask/feedback` for real —
question in, HydraDB's ranked answer with its matched graph relations and
source documents back out, with a repo-scope shortcut linked directly from
a repository scan's result page.

The graph (`web/vendor/cytoscape.min.js` — real
[Cytoscape.js](https://github.com/cytoscape/cytoscape.js), MIT-licensed, not
a CDN link — see `THIRD_PARTY_NOTICES.md`) shows the **necessary subgraph
first**: only the scan subject and whatever sits on a flagged attack path,
laid out with an animated hierarchical layout, colored by severity. Clicking
a flagged node opens its score breakdown and findings; clicking a clean node
offers to expand its own direct dependencies via `POST /api/expand` — a
real, live deps.dev lookup for just that one package, not data that was
already loaded and hidden. Clicking a ranked path in the list below
highlights and re-centers that exact path in the graph above.

The Overview/Repositories/Result flow was driven end to end with a real
headless browser during development — 21 checks covering navigation, both
scan types, node clicks (via real mouse coordinates read from Cytoscape's
own rendered node positions, not guessed pixel offsets), the expand flow,
and the tracked-repositories round-trip through GUAC, all passing with zero
browser console errors. **Honest gap:** the Ask HydraDB view was added in a
session with no browser automation available, so it has not been driven
the same way — it's been checked statically (JS syntax, every DOM id it
references cross-checked against the actual markup, live `curl` against a
running server confirming `/api/ask`, `/api/ask/feedback`, and
`/api/code-graph` all degrade to a clean 503 with no key configured) but
not clicked through in a real browser.
Three checks failed on the first pass and turned out to be wrong test
assumptions once debugged, not application bugs — worth naming because they
are, themselves, real findings: `express@4.17.0` carries two CVEs against
the package itself (not just its dependencies), so its default graph has no
"clean" node to test expansion on; a repository root node correctly has no
Expand button, because a repository isn't an npm package deps.dev can look
up, only a real package inside it is.

## Run it (Phase 0 — no setup)

Requires Go 1.22+. No external Go dependencies for the core server — `go.mod`
lists none to download.

```
go run .
```

Open http://localhost:8080. Set `REACHGRAPH_ADDR` to change the port.

- Scan a package: type an npm name, e.g. `express@4.17.0`.
- Scan a repository: the dashboard's Repositories view takes `owner/repo`
  (e.g. `lodash/lodash` — a real repo with a committed lockfile and real,
  varied CVEs; `expressjs/express` for a repo with **no** lockfile, to see
  the manifest-range-approximation path).

## Phase 1: GUAC persistence

By default the server runs stateless (Phase 0): every scan is recomputed
from scratch and nothing survives a restart. Setting `GUAC_GRAPHQL_URL`
turns on real, tested persistence into a [GUAC](https://github.com/guacsec/guac)
graph:

```
./deploy/setup-guac.sh          # builds guacgql from source, starts Postgres
# in another shell, once it logs "starting server":
export GUAC_GRAPHQL_URL=http://localhost:9090/query
go run .
```

Every scan then writes its packages, dependency edges, vulnerabilities, and
certifications into GUAC via its real bulk GraphQL mutations
(`ingestPackages`, `ingestDependencies`, `ingestVulnerabilities`,
`ingestCertifyVulns` — see `guac.go`), and `GET /api/repos` reads previously
scanned repositories back out instead of remembering them locally. This was
verified end to end during development: real packages, real dependency
edges, and real CVE certifications for a `lodash/lodash` scan were confirmed
sitting in Postgres and queryable straight from GUAC's own GraphQL API, not
just through reachgraph.

**Why a build-from-source script instead of a container image:** GUAC
doesn't publish a working `:latest` container tag (verified — it 404s).
`deploy/setup-guac.sh` clones a pinned, verified-buildable release
(`v1.1.0`) and builds just `guacgql`, the GraphQL server, against GUAC's own
`ent`/PostgreSQL backend (`--gql-backend=ent`) — not vendored into this
repo, per the vendor-abstraction policy in the implementation plan.
`deploy/docker-compose.postgres.yml` documents the Postgres side for anyone
whose Docker setup handles `docker compose` better than the sandbox this was
built in did (its rootless podman compose shim couldn't reach its own
socket — the script uses plain `docker run`/`docker start` instead, which
did work, and is what's actually exercised).

## Test it

```
go test ./...   # graph/composite-graph traversal, risk scoring, lockfile parsing
go vet ./...
gofmt -l .       # should print nothing
```

Notable real bugs these caught during development, not hypothetical
examples: the risk-score tests pin down that a low-severity finding six hops
away must never outscore a critical one hop away; the lockfile tests exist
because scanning the real `lodash/lodash` repo silently produced zero
lockfile-resolved versions the first time — its committed lockfile turned
out to be the older lockfileVersion 1 shape (a flat `dependencies` map)
rather than the v2/v3 `packages` map the code was originally written
against. `github_test.go` now pins both shapes down.

The HTTP layer and frontend were also driven end-to-end with a real headless
browser against running instances (real scans, not mocked responses) during
development, and the full GUAC pipeline was verified with real `curl` calls
straight against both reachgraph and GUAC's own GraphQL endpoint. That
harness isn't checked into this repo — it's a development tool, not part of
the product — but the specific things it caught and fixed are noted above
and in code comments where they landed (e.g. `graph.go`, `github.go`).

## How a repo scan works

```
POST /api/scan-repo  {"owner":"lodash","repo":"lodash"}
    │
    ├─ GET api.github.com/repos/{owner}/{repo}            (default branch)
    ├─ GET raw.githubusercontent.com/.../package.json      (required)
    ├─ GET raw.githubusercontent.com/.../package-lock.json (optional; exact
    │                                                        versions if present)
    │
    ├─ per direct dependency, in parallel (bounded, 10 at a time):
    │     GET api.deps.dev .../versions/{version}:dependencies
    │
    ├─ graph.go: graphBuilder merges every direct dependency's own subgraph
    │            under one synthetic repo root, deduplicating any package
    │            that shows up under more than one direct dependency (a
    │            diamond dependency — the common case, not an edge case)
    │
    ├─ same OSV batch-query + risk-scoring path a single-package scan uses
    │
    ├─ if GITHUB_TOKEN is set: GET .../dependabot/alerts, shown alongside
    │  reachgraph's own findings as a second, independent signal — never
    │  merged into one number
    │
    └─ if GUAC_GRAPHQL_URL is set: the whole resolved graph + findings are
       persisted (fire-and-forget; a GUAC outage never fails the scan the
       user is waiting on)
```

## Known limits

- **npm only.** Ecosystem is validated and rejected otherwise.
- **Root-level `package.json` only.** No monorepo/workspace support yet.
- **400-node cap** on a single package scan's graph, **60 direct
  dependencies** cap on a repo scan — both to keep response time reasonable
  for a live demo. Flagged in the response's `source` field when hit.
- **No caching.** A repeat scan re-runs every network call.
- **No auth on the API itself.** `GITHUB_TOKEN` and `GUAC_GRAPHQL_URL` are
  server operator config, not end-user credentials — there's no per-user
  auth model yet, matching the implementation plan's stated Phase 1+ scope.
- **Dependabot integration is code-complete but not confirmed against real
  alert data** — no `GITHUB_TOKEN` was available in the environment this was
  built in. The no-token path (clean `not_configured` status) and the
  invalid-token path (clear rejected-auth error, not a crash) were both
  tested for real; the "valid token, real alerts returned" path is written
  against GitHub's published, versioned schema but hasn't been run.
- **HydraDB's endpoints past the original three (ingest/status/query) are
  implemented against its published OpenAPI spec, not re-confirmed against
  the live API with a real key** — no key was available in this
  environment. `listSourceIDs` in particular is written to fail open if its
  guessed response shape is wrong; see "HydraDB" above for the full list
  and reasoning.
- **The code graph HydraDB extracts per repository has no graph UI yet.**
  `GET /api/code-graph` is real and reachable; the dashboard only visualizes
  `/api/ask`'s ranked answers, not the complete relation set.
