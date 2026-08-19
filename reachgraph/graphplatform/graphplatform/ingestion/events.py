"""Normalized events published to the event queue (queue.py) by registry
connectors, and consumed by writer.GraphIngestionWriter, which is the only
thing in the ingestion layer that turns these into GraphWriteService calls
-- connectors never touch the graph directly.

Kept as plain JSON-serializable dicts on the wire; this dataclass is the
typed shape connectors and the writer agree on. `type` doubles as the
dict's discriminator field the writer switches on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STREAM_REGISTRY = "graphplatform:registry"


@dataclass
class PackageVersionPublished:
    ecosystem: str  # "npm" | "pypi"
    package_name: str
    version: str
    event_time: str  # ISO-8601 -- the registry's real publish timestamp, never "now"
    dependencies: dict[str, str] = field(default_factory=dict)  # name -> range
    maintainer_identity: str | None = None
    maintainer_platform: str | None = None
    has_install_script: bool = False  # real signal (npm scripts.preinstall/install/postinstall); always False for pypi -- see registry/pypi.py
    content_hash: str | None = None  # real, when the registry exposes one (npm dist.shasum, pypi digests.sha256)
    signing_keyid: str | None = None  # real npm registry signing key id (dist.signatures[0].keyid); no pypi equivalent captured here
    source: str = ""  # connector name, for logging/debugging
    type: str = field(default="package_version_published", init=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def event_from_dict(raw: dict[str, Any]) -> PackageVersionPublished:
    kind = raw.get("type")
    if kind == "package_version_published":
        return PackageVersionPublished(**{k: v for k, v in raw.items() if k != "type"})
    raise ValueError(f"unknown event type: {kind!r}")
