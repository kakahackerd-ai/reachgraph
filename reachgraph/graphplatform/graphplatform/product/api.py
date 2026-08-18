"""Product Surfaces HTTP API Server -- Phase 6.

Exposes REST API endpoints for:
- npm/PyPI package lookup with caching & rate limiting
- Async GitHub repository scanning & job status polling
- Six core supply-chain questions & predictive cascade
- Real-time alerts & reconciliation sweep
- GitHub App webhook receiver
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..alerting.notifier import InMemoryAlertLog, WebhookNotifier
from ..alerting.service import AlertingService
from ..query.service import QueryReasoningService
from ..reconciliation.service import ReconciliationService
from ..write_service import GraphWriteService
from .bot import GitHubBotService
from .lookup import PackageLookupService, RateLimiter
from .scanner import RepoScannerService

log = logging.getLogger("graphplatform.product.api")


def create_services(write_service: GraphWriteService) -> dict[str, Any]:
    query_svc = QueryReasoningService(write_service)
    alert_log = InMemoryAlertLog()
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL")
    notifiers = [WebhookNotifier(webhook_url)] if webhook_url else [WebhookNotifier()]
    alert_svc = AlertingService(write_service, query_svc, notifiers=notifiers, alert_log=alert_log)
    reconcile_svc = ReconciliationService(write_service, query_svc, alert_svc)
    lookup_svc = PackageLookupService(query_svc, write_service)
    scanner_svc = RepoScannerService(query_svc, write_service)
    bot_svc = GitHubBotService(query_svc, write_service, webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET"))

    return {
        "write_service": write_service,
        "query_service": query_svc,
        "alert_service": alert_svc,
        "alert_log": alert_log,
        "reconciliation_service": reconcile_svc,
        "lookup_service": lookup_svc,
        "scanner_service": scanner_svc,
        "bot_service": bot_svc,
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
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def param(key: str, default: str = "") -> str:
            val = qs.get(key)
            return val[0] if val else default

        if path in ("/health", "/readyz"):
            self._send_json(200, {"status": "ready", "service": "reachgraph-product-api"})
            return

        if path in ("/api/v2/alerts", "/api/v2/alerts/"):
            log_svc: InMemoryAlertLog = self.services["alert_log"]
            limit = int(param("limit", "50"))
            self._send_json(200, {"status": "ok", "alerts": log_svc.list_alerts(limit), "total_alerts": log_svc.count})
            return

        if path in ("/api/v2/reconciliation/status", "/api/v2/reconcile/status"):
            r_svc: ReconciliationService = self.services["reconciliation_service"]
            self._send_json(200, {"status": "ok", **r_svc.status.to_dict()})
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

        if path in ("/api/v2/query/introducing-version",):
            adv_key = param("advisory_id") or param("advisory_key") or param("target")
            if not adv_key:
                self._send_json(400, {"error": "advisory_id is required"})
                return
            res = q_svc.introducing_version(adv_key, consistency=param("consistency", "strong"))
            self._send_json(200, res.to_dict())
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

        if path in ("/api/v2/query/typosquats",):
            pkg_key = param("package") or param("package_key") or param("target")
            res = q_svc.nearby_typosquats(pkg_key, consistency=param("consistency", "strong"))
            self._send_json(200, {"package": pkg_key, "results": [r.to_dict() for r in res], "total": len(res)})
            return

        if path in ("/api/v2/query/shared-maintainers",):
            pkg_key = param("package") or param("package_key") or param("target")
            res = q_svc.shared_maintainers_and_infra(pkg_key, consistency=param("consistency", "strong"))
            self._send_json(200, {"package": pkg_key, "results": [r.to_dict() for r in res], "total": len(res)})
            return

        if path in ("/api/v2/query/predict-propagation",):
            vkey = param("version") or param("flagged_version_key") or param("target")
            res = q_svc.predict_propagation(vkey, write_to_graph=False, consistency=param("consistency", "strong"))
            self._send_json(200, {"version": vkey, "candidates": [r.to_dict() for r in res], "total": len(res)})
            return

        if path in ("/api/v2/query/predict-early-warning",):
            pkg_key = param("package") or param("package_key") or param("target")
            res = q_svc.predict_early_warning(pkg_key, write_to_graph=False, consistency=param("consistency", "strong"))
            self._send_json(200, res.to_dict())
            return

        if path in ("/api/v2/query/detect-chain",):
            a = param("package_a")
            b = param("package_b")
            res = q_svc.detect_chain(a, b, consistency=param("consistency", "strong"))
            self._send_json(200, res.to_dict() if res else {"chain_detected": False, "detail": "No vulnerable interaction chain identified"})
            return

        self._send_json(404, {"error": "endpoint_not_found", "path": path})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json()

        # 1. Package Lookup
        if path == "/api/v2/lookup":
            l_svc: PackageLookupService = self.services["lookup_service"]
            eco = body.get("ecosystem", "npm")
            name = body.get("package") or body.get("name", "")
            ver = body.get("version")
            if not name:
                self._send_json(400, {"error": "package is required"})
                return
            client_ip = self.client_address[0] if self.client_address else "local"
            res = l_svc.lookup(eco, name, ver, client_id=client_ip)
            self._send_json(200 if "error" not in res else 429, res)
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

        # 3. Test Alert Dispatch
        if path in ("/api/v2/alerts/test", "/api/v2/alerts/test/"):
            a_svc: AlertingService = self.services["alerting_service"]
            pkg_key = body.get("package_key", "npm:test-lib")
            adv_id = body.get("advisory_id", "GHSA-manual-test")
            sev = body.get("severity", "HIGH")
            alerts = a_svc.trigger_advisory_written(adv_id, f"{pkg_key}@1.0.0", sev, summary=f"Test alert triggered for {pkg_key}")
            self._send_json(200, {"status": "ok", "alerts_dispatched": len(alerts), "alerts": [a.to_dict() for a in alerts]})
            return

        # 4. Reconciliation
        if path in ("/api/v2/reconciliation/run", "/api/v2/reconcile/run"):
            r_svc: ReconciliationService = self.services["reconciliation_service"]
            auto_correct = body.get("auto_correct", True)
            discs = r_svc.run_sweep(auto_correct=auto_correct)
            self._send_json(200, {
                "status": "completed",
                "discrepancies_found": len(discs),
                "duration_s": r_svc.status.last_sweep_duration_s,
                "discrepancies": [d.to_dict() for d in discs]
            })
            return

        # 5. GitHub Bot Webhook
        if path == "/api/v2/webhook/github":
            b_svc: GitHubBotService = self.services["bot_service"]
            event_type = self.headers.get("X-GitHub-Event", "push")
            res = b_svc.handle_webhook_event(event_type, body)
            self._send_json(200, res)
            return

        self._send_json(404, {"error": "endpoint_not_found", "path": path})


def run_api_server(
    write_service: GraphWriteService,
    host: str = "0.0.0.0",
    port: int = 8081,
) -> HTTPServer:
    services = create_services(write_service)
    ProductAPIHandler.services = services
    server = HTTPServer((host, port), ProductAPIHandler)
    log.info("ReachGraph Product API server running on %s:%d", host, port)
    return server
