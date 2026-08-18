#!/usr/bin/env python3
"""Phase 5 Reconciliation Sweep CLI.

Runs the independent audit sweep across registry, advisory, manifest, and
alerting states and prints structured discrepancy reports.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService
from graphplatform.alerting.service import AlertingService
from graphplatform.query.service import QueryReasoningService
from graphplatform.reconciliation.service import ReconciliationService

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
    parser.add_argument("--dry-run", action="store_true", help="Log discrepancies without auto-correcting")
    parser.add_argument("--packages", nargs="*", default=["lodash", "express"], help="Sample packages to verify")
    args = parser.parse_args()

    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    token = _load_hydra_token()
    svc = GraphWriteService(uri, token)
    svc.verify_connectivity()

    query_svc = QueryReasoningService(svc)
    alert_svc = AlertingService(svc, query_svc)
    reconcile_svc = ReconciliationService(svc, query_svc, alert_svc)

    print("\n" + "=" * 80)
    print(" REACHGRAPH PHASE 5 -- RECONCILIATION AUDIT SWEEP")
    print("=" * 80)

    discrepancies = reconcile_svc.run_sweep(
        sample_packages=args.packages,
        auto_correct=not args.dry_run,
    )

    status = reconcile_svc.status.to_dict()
    print(f"\n[+] Sweep Status: {'HEALTHY' if status['healthy'] else 'DISCREPANCIES DETECTED'}")
    print(f"    Duration:     {status['last_duration_s']}s")
    print(f"    Found:        {len(discrepancies)} discrepancy report(s)")

    if discrepancies:
        print("\nDiscrepancy Details:")
        for i, d in enumerate(discrepancies, 1):
            print(f"  {i}. [{d.stage.upper()}] {d.entity_type}: {d.entity_key}")
            print(f"     Description:  {d.description}")
            print(f"     Action Taken: {d.action_taken}")
            print(f"     Detected At:  {d.detected_at}")
    else:
        print("\n[OK] Zero discrepancies found -- HydraDB is fully in sync with audited sample.")

    print("\n" + "=" * 80 + "\n")
    svc.close()


if __name__ == "__main__":
    main()
