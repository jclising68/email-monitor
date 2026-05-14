"""
lemlist_client.py — Thin wrapper around the Lemlist API.

Auth: HTTP Basic Authentication (empty login, API key as password)
Base URL: https://api.lemlist.com/api

Handles:
  - Basic auth (one key per workspace)
  - Exponential backoff on 429 / transient 5xx
  - Per-account SMTP/IMAP connectivity checks
  - Campaign stats (batch) and pause
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.lemlist.com/api"
_MAX_RETRIES = 5


class LemlistAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class LemlistClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.auth = HTTPBasicAuth("", api_key)

    # ── Low-level request with retry ─────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self._base_url}{path}"
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Lemlist %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                logger.warning(
                    "Lemlist rate limited on %s %s (attempt %d/%d); waiting %ds",
                    method, path, attempt, _MAX_RETRIES, retry_after,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(retry_after)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 500:
                logger.warning(
                    "Lemlist server error %d on %s %s (attempt %d/%d)",
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
                raise LemlistAPIError(resp.status_code, str(detail))

            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise LemlistAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    # ── Account endpoints ─────────────────────────────────────────────────────

    def get_accounts(self) -> List[Dict]:
        """
        GET /user/channels
        Returns list of email account dicts, each with: id, email, provider.
        """
        data = self._request("GET", "/user/channels")
        return data.get("email", {}).get("accounts", [])

    def test_account(self, account_id: str) -> Dict:
        """
        POST /user/email-accounts/{id}/test
        Runs a live SMTP + IMAP connectivity check.
        Returns {smtp: {success: bool}, imap: {success: bool}}.
        Returns empty dict on error (treated as disconnected by is_connected()).

        NOTE: Only meaningful for provider="custom" (raw SMTP/IMAP accounts).
        Google/Microsoft accounts use OAuth2 — the SMTP/IMAP test always
        returns false for them even when they are fully connected, because
        OAuth2 tokens are not usable as raw SMTP credentials.
        """
        try:
            return self._request("POST", f"/user/email-accounts/{account_id}/test")
        except LemlistAPIError as exc:
            logger.warning("Lemlist: connectivity test failed for %s: %s", account_id, exc)
            return {}
        except Exception as exc:
            logger.warning("Lemlist: unexpected error testing %s: %s", account_id, exc)
            return {}

    @staticmethod
    def is_oauth_provider(provider: str) -> bool:
        """True for Google/Microsoft accounts that use OAuth2 (not raw SMTP/IMAP)."""
        return provider.lower() in ("google", "microsoft")

    @staticmethod
    def is_connected(test_result: Dict) -> bool:
        """True if SMTP connectivity test succeeded (only reliable for custom SMTP/IMAP)."""
        return bool(test_result.get("smtp", {}).get("success", False))

    # ── Campaign endpoints ────────────────────────────────────────────────────

    def get_campaigns(self) -> List[Dict]:
        """
        GET /campaigns
        Returns list of campaigns, each with: _id, name, status.
        Status values: running, paused, draft, ended, archived, errors.
        """
        try:
            result = self._request("GET", "/campaigns")
            if isinstance(result, list):
                return result
            return []
        except LemlistAPIError as exc:
            logger.warning("Lemlist: failed to get campaigns: %s", exc)
            return []

    def get_campaign_stats_batch(
        self,
        campaign_ids: List[str],
        start_date: str,
        end_date: str,
    ) -> List[Dict]:
        """
        POST /v2/campaigns/stats/batch
        Returns list of stats from results[].
        Key fields: campaignId, messagesSent, messagesBounced, replied,
                    delivered, nbLeads, nbLeadsLaunched, opened, clicked.
        Batches in chunks of 100 (API max).
        """
        all_results: List[Dict] = []
        for i in range(0, len(campaign_ids), 100):
            chunk = campaign_ids[i:i + 100]
            try:
                data = self._request("POST", "/v2/campaigns/stats/batch", json={
                    "campaignIds": chunk,
                    "startDate": start_date,
                    "endDate": end_date,
                })
                all_results.extend(data.get("results", []))
            except LemlistAPIError as exc:
                logger.warning("Lemlist: campaign stats batch failed: %s", exc)
        return all_results

    def pause_campaign(self, campaign_id: str) -> bool:
        """POST /campaigns/{id}/pause. Returns True on success."""
        try:
            self._request("POST", f"/campaigns/{campaign_id}/pause")
            logger.info("Lemlist: paused campaign %s.", campaign_id)
            return True
        except LemlistAPIError as exc:
            logger.error("Lemlist: failed to pause campaign %s: %s", campaign_id, exc)
            return False
        except Exception as exc:
            logger.error("Lemlist: unexpected error pausing campaign %s: %s", campaign_id, exc)
            return False
