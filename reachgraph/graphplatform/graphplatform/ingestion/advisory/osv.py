"""OSV advisory connector.

OSV (https://api.osv.dev) has no global "list every advisory" or "changes
since" endpoint -- verified by hand; the documented bulk mechanism is a
per-ecosystem GCS zip snapshot
(osv-vulnerabilities.storage.googleapis.com/<ecosystem>/all.zip) refreshed
periodically, with no incremental cursor either. For a bounded ingestion
sample this connector instead queries the real, verified
POST https://api.osv.dev/v1/query endpoint per (ecosystem, package) pair --
the same shape for both backfill and "live" polling.

Because there's no real push/changes feed to subscribe to, fetch_or_subscribe
here means: re-query the same watched (ecosystem, package) list on an
interval, and only yield an advisory the first time this process sees its
id, or again if its `modified` timestamp has moved since this process last
saw it. This is a real limitation of OSV's public API, not smoothed over.

Ecosystem spelling here is whatever OSV uses ("npm", "PyPI", ...), passed
through as-is on the `affected[].ecosystem` field of each event -- the
writer normalizes to this schema's spelling, not this connector.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import httpx

from ..events import AdvisoryPublished

log = logging.getLogger("graphplatform.ingestion.advisory.osv")

QUERY_URL = "https://api.osv.dev/v1/query"


class OSVConnector:
    name = "osv"

    def __init__(self, *, http: httpx.Client | None = None, poll_interval_s: float = 30.0) -> None:
        self._http = http or httpx.Client(timeout=20.0)
        self._poll_interval_s = poll_interval_s
        self._seen_modified: dict[str, str] = {}  # advisory id -> last-seen `modified`

    def _query_one(self, ecosystem: str, package_name: str) -> list[dict]:
        resp = self._http.post(QUERY_URL, json={"package": {"name": package_name, "ecosystem": ecosystem}})
        resp.raise_for_status()
        return resp.json().get("vulns", [])

    def _to_event(self, vuln: dict, ecosystem: str, package_name: str) -> AdvisoryPublished:
        severity = vuln.get("database_specific", {}).get("severity", "UNKNOWN")
        affected = []
        for entry in vuln.get("affected", []):
            pkg = entry.get("package", {})
            item: dict[str, object] = {
                "ecosystem": pkg.get("ecosystem", ecosystem),
                "package_name": pkg.get("name", package_name),
            }
            versions = entry.get("versions")
            if versions:
                item["versions"] = versions
            affected.append(item)
        return AdvisoryPublished(
            source=self.name,
            advisory_id=vuln["id"],
            summary=vuln.get("summary") or vuln.get("details", "")[:200],
            severity=severity,
            advisory_published_at=vuln.get("published") or vuln.get("modified"),
            affected=affected,
        )

    def backfill(self, packages: list[tuple[str, str]]) -> Iterator[AdvisoryPublished]:
        """packages: (ecosystem, package_name) pairs in OSV's own spelling,
        e.g. [("npm", "lodash"), ("PyPI", "requests")].
        """
        seen_ids: set[str] = set()
        for ecosystem, package_name in packages:
            for vuln in self._query_one(ecosystem, package_name):
                if vuln["id"] in seen_ids:
                    continue
                seen_ids.add(vuln["id"])
                self._seen_modified[vuln["id"]] = vuln.get("modified", "")
                yield self._to_event(vuln, ecosystem, package_name)

    def fetch_or_subscribe(
        self, *, watch: list[tuple[str, str]], max_iterations: int | None = None
    ) -> Iterator[AdvisoryPublished]:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            for ecosystem, package_name in watch:
                for vuln in self._query_one(ecosystem, package_name):
                    modified = vuln.get("modified", "")
                    if self._seen_modified.get(vuln["id"]) == modified:
                        continue
                    self._seen_modified[vuln["id"]] = modified
                    yield self._to_event(vuln, ecosystem, package_name)
            time.sleep(self._poll_interval_s)

    def close(self) -> None:
        self._http.close()
