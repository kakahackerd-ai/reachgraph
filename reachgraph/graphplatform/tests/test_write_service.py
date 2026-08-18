"""Integration tests against a real, running local HydraDB instance.

No mocks: every test in this file is a real Bolt round trip through
GraphWriteService to neo4j://127.0.0.1:7687 (or $HYDRADB_URI). For every
node and relationship type: write it, read it back, assert the values
match, write it again with the same key and assert no duplicate was
created, and for relationships assert the read path uses the rel_id
mirror -- never r.id.
"""

from __future__ import annotations

import datetime as dt

import neo4j
import pytest

from graphplatform import schema

T0 = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
T1 = dt.datetime(2021, 6, 15, 12, 30, tzinfo=dt.timezone.utc)
T2 = dt.datetime(2022, 3, 3, tzinfo=dt.timezone.utc)


def _count_nodes(service, label: str, key: str) -> int:
    rows = service._run(f"MATCH (n:{label} {{key:$key}}) RETURN count(*) AS c", key=key, consistency="strong")
    return rows[0]["c"]


def _count_edges(service, rel_type: str, a_label: str, a_key: str, b_label: str, b_key: str) -> int:
    rows = service._run(
        f"MATCH (a:{a_label} {{key:$a}})-[r:{rel_type}]->(b:{b_label} {{key:$b}}) RETURN count(*) AS c",
        a=a_key,
        b=b_key,
        consistency="strong",
    )
    return rows[0]["c"]


# ---------------------------------------------------------------------------
# Node round trips
# ---------------------------------------------------------------------------


def test_upsert_package_roundtrip_and_idempotent(service, cleanup, run_id):
    key = f"npm:test-pkg-{run_id}"
    cleanup(schema.PACKAGE, key)

    created1 = service.upsert_package(key, "npm", "test-pkg", first_observed_at=T0, event_time=T0)
    assert created1 is True

    got = service.get_package(key, consistency="strong")
    assert got == {
        "key": key,
        "ecosystem": "npm",
        "name": "test-pkg",
        "first_observed_at": schema.to_iso(T0),
        "event_time": schema.to_iso(T0),
    }

    # idempotent re-upsert with a different event_time: no duplicate, and
    # first_observed_at must NOT move even though we pass a later T1 here.
    created2 = service.upsert_package(key, "npm", "test-pkg-renamed", first_observed_at=T1, event_time=T1)
    assert created2 is False
    assert _count_nodes(service, schema.PACKAGE, key) == 1

    got2 = service.get_package(key, consistency="strong")
    assert got2["name"] == "test-pkg-renamed"  # regular property updates
    assert got2["event_time"] == schema.to_iso(T1)
    assert got2["first_observed_at"] == schema.to_iso(T0)  # set once, never overwritten


def test_annotate_package_sets_properties_without_touching_core_fields(service, cleanup, run_id):
    key = f"npm:test-pkg-{run_id}"
    cleanup(schema.PACKAGE, key)
    service.upsert_package(key, "npm", "test-pkg", first_observed_at=T0, event_time=T0)

    service.annotate_package(key, socket_score=0.42, socket_scored_at="2024-01-01T00:00:00Z")
    got = service.get_package(key, consistency="strong")
    assert got["ecosystem"] == "npm"  # untouched
    assert got["first_observed_at"] == schema.to_iso(T0)  # untouched

    row = service._run(
        "MATCH (n:Package {key:$key}) RETURN n.socket_score AS score, n.socket_scored_at AS scored_at",
        key=key,
        consistency="strong",
    )[0]
    assert row == {"score": 0.42, "scored_at": "2024-01-01T00:00:00Z"}


def test_annotate_package_on_missing_key_is_a_silent_noop(service, run_id):
    key = f"npm:does-not-exist-{run_id}"
    service.annotate_package(key, socket_score=0.9)  # must not raise
    assert service.get_package(key, consistency="strong") is None


def test_upsert_version_roundtrip_and_idempotent(service, cleanup, run_id):
    pkg_key = f"npm:test-pkg-{run_id}"
    ver_key = f"npm:test-pkg-{run_id}@1.0.0"
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, ver_key)

    service.upsert_package(pkg_key, "npm", "test-pkg", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T1, event_time=T1)

    assert _count_nodes(service, schema.VERSION, ver_key) == 1
    got = service.get_version(ver_key, consistency="strong")
    assert got["package_key"] == pkg_key
    assert got["version"] == "1.0.0"
    assert got["first_observed_at"] == schema.to_iso(T0)
    assert got["event_time"] == schema.to_iso(T1)


def test_upsert_maintainer_roundtrip_and_idempotent(service, cleanup, run_id):
    key = f"npm:maintainer:test-{run_id}"
    cleanup(schema.MAINTAINER, key)

    service.upsert_maintainer(key, "npm", "test@example.com", first_observed_at=T0, event_time=T0)
    service.upsert_maintainer(key, "npm", "test@example.com", first_observed_at=T1, event_time=T1)

    assert _count_nodes(service, schema.MAINTAINER, key) == 1
    got = service.get_maintainer(key, consistency="strong")
    assert got["platform"] == "npm"
    assert got["identity"] == "test@example.com"
    assert got["first_observed_at"] == schema.to_iso(T0)


def test_upsert_infrastructure_roundtrip_and_idempotent(service, cleanup, run_id):
    key = f"infra:test-{run_id}"
    cleanup(schema.INFRASTRUCTURE, key)

    service.upsert_infrastructure(key, "ci_system", "github-actions", first_observed_at=T0, event_time=T0)
    service.upsert_infrastructure(key, "ci_system", "github-actions", first_observed_at=T1, event_time=T1)

    assert _count_nodes(service, schema.INFRASTRUCTURE, key) == 1
    got = service.get_infrastructure(key, consistency="strong")
    assert got["kind"] == "ci_system"
    assert got["identifier"] == "github-actions"


def test_upsert_application_roundtrip_and_idempotent(service, cleanup, run_id):
    key = f"test-org/test-repo-{run_id}"
    cleanup(schema.APPLICATION, key)

    service.upsert_application(key, "test-org", f"test-repo-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_application(key, "test-org", f"test-repo-{run_id}", first_observed_at=T1, event_time=T1)

    assert _count_nodes(service, schema.APPLICATION, key) == 1
    got = service.get_application(key, consistency="strong")
    assert got["org"] == "test-org"
    assert got["subpath"] == ""


def test_upsert_advisory_roundtrip_and_idempotent(service, cleanup, run_id):
    key = f"osv:TEST-{run_id}"
    cleanup(schema.ADVISORY, key)

    service.upsert_advisory(key, "osv", f"TEST-{run_id}", "a test advisory", first_observed_at=T0, event_time=T0)
    service.upsert_advisory(key, "osv", f"TEST-{run_id}", "updated summary", first_observed_at=T1, event_time=T1)

    assert _count_nodes(service, schema.ADVISORY, key) == 1
    got = service.get_advisory(key, consistency="strong")
    assert got["summary"] == "updated summary"
    assert got["first_observed_at"] == schema.to_iso(T0)


# ---------------------------------------------------------------------------
# Relationship round trips
# ---------------------------------------------------------------------------


def test_depends_on_roundtrip_idempotent_and_uses_rel_id_mirror(service, cleanup, run_id):
    a_key = f"npm:test-a-{run_id}"
    b_key = f"npm:test-b-{run_id}"
    cleanup(schema.PACKAGE, a_key)
    cleanup(schema.PACKAGE, b_key)
    service.upsert_package(a_key, "npm", "a", first_observed_at=T0, event_time=T0)
    service.upsert_package(b_key, "npm", "b", first_observed_at=T0, event_time=T0)

    created1 = service.write_depends_on(
        schema.PACKAGE, a_key, b_key, "^1.0.0", "npm", first_observed_at=T0, event_time=T0
    )
    assert created1 is True
    created2 = service.write_depends_on(
        schema.PACKAGE, a_key, b_key, "^2.0.0", "npm", first_observed_at=T1, event_time=T1
    )
    assert created2 is False  # idempotent: same logical edge, not a duplicate

    assert _count_edges(service, schema.DEPENDS_ON, schema.PACKAGE, a_key, schema.PACKAGE, b_key) == 1

    deps = service.get_dependencies_of(schema.PACKAGE, a_key, consistency="strong")
    assert deps == [{"package_key": b_key, "range": "^2.0.0", "manager": "npm"}]

    dependents = service.get_dependents_of(b_key, consistency="strong")
    assert dependents == [{"source_key": a_key, "range": "^2.0.0", "manager": "npm", "source_label": schema.PACKAGE}]

    # explicit rel_id mirror check, straight off the edge
    rows = service._run(
        f"MATCH (a:{schema.PACKAGE} {{key:$a}})-[r:{schema.DEPENDS_ON}]->(b:{schema.PACKAGE} {{key:$b}}) "
        f"RETURN r.rel_id AS rel_id, r.first_observed_at AS foa",
        a=a_key,
        b=b_key,
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["rel_id"] == schema.stable_id(schema.DEPENDS_ON, a_key, b_key)
    assert rows[0]["foa"] == schema.to_iso(T0)  # set once


def test_relationship_reading_r_dot_id_is_broken_confirms_why_rel_id_mirror_exists(service, cleanup, run_id):
    """Documents the real engine bug this service works around: RETURN r.id
    on a relationship variable fails even though the variable is bound.
    This is a living regression check -- if HydraDB ever fixes this, the
    test starts failing and is the signal to reconsider the mirror-property
    workaround, not delete it blindly.
    """
    a_key = f"npm:test-rid-a-{run_id}"
    b_key = f"npm:test-rid-b-{run_id}"
    cleanup(schema.PACKAGE, a_key)
    cleanup(schema.PACKAGE, b_key)
    service.upsert_package(a_key, "npm", "a", first_observed_at=T0, event_time=T0)
    service.upsert_package(b_key, "npm", "b", first_observed_at=T0, event_time=T0)
    service.write_depends_on(schema.PACKAGE, a_key, b_key, "^1", "npm", first_observed_at=T0, event_time=T0)

    with pytest.raises(neo4j.exceptions.Neo4jError):
        service._run(
            f"MATCH (a:{schema.PACKAGE} {{key:$a}})-[r:{schema.DEPENDS_ON}]->(b:{schema.PACKAGE} {{key:$b}}) "
            f"RETURN r.id AS rid",
            a=a_key,
            b=b_key,
        )


def test_resolved_version_at_interval_history_and_supersede(service, cleanup, run_id):
    app_key = f"test-org/interval-repo-{run_id}"
    pkg_key = f"npm:test-ivl-{run_id}"
    v1_key = f"{pkg_key}@1.0.0"
    v2_key = f"{pkg_key}@2.0.0"
    cleanup(schema.APPLICATION, app_key)
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, v1_key)
    cleanup(schema.VERSION, v2_key)

    service.upsert_application(app_key, "test-org", f"interval-repo-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_key, "npm", "test-ivl", first_observed_at=T0, event_time=T0)
    service.upsert_version(v1_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_version(v2_key, pkg_key, "2.0.0", first_observed_at=T1, event_time=T1)

    service.resolve_version(app_key, v1_key, T0, first_observed_at=T0, event_time=T0)
    current = service.get_current_resolutions(app_key, consistency="strong")
    assert current == [{"version_key": v1_key, "resolved_at": schema.to_iso(T0)}]

    # supersede v1 with v2: close the v1 interval, open a fresh v2 interval.
    service.supersede_version(app_key, v1_key, resolved_at=T0, superseded_at=T1)
    service.resolve_version(app_key, v2_key, T1, first_observed_at=T1, event_time=T1)

    current2 = service.get_current_resolutions(app_key, consistency="strong")
    assert current2 == [{"version_key": v2_key, "resolved_at": schema.to_iso(T1)}]

    # history is preserved: v1's interval still exists, now closed.
    rows = service._run(
        f"MATCH (a:{schema.APPLICATION} {{key:$a}})-[r:{schema.RESOLVED_VERSION_AT}]->(b:{schema.VERSION} {{key:$b}}) "
        f"RETURN r.resolved_at AS resolved_at, r.superseded_at AS superseded_at",
        a=app_key,
        b=v1_key,
        consistency="strong",
    )
    assert rows == [{"resolved_at": schema.to_iso(T0), "superseded_at": schema.to_iso(T1)}]

    # idempotency: re-resolving v1 at the same resolved_at doesn't duplicate.
    service.resolve_version(app_key, v1_key, T0, first_observed_at=T0, event_time=T0)
    assert _count_edges(service, schema.RESOLVED_VERSION_AT, schema.APPLICATION, app_key, schema.VERSION, v1_key) == 1


def test_published_by_roundtrip_idempotent(service, cleanup, run_id):
    ver_key = f"npm:test-pub-{run_id}@1.0.0"
    pkg_key = f"npm:test-pub-{run_id}"
    m_key = f"npm:maintainer:pub-{run_id}"
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, ver_key)
    cleanup(schema.MAINTAINER, m_key)

    service.upsert_package(pkg_key, "npm", "test-pub", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)
    service.upsert_maintainer(m_key, "npm", "pub@example.com", first_observed_at=T0, event_time=T0)

    service.write_published_by(ver_key, m_key, first_observed_at=T0, event_time=T0)
    service.write_published_by(ver_key, m_key, first_observed_at=T1, event_time=T1)

    assert _count_edges(service, schema.PUBLISHED_BY, schema.VERSION, ver_key, schema.MAINTAINER, m_key) == 1
    got = service.get_maintainers_of(ver_key, consistency="strong")
    assert got == [{"maintainer_key": m_key, "event_time": schema.to_iso(T1)}]


def test_affects_roundtrip_idempotent(service, cleanup, run_id):
    adv_key = f"osv:AFF-{run_id}"
    pkg_key = f"npm:test-aff-{run_id}"
    ver_key = f"{pkg_key}@1.0.0"
    cleanup(schema.ADVISORY, adv_key)
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, ver_key)

    service.upsert_advisory(adv_key, "osv", f"AFF-{run_id}", "test", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_key, "npm", "test-aff", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)

    service.write_affects(adv_key, schema.VERSION, ver_key, T0, "high", first_observed_at=T0, event_time=T0)
    service.write_affects(adv_key, schema.VERSION, ver_key, T0, "critical", first_observed_at=T1, event_time=T1)

    assert _count_edges(service, schema.AFFECTS, schema.ADVISORY, adv_key, schema.VERSION, ver_key) == 1
    got = service.get_advisories_for(schema.VERSION, ver_key, consistency="strong")
    assert got == [{"advisory_key": adv_key, "severity": "critical", "advisory_published_at": schema.to_iso(T0)}]


def test_introduced_in_roundtrip_idempotent(service, cleanup, run_id):
    adv_key = f"osv:INTRO-{run_id}"
    pkg_key = f"npm:test-intro-{run_id}"
    ver_key = f"{pkg_key}@1.0.0"
    cleanup(schema.ADVISORY, adv_key)
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, ver_key)

    service.upsert_advisory(adv_key, "osv", f"INTRO-{run_id}", "test", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_key, "npm", "test-intro", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)

    service.write_introduced_in(adv_key, ver_key, 0.7, "heuristic", first_observed_at=T0, event_time=T0)
    service.write_introduced_in(adv_key, ver_key, 0.95, "manual", first_observed_at=T1, event_time=T1)

    assert _count_edges(service, schema.INTRODUCED_IN, schema.ADVISORY, adv_key, schema.VERSION, ver_key) == 1
    rows = service._run(
        f"MATCH (adv:{schema.ADVISORY} {{key:$a}})-[r:{schema.INTRODUCED_IN}]->(v:{schema.VERSION} {{key:$b}}) "
        f"RETURN r.confidence AS confidence, r.evidence AS evidence",
        a=adv_key,
        b=ver_key,
        consistency="strong",
    )
    assert rows == [{"confidence": 0.95, "evidence": "manual"}]


def test_same_maintainer_as_roundtrip_idempotent_and_validates_evidence_type(service, cleanup, run_id):
    a_key = f"npm:maintainer:sm-a-{run_id}"
    b_key = f"npm:maintainer:sm-b-{run_id}"
    cleanup(schema.MAINTAINER, a_key)
    cleanup(schema.MAINTAINER, b_key)
    service.upsert_maintainer(a_key, "npm", "a@example.com", first_observed_at=T0, event_time=T0)
    service.upsert_maintainer(b_key, "npm", "b@example.com", first_observed_at=T0, event_time=T0)

    with pytest.raises(ValueError):
        service.write_same_maintainer_as(a_key, b_key, 0.9, "not-a-real-type", first_observed_at=T0, event_time=T0)

    service.write_same_maintainer_as(
        a_key, b_key, 0.9, "verified_email", first_observed_at=T0, event_time=T0
    )
    service.write_same_maintainer_as(
        a_key, b_key, 0.99, "signing_key", first_observed_at=T1, event_time=T1
    )
    assert _count_edges(service, schema.SAME_MAINTAINER_AS, schema.MAINTAINER, a_key, schema.MAINTAINER, b_key) == 1
    rows = service._run(
        f"MATCH (a:{schema.MAINTAINER} {{key:$a}})-[r:{schema.SAME_MAINTAINER_AS}]->(b:{schema.MAINTAINER} {{key:$b}}) "
        f"RETURN r.confidence AS confidence, r.evidence_type AS evidence_type",
        a=a_key,
        b=b_key,
        consistency="strong",
    )
    assert rows == [{"confidence": 0.99, "evidence_type": "signing_key"}]


def test_shares_infrastructure_with_roundtrip_idempotent(service, cleanup, run_id):
    a_key = f"npm:test-si-a-{run_id}"
    b_key = f"npm:test-si-b-{run_id}"
    cleanup(schema.PACKAGE, a_key)
    cleanup(schema.PACKAGE, b_key)
    service.upsert_package(a_key, "npm", "a", first_observed_at=T0, event_time=T0)
    service.upsert_package(b_key, "npm", "b", first_observed_at=T0, event_time=T0)

    service.write_shares_infrastructure_with(
        schema.PACKAGE, a_key, schema.PACKAGE, b_key, "ci_system", first_observed_at=T0, event_time=T0
    )
    service.write_shares_infrastructure_with(
        schema.PACKAGE, a_key, schema.PACKAGE, b_key, "ip_range", first_observed_at=T1, event_time=T1
    )
    assert _count_edges(service, schema.SHARES_INFRASTRUCTURE_WITH, schema.PACKAGE, a_key, schema.PACKAGE, b_key) == 1


def test_possible_typosquat_of_roundtrip_idempotent(service, cleanup, run_id):
    a_key = f"npm:test-typo-{run_id}"
    b_key = f"npm:test-real-{run_id}"
    cleanup(schema.PACKAGE, a_key)
    cleanup(schema.PACKAGE, b_key)
    service.upsert_package(a_key, "npm", "typo", first_observed_at=T0, event_time=T0)
    service.upsert_package(b_key, "npm", "real", first_observed_at=T0, event_time=T0)

    service.write_typosquat_of(a_key, b_key, 0.92, "damerau-levenshtein", first_observed_at=T0, event_time=T0)
    service.write_typosquat_of(a_key, b_key, 0.95, "damerau-levenshtein", first_observed_at=T1, event_time=T1)
    assert _count_edges(service, schema.POSSIBLE_TYPOSQUAT_OF, schema.PACKAGE, a_key, schema.PACKAGE, b_key) == 1


def test_predicted_exposure_is_structurally_distinct_from_affects(service, cleanup, run_id):
    app_key = f"test-org/pred-repo-{run_id}"
    pkg_key = f"npm:test-pred-{run_id}"
    ver_key = f"{pkg_key}@1.0.0"
    cleanup(schema.APPLICATION, app_key)
    cleanup(schema.PACKAGE, pkg_key)
    cleanup(schema.VERSION, ver_key)

    service.upsert_application(app_key, "test-org", f"pred-repo-{run_id}", first_observed_at=T0, event_time=T0)
    service.upsert_package(pkg_key, "npm", "test-pred", first_observed_at=T0, event_time=T0)
    service.upsert_version(ver_key, pkg_key, "1.0.0", first_observed_at=T0, event_time=T0)

    with pytest.raises(ValueError):
        service.write_predicted_exposure(
            schema.APPLICATION, app_key, schema.VERSION, ver_key, T0, 0.5, "not-a-real-basis",
            first_observed_at=T0, event_time=T0,
        )

    service.write_predicted_exposure(
        schema.APPLICATION, app_key, schema.VERSION, ver_key, T0, 0.6, "propagation",
        first_observed_at=T0, event_time=T0,
    )

    # never confirmed: no AFFECTS edge was created by this call.
    assert service.get_advisories_for(schema.VERSION, ver_key, consistency="strong") == []
    assert _count_edges(service, schema.PREDICTED_EXPOSURE, schema.APPLICATION, app_key, schema.VERSION, ver_key) == 1
    assert _count_edges(service, schema.AFFECTS, schema.APPLICATION, app_key, schema.VERSION, ver_key) == 0


# ---------------------------------------------------------------------------
# Consistency parameter plumbing
# ---------------------------------------------------------------------------


def test_consistency_parameter_accepts_causal_and_strong(service, cleanup, run_id):
    key = f"npm:test-consistency-{run_id}"
    cleanup(schema.PACKAGE, key)
    service.upsert_package(key, "npm", "c", first_observed_at=T0, event_time=T0)

    assert service.get_package(key, consistency="causal") is not None
    assert service.get_package(key, consistency="strong") is not None


def test_consistency_parameter_rejects_invalid_value(service):
    with pytest.raises(ValueError):
        service.get_package("does-not-matter", consistency="eventually")  # type: ignore[arg-type]
