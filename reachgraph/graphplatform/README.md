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
    dependents/           github_scrape.py -- GitHub network/dependents scrape + deps.dev counts (Flow 1)
    manifest/             monorepo/workspace manifest discovery (npm/yarn/pnpm/poetry) + ingest
    codegraph/             import_scan.py (static per-file import scan) + gitnexus_client.py (real gitnexus CLI integration) -- Flow 2
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

`Application` represents either a real GitHub repo/subpackage (`org/repo[/subpath]`) that resolves package versions, or a repo scraped off GitHub's `network/dependents` page (Flow 1). `File` (`Application` -[:CONTAINS]-> `File` -[:IMPORTS]-> `Package`) is Flow 2's per-file import graph, populated by `ingestion/codegraph/import_scan.py`'s static scan. The real `gitnexus` CLI (`ingestion/codegraph/gitnexus_client.py`) supplies a separate, complementary layer on top: its own local (intra-repo) file-to-file `IMPORTS` graph, walked in Python (not persisted to HydraDB) to expand "files that directly import X" into "files reachable from an importer of X through the repo's own local call chain." Confirmed by hand that gitnexus itself has no concept of external package dependencies at all -- it never creates a node for one, even with the package installed in `node_modules` -- so it complements import_scan.py rather than replacing it.

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

Because of the ceiling above, both product flows deliberately cap how much they write per request: repo scans dedupe by package key and cap resolved dependencies per sub-package (`ingestion/manifest/service.py`'s `_DEFAULT_MAX_RESOLVED_PER_SUBPACKAGE`), and the GitHub dependents scrape caps at `max_dependents` (default 100) per lookup rather than trying to ingest a popular package's full reverse-dependency set. `write_service.py`'s bulk `*_batch` methods (used by both flows' hot paths) exist because of this ceiling too -- HydraDB only accelerates two write shapes under `UNWIND` (single-node `MERGE+SET` matched by `id`, two-endpoint relationship `MERGE` matched by `id`; there is no batched-read path at all), and **concurrent writes hit the ceiling far sooner than the same writes done sequentially** (confirmed by hand: 500 relationship `SET`s through a 16-worker pool failed within a second; the same 500 done one at a time took ~38s with zero failures) -- never thread-pool a write path here, only reads.

**gitnexus indexes into a global, machine-wide registry, not one scoped to the repo being analyzed.** `gitnexus list` shows every repo ever analyzed on the box. Once more than one repo has been indexed, a bare `gitnexus cypher` fails ("Multiple repositories indexed"); `gitnexus_client.py` always resolves an explicit `--repo` first. The registry key is **not** the directory basename either -- gitnexus derives it itself (usually from `package.json`'s `name` field), so a checkout at `.../serve-clone` registers as plain `"serve"`. `_resolve_repo_name()` handles this by parsing `gitnexus list`'s output and matching on its `Path:` field rather than guessing.
