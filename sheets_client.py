"""
sheets_client.py — Read Workspaces/Settings from Google Sheets; read/write
                   alert_state and meta.

Auth: a Google *service account* if one is configured (see config.load_google_
credentials — GOOGLE_CREDENTIALS_JSON / _B64 / _FILE); the Sheet is shared with
the service account's email. Otherwise falls back to OAuth user auth
(credentials.json + token.json / GOOGLE_TOKEN_JSON_B64).

Tab names (case-sensitive):
  Workspaces  : workspace_name | active | api_key | lemlist_api_key | smartlead_api_key |
                zapmail_workspace_key_google | zapmail_workspace_key_microsoft |
                mission_inbox_api_key | premiuminbox_workspace_id | scaledmail_organization_id
                (only workspace_name + active + one sending key are required)
  Settings    : key | value | notes   ← client-editable credential store,
                created + seeded automatically on first run
  alert_state : email | workspace_name | first_detected | last_alerted |
                reconnect_attempts | status   ← managed automatically
  meta        : key | value   ← report idempotency flags, managed automatically

Mailboxes are discovered live from each provider's API — no sheet tab needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import json
import os

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

_CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "credentials.json")
_TOKEN_FILE       = os.path.join(os.path.dirname(__file__), "token.json")


def _get_oauth_credentials() -> OAuthCredentials:
    """
    Load or refresh OAuth2 credentials.

    Two modes:
      - Server/CI (GitHub Actions): reads token JSON from GOOGLE_TOKEN_JSON_B64 env var.
        Uses the refresh_token to get new access tokens — no browser needed.
      - Local: reads/writes token.json file; opens browser on first run.
    """
    import base64

    token_b64 = os.environ.get("GOOGLE_TOKEN_JSON_B64")
    if token_b64:
        try:
            token_data = json.loads(base64.b64decode(token_b64).decode("utf-8"))
        except Exception as e:
            raise EnvironmentError(f"Failed to decode GOOGLE_TOKEN_JSON_B64: {e}")
        creds = OAuthCredentials.from_authorized_user_info(token_data, SCOPES)
        if not creds.valid and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    # Local file-based OAuth2 (opens browser for consent if no token.json)
    creds = None
    if os.path.exists(_TOKEN_FILE):
        creds = OAuthCredentials.from_authorized_user_file(_TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(_CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds

TAB_WORKSPACES  = "Workspaces"
TAB_ALERT_STATE = "alert_state"   # created automatically on first run (lowercase is fine)
TAB_META        = "meta"          # small key/value store for idempotency flags
TAB_SETTINGS    = "Settings"      # client-editable credential store (created + seeded on first run)

_AS_COLS       = ["email", "workspace_name", "first_detected", "last_alerted", "reconnect_attempts", "status"]
_META_COLS     = ["key", "value"]
_SETTINGS_COLS = ["key", "value", "notes"]

# Rows seeded into the 'Settings' tab on first run so a non-technical client sees
# the full menu of what they can fill in. Blank 'value' → fall back to the env
# var / secret, then to the code default. Only credential-type keys live here.
_SETTINGS_SEED = [
    ("slack_webhook_url",
     "REQUIRED — Slack Incoming Webhook URL. Create at api.slack.com/apps -> your app -> Incoming Webhooks."),
    ("zapmail_api_key",
     "Optional — ZapMail API key (global). Only if using ZapMail. app.zapmail.ai -> Settings -> API Keys."),
    ("premiuminbox_api_token",
     "Optional — Premium Inboxes API token (global). Only if using Premium Inboxes. portal.premiuminboxes.com -> Settings -> API Token."),
    ("scaledmail_api_key",
     "Optional — ScaledMail API key (global). Only if using ScaledMail. app.scaledmail.com/settings."),
]

class SheetsClient:
    def __init__(self, credentials_dict: dict, sheet_id: str):
        """
        Two auth paths:
          - Service account (preferred, client-friendly): if credentials_dict is
            a parsed service-account key ({"type": "service_account", ...}), use
            it directly. The Sheet must be shared with the service account's
            client_email. No browser, no token refresh, no per-user setup.
          - OAuth user auth (legacy): if no service account is configured, fall
            back to credentials.json + token.json (GOOGLE_TOKEN_JSON_B64 in CI).
        """
        self._sheet_id = sheet_id
        if isinstance(credentials_dict, dict) and credentials_dict.get("type") == "service_account":
            logger.info(
                "Google auth: service account (%s)", credentials_dict.get("client_email", "?"),
            )
            creds = ServiceAccountCredentials.from_service_account_info(
                credentials_dict, scopes=SCOPES,
            )
        else:
            logger.info("Google auth: OAuth user token (no service account configured).")
            creds = _get_oauth_credentials()
        self._gc = gspread.authorize(creds)
        self._spreadsheet = None  # lazy open

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _open(self) -> gspread.Spreadsheet:
        if self._spreadsheet is None:
            self._spreadsheet = self._gc.open_by_key(self._sheet_id)
        return self._spreadsheet

    def _worksheet(self, tab: str) -> gspread.Worksheet:
        import time
        max_retries = 5
        last_exc = None
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    self._spreadsheet = None  # force re-open on retry
                    time.sleep(min(10 * attempt, 60))  # 10s, 20s, 30s, 40s
                try:
                    return self._open().worksheet(tab)
                except gspread.exceptions.WorksheetNotFound:
                    # Auto-create alert_state and meta tabs if missing
                    if tab == TAB_ALERT_STATE:
                        logger.info("Creating missing '%s' tab in Google Sheet.", tab)
                        ws = self._open().add_worksheet(title=tab, rows=1000, cols=len(_AS_COLS))
                        ws.append_row(_AS_COLS)
                        return ws
                    if tab == TAB_META:
                        logger.info("Creating missing '%s' tab in Google Sheet.", tab)
                        ws = self._open().add_worksheet(title=tab, rows=100, cols=len(_META_COLS))
                        ws.append_row(_META_COLS)
                        return ws
                    if tab == TAB_SETTINGS:
                        logger.info("Creating missing '%s' tab in Google Sheet (seeding %d rows).",
                                    tab, len(_SETTINGS_SEED))
                        ws = self._open().add_worksheet(title=tab, rows=100, cols=len(_SETTINGS_COLS))
                        ws.append_row(_SETTINGS_COLS)
                        ws.append_rows([[key, "", note] for key, note in _SETTINGS_SEED])
                        return ws
                    raise
            except gspread.exceptions.WorksheetNotFound:
                raise
            except Exception as e:
                last_exc = e
                logger.warning("Google Sheets transient error (attempt %d/%d): %s", attempt + 1, max_retries, e)
        raise last_exc

    # ── Public read methods ───────────────────────────────────────────────────

    def get_workspaces(self) -> List[Dict]:
        """
        Return list of active workspace dicts.
        Required columns : workspace_name, active
        Optional columns : api_key (Instantly), lemlist_api_key, smartlead_api_key,
                           zapmail_workspace_key_google, zapmail_workspace_key_microsoft,
                           mission_inbox_api_key, premiuminbox_workspace_id,
                           scaledmail_organization_id
        A workspace is valid if it has at least one sending-platform key:
        api_key (Instantly) OR lemlist_api_key OR smartlead_api_key.
        """
        ws = self._worksheet(TAB_WORKSPACES)
        rows = ws.get_all_records()  # no expected_headers — extra cols are optional
        # Normalize: strip whitespace from column header keys (guards against accidental spaces)
        rows = [{k.strip(): v for k, v in row.items()} for row in rows]
        result = []
        for row in rows:
            active = str(row.get("active", "")).strip().upper()
            if active not in ("TRUE", "1", "YES"):
                continue
            ws_name = str(row.get("workspace_name", "")).strip()
            api_key = str(row.get("api_key", "")).strip()
            lemlist_api_key = str(row.get("lemlist_api_key", "")).strip()
            smartlead_api_key = str(row.get("smartlead_api_key", "")).strip()
            if not ws_name or not (api_key or lemlist_api_key or smartlead_api_key):
                logger.warning("Skipping workspace row with missing name or all api keys: %s", row)
                continue
            result.append({
                "workspace_name":           ws_name,
                "api_key":                  api_key,
                # ZapMail workspace keys — one per provider, each optional
                "zapmail_workspace_key_google":    str(row.get("zapmail_workspace_key_google", "")).strip(),
                "zapmail_workspace_key_microsoft": str(row.get("zapmail_workspace_key_microsoft", "")).strip(),
                # Optional Mission Inbox API key — empty string if not present
                "mission_inbox_api_key":    str(row.get("mission_inbox_api_key", "")).strip(),
                # Optional Lemlist API key — empty string if not present
                "lemlist_api_key":          lemlist_api_key,
                # Optional Smartlead API key — empty string if not present
                "smartlead_api_key":        smartlead_api_key,
                # Optional inbox-infrastructure provider IDs — empty string if not present
                "premiuminbox_workspace_id":   str(row.get("premiuminbox_workspace_id", "")).strip(),
                "scaledmail_organization_id":  str(row.get("scaledmail_organization_id", "")).strip(),
            })
        logger.debug("Loaded %d active workspace(s) from Sheets.", len(result))
        return result

    def get_settings(self) -> Dict[str, str]:
        """
        Return the client-editable 'Settings' tab as a dict of {key: value}, for
        non-empty values only. Auto-creates + seeds the tab if missing, and
        appends any seed rows a client may have deleted so the menu stays complete.

        Never raises for a missing/blank tab — returns {} so the caller falls
        back to env vars / defaults.
        """
        ws = self._worksheet(TAB_SETTINGS)
        if not ws.row_values(1):
            ws.append_row(_SETTINGS_COLS)
            ws.append_rows([[key, "", note] for key, note in _SETTINGS_SEED])
            return {}

        rows = ws.get_all_records(expected_headers=_SETTINGS_COLS)
        present_keys = {str(r.get("key", "")).strip() for r in rows}
        missing = [[key, "", note] for key, note in _SETTINGS_SEED if key not in present_keys]
        if missing:
            ws.append_rows(missing)

        settings: Dict[str, str] = {}
        for row in rows:
            key = str(row.get("key", "")).strip()
            val = str(row.get("value", "")).strip()
            if key and val:
                settings[key] = val
        logger.debug("Loaded %d non-empty setting(s) from Sheets.", len(settings))
        return settings

    def get_alert_state(self) -> Dict[str, Dict]:
        """
        Return current alert_state as a dict keyed by email (lowercase).
        Creates the header row automatically if the sheet is empty.
        """
        ws = self._worksheet(TAB_ALERT_STATE)
        if not ws.row_values(1):
            ws.append_row(_AS_COLS)
            return {}
        rows = ws.get_all_records(expected_headers=_AS_COLS)
        state = {}
        for row in rows:
            email = str(row.get("email", "")).strip().lower()
            if email:
                state[email] = {k: row.get(k, "") for k in _AS_COLS}
                try:
                    state[email]["reconnect_attempts"] = int(state[email]["reconnect_attempts"] or 0)
                except ValueError:
                    state[email]["reconnect_attempts"] = 0
        logger.debug("Loaded %d alert_state row(s) from Sheets.", len(state))
        return state

    # ── Public write methods ──────────────────────────────────────────────────

    # ── Cached alert_state index (avoids repeated Google Sheets API calls) ────

    _as_ws: Optional[gspread.Worksheet] = None
    _as_index: Optional[Dict[str, int]] = None

    def _ensure_alert_state_index(self) -> tuple[gspread.Worksheet, Dict[str, int]]:
        """Load the alert_state index once per run; subsequent calls use cache."""
        if self._as_ws is not None and self._as_index is not None:
            return self._as_ws, self._as_index
        ws = self._worksheet(TAB_ALERT_STATE)
        if not ws.row_values(1):
            ws.append_row(_AS_COLS)
        all_emails = ws.col_values(1)[1:]  # skip header
        self._as_ws = ws
        self._as_index = {e.strip().lower(): i + 2 for i, e in enumerate(all_emails) if e.strip()}
        return self._as_ws, self._as_index

    def upsert_alert_state(self, email: str, workspace_name: str,
                           first_detected: str, last_alerted: str,
                           reconnect_attempts: int, status: str) -> None:
        """Insert or update a row in alert_state for the given email."""
        ws, index = self._ensure_alert_state_index()
        row_data = [
            email.lower(), workspace_name, first_detected, last_alerted,
            reconnect_attempts, status,
        ]
        key = email.lower()
        if key in index:
            row_num = index[key]
            ws.update(f"A{row_num}:F{row_num}", [row_data])
            logger.debug("Updated alert_state row %d for %s", row_num, email)
        else:
            ws.append_row(row_data)
            # Update cache: new row is at end (after all existing + header)
            index[key] = len(index) + 2
            logger.debug("Appended alert_state row for %s", email)

    def delete_alert_state(self, email: str) -> None:
        """Remove a row from alert_state (account came back online)."""
        ws, index = self._ensure_alert_state_index()
        key = email.lower()
        if key not in index:
            return
        row_num = index[key]
        ws.delete_rows(row_num)
        # Update cache: remove this key and shift all rows below it up by 1
        del index[key]
        for k, v in index.items():
            if v > row_num:
                index[k] = v - 1
        logger.debug("Deleted alert_state row %d for %s (account recovered)", row_num, email)

    # ── Meta key/value store (idempotency flags) ──────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        """Return the stored value for a meta key, or None if absent."""
        ws = self._worksheet(TAB_META)
        if not ws.row_values(1):
            ws.append_row(_META_COLS)
            return None
        rows = ws.get_all_records(expected_headers=_META_COLS)
        for row in rows:
            if str(row.get("key", "")).strip() == key:
                return str(row.get("value", "")).strip()
        return None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a meta key/value row."""
        ws = self._worksheet(TAB_META)
        if not ws.row_values(1):
            ws.append_row(_META_COLS)
        keys_col = ws.col_values(1)[1:]  # skip header
        for i, existing_key in enumerate(keys_col):
            if existing_key.strip() == key:
                row_num = i + 2
                ws.update(f"A{row_num}:B{row_num}", [[key, value]])
                logger.debug("Updated meta row %d: %s=%s", row_num, key, value)
                return
        ws.append_row([key, value])
        logger.debug("Appended meta row: %s=%s", key, value)

