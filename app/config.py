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

# ---- ads (provider-agnostic) ----------------------------------------------
# Default network is Google AdSense: set ADSENSE_CLIENT (ca-pub-...) and the
# per-placement ad-unit slot IDs below. Any other network can be dropped in
# per placement with AD_HTML_<PLACEMENT> raw markup, which wins over AdSense.
# Nothing renders when all of these are empty, so dev/pre-approval is clean.
ADSENSE_CLIENT = _env("ADSENSE_CLIENT")
AD_SLOTS = {
    "top": _env("AD_SLOT_TOP"),
    "mid": _env("AD_SLOT_MID"),
    "footer": _env("AD_SLOT_FOOTER"),
    "sidebar": _env("AD_SLOT_SIDEBAR"),
    "anchor": _env("AD_SLOT_ANCHOR"),
}
AD_RAW = {name: _env("AD_HTML_" + name.upper()) for name in AD_SLOTS}
ADS_ENABLED = bool(ADSENSE_CLIENT) or any(AD_SLOTS.values()) or any(AD_RAW.values())

# The owner console is unavailable until a password is configured.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

# absolute origin used in the sitemap and generated links. When unset,
# request-context code falls back to the incoming request's own origin, so a
# local run never links off-site.
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")
DEFAULT_PUBLIC_URL = "https://decoded.report"


DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "decode.db"


def has_twelve() -> bool:
    return bool(TWELVEDATA_API_KEY)


def has_finnhub() -> bool:
    return bool(FINNHUB_API_KEY)
