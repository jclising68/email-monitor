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
        GET /accounts. Returns:
          {email: {
              "health_score": int,
              "timestamp_created": str | None,  # ISO8601 from Instantly
              "warmup_status": int | None,      # 1 = warming up
          }}
        for all connected accounts that have a warmup score.

        timestamp_created is included so callers can grant a grace period
        to brand-new accounts that legitimately start at 0% before ramp-up.
        """
        result: Dict[str, Dict] = {}
        for account in accounts:
            email = str(account.get("email", "")).strip().lower()
            if not email:
                continue
            score = account.get("stat_warmup_score")
            if score is None:
                continue
            try:
                result[email] = {
                    "health_score": int(score),
                    "timestamp_created": account.get("timestamp_created"),
                    "warmup_status": account.get("warmup_status"),
                }
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
          - leads_count, contacted_count
          - unsubscribed_count, link_click_count

        Note: we deliberately do NOT pass exclude_total_leads_count so
        leads_count comes back populated — otherwise the daily report
        shows "Leads: X/0" which is meaningless.
        """
        params: Dict = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

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
            # Instantly v2: POST /campaigns/{id}/pause — json={} needed because
            # the session always sets Content-Type: application/json
            self._request("POST", f"/campaigns/{campaign_id}/pause", json={})
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
            # Instantly v2: POST /accounts/{email}/pause — json={} needed because
            # the session always sets Content-Type: application/json
            self._request("POST", f"/accounts/{email}/pause", json={})
            logger.info("Paused account %s.", email)
            return True
        except InstantlyAPIError as exc:
            logger.error("Failed to pause account %s: %s", email, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error pausing account %s: %s", email, exc)
            return False

    def update_daily_limit(self, email: str, daily_limit: int) -> bool:
        """Set the daily campaign sending limit for an account. Returns True on success."""
        try:
            self._request("PATCH", f"/accounts/{email}", json={"daily_limit": daily_limit})
            logger.info("Set daily_limit=%d for account %s.", daily_limit, email)
            return True
        except InstantlyAPIError as exc:
            logger.error("Failed to update daily_limit for %s: %s", email, exc)
            return False
        except Exception as exc:
            logger.error("Unexpected error updating daily_limit for %s: %s", email, exc)
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

    # ── Error classification ──────────────────────────────────────────────────
    #
    # Some "disconnected" states in Instantly are NOT reconnectable:
    #   - Sending limit hit at Google/Microsoft (auto-recovers in ~24h)
    #   - Provider flagged suspicious activity (needs human intervention)
    # Pushing reconnects at these accounts wastes API calls, emits misleading
    # Slack alerts, and leaves the user chasing a non-existent auth problem.
    #
    # We classify by inspecting Instantly's `status_message` — a dict with
    # the raw SMTP response text from the provider (verified live against
    # Textgrid workspace on 2026-04-21). Pattern matching is intentionally
    # loose (case-insensitive substring) so new wording variations still map.
    #
    # Provider-agnostic: same classifier applies to Google (2), Microsoft (3),
    # IMAP (1), and AWS (4) accounts because all SMTP providers return
    # enhanced status codes (RFC 3463) in similar shapes.

    # Signatures checked in priority order — first match wins.
    # Keys = category; values = list of case-insensitive substrings.
    _ERROR_SIGNATURES = (
        ("sending_limit_exceeded", (
            "5.4.5",                         # Gmail daily user sending limit
            "daily user sending limit",
            "daily sending quota",
            "rate limit exceeded",
            "throttled",
            "exceeded the message limit",    # Outlook/O365
            "exceeded its quota",
        )),
        ("suspicious_activity_blocked", (
            "5.7.1",                         # Gmail spam / suspicious
            "unusual sending",
            "suspicious",
            "this message was blocked",
            "message rejected due to",
            "550-5.7.26",                    # MS: unauthenticated relay
        )),
        ("authentication_failure", (
            "5.7.8",                         # auth required / bad creds
            "535",                           # SMTP auth failed
            "authentication failed",
            "username and password not accepted",
            "invalid credentials",
            "invalid login",
            "application-specific password required",
        )),
        ("mailbox_full", (
            "5.2.2",
            "mailbox full",
            "over quota",
            "quota exceeded",
        )),
    )

    # Categories whose auto-recovery path does NOT involve reconnecting.
    # Reconnect is still attempted for authentication_failure and unknowns.
    NON_RECONNECTABLE_CATEGORIES = frozenset({
        "sending_limit_exceeded",
        "suspicious_activity_blocked",
        "mailbox_full",
    })

    @staticmethod
    def classify_account_error(account: Dict) -> Dict:
        """
        Inspect an account's status and status_message to decide how to handle it.

        Returns a dict:
          {
            "category": str,        # see categories below
            "detail": str,          # human-readable summary of the underlying error
            "response_code": int|None,  # SMTP response code if parseable
            "autofix_failed": bool,     # Instantly's autofix signal (hint only)
            "should_reconnect": bool,   # False for provider-side issues
            "auto_recoverable": bool,   # True if the error clears itself (e.g. daily limits)
          }

        Categories:
          - connected                   — status == 1/active
          - paused                      — status == 2 (user-paused)
          - sending_limit_exceeded      — provider daily-send cap hit; recovers in ~24h
          - suspicious_activity_blocked — provider flagged sends; needs human
          - authentication_failure      — credentials bad; reconnect will help
          - mailbox_full                — recipient-side quota (rare on sender)
          - disconnected_unknown        — error with no recognized signature
        """
        # Healthy states short-circuit — no inspection needed.
        if InstantlyClient.is_connected(account):
            return {
                "category": "connected", "detail": "", "response_code": None,
                "autofix_failed": False, "should_reconnect": False,
                "auto_recoverable": False,
            }
        if InstantlyClient.is_paused(account):
            return {
                "category": "paused", "detail": "", "response_code": None,
                "autofix_failed": False, "should_reconnect": False,
                "auto_recoverable": False,
            }

        sm = account.get("status_message")
        response_code: Optional[int] = None
        text = ""

        # Instantly returns status_message as a dict for SMTP errors (verified live)
        # but defend against the string / None shapes other APIs sometimes send.
        if isinstance(sm, dict):
            rc = sm.get("responseCode") or sm.get("response_code")
            try:
                response_code = int(rc) if rc is not None else None
            except (TypeError, ValueError):
                response_code = None
            # Concatenate every text-ish field so substring matching finds
            # the signature wherever Instantly chose to put it.
            for k in ("response", "e_message", "code", "command", "message"):
                v = sm.get(k)
                if v:
                    text += f" {v}"
        elif isinstance(sm, str):
            text = sm
        text = text.lower()

        autofix_failed = bool(account.get("autofix_failed", False))

        # Walk signatures in priority order — first match wins.
        matched_category = "disconnected_unknown"
        for cat, signatures in InstantlyClient._ERROR_SIGNATURES:
            if any(sig in text for sig in signatures):
                matched_category = cat
                break

        # Trim detail to the first 200 chars of the e_message (most human-readable)
        # fall back to response, then to status value.
        detail = ""
        if isinstance(sm, dict):
            detail = str(sm.get("e_message") or sm.get("response") or "").strip()
        elif isinstance(sm, str):
            detail = sm.strip()
        if not detail:
            detail = f"status={account.get('status')}"
        if len(detail) > 240:
            detail = detail[:237] + "..."

        should_reconnect = matched_category not in InstantlyClient.NON_RECONNECTABLE_CATEGORIES
        auto_recoverable = matched_category in ("sending_limit_exceeded",)

        return {
            "category": matched_category,
            "detail": detail,
            "response_code": response_code,
            "autofix_failed": autofix_failed,
            "should_reconnect": should_reconnect,
            "auto_recoverable": auto_recoverable,
        }

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
