"""All environment configuration in one place (mirrors pricing/config/env.js)."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENV: str = "dev"
    # Public https origin of THIS backend (carriers call back to it).
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # ── Firebase (same project as the Astric app + Node pricing backend) ──
    FIREBASE_PROJECT_ID: str = ""
    FIREBASE_CLIENT_EMAIL: str = ""
    FIREBASE_PRIVATE_KEY: str = ""           # literal \n sequences are fixed up
    GOOGLE_APPLICATION_CREDENTIALS: str = "" # alternative: service-account file

    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Secrets ──────────────────────────────────────────────────────────
    # Fernet key (urlsafe base64, 32 bytes):  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
    SECRETS_KEY: str = ""
    # HMAC key for the per-call tokens in webhook / websocket URLs.
    CALL_TOKEN_SECRET: str = ""

    # ── Platform provider keys (customers bring only telephony) ──────────
    DEEPGRAM_API_KEY: str = ""
    SARVAM_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # ── LLM ───────────────────────────────────────────────────────────────
    # LLM_PROVIDER=groq  +  GROQ_MODEL=openai/gpt-oss-120b  is all that is needed. The one model runs the live call
    # and the post-call analysis. Optional per-use overrides below (leave empty to use GROQ_MODEL).
    LLM_PROVIDER: str = "groq"               # groq | deepseek (deepseek: post-call analysis only; live calls use groq)
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_VOICE_MODEL: str = ""               # optional: different model just for live calls
    GROQ_ANALYSIS_MODEL: str = ""            # optional: different model just for post-call analysis
    ANALYSIS_PROVIDER: str = ""              # optional: "" = follow LLM_PROVIDER; deepseek | groq (auto-falls back)

    # ── Limits & billing rules ────────────────────────────────────────────
    MAX_CONCURRENT_PER_TENANT: int = 3
    MAX_CONCURRENT_GLOBAL: int = 30
    MAX_CALL_SECONDS: int = 600
    BILLING_ROUND_SECONDS: int = 6           # round connected time up to this
    MIN_BILLABLE_SECONDS: int = 3            # shorter than this is free
    INFLIGHT_RESERVE_MINUTES: float = 2.0    # held per active call when gating
    DEFAULT_COUNTRY_CODE: str = "91"
    RING_TIMEOUT_SECONDS: int = 35
    PRERENDER_GREETING: bool = True

    # Silence handling (same values as the proven Vobiz pipeline)
    SILENCE_NUDGE_SECONDS: float = 15.0
    SILENCE_HANGUP_SECONDS: float = 45.0

    # Dialer
    DIALER_TICK_SECONDS: int = 10
    CALL_STUCK_SECONDS: int = 1500           # reaper: calls with no hangup after this

    CORS_ORIGINS: str = "*"

    @property
    def voice_model(self) -> str:
        return (self.GROQ_VOICE_MODEL or self.GROQ_MODEL).strip()

    @property
    def analysis_model(self) -> str:
        return (self.GROQ_ANALYSIS_MODEL or self.GROQ_MODEL).strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
