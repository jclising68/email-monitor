"""
slack_reporter.py — Format and send messages to Slack via an incoming webhook.

Two message types:
  1. Immediate ZapMail disconnection alert (fires once per new disconnection)
  2. Daily summary report (always sent, even if everything is healthy)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

_PHT = ZoneInfo("Asia/Manila")

def _now_pht() -> str:
    return datetime.now(_PHT).strftime("%m/%d/%Y %I:%M %p PHT")

logger = logging.getLogger(__name__)


class SlackReporter:
    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url

    # ── Internal send ─────────────────────────────────────────────────────────

    def _send(self, text: str) -> bool:
        """
        POST message to Slack webhook.
        Returns True on success, False on failure (never raises — monitoring must continue).
        """
        try:
            resp = requests.post(
                self._webhook_url,
                json={
                    "text": text,
                    "username": "Email Monitoring Reporter",
                    "icon_emoji": ":email:",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(
                    "Slack webhook returned HTTP %d: %s", resp.status_code, resp.text
                )
                return False
            return True
        except Exception as exc:
            logger.error("Failed to send Slack message: %s", exc)
            return False

    # ── Public: immediate alert ───────────────────────────────────────────────

    def send_zapmail_alert(self, email: str, workspace_name: str,
                           reconnect_attempted: bool = False) -> bool:
        """Send the one-time ZapMail disconnection alert."""
        detected = _now_pht()
        if reconnect_attempted:
            action = ":warning: Auto-reconnect via ZapMail API failed. Log in to Instantly and reconnect manually."
        else:
            action = ":warning: Log in to Instantly and reconnect manually."
        text = (
            f":rotating_light: *ZapMail Account Disconnected*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* ZapMail\n"
            f"*Detected:* {detected}\n"
            f"{action}"
        )
        success = self._send(text)
        if success:
            logger.info("Slack ZapMail alert sent for %s (%s)", email, workspace_name)
        return success

    def send_client_accounts_disconnected(self, accounts: list) -> bool:
        """
        One-time batched heads-up for multiple client-owned disconnected accounts.
        accounts: list of {"email": ..., "workspace_name": ...}
        """
        if not accounts:
            return True
        lines = [f":warning: *Account Disconnected — Client Action Required*"]
        lines.append(f"*Detected:* {_now_pht()}")
        lines.append(f"*Note:* These accounts are not managed by us (not ZapMail or Mission Inbox). Please notify the client to reconnect them in Instantly.\n")
        # Group by workspace
        by_ws: dict = defaultdict(list)
        for a in accounts:
            by_ws[a["workspace_name"]].append(a["email"])
        for ws_name, emails in sorted(by_ws.items()):
            lines.append(f"*Workspace: {ws_name}*")
            for email in sorted(emails):
                lines.append(f"  • {email}")
        return self._send("\n".join(lines))

    def send_reconnect_attempting(self, email: str, workspace_name: str, provider: str) -> bool:
        """Alert: reconnect attempt starting."""
        text = (
            f":arrows_counterclockwise: *Attempting to reconnect account*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider.title()}\n"
            f"*Time:* {_now_pht()}"
        )
        return self._send(text)

    def send_reconnect_success(self, email: str, workspace_name: str, provider: str) -> bool:
        """Alert: reconnect succeeded."""
        text = (
            f":white_check_mark: *Reconnected successfully*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider.title()}\n"
            f"*Time:* {_now_pht()}"
        )
        return self._send(text)

    def send_reconnect_failed(self, email: str, workspace_name: str, provider: str,
                              attempts: int, max_attempts: int) -> bool:
        """Alert: reconnect failed."""
        if attempts >= max_attempts:
            note = f":x: Max attempts ({max_attempts}) reached — *manual action required*. Log in to Instantly and reconnect."
        else:
            note = f":warning: Will retry next hourly check (attempt {attempts}/{max_attempts})."
        text = (
            f":x: *Reconnect failed*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider.title()}\n"
            f"*Time:* {_now_pht()}\n"
            f"{note}"
        )
        return self._send(text)

    # ── Public: daily report ──────────────────────────────────────────────────

    def send_daily_report(self, report_data: "ReportData") -> bool:
        """Format and send the daily summary Slack message."""
        text = _format_daily_report(report_data)
        success = self._send(text)
        if success:
            logger.info("Slack daily report sent.")
        return success


# ── Report data structures ────────────────────────────────────────────────────

class WorkspaceSummary:
    def __init__(self, name: str):
        self.name = name
        self.connected: int = 0
        self.paused: int = 0
        self.disconnected: int = 0
        self.dns_issues: int = 0


class ReportData:
    def __init__(self):
        self.generated_at: str = _now_pht()
        self.workspace_summaries: List[WorkspaceSummary] = []

        # Successful auto-reconnects this run
        self.reconnected: List[Dict] = []        # [{email, workspace_name}]

        # Still disconnected after all attempts
        self.still_disconnected: List[Dict] = [] # [{email, workspace_name, provider, attempts}]

        # DNS failures
        self.dns_failures: List[Dict] = []        # [{domain, workspace_name, missing: [str]}]

        # Workspace-level errors (bad API key, network, etc.)
        self.workspace_errors: List[Dict] = []    # [{workspace_name, error}]

    @property
    def total_workspaces(self) -> int:
        return len(self.workspace_summaries)

    @property
    def total_connected(self) -> int:
        return sum(w.connected for w in self.workspace_summaries)

    @property
    def total_disconnected(self) -> int:
        return sum(w.disconnected for w in self.workspace_summaries)

    @property
    def total_dns_issues(self) -> int:
        return sum(w.dns_issues for w in self.workspace_summaries)


# ── Formatting ────────────────────────────────────────────────────────────────

def _format_daily_report(r: ReportData) -> str:
    lines: List[str] = []

    lines.append(":bar_chart: *Instantly Email Monitor — Daily Report*")
    lines.append(f":calendar: {r.generated_at}")
    lines.append("")

    # ── Workspace summary table ──
    lines.append("*WORKSPACE SUMMARY*")
    if r.workspace_summaries:
        max_name_len = max(len(w.name) for w in r.workspace_summaries)
        for w in sorted(r.workspace_summaries, key=lambda x: x.name):
            pad = max_name_len - len(w.name)
            parts = [f"{w.connected} connected"]
            if w.paused:
                parts.append(f"{w.paused} paused")
            parts.append(f"{w.disconnected} disconnected")
            parts.append(f"{w.dns_issues} DNS issues")
            lines.append(f"`{w.name}`{' ' * pad}   " + " | ".join(parts))
    else:
        lines.append("_No workspaces checked._")

    lines.append("")
    lines.append(
        f"*TOTALS:* {r.total_workspaces} workspaces | "
        f"{r.total_connected} connected | "
        f"{r.total_disconnected} disconnected | "
        f"{r.total_dns_issues} DNS issues"
    )

    # ── Workspace errors ──
    if r.workspace_errors:
        lines.append("")
        lines.append(":x: *Workspace Errors (skipped)*")
        for e in r.workspace_errors:
            lines.append(f"• {e['workspace_name']} — {e['error']}")

    # ── Auto-reconnected ──
    lines.append("")
    if r.reconnected:
        lines.append(":white_check_mark: *Auto-Reconnected*")
        for item in r.reconnected:
            provider_tag = " _(ZapMail)_" if item.get("provider") == "zapmail" else ""
            lines.append(f"• {item['email']} ({item['workspace_name']}){provider_tag}")
    else:
        lines.append(":white_check_mark: *Auto-Reconnected*")
        lines.append("_None this run._")

    # ── Still disconnected ──
    lines.append("")
    if r.still_disconnected:
        lines.append(":x: *Still Disconnected — Action Required*")
        for item in r.still_disconnected:
            provider = item.get("provider", "unknown")
            attempts = item.get("attempts", 0)
            if provider == "zapmail":
                note = "ZapMail, manual action required"
            elif attempts > 0:
                note = f"Reconnect failed ({attempts} attempt{'s' if attempts != 1 else ''})"
            else:
                note = "Disconnected"
            lines.append(f"• {item['email']} ({item['workspace_name']}) — {note}")
    else:
        lines.append(":white_check_mark: *All accounts connected.*")

    # ── DNS issues ──
    lines.append("")
    if r.dns_failures:
        lines.append(":shield: *DNS Issues*")
        for item in r.dns_failures:
            missing = ", ".join(item.get("missing", []))
            lines.append(f"• {item['domain']} ({item['workspace_name']}) — Missing: {missing}")
    else:
        lines.append(":shield: *DNS Issues*")
        lines.append("_None detected._")

    return "\n".join(lines)
