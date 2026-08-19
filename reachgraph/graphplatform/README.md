# graphplatform — ReachGraph backend

A blast-radius graph engine backed by a **live local HydraDB** instance (`neo4j://127.0.0.1:7687` Bolt, `127.0.0.1:8443` HTTP, `127.0.0.1:9090` admin).

`graphplatform/write_service.py` (`GraphWriteService`) is the only module allowed to open a HydraDB connection — it owns integer-identity hashing, mirror-property persistence, and consistency controls (`causal` vs `strong`). Its module docstring documents HydraDB's real Cypher dialect in detail (it rejects a lot of textbook Cypher — no explicit transactions, no bare `MERGE`/`CREATE`, no `IS NULL`, no chaining a write with `MATCH`/`RETURN`/`WITH`); read it before writing new Cypher.

## Layout

```
graphplatform/
  schema.py            node labels, relationship types, stable_id() hashing
  write_service.py     GraphWriteService -- all HydraDB reads/writes go through here
  query/
    service.py          QueryReasoningService -- transitive_exposure, live_resolutions, blast_radius
    models.py            result dataclasses
  ingestion/
    registry/            npm + PyPI metadata/version connectors
    manifest/             monorepo/workspace manifest discovery (npm/yarn/pnpm/poetry) + ingest
    writer.py             GraphIngestionWriter -- turns queue events into GraphWriteService calls
    events.py, queue.py   event shapes + Redis Streams queue
  product/
    lookup.py             PackageLookupService -- Flow 1 (package blast radius)
    scanner.py             RepoScannerService -- Flow 2 (repo clone + manifest discovery + blast radius)
    api.py                  REST handler wiring both up on :8081
  scripts/                CLIs: server.py, ingest.py, smoke_test.py
tests/                    integration tests against a real running HydraDB (no mocks)
```

## Graph model

Nodes: `Package`, `Version`, `Maintainer`, `Application`, `File`.
Relationships: `DEPENDS_ON`, `RESOLVED_VERSION_AT`, `PUBLISHED_BY`, `CONTAINS`, `IMPORTS`.

`Application` represents either a real GitHub repo/subpackage (`org/repo[/subpath]`) that resolves package versions, or — once the GitHub dependents scrape lands — a package that declares a dependency on another package. `File` + `IMPORTS` (Application → File via `CONTAINS`, File → Package via `IMPORTS`) is the substrate for Flow 2's per-file import graph, populated by the `gitnexus` CLI integration (in progress).

## Blast radius

`QueryReasoningService.blast_radius(start_key, max_depth=8, consistency="causal")` (`query/service.py`) does an outward BFS from a `Package` or `Version` key across `DEPENDS_ON` (who depends on this package) and `RESOLVED_VERSION_AT` (which applications resolved a version of it), returning every reachable `Package`/`Application` with path and depth. This is the one traversal both product flows are built on top of.

## Running the services

```bash
source .venv/bin/activate

# test suite (needs a running HydraDB; Redis-dependent tests need a local redis-server too)
pytest -v

# REST API on :8081
python scripts/server.py --port 8081

# one-shot smoke test against a real HydraDB
python scripts/smoke_test.py

# registry backfill + manifest discovery CLI
python scripts/ingest.py registry npm backfill lodash express is-number
python scripts/ingest.py manifest /path/to/repo --org some-org --repo some-repo
```

## Known limitations

**The local HydraDB backend's GC always fails, and eventually so does everything else, until you wipe it.** The single-node object-store backend needs `PutMode::Update` to rewrite manifest/compaction objects on every GC cycle (~60s), and that mode is permanently unimplemented on `LocalFileSystem` — so GC fails every cycle, forever, on this backend (not a config issue). Unpruned storage epochs accumulate until, somewhere in the low thousands of cumulative Cypher statements since the last wipe, writes start failing with:

```
object store error: Operation `put_opts` with mode `PutMode::Update` not yet implemented by LocalFileSystem(file:///data/store)
```

Fix (there is no other fix — this is not a bug in this codebase):

```bash
podman stop hydradb-graphplatform
sudo rm -rf setup/hydra-db-data/store setup/hydra-db-data/cache
mkdir -p setup/hydra-db-data/store setup/hydra-db-data/cache
chmod 777 setup/hydra-db-data/store setup/hydra-db-data/cache
podman start hydradb-graphplatform
```

If instead you see plain `Permission denied` writing to `_coordination/v1` after a container restart, that's the rootless-podman uid-10001 mismatch, not the GC ceiling — `sudo chmod -R a+rwX setup/hydra-db-data/store setup/hydra-db-data/cache` fixes it (the uid falls outside the host user's subuid range, so `podman unshare` doesn't help here).

Because of the ceiling above, both product flows deliberately cap how much they write per request: repo scans dedupe by package key, and the (in-progress) GitHub dependents scrape caps at roughly 100 dependents per lookup rather than trying to ingest a popular package's full reverse-dependency set.
