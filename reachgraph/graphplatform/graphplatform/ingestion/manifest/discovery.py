"""Manifest & lockfile discovery: walks a repository, detects
monorepo/workspace structure, parses resolved (not just declared)
dependency versions, and returns one SubPackage per Application this repo
should become in the graph.

npm/yarn/pnpm workspaces and lerna share a single lockfile at the workspace
root -- there is no per-member lockfile to parse. So for a detected
workspace: the root lockfile is parsed once for its full resolved closure
(direct + transitive, exactly what's really sitting in the shared
node_modules tree), the root Application gets that whole closure, and each
member Application's `resolved` is that same closure filtered down to the
names it *declares* in its own package.json (dependencies/devDependencies/
peerDependencies/optionalDependencies) -- i.e. "what this member actually
gets when it resolves its own direct deps", not a full per-member
transitive closure (real per-member transitive resolution would require
walking npm's actual nested-override algorithm, out of scope here).

Python has no equivalent shared-workspace-lockfile convention -- Poetry,
pip, etc. resolve one pyproject.toml/requirements.txt at a time -- so each
detected Python sub-package parses its own local lockfile independently.

Go/Rust manifests (go.mod, Cargo.toml) are detected and logged but not
parsed for resolved versions -- explicitly out of scope for this phase per
the brief; the traversal path here is the same one a later phase would
extend to add real go.sum/Cargo.lock parsing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .npm_lock import parse_npm_lockfile
from .python_lock import parse_poetry_lock, parse_requirements_txt

log = logging.getLogger("graphplatform.ingestion.manifest.discovery")

NPM_LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
STUB_ECOSYSTEM_MANIFESTS = {"go.mod": "go", "Cargo.toml": "rust"}
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
_NPM_DEP_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


@dataclass
class SubPackage:
    subpath: str  # "" for repo root
    ecosystem: str  # "npm" | "pypi"
    resolved: dict[str, str] = field(default_factory=dict)  # dependency name -> resolved version
    manifest_files: list[str] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    sub_packages: list[SubPackage] = field(default_factory=list)
    stub_manifests_found: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("failed to parse json manifest", extra={"path": str(path), "error": str(e)})
        return None


def _read_yaml(path: Path) -> dict | None:
    try:
        return yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        log.warning("failed to parse yaml manifest", extra={"path": str(path), "error": str(e)})
        return None


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _find_npm_lockfile(dir_: Path) -> tuple[str, Path] | None:
    for name in NPM_LOCKFILES:
        p = dir_ / name
        if p.exists():
            return name, p
    return None


def _npm_workspace_globs(root: Path) -> list[str] | None:
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        pkg = _read_json(pkg_json_path) or {}
        ws = pkg.get("workspaces")
        if isinstance(ws, list):
            return ws
        if isinstance(ws, dict) and isinstance(ws.get("packages"), list):
            return ws["packages"]
    pnpm_ws_path = root / "pnpm-workspace.yaml"
    if pnpm_ws_path.exists():
        doc = _read_yaml(pnpm_ws_path) or {}
        if isinstance(doc.get("packages"), list):
            return doc["packages"]
    lerna_json_path = root / "lerna.json"
    if lerna_json_path.exists():
        doc = _read_json(lerna_json_path) or {}
        return doc.get("packages", ["packages/*"])
    return None


def _expand_npm_workspace_globs(root: Path, patterns: list[str]) -> list[Path]:
    dirs: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        if pattern.startswith("!"):
            continue  # negation globs (workspaces support them) -- not applied, just not treated as members
        for match in root.glob(pattern):
            if match.is_dir() and (match / "package.json").exists() and match not in seen:
                seen.add(match)
                dirs.append(match)
    return dirs


def _declared_npm_dep_names(pkg_json: dict) -> set[str]:
    names: set[str] = set()
    for field_name in _NPM_DEP_FIELDS:
        names.update((pkg_json.get(field_name) or {}).keys())
    return names


def _discover_npm(root: Path) -> list[SubPackage]:
    globs = _npm_workspace_globs(root)
    if globs is None:
        if not (root / "package.json").exists():
            return []
        lock = _find_npm_lockfile(root)
        manifest_files = [_rel(root / "package.json", root)]
        resolved: dict[str, str] = {}
        if lock:
            lock_name, lock_path = lock
            manifest_files.append(_rel(lock_path, root))
            try:
                resolved = parse_npm_lockfile(lock_name, lock_path.read_text())
            except Exception as e:
                log.warning("failed to parse npm lockfile", extra={"path": str(lock_path), "error": str(e)})
        else:
            log.info("npm package with no lockfile -- no resolved versions available", extra={"path": str(root)})
        return [SubPackage(subpath="", ecosystem="npm", resolved=resolved, manifest_files=manifest_files)]

    member_dirs = _expand_npm_workspace_globs(root, globs)
    log.info("npm/yarn/pnpm workspace detected", extra={"globs": globs, "members": len(member_dirs)})

    lock = _find_npm_lockfile(root)
    full_resolved: dict[str, str] = {}
    lock_rel: str | None = None
    if lock:
        lock_name, lock_path = lock
        lock_rel = _rel(lock_path, root)
        try:
            full_resolved = parse_npm_lockfile(lock_name, lock_path.read_text())
        except Exception as e:
            log.warning("failed to parse workspace root lockfile", extra={"path": str(lock_path), "error": str(e)})
    else:
        log.info("npm/yarn/pnpm workspace with no root lockfile -- no resolved versions available", extra={"path": str(root)})

    sub_packages: list[SubPackage] = []
    for member_dir in member_dirs:
        pkg = _read_json(member_dir / "package.json") or {}
        declared = _declared_npm_dep_names(pkg)
        resolved = {name: version for name, version in full_resolved.items() if name in declared}
        manifest_files = [_rel(member_dir / "package.json", root)]
        if lock_rel:
            manifest_files.append(lock_rel)
        sub_packages.append(
            SubPackage(subpath=_rel(member_dir, root), ecosystem="npm", resolved=resolved, manifest_files=manifest_files)
        )

    if (root / "package.json").exists():
        manifest_files = [_rel(root / "package.json", root)]
        if lock_rel:
            manifest_files.append(lock_rel)
        sub_packages.append(SubPackage(subpath="", ecosystem="npm", resolved=full_resolved, manifest_files=manifest_files))

    return sub_packages


def _python_workspace_dirs(root: Path) -> list[Path]:
    dirs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        p = Path(dirpath)
        if p == root:
            continue
        if "pyproject.toml" in filenames or "setup.py" in filenames:
            dirs.append(p)
    return dirs


def _python_subpackage(dir_: Path, root: Path) -> SubPackage:
    manifest_files: list[str] = []
    resolved: dict[str, str] = {}
    for name in ("pyproject.toml", "setup.py"):
        p = dir_ / name
        if p.exists():
            manifest_files.append(_rel(p, root))

    poetry_lock = dir_ / "poetry.lock"
    reqs_txt = dir_ / "requirements.txt"
    if poetry_lock.exists():
        manifest_files.append(_rel(poetry_lock, root))
        try:
            resolved = parse_poetry_lock(poetry_lock.read_text())
        except Exception as e:
            log.warning("failed to parse poetry.lock", extra={"path": str(poetry_lock), "error": str(e)})
    elif reqs_txt.exists():
        manifest_files.append(_rel(reqs_txt, root))
        resolved = parse_requirements_txt(reqs_txt.read_text())
    else:
        log.info(
            "python package with no lockfile/pinned requirements -- no resolved versions available",
            extra={"path": str(dir_)},
        )
    return SubPackage(subpath=_rel(dir_, root) if dir_ != root else "", ecosystem="pypi", resolved=resolved, manifest_files=manifest_files)


def _discover_python(root: Path) -> list[SubPackage]:
    member_dirs = _python_workspace_dirs(root)
    if member_dirs:
        log.info("python multi-package layout detected", extra={"members": len(member_dirs)})
        sub_packages = [_python_subpackage(d, root) for d in member_dirs]
        if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")):
            sub_packages.append(_python_subpackage(root, root))
        return sub_packages

    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "requirements.txt")):
        return [_python_subpackage(root, root)]
    return []


def _discover_stub_manifests(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname, ecosystem in STUB_ECOSYSTEM_MANIFESTS.items():
            if fname in filenames:
                rel = _rel(Path(dirpath) / fname, root)
                found.append(rel)
                log.info("found manifest for an unparsed ecosystem (TODO)", extra={"path": rel, "ecosystem": ecosystem})
    return found


def discover(repo_root: str) -> DiscoveryResult:
    root = Path(repo_root).resolve()
    return DiscoveryResult(
        sub_packages=[*_discover_npm(root), *_discover_python(root)],
        stub_manifests_found=_discover_stub_manifests(root),
    )
