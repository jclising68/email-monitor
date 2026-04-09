"""
missioninbox_client.py — Mission Inbox REST API wrapper.

Base URL : https://api.v4.missioninbox.com
Auth     : X-Server-API-Key: <API_KEY> header

SMTP/IMAP settings are fixed for all Mission Inbox mailboxes:
  SMTP host : main.outboxment.com, port 587
  IMAP host : main.outboxment.com, port 993
  Username  = email address
  Password  = fetched live from the API — no sheet row needed
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.v4.missioninbox.com"

# Fixed for all Mission Inbox mailboxes (confirmed via DNS/MX lookup)
MI_SMTP_HOST = "main.outboxment.com"
MI_SMTP_PORT = 587
MI_IMAP_HOST = "main.outboxment.com"
MI_IMAP_PORT = 993


class MissionInboxAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class MissionInboxClient:
    # Workspace-level billing status detected from API responses.
    workspace_billing_status: Optional[str] = None

    _PAYMENT_KEYWORDS = ("payment pending", "payment overdue", "suspended", "insufficient funds",
                         "subscription expired", "billing", "overdue", "account disabled")

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL):
        self._api_key = api_key
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self.workspace_billing_status = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Server-API-Key": self._api_key,
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, 4):
            try:
                resp = self._session.request(
                    method, url, headers=self._headers(), timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("MissionInbox %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < 3:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code >= 500:
                logger.warning("MissionInbox server error %d (attempt %d/3)", resp.status_code, attempt)
                if attempt < 3:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise MissionInboxAPIError(resp.status_code, str(detail))

            if not resp.content:
                return {}
            return resp.json()

        raise MissionInboxAPIError(0, "Max retries exceeded") from last_exc

    def list_mailboxes(self) -> List[Dict]:
        """Return all mailboxes in this workspace (auto-paginates).
        Also checks for workspace-level billing/payment issues."""
        all_mailboxes: List[Dict] = []
        page = 1
        while True:
            result = self._request("GET", f"/api/mailboxes?page={page}&limit=100")
            # Check for billing indicators in the response
            self._detect_billing_status(result)
            data = result.get("data", [])
            all_mailboxes.extend(data)
            if page >= result.get("totalPages", 1):
                break
            page += 1
        logger.info("MissionInbox: loaded %d mailbox(es) (workspace key: ...%s)", len(all_mailboxes), self._api_key[-6:])
        return all_mailboxes

    def _detect_billing_status(self, response: dict) -> None:
        """Check API response for billing/payment issues at workspace level."""
        if not isinstance(response, dict):
            return
        for field in ("message", "status", "billingStatus", "billing_status",
                      "subscriptionStatus", "subscription_status", "warning", "error"):
            val = str(response.get(field, "")).lower()
            if any(kw in val for kw in self._PAYMENT_KEYWORDS):
                self.workspace_billing_status = str(response.get(field, ""))
                logger.warning(
                    "MissionInbox: workspace billing issue detected: %s",
                    self.workspace_billing_status,
                )
                return

    @staticmethod
    def is_mailbox_suspended(mailbox: Dict) -> bool:
        """Check if a mailbox's status indicates suspension or payment issues."""
        status = str(mailbox.get("status", "")).lower()
        return status in ("suspended", "payment_pending", "inactive", "disabled")

    def get_credentials(self, email: str) -> Optional[Dict]:
        """
        Fetch SMTP/IMAP credentials for a mailbox by email.
        Returns a dict compatible with reconnect.py, or None if not found/error.
        """
        try:
            mailbox = self._request("GET", f"/api/mailboxes/{email}")
        except MissionInboxAPIError as exc:
            if exc.status_code == 404:
                logger.warning("MissionInbox: mailbox not found for %s", email)
                return None
            logger.error("MissionInbox: API error fetching credentials for %s: %s", email, exc)
            return None
        except Exception as exc:
            logger.error("MissionInbox: unexpected error fetching credentials for %s: %s", email, exc)
            return None

        password = mailbox.get("password")
        if not password:
            logger.error("MissionInbox: no password returned for %s", email)
            return None

        return {
            "email":         email,
            "smtp_host":     MI_SMTP_HOST,
            "smtp_port":     MI_SMTP_PORT,
            "smtp_user":     email,
            "smtp_password": password,
            "imap_host":     MI_IMAP_HOST,
            "imap_port":     MI_IMAP_PORT,
            "imap_user":     email,
            "imap_password": password,
            "provider":      "missioninbox",
        }
