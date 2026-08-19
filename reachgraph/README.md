# ReachGraph — Blast Radius for npm/PyPI Packages and GitHub Repos

ReachGraph answers one question: **if this dependency breaks or gets compromised, what does it actually reach?**

Two flows, both backed by a real local graph database (HydraDB, Bolt/Cypher) and a React + Three.js frontend with 3D graph views:

1. **Package blast radius** — enter an npm or PyPI package name. ReachGraph fetches its registry metadata, finds the real packages that depend on it (via a GitHub `network/dependents` scrape, since neither registry exposes reverse dependencies directly), stores that as `Package`/`Application` nodes and `DEPENDS_ON` edges in HydraDB, and renders the outward blast radius in 3D.
2. **Repo blast radius** — enter a GitHub repo URL (including monorepos with multiple `package.json`/`requirements.txt` files). ReachGraph clones it, discovers every manifest/workspace, and builds its dependency graph. Pick a dependency, and it highlights that dependency's blast radius within the repo.

## Architecture

```
     frontend/ — React + Vite + Three.js (:5173 dev)
                    │  /api → proxied
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

`graphplatform/` is the backend — see `graphplatform/README.md` for the schema, endpoints, and operational notes (including a real limitation of the local HydraDB backend worth reading before you hit it). `frontend/` is the UI — see below for how to run it.

There is no Go component anymore. An earlier iteration of this project had a Go gateway plus a much larger scope (OSV/GHSA advisory ingestion, typosquat detection, cross-ecosystem maintainer-sharing analysis, predictive cascades, alerting, reconciliation sweeps, a GitHub bot). All of that was cut in favor of the two flows above; the graph schema, write service, and query service now only carry `Package`, `Version`, `Maintainer`, `Application`, `File` nodes and `DEPENDS_ON`, `RESOLVED_VERSION_AT`, `PUBLISHED_BY`, `CONTAINS`, `IMPORTS` edges.

## Running it

```bash
# 1. Local HydraDB container (data dir: setup/hydra-db-data/)
podman start hydradb-graphplatform

# 2. Python backend
cd graphplatform
source .venv/bin/activate
python scripts/server.py --port 8081

# 3. Frontend (separate terminal)
cd frontend
npm install   # first time only
npm run dev   # http://localhost:5173, proxies /api to :8081
```

Open `http://localhost:5173` — the intro screen explains both flows, or jump straight to `/npm` or `/repo`.

Backend only, no UI:

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

Run the backend test suite from `graphplatform/`: `pytest -v` (see `graphplatform/README.md` for what needs Redis/network and can be skipped offline). Frontend: `cd frontend && npx tsc -b && npm run build`.

## Status

- [x] HydraDB schema + write service (Package/Version/Maintainer/Application/File, DEPENDS_ON/RESOLVED_VERSION_AT/PUBLISHED_BY/CONTAINS/IMPORTS)
- [x] **Flow 1 complete**: real GitHub `network/dependents` scrape + deps.dev counts feed `/api/v2/lookup`, which resolves metadata, writes real dependents into HydraDB, and returns package info + a `{nodes,edges}` blast-radius graph
- [x] **Flow 2 core loop complete**: repo clone + monorepo manifest discovery build a real dependency graph (`/api/v2/scan-repo`); the frontend builds a client-side graph from it and highlights a picked dependency's in-repo blast radius — no gitnexus needed for this loop to work
- [x] **Frontend complete for both flows**: intro scene (3D ambient network + guided cards), package screen, repo screen — all driven by real backend data, verified live (Playwright against the real dev server + backend, zero console errors, screenshots confirm correct rendering)
- [ ] GitNexus-driven file/import graph for Flow 2 (`gitnexus analyze` + File/IMPORTS edges) — an enhancement layered on top of the working loop above, giving file-level "where is this imported" granularity rather than just package-level

### A note on HydraDB's write ceiling

The local HydraDB backend's GC is permanently broken on this object-store backend (see `graphplatform/README.md`), so writes eventually fail until the store is wiped. This is easy to hit in real use: scanning a real-world repo with a large lockfile (hundreds of transitive dependencies), or a few package lookups with `max_dependents` near 100, can exhaust it in a single session. If `/api/v2/scan-repo` or `/api/v2/lookup` start failing with `GraphSnapshot query is not supported yet`, wipe and restart per `graphplatform/README.md`'s "Known limitations" section — it's expected, not a regression.
