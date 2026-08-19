"""Real integration with the external `gitnexus` CLI (npm package
`gitnexus`, github.com/abhigyanpatwari/GitNexus) -- its local intra-repo
file/function call graph, used to extend "this file directly imports
package X" (import_scan.py) into "these files are reachable from an
importer of X through local calls," a real notion of in-repo blast radius
below the Application level.

Confirmed by hand, not from docs (the README's own account of its CLI was
wrong on several details -- see git history): `gitnexus analyze <path>`
indexes a repo into a local `.gitnexus/` LadybugDB store (no network
calls, no API key, ~5-10s for a small repo); `gitnexus cypher "<query>"`
runs a raw query and prints real structured JSON to stdout --
`{"markdown": "<pipe-table>", "row_count": N}` -- not the MCP protocol
originally planned, which turned out to be unnecessary once the plain CLI
was shown to already emit JSON. Every graph element's JSON is embedded as
one table cell's text in that markdown pipe-table; _parse_row_json below
recovers it. Confirmed relationship types on real code: CALLS, CONTAINS
(Folder->File), DEFINES (File->Function), IMPORTS (File->File, LOCAL
imports only -- see this package's __init__.py for why external packages
never appear at all).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("graphplatform.ingestion.codegraph.gitnexus_client")

_CELL_RE = re.compile(r"^\|(.*)\|$")


class GitNexusUnavailable(RuntimeError):
    """gitnexus isn't runnable in this environment (npx/network failure,
    timeout, or a non-zero exit) -- callers should treat this as
    best-effort enrichment and degrade gracefully, not fail the scan."""


def run_analyze(repo_path: str, *, timeout_s: int = 180) -> None:
    try:
        res = subprocess.run(
            ["npx", "--yes", "gitnexus@latest", "analyze", repo_path, "--skip-skills", "--skip-agents-md"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitNexusUnavailable(f"gitnexus analyze failed to run: {exc}") from exc
    if res.returncode != 0:
        raise GitNexusUnavailable(f"gitnexus analyze exited {res.returncode}: {res.stderr[-2000:]}")


def _resolve_repo_name(repo_path: str, *, timeout_s: int = 30) -> str:
    """gitnexus indexes into a GLOBAL registry (confirmed by hand:
    `gitnexus list` shows every repo ever analyzed on this machine, not
    just the current one), keyed by a name it derives itself -- usually
    package.json's `name` field, NOT the directory basename (confirmed by
    hand: a checkout at .../serve-clone registered as plain "serve",
    matching vercel/serve's real package name). The only reliable way to
    find the right key for a given path is to ask `gitnexus list` and
    match on its `Path:` line.
    """
    try:
        res = subprocess.run(
            ["npx", "--yes", "gitnexus@latest", "list"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitNexusUnavailable(f"gitnexus list failed to run: {exc}") from exc
    if res.returncode != 0:
        raise GitNexusUnavailable(f"gitnexus list exited {res.returncode}: {res.stderr[-2000:]}")

    target = str(Path(repo_path).resolve())
    current_name: str | None = None
    for raw_line in res.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Path:"):
            path_value = line.removeprefix("Path:").strip()
            if current_name and path_value == target:
                return current_name
        elif not line.startswith(("Indexed:", "Commit:", "Branch:", "Stats:", "Clusters:", "Processes:", "Indexed Repositories")):
            current_name = line
    raise GitNexusUnavailable(f"gitnexus list has no entry for {target}")


def _run_cypher(repo_path: str, query: str, *, timeout_s: int = 60) -> list[dict]:
    repo_name = _resolve_repo_name(repo_path, timeout_s=timeout_s)
    try:
        res = subprocess.run(
            ["npx", "--yes", "gitnexus@latest", "cypher", "--repo", repo_name, query],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitNexusUnavailable(f"gitnexus cypher failed to run: {exc}") from exc
    if res.returncode != 0:
        raise GitNexusUnavailable(f"gitnexus cypher exited {res.returncode}: {res.stderr[-2000:]}")

    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise GitNexusUnavailable(f"gitnexus cypher printed non-JSON output: {res.stdout[:500]}") from exc
    if "error" in payload:
        raise GitNexusUnavailable(f"gitnexus cypher query error: {payload['error']}")

    rows: list[dict] = []
    for line in payload.get("markdown", "").splitlines():
        m = _CELL_RE.match(line.strip())
        if not m or set(line.strip()) <= {"|", "-", " "}:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        rows.append({"_cells": cells})
    # First matched row is the header; drop it.
    return rows[1:] if rows else []


@dataclass(frozen=True)
class LocalImportEdge:
    src_file: str
    dst_file: str


def local_file_import_graph(repo_path: str, *, timeout_s: int = 60) -> list[LocalImportEdge]:
    """The repo's own local (file-to-file) IMPORTS edges -- relative
    imports only, per gitnexus's own scope (see module docstring). Column
    order in the query fixes what _cells[0]/_cells[1] mean; not parsing
    the embedded relationship JSON since we only need endpoint paths here.
    """
    rows = _run_cypher(
        repo_path,
        "MATCH (a)-[r]->(b) WHERE r.type = 'IMPORTS' RETURN a.filePath AS src, b.filePath AS dst",
        timeout_s=timeout_s,
    )
    edges: list[LocalImportEdge] = []
    for row in rows:
        cells = row["_cells"]
        if len(cells) < 2:
            continue
        src, dst = cells[0], cells[1]
        if src and dst and src != dst:
            edges.append(LocalImportEdge(src_file=src, dst_file=dst))
    return edges


def locally_reachable_files(seed_files: set[str], edges: list[LocalImportEdge], *, max_depth: int = 6) -> set[str]:
    """Reverse BFS over the local import graph: every file that
    (transitively) imports one of seed_files, i.e. every file that would
    be affected by a change to a seed file through the repo's own local
    call/import chain -- not just the seed files themselves."""
    reverse: dict[str, set[str]] = {}
    for e in edges:
        reverse.setdefault(e.dst_file, set()).add(e.src_file)

    visited = set(seed_files)
    frontier = set(seed_files)
    for _ in range(max_depth):
        next_frontier: set[str] = set()
        for f in frontier:
            for importer in reverse.get(f, ()):
                if importer not in visited:
                    visited.add(importer)
                    next_frontier.add(importer)
        if not next_frontier:
            break
        frontier = next_frontier
    return visited
