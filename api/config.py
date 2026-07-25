"""Central config. Everything comes from environment variables — no secrets in git."""
import os
from functools import lru_cache


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

    # --- anthropic ---
    ANTHROPIC_API_KEY = _req("ANTHROPIC_API_KEY")
    MODEL_VISION = os.getenv("MODEL_VISION", "claude-opus-5")     # expensive errors -> best model
    MODEL_CHAT = os.getenv("MODEL_CHAT", "claude-sonnet-5")       # routing / drafting

    # --- whatsapp gateway ---
    WA_GATEWAY_URL = os.getenv("WA_GATEWAY_URL", "http://localhost:3000")
    SHARED_SECRET = _req("SHARED_SECRET")   # gateway <-> api, and cron <-> api

    # --- tenant (single pharmacy for the pilot) ---
    PHARMACY_ID = _req("PHARMACY_ID")

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
