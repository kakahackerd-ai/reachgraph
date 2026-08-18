from graphplatform import schema
from graphplatform.ingestion.events import AdvisoryPublished, PackageVersionPublished
from graphplatform.ingestion.writer import normalize_ecosystem


def test_normalize_ecosystem_maps_upstream_spellings_onto_schema():
    assert normalize_ecosystem("npm") == "npm"
    assert normalize_ecosystem("PyPI") == "pypi"
    assert normalize_ecosystem("pip") == "pypi"
    assert normalize_ecosystem("nuget") is None


def test_package_version_published_writes_package_version_dep_and_maintainer(writer, service, cleanup, run_id):
    pkg = f"test-pkg-{run_id}"
    dep = f"test-dep-{run_id}"
    event = PackageVersionPublished(
        ecosystem="npm",
        package_name=pkg,
        version="1.0.0",
        event_time="2024-04-01T00:00:00Z",
        dependencies={dep: "^2.0.0"},
        maintainer_identity=f"maintainer-{run_id}@example.com",
        maintainer_platform="npm",
        source="npm",
    ).to_dict()

    cleanup(schema.PACKAGE, f"npm:{pkg}")
    cleanup(schema.PACKAGE, f"npm:{dep}")
    cleanup(schema.MAINTAINER, f"npm:maintainer:maintainer-{run_id}@example.com")

    writer.handle(event)

    package = service.get_package(f"npm:{pkg}", consistency="strong")
    assert package is not None
    assert package["ecosystem"] == "npm"

    version = service.get_version(f"npm:{pkg}@1.0.0", consistency="strong")
    assert version is not None
    assert version["package_key"] == f"npm:{pkg}"

    deps = service.get_dependencies_of(schema.PACKAGE, f"npm:{pkg}", consistency="strong")
    assert {(d["package_key"], d["range"]) for d in deps} == {(f"npm:{dep}", "^2.0.0")}

    maintainers = service.get_maintainers_of(f"npm:{pkg}@1.0.0", consistency="strong")
    assert [m["maintainer_key"] for m in maintainers] == [f"npm:maintainer:maintainer-{run_id}@example.com"]

    # idempotent: handling the same event again creates no duplicates
    writer.handle(event)
    deps_again = service.get_dependencies_of(schema.PACKAGE, f"npm:{pkg}", consistency="strong")
    assert len(deps_again) == 1


def test_advisory_published_writes_package_and_exact_version_affects(writer, service, cleanup, run_id):
    pkg = f"vuln-pkg-{run_id}"
    advisory_id = f"GHSA-test-{run_id}"
    event = AdvisoryPublished(
        source="ghsa",
        advisory_id=advisory_id,
        summary="test advisory",
        severity="HIGH",
        advisory_published_at="2024-05-01T00:00:00Z",
        affected=[{"ecosystem": "npm", "package_name": pkg, "versions": ["1.0.0", "1.0.1"]}],
    ).to_dict()

    cleanup(schema.ADVISORY, f"ghsa:{advisory_id}")
    cleanup(schema.PACKAGE, f"npm:{pkg}")
    cleanup(schema.VERSION, f"npm:{pkg}@1.0.0")
    cleanup(schema.VERSION, f"npm:{pkg}@1.0.1")

    writer.handle(event)

    advisory = service.get_advisory(f"ghsa:{advisory_id}", consistency="strong")
    assert advisory is not None
    assert advisory["summary"] == "test advisory"

    package_affects = service.get_advisories_for(schema.PACKAGE, f"npm:{pkg}", consistency="strong")
    assert [(a["advisory_key"], a["severity"]) for a in package_affects] == [(f"ghsa:{advisory_id}", "HIGH")]

    version_affects = service.get_advisories_for(schema.VERSION, f"npm:{pkg}@1.0.0", consistency="strong")
    assert [(a["advisory_key"], a["severity"]) for a in version_affects] == [(f"ghsa:{advisory_id}", "HIGH")]


def test_advisory_published_skips_affected_entry_with_unknown_ecosystem(writer, service, cleanup, run_id):
    advisory_id = f"GHSA-unknown-eco-{run_id}"
    event = AdvisoryPublished(
        source="ghsa",
        advisory_id=advisory_id,
        summary="test advisory",
        severity="LOW",
        advisory_published_at="2024-05-01T00:00:00Z",
        affected=[{"ecosystem": "nuget", "package_name": f"whatever-{run_id}"}],
    ).to_dict()

    cleanup(schema.ADVISORY, f"ghsa:{advisory_id}")
    writer.handle(event)

    advisory = service.get_advisory(f"ghsa:{advisory_id}", consistency="strong")
    assert advisory is not None  # the advisory itself is still recorded
    assert service.get_package(f"nuget:whatever-{run_id}", consistency="strong") is None
