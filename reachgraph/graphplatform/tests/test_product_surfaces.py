"""Integration tests for the Product Surfaces (package lookup, repo scanner)."""

from __future__ import annotations

import time

from graphplatform import schema
from graphplatform.product.lookup import PackageLookupService, RateLimiter
from graphplatform.product.scanner import RepoScannerService
from graphplatform.query.service import QueryReasoningService


def test_package_lookup_real_dependents_caching_and_rate_limiting(service, cleanup, run_id):
    # Real, small, stable npm package -- lookup() resolves metadata and
    # scrapes GitHub's real network/dependents page, so this is a real
    # network round trip, not a fixture.
    pkg_key = "npm:is-number"

    query_svc = QueryReasoningService(service)
    limiter = RateLimiter(capacity=2.0, refill_rate=0.0)
    lookup_svc = PackageLookupService(query_svc, service, cache_ttl_s=10.0, rate_limiter=limiter)

    # 1. First lookup
    res1 = lookup_svc.lookup("npm", "is-number", client_id=f"test-client-{run_id}", max_dependents=5)
    assert res1["status"] == "ok"
    assert res1["package"]["ecosystem"] == "npm"
    assert res1["package"]["name"] == "is-number"
    assert res1["package"]["repository"] == "jonschlinkert/is-number"
    assert "blast_radius" in res1
    assert "graph" in res1
    assert res1["dependents"]["shown"] >= 1

    cleanup(schema.PACKAGE, pkg_key)
    for node in res1["graph"]["nodes"]:
        if node["label"] == schema.APPLICATION:
            cleanup(schema.APPLICATION, node["key"])

    # 2. Cached lookup -- must not re-hit the network or re-scrape
    res2 = lookup_svc.lookup("npm", "is-number", client_id=f"test-client-{run_id}", max_dependents=5)
    assert res2["status"] == "ok"
    assert res2["cached_at"] == res1["cached_at"]

    # 3. Exhaust rate limiter
    res_exceeded = lookup_svc.lookup("npm", "is-number", client_id=f"test-client-{run_id}", max_dependents=5)
    assert res_exceeded.get("error") == "rate_limit_exceeded"


def test_repo_scanner_async_job_flow(service, tmp_path, run_id):
    # Create a small local repo structure
    pkg_json = tmp_path / "package.json"
    pkg_json.write_text('{"name": "test-app", "dependencies": {"lodash": "4.17.21"}}')
    lock_json = tmp_path / "package-lock.json"
    lock_json.write_text('{"name": "test-app", "version": "1.0.0", "lockfileVersion": 1, "dependencies": {"lodash": {"version": "4.17.21"}}}')

    query_svc = QueryReasoningService(service)
    scanner = RepoScannerService(query_svc, service)

    job_id = scanner.submit_scan(str(tmp_path), org=f"org-{run_id}", repo="test-app")
    assert job_id.startswith("job-")

    # Poll for completion (up to 8s for live HydraDB writes and traversal)
    for _ in range(80):
        job = scanner.get_job(job_id)
        assert job is not None
        if job.status in ("completed", "failed"):
            break
        time.sleep(0.1)

    assert job.status == "completed"
    assert job.result is not None
    assert job.result["total_dependencies_scanned"] >= 1
    assert any(m["application_key"].endswith("test-app") for m in job.result["discovered_applications"])
