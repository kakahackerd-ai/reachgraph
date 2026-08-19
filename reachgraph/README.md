# ReachGraph — Blast Radius for npm/PyPI Packages and GitHub Repos

ReachGraph answers one question: **if this dependency breaks or gets compromised, what does it actually reach?**

Two flows, both backed by a real local graph database (HydraDB, Bolt/Cypher):

1. **Package blast radius** — enter an npm or PyPI package name. ReachGraph fetches its registry metadata, finds the real packages that depend on it (via a GitHub `network/dependents` lookup, since neither registry exposes reverse dependencies directly), stores that as `Package`/`Application` nodes and `DEPENDS_ON` edges in HydraDB, and computes the outward blast radius.
2. **Repo blast radius** — enter a GitHub repo URL (including monorepos with multiple `package.json`/`requirements.txt` files). ReachGraph clones it, discovers every manifest/workspace, and builds a graph of which files import which dependencies. Pick a dependency, and it computes that dependency's blast radius within the repo.

A frontend (React + Three.js, 3D graph views, guided intro) is in progress — see [Status](#status) below. Until it lands, both flows are exercised through the REST API described below.

## Architecture

```
        Browser (frontend, in progress)
                    │
                    ▼
     Python REST API — graphplatform/scripts/server.py (:8081)
                    │
                    ▼
     GraphWriteService — graphplatform/graphplatform/write_service.py
                    │  Bolt / Cypher
                    ▼
     Local HydraDB graph instance
     neo4j://127.0.0.1:7687 (Bolt) · :8443 (HTTP) · :9090 (admin)
```

Everything lives under `graphplatform/` — see `graphplatform/README.md` for the schema, endpoints, and operational notes (including a real limitation of the local HydraDB backend worth reading before you hit it).

There is no Go component anymore. An earlier iteration of this project had a Go gateway plus a much larger scope (OSV/GHSA advisory ingestion, typosquat detection, cross-ecosystem maintainer-sharing analysis, predictive cascades, alerting, reconciliation sweeps, a GitHub bot). All of that was cut in favor of the two flows above; the graph schema, write service, and query service now only carry `Package`, `Version`, `Maintainer`, `Application`, `File` nodes and `DEPENDS_ON`, `RESOLVED_VERSION_AT`, `PUBLISHED_BY`, `CONTAINS`, `IMPORTS` edges.

## Running it

```bash
# 1. Local HydraDB container (data dir: setup/hydra-db-data/)
podman start hydradb-graphplatform

# 2. Python backend
cd graphplatform
source .venv/bin/activate
python scripts/server.py --port 8081
```

```bash
# Flow 1: package blast radius
curl -s -X POST http://127.0.0.1:8081/api/v2/lookup \
  -H "Content-Type: application/json" \
  -d '{"ecosystem":"npm","package":"is-number"}'

# Flow 2: repo scan -> job -> pick a dependency -> blast radius
curl -s -X POST http://127.0.0.1:8081/api/v2/scan-repo \
  -H "Content-Type: application/json" \
  -d '{"target":"https://github.com/<owner>/<repo>"}'
curl -s http://127.0.0.1:8081/api/v2/jobs/<job_id>
```

Run the test suite from `graphplatform/`: `pytest -v` (see `graphplatform/README.md` for what needs Redis/network and can be skipped offline).

## Status

- [x] HydraDB schema + write service (Package/Version/Maintainer/Application/File, DEPENDS_ON/RESOLVED_VERSION_AT/PUBLISHED_BY/CONTAINS/IMPORTS)
- [x] Package lookup + blast radius over whatever's already ingested (`/api/v2/lookup`)
- [x] Repo clone + monorepo manifest discovery + per-dependency in-repo blast radius (`/api/v2/scan-repo`) — verified live end-to-end
- [ ] GitHub dependents scrape (real reverse-dependency source for Flow 1 — `/api/v2/lookup` currently has no way to discover *who* depends on a package, only to compute blast radius once dependents are already in the graph)
- [ ] GitNexus-driven file/import graph for Flow 2 (`gitnexus analyze` + File/IMPORTS edges)
- [ ] `/api/v2/repo/blast-radius` (blast radius of a user-picked dependency from the built repo graph)
- [ ] React + Three.js frontend (intro scene, 3D graph views for both flows)
