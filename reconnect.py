"""
reconnect.py — Auto-reconnect logic for Mission Inbox accounts.

Strategy:
  1. Try DELETE /accounts/{email}  (idempotent — 404 is OK)
  2. Try POST  /accounts           with stored SMTP/IMAP credentials from Sheets
  3. Return (success: bool, partial_failure: bool)
     partial_failure=True means DELETE succeeded but POST failed —
     the account is now gone from Instantly and needs a POST-only retry next hour.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from instantly_client import InstantlyClient, InstantlyAPIError

logger = logging.getLogger(__name__)


def _build_add_payload(creds: Dict) -> Dict:
    """
    Map account row from the 'accounts' sheet to the POST /accounts payload.
    All SMTP/IMAP fields are required; missing fields will cause a 400 error from Instantly.
    """
    return {
        "email": creds["email"],
        "smtp_host": creds.get("smtp_host", ""),
        "smtp_port": int(creds.get("smtp_port") or 587),
        "smtp_username": creds.get("smtp_user", ""),
        "smtp_password": creds.get("smtp_password", ""),
        "imap_host": creds.get("imap_host", ""),
        "imap_port": int(creds.get("imap_port") or 993),
        "imap_username": creds.get("imap_user", ""),
        "imap_password": creds.get("imap_password", ""),
    }


def attempt_reconnect(
    client: InstantlyClient,
    email: str,
    credentials: Optional[Dict],
) -> Tuple[bool, bool]:
    """
    Attempt to reconnect a Mission Inbox account.

    Returns:
        (success, partial_failure)
        success=True           → account re-added successfully
        partial_failure=True   → DELETE ok, POST failed (account is gone; retry POST next hour)
        Both False             → DELETE failed for a non-404 reason (unexpected error)
    """
    if not credentials:
        logger.error(
            "No credentials found in Sheets for %s — cannot reconnect. "
            "Add SMTP/IMAP credentials to the 'accounts' sheet.",
            email,
        )
        return False, False

    # ── Step 1: Delete (idempotent) ───────────────────────────────────────────
    try:
        client.delete_account(email)
        # Returns False on 404 (account already gone) — that's fine, continue to POST
    except InstantlyAPIError as exc:
        logger.error("DELETE failed for %s: %s — aborting reconnect.", email, exc)
        return False, False
    except Exception as exc:
        logger.error("Unexpected error deleting %s: %s — aborting reconnect.", email, exc)
        return False, False

    # ── Step 2: Add with credentials ─────────────────────────────────────────
    payload = _build_add_payload(credentials)
    missing_fields = [k for k, v in payload.items() if k != "email" and not v]
    if missing_fields:
        logger.warning(
            "Credentials for %s are incomplete (missing: %s). "
            "Reconnect will likely fail.",
            email, ", ".join(missing_fields),
        )

    try:
        client.add_account(payload)
        logger.info("Successfully reconnected %s.", email)
        return True, False
    except InstantlyAPIError as exc:
        logger.error(
            "POST /accounts failed for %s after successful DELETE: %s. "
            "Account is now absent from Instantly — will retry POST next run.",
            email, exc,
        )
        return False, True  # partial failure
    except Exception as exc:
        logger.error(
            "Unexpected error adding %s after DELETE: %s. "
            "Account may be absent from Instantly.",
            email, exc,
        )
        return False, True  # treat as partial failure


def attempt_post_only(
    client: InstantlyClient,
    email: str,
    credentials: Optional[Dict],
) -> bool:
    """
    POST-only reconnect for the partial-failure case where the account was already
    deleted last run but the POST failed.  Returns True on success.
    """
    if not credentials:
        logger.error("No credentials for %s — cannot do POST-only reconnect.", email)
        return False

    payload = _build_add_payload(credentials)
    try:
        client.add_account(payload)
        logger.info("POST-only reconnect successful for %s.", email)
        return True
    except InstantlyAPIError as exc:
        logger.error("POST-only reconnect failed for %s: %s", email, exc)
        return False
    except Exception as exc:
        logger.error("Unexpected error in POST-only reconnect for %s: %s", email, exc)
        return False
