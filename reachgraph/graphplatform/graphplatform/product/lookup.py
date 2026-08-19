"""Package Lookup Service.

Public package lookup surface with in-memory caching, token-bucket rate
limiting, and on-demand async fetching for unindexed packages. Blast-radius
dependents (who depends on this package) are populated separately by the
GitHub dependents scrape module -- see ingestion/dependents/github_scrape.py.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..ingestion.registry.npm import NpmConnector
from ..ingestion.registry.pypi import PyPIConnector
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
        trigger_fetch_if_missing: bool = True,
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
        target_key = f"{pkg_key}@{version}" if version else pkg_key
        cache_key = f"{target_key}"

        # 2. Check cache
        with self._lock:
            if cache_key in self._cache:
                ts, cached_data = self._cache[cache_key]
                if time.time() - ts < self.cache_ttl_s:
                    return cached_data

        # 3. Check if package exists in graph
        pkg_data = self.write_service.get_package(pkg_key, consistency="causal")
        if pkg_data is None and trigger_fetch_if_missing:
            # Trigger asynchronous on-demand fetch
            self._trigger_on_demand_fetch(eco, name)
            return {
                "status": "processing",
                "message": f"Package {pkg_key} is not yet indexed; background ingestion triggered. Check back shortly.",
                "package": name,
                "ecosystem": eco,
                "version": version,
            }

        # 4. Transitive exposure + blast radius over whatever DEPENDS_ON/
        # RESOLVED_VERSION_AT edges are already in the graph.
        exposures = [exp.to_dict() for exp in self.query_service.transitive_exposure(target_key, consistency="causal")]
        blast = self.query_service.blast_radius(target_key, consistency="causal").to_dict()

        result = {
            "status": "ok",
            "package": name,
            "ecosystem": eco,
            "version": version,
            "target_key": target_key,
            "transitive_exposures": exposures,
            "blast_radius": blast,
            "cached_at": datetime.now().isoformat(),
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
