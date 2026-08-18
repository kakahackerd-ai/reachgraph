#!/usr/bin/env python3
"""Phase 5 Alerting Worker CLI.

Subscribes to write stream events and dispatches alerts via configured webhooks.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graphplatform import GraphWriteService
from graphplatform.alerting.notifier import InMemoryAlertLog, WebhookNotifier
from graphplatform.alerting.service import AlertingService
from graphplatform.ingestion.events import STREAM_ADVISORY
from graphplatform.ingestion.queue import RedisStreamQueue
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
    parser.add_argument("--webhook", default=os.environ.get("ALERT_WEBHOOK_URL"), help="Webhook delivery URL")
    parser.add_argument("--max-events", type=int, default=None, help="Max events before stopping")
    parser.add_argument("--stop-after-idle", type=int, default=2, help="Stop after N idle reads")
    args = parser.parse_args()

    uri = os.environ.get("HYDRADB_URI", "neo4j://127.0.0.1:7687")
    token = _load_hydra_token()
    svc = GraphWriteService(uri, token)
    svc.verify_connectivity()

    query_svc = QueryReasoningService(svc)
    notifiers = [WebhookNotifier(args.webhook)] if args.webhook else [WebhookNotifier()]
    alert_log = InMemoryAlertLog()
    alert_svc = AlertingService(svc, query_svc, notifiers=notifiers, alert_log=alert_log)

    q = RedisStreamQueue(os.environ.get("GRAPHPLATFORM_REDIS_URL", "redis://127.0.0.1:6379/0"))
    total_processed = 0

    def handler(event: dict) -> None:
        nonlocal total_processed
        adv_id = event.get("advisory_id")
        sev = event.get("severity", "HIGH")
        for aff in event.get("affected", []):
            pkg_name = aff.get("package_name", "")
            eco = aff.get("ecosystem", "npm").lower()
            if pkg_name:
                t_key = f"{eco}:{pkg_name}"
                alerts = alert_svc.trigger_advisory_written(adv_id, t_key, sev, summary=event.get("summary", ""))
                for a in alerts:
                    print(f"🚨 ALERT EMITTED: [{a.severity}] {a.advisory_id} -> {len(a.exposed_applications)} application(s) exposed")
        total_processed += 1

    print(f"[*] Alert worker subscribed to {STREAM_ADVISORY}...")
    q.subscribe(
        STREAM_ADVISORY, "alerting-service", "worker-1", handler,
        stop_after_idle_reads=args.stop_after_idle, block_ms=2000, max_messages=args.max_events,
    )

    print(f"[+] Alert worker finished: processed {total_processed} event(s), recorded {alert_log.count} alert(s)")
    svc.close()
    q.close()


if __name__ == "__main__":
    main()
