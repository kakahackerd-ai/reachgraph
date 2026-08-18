"""GitHub Repository Scanner Service -- Phase 6.

Provides asynchronous background repository scanning (monorepo & workspace aware),
persisting Application & RESOLVED_VERSION_AT edges to HydraDB and computing
in-repo blast radius and risk reports.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from ..ingestion.manifest.service import discover_and_ingest
from ..query.service import QueryReasoningService
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.product.scanner")


@dataclass
class ScanJob:
    job_id: str
    target: str  # URL or local path or org/repo
    status: Literal["queued", "running", "completed", "failed"]
    started_at: str
    finished_at: str | None = None
    progress: str = ""
    error: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "target": self.target,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
        }


class RepoScannerService:
    """Manages asynchronous repo scan jobs."""

    def __init__(
        self,
        query_service: QueryReasoningService,
        write_service: GraphWriteService,
    ) -> None:
        self.query_service = query_service
        self.write_service = write_service
        self.jobs: dict[str, ScanJob] = {}
        self._lock = threading.Lock()

    def submit_scan(
        self,
        repo_target: str,
        *,
        org: str | None = None,
        repo: str | None = None,
    ) -> str:
        """Submit a new background repository scan job."""
        job_id = f"job-{uuid.uuid4().hex[:10]}"
        job = ScanJob(
            job_id=job_id,
            target=repo_target,
            status="queued",
            started_at=datetime.now().isoformat(),
            progress="Job queued for processing",
        )
        with self._lock:
            self.jobs[job_id] = job

        threading.Thread(
            target=self._run_job,
            args=(job_id, repo_target, org, repo),
            daemon=True,
        ).start()

        return job_id

    def get_job(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self.jobs.get(job_id)

    def _run_job(
        self,
        job_id: str,
        repo_target: str,
        org: str | None,
        repo: str | None,
    ) -> None:
        job = self.jobs[job_id]
        job.status = "running"
        job.progress = "Discovering manifests and lockfiles..."

        try:
            local_path = repo_target
            temp_dir = None

            if repo_target.startswith("http://") or repo_target.startswith("https://") or repo_target.startswith("git@"):
                job.progress = "Cloning repository..."
                temp_dir = tempfile.mkdtemp(prefix="reachgraph_scan_")
                # Shallow clone
                import subprocess
                cmd = ["git", "clone", "--depth", "1", repo_target, temp_dir]
                res = subprocess.run(cmd, capture_output=True, text=True)
                if res.returncode != 0:
                    raise RuntimeError(f"Git clone failed: {res.stderr}")
                local_path = temp_dir

                # Infer org/repo from URL
                cleaned = repo_target.rstrip("/").removesuffix(".git")
                parts = cleaned.split("/")
                if len(parts) >= 2:
                    org = org or parts[-2].split(":")[-1]
                    repo = repo or parts[-1]

            org = org or "org"
            repo = repo or "repo"

            # 1. Run Phase 2 Manifest Discovery and ingest into HydraDB
            job.progress = "Ingesting repository workspaces into HydraDB..."
            disc_res = discover_and_ingest(local_path, org=org, repo=repo, write_service=self.write_service)

            # 2. For each discovered member, query exposure and risk
            job.progress = "Evaluating supply chain risk and in-repo blast radius..."
            total_deps = 0
            flagged_deps: list[dict[str, Any]] = []
            typosquats_detected: list[dict[str, Any]] = []
            predicted_risks: list[dict[str, Any]] = []
            all_resolved_pkgs: set[str] = set()

            def app_key_for(sub: Any) -> str:
                return f"{org}/{repo}/{sub.subpath}" if sub.subpath else f"{org}/{repo}"

            discovered_apps = [
                {"application_key": app_key_for(sub), "subpath": sub.subpath, "ecosystem": sub.ecosystem, "resolved_count": len(sub.resolved)}
                for sub in disc_res.sub_packages
            ]

            for sub in disc_res.sub_packages:
                sub_app_key = app_key_for(sub)
                for pkg_name, ver in sub.resolved.items():
                    total_deps += 1
                    pkg_key = f"{sub.ecosystem}:{pkg_name}"
                    ver_key = f"{sub.ecosystem}:{pkg_name}@{ver}"
                    all_resolved_pkgs.add(pkg_key)

                    # Query advisories for this version
                    advs = self.write_service.get_advisories_for("Version", ver_key, consistency="causal")
                    pkg_advs = self.write_service.get_advisories_for("Package", pkg_key, consistency="causal")
                    all_adv = list({a["advisory_key"]: a for a in (advs + pkg_advs)}.values())

                    if all_adv:
                        blast = self.query_service.blast_radius(ver_key, consistency="causal")
                        # Check which of the repo's own sub-packages are affected
                        in_repo_affected = [
                            s.subpath or "(root)"
                            for s in disc_res.sub_packages
                            if app_key_for(s) in blast.applications
                        ]
                        flagged_deps.append({
                            "package": pkg_name,
                            "version": ver,
                            "version_key": ver_key,
                            "subpath": sub.subpath or "(root)",
                            "advisories": all_adv,
                            "in_repo_blast_radius": in_repo_affected,
                            "total_blast_reach": blast.total_reached,
                        })

                    # Typosquat check
                    typos = self.query_service.nearby_typosquats(pkg_key, consistency="causal")
                    for t in typos:
                        typosquats_detected.append({
                            "package": pkg_name,
                            "similar_to": t.popular_target,
                            "score": t.similarity_score,
                            "method": t.method,
                        })

                    # Early warning prediction
                    early = self.query_service.predict_early_warning(pkg_key, write_to_graph=False, consistency="causal")
                    if early.risk_score >= 0.4:
                        predicted_risks.append(early.to_dict())

            report = {
                "org": org,
                "repo": repo,
                "monorepo": len(disc_res.sub_packages) > 1,
                "discovered_applications": discovered_apps,
                "total_dependencies_scanned": total_deps,
                "unique_packages": len(all_resolved_pkgs),
                "flagged_dependencies": flagged_deps,
                "flagged_count": len(flagged_deps),
                "typosquats_detected": typosquats_detected,
                "predicted_risks": predicted_risks,
                "scanned_at": datetime.now().isoformat(),
            }

            job.status = "completed"
            job.progress = "Scan completed successfully"
            job.finished_at = datetime.now().isoformat()
            job.result = report

            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

        except Exception as e:
            log.exception("scan job %s failed", job_id)
            job.status = "failed"
            job.error = str(e)
            job.progress = "Scan failed"
            job.finished_at = datetime.now().isoformat()
