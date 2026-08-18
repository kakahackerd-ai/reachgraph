"""Live integration tests -- these hit the real OSV and GitHub Advisory
Database APIs. Bounded to a small, well-known package/ecosystem set.
"""

from graphplatform.ingestion.advisory.ghsa import GHSAConnector
from graphplatform.ingestion.advisory.osv import OSVConnector


def test_osv_backfill_lodash_has_real_known_advisory():
    conn = OSVConnector()
    try:
        events = list(conn.backfill([("npm", "lodash")]))
    finally:
        conn.close()
    assert len(events) > 0
    ids = {e.advisory_id for e in events}
    assert "GHSA-29mw-wpgm-hmr9" in ids  # real, long-published lodash ReDoS advisory
    adv = next(e for e in events if e.advisory_id == "GHSA-29mw-wpgm-hmr9")
    assert adv.source == "osv"
    assert adv.severity  # real severity string from database_specific
    assert adv.advisory_published_at
    assert any(a["package_name"] == "lodash" for a in adv.affected)


def test_osv_query_nonexistent_package_yields_nothing():
    conn = OSVConnector()
    try:
        events = list(conn.backfill([("npm", "this-package-definitely-does-not-exist-xyz-123")]))
    finally:
        conn.close()
    assert events == []


def test_osv_fetch_or_subscribe_dedupes_by_modified_across_iterations():
    conn = OSVConnector(poll_interval_s=0.1)
    try:
        first_pass = list(conn.fetch_or_subscribe(watch=[("npm", "lodash")], max_iterations=1))
        assert len(first_pass) > 0
        # a second bounded pass over the same watchlist should see no new
        # advisories -- nothing has changed since the first pass recorded
        # every advisory's `modified` timestamp.
        second_pass = list(conn.fetch_or_subscribe(watch=[("npm", "lodash")], max_iterations=1))
    finally:
        conn.close()
    assert second_pass == []


def test_ghsa_backfill_npm_advisories_are_real():
    conn = GHSAConnector()
    try:
        events = list(conn.backfill(ecosystem="npm", max_pages=1))
    finally:
        conn.close()
    assert len(events) > 0
    adv = events[0]
    assert adv.source == "ghsa"
    assert adv.advisory_id.startswith("GHSA-")
    assert adv.advisory_published_at
    assert adv.severity in ("LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")
    assert all(a["ecosystem"] == "npm" for a in adv.affected)


def test_ghsa_live_poll_is_bounded_and_advances_high_water_mark():
    conn = GHSAConnector(poll_interval_s=0.1)
    try:
        events = list(conn.fetch_or_subscribe(ecosystem="npm", max_iterations=1))
    finally:
        conn.close()
    assert len(events) > 0
    assert conn.last_updated_at  # real ISO timestamp, moved past the initial ""
