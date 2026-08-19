from graphplatform import schema
from graphplatform.ingestion.events import PackageVersionPublished
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
