"""GitHub Advisory Database (GHSA) connector.

Uses the real, unauthenticated-capable REST endpoint
GET https://api.github.com/advisories -- verified by hand, works without a
token (unlike the separate Dependabot Alerts endpoint this repo's existing
Go code already learned requires auth even for public repos -- see
github.go's errNoGitHubToken short-circuit; this is a different endpoint).
Unauthenticated calls are capped at 60 req/hour (confirmed via response
headers, matching the general REST cap this repo's Go code already
documents); set GITHUB_TOKEN to raise that to 5000/hour.

Pagination is real GitHub cursor pagination via the `Link: rel="next"`
response header -- verified by hand -- not a manually-constructed `page`
param; `after=<cursor>` is an opaque token GitHub hands back, not something
to build by hand. `sort=updated&direction=desc` (also verified real) is
what live polling walks until it crosses the last `updated_at` this process
has already seen.

`ecosystem` here uses GitHub's own spelling ("npm", "pip", ...) -- note
"pip", not "pypi" -- passed through on `affected[].ecosystem`; the writer
normalizes to this schema's spelling, not this connector.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterator

import httpx

from ..events import AdvisoryPublished

log = logging.getLogger("graphplatform.ingestion.advisory.ghsa")

ADVISORIES_URL = "https://api.github.com/advisories"


class GHSAConnector:
    name = "ghsa"

    def __init__(
        self,
        *,
        http: httpx.Client | None = None,
        token: str | None = None,
        poll_interval_s: float = 60.0,
    ) -> None:
        if token is None:
            token = os.environ.get("GITHUB_TOKEN")
        if not token:
            try:
                import subprocess
                token = subprocess.check_output(["gh", "auth", "token"], text=True, timeout=2).strip()
            except Exception:
                token = None
        self._token = token
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = http or httpx.Client(timeout=20.0, headers=headers)
        self._poll_interval_s = poll_interval_s
        self.last_updated_at = ""

    def _to_event(self, adv: dict) -> AdvisoryPublished:
        affected = []
        for v in adv.get("vulnerabilities", []) or []:
            pkg = v.get("package") or {}
            if not pkg.get("name"):
                continue
            item: dict[str, object] = {"ecosystem": pkg.get("ecosystem", ""), "package_name": pkg["name"]}
            # GHSA gives a raw range string ("<= 5.1.5"), not a clean
            # "introduced" boundary version like OSV's SEMVER events --
            # passed through as-is under `range` for phase 3's
            # version-introduction detection to interpret; `fixed` (when
            # present) is a real, unambiguous boundary.
            vuln_range = v.get("vulnerable_version_range")
            if vuln_range:
                item["range"] = vuln_range
            fixed = v.get("first_patched_version")
            if fixed:
                item["fixed"] = fixed
            affected.append(item)
        return AdvisoryPublished(
            source=self.name,
            advisory_id=adv["ghsa_id"],
            summary=adv.get("summary", ""),
            severity=(adv.get("severity") or "unknown").upper(),
            advisory_published_at=adv.get("published_at") or adv.get("updated_at"),
            affected=affected,
        )

    def _pages(self, params: dict) -> Iterator[list[dict]]:
        url: str | None = ADVISORIES_URL
        next_params: dict | None = params
        while url:
            resp = self._http.get(url, params=next_params)
            if resp.status_code == 403:
                log.warning("ghsa: github rate limit reached or token missing (HTTP 403)")
                break
            resp.raise_for_status()
            yield resp.json()
            next_params = None  # the Link header's next URL already carries every param
            url = resp.links.get("next", {}).get("url")

    def backfill(self, *, ecosystem: str | None = None, max_pages: int = 5) -> Iterator[AdvisoryPublished]:
        params = {"per_page": 100, "sort": "published", "direction": "desc"}
        if ecosystem:
            params["ecosystem"] = ecosystem
        for i, page in enumerate(self._pages(params)):
            if i >= max_pages:
                break
            for adv in page:
                yield self._to_event(adv)

    def fetch_or_subscribe(
        self, *, ecosystem: str | None = None, max_iterations: int | None = None
    ) -> Iterator[AdvisoryPublished]:
        params = {"per_page": 100, "sort": "updated", "direction": "desc"}
        if ecosystem:
            params["ecosystem"] = ecosystem
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            new_high_water = self.last_updated_at
            emitted: list[AdvisoryPublished] = []
            for page in self._pages(params):
                stop = False
                for adv in page:
                    updated_at = adv.get("updated_at", "")
                    if updated_at <= self.last_updated_at:
                        stop = True
                        break
                    new_high_water = max(new_high_water, updated_at)
                    emitted.append(self._to_event(adv))
                if stop:
                    break
            self.last_updated_at = new_high_water
            # API returns newest-first; reverse so a consumer sees advisories
            # in roughly chronological order.
            for event in reversed(emitted):
                yield event
            time.sleep(self._poll_interval_s)

    def close(self) -> None:
        self._http.close()
