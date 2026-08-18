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
        elif kind == "advisory_published":
            self._handle_advisory_published(event)
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

    def _handle_advisory_published(self, event: dict[str, Any]) -> None:
        source = event["source"]
        advisory_id = event["advisory_id"]
        advisory_key = f"{source}:{advisory_id}"
        published_at = from_iso(event["advisory_published_at"])
        now = datetime.now(timezone.utc)

        self._svc.upsert_advisory(
            advisory_key,
            source,
            advisory_id,
            event.get("summary", ""),
            first_observed_at=now,
            event_time=published_at,
        )

        placed = 0
        for item in event.get("affected") or []:
            ecosystem = normalize_ecosystem(item.get("ecosystem", ""))
            package_name = item.get("package_name")
            if not ecosystem or not package_name:
                continue  # can't place this in the graph without a known ecosystem
            placed += 1
            package_key = f"{ecosystem}:{package_name}"
            self._svc.upsert_package(
                package_key, ecosystem, package_name, first_observed_at=now, event_time=published_at
            )
            self._svc.write_affects(
                advisory_key,
                schema.PACKAGE,
                package_key,
                published_at,
                event["severity"],
                first_observed_at=now,
                event_time=published_at,
            )

            versions = item.get("versions") or []
            if len(versions) > 50:
                log.info(
                    "advisory affects too many exact versions to enumerate -- package-level AFFECTS only",
                    extra={"advisory_key": advisory_key, "package_key": package_key, "count": len(versions)},
                )
                continue
            for version in versions:
                version_key = f"{ecosystem}:{package_name}@{version}"
                self._svc.upsert_version(
                    version_key, package_key, version, first_observed_at=now, event_time=published_at
                )
                self._svc.write_affects(
                    advisory_key,
                    schema.VERSION,
                    version_key,
                    published_at,
                    event["severity"],
                    first_observed_at=now,
                    event_time=published_at,
                )

        log.info("ingested advisory_published", extra={"advisory_key": advisory_key, "affected_placed": placed})
