from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # When True (the default, and what production must use) tellers can only
    # log in and transact from a registered, active device. Set to False in a
    # local .env only to keep using the browser POS during development.
    require_registered_devices: bool = True

    transaction_timeout_minutes: int = 5
    withdrawal_approval_threshold: float = 5000.0

    # Local filesystem storage for KYC document uploads. Fine for local dev
    # and even a small single-server deployment; swap for S3/GCS storage
    # (store the bucket key here as file_reference instead of a local path)
    # once this needs to survive redeploys or scale past one server.
    kyc_upload_dir: str = "./kyc_uploads"

    # Dev/demo only. When > 0, the mock provider "answers" each transaction
    # this many seconds after it starts, by sending a genuinely signed
    # callback through the normal callback-processing path (signature check,
    # status change, commission, receipt eligibility — all the real steps).
    # 0 (the default) leaves mock transactions pending until a callback
    # arrives from somewhere else, which is what you'd want in production.
    mock_auto_confirm_seconds: float = 0.0

    # Comma-separated browser origins allowed to call this API. Add your
    # computer's LAN address here (e.g. http://192.168.1.42:5173) to test
    # the POS app from a phone on the same wifi.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # What "a day" means in reports. Transactions are stored in UTC, but the
    # people reading the report think in local days: something confirmed at
    # 01:30 in Maseru is still the previous day in UTC.
    report_timezone: str = "Africa/Maseru"

    # Provider configs are loaded per-adapter from env in adapters/registry.py

    # C-Pay (Chaperone) — real integration, not mock-backed.
    # cpay_base_url should include the full API path (confirmed working
    # value: "https://cpay-uat-env.chaperone.co.ls:5100/api/cpaypayments"),
    # since CPayAdapter just appends "/payment" or "/confirm" to it.
    # cpay_secret is the HMAC checksum secret issued alongside the API key —
    # NOT the same thing as cpay_callback_secret below, which is unused by
    # CPayAdapter since C-Pay has no callback mechanism at all.
    cpay_base_url: str = ""
    cpay_api_key: str = ""
    cpay_secret: str = ""
    cpay_client_code: str = ""
    cpay_currency: str = "LSL"
    cpay_callback_secret: str = ""

    mpesa_base_url: str = ""
    mpesa_api_key: str = ""
    mpesa_callback_secret: str = ""

    ecocash_base_url: str = ""
    ecocash_api_key: str = ""
    ecocash_callback_secret: str = ""

    mywallet_base_url: str = ""
    mywallet_api_key: str = ""
    mywallet_callback_secret: str = ""

    khetsi_base_url: str = ""
    khetsi_api_key: str = ""
    khetsi_callback_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
