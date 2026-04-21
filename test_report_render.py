"""
test_report_render.py — Verify the daily report renders provider_errors in
its own section and doesn't conflate them with real reconnect failures.
"""
from __future__ import annotations

import sys

import os
os.environ.setdefault("SLACK_WEBHOOK_URL", "http://x")
os.environ.setdefault("GOOGLE_SHEETS_ID", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON_FILE", "/dev/null")

from slack_reporter import ReportData, WorkspaceSummary, _format_daily_report


def main() -> int:
    r = ReportData()
    ws = WorkspaceSummary("Textgrid")
    ws.connected = 48
    ws.warmup = 0
    ws.disconnected = 6
    r.workspace_summaries.append(ws)

    # 3 accounts hit Gmail daily limit
    for e in ("motti.stenge@gotextgrid.com", "motti@thetextgrid.com", "stengemotti@wintextgrid.com"):
        r.provider_errors.append({
            "email": e, "workspace_name": "Textgrid",
            "category": "sending_limit_exceeded",
            "detail": "error: data command failed: 550-5.4.5 daily user sending limit exceeded",
            "response_code": 550, "auto_recoverable": True,
            "first_alert_this_run": True,
        })
    # 1 suspicious-activity block
    r.provider_errors.append({
        "email": "bad@example.com", "workspace_name": "Textgrid",
        "category": "suspicious_activity_blocked",
        "detail": "550-5.7.1 unusual sending activity detected",
        "response_code": 550, "auto_recoverable": False,
        "first_alert_this_run": True,
    })
    # 1 real auth failure that went through reconnect and failed
    r.still_disconnected.append({
        "email": "other@foo.com", "workspace_name": "Textgrid",
        "provider": "zapmail", "attempts": 5,
    })

    text = _format_daily_report(r)
    print(text)
    print("\n" + "=" * 60)

    checks = [
        ("Provider-Side Errors" in text,
         "header 'Provider-Side Errors' present"),
        ("Daily Sending Limit Exceeded" in text,
         "sending-limit category label present"),
        ("Suspicious Activity Blocked" in text,
         "suspicious category label present"),
        ("motti.stenge@gotextgrid.com" in text,
         "real rate-limited email listed"),
        ("bad@example.com" in text,
         "suspicious email listed"),
        # These accounts should NOT appear in the 'Still Disconnected' section
        (text.count("motti.stenge@gotextgrid.com") == 1,
         "rate-limited email appears only in provider-error section, not as disconnect"),
        # The real reconnect failure should still show up
        ("other@foo.com" in text,
         "real reconnect failure still listed"),
    ]
    all_ok = True
    for ok, label in checks:
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {label}")
        if not ok:
            all_ok = False
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
