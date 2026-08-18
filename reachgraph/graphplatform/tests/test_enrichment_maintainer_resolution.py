from graphplatform import schema
from graphplatform.enrichment.maintainer_resolution import MaintainerResolutionService
from graphplatform.ingestion.events import PackageVersionPublished
from graphplatform.ingestion.writer import GraphIngestionWriter


def test_same_maintainer_as_deterministic_email_match_across_platforms(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    mr = MaintainerResolutionService(service)
    email = f"same-person-{run_id}@example.com"

    npm_event = PackageVersionPublished(
        ecosystem="npm",
        package_name=f"npm-pkg-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=email,
        maintainer_platform="npm",
    ).to_dict()
    pypi_event = PackageVersionPublished(
        ecosystem="pypi",
        package_name=f"pypi-pkg-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=email,
        maintainer_platform="pypi",
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:npm-pkg-{run_id}")
    cleanup(schema.PACKAGE, f"pypi:pypi-pkg-{run_id}")
    cleanup(schema.VERSION, f"npm:npm-pkg-{run_id}@1.0.0")
    cleanup(schema.VERSION, f"pypi:pypi-pkg-{run_id}@1.0.0")
    cleanup(schema.MAINTAINER, f"npm:maintainer:{email}")
    cleanup(schema.MAINTAINER, f"pypi:maintainer:{email}")

    for event in (npm_event, pypi_event):
        writer.handle(event)
        mr.process_publish(event)

    a_key, b_key = sorted((f"npm:maintainer:{email}", f"pypi:maintainer:{email}"))
    rows = service._run(
        "MATCH (a:Maintainer {key:$a})-[r:SAME_MAINTAINER_AS]->(b:Maintainer {key:$b}) "
        "RETURN r.confidence AS confidence, r.evidence_type AS evidence_type",
        a=a_key,
        b=b_key,
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["evidence_type"] == "verified_email"
    assert rows[0]["confidence"] >= 0.9


def test_same_maintainer_as_skips_non_email_identity(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    mr = MaintainerResolutionService(service)
    handle = f"plain-handle-{run_id}"  # not email-shaped

    event_a = PackageVersionPublished(
        ecosystem="npm",
        package_name=f"handle-pkg-a-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=handle,
        maintainer_platform="npm",
    ).to_dict()
    event_b = PackageVersionPublished(
        ecosystem="npm",
        package_name=f"handle-pkg-b-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=handle,
        maintainer_platform="npm",
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:handle-pkg-a-{run_id}")
    cleanup(schema.PACKAGE, f"npm:handle-pkg-b-{run_id}")
    cleanup(schema.VERSION, f"npm:handle-pkg-a-{run_id}@1.0.0")
    cleanup(schema.VERSION, f"npm:handle-pkg-b-{run_id}@1.0.0")
    cleanup(schema.MAINTAINER, f"npm:maintainer:{handle}")

    for event in (event_a, event_b):
        writer.handle(event)
        mr.process_publish(event)

    rows = service._run(
        "MATCH (m:Maintainer {key:$key})-[r:SAME_MAINTAINER_AS]->() RETURN count(*) AS c",
        key=f"npm:maintainer:{handle}",
        consistency="strong",
    )
    assert rows[0]["c"] == 0  # same identity, but not email-shaped -- not a deterministic match


def test_shares_infrastructure_with_same_signing_keyid(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    mr = MaintainerResolutionService(service)
    keyid = f"SHA256:test-key-{run_id}"

    event_a = PackageVersionPublished(
        ecosystem="npm",
        package_name=f"infra-pkg-a-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=f"a-{run_id}@example.com",
        signing_keyid=keyid,
    ).to_dict()
    event_b = PackageVersionPublished(
        ecosystem="npm",
        package_name=f"infra-pkg-b-{run_id}",
        version="1.0.0",
        event_time="2024-01-01T00:00:00Z",
        maintainer_identity=f"b-{run_id}@example.com",
        signing_keyid=keyid,
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:infra-pkg-a-{run_id}")
    cleanup(schema.PACKAGE, f"npm:infra-pkg-b-{run_id}")
    cleanup(schema.VERSION, f"npm:infra-pkg-a-{run_id}@1.0.0")
    cleanup(schema.VERSION, f"npm:infra-pkg-b-{run_id}@1.0.0")
    cleanup(schema.MAINTAINER, f"npm:maintainer:a-{run_id}@example.com")
    cleanup(schema.MAINTAINER, f"npm:maintainer:b-{run_id}@example.com")

    for event in (event_a, event_b):
        writer.handle(event)
        mr.process_publish(event)

    a_key, b_key = sorted((f"npm:infra-pkg-a-{run_id}", f"npm:infra-pkg-b-{run_id}"))
    rows = service._run(
        "MATCH (a:Package {key:$a})-[r:SHARES_INFRASTRUCTURE_WITH]->(b:Package {key:$b}) RETURN r.evidence_type AS evidence_type",
        a=a_key,
        b=b_key,
        consistency="strong",
    )
    assert len(rows) == 1
    assert rows[0]["evidence_type"] == "signing_key"


def test_find_fuzzy_candidates_returns_without_writing(service, cleanup, run_id):
    writer = GraphIngestionWriter(service)
    mr = MaintainerResolutionService(service)

    base = f"fuzzy-handle-{run_id}"
    similar = f"fuzzy-handel-{run_id}"  # one transposed letter -- similar, not identical

    for handle in (base, similar):
        event = PackageVersionPublished(
            ecosystem="npm",
            package_name=f"pkg-for-{handle}",
            version="1.0.0",
            event_time="2024-01-01T00:00:00Z",
            maintainer_identity=handle,
        ).to_dict()
        cleanup(schema.PACKAGE, f"npm:pkg-for-{handle}")
        cleanup(schema.VERSION, f"npm:pkg-for-{handle}@1.0.0")
        cleanup(schema.MAINTAINER, f"npm:maintainer:{handle}")
        writer.handle(event)

    candidates = mr.find_fuzzy_candidates(threshold=0.8)
    pair_keys = {frozenset((c["a"], c["b"])) for c in candidates}
    assert frozenset((f"npm:maintainer:{base}", f"npm:maintainer:{similar}")) in pair_keys

    # never auto-written
    rows = service._run(
        "MATCH (m:Maintainer {key:$key})-[r:SAME_MAINTAINER_AS]->() RETURN count(*) AS c",
        key=f"npm:maintainer:{base}",
        consistency="strong",
    )
    assert rows[0]["c"] == 0
