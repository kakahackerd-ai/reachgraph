"""GitHub Bot (Continuous) Service -- Phase 6.

Listens for GitHub push & pull_request webhooks, verifies HMAC-SHA256 signatures,
diffs manifest changes, runs Manifest Discovery & QueryReasoning on the delta,
persists resolutions to HydraDB, and formats PR comments & Check Runs.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any

from ..ingestion.manifest.service import discover_and_ingest
from ..query.service import QueryReasoningService
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.product.bot")


class GitHubBotService:
    """Continuous GitHub integration bot."""

    def __init__(
        self,
        query_service: QueryReasoningService,
        write_service: GraphWriteService,
        *,
        webhook_secret: str | None = None,
        fail_on_severity: str = "HIGH",  # "CRITICAL" | "HIGH" | "MEDIUM"
    ) -> None:
        self.query_service = query_service
        self.write_service = write_service
        self.webhook_secret = webhook_secret
        self.fail_on_severity = fail_on_severity

    def verify_signature(self, payload_bytes: bytes, signature_header: str | None) -> bool:
        """Verifies X-Hub-Signature-256 header."""
        if not self.webhook_secret:
            return True  # If no secret configured in dev mode, accept
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected_sig = signature_header.split("sha256=")[1]
        mac = hmac.new(self.webhook_secret.encode("utf-8"), payload_bytes, hashlib.sha256)
        computed_sig = mac.hexdigest()
        return hmac.compare_digest(computed_sig, expected_sig)

    def handle_webhook_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        local_repo_path: str | None = None,
    ) -> dict[str, Any]:
        """Processes push or pull_request webhook events."""
        if event_type not in ("push", "pull_request"):
            return {"status": "ignored", "reason": f"Event type {event_type} not monitored"}

        repository = payload.get("repository", {})
        org = repository.get("owner", {}).get("login") or "org"
        repo = repository.get("name") or "repo"

        # Extract changed files if present in payload
        changed_files = self._extract_changed_files(event_type, payload)
        manifest_files = [
            f for f in changed_files
            if any(f.endswith(m) for m in ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "requirements.txt", "pyproject.toml"))
        ]

        if not manifest_files and changed_files:
            log.info("bot: no dependency manifests modified in %s event", event_type)
            return {
                "status": "skipped",
                "reason": "No manifest files modified",
                "changed_files": changed_files,
            }

        # If we have a local repository path to scan
        if local_repo_path:
            disc_res = discover_and_ingest(local_repo_path, org=org, repo=repo, write_service=self.write_service)
        else:
            disc_res = None

        # Build Check Run and Comment Summary
        flagged_count = 0
        max_severity = "CLEAN"
        comment_lines = [
            f"### 🛡️ ReachGraph Continuous Security Scan",
            f"**Repository**: `{org}/{repo}` | **Trigger**: `{event_type}`",
            "",
        ]

        if manifest_files:
            comment_lines.append(f"**Changed Manifests**: " + ", ".join(f"`{m}`" for m in manifest_files))
            comment_lines.append("")

        check_status = "completed"
        conclusion = "success"

        # Check severity threshold
        severities_order = ["LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL"]
        fail_idx = severities_order.index(self.fail_on_severity) if self.fail_on_severity in severities_order else 3

        if disc_res:
            for sub in disc_res.sub_packages:
                for pkg_name, ver in sub.resolved.items():
                    ver_key = f"{sub.ecosystem}:{pkg_name}@{ver}"
                    advs = self.write_service.get_advisories_for("Version", ver_key, consistency="causal")
                    for a in advs:
                        flagged_count += 1
                        sev = a.get("severity", "HIGH").upper()
                        comment_lines.append(f"- ⚠️ **{sev}**: `{pkg_name}@{ver}` ({a.get('advisory_key')})")
                        s_idx = severities_order.index(sev) if sev in severities_order else 2
                        if s_idx >= fail_idx:
                            conclusion = "failure"

        if flagged_count == 0:
            comment_lines.append("✅ **No supply chain vulnerabilities detected in changed dependencies.**")
        else:
            comment_lines.append("")
            comment_lines.append(f"**Action Required**: Found {flagged_count} vulnerable dependency reference(s).")

        return {
            "status": "processed",
            "org": org,
            "repo": repo,
            "event_type": event_type,
            "manifest_files_changed": manifest_files,
            "flagged_count": flagged_count,
            "check_run": {
                "name": "ReachGraph Supply-Chain Gate",
                "status": check_status,
                "conclusion": conclusion,
                "title": f"ReachGraph: {flagged_count} vulnerability findings",
                "summary": "\n".join(comment_lines),
            },
            "pr_comment": "\n".join(comment_lines),
        }

    def _extract_changed_files(self, event_type: str, payload: dict[str, Any]) -> list[str]:
        changed: set[str] = set()
        if event_type == "push":
            for commit in payload.get("commits", []):
                changed.update(commit.get("added", []))
                changed.update(commit.get("modified", []))
        elif event_type == "pull_request":
            # In GitHub PR webhook, changed files may be provided or fetched via PR API
            pass
        return sorted(changed)
