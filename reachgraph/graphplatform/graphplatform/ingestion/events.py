"""Normalized events published to the event queue (queue.py) by registry and
advisory connectors, and consumed by writer.GraphIngestionWriter, which is
the only thing in the ingestion layer that turns these into
GraphWriteService calls -- connectors never touch the graph directly.

Kept as plain JSON-serializable dicts on the wire; these dataclasses are the
typed shape connectors and the writer agree on. `type` doubles as the
dict's discriminator field the writer switches on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STREAM_REGISTRY = "graphplatform:registry"
STREAM_ADVISORY = "graphplatform:advisory"


@dataclass
class PackageVersionPublished:
    ecosystem: str  # "npm" | "pypi"
    package_name: str
    version: str
    event_time: str  # ISO-8601 -- the registry's real publish timestamp, never "now"
    dependencies: dict[str, str] = field(default_factory=dict)  # name -> range
    maintainer_identity: str | None = None
    maintainer_platform: str | None = None
    source: str = ""  # connector name, for logging/debugging
    type: str = field(default="package_version_published", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdvisoryPublished:
    source: str  # "osv" | "ghsa"
    advisory_id: str
    summary: str
    severity: str
    advisory_published_at: str  # ISO-8601
    # each item: {"ecosystem": str, "package_name": str, "versions": [str, ...] (optional, exact affected versions if enumerable)}
    affected: list[dict[str, Any]] = field(default_factory=list)
    type: str = field(default="advisory_published", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def event_from_dict(raw: dict[str, Any]) -> PackageVersionPublished | AdvisoryPublished:
    kind = raw.get("type")
    if kind == "package_version_published":
        return PackageVersionPublished(**{k: v for k, v in raw.items() if k != "type"})
    if kind == "advisory_published":
        return AdvisoryPublished(**{k: v for k, v in raw.items() if k != "type"})
    raise ValueError(f"unknown event type: {kind!r}")
