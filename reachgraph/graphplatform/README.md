# graphplatform -- Complete ReachGraph Platform (Phases 1 - 6)

A high-performance supply-chain vulnerability graph and reachability reasoning engine backed by a **live local HydraDB** instance (`neo4j://127.0.0.1:7687`, HTTP `127.0.0.1:8443`, admin `127.0.0.1:9090`).

`graphplatform/write_service.py` (`GraphWriteService`) is the gatekeeper to HydraDB, managing integer-identity hashing, mirror property persistence, and consistency controls (`causal` vs. `strong`).

---

## Complete Architecture Overview

```
                        ┌─────────────────────────────────────────────────────────────┐
                        │                       PRODUCT SURFACES                      │
                        │  Web UI (8080)   •   REST API (8081)   •   GitHub App Bot   │
                        └──────────────┬──────────────────────────────┬───────────────┘
                                       │                              │
                                       ▼                              ▼
                 ┌───────────────────────────────────────┐  ┌───────────────────────────────────┐
                 │       QUERY & REASONING SERVICE       │  │        REAL-TIME ALERTING         │
                 │  - Q1: Transitive Exposure (BFS)      │  │  - Advisory written trigger       │
                 │  - Q2: Introducing Version Precision  │  │  - New resolution written trigger │
                 │  - Q3: Live Resolution Window Overlap │  │  - Webhook & Slack notifiers      │
                 │  - Q4: Complete Outward Blast Radius  │  │  - Deduplicated memory/event log  │
                 │  - Q5: Nearby Typosquat Proximity     │  └─────────────────┬─────────────────┘
                 │  - Q6: Shared Maintainers & Infra     │                    │
                 │  - Predictive Propagation Cascade     │                    ▼
                 │  - Early-Warning Behavioral Radar     │  ┌───────────────────────────────────┐
                 │  - Chained-Vulnerability Detector     │  │       RECONCILIATION SWEEP        │
                 └──────────────────┬────────────────────┘  │  - Cross-stream audit sweeps      │
                                    │                       │  - Discrepancy reporting          │
                                    │                       │  - Automatic self-healing         │
                                    │                       └─────────────────┬─────────────────┘
                                    ▼                                         │
                 ┌────────────────────────────────────────────────────────────┴────────┐
                 │                          GRAPH WRITE SERVICE                        │
                 │              HydraDB Gatekeeper & Dialect Adaptation Layer          │
                 └──────────────────────────────────────┬──────────────────────────────┘
                                                        │ (Bolt / Cypher)
                                                        ▼
                 ┌─────────────────────────────────────────────────────────────────────┐
                 │                       HYDRADB GRAPH INSTANCE                        │
                 │         Nodes: Package, Version, Application, Maintainer, Advisory  │
                 │       Edges: DEPENDS_ON, RESOLVED_VERSION_AT, PUBLISHED_BY, AFFECTS │
                 │             INTRODUCED_IN, SAME_MAINTAINER_AS, PREDICTED_EXPOSURE   │
                 └─────────────────────────────────────────────────────────────────────┘
```

---

## 6 Supply Chain Reasoning Questions

1. **Which internal applications are transitively exposed?**
   - Implemented via `QueryReasoningService.transitive_exposure(target_key)`: reverse BFS over `DEPENDS_ON` and `RESOLVED_VERSION_AT`.
2. **Which version introduced the vulnerability?**
   - Implemented via `QueryReasoningService.introducing_version(advisory_key)`: direct `INTRODUCED_IN` precision query with evidence type and confidence.
3. **Which applications resolved the compromised version while it was live?**
   - Implemented via `QueryReasoningService.live_resolutions(version_key, start_time, end_time)`: interval overlap arithmetic against `[resolved_at, superseded_at)`.
4. **What is the complete blast radius?**
   - Implemented via `QueryReasoningService.blast_radius(target_key, max_depth)`: outward BFS discovery of all dependent packages and consumer applications.
5. **Are there likely typosquat packages nearby?**
   - Implemented via `QueryReasoningService.nearby_typosquats(package_key)`: bidirectional `POSSIBLE_TYPOSQUAT_OF` traversal with similarity score.
6. **Which other packages share maintainers or infrastructure with it?**
   - Implemented via `QueryReasoningService.shared_maintainers_and_infra(package_key)`: traverses `SAME_MAINTAINER_AS` across verified email identities and `SHARES_INFRASTRUCTURE_WITH` across signing keys and CI.

---

## Historical Incident Evaluation Harness

ReachGraph includes a dedicated replay harness in `scripts/evaluate.py` replaying 3 real historical supply chain attacks:
1. **`event-stream` / `flatmap-stream` (Nov 2018)**: Pinpoints `event-stream@3.3.6` dependency injection targeting Copay wallet.
2. **`ua-parser-js` (Oct 2021)**: Flags malicious `preinstall.sh` cryptominer payload with high behavioral Socket risk (0.95).
3. **`colors.js` (Jan 2022)**: Identifies protestware infinite loop affecting AWS CDK and downstream dependents.

To run the evaluation benchmark:
```bash
source .venv/bin/activate
python scripts/evaluate.py
```

---

## Running the Services

### 1. Run the Complete Test Suite (82 Tests)
```bash
source .venv/bin/activate
pytest -v
```

### 2. Launch the Product REST API Server
```bash
source .venv/bin/activate
python scripts/server.py --port 8081
```

### 3. Run Supply Chain Reasoning CLI Demo
```bash
source .venv/bin/activate
python scripts/query_demo.py --package lodash --advisory GHSA-29mw-wpgm-hmr9
```

### 4. Run Reconciliation Audit Sweep
```bash
source .venv/bin/activate
python scripts/reconcile.py
```

### 5. Launch Full ReachGraph Dashboard & Backend
In top-level directory:
```bash
go build -o reachgraph .
./reachgraph -addr :8080
```
Open `http://localhost:8080` in your browser.
