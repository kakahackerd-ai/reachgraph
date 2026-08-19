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

# The local single-node HydraDB backend's object store is LocalFileSystem,
# which doesn't implement PutMode::Update -- so its GC (which needs that
# mode to rewrite manifest/compaction objects) fails every cycle, forever,
# on this backend specifically (see graphplatform/README.md's "Known
# limitations"). Once enough cumulative writes have landed since the last
# store wipe, reads and writes alike start failing with exactly this
# message. It is not this codebase's bug and there is no query-level fix --
# the store needs a wipe+restart -- so we detect the signature and raise a
# clearly-labeled exception instead of letting a raw CypherSyntaxError with
# an opaque SlateDB message bubble up to an API caller.
_WRITE_CEILING_SIGNATURE = "historical graph epochs are not SlateDB snapshots"


class HydraDBWriteCeilingExceeded(RuntimeError):
    """Raised when the local HydraDB backend's permanent write-volume
    ceiling has been hit. See graphplatform/README.md's "Known
    limitations" -- the store needs `podman stop/rm -rf store,cache/start`.
    Not recoverable by retrying from inside this process.
    """


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
        except Exception as exc:
            if _WRITE_CEILING_SIGNATURE in str(exc):
                log.error(
                    "hydradb local write-volume ceiling exceeded -- store needs a wipe+restart, see README",
                    extra={"write": write, "consistency": consistency},
                )
                raise HydraDBWriteCeilingExceeded(
                    "HydraDB's local store has hit its permanent write-volume ceiling for this backend "
                    "(see graphplatform/README.md's Known limitations) and needs to be wiped and restarted "
                    "by an operator -- this is not a transient error and retrying will not help."
                ) from exc
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

    def _upsert_nodes_batch(
        self,
        label: str,
        rows: list[dict[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> None:
        """Bulk MERGE+SET for many nodes of the same label in a handful of
        round trips instead of one pair of round trips per node.

        Trade-off, deliberate and scoped to bulk-import callers only: does
        NOT preserve historical first_observed_at the way _upsert_node
        does (every row's first_observed_at is taken as given, not
        reconciled against what may already be stored) -- HydraDB's
        UNWIND-batch engine only accelerates MERGE-shaped writes, not
        reads (confirmed by hand: `UNWIND $keys AS key MATCH (n {key:
        key}) RETURN ...` and every read-shaped variant tried fails with
        "UNWIND batch supports one-hop relationships only" or "UNWIND
        batch node property must be id" -- there is no batched-read path
        to fetch N existing first_observed_at values in one round trip),
        so preserving per-row history would still cost N round trips and
        defeat the point. Every row must already carry `id`, `key`,
        `first_observed_at` (as an ISO string), and `event_time` (ISO
        string) plus whatever label-specific properties belong in the SET.
        """
        if label not in schema.NODE_LABELS:
            raise ValueError(f"unknown node label: {label!r}")
        if not rows:
            return
        set_clauses = ", ".join(f"n.{prop} = row.{prop}" for prop in rows[0] if prop != "id")
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            self._run(
                f"UNWIND $rows AS row MERGE (n {{id: row.id}}) SET n:{label}, {set_clauses}",
                rows=chunk,
                write=True,
            )
        log.info("graph write: node batch upserted", extra={"label": label, "count": len(rows)})

    def upsert_packages_batch(
        self,
        items: list[tuple[str, str, str]],  # (key, ecosystem, name)
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> None:
        rows = [
            {
                "id": stable_id(schema.PACKAGE, key),
                "key": key,
                "ecosystem": ecosystem,
                "name": name,
                "first_observed_at": to_iso(first_observed_at),
                "event_time": to_iso(event_time),
            }
            for key, ecosystem, name in items
        ]
        self._upsert_nodes_batch(schema.PACKAGE, rows)

    def upsert_versions_batch(
        self,
        items: list[tuple[str, str, str]],  # (key, package_key, version)
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> None:
        rows = [
            {
                "id": stable_id(schema.VERSION, key),
                "key": key,
                "package_key": package_key,
                "version": version,
                "first_observed_at": to_iso(first_observed_at),
                "event_time": to_iso(event_time),
            }
            for key, package_key, version in items
        ]
        self._upsert_nodes_batch(schema.VERSION, rows)

    def upsert_applications_batch(
        self,
        items: list[tuple[str, str, str, str]],  # (key, org, repo, subpath)
        *,
        first_observed_at: datetime,
        event_time: datetime,
    ) -> None:
        rows = [
            {
                "id": stable_id(schema.APPLICATION, key),
                "key": key,
                "org": org,
                "repo": repo,
                "subpath": subpath,
                "first_observed_at": to_iso(first_observed_at),
                "event_time": to_iso(event_time),
            }
            for key, org, repo, subpath in items
        ]
        self._upsert_nodes_batch(schema.APPLICATION, rows)

    def write_depends_on_batch_merge_only(
        self,
        source_label: str,
        items: list[tuple[str, str]],  # (source_key, target_package_key)
        *,
        chunk_size: int = 500,
    ) -> None:
        """Bulk MERGE-only DEPENDS_ON writes for many edges sharing the
        same source_label, all Package targets. Unlike write_depends_on,
        does NOT set range/manager/rel_id/timestamps -- HydraDB's
        UNWIND-batch mode only accelerates the MERGE step for
        relationships, not a chained SET (confirmed by hand: a bulk
        `UNWIND ... MATCH ...-[r]->... SET ...` fails with "UNWIND MATCH
        must end in RETURN or DELETE"). Safe for bulk-imported edges
        specifically because get_dependents_of/blast_radius match by
        relationship type and endpoint keys only, never by these
        properties -- a bulk edge is just as traversable, only its
        range/manager display fields come back empty.
        """
        if source_label not in (schema.PACKAGE, schema.APPLICATION):
            raise ValueError("DEPENDS_ON source must be Package or Application")
        if not items:
            return
        rows = [
            {
                "a_id": stable_id(source_label, source_key),
                "b_id": stable_id(schema.PACKAGE, target_key),
                "rid": stable_id(schema.DEPENDS_ON, source_key, target_key),
            }
            for source_key, target_key in items
        ]
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            self._run(
                f"UNWIND $rows AS row "
                f"MATCH (a:{source_label} {{id: row.a_id}}), (b:{schema.PACKAGE} {{id: row.b_id}}) "
                f"MERGE (a)-[r:{schema.DEPENDS_ON} {{id: row.rid}}]->(b)",
                rows=chunk,
                write=True,
            )
        log.info("graph write: DEPENDS_ON batch merged", extra={"source_label": source_label, "count": len(rows)})

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

    def resolve_versions_batch(
        self,
        app_key: str,
        items: list[tuple[str, datetime]],  # (version_key, resolved_at)
        *,
        first_observed_at: datetime,
        event_time: datetime,
        chunk_size: int = 500,
    ) -> None:
        """Bulk-open RESOLVED_VERSION_AT intervals for many versions
        resolved by the same Application. A batched MERGE pass (one round
        trip per chunk, same shape as write_depends_on_batch_merge_only)
        followed by individual SET passes -- SET can't be batched under
        UNWIND on this engine (confirmed by hand, see
        write_depends_on_batch_merge_only's docstring), and unlike that
        method this SET is NOT skippable: superseded_at is the
        open-interval sentinel blast_radius's traversal filters on, so
        every edge here needs it written. Still a large win over
        resolve_version called once per item: that's read+merge+set (3
        round trips) per edge, this is one shared batched merge plus just
        the set per edge.

        The SET pass is deliberately sequential, not thread-pooled, even
        though every SET touches a distinct relationship and reads are
        safe to parallelize (see blast_radius's callers, which do use a
        thread pool). Confirmed by hand on this specific backend: the
        exact same ~500 writes that complete with zero failures run
        sequentially (in under a minute) start hitting the write-volume
        ceiling within a second when fanned out across a 16-worker thread
        pool -- concurrent writes appear to churn this SlateDB-backed
        engine's un-GC'able manifest epochs much faster than the same
        writes done one at a time, not just faster in wall-clock terms.
        Slow-but-reliable beats fast-but-flaky here. Like the other
        batch_* methods, does not preserve historical first_observed_at
        across re-runs.
        """
        if not items:
            return
        resolved_isos = [(version_key, to_iso(resolved_at)) for version_key, resolved_at in items]
        merge_rows = [
            {
                "a_id": stable_id(schema.APPLICATION, app_key),
                "b_id": stable_id(schema.VERSION, version_key),
                "rid": stable_id(schema.RESOLVED_VERSION_AT, app_key, version_key, resolved_iso),
            }
            for version_key, resolved_iso in resolved_isos
        ]
        for start in range(0, len(merge_rows), chunk_size):
            chunk = merge_rows[start : start + chunk_size]
            self._run(
                f"UNWIND $rows AS row "
                f"MATCH (a:{schema.APPLICATION} {{id: row.a_id}}), (b:{schema.VERSION} {{id: row.b_id}}) "
                f"MERGE (a)-[r:{schema.RESOLVED_VERSION_AT} {{id: row.rid}}]->(b)",
                rows=chunk,
                write=True,
            )

        foa_iso = to_iso(first_observed_at)
        et_iso = to_iso(event_time)

        def _set_one(version_key: str, resolved_iso: str) -> None:
            rid = stable_id(schema.RESOLVED_VERSION_AT, app_key, version_key, resolved_iso)
            self._run(
                f"MATCH (a:{schema.APPLICATION} {{key: $a_key}})-[r:{schema.RESOLVED_VERSION_AT} {{id: $rid}}]->"
                f"(b:{schema.VERSION} {{key: $b_key}}) "
                f"SET r.rel_id = $rid, r.first_observed_at = $foa, r.event_time = $et, "
                f"r.resolved_at = $resolved_at, r.superseded_at = $open",
                a_key=app_key,
                b_key=version_key,
                rid=rid,
                foa=foa_iso,
                et=et_iso,
                resolved_at=resolved_iso,
                open=schema.OPEN_INTERVAL_SENTINEL,
                write=True,
            )

        for version_key, resolved_iso in resolved_isos:
            _set_one(version_key, resolved_iso)
        log.info("graph write: RESOLVED_VERSION_AT batch resolved", extra={"app_key": app_key, "count": len(items)})

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
