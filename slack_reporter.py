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

    _SLACK_MAX_CHARS = 39000  # Slack limit is 40K; leave margin

    def _send(self, text: str) -> bool:
        """
        POST message to Slack webhook.
        Auto-splits messages that exceed Slack's character limit.
        Returns True on success, False on failure (never raises — monitoring must continue).
        """
        if len(text) <= self._SLACK_MAX_CHARS:
            return self._post(text)
        # Split on double-newline (section boundaries) to avoid breaking mid-line
        chunks = self._split_message(text)
        ok = True
        for i, chunk in enumerate(chunks):
            if i > 0:
                chunk = f"_(continued {i+1}/{len(chunks)})_\n{chunk}"
            if not self._post(chunk):
                ok = False
        return ok

    def _post(self, text: str) -> bool:
        """Single POST to Slack webhook."""
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

    @classmethod
    def _split_message(cls, text: str) -> List[str]:
        """Split a long message into chunks at section boundaries."""
        sections = text.split("\n\n")
        chunks: List[str] = []
        current = ""
        for section in sections:
            candidate = f"{current}\n\n{section}" if current else section
            if len(candidate) > cls._SLACK_MAX_CHARS and current:
                chunks.append(current)
                current = section
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text]

    # ── Public: crash / critical alerts ────────────────────────────────────────

    def send_crash_alert(self, detail: str) -> bool:
        """Send a critical/crash alert to Slack."""
        return self._send(f":fire: *Email Monitor CRASHED*\n{detail}")

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
        """Alert: single reconnect attempt starting."""
        text = (
            f":arrows_counterclockwise: *Attempting to reconnect account*\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Provider:* {provider}\n"
            f"*Time:* {_now_pht()}"
        )
        return self._send(text)

    def send_reconnect_attempting_batch(self, accounts: list, workspace_name: str) -> bool:
        """Batched alert: multiple reconnect attempts starting in one workspace."""
        if not accounts:
            return True
        if len(accounts) == 1:
            a = accounts[0]
            return self.send_reconnect_attempting(a["email"], workspace_name, a.get("provider", "ZapMail"))
        lines = [
            f":arrows_counterclockwise: *Attempting to reconnect {len(accounts)} accounts*",
            f"*Workspace:* {workspace_name}",
            f"*Time:* {_now_pht()}",
            "",
        ]
        for a in accounts:
            prov = a.get("provider", "")
            prov_tag = f" _({prov})_" if prov else ""
            lines.append(f"• {a['email']}{prov_tag}")
        return self._send("\n".join(lines))

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

    # ── Public: provider-side errors (reconnect won't help) ─────────────────

    # Presentation metadata per category — emoji, title, short cause, recovery hint.
    # Adding a new category elsewhere? Append one row here and it flows through.
    _PROVIDER_ERROR_META = {
        "sending_limit_exceeded": {
            "emoji": ":hourglass_flowing_sand:",
            "title": "Daily Sending Limit Exceeded",
            "cause": "Email provider (Gmail/Outlook) hit its per-mailbox 24-hour send cap. The account is still authenticated — this is a provider-side throttle, not a disconnection.",
            "recovery": "Auto-recovers in ~24h when the provider's rolling counter resets. No reconnect will help. If this repeats, reduce `daily_limit` for this account in Instantly or lower `DAILY_LIMIT_MAX`.",
        },
        "suspicious_activity_blocked": {
            "emoji": ":rotating_light:",
            "title": "Suspicious Activity Blocked by Provider",
            "cause": "Email provider flagged this mailbox for unusual sending. Reconnecting will NOT clear the flag.",
            "recovery": "Sign in to the mailbox directly, review any security prompts, and pause the account until the provider lifts the block. Consider re-verifying the lead list.",
        },
        "mailbox_full": {
            "emoji": ":inbox_tray:",
            "title": "Mailbox Storage Full",
            "cause": "Mailbox has exceeded its storage quota — sending is blocked until messages are cleared.",
            "recovery": "Clear or expand mailbox storage. Reconnect is not the issue.",
        },
    }

    def send_provider_error_alert(self, email: str, workspace_name: str,
                                  category: str, detail: str,
                                  response_code: Optional[int] = None) -> bool:
        """
        One-time (per 24h cooldown) alert for a provider-side error that
        reconnect cannot fix. The category must be a key in _PROVIDER_ERROR_META;
        unknown categories fall back to a generic heading.
        """
        meta = self._PROVIDER_ERROR_META.get(category, {
            "emoji": ":warning:",
            "title": category.replace("_", " ").title(),
            "cause": "Provider-side error detected on the account.",
            "recovery": "Investigate in Instantly — automatic reconnect was intentionally skipped.",
        })
        # Keep the raw SMTP text short so Slack renders cleanly.
        detail_line = detail.strip().replace("\n", " ")
        if len(detail_line) > 300:
            detail_line = detail_line[:297] + "..."
        code_tag = f" (SMTP {response_code})" if response_code else ""
        text = (
            f"{meta['emoji']} *{meta['title']}*{code_tag}\n"
            f"*Account:* {email}\n"
            f"*Workspace:* {workspace_name}\n"
            f"*Detected:* {_now_pht()}\n"
            f"*Cause:* {meta['cause']}\n"
            f"*What we did:* Skipped auto-reconnect — it would not help here.\n"
            f"*Recovery:* {meta['recovery']}\n"
            f"*Provider error:* `{detail_line}`"
        )
        ok = self._send(text)
        if ok:
            logger.info(
                "Slack provider-error alert sent for %s (%s): category=%s",
                email, workspace_name, category,
            )
        return ok

    def send_provider_error_batch(self, entries: list) -> bool:
        """
        Batched version — one Slack message grouping every provider-side
        error hit this run. Use when many accounts in the same workspace
        hit the same limit simultaneously (common with Gmail daily caps).

        entries: list of dicts {email, workspace_name, category, detail, response_code}
        """
        if not entries:
            return True
        if len(entries) == 1:
            e = entries[0]
            return self.send_provider_error_alert(
                e["email"], e["workspace_name"], e["category"],
                e.get("detail", ""), e.get("response_code"),
            )
        # Group by (workspace, category) so one section per cluster
        grouped: dict = defaultdict(list)
        for e in entries:
            grouped[(e["workspace_name"], e["category"])].append(e)

        lines = [
            f":warning: *Provider-Side Errors Detected — reconnect skipped*",
            f"*Time:* {_now_pht()}",
            "",
        ]
        for (ws_name, category), items in sorted(grouped.items()):
            meta = self._PROVIDER_ERROR_META.get(category, {
                "emoji": ":warning:",
                "title": category.replace("_", " ").title(),
                "cause": "Provider-side error.",
                "recovery": "Investigate in Instantly.",
            })
            lines.append(f"{meta['emoji']} *{meta['title']}* — workspace `{ws_name}` ({len(items)} account{'s' if len(items) != 1 else ''})")
            lines.append(f"_{meta['cause']}_")
            for it in sorted(items, key=lambda x: x["email"]):
                lines.append(f"  • {it['email']}")
            lines.append(f":point_right: {meta['recovery']}")
            lines.append("")
        return self._send("\n".join(lines))

    # ── Public: daily limit adjustments ──────────────────────────────────────

    def send_daily_limit_adjusted_batch(self, adjustments: list, max_limit: int) -> bool:
        """
        Immediate batched alert when one or more accounts had their daily_limit
        auto-adjusted down this run. One message for the whole run (grouped by
        workspace) so we never spam a message per account.
        """
        if not adjustments:
            return True
        lines = [
            f":scales: *Daily Campaign Limit Adjusted* — cap is {max_limit}",
            f"*Time:* {_now_pht()}",
            "",
        ]
        by_ws: dict = defaultdict(list)
        for a in adjustments:
            by_ws[a["workspace_name"]].append(a)
        for ws_name, items in sorted(by_ws.items()):
            lines.append(f"*Workspace: {ws_name}*")
            for item in sorted(items, key=lambda x: x["email"]):
                prev = item.get("previous_limit")
                new = item.get("new_limit")
                ok = item.get("success", True)
                tag = "" if ok else " :x: _(update failed — adjust manually in Instantly)_"
                lines.append(f"  • {item['email']} — {prev} → {new}{tag}")
        lines.append("")
        lines.append(f":information_source: _Auto-adjusted to keep sending volume at or below {max_limit}/day._")
        return self._send("\n".join(lines))

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
    def __init__(self, name: str, tool: str = "instantly"):
        self.name = name
        self.tool = tool   # "instantly" or "lemlist"
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

        # First-attempt reconnects this run (for batched Slack alert)
        self.reconnect_attempted: List[Dict] = [] # [{email, workspace_name, provider}]

        # Still disconnected after all attempts
        self.still_disconnected: List[Dict] = [] # [{email, workspace_name, provider, attempts}]

        # DNS failures
        self.dns_failures: List[Dict] = []        # [{domain, workspace_name, missing: [str]}]

        # Workspace-level errors (bad API key, network, etc.)
        self.workspace_errors: List[Dict] = []    # [{workspace_name, error}]

        # Health score alerts (low warmup health)
        # [{email, workspace_name, health_score}]
        self.health_alerts: List[Dict] = []

        # Domain health data (for weekly report)
        # [{domain, workspace_name, avg_health, account_count, dns_status}]
        self.domain_health: List[Dict] = []

        # Tracking domain issues
        # [{email, workspace_name, issue}]
        self.tracking_domain_issues: List[Dict] = []

        # Campaign bounce rate alerts
        # [{campaign_name, workspace_name, bounce_rate, bounced, sent}]
        self.bounce_alerts: List[Dict] = []

        # Accounts whose daily_limit was auto-adjusted down this run
        # [{email, workspace_name, previous_limit, new_limit, success}]
        self.daily_limit_adjustments: List[Dict] = []

        # Accounts hit by a provider-side error that reconnect can't fix
        # (sending-limit, suspicious activity, mailbox full, etc.)
        # [{email, workspace_name, category, detail, response_code,
        #   auto_recoverable, first_alert_this_run}]
        self.provider_errors: List[Dict] = []

    @property
    def total_workspaces(self) -> int:
        # Count unique base workspace names (strips "[Instantly]" / "[Lemlist]" suffixes)
        return len({w.name.split(" [")[0] for w in self.workspace_summaries})

    @property
    def total_connected(self) -> int:
        return sum(w.connected for w in self.workspace_summaries)

    @property
    def total_warmup(self) -> int:
        return sum(w.warmup for w in self.workspace_summaries if w.tool == "instantly")

    @property
    def total_paused(self) -> int:
        return sum(w.paused for w in self.workspace_summaries if w.tool == "instantly")

    @property
    def total_disconnected(self) -> int:
        return sum(w.disconnected for w in self.workspace_summaries)

    @property
    def total_dns_issues(self) -> int:
        return sum(w.dns_issues for w in self.workspace_summaries if w.tool == "instantly")


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
            if w.tool == "lemlist":
                parts = [f"{w.connected} connected"]
                parts.append(f"{w.disconnected} disconnected")
            else:
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
            reason = item.get("reason", "")
            label = {"zapmail": "ZapMail", "missioninbox": "Mission Inbox", "client": "Client account"}.get(provider, "")
            if reason:
                # Specific reason (e.g. payment pending) — show directly
                note = reason
            elif provider == "lemlist":
                note = "Lemlist account — reconnect manually in Lemlist dashboard"
            elif provider == "client":
                note = "Client account — notify client to reconnect in Instantly"
            elif provider in ("zapmail", "missioninbox") and attempts == 0:
                note = f"{label}, manual action required"
            elif attempts > 0:
                suffix = f" ({label})" if label else ""
                note = f"Reconnect failed ({attempts} attempt{'s' if attempts != 1 else ''}){suffix}"
            else:
                note = "Disconnected"
            lines.append(f"• {item['email']} ({item['workspace_name']}) — {note}")
    else:
        lines.append(":white_check_mark: *All accounts connected.*")

    # ── Provider-side errors (reconnect won't help; grouped separately) ──
    if r.provider_errors:
        lines.append("")
        lines.append(":warning: *Provider-Side Errors* _(reconnect skipped — won't help)_")
        # Group by category so users see clusters
        by_cat: dict = defaultdict(list)
        for item in r.provider_errors:
            by_cat[item.get("category", "unknown")].append(item)
        _CAT_LABELS = {
            "sending_limit_exceeded": ":hourglass_flowing_sand: Daily Sending Limit Exceeded _(auto-recovers ~24h)_",
            "suspicious_activity_blocked": ":rotating_light: Suspicious Activity Blocked _(needs human)_",
            "mailbox_full": ":inbox_tray: Mailbox Storage Full",
        }
        for cat, items in sorted(by_cat.items()):
            label = _CAT_LABELS.get(cat, f":warning: {cat.replace('_', ' ').title()}")
            lines.append(f"*{label}*")
            for item in sorted(items, key=lambda x: (x.get("workspace_name", ""), x.get("email", ""))):
                lines.append(f"  • {item['email']} ({item['workspace_name']})")

    # ── Health alerts ──
    lines.append("")
    if r.health_alerts:
        lines.append(":thermometer: *Warmup Health Alerts*")
        # Sort: mature critical first (worst actual problems), then new/warming up, then warnings
        def _sort_key(x):
            score = x.get("health_score", 100)
            is_new = bool(x.get("is_new_account"))
            # mature low-health first, then warnings, then new accounts last
            return (is_new, score)

        for item in sorted(r.health_alerts, key=_sort_key):
            score = item.get("health_score", "?")
            is_new = bool(item.get("is_new_account"))
            age_days = item.get("age_days")
            if is_new:
                emoji = ":seedling:"  # new account warming up — not a crisis
            elif score != "?" and score < 60:
                emoji = ":red_circle:"
            else:
                emoji = ":large_orange_circle:"
            age_tag = ""
            if age_days is not None:
                age_tag = f" _(age {age_days:.1f}d)_"
            action_taken = item.get("action_taken", "")
            lines.append(
                f"• {emoji} {item['email']} ({item['workspace_name']}) — "
                f"Health: {score}%{age_tag}\n"
                f"   :point_right: _{action_taken}_"
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

    # ── Bounce rate alerts (only shown when issues exist) ──
    if r.bounce_alerts:
        lines.append("")
        lines.append(":boom: *Campaign Bounce Alerts*")
        for item in sorted(r.bounce_alerts, key=lambda x: x.get("bounce_rate", 0), reverse=True):
            bounce_rate = item.get("bounce_rate", 0)
            # Severity: pause-worthy bounces get the red dot; the rest a warning.
            sev_emoji = ":red_circle:" if "Auto-paused" in item.get("action_taken", "") else ":warning:"
            reply_rate = item.get("reply_rate", 0)
            open_rate = item.get("open_rate", 0)
            action_taken = item.get("action_taken", "")
            total_leads = item.get("total_leads", 0)
            contacted = item.get("contacted", 0)
            # Only render leads progress when we actually have the total
            # (avoids the old "252/0" display when leads_count was excluded)
            if total_leads > 0:
                leads_tag = f" | Leads contacted: {contacted}/{total_leads}"
            elif contacted > 0:
                leads_tag = f" | Leads contacted: {contacted}"
            else:
                leads_tag = ""
            unsub = item.get("unsubscribed", 0)
            unsub_tag = f" | Unsubscribed: {unsub}" if unsub else ""
            lines.append(
                f"• {sev_emoji} *{item['campaign_name']}* ({item['workspace_name']})\n"
                f"   Bounce: {bounce_rate:.1f}% ({item['bounced']}/{item['sent']}) | "
                f"Opens: {item.get('opens', 0)} ({open_rate:.1f}%) | "
                f"Replies: {item.get('replies', 0)} ({reply_rate:.1f}%)"
                f"{leads_tag}{unsub_tag}\n"
                f"   :point_right: _{action_taken}_"
            )

    # ── Daily limit adjustments ──
    if r.daily_limit_adjustments:
        lines.append("")
        lines.append(":scales: *Daily Campaign Limit Adjustments*")
        by_ws_dl: dict = defaultdict(list)
        for item in r.daily_limit_adjustments:
            by_ws_dl[item["workspace_name"]].append(item)
        for ws_name, items in sorted(by_ws_dl.items()):
            lines.append(f"_{ws_name}_")
            for item in sorted(items, key=lambda x: x["email"]):
                prev = item.get("previous_limit")
                new = item.get("new_limit")
                ok = item.get("success", True)
                tag = "" if ok else " :x: _(update failed)_"
                lines.append(f"  • {item['email']} — {prev} → {new}{tag}")

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
                f"Accounts: {d['account_count']}{dns_tag}"
            )
        lines.append("")

    lines.append(":white_check_mark: *All Domains*")
    for d in sorted_domains:
        emoji = ":red_circle:" if d.get("avg_health", 100) < 60 else (
            ":large_orange_circle:" if d.get("avg_health", 100) < 85 else ":large_green_circle:"
        )
        dns_tag = ""
        dns_status = d.get("dns_status", [])
        if dns_status:
            dns_tag = f" | DNS: :x: {', '.join(dns_status)}"
        lines.append(
            f"• {emoji} `{d['domain']}` ({d['workspace_name']}) — "
            f"Health: {d['avg_health']:.0f}% | "
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
