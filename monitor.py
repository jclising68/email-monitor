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
        # ZapMail uses the SAME workspace key for both Google and Microsoft —
        # the x-service-provider header determines which mailboxes are returned.
        # Microsoft column in sheet is optional: if present, uses that key;
        # if empty but Google key exists, reuses Google key for Microsoft too.
        zapmail_email_set: set = set()
        zapmail_email_to_client: Dict[str, ZapMailClient] = {}

        zm_key_google = ws.get("zapmail_workspace_key_google", "")
        zm_key_microsoft = ws.get("zapmail_workspace_key_microsoft", "")

        # Build provider → workspace key mapping.
        # Microsoft reuses Google key if its own column is empty.
        _zm_provider_keys: Dict[str, str] = {}
        if zm_key_google:
            _zm_provider_keys["GOOGLE"] = zm_key_google
            # Automatically try Microsoft with same key (ZapMail uses same workspace key)
            _zm_provider_keys["MICROSOFT"] = zm_key_microsoft or zm_key_google

        if zapmail_api_key:
            for provider, wk in _zm_provider_keys.items():
                zm_client = ZapMailClient(
                    api_key=zapmail_api_key,
                    workspace_key=wk,
                    service_provider=provider,
                    base_url=_cfg.zapmail_api_base_url,
                )
                try:
                    zm_mailboxes = zm_client.list_mailboxes()
                    if not zm_mailboxes:
                        logger.debug(
                            "ZapMail [%s]: workspace '%s' has 0 mailboxes — skipping.",
                            provider, ws_name,
                        )
                        continue
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
                    # Microsoft may not exist for all workspaces — only log as
                    # error if it was explicitly configured in the sheet.
                    if provider == "MICROSOFT" and not zm_key_microsoft:
                        logger.debug(
                            "ZapMail [MICROSOFT]: no Microsoft mailboxes for '%s' (expected).",
                            ws_name,
                        )
                    else:
                        logger.error(
                            "ZapMail [%s]: failed to list mailboxes for workspace '%s' (%s) — "
                            "ZapMail %s reconnect disabled for this workspace.",
                            provider, ws_name, exc, provider,
                        )

            if zapmail_email_set:
                active_providers = [p for p in _zm_provider_keys if p in
                    {zapmail_email_to_client[e]._service_provider for e in zapmail_email_set
                     if e in zapmail_email_to_client}]
                logger.info(
                    "ZapMail: workspace '%s' total %d unique mailbox(es) across %s.",
                    ws_name, len(zapmail_email_set),
                    ", ".join(active_providers) if active_providers else "GOOGLE",
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

                # Clear alert_state if account recovered
                if email in alert_state:
                    prev_status = alert_state[email].get("status", "")
                    if prev_status == "reconnect_pending":
                        # Reconnect from last run actually worked — confirm it now
                        # Detect provider from email sets since sheet doesn't store it
                        if email in missioninbox_email_set:
                            prov, prov_label = "missioninbox", "Mission Inbox"
                        elif email in zapmail_email_set:
                            prov, prov_label = "zapmail", "ZapMail"
                        else:
                            prov, prov_label = "", ""
                        logger.info("Account %s (%s) confirmed recovered after reconnect.", email, ws_name)
                        if prov_label:
                            slack.send_reconnect_success(email, ws_name, prov_label)
                        report.reconnected.append({
                            "email": email, "workspace_name": ws_name,
                            "provider": prov,
                        })
                    else:
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
                    # Track in alert_state and show in daily report.
                    is_new = is_new_disconnection(email, alert_state)
                    if is_new:
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
                    # Always add to report so it shows in daily report
                    report.still_disconnected.append({
                        "email": email, "workspace_name": ws_name,
                        "provider": "client", "attempts": 0,
                    })

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

        # ── Warmup health (extracted from account data, no extra API call) ─
        connected_accounts = [
            a for a in accounts if client.is_connected(a)
        ]
        if connected_accounts:
            try:
                warmup_health = client.extract_warmup_health(connected_accounts)
                _process_warmup_health(
                    warmup_health, ws_name, client, report, _cfg,
                    dns_failures_by_domain={
                        f["domain"]: f["missing"]
                        for f in report.dns_failures
                        if f.get("workspace_name") == ws_name
                    },
                )
            except Exception as exc:
                logger.warning("Warmup health processing failed for %s: %s (non-fatal)", ws_name, exc)

        # ── Campaign bounce rate check ────────────────────────────────────
        try:
            end_date_c = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            start_date_c = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
            campaigns = client.get_campaign_analytics(start_date_c, end_date_c)
            _process_campaign_health(campaigns, ws_name, client, report, _cfg)
        except Exception as exc:
            logger.warning("Campaign analytics failed for %s: %s (non-fatal)", ws_name, exc)

        report.workspace_summaries.append(summary)
        logger.info(
            "Workspace %s: %d connected, %d warmup, %d paused, %d disconnected, %d DNS issues.",
            ws_name, summary.connected, summary.warmup, summary.paused, summary.disconnected, summary.dns_issues,
        )

    # ── Client disconnections go in daily report (no real-time alert) ─────────
    # new_client_disconnections is already tracked in report via still_disconnected

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
    warmup_health: Dict,
    ws_name: str,
    client: "InstantlyClient",
    report: ReportData,
    cfg: "Config",
    dns_failures_by_domain: Optional[Dict[str, List[str]]] = None,
) -> None:
    """
    Process warmup health scores from account data for one workspace.

    Uses stat_warmup_score (0-100) from the account list — no extra API call.
    Fully isolated from the reconnect flow.

    Automated actions:
      - Health < 60% (critical): auto-pauses the account in Instantly
      - Health 60-80% (warning): alert only in daily report

    All health issues are collected into report.health_alerts and
    report.domain_health — they surface in the daily/weekly report.
    """
    dns_failures_by_domain = dns_failures_by_domain or {}

    # Per-domain aggregation
    domain_data: Dict[str, Dict] = defaultdict(lambda: {
        "health_scores": [], "account_count": 0,
    })

    for email, data in warmup_health.items():
        health_score = data.get("health_score")
        if health_score is None:
            continue

        domain = email.split("@")[-1] if "@" in email else ""

        # Aggregate for domain report
        if domain:
            dd = domain_data[domain]
            dd["account_count"] += 1
            dd["health_scores"].append(health_score)

        # ── Health below threshold — collect + auto-act ───────────────────
        if health_score < cfg.health_score_alert_threshold:
            is_critical = health_score < cfg.health_score_critical_threshold
            action_taken = ""

            if is_critical:
                # Auto-pause the account to protect domain reputation
                try:
                    paused = client.pause_account(email)
                    if paused:
                        action_taken = "Auto-paused in Instantly"
                        logger.warning(
                            "Auto-paused %s (%s) — health score %d%% is critical.",
                            email, ws_name, health_score,
                        )
                    else:
                        action_taken = "Auto-pause failed — pause manually in Instantly"
                        logger.error("Failed to auto-pause %s (%s).", email, ws_name)
                except Exception as exc:
                    action_taken = "Auto-pause failed — pause manually in Instantly"
                    logger.error("Error auto-pausing %s (%s): %s", email, ws_name, exc)
            else:
                action_taken = "Monitor closely — reduce send volume if it drops further"

            logger.info(
                "Low health for %s (%s): score=%d — %s",
                email, ws_name, health_score, action_taken,
            )
            report.health_alerts.append({
                "email": email, "workspace_name": ws_name,
                "health_score": health_score,
                "action_taken": action_taken,
            })

    # ── Build domain health entries (always, for daily + weekly reports) ───
    for domain, dd in domain_data.items():
        avg_health = (
            sum(dd["health_scores"]) / len(dd["health_scores"])
            if dd["health_scores"] else 100.0
        )
        dns_status = dns_failures_by_domain.get(domain, [])

        report.domain_health.append({
            "domain": domain,
            "workspace_name": ws_name,
            "avg_health": avg_health,
            "account_count": dd["account_count"],
            "dns_status": dns_status,
        })


def _process_campaign_health(
    campaigns: List[Dict],
    ws_name: str,
    client: "InstantlyClient",
    report: ReportData,
    cfg: "Config",
) -> None:
    """
    Check campaign bounce rates. Fully isolated from reconnect flow.
    Only checks active campaigns (status=1) with meaningful send volume.

    Automated actions:
      - Bounce ≥ 10%: auto-pauses the campaign in Instantly
      - Bounce 5-10%: alert only in daily report
    """
    for campaign in campaigns:
        status = campaign.get("campaign_status")
        if status != 1:  # only check active campaigns
            continue

        name = campaign.get("campaign_name", "Unknown")
        campaign_id = campaign.get("campaign_id", "")
        sent = campaign.get("emails_sent_count", 0)
        bounced = campaign.get("bounced_count", 0)
        contacted = campaign.get("contacted_count", 0)
        total_leads = campaign.get("leads_count", 0)
        replies = campaign.get("reply_count_unique", campaign.get("reply_count", 0))

        # Need meaningful volume — at least 50 emails sent to judge bounce rate
        if sent < 50:
            continue

        bounce_rate = (bounced / sent * 100) if sent > 0 else 0.0

        if bounce_rate >= cfg.bounce_rate_alert_threshold:
            reply_rate = (replies / sent * 100) if sent > 0 else 0.0
            action_taken = ""

            # Auto-pause campaigns with bounce ≥ 10%
            if bounce_rate >= 10 and campaign_id:
                try:
                    paused = client.pause_campaign(campaign_id)
                    if paused:
                        action_taken = "Auto-paused campaign — re-verify lead list before resuming"
                        logger.warning(
                            "Auto-paused campaign '%s' (%s) — bounce rate %.1f%% exceeds 10%%.",
                            name, ws_name, bounce_rate,
                        )
                    else:
                        action_taken = "Auto-pause failed — pause manually in Instantly and re-verify lead list"
                        logger.error("Failed to auto-pause campaign '%s' (%s).", name, ws_name)
                except Exception as exc:
                    action_taken = "Auto-pause failed — pause manually in Instantly and re-verify lead list"
                    logger.error("Error auto-pausing campaign '%s' (%s): %s", name, ws_name, exc)
            else:
                action_taken = "Review lead list quality — consider re-verifying emails before sending more"

            logger.warning(
                "High bounce rate for campaign '%s' (%s): %.1f%% (%d/%d) — %s",
                name, ws_name, bounce_rate, bounced, sent, action_taken,
            )
            report.bounce_alerts.append({
                "campaign_name": name,
                "workspace_name": ws_name,
                "bounce_rate": bounce_rate,
                "bounced": bounced,
                "sent": sent,
                "contacted": contacted,
                "total_leads": total_leads,
                "reply_rate": reply_rate,
                "replies": replies,
                "action_taken": action_taken,
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

    # ── If last reconnect is pending confirmation, check if it failed ────
    if existing_row and existing_row.get("status") == "reconnect_pending":
        new_attempts = current_attempts + 1
        logger.warning(
            "MI reconnect for %s (%s) did not hold — still disconnected. "
            "Attempt %d/%d.", email, ws_name, new_attempts, cfg.max_reconnect_attempts,
        )
        new_row = build_reconnect_failed_row(email, ws_name, existing_row, new_attempts)
        new_row["status"] = "reconnect_failed"
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for MI pending %s: %s", email, exc)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": new_attempts,
        })
        return

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
    if current_attempts == 0:
        slack.send_reconnect_attempting(email, ws_name, "Mission Inbox")

    if is_partial:
        success = attempt_post_only(client, email, creds)
        partial = not success
    else:
        success, partial = attempt_reconnect(client, email, creds)

    if success:
        # Don't celebrate yet — mark as pending and verify on NEXT run.
        logger.info(
            "MI reconnect API succeeded for %s (%s) — will verify on next run.",
            email, ws_name,
        )
        new_row = build_reconnect_failed_row(email, ws_name, existing_row, current_attempts + 1)
        new_row["status"] = "reconnect_pending"
        try:
            sheets.upsert_alert_state(**new_row)
            # Extra fields for in-memory use only (not written to sheet)
            new_row["provider"] = "missioninbox"
            new_row["provider_label"] = "Mission Inbox"
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for MI pending %s: %s", email, exc)
    else:
        new_attempts = current_attempts + 1
        status       = "partial_reconnect_failure" if partial else "reconnect_failed"
        logger.warning(
            "Reconnect failed for %s (%s). Attempts: %d. Status: %s",
            email, ws_name, new_attempts, status,
        )
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

    # ── If last reconnect is pending confirmation, check if it failed ────────
    if existing_row and existing_row.get("status") == "reconnect_pending":
        # Account is STILL disconnected after last run's "successful" reconnect.
        # The reconnect didn't actually work. Count it as a failed attempt.
        new_attempts = current_attempts + 1
        logger.warning(
            "ZapMail reconnect for %s (%s) did not hold — still disconnected. "
            "Attempt %d/%d.", email, ws_name, new_attempts, cfg.max_reconnect_attempts,
        )
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

    # ── Try auto-reconnect ────────────────────────────────────────────────────
    if zapmail_client and current_attempts < cfg.max_reconnect_attempts:
        logger.info(
            "Attempting ZapMail reconnect for %s (%s), attempt #%d",
            email, ws_name, current_attempts + 1,
        )
        if current_attempts == 0:
            slack.send_reconnect_attempting(email, ws_name, "ZapMail")
        success, permanent_failure = zapmail_client.reconnect_email(email)

        if success:
            # Don't celebrate yet — mark as pending and verify on NEXT run.
            logger.info(
                "ZapMail export succeeded for %s (%s) — will verify on next run.",
                email, ws_name,
            )
            new_row = build_reconnect_failed_row(email, ws_name, existing_row, current_attempts + 1)
            new_row["status"] = "reconnect_pending"
            try:
                sheets.upsert_alert_state(**new_row)
                new_row["provider"] = "zapmail"
                new_row["provider_label"] = "ZapMail"
                alert_state[email] = new_row
            except Exception as exc:
                logger.error("Failed to write alert_state for ZapMail pending %s: %s", email, exc)
            return  # wait for next run to confirm

        # Reconnect failed
        if permanent_failure:
            # Permanent error (e.g. invalid credentials) — skip to max so we don't
            # waste 5 retries on something that needs manual ZapMail fix.
            new_attempts = cfg.max_reconnect_attempts
            logger.warning(
                "ZapMail permanent failure for %s (%s) — skipping to max attempts. "
                "Fix the Instantly credentials in ZapMail.", email, ws_name,
            )
        else:
            new_attempts = current_attempts + 1

        logger.warning(
            "ZapMail reconnect failed for %s (%s). Attempt %d/%d.",
            email, ws_name, new_attempts, cfg.max_reconnect_attempts,
        )
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
