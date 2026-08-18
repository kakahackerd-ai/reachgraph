"""PyPI registry connector.

PyPI publishes no push feed. Live mode polls the real, currently-supported
XML-RPC `changelog_since_serial` method on https://pypi.org/pypi -- verified
by hand: the older `changelog` method now actively rejects calls
("The changelog method has been deprecated, use changelog_since_serial
instead"), and `changelog_since_serial(serial)` returns real
(name, version, timestamp, action, serial) rows, monotonically increasing
by serial. `changelog_last_serial()` (also verified real) gives a starting
point for a fresh subscription. Each "new release" action is a version to
fetch full metadata for.

Backfill mode fetches https://pypi.org/pypi/<name>/json once per package
to enumerate its known versions, then https://pypi.org/pypi/<name>/<version>/json
once per version for that version's real metadata -- verified by hand that
this second fetch is not optional: the unversioned endpoint's `info` block
always describes the *latest* release only (confirmed by diffing
`requests`' unversioned `info.requires_dist` against its 2.25.0-specific
endpoint -- they're genuinely different dependency sets), so using it for
every historical version would silently attach the wrong dependencies to
every version except the newest.

Dependency ranges come from `info.requires_dist`. Entries gated behind an
extra (`; extra == "..."`) are skipped since they're not installed by
default and DEPENDS_ON.range is a single string, not a per-extra map.
"""

from __future__ import annotations

import logging
import re
import time
import xmlrpc.client
from typing import Iterator

import httpx

from ..events import PackageVersionPublished

log = logging.getLogger("graphplatform.ingestion.registry.pypi")

JSON_URL = "https://pypi.org/pypi/{name}/json"
JSON_URL_VERSION = "https://pypi.org/pypi/{name}/{version}/json"
XMLRPC_URL = "https://pypi.org/pypi"

_REQUIRES_DIST_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")


def _parse_requires_dist(requires_dist: list[str] | None) -> dict[str, str]:
    deps: dict[str, str] = {}
    for entry in requires_dist or []:
        base, _, marker = entry.partition(";")
        if "extra ==" in marker:
            continue
        m = _REQUIRES_DIST_NAME_RE.match(base)
        if not m:
            continue
        name, spec = m.group(1), m.group(2).strip()
        deps[name] = spec or "*"
    return deps


class PyPIConnector:
    name = "pypi"
    source_type = "pypi"

    def __init__(self, *, http: httpx.Client | None = None, poll_interval_s: float = 15.0) -> None:
        self._http = http or httpx.Client(timeout=15.0)
        self._rpc = xmlrpc.client.ServerProxy(XMLRPC_URL)
        self._poll_interval_s = poll_interval_s
        self.last_serial = 0

    def current_serial(self) -> int:
        return int(self._rpc.changelog_last_serial())

    def _release_event(self, name: str, version: str) -> PackageVersionPublished | None:
        resp = self._http.get(JSON_URL_VERSION.format(name=name, version=version))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        doc = resp.json()
        release_files = doc.get("urls") or doc.get("releases", {}).get(version) or []
        upload_time = release_files[0]["upload_time_iso_8601"] if release_files else None
        if not upload_time:
            # No real upload timestamp for this release (e.g. it has no
            # files left) -- skip rather than fabricate one with "now".
            return None
        info = doc.get("info", {})
        return PackageVersionPublished(
            ecosystem="pypi",
            package_name=name,
            version=version,
            event_time=upload_time,
            dependencies=_parse_requires_dist(info.get("requires_dist")),
            maintainer_identity=info.get("author_email") or info.get("author") or info.get("maintainer_email"),
            maintainer_platform="pypi",
            source=self.name,
        )

    def backfill(self, package_names: list[str]) -> Iterator[PackageVersionPublished]:
        for name in package_names:
            resp = self._http.get(JSON_URL.format(name=name))
            if resp.status_code == 404:
                log.warning("pypi backfill: package not found", extra={"package": name})
                continue
            resp.raise_for_status()
            doc = resp.json()
            for version in doc.get("releases") or {}:
                event = self._release_event(name, version)
                if event:
                    yield event

    def fetch_or_subscribe(
        self, *, since_serial: int | None = None, max_iterations: int | None = None
    ) -> Iterator[PackageVersionPublished]:
        """self.last_serial tracks progress -- persist and pass back in as
        since_serial to resume across a process restart. Defaults to the
        current serial (i.e. only new releases from now on) when omitted.
        """
        self.last_serial = since_serial if since_serial is not None else self.current_serial()
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            rows = self._rpc.changelog_since_serial(self.last_serial)
            if not rows:
                time.sleep(self._poll_interval_s)
                continue
            for name, version, _ts, action, row_serial in rows:
                self.last_serial = max(self.last_serial, row_serial)
                if action != "new release" or not version:
                    continue
                event = self._release_event(name, version)
                if event:
                    yield event
            time.sleep(self._poll_interval_s)

    def close(self) -> None:
        self._http.close()
