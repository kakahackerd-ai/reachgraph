"""GitHub Repository Scanner Service.

Provides asynchronous background repository scanning (monorepo & workspace
aware), persisting Application & RESOLVED_VERSION_AT edges to HydraDB and
computing an in-repo blast-radius report per discovered dependency.
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

            # 2. For each discovered dependency, compute its in-repo blast radius
            job.progress = "Computing in-repo blast radius per dependency..."
            total_deps = 0
            dependency_options: list[dict[str, Any]] = []
            all_resolved_pkgs: set[str] = set()

            def app_key_for(sub: Any) -> str:
                return f"{org}/{repo}/{sub.subpath}" if sub.subpath else f"{org}/{repo}"

            discovered_apps = [
                {"application_key": app_key_for(sub), "subpath": sub.subpath, "ecosystem": sub.ecosystem, "resolved_count": len(sub.resolved)}
                for sub in disc_res.sub_packages
            ]

            for sub in disc_res.sub_packages:
                for pkg_name, ver in sub.resolved.items():
                    total_deps += 1
                    pkg_key = f"{sub.ecosystem}:{pkg_name}"
                    ver_key = f"{sub.ecosystem}:{pkg_name}@{ver}"
                    if pkg_key in all_resolved_pkgs:
                        continue
                    all_resolved_pkgs.add(pkg_key)

                    blast = self.query_service.blast_radius(ver_key, consistency="causal")
                    in_repo_affected = [
                        s.subpath or "(root)"
                        for s in disc_res.sub_packages
                        if app_key_for(s) in blast.applications
                    ]
                    dependency_options.append({
                        "package_key": pkg_key,
                        "name": pkg_name,
                        "ecosystem": sub.ecosystem,
                        "subpath": sub.subpath or "(root)",
                        "in_repo_blast_radius": in_repo_affected,
                        "total_blast_reach": blast.total_reached,
                    })

            report = {
                "org": org,
                "repo": repo,
                "monorepo": len(disc_res.sub_packages) > 1,
                "discovered_applications": discovered_apps,
                "total_dependencies_scanned": total_deps,
                "unique_packages": len(all_resolved_pkgs),
                "dependency_options": dependency_options,
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
