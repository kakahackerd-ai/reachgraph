"""Phase 5 Reconciliation Models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class DiscrepancyReport:
    """Actionable bug report identifying a missing or dropped graph entity."""
    discrepancy_id: str
    stage: str  # "registry_ingestion" | "advisory_ingestion" | "manifest_discovery" | "alerting"
    entity_type: str  # "Package" | "Version" | "Advisory" | "Resolution" | "Alert"
    entity_key: str
    description: str
    action_taken: str  # "logged_and_corrected" | "logged_only"
    detected_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "discrepancy_id": self.discrepancy_id,
            "stage": self.stage,
            "entity_type": self.entity_type,
            "entity_key": self.entity_key,
            "description": self.description,
            "action_taken": self.action_taken,
            "detected_at": self.detected_at,
        }


@dataclass
class ReconciliationStatus:
    """Health and metrics status for the reconciliation sweep."""
    last_run_at: str | None = None
    last_duration_s: float = 0.0
    total_runs: int = 0
    total_discrepancies_found: int = 0
    recent_discrepancies: list[DiscrepancyReport] = field(default_factory=list)
    healthy: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_run_at": self.last_run_at,
            "last_duration_s": round(self.last_duration_s, 3),
            "total_runs": self.total_runs,
            "total_discrepancies_found": self.total_discrepancies_found,
            "discrepancy_count": len(self.recent_discrepancies),
            "recent_discrepancies": [d.to_dict() for d in self.recent_discrepancies[-20:]],
            "healthy": self.healthy,
        }
