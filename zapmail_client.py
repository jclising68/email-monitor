"""
zapmail_client.py — ZapMail REST API wrapper.

Base URL : https://api.zapmail.ai/api  (configurable via ZAPMAIL_API_BASE_URL)
Auth     : x-auth-zapmail: <API_KEY> header
Workspace: x-workspace-key: <WORKSPACE_KEY> header (one per Instantly workspace)

Key endpoints used:
  GET  /v2/mailboxes/list                   — list all mailboxes in workspace
  POST /v2/exports/mailboxes                — re-export mailboxes to Instantly
  POST /v2/exports/accounts/third-party     — one-time: register Instantly credentials
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.zapmail.ai/api"
_MAX_RETRIES = 5


class ZapMailAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code


class ZapMailClient:
    def __init__(
        self,
        api_key: str,
        workspace_key: str,
        service_provider: str = "GOOGLE",
        base_url: str = _DEFAULT_BASE_URL,
    ):
        self._api_key = api_key
        self._workspace_key = workspace_key
        self._service_provider = (service_provider or "GOOGLE").upper()
        self._base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._session = requests.Session()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        h = {
            "content-type": "application/json",
            "x-auth-zapmail": self._api_key,
            "user-agent": "zapmail-mcp-server/2.0",
        }
        if self._workspace_key:
            h["x-workspace-key"] = self._workspace_key
        if self._service_provider:
            h["x-service-provider"] = self._service_provider
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
                logger.warning("ZapMail %s %s attempt %d failed: %s", method, path, attempt, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff))
                logger.warning("ZapMail rate limited (attempt %d/%d); waiting %ds", attempt, _MAX_RETRIES, retry_after)
                if attempt < _MAX_RETRIES:
                    time.sleep(retry_after)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 500:
                logger.warning("ZapMail server error %d (attempt %d/%d)", resp.status_code, attempt, _MAX_RETRIES)
                if attempt < _MAX_RETRIES:
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 16)
                continue

            if resp.status_code >= 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                raise ZapMailAPIError(resp.status_code, str(detail))

            if resp.status_code == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        raise ZapMailAPIError(0, f"Max retries exceeded for {method} {path}") from last_exc

    # ── Mailbox methods ───────────────────────────────────────────────────────

    def list_mailboxes(self) -> List[Dict]:
        """
        GET /v2/mailboxes/list — return all mailboxes in this workspace.
        Actual response: {status, message, data: {domains: [{domain, mailboxes: [...]}]}}
        Mailboxes are flattened across all domains.
        """
        result = self._request("GET", "/v2/mailboxes/list")
        if isinstance(result, list):
            return result

        data = result.get("data", result)
        if isinstance(data, dict):
            # Primary structure: data.domains[].mailboxes
            if "domains" in data:
                mailboxes: List[Dict] = []
                for domain_obj in data.get("domains", []):
                    mailboxes.extend(domain_obj.get("mailboxes", []))
                return mailboxes
            # Fallback: flat list under a common key
            for key in ("mailboxes", "items", "results"):
                if key in data and isinstance(data[key], list):
                    return data[key]
        elif isinstance(data, list):
            return data

        logger.warning("ZapMail list_mailboxes: unexpected response shape: %s", list(result.keys()))
        return []

    def find_mailbox_by_email(self, email: str) -> Optional[Dict]:
        """Search all mailboxes client-side (ZapMail has no dedicated search endpoint)."""
        email_lower = email.lower()
        for mb in self.list_mailboxes():
            if str(mb.get("email", "")).lower() == email_lower:
                return mb
        return None

    def export_mailboxes(
        self,
        mailbox_ids: Optional[List[str]] = None,
        contains: Optional[str] = None,
        apps: Optional[List[str]] = None,
        status: Optional[str] = None,
    ) -> Dict:
        """
        POST /v2/exports/mailboxes — push mailboxes to Instantly (or other platforms).

        At least one of mailbox_ids or contains must be provided.
        apps defaults to ["INSTANTLY"].
        """
        payload: Dict = {"apps": apps or ["INSTANTLY"]}
        if mailbox_ids:
            payload["ids"] = mailbox_ids
        if contains:
            payload["contains"] = contains
        if status:
            payload["status"] = status
        return self._request("POST", "/v2/exports/mailboxes", json=payload)

    # Errors that mean "don't retry — needs manual fix in ZapMail"
    _PERMANENT_ERRORS = ("invalid account credentials", "unauthorized", "forbidden")

    def reconnect_email(self, email: str) -> tuple:
        """
        Find the mailbox in ZapMail by email, then re-export it to Instantly.

        Returns (success: bool, permanent_failure: bool)
          - (True, False)  = export API succeeded
          - (False, True)  = permanent error, don't retry (e.g. invalid credentials)
          - (False, False)  = transient error, retry might help
        """
        try:
            mailbox = self.find_mailbox_by_email(email)
        except ZapMailAPIError as exc:
            logger.error("ZapMail: failed to list mailboxes for %s: %s", email, exc)
            return False, False
        except Exception as exc:
            logger.error("ZapMail: unexpected error listing mailboxes for %s: %s", email, exc)
            return False, False

        if not mailbox:
            logger.warning(
                "ZapMail: mailbox not found for %s in workspace key '%s'. "
                "Check that this email exists in the ZapMail workspace.",
                email, self._workspace_key,
            )
            return False, True  # permanent: mailbox doesn't exist

        # ZapMail may use 'id', '_id', or 'mailboxId'
        mailbox_id = (
            mailbox.get("id")
            or mailbox.get("_id")
            or mailbox.get("mailboxId")
            or mailbox.get("mailbox_id")
        )
        if not mailbox_id:
            logger.error(
                "ZapMail: mailbox for %s has no usable ID field. Keys: %s",
                email, list(mailbox.keys()),
            )
            return False, True

        try:
            self.export_mailboxes(mailbox_ids=[str(mailbox_id)])
            logger.info("ZapMail: successfully re-exported %s to Instantly.", email)
            return True, False
        except ZapMailAPIError as exc:
            is_permanent = any(pe in str(exc).lower() for pe in self._PERMANENT_ERRORS)
            if is_permanent:
                logger.error("ZapMail: PERMANENT export failure for %s: %s — will not retry.", email, exc)
            else:
                logger.error("ZapMail: export failed for %s: %s", email, exc)
            return False, is_permanent
        except Exception as exc:
            logger.error("ZapMail: unexpected export error for %s: %s", email, exc)
            return False, False

    # ── One-time setup ────────────────────────────────────────────────────────

    def register_instantly_account(self, instantly_email: str, instantly_password: str) -> bool:
        """
        POST /v2/exports/accounts/third-party
        One-time call to register your Instantly login credentials with ZapMail
        so it can push mailboxes to Instantly on your behalf.

        Run this once per ZapMail workspace. After success, export_mailboxes will work.
        Returns True on success.
        """
        try:
            self._request(
                "POST",
                "/v2/exports/accounts/third-party",
                json={"email": instantly_email, "password": instantly_password, "app": "INSTANTLY"},
            )
            logger.info(
                "ZapMail: Instantly account '%s' registered successfully for workspace '%s'.",
                instantly_email, self._workspace_key,
            )
            return True
        except ZapMailAPIError as exc:
            logger.error("ZapMail: failed to register Instantly account: %s", exc)
            return False
