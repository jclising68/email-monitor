"""
instantly_client.py — Thin wrapper around the Instantly API v2.

Handles:
  - Bearer auth (one key per workspace)
  - Cursor-based pagination (starting_after)
  - Exponential backoff on 429 / transient 5xx
  - Per-request error classification
"""
from __future__ import annotations

import logging
import string
import time
from typing import Dict, Iterator, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.instantly.ai/api/v2"
_MAX_RETRIES = 5
_PAGE_LIMIT = 100  # max accounts per page (Instantly default is 10; cap at 100)


class InstantlyAPIError(Exception):
    """Raised for non-retryable API errors (4xx that are not 429)."""
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class InstantlyClient:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    # ── Low-level request with retry ─────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Request %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code == 429:
                # Respect Retry-After if present, else exponential backoff
                retry_after = int(resp.headers.get("Retry-After", backoff))
                logger.warning(
                    "Rate limited on %s %s (attempt %d/%d); waiting %ds",
                    method, path, attempt, _MAX_RETRIES, retry_after,
                )
                if attempt < _MAX_RETRIES:
                    time.sleep(retry_after)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 500:
                logger.warning(
                    "Server error %d on %s %s (attempt %d/%d)",
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
                raise InstantlyAPIError(resp.status_code, str(detail))

            # 2xx — try to parse JSON, return empty dict for 204
            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise InstantlyAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    # ── Account endpoints ─────────────────────────────────────────────────────

    def get_all_accounts(self) -> List[Dict]:
        """
        Return all email accounts for this workspace.

        Three-phase approach to work around Instantly's timestamp-based cursor,
        which skips accounts that share the same timestamp_created as a page boundary:
          Phase 1: cursor pagination — discovers initial domains, collects most accounts.
          Phase 2: alphabet sweep (a-z) — discovers domains missed by cursor pagination.
          Phase 3: per-domain search — fetches all accounts for every discovered domain.

        Result is deduplicated by email and matches the Instantly UI count exactly.
        """
        accounts: List[Dict] = []
        seen_emails: set = set()
        seen_cursors: set = set()
        domains: set = set()
        starting_after: Optional[str] = None

        # ── Phase 1: cursor pagination ────────────────────────────────────────
        while True:
            params: Dict = {"limit": _PAGE_LIMIT}
            if starting_after:
                params["starting_after"] = starting_after

            data = self._request("GET", "/accounts", params=params)
            items = data.get("items") or data.get("accounts") or []

            for item in items:
                email = str(item.get("email", "")).strip().lower()
                if email:
                    domains.add(email.split("@")[-1])
                    if email not in seen_emails:
                        seen_emails.add(email)
                        accounts.append(item)

            cursor = data.get("next_starting_after") or data.get("next_cursor") or data.get("nextCursor")
            if not items or not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            starting_after = cursor

        # ── Phase 2: alphabet sweep — discover domains cursor pagination missed ─
        # Paginate each letter so workspaces with >100 accounts per letter are fully covered.
        for letter in string.ascii_lowercase:
            ltr_after: Optional[str] = None
            ltr_cursors: set = set()
            while True:
                params = {"limit": _PAGE_LIMIT, "search": letter}
                if ltr_after:
                    params["starting_after"] = ltr_after
                data = self._request("GET", "/accounts", params=params)
                items = data.get("items") or data.get("accounts") or []
                for item in items:
                    email = str(item.get("email", "")).strip().lower()
                    if email and "@" in email:
                        domains.add(email.split("@")[-1])
                ltr_cursor = data.get("next_starting_after") or data.get("next_cursor") or data.get("nextCursor")
                if not items or not ltr_cursor or ltr_cursor in ltr_cursors:
                    break
                ltr_cursors.add(ltr_cursor)
                ltr_after = ltr_cursor

        # ── Phase 3: per-domain search for ALL discovered domains ─────────────
        for domain in sorted(domains):
            dom_cursors: set = set()
            dom_after: Optional[str] = None
            while True:
                params = {"limit": _PAGE_LIMIT, "search": domain}
                if dom_after:
                    params["starting_after"] = dom_after
                data = self._request("GET", "/accounts", params=params)
                items = data.get("items") or data.get("accounts") or []
                for item in items:
                    email = str(item.get("email", "")).strip().lower()
                    if email and email not in seen_emails:
                        seen_emails.add(email)
                        accounts.append(item)
                dom_cursor = data.get("next_starting_after") or data.get("next_cursor") or data.get("nextCursor")
                if not items or not dom_cursor or dom_cursor in dom_cursors:
                    break
                dom_cursors.add(dom_cursor)
                dom_after = dom_cursor

        logger.debug("Fetched %d unique account(s) for this workspace.", len(accounts))
        return accounts

    def get_account(self, email: str) -> Optional[Dict]:
        """Fetch a single account by email. Returns None if not found (404)."""
        try:
            return self._request("GET", f"/accounts/{email}")
        except InstantlyAPIError as exc:
            if exc.status_code == 404:
                return None
            raise

    def delete_account(self, email: str) -> bool:
        """Delete an account. Returns True on success, False if not found."""
        try:
            self._request("DELETE", f"/accounts/{email}")
            logger.info("Deleted account %s from Instantly.", email)
            return True
        except InstantlyAPIError as exc:
            if exc.status_code == 404:
                logger.warning("Account %s not found on delete (already removed?).", email)
                return False
            raise

    def add_account(self, payload: dict) -> Dict:
        """
        Add a new account.  payload keys (all required for SMTP/IMAP):
          email, smtp_host, smtp_port, smtp_username, smtp_password,
          imap_host, imap_port, imap_username, imap_password
        """
        result = self._request("POST", "/accounts", json=payload)
        logger.info("Added account %s to Instantly.", payload.get("email"))
        return result

    # ── DNS vitals ────────────────────────────────────────────────────────────

    def check_vitals(self, email: str) -> Optional[Dict]:
        """
        POST /accounts/test/vitals for an email address.
        Returns the vitals dict or None on non-fatal error.
        Expected response shape:
          {
            "spf": {"valid": true/false, ...},
            "dkim": {"valid": true/false, ...},
            "dmarc": {"valid": true/false, ...}
          }
        """
        try:
            return self._request("POST", "/accounts/test/vitals", json={"email": email})
        except InstantlyAPIError as exc:
            logger.warning("Vitals check failed for %s: %s", email, exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected error during vitals check for %s: %s", email, exc)
            return None

    # ── Warmup health (from account data) ───────────────────────────────────

    @staticmethod
    def extract_warmup_health(accounts: List[Dict]) -> Dict[str, Dict]:
        """
        Extract warmup health scores from account data already fetched by
        get_all_accounts(). No extra API call needed.

        Each account has `stat_warmup_score` (0-100) in the response from
        GET /accounts. Returns {email: {"health_score": int}} for all
        connected accounts that have a warmup score.
        """
        result: Dict[str, Dict] = {}
        for account in accounts:
            email = str(account.get("email", "")).strip().lower()
            if not email:
                continue
            score = account.get("stat_warmup_score")
            if score is not None:
                try:
                    result[email] = {"health_score": int(score)}
                except (ValueError, TypeError):
                    pass
        return result

    # ── Campaign analytics ─────────────────────────────────────────────────────

    def get_campaign_analytics(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict]:
        """
        GET /campaigns/analytics — performance metrics for all campaigns.

        Returns list of campaign dicts with:
          - campaign_name, campaign_id, campaign_status
          - emails_sent_count, bounced_count, reply_count, open_count
          - unsubscribed_count, link_click_count
        """
        params: Dict = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        params["exclude_total_leads_count"] = "true"  # must be lowercase string for API

        try:
            result = self._request("GET", "/campaigns/analytics", params=params)
            if isinstance(result, list):
                return result
            return result.get("items", result.get("campaigns", []))
        except InstantlyAPIError as exc:
            logger.warning("Campaign analytics failed: %s", exc)
            return []
        except Exception as exc:
            logger.warning("Campaign analytics unexpected error: %s", exc)
            return []

    # ── Automated actions ──────────────────────────────────────────────────────

    def pause_campaign(self, campaign_id: str) -> bool:
        """Pause a campaign to stop all sending. Returns True on success."""
        try:
            # Instantly v2: PATCH /campaigns/{id} with status=2 (paused)
            self._request("PATCH", f"/campaigns/{campaign_id}", json={"status": 2})
            logger.info("Paused campaign %s.", campaign_id)
            return True
        except InstantlyAPIError as exc:
            logger.error("Failed to pause campaign %s: %s", campaign_id, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error pausing campaign %s: %s", campaign_id, exc)
            return False

    def pause_account(self, email: str) -> bool:
        """Pause an account — stops all sending and warmup. Returns True on success."""
        try:
            # Instantly v2: PATCH /accounts/{email} with status=2 (paused)
            self._request("PATCH", f"/accounts/{email}", json={"status": 2})
            logger.info("Paused account %s.", email)
            return True
        except InstantlyAPIError as exc:
            logger.error("Failed to pause account %s: %s", email, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error pausing account %s: %s", email, exc)
            return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def is_connected(account: Dict) -> bool:
        """True if the account status indicates it is healthy / connected."""
        status = str(account.get("status", "")).lower()
        # Instantly status 1 = Active/Connected
        return status in ("connected", "active", "1", "ok")

    @staticmethod
    def is_paused(account: Dict) -> bool:
        """True if the account is intentionally paused (status 2).
        Paused accounts should NOT be reconnected or alerted — the user paused them on purpose."""
        status = str(account.get("status", "")).lower()
        return status in ("2", "paused")

    @staticmethod
    def is_warming_up(account: Dict) -> bool:
        """True if the account is in warmup mode (warmup_status == 1).
        Warming-up accounts are connected but intentionally sending at reduced volume."""
        warmup_status = account.get("warmup_status")
        return str(warmup_status) == "1"

    @staticmethod
    def parse_dns_failures(vitals: Dict) -> List[str]:
        """Return list of DNS record names that failed (e.g. ['DKIM', 'DMARC'])."""
        failures = []
        for record in ("spf", "dkim", "dmarc"):
            info = vitals.get(record, {})
            if isinstance(info, dict):
                valid = info.get("valid", info.get("passed", True))
            else:
                valid = bool(info)
            if not valid:
                failures.append(record.upper())
        return failures

    @staticmethod
    def has_tracking_domain_issue(account: Dict) -> Optional[str]:
        """
        Check if account has a tracking domain configured but it's not active.
        Returns the issue description string, or None if healthy/no tracking domain.
        """
        td_name = str(account.get("tracking_domain_name") or "").strip()
        if not td_name:
            return None  # no tracking domain configured — not an issue
        td_status = str(account.get("tracking_domain_status") or "").strip()
        if td_status == "CTD_ACTIVE":
            return None  # healthy
        return f"tracking domain '{td_name}' status: {td_status or 'unknown'}"
