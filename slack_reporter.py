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

    def send_manual_reconnect_alert(self, email: str, workspace_name: str,
                                    provider: str, reconnect_attempted: bool = False) -> bool:
        """Send a one-time disconnection alert for any provider requiring manual action."""
        detected = _now_pht()
        provider_label = provider
        if reconnect_attempted:
            action = f":warning: Auto-reconnect via {provider_label} API failed. Log in to Instantly and reconnect manually."
        else:
            action = ":warning: Log in to Instantly and reconnect manually."
        text = (
            f":rotating_light: *{provider_label} Account Disconnected*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider_label}\n"
            f"*Detected:* {detected}\n"
            f"{action}"
        )
        success = self._send(text)
        if success:
            logger.info("Slack %s alert sent for %s (%s)", provider_label, email, workspace_name)
        return success

    def send_zapmail_alert(self, email: str, workspace_name: str,
                           reconnect_attempted: bool = False) -> bool:
        """Backwards-compatible alias — use send_manual_reconnect_alert instead."""
        return self.send_manual_reconnect_alert(email, workspace_name, "ZapMail", reconnect_attempted)

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
            f"*Provider:* {provider}\n"
            f"*Time:* {_now_pht()}"
        )
        return self._send(text)

    def send_reconnect_success(self, email: str, workspace_name: str, provider: str) -> bool:
        """Alert: reconnect succeeded."""
        text = (
            f":white_check_mark: *Reconnected successfully*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider}\n"
            f"*Time:* {_now_pht()}"
        )
        return self._send(text)

    def send_reconnect_failed(self, email: str, workspace_name: str, provider: str,
                              attempts: int, max_attempts: int) -> bool:
        """Alert: reconnect failed."""
        if attempts >= max_attempts:
            note = f":x: Max attempts ({max_attempts}) reached — *manual action required*. Log in to Instantly and reconnect."
        else:
            note = f":warning: Will retry next check (attempt {attempts}/{max_attempts})."
        text = (
            f":x: *Reconnect failed*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider}\n"
            f"*Time:* {_now_pht()}\n"
            f"{note}"
        )
        return self._send(text)

    # ── Public: health alerts ────────────────────────────────────────────────

    def send_health_alert(self, email: str, workspace_name: str,
                          health_score: int, spam_rate: float,
                          is_critical: bool = False) -> bool:
        """Immediate alert when an account's warmup health drops below threshold."""
        severity = "CRITICAL" if is_critical else "WARNING"
        emoji = ":red_circle:" if is_critical else ":large_orange_circle:"
        action = (
            "*Auto-pause recommended.* This account may be damaging domain reputation."
            if is_critical
            else "Monitor closely — health may recover, or consider pausing."
        )
        text = (
            f"{emoji} *Warmup Health {severity}*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Health Score:* {health_score}%\n"
            f"*Spam Rate:* {spam_rate:.1f}%\n"
            f"*Time:* {_now_pht()}\n"
            f"{action}"
        )
        return self._send(text)

    def send_spam_rate_alert(self, email: str, workspace_name: str,
                             spam_rate: float, landed_spam: int, landed_inbox: int) -> bool:
        """Immediate alert when spam rate exceeds threshold."""
        text = (
            f":rotating_light: *High Spam Rate Detected*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Spam Rate:* {spam_rate:.1f}% ({landed_spam} spam / {landed_inbox + landed_spam} total)\n"
            f"*Time:* {_now_pht()}\n"
            f":warning: Investigate deliverability — consider pausing this account."
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

    # ── Public: weekly domain health report ──────────────────────────────────

    def send_weekly_domain_report(self, report_data: "ReportData") -> bool:
        """Format and send the weekly domain health Slack message."""
        text = _format_weekly_domain_report(report_data)
        success = self._send(text)
        if success:
            logger.info("Slack weekly domain health report sent.")
        return success


# ── Report data structures ────────────────────────────────────────────────────

class WorkspaceSummary:
    def __init__(self, name: str):
        self.name = name
        self.connected: int = 0
        self.warmup: int = 0
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

        # Health score alerts (low warmup health)
        # [{email, workspace_name, health_score, spam_rate}]
        self.health_alerts: List[Dict] = []

        # Domain health data (for weekly report)
        # [{domain, workspace_name, avg_health, total_inbox, total_spam, spam_rate, account_count, dns_status}]
        self.domain_health: List[Dict] = []

        # Tracking domain issues
        # [{email, workspace_name, issue}]
        self.tracking_domain_issues: List[Dict] = []

        # Campaign bounce rate alerts
        # [{campaign_name, workspace_name, bounce_rate, bounced, sent}]
        self.bounce_alerts: List[Dict] = []

    @property
    def total_workspaces(self) -> int:
        return len(self.workspace_summaries)

    @property
    def total_connected(self) -> int:
        return sum(w.connected for w in self.workspace_summaries)

    @property
    def total_warmup(self) -> int:
        return sum(w.warmup for w in self.workspace_summaries)

    @property
    def total_paused(self) -> int:
        return sum(w.paused for w in self.workspace_summaries)

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
            total_online = w.connected + w.warmup
            warmup_tag = f" ({w.warmup} warmup)" if w.warmup else ""
            parts = [f"{total_online} connected{warmup_tag}"]
            if w.paused:
                parts.append(f"{w.paused} paused")
            parts.append(f"{w.disconnected} disconnected")
            parts.append(f"{w.dns_issues} DNS issues")
            lines.append(f"`{w.name}`{' ' * pad}   " + " | ".join(parts))
    else:
        lines.append("_No workspaces checked._")

    lines.append("")
    total_online = r.total_connected + r.total_warmup
    warmup_tag = f" ({r.total_warmup} warmup)" if r.total_warmup else ""
    totals = [
        f"{r.total_workspaces} workspaces",
        f"{total_online} connected{warmup_tag}",
    ]
    if r.total_paused:
        totals.append(f"{r.total_paused} paused")
    totals += [f"{r.total_disconnected} disconnected", f"{r.total_dns_issues} DNS issues"]
    lines.append(f"*TOTALS:* " + " | ".join(totals))

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
        _PROVIDER_LABELS = {"zapmail": "ZapMail", "missioninbox": "Mission Inbox"}
        for item in r.reconnected:
            provider = item.get("provider", "")
            label = _PROVIDER_LABELS.get(provider, provider.title())
            provider_tag = f" _({label})_" if label else ""
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
            label = {"zapmail": "ZapMail", "missioninbox": "Mission Inbox"}.get(provider, "")
            if provider in ("zapmail", "missioninbox") and attempts == 0:
                note = f"{label}, manual action required"
            elif attempts > 0:
                suffix = f" ({label})" if label else ""
                note = f"Reconnect failed ({attempts} attempt{'s' if attempts != 1 else ''}){suffix}"
            else:
                note = "Disconnected"
            lines.append(f"• {item['email']} ({item['workspace_name']}) — {note}")
    else:
        lines.append(":white_check_mark: *All accounts connected.*")

    # ── Health alerts ──
    lines.append("")
    if r.health_alerts:
        lines.append(":thermometer: *Warmup Health Alerts*")
        for item in sorted(r.health_alerts, key=lambda x: x.get("health_score", 100)):
            score = item.get("health_score", "?")
            spam = item.get("spam_rate", 0)
            emoji = ":red_circle:" if score != "?" and score < 60 else ":large_orange_circle:"
            lines.append(
                f"• {emoji} {item['email']} ({item['workspace_name']}) — "
                f"Health: {score}%, Spam rate: {spam:.1f}%"
            )
    else:
        lines.append(":thermometer: *Warmup Health*")
        lines.append("_All accounts healthy._")

    # ── Tracking domain issues ──
    if r.tracking_domain_issues:
        lines.append("")
        lines.append(":link: *Tracking Domain Issues*")
        for item in r.tracking_domain_issues:
            lines.append(f"• {item['email']} ({item['workspace_name']}) — {item['issue']}")

    # ── Bounce rate alerts ──
    if r.bounce_alerts:
        lines.append("")
        lines.append(":boom: *Campaign Bounce Alerts*")
        for item in sorted(r.bounce_alerts, key=lambda x: x.get("bounce_rate", 0), reverse=True):
            lines.append(
                f"• {item['campaign_name']} ({item['workspace_name']}) — "
                f"Bounce rate: {item['bounce_rate']:.1f}% "
                f"({item['bounced']}/{item['sent']} emails)"
            )

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


def _format_weekly_domain_report(r: ReportData) -> str:
    """Format the weekly domain health report."""
    lines: List[str] = []

    lines.append(":globe_with_meridians: *Instantly Email Monitor — Weekly Domain Health Report*")
    lines.append(f":calendar: {r.generated_at}")
    lines.append("")

    if not r.domain_health:
        lines.append("_No domain health data available._")
        return "\n".join(lines)

    # Sort by health score ascending (worst first)
    sorted_domains = sorted(r.domain_health, key=lambda d: d.get("avg_health", 100))

    # Domains needing attention (< 85% health)
    bad_domains = [d for d in sorted_domains if d.get("avg_health", 100) < 85]

    if bad_domains:
        lines.append(":warning: *Domains Needing Attention*")
        for d in bad_domains:
            dns_tag = ""
            dns_status = d.get("dns_status", [])
            if dns_status:
                dns_tag = f" | DNS: Missing {', '.join(dns_status)}"
            lines.append(
                f"• :red_circle: *{d['domain']}* ({d['workspace_name']})\n"
                f"   Health: {d['avg_health']:.0f}% | "
                f"Spam rate: {d['spam_rate']:.1f}% | "
                f"Accounts: {d['account_count']}{dns_tag}"
            )
        lines.append("")

    lines.append(":white_check_mark: *All Domains*")
    for d in sorted_domains:
        emoji = ":red_circle:" if d.get("avg_health", 100) < 60 else (
            ":large_orange_circle:" if d.get("avg_health", 100) < 85 else ":green_circle:"
        )
        dns_tag = ""
        dns_status = d.get("dns_status", [])
        if dns_status:
            dns_tag = f" | DNS: :x: {', '.join(dns_status)}"
        lines.append(
            f"• {emoji} `{d['domain']}` ({d['workspace_name']}) — "
            f"Health: {d['avg_health']:.0f}% | "
            f"Spam: {d['spam_rate']:.1f}% ({d.get('total_spam', 0)} of {d.get('total_inbox', 0) + d.get('total_spam', 0)}) | "
            f"{d['account_count']} account{'s' if d['account_count'] != 1 else ''}{dns_tag}"
        )

    # Summary line
    lines.append("")
    total_domains = len(sorted_domains)
    total_bad = len(bad_domains)
    if total_bad:
        lines.append(f":bar_chart: *Summary:* {total_domains} domains total, {total_bad} need attention")
    else:
        lines.append(f":bar_chart: *Summary:* {total_domains} domains — all healthy")

    return "\n".join(lines)
