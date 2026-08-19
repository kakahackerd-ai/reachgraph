"""Query & Reasoning Data Models.

Structured representations for the blast-radius-oriented reasoning queries
in query/service.py: transitive exposure, live resolution windows, and the
core blast-radius traversal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TransitiveExposureResult:
    """Answer to Question 1: Which internal applications are transitively exposed?"""
    application_key: str
    org: str
    repo: str
    subpath: str
    resolved_version: str
    resolved_at: str
    depth: int
    path: list[str]  # chain of keys from application to target
    status: Literal["confirmed", "predicted"] = "confirmed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.status,
            "application_key": self.application_key,
            "org": self.org,
            "repo": self.repo,
            "subpath": self.subpath,
            "resolved_version": self.resolved_version,
            "resolved_at": self.resolved_at,
            "depth": self.depth,
            "path": self.path,
        }


@dataclass
class LiveResolutionResult:
    """Answer to Question 3: Which applications resolved the compromised version while it was live?"""
    application_key: str
    version_key: str
    resolved_at: str
    superseded_at: str | None
    window_start: str
    window_end: str
    was_live_in_window: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_key": self.application_key,
            "version_key": self.version_key,
            "resolved_at": self.resolved_at,
            "superseded_at": self.superseded_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "was_live_in_window": self.was_live_in_window,
        }


@dataclass
class BlastRadiusNode:
    key: str
    label: str  # "Package" | "Application" | "Version"
    depth: int
    path: list[str]


@dataclass
class BlastRadiusResult:
    """Answer to Question 4: What is the complete blast radius?"""
    source_key: str
    total_reached: int
    max_depth: int
    packages: list[str]
    applications: list[str]
    nodes: list[BlastRadiusNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "total_reached": self.total_reached,
            "max_depth": self.max_depth,
            "packages": self.packages,
            "applications": self.applications,
            "nodes": [
                {"key": n.key, "label": n.label, "depth": n.depth, "path": n.path}
                for n in self.nodes
            ],
        }


def blast_radius_to_graph(result: BlastRadiusResult) -> dict[str, Any]:
    """Serialize a BlastRadiusResult to a plain {nodes, edges} graph for the
    frontend's 3D renderer. BlastRadiusResult carries each node's full path
    from the source but no edge list -- synthesize edges from consecutive
    path elements, deduped.
    """
    nodes = [{"key": result.source_key, "label": "Package" if "@" not in result.source_key else "Version", "depth": 0}]
    nodes += [{"key": n.key, "label": n.label, "depth": n.depth} for n in result.nodes if n.key != result.source_key]

    edges: list[dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()
    for n in result.nodes:
        for a, b in zip(n.path, n.path[1:]):
            if (a, b) in seen_edges:
                continue
            seen_edges.add((a, b))
            edges.append({"source": a, "target": b})

    return {"nodes": nodes, "edges": edges}
