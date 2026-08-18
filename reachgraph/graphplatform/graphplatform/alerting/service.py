"""Alerting Service -- Phase 5.

Subscribes to write stream events (new AFFECTS edges and new RESOLVED_VERSION_AT
edges), evaluates real-time exposure with strong consistency, deduplicates,
and dispatches context-rich notifications.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from .. import schema
from ..query.service import QueryReasoningService
from ..schema import now_utc, to_iso
from ..write_service import GraphWriteService
from .models import Alert, NotificationChannel
from .notifier import InMemoryAlertLog, WebhookNotifier

log = logging.getLogger("graphplatform.alerting.service")


class AlertingService:
    """Real-time supply chain alerting service."""

    def __init__(
        self,
        write_service: GraphWriteService,
        query_service: QueryReasoningService,
        *,
        notifiers: list[NotificationChannel] | None = None,
        alert_log: InMemoryAlertLog | None = None,
    ) -> None:
        self.write_service = write_service
        self.query_service = query_service
        self.notifiers = notifiers or []
        self.alert_log = alert_log or InMemoryAlertLog()
        self._seen_dedupe_keys: set[str] = set()

    def add_notifier(self, notifier: NotificationChannel) -> None:
        self.notifiers.append(notifier)

    def trigger_advisory_written(
        self,
        advisory_key: str,
        target_key: str,
        severity: str,
        summary: str = "",
    ) -> list[Alert]:
        """Triggered when a new AFFECTS edge is written.
        Uses strong consistency to avoid stale reads on fresh writes.
        """
        pkg_key = target_key.split("@")[0] if "@" in target_key else target_key
        version_key = target_key if "@" in target_key else f"{target_key}@*"

        # Query exposures with strong consistency
        exposures = self.query_service.transitive_exposure(target_key, consistency="strong")
        blast = self.query_service.blast_radius(target_key, consistency="strong")

        exposed_apps = [exp.to_dict() for exp in exposures]

        # Only emit alert if there is exposure or blast radius impact
        if not exposed_apps and blast.total_reached == 0:
            log.debug("no exposure found for advisory %s on %s; skipping alert", advisory_key, target_key)
            return []

        alert = Alert(
            alert_id=f"alert-{uuid.uuid4().hex[:8]}",
            advisory_id=advisory_key,
            package_key=pkg_key,
            version_key=version_key,
            severity=severity.upper(),
            summary=summary or f"Vulnerability {advisory_key} affects {target_key}",
            exposed_applications=exposed_apps,
            blast_radius_summary=blast.to_dict(),
            trigger_type="new_advisory",
            created_at=to_iso(now_utc()),
        )

        if alert.dedupe_key in self._seen_dedupe_keys:
            log.info("deduplicating already-emitted alert: %s", alert.dedupe_key)
            return []

        self._seen_dedupe_keys.add(alert.dedupe_key)
        self.alert_log.record(alert)

        for n in self.notifiers:
            try:
                n.send(alert)
            except Exception:
                log.exception("notifier %s failed on alert %s", getattr(n, "name", "unknown"), alert.alert_id)

        return [alert]

    def trigger_resolution_written(
        self,
        app_key: str,
        version_key: str,
    ) -> list[Alert]:
        """Triggered when a new RESOLVED_VERSION_AT edge is written.
        Checks for any existing known advisories against this resolved version
        using strong consistency.
        """
        pkg_key = version_key.split("@")[0] if "@" in version_key else version_key

        # Check advisories on the specific version and on the package
        advs_v = self.write_service.get_advisories_for(schema.VERSION, version_key, consistency="strong")
        advs_p = self.write_service.get_advisories_for(schema.PACKAGE, pkg_key, consistency="strong")
        all_advs = {a["advisory_key"]: a for a in (advs_v + advs_p)}.values()

        if not all_advs:
            return []

        emitted: list[Alert] = []
        for adv in all_advs:
            advisory_key = adv["advisory_key"]
            severity = adv.get("severity") or "HIGH"

            exposures = self.query_service.transitive_exposure(version_key, consistency="strong")
            blast = self.query_service.blast_radius(version_key, consistency="strong")
            exposed_apps = [exp.to_dict() for exp in exposures if exp.application_key == app_key] or [
                {"application_key": app_key, "depth": 1, "resolved_version": version_key, "path": [app_key, version_key]}
            ]

            alert = Alert(
                alert_id=f"alert-{uuid.uuid4().hex[:8]}",
                advisory_id=advisory_key,
                package_key=pkg_key,
                version_key=version_key,
                severity=severity.upper(),
                summary=f"Application {app_key} deployed version {version_key} with known advisory {advisory_key}",
                exposed_applications=exposed_apps,
                blast_radius_summary=blast.to_dict(),
                trigger_type="new_resolution",
                created_at=to_iso(now_utc()),
            )

            if alert.dedupe_key in self._seen_dedupe_keys:
                continue

            self._seen_dedupe_keys.add(alert.dedupe_key)
            self.alert_log.record(alert)
            emitted.append(alert)

            for n in self.notifiers:
                try:
                    n.send(alert)
                except Exception:
                    log.exception("notifier failed on alert %s", alert.alert_id)

        return emitted
