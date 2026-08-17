"""Graph schema for the supply-chain vulnerability graph.

This module is descriptive, not an ORM: it names every node label and
relationship type this system is allowed to write, and documents the
timestamp discipline every one of them carries. The actual Cypher lives in
write_service.py, shaped around HydraDB's real query-engine dialect (see the
module docstring there) rather than textbook OpenCypher.

Timestamp discipline (applies to every node and every relationship):
  - first_observed_at: when this pipeline first saw the entity. Set once,
    on first write, and never overwritten by a later upsert of the same key.
  - event_time: when the thing actually happened in the real world (e.g. a
    registry's real publish timestamp). Always supplied explicitly by the
    caller; this layer never defaults it to "now".
Both are stored as ISO-8601 strings (UTC) — see to_iso/from_iso below. The
underlying query engine has no native temporal type support worth relying
on (see write_service.py), and ISO-8601 strings sort and compare correctly
as plain strings, which is all phase 1 needs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

# ---------------------------------------------------------------------------
# Node labels
# ---------------------------------------------------------------------------

PACKAGE = "Package"
VERSION = "Version"
MAINTAINER = "Maintainer"
INFRASTRUCTURE = "Infrastructure"
APPLICATION = "Application"
ADVISORY = "Advisory"

NODE_LABELS = frozenset({PACKAGE, VERSION, MAINTAINER, INFRASTRUCTURE, APPLICATION, ADVISORY})

# ---------------------------------------------------------------------------
# Relationship types
# ---------------------------------------------------------------------------

DEPENDS_ON = "DEPENDS_ON"
RESOLVED_VERSION_AT = "RESOLVED_VERSION_AT"
PUBLISHED_BY = "PUBLISHED_BY"
AFFECTS = "AFFECTS"
INTRODUCED_IN = "INTRODUCED_IN"
SAME_MAINTAINER_AS = "SAME_MAINTAINER_AS"
SHARES_INFRASTRUCTURE_WITH = "SHARES_INFRASTRUCTURE_WITH"
POSSIBLE_TYPOSQUAT_OF = "POSSIBLE_TYPOSQUAT_OF"
PREDICTED_EXPOSURE = "PREDICTED_EXPOSURE"

REL_TYPES = frozenset({
    DEPENDS_ON, RESOLVED_VERSION_AT, PUBLISHED_BY, AFFECTS, INTRODUCED_IN,
    SAME_MAINTAINER_AS, SHARES_INFRASTRUCTURE_WITH, POSSIBLE_TYPOSQUAT_OF,
    PREDICTED_EXPOSURE,
})

# PREDICTED_EXPOSURE is the one relationship type that is never a confirmed
# fact -- it must stay structurally distinguishable from AFFECTS and
# RESOLVED_VERSION_AT so no caller can mistake a prediction for ground
# truth. Concretely: it is a different relationship TYPE (not a flag on an
# existing edge), and every write of it requires `basis` to be one of these.
PREDICTION_BASES = frozenset({"propagation", "early_warning"})

EVIDENCE_TYPES_MAINTAINER = frozenset({"verified_email", "signing_key", "manual"})
EVIDENCE_TYPES_INFRASTRUCTURE = frozenset({"ci_system", "ip_range", "signing_key"})

Consistency = Literal["causal", "strong"]


def stable_id(*parts: str) -> int:
    """Deterministic non-negative 63-bit integer derived from natural-key parts.

    HydraDB's query engine (verified by hand -- see write_service.py) treats
    the literal property name `id` as a reserved, integer-only merge/create
    identity on both nodes and relationships: `MERGE (n {id: ...})` and
    `MERGE (a)-[r:TYPE {id: ...}]->(b)` both reject non-integer values. Our
    natural keys are strings (e.g. "npm:lodash"), so every node and
    relationship gets a deterministic integer `id` hashed from its natural
    key (and, for relationships, its endpoints' keys plus any
    interval-distinguishing fields), used *only* as the merge handle. The
    real human-facing identifier is always mirrored into a separate string
    property (`key` for nodes, `rel_id` for relationships) and every read
    path in this service uses that mirror, never `id`.

    Collision risk: blake2b digest truncated to 63 bits gives ~9.2e18
    possible values. For the entity volumes this system deals with, the
    birthday-bound collision probability is negligible; this is a documented
    tradeoff, not an oversight.
    """
    digest = hashlib.blake2b(":".join(parts).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def to_iso(dt: datetime) -> str:
    """Serialize a datetime to an ISO-8601 UTC string for storage."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# Sentinel used internally for RESOLVED_VERSION_AT.superseded_at when a
# resolution is still current. The query engine's WHERE clause only supports
# "boolean combinations of property comparisons" (verified by hand -- no
# IS NULL / IS NOT NULL support), so a nullable field can't be found via
# `WHERE r.superseded_at IS NULL`. An empty string is used as the "still
# current" marker on the wire and translated to/from Python `None` at the
# write_service.py API boundary, so callers of this service never see the
# sentinel -- only real code in write_service.py should ever reference it.
OPEN_INTERVAL_SENTINEL = ""
