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


