"""Integration tests for Phase 5 Alerting & Reconciliation."""

from __future__ import annotations

import datetime as dt
import pytest

from graphplatform import schema
from graphplatform.alerting.notifier import InMemoryAlertLog, WebhookNotifier
from graphplatform.alerting.service import AlertingService
from graphplatform.query.service import QueryReasoningService
from graphplatform.reconciliation.service import ReconciliationService

T0 = dt.datetime(2021, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2021, 6, 1, 12, 0, tzinfo=dt.timezone.utc)


def test_alerting_trigger_on_advisory_written(service, cleanup, run_id):
    pkg_key = f"npm:vuln-lib-{run_id}"
    ver_key = f"{pkg_key}@1.2.3"
    app_key = f"app:org-{run_id}/prod-service"
    adv_key = f"osv:GHSA-alert-{run_id}"

    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.APPLICATION, app_key)
    cleanup(schema.ADVISORY, adv_key)

    service.upsert_package(pkg_key, "npm", f"vuln-lib-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.2.3", first_observed_at=T0, event_time=T0)
    service.upsert_application(app_key, f"org-{run_id}", "prod-service", first_observed_at=T0, event_time=T0)
    service.resolve_version(app_key, ver_key, resolved_at=T0, first_observed_at=T0, event_time=T0)

    # Set up Alerting Service with mock webhook and memory log
    query_svc = QueryReasoningService(service)
    alert_log = InMemoryAlertLog()
    webhook = WebhookNotifier()
    alert_svc = AlertingService(service, query_svc, notifiers=[webhook], alert_log=alert_log)

    # Write AFFECTS edge
    service.upsert_advisory(adv_key, "osv", f"GHSA-alert-{run_id}", "Critical RCE", first_observed_at=T1, event_time=T1)
    service.write_affects(adv_key, "Version", ver_key, advisory_published_at=T1, severity="CRITICAL", first_observed_at=T1, event_time=T1)

    # Trigger alerting
    alerts = alert_svc.trigger_advisory_written(adv_key, ver_key, "CRITICAL", summary="Critical RCE")
    assert len(alerts) == 1
    a = alerts[0]
    assert a.advisory_id == adv_key
    assert a.severity == "CRITICAL"
    assert any(exp["application_key"] == app_key for exp in a.exposed_applications)
    assert alert_log.count == 1

    # Verify deduplication: second trigger must not duplicate
    dup_alerts = alert_svc.trigger_advisory_written(adv_key, ver_key, "CRITICAL")
    assert len(dup_alerts) == 0
    assert alert_log.count == 1


def test_alerting_trigger_on_resolution_written(service, cleanup, run_id):
    pkg_key = f"npm:known-bad-{run_id}"
    ver_key = f"{pkg_key}@1.0.0"
    app_key = f"app:org-{run_id}/new-deploy"
    adv_key = f"osv:GHSA-pre-existing-{run_id}"

    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.APPLICATION, app_key)
    cleanup(schema.ADVISORY, adv_key)

    service.upsert_package(pkg_key, "npm", f"known-bad-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_advisory(adv_key, "osv", f"GHSA-pre-existing-{run_id}", "Pre-existing flaw", first_observed_at=T0, event_time=T0)
    service.write_affects(adv_key, "Version", ver_key, advisory_published_at=T0, severity="HIGH", first_observed_at=T0, event_time=T0)

    query_svc = QueryReasoningService(service)
    alert_log = InMemoryAlertLog()
    alert_svc = AlertingService(service, query_svc, notifiers=[WebhookNotifier()], alert_log=alert_log)

    # New application deploys the vulnerable version
    service.upsert_application(app_key, f"org-{run_id}", "new-deploy", first_observed_at=T1, event_time=T1)
    service.resolve_version(app_key, ver_key, resolved_at=T1, first_observed_at=T1, event_time=T1)

    alerts = alert_svc.trigger_resolution_written(app_key, ver_key)
    assert len(alerts) >= 1
    assert alerts[0].trigger_type == "new_resolution"
    assert alerts[0].package_key == pkg_key


def test_reconciliation_sweep_audit_and_auto_correct(service, cleanup, run_id):
    query_svc = QueryReasoningService(service)
    alert_svc = AlertingService(service, query_svc)
    reconcile_svc = ReconciliationService(service, query_svc, alert_svc)

    # Run sweep with intentional missing package to verify discrepancy reporting
    missing_pkg = f"reconcile-test-pkg-{run_id}"
    discrepancies = reconcile_svc.run_sweep(
        sample_packages=[missing_pkg],
        auto_correct=True,
    )

    assert len(discrepancies) >= 1
    d = next(disc for disc in discrepancies if disc.entity_key == f"npm:{missing_pkg}")
    assert d.stage == "registry_ingestion"
    assert d.action_taken == "logged_and_corrected"

    # Status check
    status = reconcile_svc.status.to_dict()
    assert status["total_runs"] >= 1
    assert status["total_discrepancies_found"] >= 1
