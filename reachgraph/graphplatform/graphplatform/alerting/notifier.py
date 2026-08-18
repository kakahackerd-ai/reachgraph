"""Notification Channel implementations for Alerting."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .models import Alert, NotificationChannel

log = logging.getLogger("graphplatform.alerting.notifier")


class WebhookNotifier:
    """Delivers alerts to a generic HTTP or Slack-compatible webhook endpoint."""

    name = "webhook"

    def __init__(
        self,
        webhook_url: str | None = None,
        *,
        http: httpx.Client | None = None,
        slack_format: bool = False,
    ) -> None:
        self.webhook_url = webhook_url
        self._http = http or httpx.Client(timeout=10.0)
        self.slack_format = slack_format

    def send(self, alert: Alert) -> bool:
        if not self.webhook_url:
            log.info("alert emitted (no webhook configured): %s for %s", alert.alert_id, alert.package_key)
            return True

        payload: dict[str, Any]
        if self.slack_format or "slack.com" in self.webhook_url or "hooks.slack.com" in self.webhook_url:
            # Format as rich Slack block
            apps_text = ", ".join(f"`{a.get('application_key')}`" for a in alert.exposed_applications) or "None"
            payload = {
                "text": f"🚨 *ReachGraph Security Alert: {alert.severity}* in `{alert.package_key}`",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*Security Alert [{alert.severity}]*: *{alert.advisory_id}*\n"
                                f"*Package*: `{alert.package_key}` (version `{alert.version_key}`)\n"
                                f"*Summary*: {alert.summary}\n"
                                f"*Trigger*: `{alert.trigger_type}`\n"
                                f"*Exposed Applications*: {apps_text}\n"
                                f"*Blast Radius Reach*: {alert.blast_radius_summary.get('total_reached', 0)} node(s)"
                            ),
                        },
                    }
                ],
            }
        else:
            payload = alert.to_dict()

        try:
            resp = self._http.post(self.webhook_url, json=payload)
            resp.raise_for_status()
            log.info("alert delivered via webhook: %s (HTTP %d)", alert.alert_id, resp.status_code)
            return True
        except Exception:
            log.exception("failed to deliver alert %s to webhook %s", alert.alert_id, self.webhook_url)
            return False

    def close(self) -> None:
        self._http.close()


class InMemoryAlertLog:
    """Maintains a bounded in-memory ring buffer of recent alerts for UI and metrics."""

    def __init__(self, max_size: int = 500) -> None:
        self.max_size = max_size
        self._alerts: list[Alert] = []

    def record(self, alert: Alert) -> None:
        self._alerts.append(alert)
        if len(self._alerts) > self.max_size:
            self._alerts.pop(0)

    def list_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        return [a.to_dict() for a in reversed(self._alerts[-limit:])]

    def clear(self) -> None:
        self._alerts.clear()

    @property
    def count(self) -> int:
        return len(self._alerts)
