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
from zoneinfo import ZoneInfo

_PHT = ZoneInfo("Asia/Manila")

try:
    from config import Config
    _cfg = Config()
    _cfg.configure_logging()
except EnvironmentError as _env_err:
    logging.basicConfig(level=logging.INFO)
    logging.critical("Configuration error: %s", _env_err)
    sys.exit(1)

from instantly_client import InstantlyClient, InstantlyAPIError
from lemlist_client import LemlistClient
from missioninbox_client import MissionInboxClient
from premiuminbox_client import PremiumInboxClient
from reconnect import attempt_reconnect, attempt_post_only
from scaledmail_client import ScaledMailClient
from sheets_client import SheetsClient
from slack_reporter import SlackReporter, ReportData, WorkspaceSummary
from smartlead_client import SmartleadClient
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

    # ── Resolve credentials from the Sheet's 'Settings' tab ───────────────────
    # A non-empty Sheet value wins over the env var / secret. This is how a
    # non-technical client configures the system: paste a webhook / API key into
    # a cell, no code or GitHub-secret changes needed.
    try:
        _cfg.apply_overrides(sheets.get_settings())
    except Exception as exc:
        logger.warning(
            "Could not read the 'Settings' tab (%s) — falling back to env vars / defaults.", exc,
        )

    if not _cfg.slack_webhook_url:
        logger.critical(
            "No Slack webhook configured. Add it to the Google Sheet 'Settings' tab "
            "(row 'slack_webhook_url'), or set the SLACK_WEBHOOK_URL secret."
        )
        sys.exit(1)

    slack  = SlackReporter(_cfg.slack_webhook_url)
    report = ReportData()

    # ── Load Sheets data ──────────────────────────────────────────────────────
    try:
        workspaces = sheets.get_workspaces()
    except Exception as exc:
        logger.critical("Cannot read 'workspaces' sheet: %s", exc)
        slack.send_crash_alert(f"Cannot read workspaces sheet: {exc}")
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
            _send_daily_report_once(sheets, slack, report)
        return

    # ── Per-workspace processing ──────────────────────────────────────────────
    for ws in workspaces:
        ws_name  = ws["workspace_name"]
        api_key  = ws["api_key"]
        logger.info("Processing workspace: %s", ws_name)

        # Workspaces with no Instantly api_key skip Instantly processing entirely
        # and are handled by their sending-platform processor(s) directly.
        if not api_key:
            lemlist_api_key   = ws.get("lemlist_api_key", "")
            smartlead_api_key = ws.get("smartlead_api_key", "")
            if lemlist_api_key:
                _process_lemlist_workspace(ws_name, lemlist_api_key, report, sheets, alert_state, _cfg)
            if smartlead_api_key:
                _process_smartlead_workspace(ws_name, smartlead_api_key, report, sheets, alert_state, _cfg)
            continue

        client  = InstantlyClient(api_key, base_url=_cfg.instantly_base_url)
        summary = WorkspaceSummary(ws_name)

        # Build ZapMail client(s) for this workspace.
        # ZapMail uses the SAME workspace key for both Google and Microsoft —
        # the x-service-provider header determines which mailboxes are returned.
        # Microsoft column in sheet is optional: if present, uses that key;
        # if empty but Google key exists, reuses Google key for Microsoft too.
        zapmail_email_set: set = set()
        zapmail_email_to_client: Dict[str, ZapMailClient] = {}
        zapmail_email_to_mailbox: Dict[str, Dict] = {}  # cached mailbox data (incl. IDs)

        zm_key_google = ws.get("zapmail_workspace_key_google", "")
        zm_key_microsoft = ws.get("zapmail_workspace_key_microsoft", "")

        _zm_provider_keys: Dict[str, str] = {}
        if zm_key_google:
            _zm_provider_keys["GOOGLE"] = zm_key_google
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
                            zapmail_email_to_mailbox[mb_email] = mb  # cache full mailbox object
                    logger.info(
                        "ZapMail [%s]: workspace '%s' has %d mailbox(es).",
                        provider, ws_name, len(zm_mailboxes),
                    )
                except Exception as exc:
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
                logger.info(
                    "ZapMail: workspace '%s' total %d unique mailbox(es).",
                    ws_name, len(zapmail_email_set),
                )

        # Detect ZapMail billing issues via subscription API + response inspection
        zapmail_billing_issue: Optional[str] = None
        _checked_zm_clients: set = set()
        for zm_c in zapmail_email_to_client.values():
            if id(zm_c) in _checked_zm_clients:
                continue
            _checked_zm_clients.add(id(zm_c))
            # 1. Check subscription status via dedicated API endpoint
            if not zapmail_billing_issue:
                sub_issue = zm_c.check_subscription_status()
                if sub_issue:
                    zapmail_billing_issue = sub_issue
            # 2. Also check if list_mailboxes response had billing indicators
            if not zapmail_billing_issue and zm_c.workspace_billing_status:
                zapmail_billing_issue = zm_c.workspace_billing_status
        if zapmail_billing_issue:
            logger.warning(
                "ZapMail billing issue for workspace '%s': %s",
                ws_name, zapmail_billing_issue,
            )

        # Build a Mission Inbox client for this workspace if it has an API key configured
        missioninbox_client: Optional[MissionInboxClient] = None
        missioninbox_email_set: set = set()
        missioninbox_billing_issue: Optional[str] = None
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
                # Check for billing issues
                if missioninbox_client.workspace_billing_status:
                    missioninbox_billing_issue = missioninbox_client.workspace_billing_status
                    logger.warning(
                        "MissionInbox billing issue for workspace '%s': %s",
                        ws_name, missioninbox_billing_issue,
                    )
            except Exception as exc:
                logger.error(
                    "MissionInbox: failed to list mailboxes for workspace '%s' (%s) — "
                    "MI email set empty but client kept for individual lookups.", ws_name, exc,
                )

        # Build a Premium Inboxes client if a global token + workspace id are set.
        # PHASE 1: used for disconnection alerting + billing detection only —
        # auto-reconnect is not implemented yet (needs the portal API docs).
        premiuminbox_client: Optional[PremiumInboxClient] = None
        premiuminbox_email_set: set = set()
        premiuminbox_billing_issue: Optional[str] = None
        pi_ws_id = ws.get("premiuminbox_workspace_id", "")
        if pi_ws_id and _cfg.premiuminbox_api_token:
            premiuminbox_client = PremiumInboxClient(
                api_token=_cfg.premiuminbox_api_token,
                workspace_id=pi_ws_id,
                base_url=_cfg.premiuminbox_api_base_url,
            )
            try:
                pi_accounts = premiuminbox_client.list_email_accounts()
                premiuminbox_email_set = {
                    str(mb.get("email", "")).lower() for mb in pi_accounts if mb.get("email")
                }
                logger.info("PremiumInbox: workspace '%s' has %d mailbox(es).", ws_name, len(premiuminbox_email_set))
                premiuminbox_billing_issue = (
                    premiuminbox_client.check_subscription_status()
                    or premiuminbox_client.workspace_billing_status
                )
            except Exception as exc:
                logger.error("PremiumInbox: failed to list mailboxes for workspace '%s' (%s).", ws_name, exc)

        # Build a ScaledMail client if a global key + organization id are set.
        # PHASE 1: disconnection alerting + billing detection only.
        scaledmail_client: Optional[ScaledMailClient] = None
        scaledmail_email_set: set = set()
        scaledmail_billing_issue: Optional[str] = None
        sm_org_id = ws.get("scaledmail_organization_id", "")
        if sm_org_id and _cfg.scaledmail_api_key:
            scaledmail_client = ScaledMailClient(
                api_key=_cfg.scaledmail_api_key,
                organization_id=sm_org_id,
                base_url=_cfg.scaledmail_api_base_url,
            )
            try:
                sm_mailboxes = scaledmail_client.list_mailboxes()
                scaledmail_email_set = {
                    str(mb.get("email", "")).lower() for mb in sm_mailboxes if mb.get("email")
                }
                logger.info("ScaledMail: workspace '%s' has %d mailbox(es).", ws_name, len(scaledmail_email_set))
                scaledmail_billing_issue = (
                    scaledmail_client.check_billing() or scaledmail_client.workspace_billing_status
                )
            except Exception as exc:
                logger.error("ScaledMail: failed to list mailboxes for workspace '%s' (%s).", ws_name, exc)

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
        connected_accounts: List[Dict] = []  # collected in main loop for warmup health

        for account in accounts:
            email: str = str(account.get("email", "")).strip().lower()
            if not email:
                continue

            # ── Daily campaign limit cap ──────────────────────────────────
            # Any account with daily_limit above the configured cap gets
            # auto-adjusted back down. Runs for every account regardless of
            # status — a paused/disconnected account can still carry a high
            # limit that applies when it resumes.
            raw_limit = account.get("daily_limit")
            if raw_limit is not None:
                try:
                    current_limit = int(raw_limit)
                except (TypeError, ValueError):
                    current_limit = None
                if current_limit is not None and current_limit > _cfg.daily_limit_max:
                    try:
                        ok = client.update_daily_limit(email, _cfg.daily_limit_max)
                    except Exception as exc:
                        ok = False
                        logger.error(
                            "Error updating daily_limit for %s (%s): %s",
                            email, ws_name, exc,
                        )
                    report.daily_limit_adjustments.append({
                        "email": email,
                        "workspace_name": ws_name,
                        "previous_limit": current_limit,
                        "new_limit": _cfg.daily_limit_max,
                        "success": ok,
                    })

            connected = client.is_connected(account)
            paused    = not connected and client.is_paused(account)
            in_warmup = connected and client.is_warming_up(account)

            if connected:
                connected_accounts.append(account)
                if in_warmup:
                    summary.warmup += 1
                else:
                    summary.connected += 1

                # Clear alert_state if account recovered
                if email in alert_state:
                    prev_status = alert_state[email].get("status", "")
                    if prev_status == "reconnect_pending":
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

                # Tracking domain check (data already in account object, no API call)
                try:
                    td_issue = client.has_tracking_domain_issue(account)
                    if td_issue:
                        report.tracking_domain_issues.append({
                            "email": email, "workspace_name": ws_name,
                            "issue": td_issue,
                        })
                except Exception:
                    pass

            elif paused:
                summary.paused += 1

            else:
                summary.disconnected += 1

                # ── Classify the error FIRST ──────────────────────────────────
                # Some "disconnected" states are actually provider throttles
                # (Gmail 5.4.5 daily limit, suspicious-activity blocks, etc.).
                # For those categories reconnecting is pointless — it wastes
                # API calls, emits misleading Slack alerts, and the account
                # would still be blocked after a successful reconnect.
                err_info = InstantlyClient.classify_account_error(account)
                if not err_info["should_reconnect"] and err_info["category"] not in ("connected", "paused"):
                    _handle_non_reconnectable_error(
                        email, ws_name, err_info, sheets, slack,
                        alert_state, report, _cfg,
                    )
                    continue

                provider = _resolve_provider(
                    email, missioninbox_email_set, zapmail_email_set, missioninbox_client,
                    premiuminbox_email_set, scaledmail_email_set,
                )

                if provider == "missioninbox":
                    _handle_missioninbox_disconnect(
                        email, ws_name, client, sheets, slack, missioninbox_client,
                        missioninbox_billing_issue,
                        alert_state, report, _cfg,
                    )
                elif provider == "zapmail":
                    _handle_zapmail_disconnect(
                        email, ws_name,
                        zapmail_email_to_client.get(email),
                        zapmail_email_to_mailbox.get(email),
                        zapmail_billing_issue,
                        sheets, slack, alert_state, report, _cfg,
                    )
                elif provider == "premiuminbox":
                    _handle_infra_provider_disconnect(
                        "premiuminbox", "Premium Inboxes", premiuminbox_client,
                        email, ws_name, premiuminbox_billing_issue,
                        sheets, alert_state, report, _cfg,
                    )
                elif provider == "scaledmail":
                    _handle_infra_provider_disconnect(
                        "scaledmail", "ScaledMail", scaledmail_client,
                        email, ws_name, scaledmail_billing_issue,
                        sheets, alert_state, report, _cfg,
                    )
                else:
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
                    report.still_disconnected.append({
                        "email": email, "workspace_name": ws_name,
                        "provider": "client", "attempts": 0,
                    })

        # ── Send batched reconnect alert (one message for all first-attempts) ──
        first_attempt_emails = [
            e for e in report.reconnect_attempted
            if e.get("workspace_name") == ws_name
        ]
        if first_attempt_emails:
            slack.send_reconnect_attempting_batch(first_attempt_emails, ws_name)

        # ── Warmup health (extracted from account data, no extra API call) ─
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

        # ── Signature check (read from account data already fetched — no extra API call) ──
        report.signature_checked_by_ws[ws_name] = len(accounts)
        for _acct in accounts:
            _email = str(_acct.get("email", "")).strip().lower()
            if not _email:
                continue
            if not str(_acct.get("signature") or "").strip():
                report.signature_issues.append({"email": _email, "workspace_name": ws_name})

        # ── Lemlist / Smartlead processing (if also configured on this workspace) ──
        lemlist_api_key   = ws.get("lemlist_api_key", "")
        smartlead_api_key = ws.get("smartlead_api_key", "")
        if lemlist_api_key or smartlead_api_key:
            # Rename Instantly summary so the tool is clear in the report
            summary.name = f"{ws_name} [Instantly]"
        if lemlist_api_key:
            _process_lemlist_workspace(ws_name, lemlist_api_key, report, sheets, alert_state, _cfg)
        if smartlead_api_key:
            _process_smartlead_workspace(ws_name, smartlead_api_key, report, sheets, alert_state, _cfg)

        report.workspace_summaries.append(summary)
        logger.info(
            "Workspace %s: %d connected, %d warmup, %d paused, %d disconnected, %d DNS issues.",
            ws_name, summary.connected, summary.warmup, summary.paused, summary.disconnected, summary.dns_issues,
        )

    # ── Daily limit adjustments — immediate batched alert ────────────────────
    # Only sends if we actually adjusted anything this run. Self-regulating:
    # once an account is capped, next run sees it at the cap and stays silent.
    if report.daily_limit_adjustments:
        slack.send_daily_limit_adjusted_batch(
            report.daily_limit_adjustments, _cfg.daily_limit_max,
        )

    # ── Provider-side errors — one batched Slack message per run ─────────────
    # Per-account 24h dedup already happened inside _handle_non_reconnectable_error
    # (entries with first_alert_this_run=False stay out of the batch). So when
    # many Gmail mailboxes hit the 24h cap in the same run they produce ONE
    # grouped Slack message instead of one message per account.
    fresh_provider_errors = [
        e for e in report.provider_errors if e.get("first_alert_this_run")
    ]
    if fresh_provider_errors:
        slack.send_provider_error_batch(fresh_provider_errors)

    # ── Daily report (guarded: once per PHT calendar day) ────────────────────
    if send_report:
        _send_daily_report_once(sheets, slack, report)

    # ── Weekly report (guarded: once per ISO week) ───────────────────────────
    if send_weekly_report:
        _send_weekly_report_once(sheets, slack, report)

    logger.info("=== Email Monitor finished ===")


# ── Report idempotency ───────────────────────────────────────────────────────

_META_KEY_DAILY  = "last_daily_report_date"
_META_KEY_WEEKLY = "last_weekly_report_week"


def _send_daily_report_once(sheets: SheetsClient, slack: SlackReporter,
                            report: ReportData) -> None:
    """
    Send the daily Slack report — but only once per PHT calendar day.
    Subsequent --report invocations the same day (delayed cron retry,
    manual workflow_dispatch, etc.) are silent no-ops.
    """
    today_pht = datetime.now(_PHT).strftime("%Y-%m-%d")
    try:
        last_sent = sheets.get_meta(_META_KEY_DAILY)
    except Exception as exc:
        logger.warning("Could not read daily-report meta (%s) — will send anyway.", exc)
        last_sent = None
    if last_sent == today_pht:
        logger.info("Daily report already sent today (%s) — skipping duplicate.", today_pht)
        return
    logger.info("Sending daily Slack report.")
    if slack.send_daily_report(report):
        try:
            sheets.set_meta(_META_KEY_DAILY, today_pht)
        except Exception as exc:
            logger.error(
                "Daily report sent but failed to persist last-sent date (%s) — "
                "next run may re-send: %s", today_pht, exc,
            )


def _send_weekly_report_once(sheets: SheetsClient, slack: SlackReporter,
                             report: ReportData) -> None:
    """Send the weekly domain-health report — once per ISO week in PHT."""
    this_week = datetime.now(_PHT).strftime("%G-W%V")
    try:
        last_sent = sheets.get_meta(_META_KEY_WEEKLY)
    except Exception as exc:
        logger.warning("Could not read weekly-report meta (%s) — will send anyway.", exc)
        last_sent = None
    if last_sent == this_week:
        logger.info("Weekly report already sent this week (%s) — skipping duplicate.", this_week)
        return
    logger.info("Sending weekly domain health report.")
    if slack.send_weekly_domain_report(report):
        try:
            sheets.set_meta(_META_KEY_WEEKLY, this_week)
        except Exception as exc:
            logger.error(
                "Weekly report sent but failed to persist last-sent week (%s) — "
                "next run may re-send: %s", this_week, exc,
            )


# ── Disconnect handlers ───────────────────────────────────────────────────────

def _resolve_provider(
    email: str,
    missioninbox_email_set: set,
    zapmail_email_set: set,
    missioninbox_client: Optional["MissionInboxClient"],
    premiuminbox_email_set: Optional[set] = None,
    scaledmail_email_set: Optional[set] = None,
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

    # 4. Inbox-infrastructure providers (Premium Inboxes, ScaledMail) — detected
    #    from their live mailbox lists, same as ZapMail.
    if premiuminbox_email_set and email in premiuminbox_email_set:
        return "premiuminbox"
    if scaledmail_email_set and email in scaledmail_email_set:
        return "scaledmail"

    return "unknown"


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    """Parse an Instantly ISO8601 timestamp (may end in 'Z'). Returns tz-aware UTC or None."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Instantly sends e.g. "2026-04-10T14:22:31.000Z" — fromisoformat handles
        # it on Python 3.11+, but we normalise Z→+00:00 for older runtimes.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _account_age_days(timestamp_created: Optional[str]) -> Optional[float]:
    """Days since account was created in Instantly, or None if unknown."""
    created = _parse_ts(timestamp_created)
    if created is None:
        return None
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400.0


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
      - Health < critical_threshold AND account is older than the grace
        period: auto-pauses the account in Instantly (pauses BOTH warmup
        and campaign sending — Instantly status=2 is account-wide).
      - Health < critical_threshold AND account is still within grace
        period: alert only, flagged as "new account warming up".
      - Health between critical and alert threshold: alert only.

    The grace period protects brand-new accounts that legitimately start
    at stat_warmup_score=0 before the warmup algorithm has any data.
    Without it every newly-added account gets auto-paused on first run.

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
            age_days = _account_age_days(data.get("timestamp_created"))
            is_new_account = (
                age_days is not None and age_days < cfg.health_score_grace_days
            )
            age_unknown = age_days is None
            action_taken = ""

            if is_critical and is_new_account:
                # Brand-new account still ramping up — DO NOT auto-pause.
                # stat_warmup_score=0 is the expected starting state; pausing
                # here would stop warmup from ever running and trash the
                # account before it has a chance to warm up.
                action_taken = (
                    f"New account (age {age_days:.1f}d, grace {cfg.health_score_grace_days}d) "
                    f"— skipping auto-pause while warmup ramps up"
                )
                logger.info(
                    "Skipping auto-pause for %s (%s): score=%d%%, age=%.1fd (grace=%dd).",
                    email, ws_name, health_score, age_days, cfg.health_score_grace_days,
                )
            elif is_critical and age_unknown:
                # We cannot determine the account's age — be conservative
                # and do NOT auto-pause. Better to alert and let a human
                # decide than to nuke a ramping-up account.
                action_taken = (
                    "Critical health but account age unknown — "
                    "review manually before pausing"
                )
                logger.warning(
                    "Skipping auto-pause for %s (%s): score=%d%%, age=unknown.",
                    email, ws_name, health_score,
                )
            elif is_critical:
                # Mature account with critically low health — auto-pause.
                # This pauses the account in Instantly entirely (status=2)
                # which halts BOTH warmup sending AND use in campaigns.
                try:
                    paused = client.pause_account(email)
                    if paused:
                        action_taken = "Auto-paused in Instantly (halts warmup + campaign sending)"
                        logger.warning(
                            "Auto-paused %s (%s) — health score %d%% is critical, age %s.",
                            email, ws_name, health_score,
                            f"{age_days:.1f}d" if age_days is not None else "unknown",
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
                "age_days": age_days,
                "is_new_account": is_new_account,
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

    All metrics are pulled live from GET /campaigns/analytics — nothing
    is hardcoded or assumed. Thresholds come from config:
      - bounce_rate_alert_threshold:  % at which to surface a warning
      - campaign_bounce_pause_threshold: % at which to auto-pause
      - campaign_min_sent_for_bounce_check: min sent to judge bounce %
    """
    for campaign in campaigns:
        status = campaign.get("campaign_status")
        if status != 1:  # only check active campaigns
            continue

        # ── All fields from Instantly /campaigns/analytics (no hardcoding) ──
        name = campaign.get("campaign_name", "Unknown")
        campaign_id = campaign.get("campaign_id", "")
        sent = int(campaign.get("emails_sent_count") or 0)
        bounced = int(campaign.get("bounced_count") or 0)
        contacted = int(campaign.get("contacted_count") or 0)
        total_leads = int(campaign.get("leads_count") or 0)
        replies = int(
            campaign.get("reply_count_unique")
            or campaign.get("reply_count")
            or 0
        )
        opens = int(
            campaign.get("open_count_unique")
            or campaign.get("open_count")
            or 0
        )
        clicks = int(
            campaign.get("link_click_count_unique")
            or campaign.get("link_click_count")
            or 0
        )
        unsubscribed = int(campaign.get("unsubscribed_count") or 0)

        # Need meaningful volume — config-driven minimum to judge bounce rate.
        if sent < cfg.campaign_min_sent_for_bounce_check:
            continue

        bounce_rate = (bounced / sent * 100) if sent > 0 else 0.0

        if bounce_rate >= cfg.bounce_rate_alert_threshold:
            reply_rate = (replies / sent * 100) if sent > 0 else 0.0
            open_rate = (opens / sent * 100) if sent > 0 else 0.0
            action_taken = ""

            should_pause = (
                bounce_rate >= cfg.campaign_bounce_pause_threshold
                and bool(campaign_id)
            )
            if should_pause:
                try:
                    paused = client.pause_campaign(campaign_id)
                    if paused:
                        action_taken = "Auto-paused campaign — re-verify lead list before resuming"
                        logger.warning(
                            "Auto-paused campaign '%s' (%s) — bounce rate %.1f%% exceeds %d%%.",
                            name, ws_name, bounce_rate,
                            cfg.campaign_bounce_pause_threshold,
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
                "opens": opens,
                "open_rate": open_rate,
                "clicks": clicks,
                "unsubscribed": unsubscribed,
                "action_taken": action_taken,
            })


def _process_lemlist_workspace(
    ws_name: str,
    api_key: str,
    report: ReportData,
    sheets: SheetsClient,
    alert_state: Dict,
    cfg: "Config",
) -> None:
    """
    Fetch and process all Lemlist email accounts for a workspace.

    Per-account connection status comes from a live SMTP/IMAP test
    (Lemlist has no native status field in the account listing).
    No auto-reconnect is available for Lemlist — disconnected accounts
    appear in the daily report and must be fixed manually in Lemlist.

    Campaign bounce rates are checked using the same thresholds as Instantly.
    Warmup health is not available via the Lemlist API.
    """
    client = LemlistClient(api_key)
    summary = WorkspaceSummary(f"{ws_name} [Lemlist]", tool="lemlist")

    try:
        accounts = client.get_accounts()
    except Exception as exc:
        logger.error("Lemlist workspace %s: failed to get accounts: %s", ws_name, exc)
        report.workspace_errors.append({"workspace_name": f"{ws_name} [Lemlist]", "error": str(exc)})
        return

    for account in accounts:
        email = str(account.get("email", "")).strip().lower()
        account_id = str(account.get("id", "")).strip()
        provider = str(account.get("provider", "")).strip().lower()
        if not email or not account_id:
            continue

        # Google and Microsoft accounts use OAuth2 — the SMTP/IMAP test endpoint
        # always returns false for them even when connected (OAuth2 tokens are not
        # raw SMTP credentials). Treat OAuth2 accounts as connected if they appear
        # in the accounts list; Lemlist manages their OAuth state internally.
        # Only run the live test for custom SMTP/IMAP accounts where it is reliable.
        if LemlistClient.is_oauth_provider(provider):
            connected = True
        else:
            test_result = client.test_account(account_id)
            connected = LemlistClient.is_connected(test_result)

        if connected:
            summary.connected += 1
            if email in alert_state:
                logger.info("Lemlist account %s (%s) recovered — clearing alert state.", email, ws_name)
                try:
                    sheets.delete_alert_state(email)
                    del alert_state[email]
                except Exception as exc:
                    logger.error("Failed to clear alert_state for Lemlist %s: %s", email, exc)
        else:
            summary.disconnected += 1
            if is_new_disconnection(email, alert_state):
                existing_row = alert_state.get(email, {})
                new_row = {
                    "email": email.lower(),
                    "workspace_name": ws_name,
                    "first_detected": existing_row.get("first_detected") or utcnow_str(),
                    "last_alerted": utcnow_str(),
                    "reconnect_attempts": 0,
                    "status": "lemlist_disconnected",
                }
                try:
                    sheets.upsert_alert_state(**new_row)
                    alert_state[email] = new_row
                except Exception as exc:
                    logger.error("Failed to write alert_state for Lemlist %s: %s", email, exc)
            report.still_disconnected.append({
                "email": email,
                "workspace_name": ws_name,
                "provider": "lemlist",
                "attempts": 0,
            })

    # ── Campaign bounce check ─────────────────────────────────────────────────
    try:
        end_dt   = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=7)
        end_str   = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        campaigns = client.get_campaigns()
        running_campaigns = [c for c in campaigns if c.get("status") == "running"]
        if running_campaigns:
            campaign_ids = [c["_id"] for c in running_campaigns]
            name_map = {c["_id"]: c.get("name", "Unknown") for c in running_campaigns}
            stats_list = client.get_campaign_stats_batch(campaign_ids, start_str, end_str)
            _process_lemlist_campaign_health(stats_list, name_map, ws_name, client, report, cfg)
    except Exception as exc:
        logger.warning("Lemlist campaign analytics failed for %s: %s (non-fatal)", ws_name, exc)

    report.workspace_summaries.append(summary)
    logger.info(
        "Lemlist workspace %s: %d connected, %d disconnected.",
        ws_name, summary.connected, summary.disconnected,
    )


def _process_lemlist_campaign_health(
    stats_list: List[Dict],
    name_map: Dict[str, str],
    ws_name: str,
    client: "LemlistClient",
    report: ReportData,
    cfg: "Config",
) -> None:
    """
    Check Lemlist campaign bounce rates using the same thresholds as Instantly.
    Fields: messagesSent, messagesBounced, replied, nbLeads, nbLeadsLaunched, opened, clicked.
    """
    for stats in stats_list:
        campaign_id = stats.get("campaignId", "")
        name = name_map.get(campaign_id, "Unknown")
        sent    = int(stats.get("messagesSent") or 0)
        bounced = int(stats.get("messagesBounced") or 0)
        replied = int(stats.get("replied") or 0)

        if sent < cfg.campaign_min_sent_for_bounce_check:
            continue

        bounce_rate = (bounced / sent * 100) if sent > 0 else 0.0

        if bounce_rate >= cfg.bounce_rate_alert_threshold:
            reply_rate = (replied / sent * 100) if sent > 0 else 0.0
            action_taken = ""

            should_pause = (
                bounce_rate >= cfg.campaign_bounce_pause_threshold
                and bool(campaign_id)
            )
            if should_pause:
                try:
                    paused = client.pause_campaign(campaign_id)
                    if paused:
                        action_taken = "Auto-paused campaign — re-verify lead list before resuming"
                        logger.warning(
                            "Auto-paused Lemlist campaign '%s' (%s) — bounce rate %.1f%% exceeds %d%%.",
                            name, ws_name, bounce_rate, cfg.campaign_bounce_pause_threshold,
                        )
                    else:
                        action_taken = "Auto-pause failed — pause manually in Lemlist and re-verify lead list"
                except Exception as exc:
                    action_taken = "Auto-pause failed — pause manually in Lemlist and re-verify lead list"
                    logger.error("Error auto-pausing Lemlist campaign '%s' (%s): %s", name, ws_name, exc)
            else:
                action_taken = "Review lead list quality — consider re-verifying emails before sending more"

            logger.warning(
                "High bounce rate for Lemlist campaign '%s' (%s): %.1f%% (%d/%d) — %s",
                name, ws_name, bounce_rate, bounced, sent, action_taken,
            )
            report.bounce_alerts.append({
                "campaign_name": f"{name} (Lemlist)",
                "workspace_name": ws_name,
                "bounce_rate": bounce_rate,
                "bounced": bounced,
                "sent": sent,
                "contacted": int(stats.get("nbLeadsLaunched") or 0),
                "total_leads": int(stats.get("nbLeads") or 0),
                "reply_rate": reply_rate,
                "replies": replied,
                "opens": int(stats.get("opened") or 0),
                "open_rate": 0.0,
                "clicks": int(stats.get("clicked") or 0),
                "unsubscribed": 0,
                "action_taken": action_taken,
            })


def _process_smartlead_workspace(
    ws_name: str,
    api_key: str,
    report: ReportData,
    sheets: SheetsClient,
    alert_state: Dict,
    cfg: "Config",
) -> None:
    """
    Fetch and process all Smartlead email accounts for a workspace.

    Connection status comes straight from the account payload
    (`is_smtp_success`) — no live probe needed, unlike Lemlist.

    Automated actions:
      - Per-account daily send limit capped at cfg.daily_limit_max.
      - Campaign bounce rate checked with the same thresholds as Instantly
        (auto-pause at cfg.campaign_bounce_pause_threshold).

    Alert-only (no auto-reconnect for Smartlead in v1 — fix in the Smartlead
    dashboard): disconnected accounts and low warmup health surface in the
    daily report.
    """
    client = SmartleadClient(api_key)
    summary = WorkspaceSummary(f"{ws_name} [Smartlead]", tool="smartlead")

    try:
        accounts = client.get_email_accounts()
    except Exception as exc:
        logger.error("Smartlead workspace %s: failed to get accounts: %s", ws_name, exc)
        report.workspace_errors.append({"workspace_name": f"{ws_name} [Smartlead]", "error": str(exc)})
        return

    for account in accounts:
        email = str(account.get("from_email") or account.get("email") or "").strip().lower()
        account_id = account.get("id")
        if not email or account_id is None:
            continue

        # ── Daily send limit cap ─────────────────────────────────────────────
        raw_limit = account.get("message_per_day", account.get("max_email_per_day"))
        if raw_limit is not None:
            try:
                current_limit = int(raw_limit)
            except (TypeError, ValueError):
                current_limit = None
            if current_limit is not None and current_limit > cfg.daily_limit_max:
                ok = client.update_daily_limit(account_id, cfg.daily_limit_max)
                report.daily_limit_adjustments.append({
                    "email": email,
                    "workspace_name": f"{ws_name} [Smartlead]",
                    "previous_limit": current_limit,
                    "new_limit": cfg.daily_limit_max,
                    "success": ok,
                })

        # ── Connection status ────────────────────────────────────────────────
        if SmartleadClient.is_connected(account):
            summary.connected += 1
            if email in alert_state:
                logger.info("Smartlead account %s (%s) recovered — clearing alert state.", email, ws_name)
                try:
                    sheets.delete_alert_state(email)
                    del alert_state[email]
                except Exception as exc:
                    logger.error("Failed to clear alert_state for Smartlead %s: %s", email, exc)
        else:
            summary.disconnected += 1
            if is_new_disconnection(email, alert_state):
                existing_row = alert_state.get(email, {})
                new_row = {
                    "email": email.lower(),
                    "workspace_name": ws_name,
                    "first_detected": existing_row.get("first_detected") or utcnow_str(),
                    "last_alerted": utcnow_str(),
                    "reconnect_attempts": 0,
                    "status": "smartlead_disconnected",
                }
                try:
                    sheets.upsert_alert_state(**new_row)
                    alert_state[email] = new_row
                except Exception as exc:
                    logger.error("Failed to write alert_state for Smartlead %s: %s", email, exc)
            report.still_disconnected.append({
                "email": email,
                "workspace_name": ws_name,
                "provider": "smartlead",
                "attempts": 0,
            })

        # ── Warmup health (from account payload — no extra API call) ──────────
        wh = SmartleadClient.warmup_health(account)
        if wh:
            score = wh["health_score"]
            if score is not None and score < cfg.health_score_alert_threshold:
                report.health_alerts.append({
                    "email": email,
                    "workspace_name": f"{ws_name} [Smartlead]",
                    "health_score": int(score),
                    "age_days": None,
                    "is_new_account": False,
                    "action_taken": "Monitor closely — reduce send volume if it drops further",
                })
            if wh["spam_rate"] >= cfg.spam_rate_alert_threshold and wh["sent"] > 0:
                report.health_alerts.append({
                    "email": email,
                    "workspace_name": f"{ws_name} [Smartlead]",
                    "health_score": int(score) if score is not None else 0,
                    "age_days": None,
                    "is_new_account": False,
                    "action_taken": f"Warmup spam rate {wh['spam_rate']:.1f}% "
                                    f"({wh['spam']}/{wh['sent']}) — pause and investigate",
                })

        # ── Signature check ──────────────────────────────────────────────────
        if not str(account.get("signature") or "").strip():
            report.signature_issues.append({"email": email, "workspace_name": ws_name})

    report.signature_checked_by_ws[f"{ws_name} [Smartlead]"] = len(accounts)

    # ── Campaign bounce check ────────────────────────────────────────────────
    try:
        campaigns = client.get_campaigns()
        active = [c for c in campaigns if str(c.get("status", "")).upper() == "ACTIVE"]
        _process_smartlead_campaign_health(active, ws_name, client, report, cfg)
    except Exception as exc:
        logger.warning("Smartlead campaign analytics failed for %s: %s (non-fatal)", ws_name, exc)

    report.workspace_summaries.append(summary)
    logger.info(
        "Smartlead workspace %s: %d connected, %d disconnected.",
        ws_name, summary.connected, summary.disconnected,
    )


def _process_smartlead_campaign_health(
    campaigns: List[Dict],
    ws_name: str,
    client: "SmartleadClient",
    report: ReportData,
    cfg: "Config",
) -> None:
    """
    Check Smartlead campaign bounce rates using the same thresholds as Instantly.
    Reads GET /campaigns/{id}/analytics defensively (field names vary).
    """
    for campaign in campaigns:
        campaign_id = campaign.get("id")
        name = campaign.get("name", "Unknown")
        if campaign_id is None:
            continue

        stats = client.get_campaign_analytics(campaign_id)
        if not stats:
            continue

        sent = int(
            stats.get("sent_count")
            or stats.get("email_sent_count")
            or stats.get("total_sent")
            or 0
        )
        bounced = int(
            stats.get("bounce_count")
            or stats.get("bounced_count")
            or stats.get("total_bounced")
            or 0
        )
        replied = int(
            stats.get("reply_count")
            or stats.get("replied_count")
            or stats.get("total_replies")
            or 0
        )

        if sent < cfg.campaign_min_sent_for_bounce_check:
            continue

        bounce_rate = (bounced / sent * 100) if sent > 0 else 0.0
        if bounce_rate < cfg.bounce_rate_alert_threshold:
            continue

        reply_rate = (replied / sent * 100) if sent > 0 else 0.0
        action_taken = ""
        if bounce_rate >= cfg.campaign_bounce_pause_threshold:
            paused = client.pause_campaign(campaign_id)
            if paused:
                action_taken = "Auto-paused campaign — re-verify lead list before resuming"
                logger.warning(
                    "Auto-paused Smartlead campaign '%s' (%s) — bounce rate %.1f%% exceeds %d%%.",
                    name, ws_name, bounce_rate, cfg.campaign_bounce_pause_threshold,
                )
            else:
                action_taken = "Auto-pause failed — pause manually in Smartlead and re-verify lead list"
        else:
            action_taken = "Review lead list quality — consider re-verifying emails before sending more"

        logger.warning(
            "High bounce rate for Smartlead campaign '%s' (%s): %.1f%% (%d/%d) — %s",
            name, ws_name, bounce_rate, bounced, sent, action_taken,
        )
        report.bounce_alerts.append({
            "campaign_name": f"{name} (Smartlead)",
            "workspace_name": ws_name,
            "bounce_rate": bounce_rate,
            "bounced": bounced,
            "sent": sent,
            "contacted": int(stats.get("contacted_count") or stats.get("total_contacted") or 0),
            "total_leads": int(stats.get("lead_count") or stats.get("total_leads") or 0),
            "reply_rate": reply_rate,
            "replies": replied,
            "opens": int(stats.get("open_count") or stats.get("unique_open_count") or 0),
            "open_rate": 0.0,
            "clicks": int(stats.get("click_count") or stats.get("unique_click_count") or 0),
            "unsubscribed": int(stats.get("unsubscribed_count") or 0),
            "action_taken": action_taken,
        })


def _handle_non_reconnectable_error(
    email: str,
    ws_name: str,
    err_info: Dict,
    sheets: SheetsClient,
    slack: SlackReporter,
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """
    Handle account errors that reconnect cannot fix (sending-limit,
    suspicious-activity, mailbox-full, etc.). Provider-agnostic — the
    category comes from InstantlyClient.classify_account_error() and
    applies identically across Google, Microsoft, IMAP, and AWS accounts.

    Behaviour:
      - Never calls the reconnect APIs (would waste calls and confuse users).
      - Alerts Slack once per provider_error_realert_hours (default 24h).
      - Writes alert_state with status=category so subsequent runs know the
        account is in a known error mode — recovery at the top of run() still
        clears this when the account returns to status=1.
      - Adds the entry to report.provider_errors so the daily Slack summary
        lists it in its own section (not as a reconnect failure).
    """
    category = err_info["category"]
    existing_row = alert_state.get(email)

    # Dedupe: alert only on first detection, or once the realert window expires.
    is_new = is_new_disconnection(email, alert_state)
    should_real = should_realert(email, alert_state, cfg.provider_error_realert_hours)

    # If the stored status differs from the new category, treat as a new
    # alert condition — user needs to know the failure mode changed.
    prev_status = (existing_row or {}).get("status", "")
    category_changed = bool(existing_row) and prev_status != category

    will_alert = is_new or should_real or category_changed
    if will_alert:
        logger.info(
            "Provider-side error staged for batched alert: %s (%s): %s",
            email, ws_name, category,
        )
    else:
        logger.debug(
            "Provider-side error for %s (%s) still active (%s) — within realert window, silent.",
            email, ws_name, category,
        )

    # Persist state so subsequent runs see the category and stay silent
    # until the realert window expires (or the account recovers).
    now = utcnow_str()
    first_detected = (existing_row or {}).get("first_detected") or now
    last_alerted = now if will_alert else ((existing_row or {}).get("last_alerted") or now)
    new_row = {
        "email": email.lower(),
        "workspace_name": ws_name,
        "first_detected": first_detected,
        "last_alerted": last_alerted,
        "reconnect_attempts": int((existing_row or {}).get("reconnect_attempts", 0)),
        "status": category,
    }
    try:
        sheets.upsert_alert_state(**new_row)
        alert_state[email] = new_row
    except Exception as exc:
        logger.error("Failed to write alert_state for provider error %s: %s", email, exc)

    # Feed the daily report — surfaces these separately from reconnect failures.
    report.provider_errors.append({
        "email": email,
        "workspace_name": ws_name,
        "category": category,
        "detail": err_info.get("detail", ""),
        "response_code": err_info.get("response_code"),
        "auto_recoverable": err_info.get("auto_recoverable", False),
        "first_alert_this_run": will_alert,
    })


def _handle_missioninbox_disconnect(
    email: str,
    ws_name: str,
    client: InstantlyClient,
    sheets: SheetsClient,
    slack: "SlackReporter",
    missioninbox_client: Optional["MissionInboxClient"],
    billing_issue: Optional[str],
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """Attempt auto-reconnect for a disconnected Mission Inbox account."""
    existing_row    = alert_state.get(email)
    current_attempts = int((existing_row or {}).get("reconnect_attempts", 0))

    # ── Billing issue — skip reconnect, report payment problem ─────────────
    if billing_issue:
        logger.warning(
            "MissionInbox billing issue for %s (%s): %s — skipping reconnect.",
            email, ws_name, billing_issue,
        )
        new_row = build_new_alert_state_row(email, ws_name)
        if existing_row:
            new_row["first_detected"] = existing_row.get("first_detected", new_row["first_detected"])
        new_row["status"] = "missioninbox_billing_issue"
        new_row["reconnect_attempts"] = cfg.max_reconnect_attempts
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for MI billing %s: %s", email, exc)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": 0,
            "reason": f"Mission Inbox payment pending — {billing_issue}",
        })
        return

    # ── Billing resolved — reset attempts so reconnect can retry ───────────
    if existing_row and existing_row.get("status") == "missioninbox_billing_issue":
        logger.info(
            "MissionInbox billing resolved for %s (%s) — resetting reconnect attempts.",
            email, ws_name,
        )
        current_attempts = 0

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
        report.reconnect_attempted.append({
            "email": email, "workspace_name": ws_name, "provider": "Mission Inbox",
        })

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
    cached_mailbox: Optional[Dict],
    billing_issue: Optional[str],
    sheets: SheetsClient,
    slack: SlackReporter,
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """
    Attempt ZapMail auto-reconnect via the ZapMail export API.
    Uses cached_mailbox from the initial list_mailboxes() call to avoid duplicate API calls.
    Skips reconnect if billing_issue is detected (payment pending, subscription expired, etc.).
    Falls back to a one-time Slack alert if no client is configured or reconnect fails.
    """
    existing_row     = alert_state.get(email)
    current_attempts = int((existing_row or {}).get("reconnect_attempts", 0))

    # ── Billing issue — skip reconnect, report payment problem ─────────────
    mailbox_suspended = cached_mailbox and ZapMailClient.is_mailbox_suspended(cached_mailbox)
    effective_billing_issue = billing_issue or (
        f"Mailbox suspended (status: {cached_mailbox.get('status', '?')})"
        if mailbox_suspended else None
    )

    if effective_billing_issue:
        logger.warning(
            "ZapMail billing issue for %s (%s): %s — skipping reconnect.",
            email, ws_name, effective_billing_issue,
        )
        new_row = build_new_alert_state_row(email, ws_name)
        if existing_row:
            new_row["first_detected"] = existing_row.get("first_detected", new_row["first_detected"])
        new_row["status"] = "zapmail_billing_issue"
        new_row["reconnect_attempts"] = cfg.max_reconnect_attempts
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for billing %s: %s", email, exc)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "zapmail", "attempts": 0,
            "reason": f"ZapMail payment pending — {effective_billing_issue}",
        })
        return

    # ── Billing resolved — reset attempts so reconnect can retry ───────────
    if existing_row and existing_row.get("status") == "zapmail_billing_issue":
        logger.info(
            "ZapMail billing resolved for %s (%s) — resetting reconnect attempts.",
            email, ws_name,
        )
        current_attempts = 0

    # ── If last reconnect is pending confirmation, check if it failed ────────
    if existing_row and existing_row.get("status") == "reconnect_pending":
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
        # Track first-attempt for batched Slack alert (sent after all accounts processed)
        if current_attempts == 0:
            report.reconnect_attempted.append({
                "email": email, "workspace_name": ws_name, "provider": "ZapMail",
            })
        # Use cached mailbox to avoid re-fetching list_mailboxes() for each email
        success, permanent_failure = zapmail_client.reconnect_email(
            email, cached_mailbox=cached_mailbox
        )

        if success:
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
            return

        if permanent_failure:
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


def _handle_infra_provider_disconnect(
    provider: str,
    provider_label: str,
    provider_client,
    email: str,
    ws_name: str,
    billing_issue: Optional[str],
    sheets: SheetsClient,
    alert_state: Dict,
    report: ReportData,
    cfg: Config,
) -> None:
    """
    Disconnect handling for the inbox-infrastructure providers whose auto-reconnect
    is not implemented yet (Premium Inboxes, ScaledMail).

    PHASE 1 behaviour — modelled on the "no client / max attempts" tail of
    _handle_zapmail_disconnect:
      - If a billing/subscription issue is detected, skip everything else and
        report a payment problem.
      - Otherwise alert once (re-alert honours cfg.zapmail_realert_hours) and
        write alert_state so subsequent runs stay quiet until recovery.
      - Recovery is cleared by the connected-account branch at the top of run().

    PHASE 2 (after API credentials): call provider_client.reconnect_email(email)
    here with the pending / verify-next-run flow ZapMail uses.
    """
    existing_row = alert_state.get(email)
    status_key = f"{provider}_disconnected"

    if billing_issue:
        logger.warning(
            "%s billing issue for %s (%s): %s — skipping reconnect.",
            provider_label, email, ws_name, billing_issue,
        )
        new_row = build_new_alert_state_row(email, ws_name)
        if existing_row:
            new_row["first_detected"] = existing_row.get("first_detected", new_row["first_detected"])
        new_row["status"] = f"{provider}_billing_issue"
        new_row["reconnect_attempts"] = cfg.max_reconnect_attempts
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for %s billing %s: %s", provider, email, exc)
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": provider, "attempts": 0,
            "reason": f"{provider_label} payment pending — {billing_issue}",
        })
        return

    # Phase 2 hook — currently a no-op stub that returns (False, False).
    if provider_client is not None:
        try:
            provider_client.reconnect_email(email)
        except Exception as exc:
            logger.warning("%s reconnect_email raised for %s: %s (ignored)", provider_label, email, exc)

    is_new      = is_new_disconnection(email, alert_state)
    should_real = should_realert(email, alert_state, cfg.zapmail_realert_hours)
    if is_new or should_real:
        logger.info("Sending %s disconnect alert for %s (%s).", provider_label, email, ws_name)
        new_row = build_new_alert_state_row(email, ws_name)
        if existing_row:
            new_row["first_detected"] = existing_row.get("first_detected", new_row["first_detected"])
        new_row["status"] = status_key
        try:
            sheets.upsert_alert_state(**new_row)
            alert_state[email] = new_row
        except Exception as exc:
            logger.error("Failed to write alert_state for %s %s: %s", provider, email, exc)
    else:
        logger.debug("%s %s (%s) still disconnected — already alerted, silent.", provider_label, email, ws_name)

    report.still_disconnected.append({
        "email": email, "workspace_name": ws_name,
        "provider": provider, "attempts": 0,
        "reason": f"{provider_label} account — reconnect in the {provider_label} dashboard",
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
            SlackReporter(_cfg.slack_webhook_url).send_crash_alert(
                f"Unhandled error: `{exc}`\nCheck GitHub Actions logs for full traceback."
            )
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
