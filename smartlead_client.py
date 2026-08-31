"""
smartlead_client.py — Thin wrapper around the Smartlead API.

Auth     : api_key passed as a query parameter on every request
Base URL : https://server.smartlead.ai/api/v1

Handles:
  - api_key query-param auth (one key per workspace)
  - Exponential backoff on 429 / transient 5xx
  - Paginated email-account listing (connection status is in the payload —
    no live SMTP/IMAP test needed, unlike Lemlist)
  - Campaign listing, aggregate analytics, and pause
  - Per-account daily-send-limit update
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://server.smartlead.ai/api/v1"
_MAX_RETRIES = 5
_PAGE_SIZE = 100  # Smartlead max

# Smartlead account "type" values that authenticate via OAuth2 (Google/Microsoft).
# For these, a broken connection must be fixed by re-authorising in the Smartlead
# UI — there is no credential we can push. Custom SMTP accounts could in principle
# be re-pushed, but v1 treats every provider as manual-reconnect (like Lemlist).
_OAUTH_TYPES = {"GMAIL", "GOOGLE", "OUTLOOK", "MICROSOFT", "OFFICE365"}


class SmartleadAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class SmartleadClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL):
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._session = requests.Session()

    # ── Low-level request with retry ─────────────────────────────────────────

    def _request(self, method: str, path: str, *, params: Optional[Dict] = None, **kwargs):
        url = f"{self._base_url}{path}"
        params = dict(params or {})
        params["api_key"] = self._api_key
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, params=params, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Smartlead %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                logger.warning(
                    "Smartlead rate limited on %s %s (attempt %d/%d); waiting %ds",
                    method, path, attempt, _MAX_RETRIES, retry_after,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(retry_after)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 500:
                logger.warning(
                    "Smartlead server error %d on %s %s (attempt %d/%d)",
                    resp.status_code, method, path, attempt, _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise SmartleadAPIError(resp.status_code, str(detail))

            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise SmartleadAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    # ── Account endpoints ────────────────────────────────────────────────────

    def get_email_accounts(self) -> List[Dict]:
        """
        GET /email-accounts/ — all sender accounts on the workspace (auto-paginates).

        Each account dict includes: id, from_name, from_email, type,
        is_smtp_success, is_imap_success, smtp_failure_error, imap_failure_error,
        message_per_day, daily_sent_count, signature, custom_tracking_domain,
        and a nested warmup_details {status, total_sent_count, total_spam_count,
        warmup_reputation}.
        """
        all_accounts: List[Dict] = []
        offset = 0
        while True:
            data = self._request(
                "GET", "/email-accounts/",
                params={"offset": offset, "limit": _PAGE_SIZE},
            )
            page = data if isinstance(data, list) else data.get("data", [])
            if not page:
                break
            all_accounts.extend(page)
            if len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
        logger.info("Smartlead: loaded %d email account(s).", len(all_accounts))
        return all_accounts

    def update_daily_limit(self, account_id, new_limit: int) -> bool:
        """
        POST /email-accounts/{id} — set max_email_per_day.
        Returns True on success. Never raises (monitoring must continue).
        """
        try:
            self._request(
                "POST", f"/email-accounts/{account_id}",
                json={"max_email_per_day": new_limit},
            )
            logger.info("Smartlead: set daily limit for account %s to %d.", account_id, new_limit)
            return True
        except Exception as exc:
            logger.error("Smartlead: failed to update daily limit for %s: %s", account_id, exc)
            return False

    # ── Campaign endpoints ───────────────────────────────────────────────────

    def get_campaigns(self) -> List[Dict]:
        """
        GET /campaigns/ — list campaigns.
        Each has: id, name, status (DRAFTED | ACTIVE | COMPLETED | STOPPED | PAUSED).
        """
        try:
            result = self._request("GET", "/campaigns/")
            if isinstance(result, list):
                return result
            return result.get("data", []) if isinstance(result, dict) else []
        except SmartleadAPIError as exc:
            logger.warning("Smartlead: failed to list campaigns: %s", exc)
            return []

    def get_campaign_analytics(self, campaign_id) -> Dict:
        """
        GET /campaigns/{id}/analytics — aggregate counters for one campaign.
        Field names vary by account age; callers read defensively.
        Returns {} on error.
        """
        try:
            result = self._request("GET", f"/campaigns/{campaign_id}/analytics")
            return result if isinstance(result, dict) else {}
        except SmartleadAPIError as exc:
            logger.warning("Smartlead: analytics failed for campaign %s: %s", campaign_id, exc)
            return {}

    def pause_campaign(self, campaign_id) -> bool:
        """POST /campaigns/{id}/status  body {"status": "PAUSED"}. Returns True on success."""
        try:
            self._request("POST", f"/campaigns/{campaign_id}/status", json={"status": "PAUSED"})
            logger.info("Smartlead: paused campaign %s.", campaign_id)
            return True
        except Exception as exc:
            logger.error("Smartlead: failed to pause campaign %s: %s", campaign_id, exc)
            return False

    # ── Classification helpers ───────────────────────────────────────────────

    @staticmethod
    def is_connected(account: Dict) -> bool:
        """
        True if the account's SMTP check is passing. Smartlead reports this
        directly in the account payload, so no live probe is required.
        Falls back to True when the field is absent (older payloads) to avoid
        false disconnection alerts.
        """
        smtp = account.get("is_smtp_success")
        if smtp is None:
            return True
        return bool(smtp)

    @staticmethod
    def is_oauth(account: Dict) -> bool:
        """True for Google/Microsoft accounts (OAuth2 — manual reconnect only)."""
        return str(account.get("type", "")).strip().upper() in _OAUTH_TYPES

    @staticmethod
    def warmup_health(account: Dict) -> Optional[Dict]:
        """
        Pull warmup stats from an account payload. Returns a dict with
        health_score (0-100), spam_rate (0-100), and status — or None if the
        account has no warmup data.
        """
        wd = account.get("warmup_details") or account.get("warmup") or {}
        if not isinstance(wd, dict) or not wd:
            return None
        sent = int(wd.get("total_sent_count") or 0)
        spam = int(wd.get("total_spam_count") or 0)
        rep = wd.get("warmup_reputation")
        try:
            # Smartlead sometimes returns "98%" as a string
            health_score = float(str(rep).rstrip("%")) if rep not in (None, "") else None
        except (TypeError, ValueError):
            health_score = None
        spam_rate = (spam / sent * 100) if sent > 0 else 0.0
        return {
            "health_score": health_score,
            "spam_rate": spam_rate,
            "sent": sent,
            "spam": spam,
            "status": str(wd.get("status", "")),
        }
