from graphplatform import schema
from graphplatform.enrichment.version_introduction import VersionIntroductionService
from graphplatform.ingestion.events import AdvisoryPublished, PackageVersionPublished
from graphplatform.ingestion.writer import GraphIngestionWriter


def test_clear_dependency_signal_yields_high_confidence(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    vi = VersionIntroductionService(service)
    pkg = f"vi-pkg-{run_id}"

    v1 = PackageVersionPublished(
        ecosystem="npm", package_name=pkg, version="1.0.0", event_time="2024-01-01T00:00:00Z", dependencies={}
    ).to_dict()
    v2 = PackageVersionPublished(
        ecosystem="npm",
        package_name=pkg,
        version="2.0.0",
        event_time="2024-02-01T00:00:00Z",
        dependencies={"evil-dep": "^1.0.0"},
        has_install_script=True,
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:{pkg}")
    cleanup(schema.PACKAGE, "npm:evil-dep")
    cleanup(schema.VERSION, f"npm:{pkg}@1.0.0")
    cleanup(schema.VERSION, f"npm:{pkg}@2.0.0")

    for event in (v1, v2):
        writer.handle(event)
        vi.record_publish(event)

    advisory_id = f"GHSA-test-{run_id}"
    advisory_event = AdvisoryPublished(
        source="osv",
        advisory_id=advisory_id,
        summary="test",
        severity="HIGH",
        advisory_published_at="2024-03-01T00:00:00Z",
        affected=[{"ecosystem": "npm", "package_name": pkg, "introduced": "2.0.0"}],
    ).to_dict()
    cleanup(schema.ADVISORY, f"osv:{advisory_id}")
    writer.handle(advisory_event)
    vi.detect_introduction(advisory_event)

    rows = service._run(
        "MATCH (a:Advisory {key:$key})-[r:INTRODUCED_IN]->(v:Version) "
        "RETURN v.key AS version_key, r.confidence AS confidence, r.evidence AS evidence",
        key=f"osv:{advisory_id}",
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["version_key"] == f"npm:{pkg}@2.0.0"
    assert rows[0]["confidence"] >= 0.9  # two signals: dep added + install script added
    assert "evil-dep" in rows[0]["evidence"]
    assert "install script" in rows[0]["evidence"]


def test_no_signal_falls_back_to_low_confidence_not_fabricated_precision(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    vi = VersionIntroductionService(service)
    pkg = f"vi-quiet-pkg-{run_id}"

    v1 = PackageVersionPublished(
        ecosystem="npm", package_name=pkg, version="1.0.0", event_time="2024-01-01T00:00:00Z", dependencies={"a": "1.0.0"}
    ).to_dict()
    v2 = PackageVersionPublished(
        ecosystem="npm", package_name=pkg, version="1.0.1", event_time="2024-01-02T00:00:00Z", dependencies={"a": "1.0.0"}
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:{pkg}")
    cleanup(schema.PACKAGE, "npm:a")
    cleanup(schema.VERSION, f"npm:{pkg}@1.0.0")
    cleanup(schema.VERSION, f"npm:{pkg}@1.0.1")

    for event in (v1, v2):
        writer.handle(event)
        vi.record_publish(event)

    advisory_id = f"GHSA-quiet-{run_id}"
    advisory_event = AdvisoryPublished(
        source="osv",
        advisory_id=advisory_id,
        summary="test",
        severity="LOW",
        advisory_published_at="2024-03-01T00:00:00Z",
        affected=[{"ecosystem": "npm", "package_name": pkg, "introduced": "1.0.1"}],
    ).to_dict()
    cleanup(schema.ADVISORY, f"osv:{advisory_id}")
    writer.handle(advisory_event)
    vi.detect_introduction(advisory_event)

    rows = service._run(
        "MATCH (a:Advisory {key:$key})-[r:INTRODUCED_IN]->(v:Version) RETURN r.confidence AS confidence, r.evidence AS evidence",
        key=f"osv:{advisory_id}",
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["confidence"] < 0.5
    assert rows[0]["evidence"] == "no clear diff signal, defaulted to advisory range start"


def test_no_stated_introduction_point_is_skipped_not_guessed(service, cleanup, run_id):
    vi = VersionIntroductionService(service)
    advisory_id = f"GHSA-noboundary-{run_id}"
    advisory_event = AdvisoryPublished(
        source="ghsa",
        advisory_id=advisory_id,
        summary="test",
        severity="LOW",
        advisory_published_at="2024-03-01T00:00:00Z",
        affected=[{"ecosystem": "npm", "package_name": f"vi-noboundary-{run_id}", "range": "<= 5.0.0"}],
    ).to_dict()
    vi.detect_introduction(advisory_event)  # must not raise

    rows = service._run(
        "MATCH (a:Advisory {key:$key})-[r:INTRODUCED_IN]->() RETURN count(*) AS c",
        key=f"ghsa:{advisory_id}",
        consistency="strong",
    )
    assert rows[0]["c"] == 0
