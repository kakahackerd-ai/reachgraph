"""Resolved-version parsing for the three npm-ecosystem lockfiles this phase
supports: package-lock.json (lockfileVersion 1 and 2/3), classic yarn.lock
(v1 "# yarn lockfile v1" format), and pnpm-lock.yaml.

Every parser returns a flat {package_name: resolved_version} dict -- the
full resolved set (direct + transitive), which is what Manifest Discovery
needs to write RESOLVED_VERSION_AT edges. Where a name resolves to more
than one version in the same lockfile (a real, common npm situation for
conflicting transitive requirements), the *shallowest* occurrence wins --
that's the version Node's module resolution actually hands back for a bare
`require(name)`/`import name` at the package root, which is the version
this "internal inventory" cares about; shadowed deeper copies are a real
npm/lockfile detail this simplification deliberately drops for now.
"""

from __future__ import annotations

import re

import yaml


def parse_npm_lockfile(filename: str, content: str) -> dict[str, str]:
    if filename == "package-lock.json":
        import json

        return _parse_package_lock(json.loads(content))
    if filename == "yarn.lock":
        return _parse_yarn_lock(content)
    if filename == "pnpm-lock.yaml":
        return _parse_pnpm_lock(content)
    raise ValueError(f"unsupported npm lockfile: {filename!r}")


def _parse_package_lock(doc: dict) -> dict[str, str]:
    if "packages" in doc:
        return _parse_package_lock_v2(doc)
    if "dependencies" in doc:
        return _parse_package_lock_v1(doc)
    return {}


def _parse_package_lock_v2(doc: dict) -> dict[str, str]:
    # Keys look like "node_modules/name", "node_modules/@scope/name", or
    # nested "node_modules/a/node_modules/name" for a shadowed copy; the
    # root package itself is keyed "". rsplit on the *last* "node_modules/"
    # segment gives the innermost real package name (scope included).
    best: dict[str, tuple[int, str]] = {}  # name -> (depth, version)
    for key, entry in doc.get("packages", {}).items():
        if not key or "node_modules/" not in key:
            continue
        name = key.rsplit("node_modules/", 1)[1]
        version = entry.get("version")
        if not version:
            continue
        depth = key.count("node_modules/")
        if name not in best or depth < best[name][0]:
            best[name] = (depth, version)
    return {name: version for name, (_depth, version) in best.items()}


def _parse_package_lock_v1(doc: dict) -> dict[str, str]:
    best: dict[str, tuple[int, str]] = {}

    def walk(deps: dict, depth: int) -> None:
        for name, entry in deps.items():
            version = entry.get("version")
            if version and (name not in best or depth < best[name][0]):
                best[name] = (depth, version)
            nested = entry.get("dependencies")
            if nested:
                walk(nested, depth + 1)

    walk(doc.get("dependencies", {}), 0)
    return {name: version for name, (_depth, version) in best.items()}


_YARN_DESCRIPTOR_NAME_RE = re.compile(r'^"?(@?[^@"]+(?:/[^@"]+)?)@')


def _parse_yarn_lock(content: str) -> dict[str, str]:
    if any("__metadata:" in line for line in content.splitlines()[:5]):
        # Yarn Berry (v2+) uses a different, YAML-like format -- not
        # supported by this simple line parser. Detected and skipped
        # rather than mis-parsed; classic yarn.lock (still very common) is
        # handled below.
        return {}
    resolved: dict[str, str] = {}
    block_descriptors: list[str] = []
    for line in content.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not line.startswith(" "):
            # A new descriptor line (or continuation of one), e.g.:
            #   "foo@^1.0.0", "foo@^1.2.0":
            block_descriptors.extend(d.strip().strip(",") for d in line.rstrip(":").split(","))
            continue
        stripped = line.strip()
        if stripped.startswith("version"):
            version = stripped.split(None, 1)[1].strip().strip('"')
            for descriptor in block_descriptors:
                m = _YARN_DESCRIPTOR_NAME_RE.match(descriptor)
                if m:
                    resolved[m.group(1)] = version
            block_descriptors = []
    return resolved


_PNPM_PEER_SUFFIX_RE = re.compile(r"\(.*\)$")


def _split_pnpm_key(key: str) -> tuple[str, str] | None:
    key = key.strip().lstrip("/")
    key = _PNPM_PEER_SUFFIX_RE.sub("", key)
    if key.startswith("@"):
        rest = key[1:]
        if "@" not in rest:
            return None
        scoped_name, version = rest.split("@", 1)
        return f"@{scoped_name}", version
    if "@" not in key:
        return None
    name, version = key.split("@", 1)
    return name, version


def _parse_pnpm_lock(content: str) -> dict[str, str]:
    doc = yaml.safe_load(content) or {}
    packages = doc.get("packages") or {}
    resolved: dict[str, str] = {}
    for key in packages:
        parsed = _split_pnpm_key(key)
        if parsed:
            name, version = parsed
            resolved[name] = version
    return resolved
