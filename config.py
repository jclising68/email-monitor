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
    Returns parsed Google service account JSON.
    Supports two forms:
      - GOOGLE_CREDENTIALS_JSON_B64: base64-encoded JSON (Railway-friendly)
      - GOOGLE_CREDENTIALS_JSON_FILE: path to a local JSON file
    """
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
    def __init__(self):
        self.slack_webhook_url: str = get_env("SLACK_WEBHOOK_URL")
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

        # Health score thresholds (from warmup analytics)
        self.health_score_alert_threshold: int = int(
            get_env("HEALTH_SCORE_ALERT_THRESHOLD", required=False, default="80")
        )
        self.health_score_critical_threshold: int = int(
            get_env("HEALTH_SCORE_CRITICAL_THRESHOLD", required=False, default="60")
        )

        # Spam rate threshold (percentage, 0-100). Alert if 7-day spam rate exceeds this.
        self.spam_rate_alert_threshold: int = int(
            get_env("SPAM_RATE_ALERT_THRESHOLD", required=False, default="10")
        )

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
