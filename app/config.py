"""Central configuration - everything comes from environment variables.

Copy .env.example to .env and fill it in; python-dotenv loads it on import.
No secret may ever be hard-coded anywhere else in the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _csv(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _project_path(name: str, default: Path) -> Path:
    """Resolve relative path settings against the repository root."""
    configured = Path(os.getenv(name, str(default))).expanduser()
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


#: ADK derives an app's name from the AGENT PACKAGE DIRECTORY, so every session
#: it writes is keyed on "app". Defaulting app_name to anything else (it used to
#: default to "carousel_factory") means the fetcher, the review API and the web
#: UI address different sessions and a resume silently finds nothing. Deriving
#: it from this file's own directory makes the two agree by construction, and
#: keeps working if the package is ever renamed.
_ADK_APP_NAME = Path(__file__).resolve().parent.name


@dataclass(frozen=True)
class Settings:
    # --- app ---
    app_name: str = os.getenv("APP_NAME", _ADK_APP_NAME)
    max_rework_rounds: int = int(os.getenv("MAX_REWORK_ROUNDS", "5"))
    # Automatic QA retries get their own, tighter budget. They are the
    # machine correcting itself - a missing artifact, an undersized
    # render - and if three attempts have not fixed it, a fourth will
    # not either. Sharing max_rework_rounds meant flaky renders could
    # spend the reviewer's whole allowance before they saw a carousel.
    max_qa_rounds: int = int(os.getenv("MAX_QA_ROUNDS", "3"))
    max_carousel_slides: int = int(os.getenv("MAX_CAROUSEL_SLIDES", "10"))  # IG limit

    # --- models (LLM) ---
    # "openai/" ids go through LiteLLM (see app/llm.py); bare ids (gemini-*)
    # use ADK's native Google path and need GOOGLE_API_KEY. Change these in
    # .env, not here. All-OpenAI by default per the user's subscription.
    planner_model: str = os.getenv("PLANNER_MODEL", "openai/gpt-5.6-sol")
    utility_model: str = os.getenv("UTILITY_MODEL", "openai/gpt-5.4-mini")
    # GPT-5.5 is the writing specialist for final slide/caption phrasing.
    phrasing_model: str = os.getenv("PHRASING_MODEL", "openai/gpt-5.5")
    image_model: str = os.getenv("IMAGE_MODEL", "gpt-image-2")

    # --- storage / db (Supabase) ---
    database_url: str = os.getenv("DATABASE_URL", "")  # postgresql+asyncpg://...
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_service_key: str = os.getenv("SUPABASE_SERVICE_KEY", "")
    media_bucket: str = os.getenv("MEDIA_BUCKET", "carousel-media")
    # S3-compatible credentials for the artifact service adapter
    s3_endpoint: str = os.getenv("SUPABASE_S3_ENDPOINT", "")
    s3_region: str = os.getenv("SUPABASE_S3_REGION", "us-east-1")
    s3_access_key: str = os.getenv("SUPABASE_S3_ACCESS_KEY", "")
    s3_secret_key: str = os.getenv("SUPABASE_S3_SECRET_KEY", "")

    # --- mail ---
    gmail_sender: str = os.getenv("GMAIL_SENDER", "")
    gmail_credentials_path: str = os.getenv(
        "GMAIL_CREDENTIALS_PATH", str(PROJECT_ROOT / "secrets" / "gmail-credentials.json")
    )
    gmail_token_path: str = os.getenv(
        "GMAIL_TOKEN_PATH", str(PROJECT_ROOT / "secrets" / "gmail-token.json")
    )
    reviewer_emails: list[str] = field(default_factory=lambda: _csv("REVIEWER_EMAILS"))

    # --- telegram (the review channel) ---
    # Carries the same review request the mail path did: slide previews plus
    # Approve/Reject links back to the review API. Chosen over Gmail because it
    # needs no Google Cloud project, no OAuth consent screen and no 7-day token
    # expiry - a bot token and a chat id are the whole setup.
    # NOTE: there is deliberately no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    #
    # The bot is connected from the console (Profile -> Telegram), which
    # discovers the chat id itself and stores the token ENCRYPTED in
    # app_config. Keeping an environment fallback would have meant a bearer
    # credential sitting in plaintext in a file, in every shell that inherits
    # it and in the process listing - and a second, invisible source of truth
    # that quietly overrides whatever the console shows.
    #
    # SECRETS_KEY is what encrypts it. See app/services/secret_box.py.
    secrets_key: str = os.getenv("SECRETS_KEY", "")

    # --- instagram ---
    ig_user_id: str = os.getenv("IG_USER_ID", "")
    ig_access_token: str = os.getenv("IG_ACCESS_TOKEN", "")
    ig_api_version: str = os.getenv("IG_API_VERSION", "v23.0")

    # --- CTA destinations ---
    substack_url: str = os.getenv("SUBSTACK_URL", "")
    youtube_url: str = os.getenv("YOUTUBE_URL", "")
    ig_handle: str = os.getenv("IG_HANDLE", "@baskaranbuilds")

    # --- fetcher sources ---
    rss_feeds: list[str] = field(default_factory=lambda: _csv("RSS_FEEDS"))
    youtube_channels: list[str] = field(default_factory=lambda: _csv("YOUTUBE_CHANNELS"))
    newsletter_query: str = os.getenv(
        "NEWSLETTER_QUERY", "label:newsletters newer_than:2d"
    )

    # --- web console + auth ---
    # The anon key is PUBLIC by design - it ships inside the browser bundle, so
    # it is served to the SPA by /api/auth/config. The service key never is.
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    # Supabase signs JWTs two ways depending on project age: a shared HS256
    # secret (legacy) or asymmetric keys published as JWKS (current). Set this
    # only for a legacy project; leaving it empty selects the JWKS path.
    supabase_jwt_secret: str = os.getenv("SUPABASE_JWT_SECRET", "")
    # Signs OUR session cookie - unrelated to Supabase's keys. Must be stable
    # across restarts or everyone is logged out on every deploy.
    session_secret: str = os.getenv("SESSION_SECRET", "")
    session_ttl_s: int = int(os.getenv("SESSION_TTL_S", str(12 * 3600)))
    # Seeds app_users ONLY while that table is empty, so a fresh database is
    # not locked out of its own console.
    auth_bootstrap_emails: tuple[str, ...] = tuple(
        e.strip()
        for e in os.getenv("AUTH_BOOTSTRAP_EMAILS", "").split(",")
        if e.strip()
    )
    # This service's own public URL. Used for CORS and for building absolute
    # links; Render cannot self-reference a URL in a blueprint, so it is set by
    # hand after the first deploy.
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")

    # --- observability (Langfuse; empty keys = tracing disabled) ---
    langfuse_public_key: str = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key: str = os.getenv("LANGFUSE_SECRET_KEY", "")
    langfuse_base_url: str = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

    # --- media / design assets ---
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    # Cover clip duration window in seconds (sourced video trimmed into it).
    cover_clip_min_s: float = float(os.getenv("COVER_CLIP_MIN_S", "4"))
    cover_clip_max_s: float = float(os.getenv("COVER_CLIP_MAX_S", "15"))
    skills_dir: Path = PROJECT_ROOT / "skills"
    workdir: Path = Path(os.getenv("WORKDIR", str(PROJECT_ROOT / ".work")))
    cover_overlay_template: Path = Path(
        os.getenv("COVER_OVERLAY_TEMPLATE", str(PROJECT_ROOT / "STRANGE-COVER (1).png"))
    )
    cover_reference_images: tuple[str, ...] = (
        str(PROJECT_ROOT / "CONFIG-INSTA-1.png"),
        str(PROJECT_ROOT / "STRANGE-COVER (1).png"),
        str(PROJECT_ROOT / "WhatsApp Image 2026-08-11 at 4.25.57 PM.jpeg"),
    )
    slide_width: int = 1080
    slide_height: int = 1350  # 4:5 - first item's aspect ratio governs the carousel


settings = Settings()


def agent_instructions(name: str) -> str:
    """Load an agent's instruction file from skills/agents/<name>.md.

    The Learner agent updates these files from feedback ("harness updates"),
    so instructions must always be read from disk at agent-build time.
    """
    path = settings.skills_dir / "agents" / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def load_skill(filename: str) -> str:
    """Load a shared skill file from skills/ (e.g. 'cover-style.md')."""
    path = settings.skills_dir / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""
