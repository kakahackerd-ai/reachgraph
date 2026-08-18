"""Maintainer Identity Resolution Service.

Two distinct outputs, kept as separate methods per the phase-3 brief:

- SAME_MAINTAINER_AS (Maintainer -> Maintainer): deterministic only. This
  service's one real, available deterministic signal from npm/PyPI's
  public registry data is an exact identity-string match that looks like
  an email address across two *different* Maintainer nodes (different
  `key` -- i.e. different platform/account) -- e.g. the same person
  publishing under the same email on both npm and PyPI. Neither registry's
  public API exposes a signing-key fingerprint or an explicit linked
  GitHub account for a maintainer (a real gap, not an oversight), so
  `evidence_type="signing_key"` and explicit-link matching are not
  implemented here. Anything short of an exact email match is fuzzy --
  see find_fuzzy_candidates, which is intentionally a *separate*, opt-in,
  never-auto-written path: a false positive here corrupts downstream risk
  scoring, so the conservative default is to not link.

- SHARES_INFRASTRUCTURE_WITH (Package -> Package, per phase 1's schema --
  not Maintainer -> Maintainer): fires on a real signal independent of
  confirmed identity -- two *different* packages whose most recent publish
  carried the same npm registry signing-key id (`dist.signatures[].keyid`,
  captured in phase 2's npm connector). **A real caveat worth stating
  plainly**: npm signs most ordinary registry publishes with npm's own
  centralized registry key, so in practice a large fraction of npm
  packages will legitimately share one keyid -- this signal is more useful
  for spotting a package that *deviates* from that shared baseline (a
  different, unexpected keyid shared by only a couple of packages) than
  for treating every same-keyid pair as meaningfully connected. Written at
  medium, not high, confidence for exactly this reason, and documented
  here rather than smoothed over.

Both run reactively off the registry publish event stream (an independent
consumer group on STREAM_REGISTRY, same as VersionIntroductionService --
see that module's docstring on why multiple consumer groups is the right
tool here).
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .. import schema
from ..ingestion.writer import normalize_ecosystem
from ..write_service import GraphWriteService

log = logging.getLogger("graphplatform.enrichment.maintainer_resolution")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MaintainerResolutionService:
    def __init__(self, write_service: GraphWriteService) -> None:
        self._svc = write_service
        # In-process only -- see module docstring: this is a same-run
        # cache of "which package last published under which keyid",
        # rebuilt from scratch each run. The actual conclusions
        # (SHARES_INFRASTRUCTURE_WITH edges) are the persisted source of
        # truth, not this cache; re-running from the start of the stream
        # after a restart just rediscovers the same real signal again,
        # idempotently (write_shares_infrastructure_with is a MERGE).
        self._keyid_to_packages: dict[str, set[str]] = {}

    def process_publish(self, event: dict[str, Any]) -> None:
        ecosystem = normalize_ecosystem(event["ecosystem"]) or event["ecosystem"]
        identity = event.get("maintainer_identity")
        package_key = f"{ecosystem}:{event['package_name']}"

        if identity:
            platform = event.get("maintainer_platform") or ecosystem
            maintainer_key = f"{platform}:maintainer:{identity}"
            self._resolve_same_maintainer(maintainer_key, identity)

        keyid = event.get("signing_keyid")
        if keyid:
            self._resolve_shared_infrastructure(package_key, keyid)

    # -- deterministic: exact verified-email match across accounts --------

    def _resolve_same_maintainer(self, maintainer_key: str, identity: str) -> None:
        if not _EMAIL_RE.match(identity):
            return
        others = self._svc._run(
            "MATCH (m:Maintainer) WHERE m.identity = $identity AND m.key <> $key RETURN m.key AS key",
            identity=identity,
            key=maintainer_key,
            consistency="strong",
        )
        now = datetime.now(timezone.utc)
        for row in others:
            # SAME_MAINTAINER_AS is conceptually symmetric but Cypher
            # relationships are always directed (same real limitation
            # phase 1 already documents for SHARES_INFRASTRUCTURE_WITH) --
            # canonicalize on sorted key order so re-processing in a
            # different order (e.g. after a restart) converges on the same
            # edge instead of creating both directions.
            a, b = sorted((maintainer_key, row["key"]))
            self._svc.write_same_maintainer_as(a, b, 0.95, "verified_email", first_observed_at=now, event_time=now)
            log.info(
                "maintainer-resolution: linked SAME_MAINTAINER_AS",
                extra={"a": a, "b": b, "evidence_type": "verified_email"},
            )

    # -- shared infrastructure: same npm registry signing keyid -----------

    def _resolve_shared_infrastructure(self, package_key: str, keyid: str) -> None:
        existing = self._keyid_to_packages.setdefault(keyid, set())
        now = datetime.now(timezone.utc)
        for other_key in existing:
            if other_key == package_key:
                continue
            a, b = sorted((package_key, other_key))  # canonicalize -- see _resolve_same_maintainer
            self._svc.write_shares_infrastructure_with(
                schema.PACKAGE, a, schema.PACKAGE, b, "signing_key", first_observed_at=now, event_time=now
            )
            log.info("maintainer-resolution: linked SHARES_INFRASTRUCTURE_WITH", extra={"a": a, "b": b, "keyid": keyid})
        existing.add(package_key)

    # -- fuzzy fallback: candidates only, never auto-written ----------------

    def find_fuzzy_candidates(self, threshold: float = 0.85) -> list[dict[str, Any]]:
        """Name/handle similarity across *non-identical* Maintainer
        identities. Returns candidates for review; never writes a
        SAME_MAINTAINER_AS edge itself -- see module docstring on why an
        uncertain link here is worse than no link.
        """
        maintainers = self._svc._run("MATCH (m:Maintainer) RETURN m.key AS key, m.identity AS identity", consistency="strong")
        candidates: list[dict[str, Any]] = []
        for i, a in enumerate(maintainers):
            for b in maintainers[i + 1 :]:
                if a["identity"] == b["identity"]:
                    continue  # exact matches are the deterministic path above
                score = difflib.SequenceMatcher(None, a["identity"] or "", b["identity"] or "").ratio()
                if score >= threshold:
                    candidates.append({"a": a["key"], "b": b["key"], "similarity": round(score, 3)})
        return candidates
