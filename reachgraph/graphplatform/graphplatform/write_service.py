"""Graph Write Service -- the only module in this codebase allowed to open a
HydraDB connection. Every future phase reads and writes the graph through
this module's public interface, never through a raw driver call elsewhere.

HydraDB's real query-engine dialect (verified by hand against the running
local instance -- neo4j://127.0.0.1:7687, HTTP 127.0.0.1:8443, admin
127.0.0.1:9090 -- none of this is in any published doc, since this local
Bolt/Cypher graph engine is a different product surface than HydraDB's
hosted v2 RAG API used elsewhere in this repo's `hydradb.go`):

  - The routable database/graph name is "default", not "neo4j".
  - Explicit transactions are rejected ("explicit transactions are not
    supported; use auto-commit RUN queries") -- every statement in this
    module is a single auto-commit `session.run`, never
    `session.begin_transaction`/`execute_read`/`execute_write`.
  - RETURN only supports `<var>.<property>` projections or `count(*)` --
    you cannot `RETURN n` or `RETURN r`. Every read here projects explicit
    property lists.
  - A bare, unconstrained `MATCH (n)` is rejected ("node-only MATCH
    requires an id, label, or property predicate") -- matches the task's
    documented limitation. Every MATCH here carries a label and/or a
    property predicate.
  - WHERE only supports "boolean combinations of property comparisons" --
    no IS NULL / IS NOT NULL. Nullable fields (RESOLVED_VERSION_AT's
    superseded_at) are stored with an explicit sentinel
    (schema.OPEN_INTERVAL_SENTINEL) instead, translated to/from Python
    `None` at this module's boundary -- see resolve_version/
    get_current_resolutions below.
  - The literal property name `id` is reserved on both nodes and
    relationships: any pattern predicate `{id: ...}` requires a
    non-negative integer, and is the *only* thing a MERGE/CREATE pattern
    for a fresh graph element may key on -- not a label, not any other
    property. Concretely:
      * The only way to create a node at all is the special "vertex
        upsert" fast path: `UNWIND $rows AS row MERGE (n {id: row.id})
        SET n:Label, n.prop = row.prop, ...` -- no label inside the MERGE
        pattern (apply it via SET), no ON CREATE/ON MATCH, no RETURN
        chained on, and it only works wrapped in UNWIND (even for one
        row) -- a bare `MERGE (n:Label {key: $key})` is rejected outright
        ("only one-hop edge patterns are executable in Query engine
        MERGE"), and so is a bare `CREATE (n:Label {...})` ("only one-hop
        edge patterns are executable in Query engine CREATE").
      * A relationship MERGE requires both endpoints already matched by
        their own integer `id` with an explicit label each: `UNWIND $rows
        AS row MATCH (a:LabelA {id: row.a_id}), (b:LabelB {id: row.b_id})
        MERGE (a)-[r:TYPE {id: row.rid}]->(b)` -- no SET chained on that
        statement either.
    Since every natural key in this schema is a string, every node and
    relationship gets a deterministic integer `id` derived from its
    natural key via schema.stable_id(), used *only* as the merge handle.
    The real natural key is mirrored into a plain string property (`key`
    on nodes) immediately after, in a second auto-commit statement, and
    every read in this module goes through that mirror -- this is the
    same shape as the relationship `id`/`rel_id` mirroring the task's
    known-limitations section already calls out, just discovered here to
    apply to nodes too, and to relationship *endpoints* used inside a
    MERGE pattern, not only to reading `r.id` back.
  - Read consistency ("causal" vs "strong") is a real, strictly-validated
    field -- confirmed directly against the HTTP JSON API
    (`POST /v1/graphs/{graph}/query`), where sending an unrecognized value
    for it errors with `unknown variant `X`, expected `causal` or
    `strong``. The Bolt driver has no first-class "consistency" concept,
    so this module threads it through as Bolt transaction metadata
    (`neo4j.Query(cypher, metadata={"consistency": mode})`), the
    protocol's standard mechanism for attaching a per-query directive to
    an auto-commit RUN. HydraDB accepts this without error for both
    values, but -- unlike the HTTP confirmation above -- this local,
    single-node, no-replication-lag dev instance gives no observable way
    to confirm the Bolt path actually changes read freshness. Flagged
    here rather than smoothed over, the same way this repo's `hydradb.go`
    flags its own unconfirmed corners.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import neo4j

from . import schema
from .schema import Consistency, from_iso, stable_id, to_iso

log = logging.getLogger("graphplatform.write_service")


class GraphWriteService:
    """The only object in this codebase permitted to hold a HydraDB driver."""

    def __init__(
        self,
        uri: str,
        token: str,
        database: str = "default",
        *,
        max_connection_pool_size: int = 50,
    ) -> None:
        # One driver for the process lifetime: the neo4j driver already
        # pools and multiplexes connections internally, so a long-running
        # service holds a single driver and opens cheap, short-lived
        # sessions per call -- not a fresh TCP connection per call.
        self._driver = neo4j.GraphDatabase.driver(
            uri,
            auth=neo4j.bearer_auth(token),
            encrypted=False,
            max_connection_pool_size=max_connection_pool_size,
        )
        self._database = database

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "GraphWriteService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    # -- low-level plumbing --------------------------------------------

    def _run(
        self,
        cypher: str,
        *,
        consistency: Consistency = "causal",
        write: bool = False,
        **params: Any,
    ) -> list[dict[str, Any]]:
        if consistency not in ("causal", "strong"):
            raise ValueError(f"invalid consistency: {consistency!r} (must be 'causal' or 'strong')")
        query = neo4j.Query(cypher, metadata={"consistency": consistency})
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run(query, params)
                records = [dict(r) for r in result]
        except Exception:
            log.exception(
                "hydradb query failed",
                extra={"cypher": cypher, "write": write, "consistency": consistency},
            )
            raise
        log.debug(
            "hydradb query ok",
            extra={"cypher": cypher, "write": write, "consistency": consistency, "rows": len(records)},
        )
        return records

    # -- generic node upsert/read ---------------------------------------

    def _read_node_first_observed_at(self, label: str, key: str) -> str | None:
        rows = self._run(
            f"MATCH (n:{label} {{key: $key}}) RETURN n.first_observed_at AS foa",
            key=key,
            consistency="strong",
        )
        return rows[0]["foa"] if rows else None

    def _upsert_node(
        self,
        label: str,
        key: str,
        properties: dict[str, Any],
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        """Idempotent node upsert. Returns True if this call created the node."""
        if label not in schema.NODE_LABELS:
            raise ValueError(f"unknown node label: {label!r}")

        existing_foa = self._read_node_first_observed_at(label, key)
        created = existing_foa is None
        foa = existing_foa if existing_foa is not None else to_iso(first_observed_at)

        row: dict[str, Any] = {
            "id": stable_id(label, key),
            "key": key,
            "first_observed_at": foa,
            "event_time": to_iso(event_time),
        }
        row.update(properties)
        set_clauses = ", ".join(f"n.{prop} = row.{prop}" for prop in row if prop != "id")
        self._run(
            f"UNWIND $rows AS row MERGE (n {{id: row.id}}) SET n:{label}, {set_clauses}",
            rows=[row],
            write=True,
        )
        log.info(
            "graph write: node upserted",
            extra={"label": label, "key": key, "event_time": row["event_time"], "newly_created": created},
        )
        return created

    def _read_node(
        self, label: str, key: str, fields: list[str], consistency: Consistency = "causal"
    ) -> dict[str, Any] | None:
        if label not in schema.NODE_LABELS:
            raise ValueError(f"unknown node label: {label!r}")
        projection = ", ".join(f"n.{f} AS {f}" for f in fields)
        rows = self._run(
            f"MATCH (n:{label} {{key: $key}}) RETURN {projection}",
            key=key,
            consistency=consistency,
        )
        return rows[0] if rows else None

    # -- generic relationship upsert/read --------------------------------

    def _read_rel_first_observed_at(
        self, rel_type: str, a_label: str, a_key: str, b_label: str, b_key: str, rid: int
    ) -> str | None:
        rows = self._run(
            f"MATCH (a:{a_label} {{key:$a_key}})-[r:{rel_type} {{id:$rid}}]->(b:{b_label} {{key:$b_key}}) "
            f"RETURN r.first_observed_at AS foa",
            a_key=a_key,
            b_key=b_key,
            rid=rid,
            consistency="strong",
        )
        return rows[0]["foa"] if rows else None

    def _upsert_relationship(
        self,
        rel_type: str,
        a_label: str,
        a_key: str,
        b_label: str,
        b_key: str,
        properties: dict[str, Any],
        first_observed_at: datetime,
        event_time: datetime,
        id_extra: tuple[str, ...] = (),
    ) -> bool:
        """Idempotent relationship upsert. Returns True if this call created the edge.

        id_extra lets interval-style relationships (RESOLVED_VERSION_AT)
        fold extra fields (resolved_at) into the merge key, so multiple
        historical edges can coexist between the same two nodes.
        """
        if rel_type not in schema.REL_TYPES:
            raise ValueError(f"unknown relationship type: {rel_type!r}")
        if a_label not in schema.NODE_LABELS or b_label not in schema.NODE_LABELS:
            raise ValueError(f"unknown node label in relationship endpoints: {a_label!r}, {b_label!r}")

        a_id = stable_id(a_label, a_key)
        b_id = stable_id(b_label, b_key)
        rid = stable_id(rel_type, a_key, b_key, *id_extra)

        existing_foa = self._read_rel_first_observed_at(rel_type, a_label, a_key, b_label, b_key, rid)
        created = existing_foa is None
        foa = existing_foa if existing_foa is not None else to_iso(first_observed_at)

        # Step 1: merge the edge itself, endpoints matched by their stable
        # integer id (both already upserted by the time a relationship is
        # written -- see the individual write_* methods below).
        self._run(
            f"UNWIND $rows AS row "
            f"MATCH (a:{a_label} {{id: row.a_id}}), (b:{b_label} {{id: row.b_id}}) "
            f"MERGE (a)-[r:{rel_type} {{id: row.rid}}]->(b)",
            rows=[{"a_id": a_id, "b_id": b_id, "rid": rid}],
            write=True,
        )

        # Step 2: set the rel_id mirror (never read r.id back -- see module
        # docstring) plus every real property, filtered to precisely this
        # edge so sibling historical intervals of the same type are untouched.
        params: dict[str, Any] = {
            "a_key": a_key,
            "b_key": b_key,
            "rid": rid,
            "first_observed_at": foa,
            "event_time": to_iso(event_time),
        }
        params.update(properties)
        set_clauses = ", ".join(
            f"r.{prop} = ${prop}" for prop in ("rel_id", "first_observed_at", "event_time", *properties)
        )
        self._run(
            f"MATCH (a:{a_label} {{key: $a_key}})-[r:{rel_type} {{id: $rid}}]->(b:{b_label} {{key: $b_key}}) "
            f"SET {set_clauses}",
            rel_id=rid,
            write=True,
            **params,
        )
        log.info(
            "graph write: relationship upserted",
            extra={
                "rel_type": rel_type,
                "a_key": a_key,
                "b_key": b_key,
                "event_time": params["event_time"],
                "newly_created": created,
            },
        )
        return created

    def _read_relationships(
        self,
        rel_type: str,
        a_label: str,
        a_pred: str,
        a_params: dict[str, Any],
        b_label: str,
        b_pred: str,
        b_params: dict[str, Any],
        fields: list[str],
        consistency: Consistency = "causal",
    ) -> list[dict[str, Any]]:
        projection = ", ".join(f"r.{f} AS {f}" for f in fields)
        params = {**a_params, **b_params}
        return self._run(
            f"MATCH (a:{a_label} {a_pred})-[r:{rel_type}]->(b:{b_label} {b_pred}) RETURN {projection}",
            consistency=consistency,
            **params,
        )

    # ====================================================================
    # Node upserts
    # ====================================================================

    def upsert_package(
        self, key: str, ecosystem: str, name: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_node(
            schema.PACKAGE, key, {"ecosystem": ecosystem, "name": name}, first_observed_at, event_time
        )

    def upsert_version(
        self,
        key: str,
        package_key: str,
        version: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        return self._upsert_node(
            schema.VERSION,
            key,
            {"package_key": package_key, "version": version},
            first_observed_at,
            event_time,
        )

    def upsert_maintainer(
        self, key: str, platform: str, identity: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_node(
            schema.MAINTAINER, key, {"platform": platform, "identity": identity}, first_observed_at, event_time
        )

    def upsert_infrastructure(
        self, key: str, kind: str, identifier: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_node(
            schema.INFRASTRUCTURE,
            key,
            {"kind": kind, "identifier": identifier},
            first_observed_at,
            event_time,
        )

    def upsert_application(
        self,
        key: str,
        org: str,
        repo: str,
        subpath: str = "",
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        return self._upsert_node(
            schema.APPLICATION,
            key,
            {"org": org, "repo": repo, "subpath": subpath},
            first_observed_at,
            event_time,
        )

    def upsert_advisory(
        self,
        key: str,
        source: str,
        advisory_id: str,
        summary: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        return self._upsert_node(
            schema.ADVISORY,
            key,
            {"source": source, "advisory_id": advisory_id, "summary": summary},
            first_observed_at,
            event_time,
        )

    # ====================================================================
    # Node reads
    # ====================================================================

    def get_package(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.PACKAGE, key, ["key", "ecosystem", "name", "first_observed_at", "event_time"], consistency
        )

    def get_version(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.VERSION,
            key,
            ["key", "package_key", "version", "first_observed_at", "event_time"],
            consistency,
        )

    def get_maintainer(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.MAINTAINER,
            key,
            ["key", "platform", "identity", "first_observed_at", "event_time"],
            consistency,
        )

    def get_infrastructure(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.INFRASTRUCTURE,
            key,
            ["key", "kind", "identifier", "first_observed_at", "event_time"],
            consistency,
        )

    def get_application(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.APPLICATION,
            key,
            ["key", "org", "repo", "subpath", "first_observed_at", "event_time"],
            consistency,
        )

    def get_advisory(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.ADVISORY,
            key,
            ["key", "source", "advisory_id", "summary", "first_observed_at", "event_time"],
            consistency,
        )

    # ====================================================================
    # Relationship writes
    # ====================================================================

    def write_depends_on(
        self,
        source_label: str,
        source_key: str,
        target_key: str,
        range: str,
        manager: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        if source_label not in (schema.PACKAGE, schema.APPLICATION):
            raise ValueError("DEPENDS_ON source must be Package or Application")
        return self._upsert_relationship(
            schema.DEPENDS_ON,
            source_label,
            source_key,
            schema.PACKAGE,
            target_key,
            {"range": range, "manager": manager},
            first_observed_at,
            event_time,
        )

    def resolve_version(
        self,
        app_key: str,
        version_key: str,
        resolved_at: datetime,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        """Open a new RESOLVED_VERSION_AT interval. Preserves history: does
        not touch any prior interval for this (app, version) pair -- call
        supersede_version on the old one first if it should be closed out.
        """
        resolved_iso = to_iso(resolved_at)
        return self._upsert_relationship(
            schema.RESOLVED_VERSION_AT,
            schema.APPLICATION,
            app_key,
            schema.VERSION,
            version_key,
            {"resolved_at": resolved_iso, "superseded_at": schema.OPEN_INTERVAL_SENTINEL},
            first_observed_at,
            event_time,
            id_extra=(resolved_iso,),
        )

    def supersede_version(
        self,
        app_key: str,
        version_key: str,
        resolved_at: datetime,
        superseded_at: datetime,
    ) -> None:
        """Close out one specific RESOLVED_VERSION_AT interval.

        Takes resolved_at (identifying exactly which interval to close)
        rather than inferring "the current one" -- see module docstring on
        why HydraDB's WHERE clause can't express "superseded_at IS NULL".
        The caller finds the interval to close via get_current_resolutions.
        """
        resolved_iso = to_iso(resolved_at)
        rid = stable_id(schema.RESOLVED_VERSION_AT, app_key, version_key, resolved_iso)
        self._run(
            f"MATCH (a:{schema.APPLICATION} {{key:$a_key}})"
            f"-[r:{schema.RESOLVED_VERSION_AT} {{id:$rid}}]->"
            f"(b:{schema.VERSION} {{key:$b_key}}) "
            f"SET r.superseded_at = $superseded_at",
            a_key=app_key,
            b_key=version_key,
            rid=rid,
            superseded_at=to_iso(superseded_at),
            write=True,
        )
        log.info(
            "graph write: resolution superseded",
            extra={"app_key": app_key, "version_key": version_key, "resolved_at": resolved_iso},
        )

    def write_published_by(
        self, version_key: str, maintainer_key: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_relationship(
            schema.PUBLISHED_BY,
            schema.VERSION,
            version_key,
            schema.MAINTAINER,
            maintainer_key,
            {},
            first_observed_at,
            event_time,
        )

    def write_affects(
        self,
        advisory_key: str,
        target_label: str,
        target_key: str,
        advisory_published_at: datetime,
        severity: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        if target_label not in (schema.VERSION, schema.PACKAGE):
            raise ValueError("AFFECTS target must be Version or Package")
        return self._upsert_relationship(
            schema.AFFECTS,
            schema.ADVISORY,
            advisory_key,
            target_label,
            target_key,
            {"advisory_published_at": to_iso(advisory_published_at), "severity": severity},
            first_observed_at,
            event_time,
        )

    def write_introduced_in(
        self,
        advisory_key: str,
        version_key: str,
        confidence: float,
        evidence: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        return self._upsert_relationship(
            schema.INTRODUCED_IN,
            schema.ADVISORY,
            advisory_key,
            schema.VERSION,
            version_key,
            {"confidence": confidence, "evidence": evidence},
            first_observed_at,
            event_time,
        )

    def write_same_maintainer_as(
        self,
        maintainer_a_key: str,
        maintainer_b_key: str,
        confidence: float,
        evidence_type: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        if evidence_type not in schema.EVIDENCE_TYPES_MAINTAINER:
            raise ValueError(f"invalid evidence_type: {evidence_type!r}")
        return self._upsert_relationship(
            schema.SAME_MAINTAINER_AS,
            schema.MAINTAINER,
            maintainer_a_key,
            schema.MAINTAINER,
            maintainer_b_key,
            {"confidence": confidence, "evidence_type": evidence_type},
            first_observed_at,
            event_time,
        )

    def write_shares_infrastructure_with(
        self,
        a_label: str,
        a_key: str,
        b_label: str,
        b_key: str,
        evidence_type: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        """Directed in storage (Cypher relationships are always directed),
        conceptually symmetric. Callers/readers should treat it as
        undirected -- query both directions, or canonicalize endpoint order
        before writing, if exact-once-per-pair matters to a later phase.
        """
        if a_label not in (schema.VERSION, schema.PACKAGE) or b_label not in (schema.VERSION, schema.PACKAGE):
            raise ValueError("SHARES_INFRASTRUCTURE_WITH endpoints must be Version or Package")
        if evidence_type not in schema.EVIDENCE_TYPES_INFRASTRUCTURE:
            raise ValueError(f"invalid evidence_type: {evidence_type!r}")
        return self._upsert_relationship(
            schema.SHARES_INFRASTRUCTURE_WITH,
            a_label,
            a_key,
            b_label,
            b_key,
            {"evidence_type": evidence_type},
            first_observed_at,
            event_time,
        )

    def write_typosquat_of(
        self,
        source_key: str,
        target_key: str,
        similarity_score: float,
        method: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        return self._upsert_relationship(
            schema.POSSIBLE_TYPOSQUAT_OF,
            schema.PACKAGE,
            source_key,
            schema.PACKAGE,
            target_key,
            {"similarity_score": similarity_score, "method": method},
            first_observed_at,
            event_time,
        )

    def write_predicted_exposure(
        self,
        source_label: str,
        source_key: str,
        target_label: str,
        target_key: str,
        predicted_at: datetime,
        confidence: float,
        basis: str,
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> bool:
        """Structurally distinct from AFFECTS/RESOLVED_VERSION_AT by
        relationship TYPE alone, so no reader can mistake a prediction for
        a confirmed fact by forgetting to check a flag.
        """
        if basis not in schema.PREDICTION_BASES:
            raise ValueError(f"invalid basis: {basis!r} (must be one of {sorted(schema.PREDICTION_BASES)})")
        if source_label not in (schema.APPLICATION, schema.PACKAGE):
            raise ValueError("PREDICTED_EXPOSURE source must be Application or Package")
        if target_label not in (schema.VERSION, schema.PACKAGE):
            raise ValueError("PREDICTED_EXPOSURE target must be Version or Package")
        return self._upsert_relationship(
            schema.PREDICTED_EXPOSURE,
            source_label,
            source_key,
            target_label,
            target_key,
            {"predicted_at": to_iso(predicted_at), "confidence": confidence, "basis": basis},
            first_observed_at,
            event_time,
        )

    # ====================================================================
    # Relationship / traversal reads
    # ====================================================================

    def get_dependencies_of(
        self, source_label: str, source_key: str, consistency: Consistency = "causal"
    ) -> list[dict[str, Any]]:
        if source_label not in (schema.PACKAGE, schema.APPLICATION):
            raise ValueError("DEPENDS_ON source must be Package or Application")
        return self._run(
            f"MATCH (a:{source_label} {{key:$key}})-[r:{schema.DEPENDS_ON}]->(b:{schema.PACKAGE}) "
            f"RETURN b.key AS package_key, r.range AS range, r.manager AS manager",
            key=source_key,
            consistency=consistency,
        )

    def get_dependents_of(self, package_key: str, consistency: Consistency = "causal") -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for source_label in (schema.PACKAGE, schema.APPLICATION):
            rows = self._run(
                f"MATCH (a:{source_label})-[r:{schema.DEPENDS_ON}]->(b:{schema.PACKAGE} {{key:$key}}) "
                f"RETURN a.key AS source_key, r.range AS range, r.manager AS manager",
                key=package_key,
                consistency=consistency,
            )
            for row in rows:
                row["source_label"] = source_label
            results.extend(rows)
        return results

    def get_current_resolutions(self, app_key: str, consistency: Consistency = "causal") -> list[dict[str, Any]]:
        rows = self._run(
            f"MATCH (a:{schema.APPLICATION} {{key:$key}})-[r:{schema.RESOLVED_VERSION_AT}]->(b:{schema.VERSION}) "
            f"WHERE r.superseded_at = $open "
            f"RETURN b.key AS version_key, r.resolved_at AS resolved_at",
            key=app_key,
            open=schema.OPEN_INTERVAL_SENTINEL,
            consistency=consistency,
        )
        return rows

    def get_maintainers_of(self, version_key: str, consistency: Consistency = "causal") -> list[dict[str, Any]]:
        return self._run(
            f"MATCH (v:{schema.VERSION} {{key:$key}})-[r:{schema.PUBLISHED_BY}]->(m:{schema.MAINTAINER}) "
            f"RETURN m.key AS maintainer_key, r.event_time AS event_time",
            key=version_key,
            consistency=consistency,
        )

    def get_advisories_for(
        self, target_label: str, target_key: str, consistency: Consistency = "causal"
    ) -> list[dict[str, Any]]:
        if target_label not in (schema.VERSION, schema.PACKAGE):
            raise ValueError("AFFECTS target must be Version or Package")
        return self._run(
            f"MATCH (adv:{schema.ADVISORY})-[r:{schema.AFFECTS}]->(t:{target_label} {{key:$key}}) "
            f"RETURN adv.key AS advisory_key, r.severity AS severity, "
            f"r.advisory_published_at AS advisory_published_at",
            key=target_key,
            consistency=consistency,
        )
