#!/usr/bin/env python3
"""Phase 4 Query & Reasoning Demo.

Demonstrates answering all six core supply-chain questions plus predictive
cascade forecasting and chained-vulnerability detection against real HydraDB data.

Usage:
    python scripts/query_demo.py --package lodash --advisory GHSA-29mw-wpgm-hmr9
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService
from graphplatform.query.service import QueryReasoningService

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
DEFAULT_TOKEN_FILE = os.path.join(REPO_ROOT, "setup", "hydra-db-data", "auth-token")


def _load_hydra_token() -> str:
    token = os.environ.get("HYDRADB_TOKEN")
    if token:
        return token
    return open(os.environ.get("HYDRADB_TOKEN_FILE", DEFAULT_TOKEN_FILE)).read().strip()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="lodash", help="Package name (e.g. lodash)")
    parser.add_argument("--ecosystem", default="npm", help="Ecosystem (npm or pypi)")
    parser.add_argument("--version", default="4.17.20", help="Version string")
    parser.add_argument("--advisory", default="GHSA-29mw-wpgm-hmr9", help="Advisory key/ID")
    parser.add_argument("--consistency", choices=["causal", "strong"], default="strong")
    args = parser.parse_args()

    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    token = _load_hydra_token()
    svc = GraphWriteService(uri, token)
    svc.verify_connectivity()
    query_svc = QueryReasoningService(svc)

    pkg_key = f"{args.ecosystem}:{args.package}"
    ver_key = f"{pkg_key}@{args.version}"
    adv_key = f"osv:{args.advisory}" if not args.advisory.startswith(("osv:", "ghsa:")) else args.advisory

    print("\n" + "=" * 80)
    print(" REACHGRAPH PHASE 4 -- SUPPLY CHAIN QUERY & REASONING REPORT")
    print("=" * 80)
    print(f" Target Package:   {pkg_key}")
    print(f" Target Version:   {ver_key}")
    print(f" Advisory:         {adv_key}")
    print(f" Read Consistency: {args.consistency}")
    print("-" * 80)

    # Question 1: Transitive Exposure
    print("\n[Q1] Which internal applications are transitively exposed?")
    exposures = query_svc.transitive_exposure(ver_key, consistency=args.consistency)
    if exposures:
        for exp in exposures:
            print(f"  -> App: {exp.application_key} (depth={exp.depth}, path={' -> '.join(exp.path)})")
    else:
        print("  -> None currently exposed in graph.")

    # Question 2: Introducing Version
    print("\n[Q2] Which version introduced the vulnerability?")
    intro = query_svc.introducing_version(adv_key, consistency=args.consistency)
    print(f"  -> Introducing Version: {intro.introducing_version_key or 'Unknown'}")
    print(f"     Confidence:          {intro.confidence:.2f} (precise={intro.precise})")
    print(f"     Evidence:            {intro.evidence}")

    # Question 3: Live Resolutions in Window
    print("\n[Q3] Which applications resolved the compromised version while it was live?")
    live_res = query_svc.live_resolutions(ver_key, window_start="2020-01-01T00:00:00Z", consistency=args.consistency)
    if live_res:
        for lr in live_res:
            print(f"  -> {lr.application_key} (resolved: {lr.resolved_at}, superseded: {lr.superseded_at or 'STILL_LIVE'})")
    else:
        print("  -> No active or historical applications recorded in window.")

    # Question 4: Complete Blast Radius
    print("\n[Q4] What is the complete blast radius?")
    blast = query_svc.blast_radius(ver_key, max_depth=6, consistency=args.consistency)
    print(f"  -> Total Reached:      {blast.total_reached} nodes")
    print(f"  -> Max Traversal Depth: {blast.max_depth}")
    print(f"  -> Packages ({len(blast.packages)}):       {', '.join(blast.packages[:6]) or 'None'}")
    print(f"  -> Applications ({len(blast.applications)}):   {', '.join(blast.applications[:6]) or 'None'}")

    # Question 5: Nearby Typosquats
    print("\n[Q5] Are there likely typosquat packages nearby?")
    typos = query_svc.nearby_typosquats(pkg_key, consistency=args.consistency)
    if typos:
        for t in typos[:5]:
            print(f"  -> {t.package_key} ~ {t.popular_target} (score={t.similarity_score:.2f}, method={t.method})")
    else:
        print("  -> No typosquats flagged for this package.")

    # Question 6: Shared Maintainers or Infrastructure
    print("\n[Q6] Which other packages share maintainers or infrastructure?")
    shared = query_svc.shared_maintainers_and_infra(pkg_key, consistency=args.consistency)
    if shared:
        for s in shared[:5]:
            print(f"  -> {s.connected_package_key} via {s.link_type} ({s.evidence_type}, conf={s.confidence:.2f})")
    else:
        print("  -> No shared maintainer or infrastructure links found.")

    # Predictive Impact: Propagation & Early Warning
    print("\n" + "-" * 80)
    print(" PREDICTIVE IMPACT & CASCADE FORECASTING")
    print("-" * 80)
    prop = query_svc.predict_propagation(ver_key, write_to_graph=False, consistency=args.consistency)
    print(f"[*] Propagation Forecast (Unresolved Consumers): {len(prop)} candidate(s)")
    for p in prop[:3]:
        print(f"    -> {p.consumer_key} (range: {p.declared_range}, conf: {p.confidence:.2f}) [PREDICTED]")

    early = query_svc.predict_early_warning(pkg_key, write_to_graph=False, consistency=args.consistency)
    print(f"[*] Early-Warning Risk Score: {early.risk_score:.3f} (confidence: {early.confidence:.2f}) [PREDICTED]")
    for factor in early.contributing_factors:
        print(f"    - {factor['signal']}: {factor['detail']}")

    # Chained-Vuln Stub
    print("\n[*] Chained-Vulnerability Detection:")
    chain = query_svc.detect_chain("npm:lodash", "npm:ejs", consistency=args.consistency)
    if chain:
        print(f"    [!] CHAIN DETECTED: {chain.risk_type}")
        print(f"        {chain.description}")
        print(f"        Mitigation: {chain.mitigation}")
    else:
        print("    -> No known multi-package chain detected.")

    print("\n" + "=" * 80 + "\n")
    svc.close()


if __name__ == "__main__":
    main()
