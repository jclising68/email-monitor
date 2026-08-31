"""
premiuminbox_client.py — Premium Inboxes Client API wrapper.

Base URL  : https://api.premiuminboxes.com/api   (override via PREMIUMINBOX_API_BASE_URL)
Auth      : x-api-token: <AGENCY_TOKEN>          (32-char key, NO Bearer prefix)
Workspace : x-workspace-id: <WORKSPACE_ID>       (one per client, from the Sheet)
Get token : portal.premiuminboxes.com -> Settings -> API Token

What the PUBLIC docs describe: manage workspaces, place / cancel orders, list &
cancel email accounts, list / cancel / reactivate subscriptions. There is NO
documented "reconnect / re-sync a mailbox" action — only list + cancel for
accounts. reconnect_email() therefore stays a stub (see its docstring).

This client uses only GET reads and tolerates 404s, so a wrong path guess
degrades to "alert only", never a destructive call.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.premiuminboxes.com/api"
_MAX_RETRIES = 4

_PAYMENT_KEYWORDS = ("payment pending", "payment overdue", "past due", "suspended",
                     "insufficient funds", "subscription expired", "billing", "overdue",
                     "cancelled", "canceled", "unpaid")


class PremiumInboxAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class PremiumInboxClient:
    # Workspace-level billing status detected from API responses.
    workspace_billing_status: Optional[str] = None

    def __init__(self, api_token: str, workspace_id: str = "", base_url: str = _DEFAULT_BASE_URL):
        self._api_token = api_token
        self._workspace_id = workspace_id
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self.workspace_billing_status = None

    def _headers(self) -> Dict[str, str]:
        h = {
            "content-type": "application/json",
            "x-api-token": self._api_token,
        }
        if self._workspace_id:
            h["x-workspace-id"] = self._workspace_id
        return h

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = self._session.request(
                    method, url, headers=self._headers(), timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("PremiumInbox %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning("PremiumInbox HTTP %d (attempt %d/%d)", resp.status_code, attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise PremiumInboxAPIError(resp.status_code, str(detail))

            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise PremiumInboxAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    def _get_first_ok(self, paths: List[str]):
        """Try each candidate path; return the first non-404 JSON body, or None."""
        for p in paths:
            try:
                return self._request("GET", p)
            except PremiumInboxAPIError as exc:
                if exc.status_code in (404, 405):
                    continue
                raise
        logger.warning("PremiumInbox: none of %s responded — is the path published yet?", paths)
        return None

    # ── Reads (documented capabilities: workspaces, orders, subscriptions,
    #    email accounts — list & cancel only) ─────────────────────────────────

    def list_email_accounts(self) -> List[Dict]:
        """
        All email accounts for the workspace (x-workspace-id header scopes it).
        Each dict is expected to carry at least 'email' and a status field.
        Also runs billing detection on the response envelope.
        """
        result = self._get_first_ok(
            ["/email-accounts", "/client/email-accounts", "/accounts", "/mailboxes"]
        )
        if isinstance(result, dict):
            self._detect_billing_status(result)

        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            data = result.get("data", result)
            if isinstance(data, dict):
                for key in ("email_accounts", "emailAccounts", "mailboxes", "accounts", "items", "results"):
                    if isinstance(data.get(key), list):
                        return data[key]
            elif isinstance(data, list):
                return data
        return []

    def check_subscription_status(self) -> Optional[str]:
        """
        Check whether any subscription for this workspace has a payment problem.
        Returns a descriptive string, or None if healthy / unknown.
        """
        try:
            result = self._get_first_ok(
                ["/subscriptions", "/client/subscriptions", "/orders"]
            )
        except Exception as exc:
            logger.warning("PremiumInbox: subscription check failed: %s (non-fatal)", exc)
            return None
        if result is None:
            return None

        subs = []
        if isinstance(result, dict):
            subs = result.get("data") or result.get("subscriptions") or []
        elif isinstance(result, list):
            subs = result

        for sub in subs:
            status = str(sub.get("status", "")).lower()
            if any(kw in status for kw in _PAYMENT_KEYWORDS):
                issue = f"Subscription {sub.get('status', '?')}"
                self.workspace_billing_status = issue
                logger.warning("PremiumInbox subscription issue: %s", issue)
                return issue
        return None

    def _detect_billing_status(self, response: dict) -> None:
        if not isinstance(response, dict):
            return
        for field in ("message", "status", "billingStatus", "billing_status",
                      "subscriptionStatus", "subscription_status", "warning", "error"):
            val = str(response.get(field, "")).lower()
            if any(kw in val for kw in _PAYMENT_KEYWORDS):
                self.workspace_billing_status = str(response.get(field, ""))
                logger.warning("PremiumInbox: workspace billing issue detected: %s",
                               self.workspace_billing_status)
                return

    @staticmethod
    def is_mailbox_suspended(mailbox: Dict) -> bool:
        status = str(mailbox.get("status", "")).lower()
        return status in ("suspended", "payment_pending", "past_due", "inactive", "disabled", "cancelled")

    # ── Reconnect ───────────────────────────────────────────────────────────

    def reconnect_email(self, email: str, cached_mailbox: Optional[Dict] = None) -> tuple:
        """
        NOT IMPLEMENTED — the documented Premium Inboxes Client API exposes only
        *list* and *cancel* for email accounts, plus workspace / order /
        subscription management. There is no published "reconnect" or
        "re-push to sequencer" action.

        To enable auto-reconnect: ask Premium Inboxes support for the endpoint
        that re-syncs / re-exports an existing mailbox to the connected
        sequencer, then implement it here.

        Returns (success=False, permanent_failure=False) so the caller falls
        back to alert-only handling.
        """
        logger.info(
            "PremiumInbox: no public reconnect endpoint — %s stays alert-only. "
            "See reconnect_email() docstring.", email,
        )
        return False, False
