"""
test_settings_and_smartlead.py — Sheet-driven config + Smartlead classification.

Covers:
  - SheetsClient.get_settings() returns only non-empty rows and re-seeds missing keys
  - Config.apply_overrides() precedence (Sheet wins; blank keeps env)
  - SmartleadClient.is_connected / is_oauth / warmup_health on sample payloads
  - _process_smartlead_workspace end-to-end with a mocked client
"""
from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

os.environ.setdefault("SLACK_WEBHOOK_URL", "env-webhook")
os.environ.setdefault("GOOGLE_SHEETS_ID", "x")

_results = []


def check(cond: bool, label: str) -> None:
    _results.append((bool(cond), label))
    print(f"  [{'OK  ' if cond else 'FAIL'}] {label}")


# ── Config.apply_overrides ───────────────────────────────────────────────────

def test_apply_overrides() -> None:
    from config import Config
    c = Config()
    env_zm = c.zapmail_api_key
    c.apply_overrides({
        "slack_webhook_url": "sheet-webhook",
        "zapmail_api_key": "   ",          # whitespace only → ignored
        "premiuminbox_api_token": "pi-tok",
    })
    check(c.slack_webhook_url == "sheet-webhook", "Sheet value overrides env webhook")
    check(c.zapmail_api_key == env_zm, "blank Sheet value keeps env/default")
    check(c.premiuminbox_api_token == "pi-tok", "new credential key applied")

    c2 = Config()
    c2.apply_overrides({})
    check(c2.slack_webhook_url == "env-webhook", "empty settings dict is a no-op")


# ── SheetsClient.get_settings ────────────────────────────────────────────────

def test_get_settings() -> None:
    import sheets_client
    from sheets_client import SheetsClient, _SETTINGS_SEED

    sc = SheetsClient.__new__(SheetsClient)  # bypass __init__ (no real auth)

    fake_ws = MagicMock()
    fake_ws.row_values.return_value = ["key", "value", "notes"]
    fake_ws.get_all_records.return_value = [
        {"key": "slack_webhook_url", "value": "https://hooks.slack.com/x", "notes": ""},
        {"key": "zapmail_api_key", "value": "", "notes": ""},
        # premiuminbox_api_token + scaledmail_api_key rows deleted by the client
    ]
    appended = []
    fake_ws.append_rows.side_effect = lambda rows: appended.extend(rows)
    sc._worksheet = types.MethodType(lambda self, tab: fake_ws, sc)

    settings = sc.get_settings()
    check(settings == {"slack_webhook_url": "https://hooks.slack.com/x"},
          "only non-empty rows returned")
    appended_keys = {r[0] for r in appended}
    check(appended_keys == {"premiuminbox_api_token", "scaledmail_api_key"},
          "missing seed rows are re-appended")
    check(len(_SETTINGS_SEED) == 4, "seed list has the 4 credential keys")


# ── SmartleadClient classification ───────────────────────────────────────────

def test_smartlead_classifiers() -> None:
    from smartlead_client import SmartleadClient as S

    check(S.is_connected({"is_smtp_success": True}) is True, "smtp success → connected")
    check(S.is_connected({"is_smtp_success": False}) is False, "smtp failure → disconnected")
    check(S.is_connected({"from_email": "a@b.com"}) is True, "missing field → assume connected")
    check(S.is_oauth({"type": "GMAIL"}) is True, "GMAIL type → oauth")
    check(S.is_oauth({"type": "SMTP"}) is False, "SMTP type → not oauth")

    wh = S.warmup_health({"warmup_details": {
        "warmup_reputation": "97%", "total_sent_count": 200, "total_spam_count": 4, "status": "ACTIVE",
    }})
    check(wh is not None and wh["health_score"] == 97.0, "reputation string parsed to number")
    check(abs(wh["spam_rate"] - 2.0) < 1e-9, "spam rate computed")
    check(S.warmup_health({}) is None, "no warmup data → None")


# ── _process_smartlead_workspace ─────────────────────────────────────────────

def test_process_smartlead_workspace() -> None:
    import config as _cfg_mod

    class FakeCfg:
        daily_limit_max = 30
        health_score_alert_threshold = 80
        spam_rate_alert_threshold = 10
        bounce_rate_alert_threshold = 5
        campaign_bounce_pause_threshold = 10
        campaign_min_sent_for_bounce_check = 50

        def configure_logging(self):
            pass
    _cfg_mod.Config = FakeCfg
    _cfg_mod.config = FakeCfg()

    import monitor
    from slack_reporter import ReportData

    fake_client = MagicMock()
    fake_client.get_email_accounts.return_value = [
        {"id": 1, "from_email": "good@acme.com", "type": "SMTP", "is_smtp_success": True,
         "message_per_day": 50, "signature": "cheers"},
        {"id": 2, "from_email": "down@acme.com", "type": "GMAIL", "is_smtp_success": False,
         "message_per_day": 20, "signature": ""},
    ]
    fake_client.get_campaigns.return_value = [{"id": 9, "name": "Q3", "status": "ACTIVE"}]
    fake_client.get_campaign_analytics.return_value = {
        "sent_count": 400, "bounce_count": 60, "reply_count": 8,
    }
    fake_client.update_daily_limit.return_value = True
    fake_client.pause_campaign.return_value = True

    from smartlead_client import SmartleadClient as _RealS
    fake_cls = MagicMock(side_effect=lambda *a, **k: fake_client)
    fake_cls.is_connected = _RealS.is_connected
    fake_cls.is_oauth = _RealS.is_oauth
    fake_cls.warmup_health = _RealS.warmup_health
    monitor.SmartleadClient = fake_cls

    report = ReportData()
    sheets = MagicMock()
    alert_state: dict = {}
    monitor._process_smartlead_workspace("Acme", "kkk", report, sheets, alert_state, FakeCfg())

    summ = [w for w in report.workspace_summaries if w.name == "Acme [Smartlead]"][0]
    check(summ.connected == 1 and summ.disconnected == 1, "1 connected / 1 disconnected")
    check(any(d["email"] == "down@acme.com" and d["provider"] == "smartlead"
              for d in report.still_disconnected), "disconnected account reported")
    check(any(a["email"] == "good@acme.com" and a["previous_limit"] == 50
              for a in report.daily_limit_adjustments), "over-limit account capped")
    check({"email": "down@acme.com", "workspace_name": "Acme"} in report.signature_issues,
          "empty signature flagged")
    check(any("Q3 (Smartlead)" in b["campaign_name"] for b in report.bounce_alerts),
          "15% bounce campaign alerted")
    fake_client.pause_campaign.assert_called_once_with(9)
    check(True, "campaign auto-paused at >=10% bounce")


def main() -> int:
    for fn in (test_apply_overrides, test_get_settings, test_smartlead_classifiers,
               test_process_smartlead_workspace):
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            _results.append((False, f"{fn.__name__} raised {exc!r}"))
            print(f"  [FAIL] {fn.__name__} raised {exc!r}")
    passed = sum(1 for ok, _ in _results if ok)
    print(f"\n{passed}/{len(_results)} passed.")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    sys.exit(main())
