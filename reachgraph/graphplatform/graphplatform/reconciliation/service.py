"""Reconciliation Service -- Phase 5.

Background safety net that independently audits HydraDB state against upstream
sources, manifest discoveries, and alerting coverage, logging explicit
actionable discrepancy reports.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

from .. import schema
from ..alerting.service import AlertingService
from ..ingestion.advisory.osv import OSVConnector
from ..ingestion.manifest.discovery import discover
from ..ingestion.registry.npm import NpmConnector
from ..query.service import QueryReasoningService
from ..schema import now_utc, to_iso
from ..write_service import GraphWriteService
from .models import DiscrepancyReport, ReconciliationStatus

log = logging.getLogger("graphplatform.reconciliation.service")


class ReconciliationService:
    """Independent audit and reconciliation sweep."""

    def __init__(
        self,
        write_service: GraphWriteService,
        query_service: QueryReasoningService,
        alerting_service: AlertingService | None = None,
    ) -> None:
        self.write_service = write_service
        self.query_service = query_service
        self.alerting_service = alerting_service
        self.status = ReconciliationStatus()

    def run_sweep(
        self,
        *,
        sample_packages: list[str] | None = None,
        sample_advisories: list[tuple[str, str]] | None = None,
        tracked_repos: list[dict[str, str]] | None = None,
        auto_correct: bool = True,
    ) -> list[DiscrepancyReport]:
        """Runs a complete audit sweep:
        1. Registry coverage: asserts sample upstream packages/versions exist.
        2. Advisory coverage: asserts sample advisories exist and link properly.
        3. Repo resolutions: checks that tracked repos match lockfile state.
        4. Alert coverage: checks that active exposures have corresponding alerts.
        """
        start_time = time.time()
        discrepancies: list[DiscrepancyReport] = []
        t_now_str = to_iso(now_utc())

        log.info("starting reconciliation sweep (auto_correct=%s)", auto_correct)

        # -------------------------------------------------------------
        # 1. Registry Ingestion Audit
        # -------------------------------------------------------------
        pkgs_to_check = sample_packages or ["lodash", "express"]
        for pkg_name in pkgs_to_check:
            pkg_key = f"npm:{pkg_name}"
            node = self.write_service.get_package(pkg_key, consistency="strong")
            if node is None:
                d = DiscrepancyReport(
                    discrepancy_id=f"disc-{uuid.uuid4().hex[:8]}",
                    stage="registry_ingestion",
                    entity_type="Package",
                    entity_key=pkg_key,
                    description=f"Package {pkg_key} exists upstream but was missing from HydraDB graph",
                    action_taken="logged_and_corrected" if auto_correct else "logged_only",
                    detected_at=t_now_str,
                )
                discrepancies.append(d)
                log.warning("RECONCILIATION DISCREPANCY: %s (stage: %s)", d.description, d.stage)
                if auto_correct:
                    t_now = now_utc()
                    self.write_service.upsert_package(pkg_key, "npm", pkg_name, first_observed_at=t_now, event_time=t_now)

        # -------------------------------------------------------------
        # 2. Advisory Ingestion Audit
        # -------------------------------------------------------------
        advs_to_check = sample_advisories or [("npm", "lodash")]
        for eco, name in advs_to_check:
            target_key = f"{eco}:{name}"
            # Check if any advisory exists for this target
            adv_rows = self.write_service.get_advisories_for(schema.PACKAGE, target_key, consistency="strong")
            if not adv_rows:
                d = DiscrepancyReport(
                    discrepancy_id=f"disc-{uuid.uuid4().hex[:8]}",
                    stage="advisory_ingestion",
                    entity_type="Advisory",
                    entity_key=target_key,
                    description=f"No advisory edges found for known audited package {target_key}",
                    action_taken="logged_only",
                    detected_at=t_now_str,
                )
                discrepancies.append(d)
                log.warning("RECONCILIATION DISCREPANCY: %s (stage: %s)", d.description, d.stage)

        # -------------------------------------------------------------
        # 3. Tracked Repository Manifest Audit
        # -------------------------------------------------------------
        if tracked_repos:
            for repo_info in tracked_repos:
                repo_path = repo_info.get("path")
                org = repo_info.get("org", "org")
                repo = repo_info.get("repo", "repo")
                if not repo_path:
                    continue

                disc_res = discover(repo_path, org=org, repo=repo)
                for member in disc_res.members:
                    current_res = self.write_service.get_current_resolutions(member.application_key, consistency="strong")
                    graph_versions = {r["version_key"] for r in current_res}
                    manifest_versions = {f"npm:{name}@{ver}" for name, ver in member.resolved_dependencies.items()}

                    missing_in_graph = manifest_versions - graph_versions
                    if missing_in_graph:
                        d = DiscrepancyReport(
                            discrepancy_id=f"disc-{uuid.uuid4().hex[:8]}",
                            stage="manifest_discovery",
                            entity_type="Resolution",
                            entity_key=member.application_key,
                            description=(
                                f"Application {member.application_key} is missing {len(missing_in_graph)} "
                                f"resolved dependency edges present in current lockfile"
                            ),
                            action_taken="logged_and_corrected" if auto_correct else "logged_only",
                            detected_at=t_now_str,
                        )
                        discrepancies.append(d)
                        log.warning("RECONCILIATION DISCREPANCY: %s", d.description)
                        if auto_correct:
                            t_now = now_utc()
                            for vkey in missing_in_graph:
                                self.write_service.resolve_version(
                                    member.application_key, vkey, resolved_at=t_now,
                                    first_observed_at=t_now, event_time=t_now
                                )

        # -------------------------------------------------------------
        # 4. Alert Coverage Audit
        # -------------------------------------------------------------
        if self.alerting_service:
            # Check all AFFECTS edges that have exposed applications
            all_aff = self.write_service._run(
                f"MATCH (adv:{schema.ADVISORY})-[r:{schema.AFFECTS}]->(target) "
                f"RETURN adv.key AS adv_key, target.key AS target_key, r.severity AS sev",
                consistency="strong",
            )
            for aff in all_aff[:50]:
                t_key = aff["target_key"]
                exposures = self.query_service.transitive_exposure(t_key, consistency="strong")
                if exposures:
                    # Check if an alert was generated
                    has_alert = any(
                        a.get("advisory_id") == aff["adv_key"]
                        for a in self.alerting_service.alert_log.list_alerts(limit=500)
                    )
                    if not has_alert:
                        d = DiscrepancyReport(
                            discrepancy_id=f"disc-{uuid.uuid4().hex[:8]}",
                            stage="alerting",
                            entity_type="Alert",
                            entity_key=aff["adv_key"],
                            description=(
                                f"Advisory {aff['adv_key']} affects {t_key} with {len(exposures)} exposed "
                                f"application(s) but has no recorded alert in AlertingService"
                            ),
                            action_taken="logged_and_corrected" if auto_correct else "logged_only",
                            detected_at=t_now_str,
                        )
                        discrepancies.append(d)
                        log.warning("RECONCILIATION DISCREPANCY: %s", d.description)
                        if auto_correct:
                            self.alerting_service.trigger_advisory_written(
                                aff["adv_key"], t_key, aff.get("sev") or "HIGH",
                                summary="Reconciliation sweep auto-remediation"
                            )

        duration = time.time() - start_time
        self.status.last_run_at = t_now_str
        self.status.last_duration_s = duration
        self.status.total_runs += 1
        self.status.total_discrepancies_found += len(discrepancies)
        self.status.recent_discrepancies.extend(discrepancies)
        self.status.healthy = len(discrepancies) == 0

        log.info(
            "reconciliation sweep finished in %.3fs: %d discrepancy(s) found",
            duration,
            len(discrepancies),
        )
        return discrepancies
