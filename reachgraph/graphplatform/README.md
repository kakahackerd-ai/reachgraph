# graphplatform -- Phase 1: Graph Schema & Write Service

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
- `tests/` -- integration tests against the real local instance.
- `scripts/smoke_test.py` -- the phase-1 smoke test above.

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
