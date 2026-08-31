"""
scaledmail_client.py — ScaledMail API wrapper.

Base URL : https://server.scaledmail.com/api/v1   (override via SCALEDMAIL_API_BASE_URL)
Auth     : Authorization: Bearer <API_KEY>        (one global key, per ScaledMail account)
Scope    : organization_id on every request       (one per client, from the Sheet)
Limits   : 5 requests/second — a small throttle is applied between calls.
Docs     : https://api.scaledmail.com/scaledmail-api-documentaion-1034165m0

What the PUBLIC docs cover (confirmed):
  - GET /organizations
  - Get Domains, "Get Mailboxes by Domain ID", "Get Reporting Stats"
    (these are named in the docs nav; exact paths are NOT published — the page
     says "reach out to support@scaledmail.com ... for feature requests")
  - POST /buy-pre-warm-inboxes  — buys NEW inboxes and, via a `sequencer` object
    ({provider, username, password, link, workspace, tag}), auto-pushes them to
    Instantly / Smartlead / etc. using YOUR sequencer login.

What the public docs do NOT cover: re-pushing / reconnecting an *existing*
mailbox to a sequencer. reconnect_email() stays a stub until ScaledMail confirms
that endpoint (see the comment on it).

This client uses only GET reads and tolerates 404s (paths are best-effort),
so a wrong guess degrades to "alert only", never a destructive call.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://server.scaledmail.com/api/v1"
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

    def _get_first_ok(self, paths: List[str], *, params: Optional[Dict] = None):
        """Try each candidate path; return the first non-404 JSON body, or None."""
        for p in paths:
            try:
                return self._request("GET", p, params=params)
            except ScaledMailAPIError as exc:
                if exc.status_code in (404, 405):
                    continue
                raise
        logger.warning("ScaledMail: none of %s responded — is the path published yet?", paths)
        return None

    @staticmethod
    def _unwrap_list(result) -> List[Dict]:
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            data = result.get("data", result)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("mailboxes", "inboxes", "domains", "accounts", "items", "results"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    # ── Reads (documented capabilities) ──────────────────────────────────────

    def list_domains(self) -> List[Dict]:
        """Get Domains for this organization. Each domain dict carries an 'id'."""
        result = self._get_first_ok(["/domains", "/get-domains"])
        self._detect_billing_status(result if isinstance(result, dict) else {})
        return self._unwrap_list(result)

    def list_mailboxes(self) -> List[Dict]:
        """
        All mailboxes in this organization. The docs expose "Get Mailboxes by
        Domain ID", so we walk domains -> mailboxes. A flat listing is tried
        first in case the account also exposes one.
        """
        flat = self._get_first_ok(["/mailboxes", "/get-mailboxes", "/inboxes"])
        if flat is not None:
            self._detect_billing_status(flat if isinstance(flat, dict) else {})
            mbs = self._unwrap_list(flat)
            if mbs:
                return mbs

        mailboxes: List[Dict] = []
        for dom in self.list_domains():
            dom_id = dom.get("id") or dom.get("domain_id") or dom.get("_id")
            if not dom_id:
                continue
            res = self._get_first_ok(
                ["/mailboxes", f"/domains/{dom_id}/mailboxes", "/get-mailboxes-by-domain"],
                params={"domain_id": dom_id},
            )
            mailboxes.extend(self._unwrap_list(res))
        return mailboxes

    def check_billing(self) -> Optional[str]:
        """
        Best-effort payment-hold detection from the reporting/organization data.
        Returns a descriptive string, or None if healthy / unknown.
        """
        result = self._get_first_ok(["/stats", "/reporting-stats", "/organizations"])
        if isinstance(result, (dict, list)):
            for obj in (result if isinstance(result, list) else [result]):
                if isinstance(obj, dict):
                    self._detect_billing_status(obj)
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

    # ── Reconnect ───────────────────────────────────────────────────────────

    def reconnect_email(self, email: str, cached_mailbox: Optional[Dict] = None) -> tuple:
        """
        NOT IMPLEMENTED — the public ScaledMail API has no documented endpoint to
        re-push an *existing* mailbox to a sequencer.

        The pieces that exist: POST /buy-pre-warm-inboxes accepts a `sequencer`
        object — {provider, username, password, link, workspace, tag} — and uses
        your sequencer login to push newly-bought inboxes. A "connect existing
        mailboxes to sequencer" endpoint almost certainly exists internally
        (their support does it during onboarding) but is not published.

        To enable auto-reconnect: ask support@scaledmail.com for
        "the API endpoint to (re-)push existing mailboxes for an organization to
        our sequencer", then implement it here — likely a POST that takes the
        same `sequencer` object plus a mailbox id / email list.

        Returns (success=False, permanent_failure=False) so the caller falls
        back to alert-only handling.
        """
        logger.info(
            "ScaledMail: no public reconnect endpoint — %s stays alert-only. "
            "See reconnect_email() docstring.", email,
        )
        return False, False
