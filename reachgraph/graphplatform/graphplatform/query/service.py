"""Query & Reasoning Service -- Phase 4.

The single authoritative analytical interface for answering the six core
supply-chain exposure questions, predictive cascade forecasting, and
chained-vulnerability detection over HydraDB.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .. import schema
from ..schema import Consistency, from_iso, now_utc, to_iso
from ..write_service import GraphWriteService
from .models import (
    BlastRadiusNode,
    BlastRadiusResult,
    ChainRisk,
    EarlyWarningRiskResult,
    IntroducingVersionResult,
    LiveResolutionResult,
    PredictedPropagationResult,
    SharedInfraMaintainerResult,
    TransitiveExposureResult,
    TyposquatResult,
)

log = logging.getLogger("graphplatform.query.service")


class QueryReasoningService:
    """Analytical query and prediction service over HydraDB."""

    def __init__(self, write_service: GraphWriteService) -> None:
        self.write_service = write_service

    # ====================================================================
    # 1. Transitive Exposure
    # ====================================================================

    def transitive_exposure(
        self,
        target_key: str,
        *,
        consistency: Consistency = "causal",
    ) -> list[TransitiveExposureResult]:
        """Question 1: Which internal applications are transitively exposed?

        Performs reverse traversal from a compromised Version or Package up
        through DEPENDS_ON and RESOLVED_VERSION_AT edges to find every
        Application consuming it.
        """
        # Determine whether target_key is a Version or Package
        is_version = "@" in target_key
        pkg_key = target_key.split("@")[0] if is_version else target_key

        results: list[TransitiveExposureResult] = []
        seen_apps: set[str] = set()

        # Step A: Direct resolutions from Application -> Version
        direct_query = (
            f"MATCH (app:{schema.APPLICATION})-[r:{schema.RESOLVED_VERSION_AT}]->"
            f"(v:{schema.VERSION}) "
            f"WHERE r.superseded_at = $open "
            f"RETURN app.key AS app_key, app.org AS org, app.repo AS repo, "
            f"app.subpath AS subpath, v.key AS vkey, r.resolved_at AS resolved_at"
        )
        direct_rows = self.write_service._run(
            direct_query,
            open=schema.OPEN_INTERVAL_SENTINEL,
            consistency=consistency,
        )

        for row in direct_rows:
            vkey = row["vkey"]
            app_key = row["app_key"]
            app_pkg = vkey.split("@")[0] if "@" in vkey else vkey

            if is_version:
                if vkey == target_key and app_key not in seen_apps:
                    seen_apps.add(app_key)
                    results.append(
                        TransitiveExposureResult(
                            application_key=app_key,
                            org=row.get("org") or "",
                            repo=row.get("repo") or "",
                            subpath=row.get("subpath") or "",
                            resolved_version=vkey,
                            resolved_at=row.get("resolved_at") or "",
                            depth=1,
                            path=[app_key, vkey],
                            status="confirmed",
                        )
                    )
            else:
                if app_pkg == pkg_key and app_key not in seen_apps:
                    seen_apps.add(app_key)
                    results.append(
                        TransitiveExposureResult(
                            application_key=app_key,
                            org=row.get("org") or "",
                            repo=row.get("repo") or "",
                            subpath=row.get("subpath") or "",
                            resolved_version=vkey,
                            resolved_at=row.get("resolved_at") or "",
                            depth=1,
                            path=[app_key, vkey],
                            status="confirmed",
                        )
                    )

        # Step B: Transitive dependency graph via DEPENDS_ON
        # Find all upstream packages that depend on pkg_key (up to 8 hops)
        upstream_paths = self._find_upstream_package_paths(pkg_key, consistency=consistency)

        # For each upstream package path [P_n, ..., P_1, pkg_key]
        for upstream_pkg, path_to_target in upstream_paths.items():
            for row in direct_rows:
                vkey = row["vkey"]
                app_key = row["app_key"]
                app_pkg = vkey.split("@")[0] if "@" in vkey else vkey

                if app_pkg == upstream_pkg and app_key not in seen_apps:
                    seen_apps.add(app_key)
                    full_path = [app_key, vkey] + path_to_target[1:]
                    results.append(
                        TransitiveExposureResult(
                            application_key=app_key,
                            org=row.get("org") or "",
                            repo=row.get("repo") or "",
                            subpath=row.get("subpath") or "",
                            resolved_version=vkey,
                            resolved_at=row.get("resolved_at") or "",
                            depth=len(path_to_target),
                            path=full_path,
                            status="confirmed",
                        )
                    )

        results.sort(key=lambda r: r.depth)
        return results

    def _find_upstream_package_paths(
        self, target_pkg: str, consistency: Consistency = "causal", max_depth: int = 8
    ) -> dict[str, list[str]]:
        """BFS reverse traversal through DEPENDS_ON edges to find packages that
        depend on target_pkg directly or transitively.
        Returns: {upstream_pkg_key: [upstream_pkg_key, ..., target_pkg]}
        """
        paths: dict[str, list[str]] = {}
        queue: list[tuple[str, list[str]]] = [(target_pkg, [target_pkg])]
        visited: set[str] = {target_pkg}

        while queue:
            curr_pkg, curr_path = queue.pop(0)
            if len(curr_path) > max_depth:
                continue

            dependents = self.write_service.get_dependents_of(curr_pkg, consistency=consistency)
            for dep in dependents:
                src_key = dep["source_key"]
                if dep.get("source_label") == schema.PACKAGE and src_key not in visited:
                    visited.add(src_key)
                    new_path = [src_key] + curr_path
                    paths[src_key] = new_path
                    queue.append((src_key, new_path))

        return paths

    # ====================================================================
    # 2. Introducing Version
    # ====================================================================

    def introducing_version(
        self,
        advisory_key: str,
        *,
        consistency: Consistency = "causal",
    ) -> IntroducingVersionResult:
        """Question 2: Which version introduced the vulnerability?

        Checks for an INTRODUCED_IN edge populated by Phase 3. If not
        determined with high precision, falls back to the advisory's stated
        affected range with a clear precise=False flag.
        """
        # Check direct INTRODUCED_IN edge
        rows = self.write_service._run(
            f"MATCH (adv:{schema.ADVISORY} {{key:$key}})-[r:{schema.INTRODUCED_IN}]->"
            f"(v:{schema.VERSION}) "
            f"RETURN v.key AS vkey, r.confidence AS conf, r.evidence AS ev",
            key=advisory_key,
            consistency=consistency,
        )
        if rows:
            r = rows[0]
            return IntroducingVersionResult(
                advisory_key=advisory_key,
                introducing_version_key=r["vkey"],
                confidence=float(r.get("conf") or 0.0),
                evidence=r.get("ev") or "Phase 3 version-introduction signal",
                precise=True,
            )

        # Fallback: check AFFECTS edges
        aff_rows = self.write_service._run(
            f"MATCH (adv:{schema.ADVISORY} {{key:$key}})-[r:{schema.AFFECTS}]->"
            f"(target) "
            f"RETURN target.key AS target_key, r.severity AS sev, "
            f"r.advisory_published_at AS pub_at",
            key=advisory_key,
            consistency=consistency,
        )
        if aff_rows:
            target_key = aff_rows[0]["target_key"]
            if "@" in target_key:
                return IntroducingVersionResult(
                    advisory_key=advisory_key,
                    introducing_version_key=target_key,
                    confidence=0.4,
                    evidence="not precisely determined; defaulted to advisory affected version",
                    precise=False,
                    stated_range=target_key,
                )
            else:
                return IntroducingVersionResult(
                    advisory_key=advisory_key,
                    introducing_version_key=None,
                    confidence=0.2,
                    evidence="not precisely determined; advisory only references package without precise version",
                    precise=False,
                    stated_range=target_key,
                )

        return IntroducingVersionResult(
            advisory_key=advisory_key,
            introducing_version_key=None,
            confidence=0.0,
            evidence="advisory not found in graph",
            precise=False,
        )

    # ====================================================================
    # 3. Live Resolutions (Interval Overlap)
    # ====================================================================

    def live_resolutions(
        self,
        version_key: str,
        window_start: dt.datetime | str,
        window_end: dt.datetime | str | None = None,
        *,
        consistency: Consistency = "causal",
    ) -> list[LiveResolutionResult]:
        """Question 3: Which applications resolved the compromised version
        while it was live?

        Evaluates interval overlap:
          resolved_at <= window_end AND (superseded_at == SENTINEL OR superseded_at >= window_start)
        """
        w_start_str = to_iso(window_start) if isinstance(window_start, dt.datetime) else window_start
        w_end_dt = window_end if isinstance(window_end, dt.datetime) else (from_iso(window_end) if window_end else now_utc())
        w_end_str = to_iso(w_end_dt)

        rows = self.write_service._run(
            f"MATCH (app:{schema.APPLICATION})-[r:{schema.RESOLVED_VERSION_AT}]->"
            f"(v:{schema.VERSION} {{key:$vkey}}) "
            f"RETURN app.key AS app_key, r.resolved_at AS res_at, "
            f"r.superseded_at AS sup_at",
            vkey=version_key,
            consistency=consistency,
        )

        results: list[LiveResolutionResult] = []
        for row in rows:
            res_at = row.get("res_at") or ""
            sup_at = row.get("sup_at")
            if sup_at == schema.OPEN_INTERVAL_SENTINEL:
                sup_at = None

            # Check overlap logic
            # Live condition: resolved_at <= window_end AND (superseded_at is None OR superseded_at >= window_start)
            overlap = False
            if res_at and res_at <= w_end_str:
                if sup_at is None or sup_at >= w_start_str:
                    overlap = True

            if overlap:
                results.append(
                    LiveResolutionResult(
                        application_key=row["app_key"],
                        version_key=version_key,
                        resolved_at=res_at,
                        superseded_at=sup_at,
                        window_start=w_start_str,
                        window_end=w_end_str,
                        was_live_in_window=True,
                    )
                )

        return results

    # ====================================================================
    # 4. Blast Radius (Full Outward Traversal)
    # ====================================================================

    def blast_radius(
        self,
        start_key: str,
        *,
        max_depth: int = 8,
        consistency: Consistency = "causal",
    ) -> BlastRadiusResult:
        """Question 4: What is the complete blast radius?

        Traverses outward across DEPENDS_ON and RESOLVED_VERSION_AT edges
        from a compromised node, returning every reachable Package and
        Application with path depth.
        """
        pkg_key = start_key.split("@")[0] if "@" in start_key else start_key

        visited_nodes: dict[str, BlastRadiusNode] = {}
        queue: list[tuple[str, str, int, list[str]]] = [(pkg_key, schema.PACKAGE, 0, [pkg_key])]

        if "@" in start_key:
            visited_nodes[start_key] = BlastRadiusNode(
                key=start_key, label=schema.VERSION, depth=0, path=[start_key]
            )

        visited_nodes[pkg_key] = BlastRadiusNode(
            key=pkg_key, label=schema.PACKAGE, depth=0, path=[pkg_key]
        )

        while queue:
            curr_key, curr_label, curr_depth, curr_path = queue.pop(0)
            if curr_depth >= max_depth:
                continue

            # 1. Dependents (Package/Application depending on this Package)
            if curr_label == schema.PACKAGE:
                dependents = self.write_service.get_dependents_of(curr_key, consistency=consistency)
                for dep in dependents:
                    src_key = dep["source_key"]
                    src_label = dep.get("source_label") or schema.PACKAGE
                    if src_key not in visited_nodes:
                        new_path = curr_path + [src_key]
                        node = BlastRadiusNode(
                            key=src_key, label=src_label, depth=curr_depth + 1, path=new_path
                        )
                        visited_nodes[src_key] = node
                        queue.append((src_key, src_label, curr_depth + 1, new_path))

            # 2. Check Applications that resolved versions of this package
            res_rows = self.write_service._run(
                f"MATCH (app:{schema.APPLICATION})-[r:{schema.RESOLVED_VERSION_AT}]->"
                f"(v:{schema.VERSION}) "
                f"WHERE r.superseded_at = $open "
                f"RETURN app.key AS app_key, v.key AS vkey",
                open=schema.OPEN_INTERVAL_SENTINEL,
                consistency=consistency,
            )
            for r in res_rows:
                vkey = r["vkey"]
                app_key = r["app_key"]
                app_pkg = vkey.split("@")[0] if "@" in vkey else vkey
                if app_pkg == curr_key and app_key not in visited_nodes:
                    new_path = curr_path + [app_key]
                    node = BlastRadiusNode(
                        key=app_key, label=schema.APPLICATION, depth=curr_depth + 1, path=new_path
                    )
                    visited_nodes[app_key] = node
                    queue.append((app_key, schema.APPLICATION, curr_depth + 1, new_path))

        pkgs = [k for k, n in visited_nodes.items() if n.label == schema.PACKAGE and k != pkg_key]
        apps = [k for k, n in visited_nodes.items() if n.label == schema.APPLICATION]
        max_d = max((n.depth for n in visited_nodes.values()), default=0)

        return BlastRadiusResult(
            source_key=start_key,
            total_reached=len(visited_nodes) - 1,
            max_depth=max_d,
            packages=sorted(pkgs),
            applications=sorted(apps),
            nodes=list(visited_nodes.values()),
        )

    # ====================================================================
    # 5. Nearby Typosquats
    # ====================================================================

    def nearby_typosquats(
        self,
        package_key: str,
        *,
        consistency: Consistency = "causal",
    ) -> list[TyposquatResult]:
        """Question 5: Are there likely typosquat packages nearby?

        Queries direct and reverse POSSIBLE_TYPOSQUAT_OF edges sorted by
        similarity score.
        """
        results: list[TyposquatResult] = []

        # Outgoing: package_key is a possible typosquat of target
        out_rows = self.write_service._run(
            f"MATCH (p:{schema.PACKAGE} {{key:$key}})-[r:{schema.POSSIBLE_TYPOSQUAT_OF}]->"
            f"(target:{schema.PACKAGE}) "
            f"RETURN target.key AS target_key, r.similarity_score AS score, r.method AS method",
            key=package_key,
            consistency=consistency,
        )
        for r in out_rows:
            results.append(
                TyposquatResult(
                    package_key=package_key,
                    popular_target=r["target_key"],
                    similarity_score=float(r.get("score") or 0.0),
                    method=r.get("method") or "levenshtein",
                )
            )

        # Incoming: other packages that are typosquats of package_key
        in_rows = self.write_service._run(
            f"MATCH (cand:{schema.PACKAGE})-[r:{schema.POSSIBLE_TYPOSQUAT_OF}]->"
            f"(target:{schema.PACKAGE} {{key:$key}}) "
            f"RETURN cand.key AS cand_key, r.similarity_score AS score, r.method AS method",
            key=package_key,
            consistency=consistency,
        )
        for r in in_rows:
            results.append(
                TyposquatResult(
                    package_key=r["cand_key"],
                    popular_target=package_key,
                    similarity_score=float(r.get("score") or 0.0),
                    method=r.get("method") or "levenshtein",
                )
            )

        results.sort(key=lambda x: x.similarity_score, reverse=True)
        return results

    # ====================================================================
    # 6. Shared Maintainers & Infrastructure
    # ====================================================================

    def shared_maintainers_and_infra(
        self,
        package_key: str,
        *,
        consistency: Consistency = "causal",
    ) -> list[SharedInfraMaintainerResult]:
        """Question 6: Which other packages share maintainers or infrastructure?

        Combines SAME_MAINTAINER_AS and SHARES_INFRASTRUCTURE_WITH edges
        linked to versions of package_key.
        """
        results: list[SharedInfraMaintainerResult] = []
        seen_links: set[tuple[str, str, str]] = set()

        # Step 1: Find maintainers of this package's versions
        m_rows = self.write_service._run(
            f"MATCH (v:{schema.VERSION})-[r:{schema.PUBLISHED_BY}]->(m:{schema.MAINTAINER}) "
            f"WHERE v.package_key = $pkg_key "
            f"RETURN m.key AS mkey",
            pkg_key=package_key,
            consistency=consistency,
        )
        maintainer_keys = {r["mkey"] for r in m_rows}

        # Step 2: SAME_MAINTAINER_AS links
        for mkey in maintainer_keys:
            # Check other packages published by this maintainer directly
            co_pubs = self.write_service._run(
                f"MATCH (v:{schema.VERSION})-[r:{schema.PUBLISHED_BY}]->(m:{schema.MAINTAINER} {{key:$mkey}}) "
                f"RETURN v.package_key AS other_pkg",
                mkey=mkey,
                consistency=consistency,
            )
            for cp in co_pubs:
                opkg = cp.get("other_pkg")
                if opkg and opkg != package_key:
                    sig = (package_key, opkg, mkey)
                    if sig not in seen_links:
                        seen_links.add(sig)
                        results.append(
                            SharedInfraMaintainerResult(
                                package_key=package_key,
                                connected_package_key=opkg,
                                link_type="same_maintainer",
                                shared_entity_key=mkey,
                                evidence_type="direct_account",
                                confidence=1.0,
                            )
                        )

            # Check SAME_MAINTAINER_AS edges (both directed directions)
            same_m_out = self.write_service._run(
                f"MATCH (m1:{schema.MAINTAINER} {{key:$mkey}})-[r:{schema.SAME_MAINTAINER_AS}]->(m2:{schema.MAINTAINER}) "
                f"RETURN m2.key AS other_m, r.confidence AS conf, r.evidence_type AS ev_type",
                mkey=mkey,
                consistency=consistency,
            )
            same_m_in = self.write_service._run(
                f"MATCH (m1:{schema.MAINTAINER} {{key:$mkey}})<-[r:{schema.SAME_MAINTAINER_AS}]-(m2:{schema.MAINTAINER}) "
                f"RETURN m2.key AS other_m, r.confidence AS conf, r.evidence_type AS ev_type",
                mkey=mkey,
                consistency=consistency,
            )
            same_m = same_m_out + same_m_in
            for sm in same_m:
                other_m = sm["other_m"]
                ev_type = sm.get("ev_type") or "verified_email"
                conf = float(sm.get("conf") or 0.9)
                other_pkgs = self.write_service._run(
                    f"MATCH (v:{schema.VERSION})-[r:{schema.PUBLISHED_BY}]->(m:{schema.MAINTAINER} {{key:$mkey}}) "
                    f"RETURN v.package_key AS other_pkg",
                    mkey=other_m,
                    consistency=consistency,
                )
                for op in other_pkgs:
                    opkg = op.get("other_pkg")
                    if opkg and opkg != package_key:
                        sig = (package_key, opkg, other_m)
                        if sig not in seen_links:
                            seen_links.add(sig)
                            results.append(
                                SharedInfraMaintainerResult(
                                    package_key=package_key,
                                    connected_package_key=opkg,
                                    link_type="same_maintainer",
                                    shared_entity_key=other_m,
                                    evidence_type=ev_type,
                                    confidence=conf,
                                )
                            )

        # Step 3: SHARES_INFRASTRUCTURE_WITH links (both directed directions)
        infra_out = self.write_service._run(
            f"MATCH (p1:{schema.PACKAGE} {{key:$pkg_key}})-[r:{schema.SHARES_INFRASTRUCTURE_WITH}]->(p2:{schema.PACKAGE}) "
            f"RETURN p2.key AS other_pkg, r.evidence_type AS ev_type",
            pkg_key=package_key,
            consistency=consistency,
        )
        infra_in = self.write_service._run(
            f"MATCH (p1:{schema.PACKAGE} {{key:$pkg_key}})<-[r:{schema.SHARES_INFRASTRUCTURE_WITH}]-(p2:{schema.PACKAGE}) "
            f"RETURN p2.key AS other_pkg, r.evidence_type AS ev_type",
            pkg_key=package_key,
            consistency=consistency,
        )
        infra_rows = infra_out + infra_in
        for ir in infra_rows:
            opkg = ir["other_pkg"]
            ev_type = ir.get("ev_type") or "signing_key"
            sig = (package_key, opkg, "infra")
            if sig not in seen_links:
                seen_links.add(sig)
                results.append(
                    SharedInfraMaintainerResult(
                        package_key=package_key,
                        connected_package_key=opkg,
                        link_type="shared_infrastructure",
                        shared_entity_key=ev_type,
                        evidence_type=ev_type,
                        confidence=0.85,
                    )
                )

        return results

    # ====================================================================
    # Predictive Impact / Cascade
    # ====================================================================

    def predict_propagation(
        self,
        flagged_version_key: str,
        *,
        write_to_graph: bool = True,
        consistency: Consistency = "causal",
    ) -> list[PredictedPropagationResult]:
        """Propagation forecasting: identifies consumers whose declared
        DEPENDS_ON range would include flagged_version_key, but who currently
        have NO active RESOLVED_VERSION_AT edge pointing to it.
        """
        pkg_key = flagged_version_key.split("@")[0] if "@" in flagged_version_key else flagged_version_key
        ver_str = flagged_version_key.split("@")[1] if "@" in flagged_version_key else ""

        results: list[PredictedPropagationResult] = []

        # Find all DEPENDS_ON edges to pkg_key
        dependents = self.write_service.get_dependents_of(pkg_key, consistency=consistency)

        # Check existing active resolutions
        res_rows = self.write_service._run(
            f"MATCH (app:{schema.APPLICATION})-[r:{schema.RESOLVED_VERSION_AT}]->"
            f"(v:{schema.VERSION} {{key:$vkey}}) "
            f"WHERE r.superseded_at = $open "
            f"RETURN app.key AS app_key",
            vkey=flagged_version_key,
            open=schema.OPEN_INTERVAL_SENTINEL,
            consistency=consistency,
        )
        already_resolved_apps = {r["app_key"] for r in res_rows}

        for dep in dependents:
            src_key = dep["source_key"]
            src_label = dep.get("source_label") or schema.PACKAGE
            dep_range = dep.get("range") or "*"

            # If consumer is an application and has NOT resolved this version yet
            if src_label == schema.APPLICATION and src_key in already_resolved_apps:
                continue

            confidence = 0.8 if (dep_range in ("*", "^", "~") or ver_str in dep_range) else 0.65
            pred = PredictedPropagationResult(
                consumer_key=src_key,
                consumer_label=src_label,
                declared_range=dep_range,
                flagged_version_key=flagged_version_key,
                confidence=confidence,
                basis="propagation",
                type="predicted",
            )
            results.append(pred)

            if write_to_graph:
                t_now = now_utc()
                try:
                    self.write_service.write_predicted_exposure(
                        src_label,
                        src_key,
                        schema.VERSION,
                        flagged_version_key,
                        predicted_at=t_now,
                        confidence=confidence,
                        basis="propagation",
                        first_observed_at=t_now,
                        event_time=t_now,
                    )
                except Exception:
                    log.exception("failed to persist PREDICTED_EXPOSURE (propagation)")

        return results

    def predict_early_warning(
        self,
        package_key: str,
        *,
        write_to_graph: bool = True,
        consistency: Consistency = "causal",
    ) -> EarlyWarningRiskResult:
        """Early-warning risk scoring:
        Combines behavioral signals (Socket scores, install scripts) with
        graph-structural features (links to flagged packages, maintainer changes).
        Produces a scored composite risk value.
        """
        contributing_factors: list[dict[str, Any]] = []
        score_components: list[float] = []

        # 1. Package properties & Socket score
        pkg_rows = self.write_service._run(
            f"MATCH (n:{schema.PACKAGE} {{key:$key}}) RETURN n.socket_score AS socket_score",
            key=package_key,
            consistency=consistency,
        )
        if pkg_rows and pkg_rows[0].get("socket_score") is not None:
            score_val = float(pkg_rows[0]["socket_score"])
            score_components.append(score_val)
            contributing_factors.append(
                {"signal": "socket_behavioral_score", "score": score_val, "detail": f"Socket risk score {score_val:.2f}"}
            )

        # 2. Check for nearby typosquats
        typos = self.nearby_typosquats(package_key, consistency=consistency)
        if typos:
            max_sim = max(t.similarity_score for t in typos)
            sim_score = max_sim * 0.7
            score_components.append(sim_score)
            contributing_factors.append(
                {"signal": "typosquat_proximity", "score": sim_score, "detail": f"Similar to popular {typos[0].popular_target} ({max_sim:.2f})"}
            )

        # 3. Check shared maintainers / infrastructure with known vulnerable packages
        shared = self.shared_maintainers_and_infra(package_key, consistency=consistency)
        for sh in shared:
            advs = self.write_service.get_advisories_for(schema.PACKAGE, sh.connected_package_key, consistency=consistency)
            if advs:
                score_components.append(0.75)
                contributing_factors.append(
                    {
                        "signal": "shared_entity_with_compromised_package",
                        "score": 0.75,
                        "detail": f"Shares {sh.link_type} ({sh.evidence_type}) with flagged package {sh.connected_package_key}",
                    }
                )
                break

        # Compute composite score
        if score_components:
            final_score = min(1.0, sum(score_components) / len(score_components) * (1.0 + 0.1 * (len(score_components) - 1)))
            confidence = min(0.95, 0.4 + 0.2 * len(score_components))
        else:
            final_score = 0.05
            confidence = 0.3
            contributing_factors.append({"signal": "baseline", "score": 0.05, "detail": "No anomalous signals detected"})

        result = EarlyWarningRiskResult(
            package_key=package_key,
            risk_score=round(final_score, 3),
            confidence=round(confidence, 2),
            contributing_factors=contributing_factors,
            basis="early_warning",
            type="predicted",
        )

        if write_to_graph and final_score >= 0.4:
            t_now = now_utc()
            try:
                self.write_service.write_predicted_exposure(
                    schema.PACKAGE,
                    package_key,
                    schema.PACKAGE,
                    package_key,
                    predicted_at=t_now,
                    confidence=confidence,
                    basis="early_warning",
                    first_observed_at=t_now,
                    event_time=t_now,
                )
            except Exception:
                log.exception("failed to persist PREDICTED_EXPOSURE (early_warning)")

        return result

    # ====================================================================
    # Chained-Vulnerability Detection Stub
    # ====================================================================

    def detect_chain(
        self,
        package_a: str,
        package_b: str,
        *,
        consistency: Consistency = "causal",
    ) -> ChainRisk | None:
        """Chained-Vulnerability Detection (Initial Stub).

        Detects known interaction chains between two packages along a
        DEPENDS_ON path (e.g. prototype pollution / parser mutation in
        package_a propagating unvalidated data into an unsafe sink/eval in
        package_b).
        """
        pkg_a_key = package_a.split("@")[0] if "@" in package_a else package_a
        pkg_b_key = package_b.split("@")[0] if "@" in package_b else package_b

        pollution_patterns = ("lodash", "deepmerge", "merge", "extend", "dot-prop", "minimist")
        sink_patterns = ("ejs", "pug", "handlebars", "serialize-javascript", "eval", "vm2", "execa", "shelljs")

        is_pollution = any(p in pkg_a_key.lower() for p in pollution_patterns)
        is_sink = any(s in pkg_b_key.lower() for s in sink_patterns)

        if not (is_pollution and is_sink):
            return None

        # Check if there is a dependency path between package_a and package_b in graph
        upstream = self._find_upstream_package_paths(pkg_b_key, consistency=consistency)
        path = upstream.get(pkg_a_key, [pkg_a_key, pkg_b_key])

        return ChainRisk(
            package_a=pkg_a_key,
            package_b=pkg_b_key,
            risk_type="prototype_pollution_to_code_execution",
            description=(
                f"Chained interaction: {pkg_a_key} contains object mutation / property injection "
                f"that feeds into dynamic evaluation sink in {pkg_b_key} via dependency path: "
                f"{' -> '.join(path)}"
            ),
            confidence=0.75,
            path=path,
            mitigation=(
                f"Freeze Object.prototype, sanitize keys before passing to {pkg_a_key}, "
                f"and disable unsafe evaluation in {pkg_b_key}."
            ),
        )
