"""Central config. Loads everything from environment / .env. No secrets in code."""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ─── X API ──────────────────────────────────────────────────────────
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")
X_BEARER_TOKEN = os.getenv("X_BEARER_TOKEN", "")

# ─── Supabase ───────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

# ─── Telegram ───────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID = int(os.getenv("TELEGRAM_USER_ID", "0") or "0")

# ─── Behaviour ──────────────────────────────────────────────────────
DRY_RUN = _bool("DRY_RUN", True)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Singapore")
METRICS_LOOKBACK_DAYS = int(os.getenv("METRICS_LOOKBACK_DAYS", "7") or "7")

# Posting slots: (label, "HH:MM") in TIMEZONE. The scheduler posts the next
# approved tweet at each slot.
POSTING_SLOTS = [
    ("morning", "08:00"),
    ("lunch", "12:30"),
    ("evening", "19:00"),
]

# Daily analytics snapshot time.
ANALYTICS_TIME = "00:05"
