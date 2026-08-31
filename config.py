"""
config.py — Load and validate environment variables.
"""
import os
import base64
import json
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def get_env(key: str, required: bool = True, default: str = None) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


def load_google_credentials() -> dict:
    """
    Returns the parsed Google *service account* key JSON, if one is configured.

    A service account is the client-friendly path: create it once, share the
    Google Sheet with its email address, and no browser consent / token refresh
    is ever needed. Supports three forms (checked in order):
      - GOOGLE_CREDENTIALS_JSON     : the raw JSON, pasted straight into a secret
      - GOOGLE_CREDENTIALS_JSON_B64 : base64-encoded JSON
      - GOOGLE_CREDENTIALS_JSON_FILE: path to a local JSON file

    Returns {} when none is set — sheets_client then falls back to the older
    OAuth user-auth flow (credentials.json + token.json).
    """
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw and raw.strip():
        try:
            return json.loads(raw)
        except Exception as e:
            raise EnvironmentError(f"GOOGLE_CREDENTIALS_JSON is not valid JSON: {e}")

    b64 = os.environ.get("GOOGLE_CREDENTIALS_JSON_B64")
    if b64:
        try:
            return json.loads(base64.b64decode(b64).decode("utf-8"))
        except Exception as e:
            raise EnvironmentError(f"Failed to decode GOOGLE_CREDENTIALS_JSON_B64: {e}")

    file_path = os.environ.get("GOOGLE_CREDENTIALS_JSON_FILE")
    if file_path:
        try:
            with open(file_path, "r") as f:
                return json.load(f)
        except Exception as e:
            raise EnvironmentError(f"Failed to read GOOGLE_CREDENTIALS_JSON_FILE '{file_path}': {e}")

    # No service account JSON set — OAuth2 flow via credentials.json will be used instead
    return {}


class Config:
    # Credential-type settings that may be supplied from the Sheet's "Settings" tab.
    # apply_overrides() maps these keys onto the matching attribute below.
    # (Tuning thresholds are intentionally NOT here — they stay as code defaults.)
    _SHEET_OVERRIDABLE = (
        "slack_webhook_url",
        "zapmail_api_key",
        "premiuminbox_api_token",
        "scaledmail_api_key",
    )

    def __init__(self):
        # SLACK_WEBHOOK_URL is optional at the env layer — it can instead be
        # supplied from the Sheet's "Settings" tab (see apply_overrides). run()
        # exits cleanly if neither source provides it.
        self.slack_webhook_url: str = get_env("SLACK_WEBHOOK_URL", required=False, default="")
        self.google_sheets_id: str = get_env("GOOGLE_SHEETS_ID")
        self.google_credentials: dict = load_google_credentials()
        self.log_level: str = get_env("LOG_LEVEL", required=False, default="INFO").upper()

        # Optional: override Instantly base URL (for testing/mocking)
        self.instantly_base_url: str = get_env(
            "INSTANTLY_BASE_URL", required=False, default="https://api.instantly.ai/api/v2"
        )

        # Reconnect retry cap (applies to both Mission Inbox and ZapMail)
        self.max_reconnect_attempts: int = int(
            get_env("MAX_RECONNECT_ATTEMPTS", required=False, default="5")
        )

        # How many hours to wait between re-alerting on a ZapMail disconnect (0 = never re-alert)
        self.zapmail_realert_hours: int = int(
            get_env("ZAPMAIL_REALERT_HOURS", required=False, default="0")
        )

        # ZapMail API — one global key; workspace IDs come from the 'workspaces' sheet column.
        self.zapmail_api_key: str = get_env("ZAPMAIL_API_KEY", required=False, default="")
        self.zapmail_api_base_url: str = get_env(
            "ZAPMAIL_API_BASE_URL", required=False, default="https://api.zapmail.ai/api"
        )

        # Premium Inboxes API — one global token; per-workspace IDs come from the
        # 'premiuminbox_workspace_id' sheet column. Normally supplied via the Sheet
        # 'Settings' tab; env var is a fallback.
        self.premiuminbox_api_token: str = get_env(
            "PREMIUMINBOX_API_TOKEN", required=False, default=""
        )
        self.premiuminbox_api_base_url: str = get_env(
            "PREMIUMINBOX_API_BASE_URL", required=False, default="https://api.premiuminboxes.com/api"
        )

        # ScaledMail API — one global key; per-workspace org IDs come from the
        # 'scaledmail_organization_id' sheet column. Normally supplied via the Sheet
        # 'Settings' tab; env var is a fallback.
        self.scaledmail_api_key: str = get_env("SCALEDMAIL_API_KEY", required=False, default="")
        self.scaledmail_api_base_url: str = get_env(
            "SCALEDMAIL_API_BASE_URL", required=False, default="https://server.scaledmail.com/api/v1"
        )

        # Health score thresholds (from warmup analytics)
        self.health_score_alert_threshold: int = int(
            get_env("HEALTH_SCORE_ALERT_THRESHOLD", required=False, default="80")
        )
        self.health_score_critical_threshold: int = int(
            get_env("HEALTH_SCORE_CRITICAL_THRESHOLD", required=False, default="60")
        )
        # Grace period (days) after account creation before we treat a low
        # health score as "critical" and auto-pause. New accounts start at
        # stat_warmup_score=0 and legitimately ramp up over ~5-7 days — we
        # must NOT pause them during this window or warmup breaks.
        self.health_score_grace_days: int = int(
            get_env("HEALTH_SCORE_GRACE_DAYS", required=False, default="7")
        )

        # Spam rate threshold (percentage, 0-100). Alert if 7-day spam rate exceeds this.
        self.spam_rate_alert_threshold: int = int(
            get_env("SPAM_RATE_ALERT_THRESHOLD", required=False, default="10")
        )

        # Campaign bounce rate threshold (percentage). Alert if bounced/sent exceeds this.
        self.bounce_rate_alert_threshold: int = int(
            get_env("BOUNCE_RATE_ALERT_THRESHOLD", required=False, default="5")
        )
        # Bounce % at which a campaign is auto-paused (must be >= alert threshold).
        self.campaign_bounce_pause_threshold: int = int(
            get_env("CAMPAIGN_BOUNCE_PAUSE_THRESHOLD", required=False, default="10")
        )
        # Minimum emails_sent before a campaign's bounce rate is judged —
        # smaller samples give meaningless percentages.
        self.campaign_min_sent_for_bounce_check: int = int(
            get_env("CAMPAIGN_MIN_SENT_FOR_BOUNCE_CHECK", required=False, default="50")
        )

        # Per-account daily sending cap. Any account found with daily_limit
        # above this is auto-adjusted back down to this value.
        self.daily_limit_max: int = int(
            get_env("DAILY_LIMIT_MAX", required=False, default="30")
        )

        # Non-reconnectable provider errors (e.g. Gmail "daily user sending
        # limit exceeded") auto-recover over time. We re-alert after this many
        # hours if the error is still present so operators don't forget about
        # a stuck account. 0 disables re-alerting (first alert only).
        self.provider_error_realert_hours: int = int(
            get_env("PROVIDER_ERROR_REALERT_HOURS", required=False, default="24")
        )

    def apply_overrides(self, settings: dict) -> None:
        """
        Overlay credential-type settings read from the Sheet's 'Settings' tab.

        Precedence: a non-empty Sheet value wins over the env var / secret, which
        in turn wins over the code default. Only keys in _SHEET_OVERRIDABLE are
        honoured — tuning thresholds are never Sheet-driven.

        Mutates attributes in place so callers holding a reference to this Config
        (including monitor.py's crash handler) see the resolved values.
        """
        if not settings:
            return
        for key in self._SHEET_OVERRIDABLE:
            value = str(settings.get(key, "") or "").strip()
            if value:
                setattr(self, key, value)
                logger.info("Config: '%s' overridden from Sheet 'Settings' tab.", key)

    def configure_logging(self):
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Silence overly verbose third-party loggers
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("gspread").setLevel(logging.WARNING)


# Singleton loaded once at import time (test code can patch this)
try:
    config = Config()
except EnvironmentError as exc:
    # Allow import without crashing so tests can patch env vars before using config
    config = None  # type: ignore
    logger.debug("Config not loaded at import time: %s", exc)
