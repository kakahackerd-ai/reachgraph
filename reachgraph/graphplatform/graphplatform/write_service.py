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
  - **No write statement may be followed by MATCH/RETURN/WITH, full stop**
    -- verified by hand while adding phase-3 enrichment writes:
    `MATCH (n:Package {key:$key}) SET n.prop = $val RETURN n.key` is
    rejected with "mutation queries cannot continue with MATCH, RETURN, or
    WITH after writes". This generalizes the node/relationship-upsert
    split already described above (MERGE, then a separate SET, never one
    statement) to *any* write, not just those two shapes -- a write is
    always its own auto-commit statement with nothing chained after it.
    `_annotate_node` below relies on this: it's a MATCH-scoped SET with no
    RETURN, so it silently touches zero rows (a normal Cypher no-op, not
    an error) if the key doesn't exist -- there's no cheap way to ask this
    engine "did that SET find a node" in the same round trip.
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

    def _annotate_node(self, label: str, key: str, properties: dict[str, Any]) -> None:
        """Set additional properties on an existing node without touching
        its core identity fields or timestamp discipline -- for enrichment
        writes (phase 3: GUAC, Socket, ...) that annotate data an
        upsert_* already created. Never creates a node: see the module
        docstring on why this can't RETURN whether a match happened --
        annotating a key that doesn't exist yet is a silent no-op, by
        design (enrichment should never race ahead of ingestion).
        """
        if label not in schema.NODE_LABELS:
            raise ValueError(f"unknown node label: {label!r}")
        if not properties:
            return
        set_clauses = ", ".join(f"n.{prop} = ${prop}" for prop in properties)
        self._run(
            f"MATCH (n:{label} {{key: $key}}) SET {set_clauses}",
            key=key,
            write=True,
            **properties,
        )
        log.info("graph write: node annotated", extra={"label": label, "key": key, "properties": list(properties)})

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

    def upsert_file(
        self, key: str, path: str, application_key: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_node(
            schema.FILE, key, {"path": path, "application_key": application_key}, first_observed_at, event_time
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

    def get_application(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.APPLICATION,
            key,
            ["key", "org", "repo", "subpath", "first_observed_at", "event_time"],
            consistency,
        )

    def get_file(self, key: str, consistency: Consistency = "causal") -> dict[str, Any] | None:
        return self._read_node(
            schema.FILE,
            key,
            ["key", "path", "application_key", "first_observed_at", "event_time"],
            consistency,
        )

    # ====================================================================
    # Node annotation (enrichment -- see _annotate_node)
    # ====================================================================

    def annotate_package(self, key: str, **properties: Any) -> None:
        self._annotate_node(schema.PACKAGE, key, properties)

    def annotate_version(self, key: str, **properties: Any) -> None:
        self._annotate_node(schema.VERSION, key, properties)

    def annotate_maintainer(self, key: str, **properties: Any) -> None:
        self._annotate_node(schema.MAINTAINER, key, properties)

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

    def write_contains(
        self, application_key: str, file_key: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_relationship(
            schema.CONTAINS,
            schema.APPLICATION,
            application_key,
            schema.FILE,
            file_key,
            {},
            first_observed_at,
            event_time,
        )

    def write_imports(
        self, file_key: str, package_key: str, *, first_observed_at: datetime, event_time: datetime
    ) -> bool:
        return self._upsert_relationship(
            schema.IMPORTS,
            schema.FILE,
            file_key,
            schema.PACKAGE,
            package_key,
            {},
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

    def get_importers_of(self, package_key: str, consistency: Consistency = "causal") -> list[dict[str, Any]]:
        return self._run(
            f"MATCH (f:{schema.FILE})-[r:{schema.IMPORTS}]->(p:{schema.PACKAGE} {{key:$key}}) "
            f"RETURN f.key AS file_key, f.path AS path, f.application_key AS application_key",
            key=package_key,
            consistency=consistency,
        )
