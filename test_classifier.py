"""
test_classifier.py — Sanity test for InstantlyClient.classify_account_error().

Run: python test_classifier.py

Fixtures include:
  - Real error accounts sampled live from Textgrid workspace (sending_limit_exceeded)
  - Real healthy accounts (status=1)
  - Synthetic auth-failure, suspicious-activity, and edge cases so the
    classifier is exercised against every branch — not just the one we
    happen to have live today.
"""
from __future__ import annotations

import sys
from instantly_client import InstantlyClient


def _case(label: str, account: dict, expected_category: str,
          expected_should_reconnect: bool) -> bool:
    result = InstantlyClient.classify_account_error(account)
    ok = (
        result["category"] == expected_category
        and result["should_reconnect"] == expected_should_reconnect
    )
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}")
    print(f"         -> category={result['category']}  "
          f"reconnect={result['should_reconnect']}  "
          f"auto_recoverable={result['auto_recoverable']}  "
          f"code={result['response_code']}")
    if not ok:
        print(f"         !!!  expected category={expected_category}, "
              f"reconnect={expected_should_reconnect}")
        print(f"         !!!  detail={result['detail'][:100]}")
    return ok


FIXTURES = [
    # ─── Real live sample: motti.stenge@gotextgrid.com (Textgrid, 2026-04-21)
    ("real Textgrid sending-limit hit (Google)", {
        "email": "motti.stenge@gotextgrid.com",
        "status": -3,
        "provider_code": 2,
        "status_message": {
            "code": "EENVELOPE",
            "command": "DATA",
            "response": "550-5.4.5 Daily user sending limit exceeded. For more information on Gmail\n550-5.4.5 sending limits go to\n550 5.4.5  https://support.google.com/a/answer/166852 af79cd13be357-8eb9becc72dsm190518885a.34 - gsmtp",
            "e_message": "error: data command failed: 550-5.4.5 daily user sending limit exceeded. for more information on gmail\n550-5.4.5 sending limits go to\n550 5.4.5  https://support.google.com/a/answer/166852 af79cd13be357-8eb9becc72dsm190518885a.34 - gsmtp",
            "responseCode": 550,
        },
        "autofix_failed": True,
    }, "sending_limit_exceeded", False),

    ("real Textgrid sending-limit hit #2", {
        "email": "motti@thetextgrid.com",
        "status": -3,
        "status_message": {
            "code": "EENVELOPE",
            "command": "DATA",
            "response": "550-5.4.5 Daily user sending limit exceeded...",
            "e_message": "error: data command failed: 550-5.4.5 daily user sending limit exceeded...",
            "responseCode": 550,
        },
        "autofix_failed": True,
    }, "sending_limit_exceeded", False),

    # ─── Healthy accounts
    ("real healthy account status=1", {
        "email": "motti.stenge@protextgrid.com", "status": 1, "provider_code": 1,
    }, "connected", False),

    ("status='active' (string form)", {
        "email": "foo@bar.com", "status": "active",
    }, "connected", False),

    # ─── Paused
    ("paused account", {
        "email": "foo@bar.com", "status": 2,
    }, "paused", False),

    # ─── Synthetic: authentication failure (e.g. bad password)
    ("auth failure SMTP 535", {
        "email": "foo@bar.com", "status": -1,
        "status_message": {
            "code": "EAUTH",
            "response": "535-5.7.8 Username and Password not accepted. Learn more at ...",
            "e_message": "error: Invalid login: 535-5.7.8 Username and Password not accepted...",
            "responseCode": 535,
        },
    }, "authentication_failure", True),

    ("Microsoft app-password required", {
        "email": "foo@bar.com", "status": -1,
        "status_message": {
            "response": "Application-specific password required",
            "e_message": "Application-specific password required",
        },
    }, "authentication_failure", True),

    # ─── Synthetic: suspicious activity
    ("Gmail suspicious activity 5.7.1", {
        "email": "foo@bar.com", "status": -3,
        "status_message": {
            "response": "550-5.7.1 Our system has detected unusual sending activity...",
            "e_message": "550-5.7.1 Our system has detected unusual sending activity...",
            "responseCode": 550,
        },
    }, "suspicious_activity_blocked", False),

    # ─── Synthetic: mailbox full
    ("mailbox full 5.2.2", {
        "email": "foo@bar.com", "status": -3,
        "status_message": {
            "response": "552-5.2.2 Mailbox full",
            "e_message": "552-5.2.2 Mailbox full",
            "responseCode": 552,
        },
    }, "mailbox_full", False),

    # ─── Edge cases: non-dict status_message should NOT crash
    ("status_message is a string (defensive)", {
        "email": "foo@bar.com", "status": -3,
        "status_message": "550 5.4.5 Daily user sending limit exceeded",
    }, "sending_limit_exceeded", False),

    ("status_message is None", {
        "email": "foo@bar.com", "status": -3,
        "status_message": None,
    }, "disconnected_unknown", True),

    ("status_message missing entirely", {
        "email": "foo@bar.com", "status": -3,
    }, "disconnected_unknown", True),

    ("unknown error text", {
        "email": "foo@bar.com", "status": -3,
        "status_message": {
            "response": "some weird provider-specific text we haven't seen",
            "e_message": "some weird provider-specific text",
            "responseCode": 421,
        },
    }, "disconnected_unknown", True),

    # ─── Outlook / O365 variant of sending limit
    ("Outlook throttled", {
        "email": "foo@bar.com", "status": -3,
        "status_message": {
            "response": "432 4.3.2 STOREDRV.ClientSubmit; exceeded the message limit per account",
            "e_message": "exceeded the message limit per account",
            "responseCode": 432,
        },
    }, "sending_limit_exceeded", False),
]


def main() -> int:
    print(f"Running {len(FIXTURES)} classifier test(s)...\n")
    passed = 0
    for label, account, cat, reconnect in FIXTURES:
        if _case(label, account, cat, reconnect):
            passed += 1
    print(f"\n{passed}/{len(FIXTURES)} passed.")
    return 0 if passed == len(FIXTURES) else 1


if __name__ == "__main__":
    sys.exit(main())
