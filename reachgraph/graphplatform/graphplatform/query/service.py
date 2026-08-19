"""Query & Reasoning Service.

The authoritative analytical interface for blast-radius reasoning over
HydraDB: transitive exposure, live resolution windows, and the core
outward blast-radius traversal.
"""

from __future__ import annotations

import datetime as dt

from .. import schema
from ..schema import Consistency, from_iso, now_utc, to_iso
from ..write_service import GraphWriteService
from .models import BlastRadiusNode, BlastRadiusResult, LiveResolutionResult, TransitiveExposureResult


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

