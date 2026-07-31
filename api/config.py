"""Central config. Everything comes from environment variables — no secrets in git."""
import os
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


# Placeholders for PHARMAOS_TESTING=1, for the settings whose FORM is parsed rather than
# merely read. `test-supabase_url` is a string, not a URL: supabase-py validates against
# ^(https?)://.+ and raises SupabaseException("Invalid URL") while api/db.py is still being
# imported -- so every test that imports any api module died at collection, which read as 80
# broken tests rather than one bad string. psycopg was quieter about the same problem and
# only warned ('missing "=" after "test-database_url" in connection info string'), 261 times
# in one run, which buries whatever the run was actually trying to tell you.
#
# These must never reach anything real. They are shaped to parse and to fail to connect:
# test.supabase.co does not resolve and nothing listens on localhost:5432 in CI.
#
# SUPABASE_SERVICE_KEY is here for the same reason and is easy to miss: supabase-py
# validates the key against a JWT shape too (^seg.seg.seg$), so `test-key` fails exactly
# like `test-supabase_url` did, one line further down the same constructor. Three dotted
# segments is the whole requirement -- it is not decoded and not sent anywhere -- so the
# value is deliberately the least credential-looking thing that satisfies the regex.
_TEST_PLACEHOLDERS = {
    "SUPABASE_URL": "https://test.supabase.co",
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "SUPABASE_SERVICE_KEY": "test.test.test",
}


def _req(key: str) -> str:
    """A real environment variable ALWAYS wins; the placeholder is only a last resort.

    os.getenv is consulted first and returned unconditionally when set, so production
    behaviour is unchanged and PHARMAOS_TESTING cannot override a value someone has
    deliberately supplied -- including in CI, where the workflow sets these explicitly as
    well. The placeholder exists so that importing an api module does not require a
    database to exist.
    """
    v = os.getenv(key)
    if v:
        return v
    if os.getenv("PHARMAOS_TESTING") == "1":
        return _TEST_PLACEHOLDERS.get(key, f"test-{key.lower()}")
    raise RuntimeError(f"Missing required env var: {key}")


class Settings:
    # --- database (Supabase -> Project Settings -> Database -> Connection string -> URI) ---
    DATABASE_URL = _req("DATABASE_URL")

    # --- supabase storage ---
    SUPABASE_URL = _req("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = _req("SUPABASE_SERVICE_KEY")
    BUCKET_INVOICES = os.getenv("BUCKET_INVOICES", "invoices")
    BUCKET_RX = os.getenv("BUCKET_RX", "prescriptions")
    BUCKET_DOCS = os.getenv("BUCKET_DOCS", "docs")

    # --- LLM credentials & models ---
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    
    if not GEMINI_API_KEY and not ANTHROPIC_API_KEY:
        raise RuntimeError("Missing required env var: Either GEMINI_API_KEY or ANTHROPIC_API_KEY must be set.")

    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini" if GEMINI_API_KEY else "anthropic")
    MODEL_VISION = os.getenv("MODEL_VISION", "gemini-3.6-flash" if GEMINI_API_KEY else "claude-opus-5")
    MODEL_CHAT = os.getenv("MODEL_CHAT", "gemini-3.6-flash" if GEMINI_API_KEY else "claude-sonnet-5")

    # --- whatsapp transport ---
    # 'gowa'    = go-whatsapp-web-multidevice (github.com/aldinokemal). Multi-device,
    #             so one server can hold a separate WhatsApp account per pharmacy.
    # 'baileys' = the original wa-gateway/ node service. Single device.
    # Only wa.py and main.py's webhook know the difference; no business logic moves.
    WA_BACKEND = os.getenv("WA_BACKEND", "baileys").lower()
    WA_GATEWAY_URL = os.getenv("WA_GATEWAY_URL", "http://localhost:3000")
    SHARED_SECRET = _req("SHARED_SECRET")   # gateway <-> api, and cron <-> api

    # --- GOWA (used when WA_BACKEND=gowa) ---
    GOWA_URL = os.getenv("GOWA_URL", "http://localhost:3000")
    GOWA_USER = os.getenv("GOWA_USER", "")          # from APP_BASIC_AUTH=user:pass
    GOWA_PASS = os.getenv("GOWA_PASS", "")
    # JID of the device to send AS, e.g. 254712345678@s.whatsapp.net. Optional while
    # only one device is registered; REQUIRED once a second pharmacy is added, or
    # GOWA cannot tell which account should send.
    GOWA_DEVICE_ID = os.getenv("GOWA_DEVICE_ID", "")
    # GOWA signs webhooks with HMAC-SHA256 in X-Hub-Signature-256. Its own default
    # secret is the literal string "secret", so set WHATSAPP_WEBHOOK_SECRET on the
    # GOWA side and mirror it here.
    GOWA_WEBHOOK_SECRET = os.getenv("GOWA_WEBHOOK_SECRET", "secret")

    # --- anti-ban safety gates ---
    # Comma-separated phone numbers. When set, ONLY these numbers can receive
    # messages. Leave empty in production (gate is skipped when empty).
    WA_ALLOWLIST = os.getenv("WA_ALLOWLIST", "")
    # Per-device-per-hour caps. These are deliberately conservative.
    WA_RATE_LIMIT_HOUR = int(os.getenv("WA_RATE_LIMIT_HOUR", "80"))
    WA_NEW_CHAT_LIMIT_HOUR = int(os.getenv("WA_NEW_CHAT_LIMIT_HOUR", "15"))
    # Largest batch broadcast() will accept. Bulk sending is the fastest route to a ban,
    # so a caller asking for more is refused outright rather than silently truncated.
    WA_BROADCAST_MAX = int(os.getenv("WA_BROADCAST_MAX", "25"))
    # Seconds between bulk sends, randomised within this range. A fixed interval is
    # itself a bot signature; humans do not send every 1.5s exactly.
    WA_PACE_MIN_SECS = float(os.getenv("WA_PACE_MIN_SECS", "4"))
    WA_PACE_MAX_SECS = float(os.getenv("WA_PACE_MAX_SECS", "11"))

    # --- tenant (single pharmacy for the pilot) ---
    PHARMACY_ID = _req("PHARMACY_ID")

    # Make the DATABASE enforce tenant isolation instead of trusting every query to
    # remember `where pharmacy_id = %s`. Off by default: the hand-written filters
    # still work, and switching enforcement on is a deliberate step rather than
    # something that happens because someone deployed. See db.tenant_scope().
    DB_ENFORCE_RLS = os.getenv("DB_ENFORCE_RLS", "false").lower() in ("1", "true", "yes")

    # --- m-pesa daraja ---
    MPESA_ENV = os.getenv("MPESA_ENV", "sandbox")          # sandbox | production
    MPESA_KEY = os.getenv("MPESA_CONSUMER_KEY", "")
    MPESA_SECRET = os.getenv("MPESA_CONSUMER_SECRET", "")
    MPESA_SHORTCODE = os.getenv("MPESA_SHORTCODE", "174379")
    MPESA_PASSKEY = os.getenv("MPESA_PASSKEY", "")
    MPESA_CALLBACK_URL = os.getenv("MPESA_CALLBACK_URL", "")   # https://<api>/mpesa/callback

    # --- business rules ---
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
    MIN_SHELF_LIFE_DAYS = int(os.getenv("MIN_SHELF_LIFE_DAYS", "60"))   # don't dispense from batches expiring sooner
    RX_MAX_AGE_DAYS = int(os.getenv("RX_MAX_AGE_DAYS", "90"))
    MATCH_THRESHOLD = float(os.getenv("MATCH_THRESHOLD", "0.55"))       # pg_trgm similarity floor
    LINE_CONF_THRESHOLD = float(os.getenv("LINE_CONF_THRESHOLD", "0.75"))
    POINTS_PER_KES = int(os.getenv("POINTS_PER_KES", "100"))            # 1 point per 100 KES
    DELIVERY_FEE = float(os.getenv("DELIVERY_FEE", "150"))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
