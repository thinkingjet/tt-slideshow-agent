from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_DATABASE_URL = "sqlite:///data/clip_creator.db"
DEFAULT_DATA_DIR = "data"
DEFAULT_HOOK_SECONDS = 3.0
DEFAULT_POSTBRIDGE_API_BASE_URL = "https://api.post-bridge.com/v1"
DEFAULT_PERPLEXITY_AGENT_MODEL = "perplexity/sonar"
DEFAULT_REPLICATE_IMAGE_MODEL = "openai/gpt-image-2"
DEFAULT_OPENAI_CTA_IMAGE_SIZE = "1088x1920"
DEFAULT_OPENAI_CTA_IMAGE_QUALITY = "medium"
DEFAULT_OPENAI_TEXT_MODEL = "gpt-4.1-mini"
DEFAULT_CTA_AUDIO_PATH = "phonk.mp3"
DEFAULT_SUPABASE_STORAGE_BUCKET = "product-context"


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    hook_seconds: float
    postbridge_api_key: str | None
    postbridge_api_base_url: str
    perplexity_api_key: str | None
    perplexity_agent_model: str
    replicate_api_token: str | None
    replicate_image_model: str
    openai_api_key: str | None
    openai_text_model: str
    openai_cta_image_size: str
    openai_cta_image_quality: str
    cta_audio_path: str
    supabase_url: str | None
    supabase_anon_key: str | None
    supabase_service_role_key: str | None
    supabase_storage_bucket: str


def load_settings(
    database_url: str | None = None,
    data_dir: str | Path | None = None,
    hook_seconds: float | None = None,
) -> Settings:
    load_dotenv()
    return Settings(
        database_url=database_url
        or os.getenv("DATABASE_URL")
        or DEFAULT_DATABASE_URL,
        data_dir=Path(
            data_dir
            or os.getenv("CLIP_CREATOR_DATA_DIR")
            or DEFAULT_DATA_DIR
        ),
        hook_seconds=float(
            hook_seconds
            if hook_seconds is not None
            else os.getenv("CLIP_CREATOR_HOOK_SECONDS", DEFAULT_HOOK_SECONDS)
        ),
        postbridge_api_key=os.getenv("POSTBRIDGE_API_KEY") or None,
        postbridge_api_base_url=os.getenv("POSTBRIDGE_API_BASE_URL")
        or DEFAULT_POSTBRIDGE_API_BASE_URL,
        perplexity_api_key=os.getenv("PERPLEXITY_API_KEY") or None,
        perplexity_agent_model=os.getenv("PERPLEXITY_AGENT_MODEL")
        or DEFAULT_PERPLEXITY_AGENT_MODEL,
        replicate_api_token=os.getenv("REPLICATE_API_TOKEN") or None,
        replicate_image_model=os.getenv("REPLICATE_IMAGE_MODEL") or DEFAULT_REPLICATE_IMAGE_MODEL,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_text_model=os.getenv("OPENAI_TEXT_MODEL") or DEFAULT_OPENAI_TEXT_MODEL,
        openai_cta_image_size=os.getenv("OPENAI_CTA_IMAGE_SIZE") or DEFAULT_OPENAI_CTA_IMAGE_SIZE,
        openai_cta_image_quality=os.getenv("OPENAI_CTA_IMAGE_QUALITY") or DEFAULT_OPENAI_CTA_IMAGE_QUALITY,
        cta_audio_path=os.getenv("CLIP_CREATOR_CTA_AUDIO_PATH") or DEFAULT_CTA_AUDIO_PATH,
        supabase_url=(os.getenv("SUPABASE_URL") or "").rstrip("/") or None,
        supabase_anon_key=os.getenv("SUPABASE_ANON_KEY") or None,
        supabase_service_role_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or None,
        supabase_storage_bucket=os.getenv("SUPABASE_STORAGE_BUCKET")
        or DEFAULT_SUPABASE_STORAGE_BUCKET,
    )

