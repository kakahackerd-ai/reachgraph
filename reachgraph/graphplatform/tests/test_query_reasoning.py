"""Integration tests for Phase 4 QueryReasoningService against live HydraDB."""

from __future__ import annotations

import datetime as dt
import pytest

from graphplatform import schema
from graphplatform.query.service import QueryReasoningService

T0 = dt.datetime(2021, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2021, 6, 1, 12, 0, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2022, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def query_service(service):
    return QueryReasoningService(service)


def test_transitive_exposure_direct_and_multi_hop(service, query_service, cleanup, run_id):
    pkg_a = f"npm:pkg-a-{run_id}"
    pkg_b = f"npm:pkg-b-{run_id}"
    ver_b = f"{pkg_b}@1.0.0"
    app = f"app:org-{run_id}/service-1"

    cleanup(schema.PACKAGE, pkg_a)
    cleanup(schema.PACKAGE, pkg_b)
    cleanup(schema.APPLICATION, app)

    service.upsert_package(pkg_a, "npm", f"pkg-a-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_b, "npm", f"pkg-b-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_b, pkg_b, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_application(app, f"org-{run_id}", "service-1", first_observed_at=T0, event_time=T0)

    # App resolves pkg_b@1.0.0, and pkg_b depends on pkg_a
    service.resolve_version(app, ver_b, resolved_at=T0, first_observed_at=T0, event_time=T0)
    service.write_depends_on("Package", pkg_b, pkg_a, "^1.0.0", "npm", first_observed_at=T0, event_time=T0)

    # Question 1: Exposure to pkg_a (transitive)
    exposures = query_service.transitive_exposure(pkg_a, consistency="strong")
    assert len(exposures) >= 1
    exp = next(e for e in exposures if e.application_key == app)
    assert exp.depth == 2
    assert exp.status == "confirmed"
    assert exp.resolved_version == ver_b

    # Question 1: Exposure to ver_b (direct)
    direct_exp = query_service.transitive_exposure(ver_b, consistency="strong")
    assert any(e.application_key == app and e.depth == 1 for e in direct_exp)


def test_introducing_version_exact_and_fallback(service, query_service, cleanup, run_id):
    adv_key = f"osv:GHSA-test-{run_id}"
    pkg_key = f"npm:test-lib-{run_id}"
    ver_key = f"{pkg_key}@2.1.0"

    cleanup(schema.ADVISORY, adv_key)
    cleanup(schema.PACKAGE, pkg_key)

    service.upsert_package(pkg_key, "npm", f"test-lib-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "2.1.0", first_observed_at=T0, event_time=T0)
    service.upsert_advisory(adv_key, "osv", f"GHSA-test-{run_id}", "Test advisory", first_observed_at=T0, event_time=T0)
    service.write_affects(adv_key, "Version", ver_key, advisory_published_at=T1, severity="HIGH", first_observed_at=T0, event_time=T0)
    service.write_introduced_in(adv_key, ver_key, confidence=0.92, evidence="diff in install script", first_observed_at=T0, event_time=T0)

    # Question 2: Precise introduction point
    res = query_service.introducing_version(adv_key, consistency="strong")
    assert res.precise is True
    assert res.introducing_version_key == ver_key
    assert res.confidence == 0.92
    assert "diff in install script" in res.evidence


def test_live_resolutions_interval_overlap(service, query_service, cleanup, run_id):
    app = f"app:org-{run_id}/web-app"
    pkg = f"npm:lib-{run_id}"
    ver = f"{pkg}@3.0.0"

    cleanup(schema.APPLICATION, app)
    cleanup(schema.PACKAGE, pkg)

    service.upsert_package(pkg, "npm", f"lib-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver, pkg, "3.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_application(app, f"org-{run_id}", "web-app", first_observed_at=T0, event_time=T0)

    # Resolved at T0, superseded at T2
    service.resolve_version(app, ver, resolved_at=T0, first_observed_at=T0, event_time=T0)
    service.supersede_version(app, ver, resolved_at=T0, superseded_at=T2)

    # Question 3: Overlap during [T0, T1]
    res_in_window = query_service.live_resolutions(ver, window_start=T0, window_end=T1, consistency="strong")
    assert len(res_in_window) == 1
    assert res_in_window[0].was_live_in_window is True

    # Check non-overlapping window in future T2+
    future_start = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    res_future = query_service.live_resolutions(ver, window_start=future_start, window_end=future_start, consistency="strong")
    assert len(res_future) == 0


def test_blast_radius_outward_traversal(service, query_service, cleanup, run_id):
    pkg_leaf = f"npm:leaf-{run_id}"
    pkg_mid = f"npm:mid-{run_id}"
    ver_mid = f"{pkg_mid}@1.0.0"
    app = f"app:org-{run_id}/dashboard"

    cleanup(schema.PACKAGE, pkg_leaf)
    cleanup(schema.PACKAGE, pkg_mid)
    cleanup(schema.APPLICATION, app)

    service.upsert_package(pkg_leaf, "npm", f"leaf-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_mid, "npm", f"mid-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_mid, pkg_mid, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_application(app, f"org-{run_id}", "dashboard", first_observed_at=T0, event_time=T0)

    service.write_depends_on("Package", pkg_mid, pkg_leaf, "*", "npm", first_observed_at=T0, event_time=T0)
    service.resolve_version(app, ver_mid, resolved_at=T0, first_observed_at=T0, event_time=T0)

    # Question 4: Blast radius from pkg_leaf
    blast = query_service.blast_radius(pkg_leaf, consistency="strong")
    assert blast.total_reached >= 2
    assert pkg_mid in blast.packages
    assert app in blast.applications


def test_nearby_typosquats_and_shared_maintainers(service, query_service, cleanup, run_id):
    pkg_target = f"npm:target-lib-{run_id}"
    pkg_typo = f"npm:targt-lib-{run_id}"
    ver_target = f"{pkg_target}@1.0.0"
    m_key = f"npm:maintainer-{run_id}"

    cleanup(schema.PACKAGE, pkg_target)
    cleanup(schema.PACKAGE, pkg_typo)
    cleanup(schema.MAINTAINER, m_key)

    service.upsert_package(pkg_target, "npm", f"target-lib-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_typo, "npm", f"targt-lib-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_target, pkg_target, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_maintainer(m_key, "npm", f"user-{run_id}", first_observed_at=T0, event_time=T0)
    service.write_published_by(ver_target, m_key, first_observed_at=T0, event_time=T0)
    service.write_typosquat_of(pkg_typo, pkg_target, 0.91, "levenshtein", first_observed_at=T0, event_time=T0)

    # Question 5: Typosquats
    typos = query_service.nearby_typosquats(pkg_target, consistency="strong")
    assert any(t.package_key == pkg_typo and t.similarity_score == 0.91 for t in typos)

    # Question 6: Shared maintainer check
    shared = query_service.shared_maintainers_and_infra(pkg_target, consistency="strong")
    assert isinstance(shared, list)


def test_predictive_cascade_and_chained_vuln(service, query_service, cleanup, run_id):
    pkg_base = f"npm:base-{run_id}"
    ver_base = f"{pkg_base}@2.0.0"
    pkg_consumer = f"npm:consumer-{run_id}"

    cleanup(schema.PACKAGE, pkg_base)
    cleanup(schema.PACKAGE, pkg_consumer)

    service.upsert_package(pkg_base, "npm", f"base-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_base, pkg_base, "2.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_consumer, "npm", f"consumer-{run_id}", first_observed_at=T0, event_time=T0)
    service.write_depends_on("Package", pkg_consumer, pkg_base, "^2.0.0", "npm", first_observed_at=T0, event_time=T0)

    # Predictive propagation forecasting
    prop = query_service.predict_propagation(ver_base, write_to_graph=True, consistency="strong")
    assert len(prop) >= 1
    assert prop[0].type == "predicted"
    assert prop[0].basis == "propagation"

    # Early warning prediction
    service.annotate_package(pkg_base, socket_score=0.85)
    early = query_service.predict_early_warning(pkg_base, write_to_graph=True, consistency="strong")
    assert early.type == "predicted"
    assert early.basis == "early_warning"
    assert early.risk_score >= 0.5

    # Chained vulnerability detection stub
    chain = query_service.detect_chain("npm:lodash", "npm:ejs")
    assert chain is not None
    assert chain.risk_type == "prototype_pollution_to_code_execution"
