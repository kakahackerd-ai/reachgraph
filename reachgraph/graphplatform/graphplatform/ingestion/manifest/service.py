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


# A single sub-package's lockfile can resolve many hundreds of transitive
# dependencies. Package/Version nodes batch into a couple of round trips
# regardless of count, but each RESOLVED_VERSION_AT edge still needs its
# own sequential SET statement (see write_service.py's
# resolve_versions_batch docstring on why this is sequential, not
# thread-pooled: confirmed by hand that concurrent writes hit HydraDB's
# local write-volume ceiling far sooner than the same writes done one at a
# time). 600 sequential SETs is a real, tested-by-hand number that
# completes reliably in well under a minute; this cap exists to bound
# worst-case job duration for pathologically large lockfiles, not because
# the write path is unreliable at this volume. Truncating (deterministically
# -- same lockfile always keeps the same subset) keeps a single scan
# within a safe budget; DiscoveryResult still reports the true total so
# callers can be honest about what was left out.
_DEFAULT_MAX_RESOLVED_PER_SUBPACKAGE = 600


def discover_and_ingest(
    repo_path: str,
    org: str,
    repo: str,
    write_service: GraphWriteService,
    *,
    max_resolved_per_subpackage: int = _DEFAULT_MAX_RESOLVED_PER_SUBPACKAGE,
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
        if len(sub.resolved) > max_resolved_per_subpackage:
            log.warning(
                "truncating resolved dependencies for ingestion -- write-volume ceiling",
                extra={"subpath": sub.subpath or "(root)", "total": len(sub.resolved), "kept": max_resolved_per_subpackage},
            )
            sub.resolved = dict(list(sub.resolved.items())[:max_resolved_per_subpackage])

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
        new_version_keys = {f"{sub.ecosystem}:{name}@{ver}" for name, ver in sub.resolved.items()}

        # Package + Version nodes for every resolved dependency, plus their
        # RESOLVED_VERSION_AT edges to this Application: all bulk writes
        # (see write_service.py's upsert_packages_batch/
        # upsert_versions_batch/resolve_versions_batch) instead of a
        # read-then-write-then-write sequence per dependency -- confirmed
        # live this was the difference between a real ~300-dependency repo
        # scan hanging for minutes and completing in seconds.
        write_service.upsert_packages_batch(
            [(f"{sub.ecosystem}:{name}", sub.ecosystem, name) for name in sub.resolved],
            first_observed_at=now,
            event_time=resolved_at,
        )
        write_service.upsert_versions_batch(
            [(f"{sub.ecosystem}:{name}@{ver}", f"{sub.ecosystem}:{name}", ver) for name, ver in sub.resolved.items()],
            first_observed_at=now,
            event_time=resolved_at,
        )

        write_service.resolve_versions_batch(
            app_key,
            [(f"{sub.ecosystem}:{name}@{ver}", resolved_at) for name, ver in sub.resolved.items()],
            first_observed_at=now,
            event_time=resolved_at,
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
