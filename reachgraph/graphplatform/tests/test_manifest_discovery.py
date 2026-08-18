import os

from graphplatform.ingestion.manifest.discovery import discover

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _by_subpath(result):
    return {sp.subpath: sp for sp in result.sub_packages}


def test_npm_workspace_monorepo_detected_with_per_member_and_root_resolution():
    result = discover(os.path.join(FIXTURES, "npm_monorepo"))
    by_subpath = _by_subpath(result)

    assert set(by_subpath) == {"packages/pkg-a", "packages/pkg-b", ""}

    # each member's resolved set is its own *declared* deps, resolved via
    # the shared workspace lockfile -- pkg-b does not declare is-number
    # directly, so it must not appear there even though it's present
    # transitively in the shared lockfile.
    assert by_subpath["packages/pkg-a"].ecosystem == "npm"
    assert by_subpath["packages/pkg-a"].resolved == {"lodash": "4.17.21"}
    assert by_subpath["packages/pkg-b"].resolved == {"is-odd": "3.0.1"}
    assert "is-number" not in by_subpath["packages/pkg-b"].resolved

    # the workspace root gets the whole resolved closure -- everything
    # actually present in the shared node_modules tree, transitive included.
    root_resolved = by_subpath[""].resolved
    assert root_resolved == {"lodash": "4.17.21", "is-odd": "3.0.1", "is-number": "6.0.0"}


def test_python_multi_package_monorepo_detected_with_independent_lockfiles():
    result = discover(os.path.join(FIXTURES, "py_monorepo"))
    by_subpath = _by_subpath(result)

    assert set(by_subpath) == {"services/svc-a", "services/svc-b"}
    assert by_subpath["services/svc-a"].ecosystem == "pypi"
    assert by_subpath["services/svc-a"].resolved == {"requests": "2.31.0", "certifi": "2024.2.2"}
    # svc-b has no poetry.lock, only a requirements.txt with one exact pin
    # and one range -- only the pin is a resolved version.
    assert by_subpath["services/svc-b"].resolved == {"click": "8.1.7"}


def test_single_npm_package_no_workspace(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "solo", "dependencies": {"lodash": "^4.0.0"}}')
    (tmp_path / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "packages": {"": {"name": "solo"}, '
        '"node_modules/lodash": {"version": "4.17.21"}}}'
    )
    result = discover(str(tmp_path))
    assert len(result.sub_packages) == 1
    sp = result.sub_packages[0]
    assert sp.subpath == ""
    assert sp.ecosystem == "npm"
    assert sp.resolved == {"lodash": "4.17.21"}


def test_go_and_rust_manifests_are_found_but_not_parsed(tmp_path):
    (tmp_path / "go.mod").write_text("module example.com/foo\n\ngo 1.21\n")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "foo"\nversion = "0.1.0"\n')
    result = discover(str(tmp_path))
    assert result.sub_packages == []
    assert set(result.stub_manifests_found) == {"go.mod", "Cargo.toml"}


def test_repo_with_nothing_found_returns_empty(tmp_path):
    result = discover(str(tmp_path))
    assert result.sub_packages == []
    assert result.stub_manifests_found == []
