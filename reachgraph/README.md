# ReachGraph — Predictive Supply Chain Graph & Blast Radius Engine

> **ReachGraph** is an enterprise-grade supply-chain security and blast-radius reasoning platform powered by a live, local **HydraDB** graph cluster. It ingests multi-ecosystem registries (**npm** and **PyPI**), monitors global advisory feeds (**OSV** and **GitHub Advisory Database / GHSA**), resolves transitive dependency trees, tracks cross-ecosystem maintainer accounts, and computes deterministic blast radius with predictive cascade forecasting.

---

## 📑 Table of Contents

1. [Platform Architecture & Topology](#1-platform-architecture--topology)
2. [How HydraDB is Used in ReachGraph](#2-how-hydradb-is-used-in-reachgraph)
3. [Blast Radius: Calculation & Predictive Forecasting](#3-blast-radius-calculation--predictive-forecasting)
4. [Registry Ingestion: npm & PyPI Package & Maintainer Pipelines](#4-registry-ingestion-npm--pypi-package--maintainer-pipelines)
5. [Vulnerability Timestamps & Temporal Exposure Intervals](#5-vulnerability-timestamps--temporal-exposure-intervals)
6. [Cross-Stream Reconciliation & Self-Healing](#6-cross-stream-reconciliation--self-healing)
7. [The 6 Core Supply Chain Reasoning Questions](#7-the-6-core-supply-chain-reasoning-questions)
8. [Historical Attack Incident Benchmark (100% Accuracy)](#8-historical-attack-incident-benchmark-100-accuracy)
9. [Interactive Cyber Graph Web Dashboard](#9-interactive-cyber-graph-web-dashboard)
10. [Quickstart & Operations Guide](#10-quickstart--operations-guide)

---

## 1. Platform Architecture & Topology

ReachGraph operates as a unified dual-server engine connected to a local, multi-node HydraDB graph cluster:

```
                                  ┌────────────────────────────────────────────────────────┐
                                  │               ReachGraph Web Dashboard                 │
                                  │      (Single-Page Cyber Command Center on :8080)       │
                                  └─────────────────────────┬──────────────────────────────┘
                                                            │
                                  ┌─────────────────────────▼──────────────────────────────┐
                                  │                  Go Gateway Server                     │
                                  │       Reverse Proxy / Live Manifest Resolver (:8080)   │
                                  └───────────────┬──────────────────────────┬─────────────┘
                                                  │                          │
                        /api/v2/* Reverse Proxy  │                          │ Local Cypher Query
                                                  │                          │ POST /api/ask
                                  ┌───────────────▼───────────┐  ┌───────────▼─────────────┐
                                  │ ReachGraph Python REST API │  │  Local HydraDB Engine   │
                                  │  (Query & Enrichment :8081)│  │   HTTP API on :8443     │
                                  └───────────────┬───────────┘  │   Bolt Cypher on :7687  │
                                                  │              └───────────▲─────────────┘
                                                  └──────────────────────────┘
                                                    Live Cypher Graph Transactions
```

- **Go Gateway Server (`:8080`)**: Embedded web dashboard, repository manifest scanner, raw GitHub AST scanner, and API reverse proxy.
- **Python Product Engine (`:8081`)**: REST microservices for Phase 4–6 reasoning, package lookup with token-bucket rate limiting, async repository discovery, alert logs, and reconciliation sweeps.
- **Local HydraDB Cluster (`:7687` Bolt / `:8443` HTTP)**: Multi-tenant causal graph database enforcing strict causal consistency and deterministic integer hashing.

---

## 2. How HydraDB is Used in ReachGraph

HydraDB acts as the authoritative causal graph database for ReachGraph, providing deterministic traversal, graph partitioning, and temporal versioning.

### A. Graph Schema & Idempotent Cypher Writes
HydraDB uses strict OpenCypher dialect rules. All entity IDs are hashed into 63-bit signed integers:
$$\text{vertex\_id} = \text{hash64}(\text{key}) \pmod{2^{63}-1}$$

```cypher
// Idempotent Package & Version Ingestion
UNWIND $rows AS row
MERGE (n {id: row.id})
SET n:Package,
    n.key = row.key,
    n.name = row.name,
    n.ecosystem = row.ecosystem,
    n.rel_id = row.id
```

### B. Graph Entity Model
| Node Label | Key Pattern | Description |
|---|---|---|
| `Package` | `npm:lodash`, `pypi:cryptography` | Top-level ecosystem package |
| `PackageVersion` | `npm:lodash@4.17.21` | Specific release version with AST metadata |
| `Advisory` | `ghsa:GHSA-29mw-wpgm-hmr9`, `osv:CVE-2021-23337` | Vulnerability & malware advisory |
| `Application` | `app:fintech/copay-wallet`, `app:org/core-web` | Deployed internal microservice or monorepo |
| `Maintainer` | `maintainer:npm:dougwilson`, `maintainer:pypi:alex` | Registry author/maintainer identity |
| `SigningKey` | `key:gpg:3E034170` | Cryptographic commit / package signing key |
| `SocketRiskSignal` | `signal:npm:ua-parser-js@0.7.29` | Behavioral telemetry & install-script risk |

### C. Natural-Language Ask Engine (`POST /api/ask`)
The **Ask HydraDB** feature translates free-text security questions directly into local Cypher queries on `http://127.0.0.1:8443/v1/graphs/default/query` using cell `cell-0` and returns structured graph triplets (`Subject -> Predicate -> Object`), citations, and relevance confidence scores.

---

## 3. Blast Radius: Calculation & Predictive Forecasting

ReachGraph implements both **retrospective blast radius** and **predictive cascade forecasting**.

### A. Deterministic Blast Radius Traversal
When a package or release is compromised, ReachGraph executes an outward Breadth-First Search (BFS) starting at depth 0:

$$\text{BlastRadius}(v_0) = \{ v \in V \mid \exists \text{ path } v_0 \xrightarrow{\text{DEPENDS\_ON}^*} v \lor v_0 \xrightarrow{\text{RESOLVED\_VERSION\_AT}} v \}$$

- **Hops & Depth**: Traverses through direct dependencies, sub-packages, and production application nodes.
- **Reverse Exposure Isolation**: Automatically isolates internal microservices exposed via transitive paths:
  $$\text{Target} \rightarrow \text{Intermediate Dependency} \rightarrow \text{Internal Service}$$

### B. Predictive Cascade Forecasting
Even before an advisory is published or an application deploys an update, ReachGraph predicts downstream propagation:
1. **Semver Constraint Range Analysis**: Evaluates whether consumer manifests use loose range specifiers (`^4.17.0`, `~2.2.0`, `*`).
2. **Predictive Edge Synthesis**: Identifies applications that will automatically ingest the malicious version on next `npm install` / `pip install`.
3. **Graph Tagging**: Writes `PREDICTED_AFFECTS` relationships into HydraDB with probability metrics:

```cypher
MATCH (f:PackageVersion {key: $flagged_key})
MATCH (downstream:PackageVersion)-[:DEPENDS_ON]->(p:Package {name: f.name})
WHERE downstream.semver_constraint =~ '^\\^.*' OR downstream.semver_constraint = '*'
MERGE (f)-[r:PREDICTED_AFFECTS]->(downstream)
SET r.confidence = 0.90, r.reason = 'unpinned_semver_range'
```

### C. Chained Vulnerability Detection
ReachGraph discovers compound attack chains where a low-severity flaw feeds directly into an execution sink:
- **Source**: Prototype pollution in `npm:lodash` (`_.merge`, `_.set`).
- **Sink**: Dynamic template evaluation in `npm:ejs` / `npm:pug`.
- **Verdict**: Flags compound attack vector $\text{lodash} \rightarrow \text{ejs}$ and recommends specific prototype freezing mitigations.

---

## 4. Registry Ingestion: npm & PyPI Package & Maintainer Pipelines

ReachGraph features live, asynchronous ingestion connectors for public package registries.

```
                      ┌───────────────────────────────────────────────┐
                      │             Live Package Ingestion            │
                      └──────────────────────┬────────────────────────┘
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       │                                           │
         ┌─────────────▼─────────────┐               ┌─────────────▼─────────────┐
         │       npm Registry        │               │       PyPI Registry       │
         │ https://registry.npmjs.org│               │ https://pypi.org/pypi/... │
         └─────────────┬─────────────┘               └─────────────┬─────────────┘
                       │                                           │
                       │ Extract maintainers[{name,email}],        │ Extract maintainer, author,
                       │ install-scripts, integrity hashes         │ upload timestamps, wheels
                       │                                           │
                       └─────────────────────┬─────────────────────┘
                                             │
                               ┌─────────────▼─────────────┐
                               │   Maintainer Resolution   │
                               │   Cross-Ecosystem Merge   │
                               └─────────────┬─────────────┘
                                             │
                               ┌─────────────▼─────────────┐
                               │     Local HydraDB Bolt    │
                               │    SAME_MAINTAINER_AS     │
                               │ SHARES_INFRASTRUCTURE_WITH│
                               └───────────────────────────┘
```

### A. npm Registry Connector
- **Endpoint**: `https://registry.npmjs.org/<package>` (supports `@scope/package` URL escaping).
- **Metadata Extracted**:
  - `maintainers`: Array of `{ name: string, email: string }`.
  - `time`: Exact ISO-8601 release timestamps for each version.
  - `dist.shasum` & `dist.integrity`: SHA-512 tarball hashes.
  - `scripts.preinstall` / `scripts.postinstall`: Automated install-script flag detection (`hasInstallScript`).

### B. PyPI Registry Connector
- **Endpoint**: `https://pypi.org/pypi/<package>/json`.
- **Metadata Extracted**:
  - `info.maintainer`, `info.maintainer_email`, `info.author`, `info.author_email`.
  - `releases`: Release mapping with UTC upload timestamps.
  - Package summary, project URLs, and license classification.

### C. Cross-Ecosystem Maintainer Resolution
1. **Identity Normalization**: Canonicalizes usernames and emails:
   $$\text{CanonicalEmail}(\text{email}) = \text{lowercase}(\text{trim}(\text{email}))$$
2. **Account Linking**: Detects when the same human maintainer publishes packages across both npm and PyPI.
3. **Account Takeover (ATO) Blast Radius**: If a maintainer's account is compromised, ReachGraph queries `SAME_MAINTAINER_AS` to immediately enumerate all packages controlled by that identity across the entire software ecosystem:

```cypher
MATCH (p1:Package {key: $pkg_key})<-[:MAINTAINS]-(m:Maintainer)-[:MAINTAINS]->(p2:Package)
WHERE p1 <> p2
RETURN p2.key AS shared_package, m.name AS maintainer_name
```

---

## 5. Vulnerability Timestamps & Temporal Exposure Intervals

ReachGraph tracks exact timestamps to reconstruct timelines:

### A. Vulnerability Occurrence & Publication Timestamps
- **Introducing Version Precision**: Pinpoints the exact release that introduced a vulnerability (e.g. `npm:ua-parser-js@0.7.29` published at `2021-10-22T14:30:00Z`).
- **AST Diff & Heuristic Verification**: Determines if the advisory was introduced via malicious install scripts, AST modifications, or explicit advisory bounds.

### B. Active Exposure Interval Query (`Q3`)
Calculates the exact temporal overlap between an application's deployment window and the vulnerability's live duration:

$$\text{ActiveExposure} = [T_{\text{resolved}}, T_{\text{superseded}}) \cap [T_{\text{advisory\_published}}, T_{\text{patched}})$$

```bash
# Query live resolutions for a specific window
curl -s "http://127.0.0.1:8080/api/v2/query/resolutions?version=npm:lodash@4.17.20&window_start=2021-01-01T00:00:00Z"
```

---

## 6. Cross-Stream Reconciliation & Self-Healing

The **Reconciliation Engine** audits graph invariants across 4 disparate data streams:
1. **Registry Stream**: Verified against npm and PyPI releases.
2. **Advisory Stream**: Verified against OSV and GHSA feeds.
3. **Manifest Stream**: Verified against GitHub lockfiles and dependency trees.
4. **Alert Stream**: Verified against active security notifications.

- **Automated Self-Healing**: Discrepancies (missing alert records, un-indexed dependencies) are automatically repaired via idempotent Cypher transactions.
- **Audit Reports**: Generates structured `DiscrepancyReport` entries viewable in the Web Dashboard.

---

## 7. The 6 Core Supply Chain Reasoning Questions

| Question | Cypher Traversal & Algorithm | REST Endpoint |
|---|---|---|
| **[Q1] Transitive Exposure** | Reverse BFS through `DEPENDS_ON` and `RESOLVED_VERSION_AT` to find exposed internal applications | `GET /api/v2/query/exposure?target=npm:lodash` |
| **[Q2] Introducing Version** | Advisory range evaluation & dependency tree diffing to find introducing release and published timestamp | `GET /api/v2/query/introducing-version?advisory_id=GHSA-29mw-wpgm-hmr9` |
| **[Q3] Live Resolutions** | Temporal interval overlap checking over $[T_{\text{resolved}}, T_{\text{superseded}})$ | `GET /api/v2/query/resolutions?version=npm:lodash@4.17.20` |
| **[Q4] Blast Radius** | Outward BFS discovery of all connected downstream packages and services | `GET /api/v2/query/blast-radius?target=npm:lodash` |
| **[Q5] Typosquat Radar** | Normalized Damerau-Levenshtein edit-distance and homoglyph substitution matching | `GET /api/v2/query/typosquats?package=npm:lodash` |
| **[Q6] Shared Maintainers** | Multi-hop identity resolution across `MAINTAINS` and `SHARES_INFRASTRUCTURE_WITH` | `GET /api/v2/query/shared-maintainers?package=npm:lodash` |

---

## 8. Historical Attack Incident Benchmark (100% Accuracy)

ReachGraph includes a historical evaluation harness (`graphplatform/scripts/evaluate.py`) replaying 3 real supply chain attacks:

```
====================================================================================
 REACHGRAPH PHASE 6 -- HISTORICAL INCIDENT EVALUATION HARNESS
====================================================================================

 [INCIDENT 1] event-stream / flatmap-stream Supply Chain Attack (Nov 2018)
  • ReachGraph Exposure:    1 app(s) transitively exposed (app:fintech/copay-wallet)
  • Introducing Version:    npm:event-stream@3.3.6 (confidence: 0.95, diff: flatmap-stream added)
  • Graph Blast Radius:     2 node(s) reached across depth 2
  ✓ VERDICT: ACCURATE RECONSTRUCTION (Match with public post-mortem)

 [INCIDENT 2] ua-parser-js Hijacked Release (Oct 2021)
  • ReachGraph Exposure:    1 app(s) transitively exposed (app:org/core-web)
  • Introducing Version:    npm:ua-parser-js@0.7.29 (install-script payload: preinstall.sh)
  • Early-Warning Score:    0.95 (Behavioral risk: Socket score 0.95)
  ✓ VERDICT: ACCURATE RECONSTRUCTION (Early warning flagged high behavioral risk)

 [INCIDENT 3] colors.js Protestware Infinite Loop (Jan 2022)
  • ReachGraph Exposure:    1 app(s) transitively exposed (app:cloud/aws-service)
  • Blast Radius Reach:     1 connected node(s)
  ✓ VERDICT: ACCURATE RECONSTRUCTION (Downstream applications correctly isolated)

 EVALUATION SUMMARY: 3/3 INCIDENTS REPLAYED WITH 100% RECONSTRUCTION ACCURACY
====================================================================================
```

---

## 9. Interactive Cyber Graph Web Dashboard

The web dashboard ([`http://localhost:8080`](http://localhost:8080)) provides a single-page command center:

- **Multi-Layout Cyber Canvas**: Switch dynamically between **Force-Directed (Cose Physics)**, **Tree (Hierarchical)**, **Radial Concentric**, and **Circular** layouts.
- **Vulnerability Occurrence & Service Impact Radar**: Input any CVE or package to immediately view its introduction timestamp, relative elapsed time, and list of affected microservices.
- **Interactive Node Drawer**: Slide-out metadata inspector detailing Socket scores, AST install scripts, and CVE links.
- **Live Alert Feed**: Webhook dispatcher and real-time security alert log.
- **Reconciliation Center**: One-click graph integrity sweeps with automated self-healing.

---

## 10. Quickstart & Operations Guide

### Prerequisites
- **Go 1.22+**
- **Python 3.11+**
- **Local HydraDB Instance** running on `neo4j://127.0.0.1:7687` and `http://127.0.0.1:8443`

### Step 1: Start Python Product API
```bash
cd graphplatform
source .venv/bin/activate
pip install -r requirements.txt
python scripts/server.py --port 8081
```

### Step 2: Build & Start Go Gateway Server
```bash
cd ..
go build -o reachgraph .
./reachgraph -addr :8080
```

### Step 3: Run Automated Test Suite (82 Tests)
```bash
cd graphplatform
pytest -v
```

### Step 4: Run Historical Incident Evaluation Benchmark
```bash
python scripts/evaluate.py
```

### Step 5: Open Dashboard
Visit **[http://localhost:8080](http://localhost:8080)** in your browser.
