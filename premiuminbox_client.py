"""
premiuminbox_client.py — Premium Inboxes Client API wrapper.

Auth      : x-api-token: <AGENCY_TOKEN>   (one token for the whole agency)
Workspace : x-workspace-id: <WORKSPACE_ID>  (one per client, from the Sheet)
Base URL  : https://portal.premiuminboxes.com/api/client  (override via PREMIUMINBOX_API_BASE_URL)

Status: PHASE 1.
  - list_email_accounts() + subscription/billing detection are wired and used
    for disconnection *alerting* today.
  - reconnect_email() is a stub: the "re-push mailbox to the sequencer" action
    and the exact endpoint paths need to be confirmed against the portal docs
    (requires an API token). Until then, disconnections are alert-only.

TODO(after API token):
  - confirm base URL + the email-accounts list path and response shape
  - confirm the subscriptions list path + status field names
  - implement reconnect_email() (re-export / re-provision action)
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://portal.premiuminboxes.com/api/client"
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

    # ── Reads ────────────────────────────────────────────────────────────────

    def list_email_accounts(self) -> List[Dict]:
        """
        Return all email accounts in the workspace. Each dict is expected to carry
        at least 'email' and a status field. Also runs billing detection on the
        response envelope.

        TODO(after API token): confirm the path ('/email-accounts' assumed) and
        the response shape (list vs {data: [...]} vs {mailboxes: [...]}).
        """
        result = self._request("GET", "/email-accounts")
        self._detect_billing_status(result)

        if isinstance(result, list):
            return result
        data = result.get("data", result)
        if isinstance(data, dict):
            for key in ("email_accounts", "emailAccounts", "mailboxes", "accounts", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        elif isinstance(data, list):
            return data
        logger.warning("PremiumInbox list_email_accounts: unexpected response shape.")
        return []

    def check_subscription_status(self) -> Optional[str]:
        """
        Check whether any subscription for this workspace has a payment problem.
        Returns a descriptive string, or None if healthy / unknown.

        TODO(after API token): confirm the path ('/subscriptions' assumed) and
        the status field ('status' assumed; values like ACTIVE / PAST_DUE /
        CANCELLED / PAYMENT_PENDING).
        """
        try:
            result = self._request("GET", "/subscriptions")
        except Exception as exc:
            logger.warning("PremiumInbox: subscription check failed: %s (non-fatal)", exc)
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

    # ── Reconnect (stub) ─────────────────────────────────────────────────────

    def reconnect_email(self, email: str, cached_mailbox: Optional[Dict] = None) -> tuple:
        """
        Re-push / re-provision a mailbox so the sequencer picks it back up.

        NOT IMPLEMENTED — needs the portal API docs (requires an API token).
        Returns (success=False, permanent_failure=False) so the caller falls
        back to alert-only handling.
        """
        logger.info(
            "PremiumInbox: auto-reconnect not implemented yet for %s — alert-only. "
            "Provide an API token to enable this.", email,
        )
        return False, False
