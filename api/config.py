"""Central config. Everything comes from environment variables — no secrets in git."""
import os
from functools import lru_cache
from dotenv import load_dotenv

load_dotenv()


def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v


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
