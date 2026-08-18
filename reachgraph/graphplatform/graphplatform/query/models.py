"""Phase 4 Query & Reasoning Data Models.

Defines the structured representations for answering the six core supply-chain
questions, predictive cascade forecasting, and chained-vulnerability analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class TransitiveExposureResult:
    """Answer to Question 1: Which internal applications are transitively exposed?"""
    application_key: str
    org: str
    repo: str
    subpath: str
    resolved_version: str
    resolved_at: str
    depth: int
    path: list[str]  # chain of keys from application to target
    status: Literal["confirmed", "predicted"] = "confirmed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.status,
            "application_key": self.application_key,
            "org": self.org,
            "repo": self.repo,
            "subpath": self.subpath,
            "resolved_version": self.resolved_version,
            "resolved_at": self.resolved_at,
            "depth": self.depth,
            "path": self.path,
        }


@dataclass
class IntroducingVersionResult:
    """Answer to Question 2: Which version introduced the vulnerability?"""
    advisory_key: str
    introducing_version_key: str | None
    confidence: float
    evidence: str
    precise: bool  # False if defaulted to advisory range start
    stated_range: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisory_key": self.advisory_key,
            "introducing_version_key": self.introducing_version_key,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "precise": self.precise,
            "stated_range": self.stated_range,
        }


@dataclass
class LiveResolutionResult:
    """Answer to Question 3: Which applications resolved the compromised version while it was live?"""
    application_key: str
    version_key: str
    resolved_at: str
    superseded_at: str | None
    window_start: str
    window_end: str
    was_live_in_window: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_key": self.application_key,
            "version_key": self.version_key,
            "resolved_at": self.resolved_at,
            "superseded_at": self.superseded_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "was_live_in_window": self.was_live_in_window,
        }


@dataclass
class BlastRadiusNode:
    key: str
    label: str  # "Package" | "Application" | "Version"
    depth: int
    path: list[str]


@dataclass
class BlastRadiusResult:
    """Answer to Question 4: What is the complete blast radius?"""
    source_key: str
    total_reached: int
    max_depth: int
    packages: list[str]
    applications: list[str]
    nodes: list[BlastRadiusNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "total_reached": self.total_reached,
            "max_depth": self.max_depth,
            "packages": self.packages,
            "applications": self.applications,
            "nodes": [
                {"key": n.key, "label": n.label, "depth": n.depth, "path": n.path}
                for n in self.nodes
            ],
        }


@dataclass
class TyposquatResult:
    """Answer to Question 5: Are there likely typosquat packages nearby?"""
    package_key: str
    popular_target: str
    similarity_score: float
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_key": self.package_key,
            "popular_target": self.popular_target,
            "similarity_score": self.similarity_score,
            "method": self.method,
        }


@dataclass
class SharedInfraMaintainerResult:
    """Answer to Question 6: Which other packages share maintainers or infrastructure?"""
    package_key: str
    connected_package_key: str
    link_type: Literal["same_maintainer", "shared_infrastructure"]
    shared_entity_key: str
    evidence_type: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_key": self.package_key,
            "connected_package_key": self.connected_package_key,
            "link_type": self.link_type,
            "shared_entity_key": self.shared_entity_key,
            "evidence_type": self.evidence_type,
            "confidence": self.confidence,
        }


@dataclass
class PredictedPropagationResult:
    """Predictive Impact: Propagation forecasting."""
    consumer_key: str
    consumer_label: str  # "Application" | "Package"
    declared_range: str
    flagged_version_key: str
    confidence: float
    basis: str = "propagation"
    type: Literal["predicted"] = "predicted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "basis": self.basis,
            "consumer_key": self.consumer_key,
            "consumer_label": self.consumer_label,
            "declared_range": self.declared_range,
            "flagged_version_key": self.flagged_version_key,
            "confidence": self.confidence,
        }


@dataclass
class EarlyWarningRiskResult:
    """Predictive Impact: Early-warning risk scoring."""
    package_key: str
    risk_score: float  # 0.0 to 1.0
    confidence: float
    contributing_factors: list[dict[str, Any]]
    basis: str = "early_warning"
    type: Literal["predicted"] = "predicted"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "basis": self.basis,
            "package_key": self.package_key,
            "risk_score": self.risk_score,
            "confidence": self.confidence,
            "contributing_factors": self.contributing_factors,
        }


@dataclass
class ChainRisk:
    """Chained-Vulnerability Detection Result."""
    package_a: str
    package_b: str
    risk_type: str
    description: str
    confidence: float
    path: list[str]
    mitigation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_a": self.package_a,
            "package_b": self.package_b,
            "risk_type": self.risk_type,
            "description": self.description,
            "confidence": self.confidence,
            "path": self.path,
            "mitigation": self.mitigation,
        }
