"""Package Lookup Service.

Public package lookup surface for Flow 1 (npm/PyPI package blast radius):
resolve the package's registry metadata and GitHub source repo, scrape its
real dependents off GitHub's network/dependents page, write them into
HydraDB as Application-[:DEPENDS_ON]->Package edges, and compute the
resulting blast radius. In-memory caching and token-bucket rate limiting
guard the whole thing since both the scrape and the graph write happen
synchronously within the request.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .. import schema
from ..ingestion.dependents.github_scrape import fetch_dependent_counts, fetch_dependents, fetch_package_metadata
from ..ingestion.registry.npm import NpmConnector
from ..ingestion.registry.pypi import PyPIConnector
from ..query.models import blast_radius_to_graph
from ..query.service import QueryReasoningService
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.product.lookup")


@dataclass
class RateLimiter:
    """Token-bucket rate limiter per client IP / key."""
    capacity: float = 60.0
    refill_rate: float = 1.0  # tokens per second
    _tokens: dict[str, float] = None
    _last_check: dict[str, float] = None
    _lock: threading.Lock = None

    def __post_init__(self) -> None:
        self._tokens = {}
        self._last_check = {}
        self._lock = threading.Lock()

    def acquire(self, client_id: str) -> bool:
        with self._lock:
            now = time.time()
            last = self._last_check.get(client_id, now)
            tokens = self._tokens.get(client_id, self.capacity)

            # Refill tokens
            elapsed = now - last
            tokens = min(self.capacity, tokens + elapsed * self.refill_rate)
            self._last_check[client_id] = now

            if tokens >= 1.0:
                self._tokens[client_id] = tokens - 1.0
                return True
            else:
                self._tokens[client_id] = tokens
                return False


class PackageLookupService:
    """Look up npm/PyPI package metadata plus transitive exposure and blast radius."""

    def __init__(
        self,
        query_service: QueryReasoningService,
        write_service: GraphWriteService,
        *,
        cache_ttl_s: float = 300.0,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.query_service = query_service
        self.write_service = write_service
        self.cache_ttl_s = cache_ttl_s
        self.rate_limiter = rate_limiter or RateLimiter()
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._in_flight_fetches: set[str] = set()
        self._lock = threading.Lock()

    def lookup(
        self,
        ecosystem: str,
        name: str,
        version: str | None = None,
        *,
        client_id: str = "default",
        max_dependents: int = 100,
    ) -> dict[str, Any]:
        # 1. Rate limiting check
        if not self.rate_limiter.acquire(client_id):
            return {
                "error": "rate_limit_exceeded",
                "message": "Too many requests. Please wait before querying again.",
                "retry_after_s": 5,
            }

        eco = "pypi" if ecosystem.lower() in ("pypi", "python") else "npm"
        pkg_key = f"{eco}:{name}"
        cache_key = f"{pkg_key}@{version}" if version else pkg_key

        # 2. Check cache
        with self._lock:
            if cache_key in self._cache:
                ts, cached_data = self._cache[cache_key]
                if time.time() - ts < self.cache_ttl_s:
                    return cached_data

        # 3. Resolve registry metadata + GitHub source repo (one HTTP round trip)
        meta = fetch_package_metadata(eco, name)
        if meta is None:
            return {"status": "error", "error": "package_not_found", "package": name, "ecosystem": eco}
        resolved_version = version or meta.latest_version

        now = datetime.now(timezone.utc)
        already_known = self.write_service.get_package(pkg_key, consistency="causal") is not None
        self.write_service.upsert_package(pkg_key, eco, name, first_observed_at=now, event_time=now)
        if not already_known:
            # Best-effort enrichment of the package's own metadata/forward
            # deps; doesn't gate this response, which only needs pkg_key to
            # exist (just upserted above).
            self._trigger_on_demand_fetch(eco, name)

        # 4. Scrape real dependents off GitHub and write them into the graph
        # as Application-[:DEPENDS_ON]->Package edges. Bulk-write in two
        # batch round trips (nodes, then edges) rather than a Bolt round
        # trip per dependent -- see write_service.py's
        # upsert_applications_batch/write_depends_on_batch_merge_only.
        dependents = {"shown": 0, "known_total": None, "direct_known": None, "indirect_known": None}
        if meta.source_repo:
            owner, repo = meta.source_repo
            page = fetch_dependents(owner, repo, max_items=max_dependents)

            self.write_service.upsert_applications_batch(
                [(dep.key, dep.owner, dep.repo, "") for dep in page.dependents],
                first_observed_at=now,
                event_time=now,
            )
            self.write_service.write_depends_on_batch_merge_only(
                schema.APPLICATION,
                [(dep.key, pkg_key) for dep in page.dependents],
            )
            dependents["shown"] = page.shown
            if resolved_version:
                counts = fetch_dependent_counts(eco, name, resolved_version)
                if counts:
                    dependents["known_total"] = counts.get("dependentCount")
                    dependents["direct_known"] = counts.get("directDependentCount")
                    dependents["indirect_known"] = counts.get("indirectDependentCount")

        # 5. Blast radius outward from the package, now that its real
        # dependents (if any were found) are in the graph.
        blast = self.query_service.blast_radius(pkg_key, consistency="strong")

        result = {
            "status": "ok",
            "package": {
                "ecosystem": eco,
                "name": name,
                "version": resolved_version,
                "repository": f"{meta.source_repo[0]}/{meta.source_repo[1]}" if meta.source_repo else None,
            },
            "dependents": dependents,
            "graph": blast_radius_to_graph(blast),
            "blast_radius": blast.to_dict(),
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }

        # Cache result
        with self._lock:
            self._cache[cache_key] = (time.time(), result)

        return result

    def _trigger_on_demand_fetch(self, ecosystem: str, name: str) -> None:
        key = f"{ecosystem}:{name}"
        with self._lock:
            if key in self._in_flight_fetches:
                return
            self._in_flight_fetches.add(key)

        def worker() -> None:
            try:
                log.info("running on-demand fetch for %s", key)
                if ecosystem == "pypi":
                    pconn = PyPIConnector()
                    events = list(pconn.backfill([name]))
                    pconn.close()
                else:
                    nconn = NpmConnector()
                    events = list(nconn.backfill([name]))
                    nconn.close()

                from ..ingestion.writer import GraphIngestionWriter
                writer = GraphIngestionWriter(self.write_service)
                for ev in events:
                    writer.handle(ev.to_dict())

            except Exception:
                log.exception("on-demand fetch failed for %s", key)
            finally:
                with self._lock:
                    self._in_flight_fetches.discard(key)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
