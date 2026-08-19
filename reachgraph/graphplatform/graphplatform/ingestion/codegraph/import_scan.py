"""Static, regex-based scan for which files in a repo import which of its
declared external dependencies -- the file-level "where is this used"
granularity a package-level blast radius doesn't give, and specifically
the thing gitnexus_client.py's real gitnexus integration cannot answer
(see this package's __init__.py).

Deliberately not a full AST parser (no tree-sitter/babel dependency
needed): a single import-statement regex per ecosystem, cross-referenced
against the exact set of dependency names already resolved for this
sub-package (from manifest discovery -- not every name on the registry),
is enough to answer "which files import package X" accurately for real
code. False positives (an import-shaped string inside a comment or
non-code string literal) are rare enough not to matter for a blast-radius
view; false negatives from fully dynamic imports (`require(someVariable)`)
are an accepted, documented limitation -- static analysis can't resolve
those without running the code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".gitnexus"}
_JS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_PY_EXTENSIONS = {".py"}

# Matches the specifier string in `import ... from "X"`, `export ... from
# "X"`, and `require("X")` -- the three real ways a JS/TS file names an
# imported module. Deliberately permissive about what comes before the
# string (import/export clauses have many shapes: default, named, *-as,
# type-only) since we only need the specifier, not to fully parse the
# clause.
_JS_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\brequire\(\s*)['"]([^'"]+)['"]"""
)
_PY_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

_MAX_FILE_BYTES = 2_000_000  # skip pathological single files (generated bundles, etc.)


@dataclass(frozen=True)
class FileImport:
    file_path: str  # relative to the repo root
    package_name: str


def _package_root(specifier: str) -> str:
    """'lodash/fp' -> 'lodash'; '@babel/core/lib/x' -> '@babel/core'."""
    parts = specifier.split("/")
    if specifier.startswith("@") and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0]


def _scan_js_file(text: str, known_packages: set[str]) -> set[str]:
    found: set[str] = set()
    for m in _JS_IMPORT_RE.finditer(text):
        spec = m.group(1)
        if spec.startswith(".") or spec.startswith("/"):
            continue  # relative/local import, not an external package
        pkg = _package_root(spec)
        if pkg in known_packages:
            found.add(pkg)
    return found


def _scan_py_file(text: str, known_packages: set[str]) -> set[str]:
    found: set[str] = set()
    for m in _PY_IMPORT_RE.finditer(text):
        top = m.group(1).split(".")[0]
        if top in known_packages:
            found.add(top)
    return found


def scan_directory_for_imports(
    repo_root: Path,
    subpath: str,
    ecosystem: str,
    known_packages: set[str],
    *,
    max_files: int = 3000,
) -> list[FileImport]:
    """Walk repo_root/subpath (skipping node_modules/.git/etc.), scanning
    every source file of the given ecosystem for imports of any name in
    known_packages. file_path in the results is relative to repo_root
    (not subpath), matching how Application/File keys are built elsewhere.
    """
    base = (repo_root / subpath) if subpath else repo_root
    if not base.is_dir():
        return []
    extensions = _JS_EXTENSIONS if ecosystem == "npm" else _PY_EXTENSIONS
    scan_text = _scan_js_file if ecosystem == "npm" else _scan_py_file

    results: list[FileImport] = []
    scanned = 0
    for path in base.rglob("*"):
        if scanned >= max_files:
            break
        if not path.is_file() or path.suffix not in extensions:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(repo_root).parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for pkg in scan_text(text, known_packages):
            results.append(FileImport(file_path=str(path.relative_to(repo_root)), package_name=pkg))
    return results
