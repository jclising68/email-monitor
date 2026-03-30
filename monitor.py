"""
monitor.py — Orchestration entry point.

Usage:
  python monitor.py            # Hourly check + auto-reconnect
  python monitor.py --report   # Daily report: run check first, then post to Slack
"""
from __future__ import annotations

import argparse
import logging
import sys
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
    should_realert_zapmail,
    build_new_alert_state_row,
    build_reconnect_failed_row,
    utcnow_str,
)
from zapmail_client import ZapMailClient

logger = logging.getLogger(__name__)


def run(send_report: bool = False) -> None:
    logger.info("=== Email Monitor starting (report=%s) ===", send_report)

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

        # Build a ZapMail client for this workspace if it has a workspace ID configured
        zapmail_client: Optional[ZapMailClient] = None
        zapmail_email_set: set = set()
        zm_workspace_id = ws.get("zapmail_workspace_key", "")
        if zapmail_api_key and zm_workspace_id:
            zapmail_client = ZapMailClient(
                api_key=zapmail_api_key,
                workspace_key=zm_workspace_id,
                service_provider=ws.get("zapmail_service_provider", "GOOGLE"),
                base_url=_cfg.zapmail_api_base_url,
            )
            try:
                zm_mailboxes = zapmail_client.list_mailboxes()
                zapmail_email_set = {
                    str(mb.get("email", "")).lower()
                    for mb in zm_mailboxes if mb.get("email")
                }
                logger.info(
                    "ZapMail: workspace '%s' has %d mailbox(es).",
                    ws_name, len(zapmail_email_set),
                )
            except Exception as exc:
                logger.error(
                    "ZapMail: failed to list mailboxes for workspace '%s' (%s) — "
                    "ZapMail reconnect disabled for this workspace.", ws_name, exc,
                )
                zapmail_client = None

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
                    "Mission Inbox reconnect disabled for this workspace.", ws_name, exc,
                )
                missioninbox_client = None

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
                provider = _resolve_provider(email, missioninbox_email_set, zapmail_email_set)

                if provider == "missioninbox":
                    _handle_missioninbox_disconnect(
                        email, ws_name, client, sheets, slack, missioninbox_client,
                        alert_state, report, _cfg,
                    )
                elif provider == "zapmail":
                    _handle_zapmail_disconnect(
                        email, ws_name, zapmail_client, sheets, slack,
                        alert_state, report, _cfg,
                    )
                else:
                    # Unknown provider — client-owned account we don't manage.
                    # Alert once, then re-alert every 24 hours if still disconnected.
                    is_new   = is_new_disconnection(email, alert_state)
                    re_alert = should_realert_zapmail(email, alert_state, realert_hours=24)
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

    logger.info("=== Email Monitor finished ===")


# ── Disconnect handlers ───────────────────────────────────────────────────────

def _resolve_provider(
    email: str,
    missioninbox_email_set: set,
    zapmail_email_set: set,
) -> str:
    """
    Determine provider for a disconnected account.
    Priority:
      1. If email is in Mission Inbox's live mailbox list → missioninbox
      2. If email is in ZapMail's live mailbox list → zapmail
      3. Otherwise → client-owned account, do not reconnect
    """
    if email in missioninbox_email_set:
        return "missioninbox"
    if email in zapmail_email_set:
        return "zapmail"
    return "unknown"


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
        logger.error(
            "MissionInbox: could not fetch credentials for %s (%s) — cannot reconnect.",
            email, ws_name,
        )
        report.still_disconnected.append({
            "email": email, "workspace_name": ws_name,
            "provider": "missioninbox", "attempts": current_attempts,
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
    should_real = should_realert_zapmail(email, alert_state, cfg.zapmail_realert_hours)

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
    args = parser.parse_args()
    try:
        run(send_report=args.report)
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
