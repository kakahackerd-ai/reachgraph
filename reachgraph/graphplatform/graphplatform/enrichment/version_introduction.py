"""Version-Introduction Detection Service.

Two reactive halves, each a separate consumer group on phase 2's event
streams (see graphplatform/ingestion/queue.py -- multiple consumer groups
can read the same stream independently, which is exactly what lets this
run alongside GraphIngestionWriter without stealing its messages):

1. record_publish (STREAM_REGISTRY): on every package_version_published
   event, diffs the new version against its immediate *predecessor by
   publish order* (not semver order -- there's no semver library in this
   codebase, and publish order is what a real npm/PyPI timeline actually
   gives you) and caches the diff as properties on the new Version node.
   The predecessor pointer is itself cached on the Package node
   (vi_last_*), so this needs no scan of prior versions.

2. detect_introduction (STREAM_ADVISORY): on every advisory_published
   event, looks up the advisory's *own stated* introduction point (OSV's
   `introduced`, or the first entry of an explicit `versions` list) and
   checks that exact Version node's cached diff. A real structural signal
   there (a dependency added/removed, an install script that newly
   appeared, a publisher change) raises confidence; no signal, or no
   determinable starting version at all, writes low confidence with the
   brief's own suggested wording rather than fabricating precision.

**Scope, stated plainly**: this only ever points at the advisory's own
declared range start (when one exists), then asks "does *that specific*
version look structurally suspicious." It does not walk semver ranges,
does not parse GHSA's free-text `vulnerable_version_range` (no clean
single boundary to extract from strings like "<= 5.1.5"), and where an
advisory offers no `introduced` and no explicit `versions` list at all
(a real, common case for GHSA), there is nothing to point at -- logged and
skipped, not guessed at.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..ingestion.writer import normalize_ecosystem
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.enrichment.version_introduction")

_LOW_CONFIDENCE_EVIDENCE = "no clear diff signal, defaulted to advisory range start"


class VersionIntroductionService:
    def __init__(self, write_service: GraphWriteService) -> None:
        self._svc = write_service

    # -- half 1: cache a structural diff for every newly-published version --

    def _read_package_pointer(self, package_key: str) -> dict[str, Any] | None:
        rows = self._svc._run(
            "MATCH (n:Package {key:$key}) "
            "RETURN n.vi_last_version AS version, n.vi_last_deps AS deps, "
            "n.vi_last_publisher AS publisher, n.vi_last_has_install_script AS has_install_script",
            key=package_key,
            consistency="strong",
        )
        if not rows or rows[0]["version"] is None:
            return None
        return rows[0]

    def record_publish(self, event: dict[str, Any]) -> None:
        ecosystem = normalize_ecosystem(event["ecosystem"]) or event["ecosystem"]
        name = event["package_name"]
        version = event["version"]
        package_key = f"{ecosystem}:{name}"
        version_key = f"{ecosystem}:{name}@{version}"

        deps = sorted((event.get("dependencies") or {}).keys())
        deps_str = ",".join(deps)
        publisher = event.get("maintainer_identity") or ""
        has_install_script = bool(event.get("has_install_script"))

        prior = self._read_package_pointer(package_key)
        signals: list[str] = []
        deps_added: list[str] = []
        deps_removed: list[str] = []
        publisher_changed = False
        install_script_added = False

        if prior is not None:
            prior_deps = set((prior["deps"] or "").split(",")) - {""}
            deps_added = sorted(set(deps) - prior_deps)
            deps_removed = sorted(prior_deps - set(deps))
            publisher_changed = bool(prior["publisher"]) and publisher != prior["publisher"]
            install_script_added = has_install_script and not prior["has_install_script"]
            if deps_added:
                signals.append(f"dependencies added: {', '.join(deps_added)}")
            if deps_removed:
                signals.append(f"dependencies removed: {', '.join(deps_removed)}")
            if publisher_changed:
                signals.append(f"publisher changed ({prior['publisher']!r} -> {publisher!r})")
            if install_script_added:
                signals.append("install script newly present")

        self._svc.annotate_version(
            version_key,
            vi_deps_added=",".join(deps_added),
            vi_deps_removed=",".join(deps_removed),
            vi_publisher_changed=publisher_changed,
            vi_install_script_added=install_script_added,
            vi_signal_count=len(signals),
            vi_evidence="; ".join(signals),
            vi_diffed_at=datetime.now(timezone.utc).isoformat(),
            vi_has_predecessor=prior is not None,
        )
        self._svc.annotate_package(
            package_key,
            vi_last_version=version,
            vi_last_deps=deps_str,
            vi_last_publisher=publisher,
            vi_last_has_install_script=has_install_script,
        )
        log.info(
            "version-introduction: cached diff",
            extra={"version_key": version_key, "signal_count": len(signals), "had_predecessor": prior is not None},
        )

    # -- half 2: on a new advisory, check its stated introduction point ------

    def _read_version_diff(self, version_key: str) -> dict[str, Any] | None:
        rows = self._svc._run(
            "MATCH (n:Version {key:$key}) "
            "RETURN n.vi_signal_count AS signal_count, n.vi_evidence AS evidence, n.key AS key",
            key=version_key,
            consistency="strong",
        )
        if not rows or rows[0]["key"] is None:
            return None
        return rows[0]

    def detect_introduction(self, event: dict[str, Any]) -> None:
        advisory_key = f"{event['source']}:{event['advisory_id']}"
        # This edge asserts a detection finding, not a fact with its own
        # external timestamp (unlike a registry/advisory event) -- "now"
        # is both when it was first observed and when it happened.
        now = datetime.now(timezone.utc)

        for item in event.get("affected") or []:
            ecosystem = normalize_ecosystem(item.get("ecosystem", ""))
            name = item.get("package_name")
            if not ecosystem or not name:
                continue

            candidate = item.get("introduced")
            if not candidate and item.get("versions"):
                candidate = item["versions"][0]
            if not candidate:
                log.info(
                    "version-introduction: no stated introduction point to check -- skipping",
                    extra={"advisory_key": advisory_key, "package": f"{ecosystem}:{name}"},
                )
                continue

            version_key = f"{ecosystem}:{name}@{candidate}"
            diff = self._read_version_diff(version_key)
            if diff is None:
                log.info(
                    "version-introduction: candidate version not in graph -- skipping rather than guessing",
                    extra={"advisory_key": advisory_key, "version_key": version_key},
                )
                continue

            signal_count = diff["signal_count"] or 0
            if signal_count >= 2:
                confidence, evidence = 0.9, diff["evidence"]
            elif signal_count == 1:
                confidence, evidence = 0.6, diff["evidence"]
            else:
                confidence, evidence = 0.2, _LOW_CONFIDENCE_EVIDENCE

            self._svc.write_introduced_in(
                advisory_key,
                version_key,
                confidence,
                evidence,
                first_observed_at=now,
                event_time=now,
            )
            log.info(
                "version-introduction: wrote INTRODUCED_IN",
                extra={"advisory_key": advisory_key, "version_key": version_key, "confidence": confidence},
            )
