"""Product Surfaces HTTP API Server.

Exposes REST API endpoints for:
- npm/PyPI package blast radius (metadata + dependents, cached & rate limited)
- Async GitHub repository scanning & job status polling
- Blast-radius reasoning queries (transitive exposure, live resolutions, blast radius)
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..query.service import QueryReasoningService
from ..write_service import GraphWriteService, HydraDBWriteCeilingExceeded
from .lookup import PackageLookupService
from .scanner import RepoScannerService

log = logging.getLogger("graphplatform.product.api")


def create_services(write_service: GraphWriteService) -> dict[str, Any]:
    query_svc = QueryReasoningService(write_service)
    lookup_svc = PackageLookupService(query_svc, write_service)
    scanner_svc = RepoScannerService(query_svc, write_service)

    return {
        "write_service": write_service,
        "query_service": query_svc,
        "lookup_service": lookup_svc,
        "scanner_service": scanner_svc,
    }


class ProductAPIHandler(BaseHTTPRequestHandler):
    services: dict[str, Any] = {}

    def _send_json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hub-Signature-256")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hub-Signature-256")
        self.end_headers()

    def do_GET(self) -> None:
        try:
            self._do_GET()
        except HydraDBWriteCeilingExceeded as exc:
            self._send_json(503, {"error": "hydradb_write_ceiling_exceeded", "message": str(exc)})
        except Exception:
            log.exception("unhandled error in GET %s", self.path)
            self._send_json(500, {"error": "internal_error"})

    def do_POST(self) -> None:
        try:
            self._do_POST()
        except HydraDBWriteCeilingExceeded as exc:
            self._send_json(503, {"error": "hydradb_write_ceiling_exceeded", "message": str(exc)})
        except Exception:
            log.exception("unhandled error in POST %s", self.path)
            self._send_json(500, {"error": "internal_error"})

    def _do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def param(key: str, default: str = "") -> str:
            val = qs.get(key)
            return val[0] if val else default

        if path in ("/health", "/readyz"):
            self._send_json(200, {"status": "ready", "service": "reachgraph-product-api"})
            return

        if path.startswith("/api/v2/jobs/"):
            job_id = path.removeprefix("/api/v2/jobs/")
            s_svc: RepoScannerService = self.services["scanner_service"]
            job = s_svc.get_job(job_id)
            if not job:
                self._send_json(404, {"error": "job_not_found", "job_id": job_id})
                return
            self._send_json(200, job.to_dict())
            return

        # Query GET routes
        q_svc: QueryReasoningService = self.services["query_service"]

        if path in ("/api/v2/query/exposure", "/api/v2/query/transitive-exposure"):
            target = param("target") or param("target_key") or param("package")
            if not target:
                self._send_json(400, {"error": "target parameter is required"})
                return
            res = q_svc.transitive_exposure(target, consistency=param("consistency", "strong"))
            self._send_json(200, {"target": target, "results": [r.to_dict() for r in res], "total": len(res)})
            return

        if path in ("/api/v2/query/resolutions", "/api/v2/query/live-resolutions"):
            vkey = param("version") or param("version_key") or param("target")
            w_start = param("window_start") or "2000-01-01T00:00:00Z"
            w_end = param("window_end") or None
            res = q_svc.live_resolutions(vkey, w_start, w_end, consistency=param("consistency", "strong"))
            self._send_json(200, {"version": vkey, "results": [r.to_dict() for r in res], "total": len(res)})
            return

        if path in ("/api/v2/query/blast-radius",):
            start_key = param("target") or param("start_key") or param("package")
            max_depth = int(param("max_depth", "5"))
            res = q_svc.blast_radius(start_key, max_depth=max_depth, consistency=param("consistency", "strong"))
            self._send_json(200, res.to_dict())
            return

        self._send_json(404, {"error": "endpoint_not_found", "path": path})

    def _do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json()

        # 1. Package Lookup
        if path == "/api/v2/lookup":
            l_svc: PackageLookupService = self.services["lookup_service"]
            eco = body.get("ecosystem", "npm")
            name = body.get("package") or body.get("name", "")
            ver = body.get("version")
            max_dependents = int(body.get("max_dependents", 100))
            if not name:
                self._send_json(400, {"error": "package is required"})
                return
            client_ip = self.client_address[0] if self.client_address else "local"
            res = l_svc.lookup(eco, name, ver, client_id=client_ip, max_dependents=max_dependents)
            status = 429 if res.get("error") == "rate_limit_exceeded" else 404 if res.get("error") == "package_not_found" else 200
            self._send_json(status, res)
            return

        # 2. Repo Scan Job Submission
        if path == "/api/v2/scan-repo":
            s_svc: RepoScannerService = self.services["scanner_service"]
            target = body.get("target") or body.get("repo", "")
            if not target:
                self._send_json(400, {"error": "target repository is required"})
                return
            org = body.get("org")
            repo = body.get("repo_name")
            job_id = s_svc.submit_scan(target, org=org, repo=repo)
            self._send_json(202, {"status": "queued", "job_id": job_id, "poll_url": f"/api/v2/jobs/{job_id}"})
            return

        self._send_json(404, {"error": "endpoint_not_found", "path": path})


def run_api_server(
    write_service: GraphWriteService,
    host: str = "0.0.0.0",
    port: int = 8081,
) -> ThreadingHTTPServer:
    services = create_services(write_service)
    ProductAPIHandler.services = services
    server = ThreadingHTTPServer((host, port), ProductAPIHandler)
    log.info("ReachGraph Product API server running on %s:%d", host, port)
    return server
