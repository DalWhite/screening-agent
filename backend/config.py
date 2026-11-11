from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "http://127.0.0.1:8080/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", "local-dev-key"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen3.8-flash-next-iq3_xxs"))
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.3")))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 700))
    llm_thinking: bool = field(default_factory=lambda: _bool("LLM_THINKING", False))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT", "45")))
    llm_fallback: str = field(default_factory=lambda: os.getenv("LLM_FALLBACK", "auto"))

    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/sazon.db"))

    # --- Screening profiles (one folder per recruiting process) ---
    profiles_dir: Path = field(default_factory=lambda: Path(os.getenv("PROFILES_DIR") or ROOT / "backend" / "data" / "profiles"))
    default_profile: str = field(default_factory=lambda: os.getenv("DEFAULT_PROFILE", "delivery_courier").strip())
    # Channels are addressed by phone number, so each one runs a single process (empty = DEFAULT_PROFILE)
    whatsapp_profile: str = field(default_factory=lambda: os.getenv("WHATSAPP_PROFILE", "").strip())
    voice_profile: str = field(default_factory=lambda: os.getenv("VOICE_PROFILE", "").strip())

    # --- External checks (asynchronous verifications) ---
    verification_tick_seconds: float = field(default_factory=lambda: float(os.getenv("VERIFICATION_TICK_SECONDS", "2")))
    verification_mock_delay_seconds: float = field(default_factory=lambda: float(os.getenv("VERIFICATION_MOCK_DELAY_SECONDS", "5")))
    verification_webhook_url: str = field(default_factory=lambda: os.getenv("VERIFICATION_WEBHOOK_URL", "").strip())
    # HMAC-SHA256 secret shared with webhook providers; callbacks are rejected while it is empty
    verification_webhook_secret: str = field(default_factory=lambda: os.getenv("VERIFICATION_WEBHOOK_SECRET", "").strip())
    verification_max_attempts: int = field(default_factory=lambda: _int("VERIFICATION_MAX_ATTEMPTS", 3))
    # Hosts a profile's webhook check may call, besides the host of VERIFICATION_WEBHOOK_URL (comma-separated).
    # Profiles are edited by recruiters: this keeps candidate data from being sent anywhere else.
    verification_allowed_hosts: str = field(default_factory=lambda: os.getenv("VERIFICATION_ALLOWED_HOSTS", "").strip())

    # --- Backoffice (recruiter portal). Empty password = open access (local development only) ---
    backoffice_user: str = field(default_factory=lambda: os.getenv("BACKOFFICE_USER", "recruiter").strip())
    backoffice_password: str = field(default_factory=lambda: os.getenv("BACKOFFICE_PASSWORD", "").strip())

    reengage_tick_seconds: float = field(default_factory=lambda: float(os.getenv("REENGAGE_TICK_SECONDS", "15")))
    reengage_first_delay: int = field(default_factory=lambda: _int("REENGAGE_FIRST_DELAY_SECONDS", 45))
    reengage_second_delay: int = field(default_factory=lambda: _int("REENGAGE_SECOND_DELAY_SECONDS", 120))
    reengage_max_attempts: int = field(default_factory=lambda: _int("REENGAGE_MAX_ATTEMPTS", 2))
    session_idle_seconds: int = field(default_factory=lambda: _int("SESSION_IDLE_SECONDS", 300))
    resume_window_hours: int = field(default_factory=lambda: _int("RESUME_WINDOW_HOURS", 48))

    pii_redaction: bool = field(default_factory=lambda: _bool("PII_REDACTION", True))
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 90))

    # --- Channels (Twilio WhatsApp + Voice) ---
    twilio_account_sid: str = field(default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", "").strip())
    twilio_auth_token: str = field(default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", "").strip())
    # Optional: authenticate REST calls with an API key (SK...) instead of the auth token.
    # Webhook signatures are always verified with the auth token (that is what Twilio signs with).
    twilio_api_key_sid: str = field(default_factory=lambda: os.getenv("TWILIO_API_KEY_SID", "").strip())
    twilio_api_key_secret: str = field(default_factory=lambda: os.getenv("TWILIO_API_KEY_SECRET", "").strip())
    twilio_whatsapp_from: str = field(default_factory=lambda: os.getenv("TWILIO_WHATSAPP_FROM", "").strip())
    # Approved Content Template (HX...) used for business-initiated pushes (re-engagement nudges)
    # when WhatsApp refuses free-form text: template-only senders or outside the 24h window.
    twilio_whatsapp_content_sid: str = field(default_factory=lambda: os.getenv("TWILIO_WHATSAPP_CONTENT_SID", "").strip())
    # Local Twilio simulator (python -m simulators): send REST calls here instead of https://*.twilio.com.
    # Development/testing only; leave empty in production.
    twilio_api_base_url: str = field(default_factory=lambda: os.getenv("TWILIO_API_BASE_URL", "").strip().rstrip("/"))
    twilio_validate_signatures: bool = field(default_factory=lambda: _bool("TWILIO_VALIDATE_SIGNATURES", True))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    voice_tts_voice: str = field(default_factory=lambda: os.getenv("VOICE_TTS_VOICE", ""))
    voice_max_silence_retries: int = field(default_factory=lambda: _int("VOICE_MAX_SILENCE_RETRIES", 2))
    whatsapp_char_limit: int = field(default_factory=lambda: _int("WHATSAPP_CHAR_LIMIT", 1500))
    # Twilio drops webhooks after 15s; answer within this budget or deliver the reply via REST.
    whatsapp_sync_timeout: float = field(default_factory=lambda: float(os.getenv("WHATSAPP_SYNC_TIMEOUT_SECONDS", "10")))

    # --- WhatsApp provider: "twilio" (default) or "meta" (WhatsApp Cloud API, directly) ---
    whatsapp_provider: str = field(default_factory=lambda: os.getenv("WHATSAPP_PROVIDER", "twilio").strip().lower())
    meta_access_token: str = field(default_factory=lambda: os.getenv("META_ACCESS_TOKEN", "").strip())
    meta_phone_number_id: str = field(default_factory=lambda: os.getenv("META_PHONE_NUMBER_ID", "").strip())
    # App secret signs every webhook (X-Hub-Signature-256); verify token answers the subscription handshake
    meta_app_secret: str = field(default_factory=lambda: os.getenv("META_APP_SECRET", "").strip())
    meta_verify_token: str = field(default_factory=lambda: os.getenv("META_VERIFY_TOKEN", "").strip())
    meta_validate_signatures: bool = field(default_factory=lambda: _bool("META_VALIDATE_SIGNATURES", True))
    meta_graph_version: str = field(default_factory=lambda: os.getenv("META_GRAPH_VERSION", "v21.0").strip())
    # Development only: the local simulator instead of https://graph.facebook.com
    meta_graph_base_url: str = field(default_factory=lambda: os.getenv("META_GRAPH_BASE_URL", "https://graph.facebook.com").strip().rstrip("/"))
    # Approved message template for business-initiated messages outside the 24h window.
    # Body parameters: {{1}} = candidate first name, {{2}} = agent name. Language empty = candidate's (es/en).
    meta_template_name: str = field(default_factory=lambda: os.getenv("META_TEMPLATE_NAME", "").strip())
    meta_template_language: str = field(default_factory=lambda: os.getenv("META_TEMPLATE_LANGUAGE", "").strip())
    meta_mark_read: bool = field(default_factory=lambda: _bool("META_MARK_READ", True))

    # --- Voice provider: "twilio" (default) or "vonage" (Voice API, NCCO) ---
    voice_provider: str = field(default_factory=lambda: os.getenv("VOICE_PROVIDER", "twilio").strip().lower())
    # Vonage signs webhooks with a JWT (HS256) made with the application's signature secret
    vonage_signature_secret: str = field(default_factory=lambda: os.getenv("VONAGE_SIGNATURE_SECRET", "").strip())
    vonage_validate_signatures: bool = field(default_factory=lambda: _bool("VONAGE_VALIDATE_SIGNATURES", True))

    # --- Voice notes: speech-to-text (any OpenAI-compatible /audio/transcriptions endpoint) ---
    # auto = on when STT_BASE_URL is set; none = voice notes are answered with "please type it"
    stt_provider: str = field(default_factory=lambda: os.getenv("STT_PROVIDER", "auto").strip().lower())
    stt_base_url: str = field(default_factory=lambda: os.getenv("STT_BASE_URL", "").strip().rstrip("/"))
    stt_api_key: str = field(default_factory=lambda: os.getenv("STT_API_KEY", "").strip())
    stt_model: str = field(default_factory=lambda: os.getenv("STT_MODEL", "whisper-1").strip())
    stt_timeout: float = field(default_factory=lambda: float(os.getenv("STT_TIMEOUT_SECONDS", "30")))
    whatsapp_media_max_bytes: int = field(default_factory=lambda: _int("WHATSAPP_MEDIA_MAX_BYTES", 16 * 1024 * 1024))

    data_dir: Path = ROOT / "backend" / "data"
    root_dir: Path = ROOT


settings = Settings()
