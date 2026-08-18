# graphplatform -- Phase 1 & 2: Graph Schema, Write Service, Ingestion

A supply-chain vulnerability graph, backed by a **real local HydraDB**
instance speaking the Neo4j Bolt protocol and an OpenCypher-flavored query
language (`neo4j://127.0.0.1:7687`, HTTP `127.0.0.1:8443`, admin
`127.0.0.1:9090`). This is a different HydraDB product surface than the
hosted v2 RAG API this repo's top-level `hydradb.go` talks to -- that one
is a knowledge-extraction/retrieval service; this one is a real graph
database with Bolt + Cypher, running as a local Docker container against
this box's `setup/hydra-db-data/` volume.

`graphplatform/write_service.py` (`GraphWriteService`) is the **only**
module in this codebase allowed to open a connection to it. Every later
phase reads and writes the graph exclusively through its public methods.

## Why this looks different from textbook Cypher

HydraDB's real query engine (verified by hand against the running
instance -- there is no public documentation for this product surface)
rejects a lot of standard Cypher: bare `MATCH (n)`, `RETURN n` (whole
nodes), explicit transactions, `ON CREATE`/`ON MATCH`, `IS NULL`, and
plain `MERGE`/`CREATE` on a node with a string key. The full list of
what's actually supported, and the exact query shapes this service uses
to work within it, is documented in the module docstring at the top of
`write_service.py` -- read that before touching the Cypher in this
package. The short version: every node and relationship gets a
deterministic integer `id` (hashed from its natural key) used only as the
merge handle, and the real human-facing key is mirrored into a separate
string property (`key` on nodes, `rel_id` on relationships) that every
read path uses instead.

## Setup

```
cd graphplatform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Env vars (all optional -- defaults match this box's local dev instance):

| Var | Default | Meaning |
|---|---|---|
| `HYDRADB_URI` | `neo4j://127.0.0.1:7687` | Bolt URI |
| `HYDRADB_TOKEN` | (reads `HYDRADB_TOKEN_FILE`) | Bearer auth token |
| `HYDRADB_TOKEN_FILE` | `../setup/hydra-db-data/auth-token` | Where to read the token from if `HYDRADB_TOKEN` isn't set |
| `GRAPHPLATFORM_REDIS_URL` | `redis://127.0.0.1:6379/0` | Event queue (phase 2) -- needs a local `redis-server` running |
| `GITHUB_TOKEN` | (unset) | Optional -- raises the GHSA connector's rate limit from 60/hr to 5000/hr |

The local dev instance is started via (already running on this box, see
the `docker run` invocation for the exact flags used):

```
docker run --rm -p 7687:7687 -p 8443:8443 -p 9090:9090 \
  -v $(pwd)/../setup/hydra-db-data:/data \
  -e CLOUD_PROVIDER=local -e LOCAL_PATH=/data/store \
  -e GRAPH_NAMESPACE=default -e GRAPH_ID=default \
  -e GRAPH_CELL_ID=cell-0 -e GRAPH_CELLS=cell-0 -e GRAPH_NODE_ID=node-0 \
  -e GRAPH_BOLT_NODE_ADDRESSES=node-0=127.0.0.1:7687 \
  -e GRAPH_ADVERTISED_BOLT_ADDR=127.0.0.1:7687 \
  -e GRAPH_DATA_CACHE_DIR=/data/cache -e GRAPH_AUTH_TOKEN_FILE=/data/auth-token \
  -e GRAPH_ALLOW_PLAINTEXT=true -e RUST_MIN_STACK=33554432 \
  ghcr.io/hydra-db/hydradb:latest
```

Health check: `curl http://127.0.0.1:9090/readyz` should return `200`.

## Run the tests

Integration tests hit the real running instance -- there is no mock mode.

```
source .venv/bin/activate
python -m pytest -q
```

Every node type and every relationship type gets: a write, a read-back
with value assertions, a second write with the same key asserting no
duplicate was created (idempotency) and that `first_observed_at` did not
move, and -- for relationships -- an explicit assertion that the read path
uses the `rel_id` mirror property, plus a dedicated regression test
(`test_relationship_reading_r_dot_id_is_broken_...`) that proves `RETURN
r.id` on a relationship variable still fails, which is *why* the mirror
exists.

## Smoke test

```
python scripts/smoke_test.py
```

Creates a `Package`, a `Version`, and a `Maintainer`, links them with
`PUBLISHED_BY`, reads the whole subgraph back with `consistency="strong"`,
and prints it as JSON. Idempotent -- safe to run repeatedly.

## What's here

- `graphplatform/schema.py` -- node labels, relationship types, the
  `stable_id()` integer-id derivation, ISO-8601 timestamp helpers, and the
  `OPEN_INTERVAL_SENTINEL` used internally for `RESOLVED_VERSION_AT`.
- `graphplatform/write_service.py` -- `GraphWriteService`: one
  `upsert_*`/`write_*` method per node/relationship type (all idempotent),
  one `get_*` method per basic lookup, a `consistency` parameter
  (`"causal"` | `"strong"`) on every read.
- `graphplatform/ingestion/` -- phase 2, see below.
- `tests/` -- integration tests against the real local instance.
- `scripts/smoke_test.py` -- the phase-1 smoke test above.
- `scripts/ingest.py` -- the phase-2 ingestion CLI, see below.

## Phase 2 -- Ingestion

```
graphplatform/ingestion/
  events.py             PackageVersionPublished / AdvisoryPublished -- the
                         normalized, JSON-serializable events every
                         connector emits and GraphIngestionWriter consumes.
  queue.py               EventQueue (abstract) / RedisStreamQueue -- the
                          only thing that talks to redis.
  writer.py               GraphIngestionWriter -- the only thing in this
                           layer that calls GraphWriteService; turns one
                           event into the right upsert_*/write_* calls.
  registry/{base,npm,pypi}.py       RegistryConnector Protocol + two real
                                     connectors.
  advisory/{base,osv,ghsa}.py       AdvisoryConnector Protocol + two real
                                     connectors.
  manifest/{discovery,npm_lock,python_lock,service}.py
      discovery.py    walks a repo, detects npm/yarn/pnpm workspaces,
                       lerna, and Python multi-package layouts, parses
                       resolved (not declared) dependency versions.
      service.py       discover_and_ingest(repo_path, org, repo,
                        write_service) -- the one public entry point,
                        writes Application + RESOLVED_VERSION_AT edges
                        directly through GraphWriteService (no queue in
                        front of this one, by design -- see below).
```

**Why manifest discovery skips the queue.** Registry and advisory data are
genuinely continuous, unbounded streams -- a queue between the connector and
the writer is what makes that safe to consume independently and replay on
crash. A repo scan is a single bounded unit of work triggered by a caller
who wants its result back (phase 6's scanner and bot call
`discover_and_ingest` directly and use the returned `DiscoveryResult`) --
putting a queue in front of it would only add latency and a second failure
mode for no real benefit. `discover_and_ingest` is idempotent and
`resolve_version`/`supersede_version`-aware (a re-scan closes out resolutions
that are no longer present rather than leaving stale ones), so it's still
safe to call from a webhook handler on every push.

### Running it

```
redis-server --daemonize yes          # once, if not already running

# terminal 1 -- drain both streams into HydraDB as events arrive
python scripts/ingest.py consume --stop-after-idle 3

# terminal 2 -- real, bounded backfills (publish to the queue, don't write
# HydraDB directly)
python scripts/ingest.py registry npm backfill lodash express is-number
python scripts/ingest.py registry pypi backfill requests flask
python scripts/ingest.py advisory osv backfill npm:lodash PyPI:requests   # OSV's own ecosystem spelling: npm, PyPI (capital P/I)
python scripts/ingest.py advisory ghsa backfill --ecosystem npm --max-pages 1

# live/incremental mode (bounded via --max-iterations for a demo; omit for
# a real long-running subscription)
python scripts/ingest.py registry npm live --max-iterations 1
python scripts/ingest.py advisory ghsa live --ecosystem npm --max-iterations 1

# manifest discovery -- no queue, writes directly
python scripts/ingest.py manifest /path/to/repo --org some-org --repo some-repo
```

`registry`/`advisory` `backfill` runtime is dominated by the number of
*versions* fetched, not packages: each version needs its own request for
correct metadata (`registry.npmjs.org` inlines every version in one doc;
PyPI's per-version endpoint is a separate real fetch each time -- see the
docstring in `registry/pypi.py` for why the cheaper unversioned endpoint is
wrong to use here). A 2-3 package backfill (a few hundred versions) is
seconds to low tens of seconds; `consume` writing those into HydraDB is
slower, since every event is several sequential Cypher round trips (see
"Known limitations" below).

### Real data verified against this box's local HydraDB instance

- `registry npm backfill lodash express is-number` -> 420 real published
  versions, real dependency ranges (`express@4.19.2` really does depend on
  `qs@6.11.0`, etc.), real publish timestamps.
- `registry pypi backfill requests flask` -> 224 real published versions,
  with per-version-correct `requires_dist` (verified by hand that
  `requests`' unversioned `/json` endpoint reflects only its *latest*
  release's dependencies -- using it for historical versions would have
  silently attached the wrong deps to every version but the newest; see
  `registry/pypi.py`'s docstring).
- `advisory osv backfill` / `advisory ghsa backfill` -> real advisories
  including the well-known `GHSA-29mw-wpgm-hmr9` lodash ReDoS.
- `manifest` against a real shallow clone of `changesets/changesets`
  (real pnpm workspace monorepo) -> 24 `Application` nodes (23 workspace
  members + the workspace root), each member's resolved set filtered to
  its own declared deps from the shared `pnpm-lock.yaml`, the root getting
  the full 466-package transitive closure.
- `manifest` against a real shallow clone of `python-poetry/poetry` ->
  102 `Application` nodes. Poetry's own repo turned out to be a good real
  stress test of the "walk the *entire* tree" requirement: it embeds ~100
  `tests/fixtures/*/pyproject.toml` directories for its own test suite,
  every one of which is a legitimate (if noisy) monorepo member by the
  letter of the detection rule, plus one deliberately-malformed
  `tests/fixtures/incompatible_lock/poetry.lock` that exercised the
  parser's graceful-failure path (logged a warning, kept going) for real
  rather than in a synthetic test. A real refinement for a later phase:
  skip common test/fixture directory conventions during workspace
  detection -- not built here since it would be guessing at a convention
  from one example rather than a verified rule.

## Known limitations, carried forward on purpose

- `supersede_version(app_key, version_key, resolved_at, superseded_at)`
  requires `resolved_at` to identify exactly which historical interval to
  close, rather than inferring "the current one" -- HydraDB's `WHERE`
  clause has no `IS NULL` support, so there's no query-side way to find
  "the resolution that hasn't been superseded yet" without a sentinel.
  `get_current_resolutions(app_key)` finds it for the caller.
- `SHARES_INFRASTRUCTURE_WITH` is stored directed (Cypher relationships
  always are) even though the concept is symmetric; later phases querying
  it should check both directions or canonicalize endpoint order at write
  time.
- Read consistency is threaded through the Bolt driver as transaction
  metadata (`neo4j.Query(cypher, metadata={"consistency": mode})`).
  Confirmed as a real, strictly-validated field on HydraDB's HTTP JSON
  API; accepted without error on the Bolt path too, but not independently
  provable as behavior-changing on this single-node, no-replication-lag
  local instance. Flagged, not smoothed over.
- **This local dev instance's storage backend has a real, permanent write-
  volume ceiling.** Its garbage collector needs `PutMode::Update`
  (conditional/compare-and-swap PUT) to rewrite the manifest and
  compaction objects, and the `object_store` `LocalFileSystem`
  implementation this build's `/data/store` volume uses doesn't support
  that mode at all (`Operation 'put_opts' with mode 'PutMode::Update' not
  yet implemented by LocalFileSystem`, confirmed in the container's own
  logs, recurring every GC cycle). So GC *never* succeeds here -- unpruned
  storage epochs accumulate until, after enough sustained write volume
  (observed consistently somewhere in the low thousands of Cypher
  statements on a freshly-wiped store; exact threshold not pinned down
  more precisely than that), reads start failing outright with
  `GraphSnapshot query is not supported yet: historical graph epochs are
  not SlateDB snapshots`. This is a limitation of this exact local,
  single-node, filesystem-backed dev build, not of the application code
  or the schema/write-service design -- a real object-store backend (S3,
  GCS) supports conditional PUT and wouldn't hit this. Two ways to keep
  working past it: (1) wipe `setup/hydra-db-data/store` and
  `.../cache` and restart the container to reset the epoch count (safe --
  it's a local scratch volume), or (2) bound write volume per run, e.g.
  `scripts/ingest.py consume --max-events N`, which exists specifically
  for this. Not silently retried or hidden -- `_run` lets the real
  `CypherSyntaxError` propagate.
