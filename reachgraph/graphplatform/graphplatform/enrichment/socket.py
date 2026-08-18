"""Socket behavioral-risk adapter.

Uses the real, current, org-scoped batch endpoint `POST /v0/orgs/{org}/purl`
(`https://api.socket.dev`) -- verified by hand against Socket's docs. Its
predecessor, the simpler unauthenticated-org `GET /npm/{package}/{version}/
score`, is documented as deprecated in favor of this one, so this adapter
uses the current shape even for a single package. **A real config
requirement beyond what the phase-3 brief names**: the current endpoint is
org-scoped, so a `SOCKET_ORG_SLUG` is required alongside `SOCKET_API_KEY`
-- either missing is treated as "not configured" for graceful degradation.

The four behavioral signals the brief calls out (install scripts,
obfuscated code, telemetry/network calls, sudden maintainer changes) map
onto four real, confirmed Socket alert-type slugs (docs.socket.dev /
socket.dev/alerts, verified by hand): `installScripts`, `obfuscatedFile`,
`telemetry`, `newAuthor`. The full alert taxonomy has ~80+ types; only
these four are interpreted into dedicated Version properties, matching
what the brief asks for -- everything else in `alerts[]` is still visible
via `socket_alert_count` for later phases, just not individually decoded.

None of this has been exercised against a live Socket API key in this
environment (none is configured here) -- request/response shapes are
built from Socket's public docs, not a hand-verified round trip.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.enrichment.socket")

API_BASE = "https://api.socket.dev/v0"

_SIGNAL_ALERT_TYPES = {
    "installScripts": "socket_has_install_scripts",
    "obfuscatedFile": "socket_has_obfuscated_code",
    "telemetry": "socket_has_telemetry",
    "newAuthor": "socket_recent_maintainer_change",
}


def _purl(ecosystem: str, name: str, version: str) -> str:
    return f"pkg:{ecosystem}/{name}@{version}"


class SocketAdapter:
    def __init__(
        self,
        write_service: GraphWriteService,
        *,
        api_key: str | None = None,
        org_slug: str | None = None,
        cache_ttl_s: float | None = None,
        min_interval_s: float | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._svc = write_service
        self._api_key = api_key if api_key is not None else os.environ.get("SOCKET_API_KEY")
        self._org_slug = org_slug if org_slug is not None else os.environ.get("SOCKET_ORG_SLUG")
        self._cache_ttl_s = cache_ttl_s if cache_ttl_s is not None else float(os.environ.get("SOCKET_CACHE_TTL_S", 3600))
        self._min_interval_s = (
            min_interval_s if min_interval_s is not None else float(os.environ.get("SOCKET_MIN_INTERVAL_S", 1.0))
        )
        self._http = http or httpx.Client(timeout=20.0, auth=(self._api_key or "", ""))
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._last_call_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._org_slug)

    def close(self) -> None:
        self._http.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self._min_interval_s:
            time.sleep(self._min_interval_s - elapsed)
        self._last_call_at = time.monotonic()

    def get_risk(self, ecosystem: str, name: str, version: str) -> dict[str, Any] | None:
        """Real Socket risk data for one package/version, cached for
        cache_ttl_s. Returns None on any degrade path: not configured,
        request failure, or no result for this purl -- never raises.
        """
        if not self.configured:
            log.info("socket: not configured (SOCKET_API_KEY/SOCKET_ORG_SLUG unset) -- skipping risk lookup")
            return None

        purl = _purl(ecosystem, name, version)
        cached = self._cache.get(purl)
        if cached is not None and (time.monotonic() - cached[0]) < self._cache_ttl_s:
            return cached[1]

        self._throttle()
        try:
            resp = self._http.post(
                f"{API_BASE}/orgs/{self._org_slug}/purl",
                params={"alerts": "true"},
                json={"components": [{"purl": purl}]},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("socket: request failed -- degrading to no risk data", extra={"purl": purl, "error": str(e)})
            self._cache[purl] = (time.monotonic(), None)
            return None

        result: dict[str, Any] | None = None
        for line in resp.text.splitlines():  # NDJSON
            line = line.strip()
            if not line:
                continue
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if doc.get("name") == name and doc.get("version") == version:
                result = doc
                break
        self._cache[purl] = (time.monotonic(), result)
        return result

    def sync_enrichment(self, ecosystem: str, name: str, version: str) -> bool:
        """Fetch risk data and annotate the existing Version node. Returns
        True if real data was written, False on any degrade path.
        """
        risk = self.get_risk(ecosystem, name, version)
        if risk is None:
            return False

        version_key = f"{ecosystem}:{name}@{version}"
        score = risk.get("score") or {}
        alerts = risk.get("alerts") or []
        alert_types_present = {a.get("type") for a in alerts}

        properties: dict[str, Any] = {
            "socket_overall_score": score.get("overall"),
            "socket_supply_chain_score": score.get("supplyChain"),
            "socket_vulnerability_score": score.get("vulnerability"),
            "socket_maintenance_score": score.get("maintenance"),
            "socket_quality_score": score.get("quality"),
            "socket_license_score": score.get("license"),
            "socket_alert_count": len(alerts),
            "socket_scored_at": datetime.now(timezone.utc).isoformat(),
        }
        for alert_type, prop_name in _SIGNAL_ALERT_TYPES.items():
            properties[prop_name] = alert_type in alert_types_present

        self._svc.annotate_version(version_key, **{k: v for k, v in properties.items() if v is not None})
        log.info("socket: synced enrichment", extra={"version_key": version_key, "alert_count": len(alerts)})
        return True
