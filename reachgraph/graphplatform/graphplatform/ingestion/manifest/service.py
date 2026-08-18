"""ManifestInventoryService -- turns Manifest Discovery's output
(discovery.py) into graph writes: one Application node per SubPackage, one
Package+Version node per resolved dependency, and one RESOLVED_VERSION_AT
edge per (Application, dependency) pair, superseding whatever that
Application previously resolved that's no longer present.

discover_and_ingest is deliberately the only public entry point -- a single
callable taking a repo path and returning what it found, with no assumption
that the target is one of this project's own repos. Phase 6's GitHub
scanner and bot are expected to call this directly, unmodified, against
whatever repo they've checked out.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ...write_service import GraphWriteService
from ...schema import from_iso
from .discovery import DiscoveryResult, discover

log = logging.getLogger("graphplatform.ingestion.manifest.service")


def _git_commit_time(repo_root: Path, rel_path: str) -> datetime | None:
    """Best-effort: the lockfile's real last-commit time, used as
    resolved_at in preference to "now" when the target is a git checkout.
    Returns None (caller falls back to "now") for a non-git directory or on
    any git error -- explicitly a best-effort enhancement, not a hard
    requirement, and never raises.
    """
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", rel_path],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    ts = out.stdout.strip()
    if out.returncode != 0 or not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _application_key(org: str, repo: str, subpath: str) -> str:
    return f"{org}/{repo}/{subpath}" if subpath else f"{org}/{repo}"


def discover_and_ingest(
    repo_path: str,
    org: str,
    repo: str,
    write_service: GraphWriteService,
) -> DiscoveryResult:
    """Idempotent and safe to re-run (e.g. on every push): re-running with
    an unchanged lockfile re-merges the same RESOLVED_VERSION_AT edges
    (no-ops, per GraphWriteService's idempotency guarantee); re-running
    after a lockfile changed closes out (supersede_version) whichever prior
    resolutions are no longer present and opens new ones, preserving
    history rather than overwriting it in place.
    """
    root = Path(repo_path).resolve()
    result = discover(str(root))
    now = datetime.now(timezone.utc)

    for sub in result.sub_packages:
        app_key = _application_key(org, repo, sub.subpath)

        resolved_at = now
        if sub.manifest_files:
            commit_time = _git_commit_time(root, sub.manifest_files[-1])
            if commit_time:
                resolved_at = commit_time

        write_service.upsert_application(
            app_key, org, repo, sub.subpath, first_observed_at=now, event_time=resolved_at
        )

        current = {
            row["version_key"]: row["resolved_at"]
            for row in write_service.get_current_resolutions(app_key, consistency="strong")
        }
        new_version_keys: set[str] = set()

        for dep_name, dep_version in sub.resolved.items():
            dep_package_key = f"{sub.ecosystem}:{dep_name}"
            dep_version_key = f"{sub.ecosystem}:{dep_name}@{dep_version}"
            new_version_keys.add(dep_version_key)
            write_service.upsert_package(
                dep_package_key, sub.ecosystem, dep_name, first_observed_at=now, event_time=resolved_at
            )
            write_service.upsert_version(
                dep_version_key, dep_package_key, dep_version, first_observed_at=now, event_time=resolved_at
            )
            write_service.resolve_version(
                app_key, dep_version_key, resolved_at, first_observed_at=now, event_time=resolved_at
            )

        for old_version_key, old_resolved_at_iso in current.items():
            if old_version_key not in new_version_keys:
                write_service.supersede_version(
                    app_key, old_version_key, from_iso(old_resolved_at_iso), superseded_at=resolved_at
                )

        log.info(
            "ingested application",
            extra={
                "app_key": app_key,
                "ecosystem": sub.ecosystem,
                "resolved_deps": len(sub.resolved),
                "superseded": len(current) - len(current.keys() & new_version_keys),
            },
        )

    if result.stub_manifests_found:
        log.info(
            "found manifests for unparsed ecosystems -- TODO in a later phase",
            extra={"paths": result.stub_manifests_found},
        )
    return result
