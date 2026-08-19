"""GraphIngestionWriter -- the only consumer of ingestion events, and the
only piece of the ingestion layer allowed to call GraphWriteService.
Connectors never touch the graph directly; they publish events (events.py)
to the queue (queue.py), and a caller feeds each one to
GraphIngestionWriter.handle, typically as the handler passed to
queue.EventQueue.subscribe.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import schema
from ..schema import from_iso
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.ingestion.writer")

# Upstream sources use their own ecosystem spelling (OSV: "PyPI", GHSA:
# "pip", npm/PyPI connectors: already "npm"/"pypi"); this schema only knows
# "npm" and "pypi". Connectors deliberately don't normalize -- this is the
# one place that maps every upstream spelling onto the schema's.
_ECOSYSTEM_ALIASES = {"npm": "npm", "pypi": "pypi", "pip": "pypi"}


def normalize_ecosystem(raw: str) -> str | None:
    return _ECOSYSTEM_ALIASES.get(raw.lower())


class GraphIngestionWriter:
    def __init__(self, write_service: GraphWriteService) -> None:
        self._svc = write_service

    def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "package_version_published":
            self._handle_package_version_published(event)
        else:
            raise ValueError(f"unknown event type: {kind!r}")

    def _handle_package_version_published(self, event: dict[str, Any]) -> None:
        ecosystem = event["ecosystem"]
        package_name = event["package_name"]
        version = event["version"]
        event_time = from_iso(event["event_time"])
        now = datetime.now(timezone.utc)

        package_key = f"{ecosystem}:{package_name}"
        version_key = f"{ecosystem}:{package_name}@{version}"

        self._svc.upsert_package(package_key, ecosystem, package_name, first_observed_at=now, event_time=event_time)
        self._svc.upsert_version(version_key, package_key, version, first_observed_at=now, event_time=event_time)

        for dep_name, dep_range in (event.get("dependencies") or {}).items():
            dep_key = f"{ecosystem}:{dep_name}"
            # The dependency's own Package node may not exist yet if we've
            # never seen it published directly -- upsert a stub now; a real
            # publish event for it later fills in the same ecosystem/name
            # (idempotent) and never touches first_observed_at again.
            self._svc.upsert_package(dep_key, ecosystem, dep_name, first_observed_at=now, event_time=event_time)
            self._svc.write_depends_on(
                schema.PACKAGE,
                package_key,
                dep_key,
                dep_range,
                ecosystem,
                first_observed_at=now,
                event_time=event_time,
            )

        maintainer_identity = event.get("maintainer_identity")
        if maintainer_identity:
            platform = event.get("maintainer_platform") or ecosystem
            maintainer_key = f"{platform}:maintainer:{maintainer_identity}"
            self._svc.upsert_maintainer(
                maintainer_key, platform, maintainer_identity, first_observed_at=now, event_time=event_time
            )
            self._svc.write_published_by(version_key, maintainer_key, first_observed_at=now, event_time=event_time)

        log.info(
            "ingested package_version_published",
            extra={"package_key": package_key, "version": version, "deps": len(event.get("dependencies") or {})},
        )
