"""Live integration tests -- these hit the real npm and PyPI registries.
Bounded to a couple of well-known, long-stable packages so runtime and
flakiness stay low; no mocking, per the phase-2 brief's "verify, don't
assume" instruction.
"""

from graphplatform.ingestion.registry.npm import NpmConnector
from graphplatform.ingestion.registry.pypi import PyPIConnector


def test_npm_backfill_lodash_has_real_versions_and_deps():
    conn = NpmConnector()
    try:
        events = list(conn.backfill(["lodash"]))
    finally:
        conn.close()

    assert len(events) > 100  # lodash has published hundreds of versions
    by_version = {e.version for e in events}
    assert "4.17.21" in by_version
    v = next(e for e in events if e.version == "4.17.21")
    assert v.ecosystem == "npm"
    assert v.package_name == "lodash"
    assert v.event_time  # real ISO-8601 publish timestamp, not fabricated
    assert v.dependencies == {}  # lodash 4.17.21 genuinely has zero runtime deps


def test_npm_backfill_express_has_real_dependencies():
    conn = NpmConnector()
    try:
        events = list(conn.backfill(["express"]))
    finally:
        conn.close()
    v = next(e for e in events if e.version == "4.19.2")
    assert v.dependencies.get("qs") == "6.11.0"
    assert v.maintainer_identity  # real npm publisher identity


def test_npm_backfill_nonexistent_package_yields_nothing():
    conn = NpmConnector()
    try:
        events = list(conn.backfill(["this-package-definitely-does-not-exist-xyz-123"]))
    finally:
        conn.close()
    assert events == []


def test_npm_live_changes_feed_is_real_and_bounded():
    conn = NpmConnector(poll_interval_s=0.1)
    try:
        start_seq = conn.current_seq()
        assert start_seq > 0  # the real changes feed has moved well past seq 0
        # start almost caught up, so the single bounded iteration only has
        # to process a handful of very recent changes, not replay history
        list(conn.fetch_or_subscribe(since=start_seq - 5, max_iterations=1))
    finally:
        conn.close()
    assert conn.last_seq >= start_seq - 5


def test_pypi_backfill_requests_has_real_versions_and_deps():
    conn = PyPIConnector()
    try:
        events = list(conn.backfill(["requests"]))
    finally:
        conn.close()
    assert len(events) > 50
    v = next(e for e in events if e.version == "2.31.0")
    assert v.ecosystem == "pypi"
    assert v.event_time
    assert "idna" in v.dependencies


def test_pypi_current_serial_and_bounded_poll():
    conn = PyPIConnector(poll_interval_s=0.1)
    try:
        serial = conn.current_serial()
        assert serial > 0
        list(conn.fetch_or_subscribe(since_serial=serial, max_iterations=1))
    finally:
        conn.close()
    assert conn.last_serial >= serial
