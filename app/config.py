import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PRODUCT_DIR = BASE_DIR.parent
DATA_DIR = PRODUCT_DIR / "data"

load_dotenv(PRODUCT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """Read an env var, treating an explicitly empty value as unset so that
    compose passthrough entries (which inject "" for undefined names) can
    never blank out code defaults."""
    return (os.getenv(name) or "").strip() or default


APP_NAME = _env("APP_NAME", "decoded.report")
APP_VERSION = _env("APP_VERSION", "0.1.0")
SEC_USER_AGENT = _env("SEC_USER_AGENT", "decoded.report research contact@decoded.report")

TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
MASSIVE_API_KEY = os.getenv("MASSIVE_API_KEY", "").strip()

# Telegram notifications (free Bot API): token from @BotFather, username is
# the bot's public handle used to build t.me deep links on report pages
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@")
# optional shared secret for the production webhook route
TG_WEBHOOK_SECRET = os.getenv("TG_WEBHOOK_SECRET", "").strip()

# The owner console is unavailable until a password is configured.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# absolute origin used in bot messages, sitemap and generated links. When unset,
# request-context code falls back to the incoming request's own origin, so a
# local run never links off-site; background jobs (alerts) fall back to
# DEFAULT_PUBLIC_URL.
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
DEFAULT_PUBLIC_URL = "https://decoded.report"


def public_url(path: str = "") -> str:
    base = BASE_URL or DEFAULT_PUBLIC_URL
    return base + (path if path.startswith("/") else "/" + path)

DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "decode.db"


def has_twelve() -> bool:
    return bool(TWELVEDATA_API_KEY)


def has_finnhub() -> bool:
    return bool(FINNHUB_API_KEY)
