"""npm registry connector.

Live mode subscribes to npm's real CouchDB-style replication changes feed --
https://replicate.npmjs.com/registry/_changes?since=<seq> -- verified by
hand to be the actual live feed (registry.npmjs.org itself exposes no
changes endpoint; the read replica does, and this is the real, currently
documented way to watch npm for publishes). Each change names a package
whose doc changed; this connector then fetches that package's full doc
(https://registry.npmjs.org/<name>) and diffs its `versions` map against
what this process has already emitted for that package, so a single
metadata-only edit to an old version's doc doesn't get replayed as a fake
publish.

Backfill mode fetches the full doc directly for an explicit package list --
npm has no bulk "every package" endpoint worth using for a bounded sample.

Both modes read dependencies, publish time, and publisher identity straight
off each version's entry in the doc (`versions[v].dependencies`,
`time[v]`, `versions[v]._npmUser`) -- verified by hand against a real doc
(see graphplatform/README.md's "what was verified" note).
"""

from __future__ import annotations

import logging
import time
from typing import Iterator

import httpx

from ..events import PackageVersionPublished

log = logging.getLogger("graphplatform.ingestion.registry.npm")

CHANGES_URL = "https://replicate.npmjs.com/registry/_changes"
PACKAGE_URL = "https://registry.npmjs.org/{name}"


class NpmConnector:
    name = "npm"
    source_type = "npm"

    def __init__(self, *, http: httpx.Client | None = None, poll_interval_s: float = 5.0) -> None:
        self._http = http or httpx.Client(timeout=15.0)
        self._poll_interval_s = poll_interval_s
        # Per-package high-water mark of versions already emitted this
        # process's lifetime -- not persisted; a restart re-diffs from
        # whatever `since` the caller supplies and will re-emit anything
        # published since then. Idempotent writes downstream absorb that.
        self._seen_versions: dict[str, set[str]] = {}
        self.last_seq = 0

    def current_seq(self) -> int:
        """The changes feed's current high-water mark -- a fresh live
        subscription typically wants to start here (watch for new
        publishes from now on) rather than replaying the feed's entire
        history from seq 0. `last_seq` in the response reflects the real
        current db sequence regardless of `limit` -- verified by hand --
        so limit=1 (limit=0 is rejected with 400) is enough to read it
        cheaply.
        """
        resp = self._http.get(CHANGES_URL, params={"since": 0, "limit": 1})
        resp.raise_for_status()
        return int(resp.json()["last_seq"])

    def _fetch_doc(self, name: str) -> dict | None:
        resp = self._http.get(PACKAGE_URL.format(name=name))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _events_from_doc(
        self, doc: dict, only_versions: set[str] | None = None
    ) -> Iterator[PackageVersionPublished]:
        name = doc.get("name")
        if not name:
            return
        times = doc.get("time", {})
        top_maintainers = doc.get("maintainers") or []
        for version, vdoc in (doc.get("versions") or {}).items():
            if only_versions is not None and version not in only_versions:
                continue
            event_time = times.get(version)
            if not event_time:
                # No real publish timestamp for this entry -- skip rather
                # than fabricate one with "now".
                continue
            npm_user = vdoc.get("_npmUser") or (top_maintainers[0] if top_maintainers else {})
            scripts = vdoc.get("scripts") or {}
            dist = vdoc.get("dist") or {}
            signatures = dist.get("signatures") or []
            yield PackageVersionPublished(
                ecosystem="npm",
                package_name=name,
                version=version,
                event_time=event_time,
                dependencies=dict(vdoc.get("dependencies") or {}),
                maintainer_identity=npm_user.get("email") or npm_user.get("name"),
                maintainer_platform="npm",
                has_install_script=any(k in scripts for k in ("preinstall", "install", "postinstall")),
                content_hash=dist.get("shasum"),
                signing_keyid=signatures[0].get("keyid") if signatures else None,
                source=self.name,
            )

    def backfill(self, package_names: list[str]) -> Iterator[PackageVersionPublished]:
        for name in package_names:
            doc = self._fetch_doc(name)
            if doc is None:
                log.warning("npm backfill: package not found", extra={"package": name})
                continue
            yield from self._events_from_doc(doc)

    def fetch_or_subscribe(
        self, *, since: int = 0, limit: int = 10, max_iterations: int | None = None
    ) -> Iterator[PackageVersionPublished]:
        """self.last_seq tracks the highest change seq processed so far --
        callers that need to resume across a process restart should persist
        it after each yielded event and pass it back in as `since`.
        """
        self.last_seq = since
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            resp = self._http.get(CHANGES_URL, params={"since": self.last_seq, "limit": limit})
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                time.sleep(self._poll_interval_s)
                continue
            for change in results:
                self.last_seq = change["seq"]
                pkg_name = change.get("id", "")
                if pkg_name.startswith("_design/"):
                    continue
                doc = self._fetch_doc(pkg_name)
                if doc is None:
                    continue
                already = self._seen_versions.setdefault(pkg_name, set())
                new_versions = set((doc.get("versions") or {}).keys()) - already
                if not new_versions:
                    continue
                for event in self._events_from_doc(doc, only_versions=new_versions):
                    already.add(event.version)
                    yield event
            time.sleep(self._poll_interval_s)

    def close(self) -> None:
        self._http.close()
