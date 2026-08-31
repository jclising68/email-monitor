"""
scaledmail_client.py — ScaledMail API wrapper.

Auth     : Authorization: Bearer <API_KEY>   (one global key)
Scope    : organization_id query param on every request (one per client, from the Sheet)
Base URL : https://api.scaledmail.com/api/v1  (override via SCALEDMAIL_API_BASE_URL)
Limits   : 5 requests/second — a small throttle is applied between calls.

Status: PHASE 1.
  - list_mailboxes() + billing detection are wired and used for disconnection
    *alerting* today.
  - reconnect_email() is a stub: the re-push action and exact endpoint paths
    need confirming against the full docs (requires an API key). Until then,
    disconnections are alert-only.

TODO(after API key):
  - confirm base URL + the mailboxes list path and response shape
  - confirm a stats / billing / orders path for payment-hold detection
  - implement reconnect_email()
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.scaledmail.com/api/v1"
_MAX_RETRIES = 4
_MIN_INTERVAL = 0.25  # seconds between requests (<= 5 req/s limit)

_PAYMENT_KEYWORDS = ("payment pending", "payment overdue", "past due", "suspended",
                     "insufficient funds", "subscription expired", "billing", "overdue",
                     "cancelled", "canceled", "unpaid")


class ScaledMailAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class ScaledMailClient:
    workspace_billing_status: Optional[str] = None

    def __init__(self, api_key: str, organization_id: str = "", base_url: str = _DEFAULT_BASE_URL):
        self._api_key = api_key
        self._organization_id = organization_id
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self._last_call = 0.0
        self.workspace_billing_status = None

    def _headers(self) -> Dict[str, str]:
        return {
            "content-type": "application/json",
            "authorization": f"Bearer {self._api_key}",
        }

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, *, params: Optional[Dict] = None, **kwargs) -> dict:
        url = f"{self._base_url}{path}"
        params = dict(params or {})
        if self._organization_id:
            params.setdefault("organization_id", self._organization_id)
        backoff = 1
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            self._throttle()
            try:
                resp = self._session.request(
                    method, url, headers=self._headers(), params=params, timeout=30, **kwargs
                )
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("ScaledMail %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning("ScaledMail HTTP %d (attempt %d/%d)", resp.status_code, attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise ScaledMailAPIError(resp.status_code, str(detail))

            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise ScaledMailAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    # ── Reads ────────────────────────────────────────────────────────────────

    def list_mailboxes(self) -> List[Dict]:
        """
        Return all mailboxes in this organization. Each dict is expected to carry
        at least 'email' and a status field.

        TODO(after API key): confirm the path ('/mailboxes' assumed) and shape.
        """
        result = self._request("GET", "/mailboxes")
        self._detect_billing_status(result)

        if isinstance(result, list):
            return result
        data = result.get("data", result)
        if isinstance(data, dict):
            for key in ("mailboxes", "inboxes", "accounts", "items", "results"):
                if isinstance(data.get(key), list):
                    return data[key]
        elif isinstance(data, list):
            return data
        logger.warning("ScaledMail list_mailboxes: unexpected response shape.")
        return []

    def check_billing(self) -> Optional[str]:
        """
        Best-effort payment-hold detection from the org / stats endpoint.
        Returns a descriptive string, or None if healthy / unknown.

        TODO(after API key): point this at the real billing/orders/stats path.
        """
        try:
            result = self._request("GET", "/stats")
        except Exception as exc:
            logger.warning("ScaledMail: billing check failed: %s (non-fatal)", exc)
            return None
        self._detect_billing_status(result)
        return self.workspace_billing_status

    def _detect_billing_status(self, response: dict) -> None:
        if not isinstance(response, dict):
            return
        candidates = [response]
        data = response.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        for obj in candidates:
            for field in ("message", "status", "billingStatus", "billing_status",
                          "subscriptionStatus", "subscription_status", "warning", "error"):
                val = str(obj.get(field, "")).lower()
                if any(kw in val for kw in _PAYMENT_KEYWORDS):
                    self.workspace_billing_status = str(obj.get(field, ""))
                    logger.warning("ScaledMail: workspace billing issue detected: %s",
                                   self.workspace_billing_status)
                    return

    @staticmethod
    def is_mailbox_suspended(mailbox: Dict) -> bool:
        status = str(mailbox.get("status", "")).lower()
        return status in ("suspended", "payment_pending", "past_due", "inactive", "disabled", "cancelled")

    # ── Reconnect (stub) ─────────────────────────────────────────────────────

    def reconnect_email(self, email: str, cached_mailbox: Optional[Dict] = None) -> tuple:
        """
        NOT IMPLEMENTED — needs the full API docs (requires an API key).
        Returns (success=False, permanent_failure=False) so the caller falls
        back to alert-only handling.
        """
        logger.info(
            "ScaledMail: auto-reconnect not implemented yet for %s — alert-only. "
            "Provide an API key to enable this.", email,
        )
        return False, False
