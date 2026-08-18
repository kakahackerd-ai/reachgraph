"""Phase 5 Alerting & Notification Models and Interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class Alert:
    """Security Alert generated upon advisory or resolution events."""
    alert_id: str
    advisory_id: str
    package_key: str
    version_key: str
    severity: str
    summary: str
    exposed_applications: list[dict[str, Any]]
    blast_radius_summary: dict[str, Any]
    trigger_type: str  # "new_advisory" | "new_resolution"
    created_at: str
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            app_keys = sorted([a.get("application_key", "") for a in self.exposed_applications])
            self.dedupe_key = f"{self.advisory_id}:{self.version_key}:{','.join(app_keys)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "advisory_id": self.advisory_id,
            "package_key": self.package_key,
            "version_key": self.version_key,
            "severity": self.severity,
            "summary": self.summary,
            "exposed_applications": self.exposed_applications,
            "blast_radius_summary": self.blast_radius_summary,
            "trigger_type": self.trigger_type,
            "created_at": self.created_at,
            "dedupe_key": self.dedupe_key,
        }


class NotificationChannel(Protocol):
    """Abstract notification delivery channel."""
    name: str

    def send(self, alert: Alert) -> bool:
        """Send an alert to the channel. Returns True if delivery succeeded."""
        ...
