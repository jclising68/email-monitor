"""
monitor.py — Orchestration entry point.

Usage:
  python monitor.py                  # Hourly check + auto-reconnect
  python monitor.py --report         # Daily report: run check first, then post to Slack
  python monitor.py --weekly-report  # Weekly domain health report
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

try:
    from config import Config
    _cfg = Config()
    _cfg.configure_logging()
except EnvironmentError as _env_err:
    logging.basicConfig(level=logging.INFO)
    logging.critical("Configuration error: %s", _env_err)
    sys.exit(1)

from instantly_client import InstantlyClient, InstantlyAPIError
from missioninbox_client import MissionInboxClient
from reconnect import attempt_reconnect, attempt_post_only
from sheets_client import SheetsClient
from slack_reporter import SlackReporter, ReportData, WorkspaceSummary
from state import (
    is_new_disconnection,
    should_realert,
    build_new_alert_state_row,
    build_reconnect_failed_row,
    utcnow_str,
)
from zapmail_client import ZapMailClient

logger = logging.getLogger(__name__)


def run(send_report: bool = False, send_weekly_report: bool = False) -> None:
    logger.info("=== Email Monitor starting (report=%s, weekly=%s) ===", send_report, send_weekly_report)

    sheets = SheetsClient(_cfg.google_credentials, _cfg.google_sheets_id)
    slack  = SlackReporter(_cfg.slack_webhook_url)
    report = ReportData()

    # ── Load Sheets data ──────────────────────────────────────────────────────
    try:
        workspaces = sheets.get_workspaces()
    except Exception as exc:
        logger.critical("Cannot read 'workspaces' sheet: %s", exc)
        slack._send(f":fire: *Email Monitor CRITICAL:* Cannot read workspaces sheet: {exc}")
        sys.exit(1)

    try:
        alert_state: Dict[str, Dict] = sheets.get_alert_state()
    except Exception as exc:
        logger.error("Cannot read 'alert_state' sheet: %s — proceeding without dedup.", exc)
        alert_state = {}

    # ZapMail uses one global API key + a per-workspace ID (from the sheet).
    # Mission Inbox uses a per-workspace API key (from the sheet).
    # Both clients are built inside the workspace loop below.
    zapmail_api_key = _cfg.zapmail_api_key
    if not zapmail_api_key:
        logger.debug("ZAPMAIL_API_KEY not set — ZapMail auto-reconnect disabled.")

    if not workspaces:
        logger.warning("No active workspaces found in Sheets. Nothing to do.")
        if send_report:
            slack.send_daily_report(report)
        return

    new_client_disconnections: List[Dict] = []  # batched for single Slack message

    # ── Per-workspace processing ──────────────────────────────────────────────
    for ws in workspaces:
        ws_name  = ws["workspace_name"]
        api_key  = ws["api_key"]
        logger.info("Processing workspace: %s", ws_name)

        client  = InstantlyClient(api_key, base_url=_cfg.instantly_base_url)
        summary = WorkspaceSummary(ws_name)

        # Build ZapMail client(s) for this workspace.
        # Each provider (GOOGLE, MICROSOFT) has its own workspace key in the sheet.
        # One client per provider, with a merged email set and per-email
        # client mapping so reconnect uses the correct provider + workspace key.
        zapmail_email_set: set = set()
        zapmail_email_to_client: Dict[str, ZapMailClient] = {}

        # Provider → workspace key mapping from sheet columns
        _zm_provider_keys = {
            "GOOGLE":    ws.get("zapmail_workspace_key_google", ""),
            "MICROSOFT": ws.get("zapmail_workspace_key_microsoft", ""),
        }

        if zapmail_api_key:
            for provider, wk in _zm_provider_keys.items():
                if not wk:
                    continue  # no workspace key for this provider — skip
                zm_client = ZapMailClient(
                    api_key=zapmail_api_key,
                    workspace_key=wk,
                    service_provider=provider,
                    base_url=_cfg.zapmail_api_base_url,
                )
                try:
                    zm_mailboxes = zm_client.list_mailboxes()
                    for mb in zm_mailboxes:
                        mb_email = str(mb.get("email", "")).lower().strip()
                        if mb_email and mb_email not in zapmail_email_set:
                            zapmail_email_set.add(mb_email)
                            zapmail_email_to_client[mb_email] = zm_client
                    logger.info(
                        "ZapMail [%s]: workspace '%s' has %d mailbox(es).",
                        provider, ws_name, len(zm_mailboxes),
                    )
                except Exception as exc:
                    logger.error(
                        "ZapMail [%s]: failed to list mailboxes for workspace '%s' (%s) — "
                        "ZapMail %s reconnect disabled for this workspace.",
                        provider, ws_name, exc, provider,
                    )

            if zapmail_email_set:
                active_providers = [p for p, wk in _zm_provider_keys.items() if wk]
                logger.info(
                    "ZapMail: workspace '%s' total %d unique mailbox(es) across %s.",
                    ws_name, len(zapmail_email_set), ", ".join(active_providers),
                )

        # Build a Mission Inbox client for this workspace if it has an API key configured
        missioninbox_client: Optional[MissionInboxClient] = None
        missioninbox_email_set: set = set()
        mi_api_key = ws.get("mission_inbox_api_key", "")
        if mi_api_key:
            missioninbox_client = MissionInboxClient(api_key=mi_api_key)
            try:
                mi_mailboxes = missioninbox_client.list_mailboxes()
                missioninbox_email_set = {
                    str(mb.get("email", "")).lower()
                    for mb in mi_mailboxes if mb.get("email")
                }
                logger.info(
                    "MissionInbox: workspace '%s' has %d mailbox(es).",
                    ws_name, len(missioninbox_email_set),
                )
            except Exception as exc:
                logger.error(
                    "MissionInbox: failed to list mailboxes for workspace '%s' (%s) — "
                    "MI email set empty but client kept for individual lookups.", ws_name, exc,
                )

        try:
            accounts = client.get_all_accounts()
        except InstantlyAPIError as exc:
            logger.error("Workspace %s: API error fetching accounts: %s", ws_name, exc)
            report.workspace_errors.append({"workspace_name": ws_name, "error": str(exc)})
            continue
        except Exception as exc:
            logger.error("Workspace %s: unexpected error: %s", ws_name, exc)
            report.workspace_errors.append({"workspace_name": ws_name, "error": str(exc)})
            continue

        checked_domains: set = set()  # DNS is per-domain; only check one email per domain

        for account in accounts:
            email: str = str(account.get("email", "")).strip().lower()
            if not email:
                continue

            connected = client.is_connected(account)
            paused    = not connected and client.is_paused(account)
            in_warmup = connected and client.is_warming_up(account)

            if connected:
                if in_warmup:
                    summary.warmup += 1
                else:
                    summary.connected += 1

                # Clear stale alert_state if account recovered
                if email in alert_state:
                    logger.info("Account %s (%s) recovered — clearing alert state.", email, ws_name)
                    try:
                        sheets.delete_alert_state(email)
                        del alert_state[email]
                    except Exception as exc:
                        logger.error("Failed to clear alert_state for %s: %s", email, exc)

                # DNS vitals check — one check per unique domain only
                domain = email.split("@")[-1]
                if domain and domain not in checked_domains:
                    checked_domains.add(domain)
                    try:
                        vitals = client.check_vitals(email)
                        if vitals:
                            failures = client.parse_dns_failures(vitals)
                            if failures:
                                summary.dns_issues += 1
                                report.dns_failures.append({
                                    "domain": domain,
                                    "workspace_name": ws_name,
                                    "missing": failures,
                                })
                                logger.info("DNS issues for %s: %s", domain, failures)
                    except Exception as exc:
                        logger.warning("DNS check failed for %s: %s (non-fatal)", domain, exc)

            elif paused:
                summary.paused += 1
                # Intentionally paused — skip silently, no reconnect, no alert

            else:
                summary.disconnected += 1
                provider = _resolve_provider(email, missioninbox_email_set, zapmail_email_set, missioninbox_client)

                if provider == "missioninbox":
                    _handle_missioninbox_disconnect(
                        email, ws_name, client, sheets, slack, missioninbox_client,
                        alert_state, report, _cfg,
                    )
                elif provider == "zapmail":
                    _handle_zapmail_disconnect(
                        email, ws_name,
                        zapmail_email_to_client.get(email),  # correct client for this email's provider
                        sheets, slack, alert_state, report, _cfg,
                    )
                else:
                    # Unknown provider — client-owned account we don't manage.
                    # Alert once, then re-alert every 24 hours if still disconnected.
                    is_new   = is_new_disconnection(email, alert_state)
                    re_alert = should_realert(email, alert_state, realert_hours=24)
                    if is_new or re_alert:
                        new_client_disconnections.append({"email": email, "workspace_name": ws_name})
                        existing_row = alert_state.get(email, {})
                        new_row = {
                            "email": email.lower(),
                            "workspace_name": ws_name,
                            "first_detected": existing_row.get("first_detected") or utcnow_str(),
                            "last_alerted": utcnow_str(),
                            "reconnect_attempts": 0,
                            "status": "client_disconnected",
                        }
                        try:
                            sheets.upsert_alert_state(**new_row)
                            alert_state[email] = new_row
                        except Exception as exc:
                            logger.error("Failed to write alert_state for client account %s: %s", email, exc)
                    else:
                        logger.debug(
                            "Client account %s (%s) still disconnected — alerted within 24h, silent.",
                            email, ws_name,
                        )

        # ── Tracking domain status check (data already in accounts) ────────
        for account in accounts:
            email_td = str(account.get("email", "")).strip().lower()
            if not email_td or not client.is_connected(account):
                continue
            try:
                td_issue = client.has_tracking_domain_issue(account)
                if td_issue:
                    report.tracking_domain_issues.append({
                        "email": email_td, "workspace_name": ws_name,
                        "issue": td_issue,
                    })
            except Exception:
                pass  # non-fatal — tracking domain check is best-effort

        # ── Warmup health analytics (separate from reconnect flow) ────────
        all_connected_emails = [
            str(a.get("email", "")).strip().lower()
            for a in accounts
            if client.is_connected(a) and str(a.get("email", "")).strip()
        ]

        if all_connected_emails:
            end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            try:
                warmup_agg = client.get_warmup_analytics(all_connected_emails, start_date, end_date)
                _process_warmup_health(
                    warmup_agg, ws_name, report, _cfg,
                    dns_failures_by_domain={
                        f["domain"]: f["missing"]
                        for f in report.dns_failures
                        if f.get("workspace_name") == ws_name
                    },
                )
            except Exception as exc:
                logger.warning("Warmup analytics failed for %s: %s (non-fatal)", ws_name, exc)

        # ── Campaign bounce rate check ────────────────────────────────────
        try:
            end_date_c = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start_date_c = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            campaigns = client.get_campaign_analytics(start_date_c, end_date_c)
            _process_campaign_health(campaigns, ws_name, report, _cfg)
        except Exception as exc:
            logger.warning("Campaign analytics failed for %s: %s (non-fatal)", ws_name, exc)

        report.workspace_summaries.append(summary)
        logger.info(
            "Workspace %s: %d connected, %d warmup, %d paused, %d disconnected, %d DNS issues.",
            ws_name, summary.connected, summary.warmup, summary.paused, summary.disconnected, summary.dns_issues,
        )

    # ── Send batched client disconnection alert (one message for all) ─────────
    if new_client_disconnections:
        slack.send_client_accounts_disconnected(new_client_disconnections)

    # ── Daily report ──────────────────────────────────────────────────────────
    if send_report:
        logger.info("Sending daily Slack report.")
        slack.send_daily_report(report)

    # ── Weekly domain health report ──────────────────────────────────────────
    if send_weekly_report:
        logger.info("Sending weekly domain health report.")
        slack.send_weekly_domain_report(report)

    logger.info("=== Email Monitor finished ===")


# ── Disconnect handlers ───────────────────────────────────────────────────────

def _resolve_provider(
    email: str,
    missioninbox_email_set: set,
    zapmail_email_set: set,
    missioninbox_client: Optional["MissionInboxClient"],
) -> str:
    """
    Determine provider for a disconnected account.
    Mission Inbox detection ONLY runs when the workspace has a Mission Inbox
    API key configured — workspaces without MI (e.g. Lead Assassin, StableSea)
    are not affected.

    Priority:
      1. MI email set (from list_mailboxes) — always wins over ZapMail
      2. If MI client exists but MI set is empty (API failure): single-email
         MI API lookup to prevent misidentification as ZapMail
      3. ZapMail live mailbox list
      4. Unknown — client-owned account, do not reconnect

    NOTE: Instantly's account list API does NOT return smtp_host/imap_host,
    so host-based detection is not possible. We rely on the MI API instead.
    """
    # 1. MI email set — takes priority over ZapMail
    if email in missioninbox_email_set:
        return "missioninbox"

    # 2. MI client exists but set is empty (list_mailboxes failed)?
    #    Do a single-email lookup before falling through to ZapMail.
    if missioninbox_client is not None and not missioninbox_email_set:
        try:
            creds = missioninbox_client.get_credentials(email)
            if creds:
                logger.info(
                    "MI individual lookup confirmed %s is Mission Inbox.", email
                )
                return "missioninbox"
        except Exception as exc:
            logger.warning(
                "MI individual lookup failed for %s: %s — falling through.",
                email, exc,
            )

    # 3. ZapMail
    if email in zapmail_email_set:
        return "zapmail"

    return "unknown"


def _process_warmup_health(
    warmup_agg: Dict,
    ws_name: str,
    report: ReportData,
    cfg: "Config",
    dns_failures_by_domain: Optional[Dict[str, List[str]]] = None,
) -> None:
    """
    Process warmup analytics aggregate data for one workspace.

    Fully isolated from the reconnect flow — failures here never affect
    account monitoring or auto-reconnect. Does NOT write to alert_state
    or Sheets. Does NOT send individual Slack messages.

    All health/spam issues are collected into report.health_alerts and
    report.domain_health — they surface ONLY in the daily report and
    weekly domain report. This guarantees zero Slack flooding.
    """
    dns_failures_by_domain = dns_failures_by_domain or {}

    # Per-domain aggregation
    domain_data: Dict[str, Dict] = defaultdict(lambda: {
        "total_inbox": 0, "total_spam": 0,
        "health_scores": [], "account_count": 0,
    })

    for email, agg in warmup_agg.items():
        health_score = agg.get("health_score")
        landed_inbox = agg.get("landed_inbox", 0)
        landed_spam  = agg.get("landed_spam", 0)
        total = landed_inbox + landed_spam

        # Calculate spam rate (guard against division by zero)
        spam_rate = (landed_spam / total * 100) if total > 0 else 0.0

        domain = email.split("@")[-1] if "@" in email else ""

        # Aggregate for domain report
        if domain:
            dd = domain_data[domain]
            dd["total_inbox"] += landed_inbox
            dd["total_spam"] += landed_spam
            dd["account_count"] += 1
            if health_score is not None:
                dd["health_scores"].append(health_score)

        # ── Collect health issues (shown in daily report only) ────────────
        if health_score is not None and health_score < cfg.health_score_alert_threshold:
            logger.info(
                "Low health for %s (%s): score=%d, spam_rate=%.1f%%",
                email, ws_name, health_score, spam_rate,
            )
            report.health_alerts.append({
                "email": email, "workspace_name": ws_name,
                "health_score": health_score, "spam_rate": spam_rate,
            })

        # ── Collect spam rate issues (shown in daily report only) ─────────
        elif total > 0 and spam_rate >= cfg.spam_rate_alert_threshold:
            logger.info(
                "High spam rate for %s (%s): %.1f%% (%d spam of %d total)",
                email, ws_name, spam_rate, landed_spam, total,
            )
            report.health_alerts.append({
                "email": email, "workspace_name": ws_name,
                "health_score": health_score if health_score is not None else 0,
                "spam_rate": spam_rate,
            })

    # ── Build domain health entries (always, for daily + weekly reports) ───
    for domain, dd in domain_data.items():
        total = dd["total_inbox"] + dd["total_spam"]
        avg_health = (
            sum(dd["health_scores"]) / len(dd["health_scores"])
            if dd["health_scores"] else 100.0
        )
        spam_rate = (dd["total_spam"] / total * 100) if total > 0 else 0.0
        dns_status = dns_failures_by_domain.get(domain, [])

        report.domain_health.append({
            "domain": domain,
            "workspace_name": ws_name,
            "avg_health": avg_health,
            "total_inbox": dd["total_inbox"],
            "total_spam": dd["total_spam"],
            "spam_rate": spam_rate,
            "account_count": dd["account_count"],
            "dns_status": dns_status,
        })


def _process_campaign_health(
    campaigns: List[Dict],
    ws_name: str,
    report: ReportData,
    cfg: "Config",
) -> None:
    """
    Check campaign bounce rates. Fully isolated from reconnect flow.
    Only checks active campaigns (status=1) with meaningful send volume.
    """
    for campaign in campaigns:
        status = campaign.get("campaign_status")
        if status != 1:  # only check active campaigns
            continue

        name = campaign.get("campaign_name", "Unknown")
        sent = campaign.get("emails_sent_count", 0)
        bounced = campaign.get("bounced_count", 0)

        # Need meaningful volume — at least 50 emails sent to judge bounce rate
        if sent < 50:
            continue

        bounce_rate = (bounced / sent * 100) if sent > 0 else 0.0

        if bounce_rate >= cfg.bounce_rate_alert_threshold:
            logger.warning(
                "High bounce rate for campaign '%s' (%s): %.1f%% (%d/%d)",
                name, ws_name, bounce_rate, bounced, sent,
            )
            report.bounce_alerts.append({
                "campaign_name": name,
                "workspace_name": ws_name,
                "bounce_rate": bounce_rate,
                "bounced": bounced,
                "sent": sent,
            })


def _handle_missioninbox_disconnect(
    email: str,
    ws_name: str,
    client: InstantlyClient,
    sheets: SheetsClient,
    slack: "SlackReporter",
    missioninbox_client: Optional["MissionInboxClient"],
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """Attempt auto-reconnect for a disconnected Mission Inbox account."""
    existing_row    = alert_state.get(email)
    current_attempts = int((existing_row or {}).get("reconnect_attempts", 0))

    if current_attempts >= cfg.max_reconnect_attempts:
        logger.warning(
            "Account %s (%s) has hit max reconnect attempts (%d). Skipping.",
            email, ws_name, cfg.max_reconnect_attempts,
        )
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": current_attempts,
        })
        return

    is_partial = (existing_row or {}).get("status") == "partial_reconnect_failure"

    # Fetch live credentials from Mission Inbox API
    creds = None
    if missioninbox_client:
        creds = missioninbox_client.get_credentials(email)
    if not creds:
        # No API key configured or API call failed — cannot auto-reconnect.
        # Alert once per 24h so the team knows it needs manual attention.
        is_new   = is_new_disconnection(email, alert_state)
        re_alert = should_realert(email, alert_state, realert_hours=24)
        if is_new or re_alert:
            logger.warning(
                "MissionInbox: no credentials available for %s (%s) — sending alert.",
                email, ws_name,
            )
            slack.send_manual_reconnect_alert(email, ws_name, "Mission Inbox", reconnect_attempted=False)
            new_row = build_new_alert_state_row(email, ws_name)
            if existing_row:
                new_row["first_detected"] = existing_row.get("first_detected", new_row["first_detected"])
            new_row["status"] = "missioninbox_no_api_key"
            try:
                sheets.upsert_alert_state(**new_row)
                alert_state[email] = new_row
            except Exception as exc:
                logger.error("Failed to write alert_state for MI no-key %s: %s", email, exc)
        else:
            logger.debug("MissionInbox %s (%s) — already alerted within 24h, silent.", email, ws_name)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": 0,
        })
        return

    logger.info(
        "Attempting %s reconnect for %s (%s), attempt #%d",
        "POST-only" if is_partial else "full",
        email, ws_name, current_attempts + 1,
    )
    slack.send_reconnect_attempting(email, ws_name, "Mission Inbox")

    if is_partial:
        success = attempt_post_only(client, email, creds)
        partial = not success
    else:
        success, partial = attempt_reconnect(client, email, creds)

    if success:
        logger.info("Reconnect succeeded for %s (%s).", email, ws_name)
        slack.send_reconnect_success(email, ws_name, "Mission Inbox")
        report.reconnected.append({"email": email, "workspace_name": ws_name, "provider": "missioninbox"})
        try:
            sheets.delete_alert_state(email)
            alert_state.pop(email, None)
        except Exception as exc:
            logger.error("Failed to clear alert_state after reconnect of %s: %s", email, exc)
    else:
        new_attempts = current_attempts + 1
        status       = "partial_reconnect_failure" if partial else "reconnect_failed"
        logger.warning(
            "Reconnect failed for %s (%s). Attempts: %d. Status: %s",
            email, ws_name, new_attempts, status,
        )
        slack.send_reconnect_failed(email, ws_name, "Mission Inbox", new_attempts, cfg.max_reconnect_attempts)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": new_attempts,
        })
        new_row = build_reconnect_failed_row(email, ws_name, existing_row, new_attempts)
        new_row["status"] = status
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for %s: %s", email, exc)


def _handle_zapmail_disconnect(
    email: str,
    ws_name: str,
    zapmail_client: Optional[ZapMailClient],
    sheets: SheetsClient,
    slack: SlackReporter,
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """
    Attempt ZapMail auto-reconnect via the ZapMail export API.
    Falls back to a one-time Slack alert if no client is configured or reconnect fails.
    """
    existing_row     = alert_state.get(email)
    current_attempts = int((existing_row or {}).get("reconnect_attempts", 0))

    # ── Try auto-reconnect ────────────────────────────────────────────────────
    if zapmail_client and current_attempts < cfg.max_reconnect_attempts:
        logger.info(
            "Attempting ZapMail reconnect for %s (%s), attempt #%d",
            email, ws_name, current_attempts + 1,
        )
        slack.send_reconnect_attempting(email, ws_name, "ZapMail")
        success = zapmail_client.reconnect_email(email)

        if success:
            logger.info("ZapMail reconnect succeeded for %s (%s).", email, ws_name)
            slack.send_reconnect_success(email, ws_name, "ZapMail")
            report.reconnected.append({"email": email, "workspace_name": ws_name, "provider": "zapmail"})
            try:
                sheets.delete_alert_state(email)
                alert_state.pop(email, None)
            except Exception as exc:
                logger.error("Failed to clear alert_state after ZapMail reconnect of %s: %s", email, exc)
            return  # done — do NOT add to still_disconnected

        # Reconnect failed — track attempts
        new_attempts = current_attempts + 1
        logger.warning(
            "ZapMail reconnect failed for %s (%s). Attempt %d/%d.",
            email, ws_name, new_attempts, cfg.max_reconnect_attempts,
        )
        slack.send_reconnect_failed(email, ws_name, "ZapMail", new_attempts, cfg.max_reconnect_attempts)
        new_row = build_reconnect_failed_row(email, ws_name, existing_row, new_attempts)
        new_row["status"] = "zapmail_reconnect_failed"
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for ZapMail %s: %s", email, exc)

        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "zapmail", "attempts": new_attempts,
        })
        return

    # ── No client or max attempts reached — fall back to alert-once ──────────
    is_new      = is_new_disconnection(email, alert_state)
    should_real = should_realert(email, alert_state, cfg.zapmail_realert_hours)

    if is_new or should_real:
        no_client_reason = (
            "ZapMail API not configured" if not zapmail_client
            else f"max reconnect attempts ({cfg.max_reconnect_attempts}) reached"
        )
        logger.info(
            "Sending ZapMail alert for %s (%s) — %s.", email, ws_name, no_client_reason
        )
        slack.send_zapmail_alert(email, ws_name, reconnect_attempted=False)

        new_row = build_new_alert_state_row(email, ws_name)
        if existing_row:
            new_row["first_detected"]     = existing_row.get("first_detected", new_row["first_detected"])
            new_row["reconnect_attempts"] = existing_row.get("reconnect_attempts", 0)
        new_row["status"] = "zapmail_disconnected"
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for ZapMail %s: %s", email, exc)
    else:
        logger.debug("ZapMail %s (%s) still disconnected — already alerted, silent.", email, ws_name)

    report.still_disconnected.append({
        "email": email, "workspace_name": ws_name,
        "provider": "zapmail", "attempts": current_attempts,
    })


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Instantly email account monitor")
    parser.add_argument(
        "--report", action="store_true",
        help="Run checks then send the daily Slack summary report",
    )
    parser.add_argument(
        "--weekly-report", action="store_true",
        help="Run checks then send the weekly domain health report",
    )
    args = parser.parse_args()
    try:
        run(send_report=args.report, send_weekly_report=args.weekly_report)
    except Exception as exc:
        logger.critical("Unhandled exception — monitor crashed: %s", exc, exc_info=True)
        try:
            SlackReporter(_cfg.slack_webhook_url)._send(
                f":fire: *Email Monitor CRASHED*\n"
                f"Unhandled error: `{exc}`\n"
                f"Check GitHub Actions logs for full traceback."
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
