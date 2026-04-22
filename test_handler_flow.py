"""
test_handler_flow.py — Integration test for _handle_non_reconnectable_error().

Mocks SheetsClient + SlackReporter so nothing actually posts. Exercises:
  - First detection → Slack alert + alert_state write
  - Second detection within realert window → silent
  - Category change → alert again
  - Recovery (not simulated here — handled by existing clear-on-connected code)
  - Batch accumulation in ReportData.provider_errors
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Stub the Config module so monitor.py can import without .env
import os
os.environ.setdefault("SLACK_WEBHOOK_URL", "http://x")
os.environ.setdefault("GOOGLE_SHEETS_ID", "x")
os.environ.setdefault("GOOGLE_CREDENTIALS_JSON_FILE", "/dev/null")

# Build a minimal Config shim that bypasses env validation
class FakeConfig:
    slack_webhook_url = "http://x"
    google_sheets_id = "x"
    google_credentials = {}
    log_level = "WARNING"
    instantly_base_url = "https://api.instantly.ai/api/v2"
    max_reconnect_attempts = 5
    zapmail_realert_hours = 0
    zapmail_api_key = ""
    zapmail_api_base_url = "https://api.zapmail.ai/api"
    health_score_alert_threshold = 80
    health_score_critical_threshold = 60
    health_score_grace_days = 7
    spam_rate_alert_threshold = 10
    bounce_rate_alert_threshold = 5
    campaign_bounce_pause_threshold = 10
    campaign_min_sent_for_bounce_check = 50
    daily_limit_max = 30
    provider_error_realert_hours = 24

    def configure_logging(self):
        import logging
        logging.basicConfig(level=logging.WARNING)


# Monkey-patch config before importing monitor
import config as _cfg_mod
_cfg_mod.Config = FakeConfig  # type: ignore
_cfg_mod.config = FakeConfig()  # type: ignore

from monitor import _handle_non_reconnectable_error
from instantly_client import InstantlyClient
from slack_reporter import ReportData


REAL_SENDING_LIMIT_ACCOUNT = {
    "email": "motti.stenge@gotextgrid.com",
    "status": -3,
    "provider_code": 2,
    "status_message": {
        "code": "EENVELOPE",
        "command": "DATA",
        "response": "550-5.4.5 Daily user sending limit exceeded. For more information on Gmail\n550-5.4.5 sending limits go to\n550 5.4.5  https://support.google.com/a/answer/166852 af79cd13be357-8eb9becc72dsm190518885a.34 - gsmtp",
        "e_message": "error: data command failed: 550-5.4.5 daily user sending limit exceeded. for more information on gmail",
        "responseCode": 550,
    },
    "autofix_failed": True,
}


def _run_case(label: str, fn) -> bool:
    try:
        fn()
        print(f"  [OK  ] {label}")
        return True
    except AssertionError as e:
        print(f"  [FAIL] {label}")
        print(f"         !!!  {e}")
        return False
    except Exception as e:
        print(f"  [ERR ] {label} — {e}")
        return False


def case_first_detection():
    sheets = MagicMock()
    slack = MagicMock()
    alert_state = {}
    report = ReportData()
    cfg = FakeConfig()

    err_info = InstantlyClient.classify_account_error(REAL_SENDING_LIMIT_ACCOUNT)
    assert err_info["category"] == "sending_limit_exceeded"

    _handle_non_reconnectable_error(
        "motti.stenge@gotextgrid.com", "Textgrid",
        err_info, sheets, slack, alert_state, report, cfg,
    )

    # Handler must NOT call Slack directly — batched send happens post-loop in run().
    assert slack.send_provider_error_alert.call_count == 0, \
        "handler should stage for batching, not call Slack inline"
    assert slack.send_provider_error_batch.call_count == 0, \
        "handler should not call the batch method either"
    # alert_state written
    assert sheets.upsert_alert_state.call_count == 1
    # In-memory alert_state updated
    assert "motti.stenge@gotextgrid.com" in alert_state
    assert alert_state["motti.stenge@gotextgrid.com"]["status"] == "sending_limit_exceeded"
    # Report collected the entry
    assert len(report.provider_errors) == 1
    assert report.provider_errors[0]["category"] == "sending_limit_exceeded"
    assert report.provider_errors[0]["first_alert_this_run"] is True
    # Reconnect batch should still be empty
    assert report.reconnect_attempted == []
    assert report.still_disconnected == []


def case_second_detection_within_realert_silent():
    sheets = MagicMock()
    slack = MagicMock()
    # Seed the in-memory state as if we already alerted
    from state import utcnow_str
    alert_state = {
        "motti.stenge@gotextgrid.com": {
            "email": "motti.stenge@gotextgrid.com",
            "workspace_name": "Textgrid",
            "first_detected": utcnow_str(),
            "last_alerted": utcnow_str(),
            "reconnect_attempts": 0,
            "status": "sending_limit_exceeded",
        }
    }
    report = ReportData()
    cfg = FakeConfig()
    cfg.provider_error_realert_hours = 24  # default

    err_info = InstantlyClient.classify_account_error(REAL_SENDING_LIMIT_ACCOUNT)
    _handle_non_reconnectable_error(
        "motti.stenge@gotextgrid.com", "Textgrid",
        err_info, sheets, slack, alert_state, report, cfg,
    )

    # Silent — no new Slack alert
    assert slack.send_provider_error_alert.call_count == 0, \
        "should be silent within realert window"
    # But still tracked in report
    assert len(report.provider_errors) == 1
    assert report.provider_errors[0]["first_alert_this_run"] is False


def case_category_change_triggers_alert():
    sheets = MagicMock()
    slack = MagicMock()
    from state import utcnow_str
    # Account was previously in sending_limit_exceeded, now it's suspicious
    alert_state = {
        "foo@bar.com": {
            "email": "foo@bar.com",
            "workspace_name": "Testspace",
            "first_detected": utcnow_str(),
            "last_alerted": utcnow_str(),
            "reconnect_attempts": 0,
            "status": "sending_limit_exceeded",
        }
    }
    report = ReportData()
    cfg = FakeConfig()

    suspicious_account = {
        "email": "foo@bar.com", "status": -3,
        "status_message": {
            "response": "550-5.7.1 Our system has detected unusual sending activity",
            "e_message": "550-5.7.1 unusual sending",
            "responseCode": 550,
        },
    }
    err_info = InstantlyClient.classify_account_error(suspicious_account)
    assert err_info["category"] == "suspicious_activity_blocked"

    _handle_non_reconnectable_error(
        "foo@bar.com", "Testspace", err_info, sheets, slack,
        alert_state, report, cfg,
    )

    # Category changed → staged for batched alert (first_alert_this_run=True)
    assert slack.send_provider_error_alert.call_count == 0, \
        "handler should not call Slack inline — batching happens post-loop"
    assert len(report.provider_errors) == 1
    assert report.provider_errors[0]["first_alert_this_run"] is True, \
        "category change must flag the entry as a fresh alert so the batch picks it up"
    # State now reflects the new category
    assert alert_state["foo@bar.com"]["status"] == "suspicious_activity_blocked"


def case_classifier_skips_auth_failure():
    """Auth failure must NOT go through the non-reconnectable handler —
    its should_reconnect is True so the provider reconnect flow stays."""
    auth_fail = {
        "email": "bad@creds.com", "status": -1,
        "status_message": {
            "response": "535-5.7.8 Username and Password not accepted",
            "e_message": "Invalid login",
            "responseCode": 535,
        },
    }
    err_info = InstantlyClient.classify_account_error(auth_fail)
    assert err_info["category"] == "authentication_failure"
    assert err_info["should_reconnect"] is True, \
        "auth failure must route to reconnect, NOT non-reconnectable handler"


def case_unknown_error_still_reconnects():
    """A disconnected account with no recognized signature should go through
    the normal reconnect flow (preserves existing behaviour for unknowns)."""
    unknown = {
        "email": "weird@bar.com", "status": -3,
        "status_message": {
            "response": "some unheard-of provider text",
            "responseCode": 421,
        },
    }
    err_info = InstantlyClient.classify_account_error(unknown)
    assert err_info["category"] == "disconnected_unknown"
    assert err_info["should_reconnect"] is True


def main() -> int:
    print("Running handler-flow integration tests...\n")
    tests = [
        ("first detection alerts + writes state + collects report entry",
         case_first_detection),
        ("second detection within realert window stays silent",
         case_second_detection_within_realert_silent),
        ("category change from one error to another re-alerts",
         case_category_change_triggers_alert),
        ("auth_failure bypasses non-reconnectable handler",
         case_classifier_skips_auth_failure),
        ("unknown error still takes the reconnect path",
         case_unknown_error_still_reconnects),
    ]
    passed = sum(_run_case(label, fn) for label, fn in tests)
    print(f"\n{passed}/{len(tests)} passed.")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
