"""
state.py — Alert deduplication logic.

Rules:
  - A ZapMail disconnection fires ONE alert when first detected.
  - Subsequent checks where the account is still disconnected are silent
    (unless ZAPMAIL_REALERT_HOURS > 0 and that many hours have elapsed).
  - When the account reconnects, its alert_state row is cleared → the
    next disconnection will alert again.
  - Mission Inbox reconnect failures are tracked via reconnect_attempts;
    no separate dedup needed (reconnect is attempted every hour regardless).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, Optional


def utcnow_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M UTC", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def is_new_disconnection(email: str, alert_state: Dict[str, Dict]) -> bool:
    """
    Return True if this is the first time we've seen this email disconnected
    (i.e. no existing row in alert_state).
    """
    return email.lower() not in alert_state


def should_realert(email: str, alert_state: Dict[str, Dict], realert_hours: int) -> bool:
    """
    Return True if enough time has elapsed since the last alert to re-alert.
    If realert_hours == 0, never re-alert.
    """
    if realert_hours <= 0:
        return False
    row = alert_state.get(email.lower())
    if not row:
        return False
    last_alerted = _parse_dt(str(row.get("last_alerted", "")))
    if last_alerted is None:
        return True
    elapsed = datetime.now(timezone.utc) - last_alerted
    return elapsed >= timedelta(hours=realert_hours)


def build_new_alert_state_row(email: str, workspace_name: str) -> Dict:
    """Return a fresh alert_state row dict for a newly detected disconnection."""
    now = utcnow_str()
    return {
        "email": email.lower(),
        "workspace_name": workspace_name,
        "first_detected": now,
        "last_alerted": now,
        "reconnect_attempts": 0,
        "status": "disconnected",
    }


def build_reconnect_failed_row(email: str, workspace_name: str,
                                existing_row: Optional[Dict],
                                attempts: int) -> Dict:
    """Return an updated alert_state row after a failed reconnect attempt."""
    now = utcnow_str()
    first_detected = (existing_row or {}).get("first_detected") or now
    return {
        "email": email.lower(),
        "workspace_name": workspace_name,
        "first_detected": first_detected,
        "last_alerted": (existing_row or {}).get("last_alerted") or now,
        "reconnect_attempts": attempts,
        "status": "reconnect_failed",
    }
