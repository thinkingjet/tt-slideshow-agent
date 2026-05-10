from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import uvicorn
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from .db import create_db_engine, create_job, get_job, init_db, make_session_factory, session_scope
from .cta_generator import generate_cta_video, generate_cta_video_from_research
from .models import ClipJob, ClipVideo, GeneratedSlideshow, JobStatus, ScheduledPost, ScheduledPostStatus, VideoStatus
from .postbridge import PostBridgeClient, PostBridgeError
from .serializers import clip_job_to_dict, clip_video_to_dict, scheduled_post_to_dict
from .settings import Settings, load_settings
from .slideshow_generator import generate_slideshow_from_research, generate_slideshow_hook
from .storage import LocalStorage
from .supabase_client import SupabaseClient, SupabaseError, SupabaseUser
from .worker import ClipWorker


WEB_DIR = Path(__file__).with_name("web")
PROCESS_LOCK = threading.Lock()
MAX_RESEARCH_FILES = 8
MAX_RESEARCH_FILE_BYTES = 15 * 1024 * 1024
PERPLEXITY_AGENT_URL = "https://api.perplexity.ai/v1/agent"

logger = logging.getLogger("clip_creator")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


class Runtime:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.storage = LocalStorage(self.settings.data_dir)
        self.engine = create_db_engine(self.settings.database_url)
        self.session_factory = make_session_factory(self.engine)
        self.storage.ensure()
        init_db(self.engine)

    def worker(self) -> ClipWorker:
        return ClipWorker(session_factory=self.session_factory, storage=self.storage)

    def postbridge(self) -> PostBridgeClient:
        return PostBridgeClient(
            api_key=self.settings.postbridge_api_key,
            base_url=self.settings.postbridge_api_base_url,
        )

    def supabase(self) -> SupabaseClient:
        return SupabaseClient(
            url=self.settings.supabase_url,
            anon_key=self.settings.supabase_anon_key,
            service_role_key=self.settings.supabase_service_role_key,
            storage_bucket=self.settings.supabase_storage_bucket,
        )


runtime = Runtime()
app = FastAPI(title="Clip Creator")
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.exception_handler(PostBridgeError)
def postbridge_error_handler(_request, exc: PostBridgeError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(SupabaseError)
def supabase_error_handler(_request, exc: SupabaseError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


class ScheduleRequest(BaseModel):
    video_ids: list[str] = Field(min_length=1)
    social_account_ids: list[int] = Field(min_length=1)
    caption: str = ""
    start_at: datetime | None = None
    interval_minutes: int = Field(default=60, ge=0)


async def _require_supabase_user(request: Request) -> SupabaseUser:
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Log in to save product research.")
    return await runtime.supabase().verify_access_token(token.strip())


def _safe_upload_name(filename: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", filename or "context-file").strip("-")
    return cleaned[:90] or "context-file"


def _research_context_dir() -> Path:
    path = runtime.settings.data_dir / "product-context"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_context_path(raw_path: str) -> Path:
    decoded = unquote(raw_path)
    resolved = (_research_context_dir() / decoded).resolve()
    context_root = _research_context_dir().resolve()
    try:
        resolved.relative_to(context_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="context file not found") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="context file not found")
    return resolved


def _research_json_schema() -> dict[str, Any]:
    def string_array(min_items: int = 1) -> dict[str, Any]:
        return {"type": "array", "minItems": min_items, "items": {"type": "string", "minLength": 1}}

    angle = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "title",
            "insight",
            "whyItWorks",
            "hookIdeas",
            "videoConcept",
            "caption",
            "cta",
            "hashtags",
            "talkingPoints",
            "proofPoints",
            "platformNotes",
        ],
        "properties": {
            "title": {"type": "string"},
            "insight": {"type": "string"},
            "whyItWorks": {"type": "string"},
            "hookIdeas": string_array(3),
            "videoConcept": {"type": "string"},
            "caption": {"type": "string"},
            "cta": {"type": "string"},
            "hashtags": string_array(5),
            "talkingPoints": string_array(3),
            "proofPoints": string_array(2),
            "platformNotes": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "product",
            "marketInsights",
            "contentAngles",
            "hookBank",
            "captions",
            "ctas",
            "hashtags",
            "postingPlan",
            "researchNotes",
            "sourcesUsed",
        ],
        "properties": {
            "product": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "summary", "targetCustomers", "buyingTriggers", "objections", "positioning"],
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                    "targetCustomers": string_array(3),
                    "buyingTriggers": string_array(3),
                    "objections": string_array(3),
                    "positioning": {"type": "string"},
                },
            },
            "marketInsights": string_array(4),
            "contentAngles": {"type": "array", "minItems": 4, "maxItems": 8, "items": angle},
            "hookBank": string_array(10),
            "captions": string_array(5),
            "ctas": string_array(5),
            "hashtags": string_array(10),
            "postingPlan": {
                "type": "array",
                "minItems": 5,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["day", "angle", "format", "hook", "caption", "cta"],
                    "properties": {
                        "day": {"type": "string"},
                        "angle": {"type": "string"},
                        "format": {"type": "string"},
                        "hook": {"type": "string"},
                        "caption": {"type": "string"},
                        "cta": {"type": "string"},
                    },
                },
            },
            "researchNotes": string_array(3),
            "sourcesUsed": {"type": "array", "items": {"type": "string"}},
        },
    }


def _extract_agent_text(agent_response: dict[str, Any]) -> str:
    if isinstance(agent_response.get("output_text"), str):
        return agent_response["output_text"]

    parts: list[str] = []
    for item in agent_response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _parse_agent_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if match:
        cleaned = match.group(1).strip()
    return json.loads(cleaned)


def _collect_agent_sources(agent_response: dict[str, Any]) -> list[dict[str, str]]:
    sources: dict[str, dict[str, str]] = {}
    for item in agent_response.get("output", []):
        if item.get("type") == "search_results":
            for result in item.get("results", []):
                url = result.get("url")
                if url:
                    sources[url] = {
                        "title": result.get("title") or url,
                        "url": url,
                        "snippet": result.get("snippet") or "",
                    }
        if item.get("type") == "fetch_url_results":
            for result in item.get("contents", []):
                url = result.get("url")
                if url:
                    sources[url] = {
                        "title": result.get("title") or url,
                        "url": url,
                        "snippet": result.get("snippet") or "",
                    }
        if item.get("type") == "message":
            for content in item.get("content", []):
                for annotation in content.get("annotations", []) or []:
                    url = annotation.get("url")
                    if url:
                        sources[url] = {
                            "title": annotation.get("title") or url,
                            "url": url,
                            "snippet": "",
                        }
    return list(sources.values())


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _ensure_count(items: list[str], fallback: list[str], count: int) -> list[str]:
    return _dedupe([*items, *fallback])[:count]


def _normalize_hashtag(tag: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "", tag.replace("#", "")).strip()
    return cleaned or "tiktokmademebuyit"


def _fallback_hooks(product_name: str) -> list[str]:
    return [
        f"I tested {product_name} so you do not have to",
        f"3 things nobody tells you about {product_name}",
        f"Before you buy {product_name}, watch this",
        f"This is why people are switching to {product_name}",
        f"The real reason {product_name} is getting attention",
        f"I did not expect {product_name} to solve this",
        f"If you struggle with this, {product_name} might help",
        f"Stop buying alternatives until you see {product_name}",
        f"{product_name} explained in 20 seconds",
        f"The easiest way to know if {product_name} is for you",
    ]


def _fallback_ctas(product_name: str) -> list[str]:
    return [
        "Tap the link to see if it is right for you.",
        "Comment 'INFO' and I will send the details.",
        "Try it today and compare it with what you use now.",
        f"Check out {product_name} before the offer changes.",
        "Save this and come back before you buy.",
    ]


def _fallback_hashtags(product_name: str) -> list[str]:
    base = [
        product_name,
        "tiktokmademebuyit",
        "productfinds",
        "amazonfinds",
        "smallbusiness",
        "lifehack",
        "musthave",
        "review",
        "viralproducts",
        "shoppingtok",
    ]
    return [_normalize_hashtag(tag) for tag in base]


def _fallback_captions(product_name: str, ctas: list[str]) -> list[str]:
    return [
        f"{product_name} solves the annoying part most people just put up with. {ctas[0]}",
        f"If you have been comparing options, start with this simple breakdown of {product_name}. {ctas[1]}",
        f"The fastest way to understand {product_name}: problem, proof, payoff. {ctas[2]}",
        f"People do not buy products. They buy the outcome. Here is the outcome {product_name} is selling. {ctas[3]}",
        f"Save this before you buy another version of {product_name}. {ctas[4]}",
    ]


def _normalize_research_plan(plan: dict[str, Any], product_name: str) -> dict[str, Any]:
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    hooks = _ensure_count(_string_list(plan.get("hookBank")), _fallback_hooks(product_name), 10)
    ctas = _ensure_count(_string_list(plan.get("ctas")), _fallback_ctas(product_name), 5)
    hashtags = _ensure_count(
        [_normalize_hashtag(tag) for tag in _string_list(plan.get("hashtags"))],
        _fallback_hashtags(product_name),
        10,
    )
    captions = _ensure_count(_string_list(plan.get("captions")), _fallback_captions(product_name, ctas), 5)

    product["name"] = str(product.get("name") or product_name).strip() or product_name
    product["summary"] = str(product.get("summary") or f"A TikTok content plan for selling {product_name}.").strip()
    product["positioning"] = str(
        product.get("positioning")
        or f"Position {product_name} around the clearest customer problem, visible proof, and a low-friction next step."
    ).strip()
    product["targetCustomers"] = _ensure_count(
        _string_list(product.get("targetCustomers")),
        [
            f"People actively comparing {product_name} with alternatives",
            "Buyers who want a faster or easier solution",
            "Customers who need proof before purchasing",
        ],
        3,
    )
    product["buyingTriggers"] = _ensure_count(
        _string_list(product.get("buyingTriggers")),
        [
            "They are frustrated with their current workaround",
            "They see a relatable before-and-after use case",
            "They understand the cost of not fixing the problem",
        ],
        3,
    )
    product["objections"] = _ensure_count(
        _string_list(product.get("objections")),
        [
            "Will this actually work for me?",
            "Is it worth the price compared with cheaper options?",
            "Can I trust the product claims?",
        ],
        3,
    )

    angles = plan.get("contentAngles") if isinstance(plan.get("contentAngles"), list) else []
    normalized_angles: list[dict[str, Any]] = []
    for index, angle in enumerate(angles[:6]):
        if not isinstance(angle, dict):
            continue
        angle_hooks = _ensure_count(_string_list(angle.get("hookIdeas")), hooks[index:index + 3], 3)
        normalized_angles.append(
            {
                "title": str(angle.get("title") or f"{product_name} angle {index + 1}").strip(),
                "insight": str(angle.get("insight") or "Lead with the customer problem before showing the product.").strip(),
                "whyItWorks": str(angle.get("whyItWorks") or "It creates relevance first, then makes the product feel like the obvious next step.").strip(),
                "hookIdeas": angle_hooks,
                "videoConcept": str(angle.get("videoConcept") or f"Show the problem, introduce {product_name}, then demonstrate the result.").strip(),
                "caption": str(angle.get("caption") or captions[index % len(captions)]).strip(),
                "cta": str(angle.get("cta") or ctas[index % len(ctas)]).strip(),
                "hashtags": _ensure_count(
                    [_normalize_hashtag(tag) for tag in _string_list(angle.get("hashtags"))],
                    hashtags,
                    5,
                ),
                "talkingPoints": _ensure_count(
                    _string_list(angle.get("talkingPoints")),
                    [
                        "Name the customer pain point",
                        "Show the product mechanism or benefit",
                        "Make the next step obvious",
                    ],
                    3,
                ),
                "proofPoints": _ensure_count(
                    _string_list(angle.get("proofPoints")),
                    [
                        "Use visible demonstration or customer quote",
                        "Show comparison with the old workaround",
                    ],
                    2,
                ),
                "platformNotes": str(angle.get("platformNotes") or "Keep the first frame text-heavy and show the product within three seconds.").strip(),
            }
        )

    while len(normalized_angles) < 4:
        index = len(normalized_angles)
        normalized_angles.append(
            {
                "title": ["Problem/Solution", "Before/After", "Objection Crusher", "Social Proof"][index],
                "insight": f"Make {product_name} relevant by tying it to a problem the viewer already feels.",
                "whyItWorks": "TikTok viewers need immediate context before they care about features.",
                "hookIdeas": hooks[index * 2:index * 2 + 3] or hooks[:3],
                "videoConcept": f"Open with a common frustration, show {product_name} in use, then end with the result.",
                "caption": captions[index % len(captions)],
                "cta": ctas[index % len(ctas)],
                "hashtags": hashtags[:5],
                "talkingPoints": ["Problem", "Proof", "Payoff"],
                "proofPoints": ["Demo the product clearly", "Show a result or comparison"],
                "platformNotes": "Use quick cuts, readable on-screen text, and a direct CTA.",
            }
        )

    posting_plan = plan.get("postingPlan") if isinstance(plan.get("postingPlan"), list) else []
    normalized_posting_plan: list[dict[str, str]] = []
    for index, item in enumerate(posting_plan[:7]):
        if not isinstance(item, dict):
            continue
        normalized_posting_plan.append(
            {
                "day": str(item.get("day") or f"Day {index + 1}"),
                "angle": str(item.get("angle") or normalized_angles[index % len(normalized_angles)]["title"]),
                "format": str(item.get("format") or "Problem/solution demo"),
                "hook": str(item.get("hook") or hooks[index % len(hooks)]),
                "caption": str(item.get("caption") or captions[index % len(captions)]),
                "cta": str(item.get("cta") or ctas[index % len(ctas)]),
            }
        )

    while len(normalized_posting_plan) < 5:
        index = len(normalized_posting_plan)
        normalized_posting_plan.append(
            {
                "day": f"Day {index + 1}",
                "angle": normalized_angles[index % len(normalized_angles)]["title"],
                "format": "Short demo with on-screen text",
                "hook": hooks[index % len(hooks)],
                "caption": captions[index % len(captions)],
                "cta": ctas[index % len(ctas)],
            }
        )

    plan["product"] = product
    plan["marketInsights"] = _ensure_count(
        _string_list(plan.get("marketInsights")),
        [
            "Short-form product content works best when the first second names a specific pain point.",
            "Proof-led videos reduce skepticism better than feature-led videos.",
            "TikTok viewers respond to concrete demonstrations and plain-language outcomes.",
            "A clear CTA matters more when the product is unfamiliar.",
        ],
        4,
    )
    plan["contentAngles"] = normalized_angles
    plan["hookBank"] = hooks
    plan["captions"] = captions
    plan["ctas"] = ctas
    plan["hashtags"] = hashtags
    plan["postingPlan"] = normalized_posting_plan
    plan["researchNotes"] = _ensure_count(
        _string_list(plan.get("researchNotes")),
        [
            "Use product-specific claims only when they are visible in the product page or uploaded context.",
            "Prioritize hooks that state the problem before introducing the product.",
            "Pair every content angle with proof: demo, comparison, customer quote, or result.",
        ],
        3,
    )
    plan["sourcesUsed"] = _string_list(plan.get("sourcesUsed"))
    return plan


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="PDF support needs the pypdf dependency installed. Run `pip install -e .`.",
        ) from exc

    import io

    reader = PdfReader(io.BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    return text.strip()


async def _read_research_uploads(
    user_id: str,
    files: list[UploadFile],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    context: list[dict[str, Any]] = []
    image_parts: list[dict[str, str]] = []
    file_records: list[dict[str, Any]] = []
    supabase = runtime.supabase()

    if len(files) > MAX_RESEARCH_FILES:
        raise HTTPException(status_code=400, detail=f"Upload up to {MAX_RESEARCH_FILES} files at once.")

    for upload in files:
        content = await upload.read()
        if not content:
            continue
        if len(content) > MAX_RESEARCH_FILE_BYTES:
            raise HTTPException(status_code=400, detail=f"{upload.filename or 'file'} is larger than 15MB.")

        filename = _safe_upload_name(upload.filename)
        content_type = upload.content_type or "application/octet-stream"
        extension = Path(filename).suffix or ".bin"
        storage_file_name = f"{uuid.uuid4().hex}{extension}"
        storage_object = await supabase.upload_research_file(
            user_id=user_id,
            file_name=storage_file_name,
            content=content,
            content_type=content_type,
        )
        signed_url = storage_object["signed_url"]

        if content_type.startswith("text/") or extension.lower() in {".txt", ".md", ".markdown", ".csv"}:
            text = content.decode("utf-8", errors="ignore")[:12000]
            context_item = {"fileName": filename, "type": "text", "text": text}
        elif content_type == "application/pdf" or extension.lower() == ".pdf":
            text = _extract_pdf_text(content)[:18000]
            context_item = {"fileName": filename, "type": "pdf", "url": signed_url, "text": text}
        elif content_type.startswith("image/"):
            text = "Image uploaded as product context."
            context_item = {"fileName": filename, "type": "image", "url": signed_url, "text": text}
            image_parts.append({"type": "input_image", "image_url": signed_url})
        else:
            text = ""
            context_item = {"fileName": filename, "type": "other", "url": signed_url}

        context.append(context_item)
        file_records.append({
            "bucket": storage_object["bucket"],
            "storage_path": storage_object["storage_path"],
            "file_name": filename,
            "content_type": content_type,
            "file_size": len(content),
            "extracted_text": text or None,
        })

    return context, image_parts, file_records


def _build_research_prompt(
    product_name: str,
    product_url: str,
    audience: str,
    product_context: str,
    uploaded_context: list[dict[str, Any]],
) -> str:
    extracted_text = "\n\n".join(
        f"### {item['fileName']}\n{item['text']}"
        for item in uploaded_context
        if item.get("text")
    )[:30000]
    uploaded_urls = "\n".join(
        f"- {item['fileName']}: {item['url']}"
        for item in uploaded_context
        if item.get("url")
    )
    return f"""Research this product and turn the research into TikTok-ready content assets.

Product name:
{product_name}

Product URL:
{product_url or "Not provided"}

Target audience or niche:
{audience or "Infer from product and research."}

Seller-provided context:
{product_context or "No written context provided."}

Extracted upload text:
{extracted_text or "No extracted text provided."}

Uploaded file URLs:
{uploaded_urls or "No uploaded file URLs."}

Use web_search for the product, category, competitors, customer language, objections, and current social conversation.
Use fetch_url for the product URL and uploaded document URLs where useful.
Return practical TikTok assets: hooks, angles, captions, CTAs, hashtags, proof points, objections, and a 5-7 day posting plan.
Keep it conversion-focused, specific, and usable by someone selling this product today.
Completeness requirements:
- product.name must be the actual product name, never "Product plan".
- product.targetCustomers, buyingTriggers, and objections must each contain at least 3 specific items.
- contentAngles must contain at least 4 complete angles.
- hookBank must contain at least 10 hooks.
- captions, ctas, and postingPlan must contain at least 5 items each.
- Never return empty arrays for any required content field. If research is limited, infer useful seller-safe items and label claims conservatively.
Return only JSON matching the schema."""


def _saved_project_response(row: dict[str, Any], files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "product_name": row.get("product_name"),
        "product_url": row.get("product_url"),
        "audience": row.get("audience"),
        "product_context": row.get("product_context"),
        "data": row.get("plan") or {},
        "sources": row.get("sources") or [],
        "uploads": row.get("uploads") or [],
        "files": files or [],
        "model": row.get("model"),
        "usage": row.get("usage"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _process_job_background(job_id: str | None = None) -> None:
    if not PROCESS_LOCK.acquire(blocking=False):
        return
    try:
        worker = runtime.worker()
        if job_id is not None:
            with runtime.session_factory() as session:
                job = session.get(ClipJob, job_id)
                if job and job.status == JobStatus.QUEUED:
                    job.status = JobStatus.PROCESSING
                    session.commit()
            worker.process_job(job_id)
        while True:
            processed_job_id = worker.run_once()
            if processed_job_id is None:
                break
    finally:
        PROCESS_LOCK.release()


def _resolve_media_path(raw_path: str) -> Path:
    decoded = unquote(raw_path)
    data_root = runtime.settings.data_dir.resolve()
    path = Path(decoded)
    # For safety and consistency, resolve relative paths *within* the data directory.
    # (e.g. "cta/foo.mp4" -> "<data_dir>/cta/foo.mp4")
    resolved = (path if path.is_absolute() else (data_root / path)).resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="media file not found") from exc
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="media file not found")
    return resolved


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/media-files/{raw_path:path}")
def media_file(raw_path: str) -> FileResponse:
    path = _resolve_media_path(raw_path)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/context-files/{raw_path:path}")
def product_context_file(raw_path: str) -> FileResponse:
    path = _resolve_context_path(raw_path)
    return FileResponse(path, filename=path.name)


@app.get("/api/settings")
def api_settings() -> dict:
    return {
        "postbridge_configured": bool(runtime.settings.postbridge_api_key),
        "postbridge_base_url": runtime.settings.postbridge_api_base_url,
        "hook_seconds": runtime.settings.hook_seconds,
        "research_configured": bool(runtime.settings.perplexity_api_key),
        "supabase": runtime.supabase().public_config(),
    }


@app.get("/api/jobs")
def list_jobs() -> dict:
    with runtime.session_factory() as session:
        jobs = session.scalars(select(ClipJob).order_by(ClipJob.created_at.desc())).all()
        return {"data": [clip_job_to_dict(job) for job in jobs]}


@app.post("/api/cta/generate")
async def generate_cta_endpoint(
    product_name: str = Form(...),
    cta_hint_1: str = Form(""),
    cta_hint_2: str = Form(""),
) -> dict:
    runtime.storage.ensure()
    if not runtime.settings.replicate_api_token:
        raise HTTPException(status_code=500, detail="Add REPLICATE_API_TOKEN to .env first (required for CTA generation).")

    cleaned_name = product_name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="product_name is required.")

    hint1 = (cta_hint_1 or "").strip() or "Get the app now"
    hint2 = (cta_hint_2 or "").strip() or hint1

    cta_id = uuid.uuid4().hex
    output_path = runtime.storage.cta_dir / f"generated_{cta_id}.mp4"

    logger.info("[cta] generate (manual) id=%s product=%s", cta_id, cleaned_name)
    await generate_cta_video(
        replicate_api_token=runtime.settings.replicate_api_token,
        model=runtime.settings.replicate_image_model,
        product_name=cleaned_name,
        cta_hint_1=hint1,
        cta_hint_2=hint2,
        output_path=output_path,
        tmp_dir=runtime.storage.tmp_dir / f"cta_{cta_id}",
        audio_path=Path(runtime.settings.cta_audio_path).resolve(),
        image_size=runtime.settings.openai_cta_image_size,
        image_quality=runtime.settings.openai_cta_image_quality,
    )

    relative = output_path.resolve().relative_to(runtime.settings.data_dir.resolve())
    logger.info("[cta] generated mp4=%s media_url=%s", output_path, f"/media-files/{relative.as_posix()}")
    return {
        "id": cta_id,
        "output_path": str(output_path),
        "media_url": f"/media-files/{relative.as_posix()}",
    }


@app.post("/api/cta/generate-from-project")
async def generate_cta_from_project(
    request: Request,
    project_id: str = Form(...),
) -> dict:
    runtime.storage.ensure()
    if not runtime.settings.replicate_api_token:
        raise HTTPException(status_code=500, detail="Add REPLICATE_API_TOKEN to .env first (required for CTA generation).")

    user = await _require_supabase_user(request)
    supabase = runtime.supabase()
    project = await supabase.get_research_project(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Saved product not found.")

    cta_id = uuid.uuid4().hex
    output_path = runtime.storage.cta_dir / f"generated_{cta_id}.mp4"
    tmp_dir = runtime.storage.tmp_dir / f"cta_{cta_id}"

    logger.info("[cta] generate-from-project user=%s project=%s cta_id=%s", user.id, project_id, cta_id)
    logger.info("[cta] replicate model=%s audio=%s", runtime.settings.replicate_image_model, runtime.settings.cta_audio_path)

    try:
        planned = await generate_cta_video_from_research(
            replicate_api_token=runtime.settings.replicate_api_token,
            image_model=runtime.settings.replicate_image_model,
            research_project=project,
            output_path=output_path,
            tmp_dir=tmp_dir,
            audio_path=Path(runtime.settings.cta_audio_path).resolve(),
            image_size=runtime.settings.openai_cta_image_size,
            image_quality=runtime.settings.openai_cta_image_quality,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    relative = output_path.resolve().relative_to(runtime.settings.data_dir.resolve())
    # Upload every generated step to Supabase Storage.
    assets: dict[str, dict[str, str]] = {}
    try:
        logger.info("[cta] uploading assets to supabase bucket=%s", runtime.settings.supabase_storage_bucket)
        assets["background_png"] = await supabase.upload_asset(
            user_id=user.id,
            storage_path=f"cta/{project_id}/{cta_id}/background.png",
            content=(tmp_dir / "cta_background.png").read_bytes(),
            content_type="image/png",
        )
        logger.info(
            "[cta] uploaded background_png path=%s url=%s",
            assets["background_png"]["storage_path"],
            assets["background_png"]["signed_url"],
        )
        assets["overlay_1_png"] = await supabase.upload_asset(
            user_id=user.id,
            storage_path=f"cta/{project_id}/{cta_id}/overlay_1.png",
            content=(tmp_dir / "cta_overlay_1.png").read_bytes(),
            content_type="image/png",
        )
        logger.info(
            "[cta] uploaded overlay_1_png path=%s url=%s",
            assets["overlay_1_png"]["storage_path"],
            assets["overlay_1_png"]["signed_url"],
        )
        assets["overlay_2_png"] = await supabase.upload_asset(
            user_id=user.id,
            storage_path=f"cta/{project_id}/{cta_id}/overlay_2.png",
            content=(tmp_dir / "cta_overlay_2.png").read_bytes(),
            content_type="image/png",
        )
        logger.info(
            "[cta] uploaded overlay_2_png path=%s url=%s",
            assets["overlay_2_png"]["storage_path"],
            assets["overlay_2_png"]["signed_url"],
        )
        assets["cta_mp4"] = await supabase.upload_asset(
            user_id=user.id,
            storage_path=f"cta/{project_id}/{cta_id}/cta.mp4",
            content=output_path.read_bytes(),
            content_type="video/mp4",
        )
        logger.info("[cta] uploaded cta_mp4 path=%s url=%s", assets["cta_mp4"]["storage_path"], assets["cta_mp4"]["signed_url"])
    except SupabaseError:
        # If upload fails we still return the local media_url so you can keep testing.
        logger.exception("[cta] supabase upload failed")
        assets = {}

    logger.info("[cta] done frame1=%r frame2=%r media_url=%s", planned["frame1_text"], planned["frame2_text"], f"/media-files/{relative.as_posix()}")
    return {
        "id": cta_id,
        "project_id": project_id,
        "product_name": project.get("product_name"),
        "frame1_text": planned["frame1_text"],
        "frame2_text": planned["frame2_text"],
        "media_url": f"/media-files/{relative.as_posix()}",
        "supabase_assets": assets,
    }


# Some clients may accidentally include a trailing slash; keep this route compatible.
@app.post("/api/cta/generate-from-project/")
async def generate_cta_from_project_slash(
    request: Request,
    project_id: str = Form(...),
) -> dict:
    return await generate_cta_from_project(request=request, project_id=project_id)


@app.get("/api/cta/list")
async def list_ctas_for_project(request: Request, project_id: str) -> dict:
    """
    List previously generated CTAs for a saved product.
    We store assets in Supabase Storage under: <user_id>/cta/<project_id>/<cta_id>/cta.mp4
    """
    user = await _require_supabase_user(request)
    if not project_id.strip():
        raise HTTPException(status_code=400, detail="project_id is required")
    supabase = runtime.supabase()
    prefix = f"{user.id}/cta/{project_id.strip().lstrip('/')}/"

    objects = await supabase.list_storage_objects(prefix=prefix, limit=500, offset=0)
    mp4s = [o for o in objects if str(o.get("name") or "").endswith("/cta.mp4")]

    items: list[dict] = []
    for obj in mp4s[:50]:
        storage_path = str(obj.get("name") or "")
        try:
            signed_url = await supabase.sign_asset(storage_path=storage_path, expires_in_seconds=60 * 60)
        except Exception:
            signed_url = ""
        # Extract cta_id from ".../<cta_id>/cta.mp4"
        parts = storage_path.strip("/").split("/")
        cta_id = parts[-2] if len(parts) >= 2 else storage_path
        items.append(
            {
                "cta_id": cta_id,
                "storage_path": storage_path,
                "signed_url": signed_url,
                "updated_at": obj.get("updated_at") or obj.get("created_at") or "",
            }
        )

    return {"project_id": project_id, "items": items}


@app.post("/api/slideshows/generate-from-project")
async def generate_slideshow_from_project(
    request: Request,
    project_id: str = Form(...),
) -> dict:
    if not runtime.settings.openai_api_key:
        raise HTTPException(status_code=500, detail="Add OPENAI_API_KEY to .env first (required for slideshow generation).")

    user = await _require_supabase_user(request)
    if not project_id.strip():
        raise HTTPException(status_code=400, detail="project_id is required")

    supabase = runtime.supabase()
    project = await supabase.get_research_project(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Saved product not found.")

    try:
        hook = await generate_slideshow_hook(runtime.settings, project)
        slideshow = await generate_slideshow_from_research(runtime.settings, project, hook=hook)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    slides = slideshow.get("slides") if isinstance(slideshow, dict) else []
    slideshow_id = None
    try:
        with session_scope(runtime.session_factory) as session:
            record = GeneratedSlideshow(
                project_id=project_id,
                user_id=user.id,
                product_name=project.get("product_name"),
                hook=hook,
                image_collection=str(slideshow.get("image_collection") or ""),
                slideshow_json=json.dumps(slideshow),
            )
            session.add(record)
            session.flush()
            slideshow_id = record.id
    except Exception:
        logger.exception("[slideshows] failed to persist slideshow")

    return {
        "id": slideshow_id,
        "project_id": project_id,
        "product_name": project.get("product_name"),
        "hook": hook,
        "slides": slides,
        "slideshow": slideshow,
    }


# Some clients may accidentally include a trailing slash; keep this route compatible.
@app.post("/api/slideshows/generate-from-project/")
async def generate_slideshow_from_project_slash(
    request: Request,
    project_id: str = Form(...),
) -> dict:
    return await generate_slideshow_from_project(request=request, project_id=project_id)


@app.post("/api/jobs")
async def create_clip_job(
    background_tasks: BackgroundTasks,
    source_url: str = Form(...),
    limit: int = Form(...),
    cta: UploadFile | None = File(default=None),
    hook_seconds: float | None = Form(None),
    generate_cta: bool = Form(False),
    product_name: str = Form(""),
    cta_hint_1: str = Form(""),
    cta_hint_2: str = Form(""),
    process_now: bool = Form(True),
) -> dict:
    runtime.storage.ensure()

    # If we generate CTA automatically we always produce an MP4.
    if generate_cta or cta is None:
        suffix = ".mp4"
    else:
        suffix = Path(cta.filename or "cta.mp4").suffix or ".mp4"

    with session_scope(runtime.session_factory) as session:
        job = create_job(
            session,
            source_url=source_url,
            cta_path="pending",
            requested_limit=limit,
            hook_seconds=hook_seconds or runtime.settings.hook_seconds,
        )
        cta_path = runtime.storage.cta_dir / f"{job.id}{suffix}"
        cta_path.parent.mkdir(parents=True, exist_ok=True)

        if generate_cta or cta is None:
            if not runtime.settings.replicate_api_token:
                raise HTTPException(status_code=500, detail="Add REPLICATE_API_TOKEN to .env first (required for CTA generation).")
            if not product_name.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Missing product_name. Run Product Research first, or upload a CTA video instead.",
                )

            # Provide safe fallback hints if the UI didn't send them.
            cta_hint_1 = (cta_hint_1 or "").strip() or "Get the app now"
            cta_hint_2 = (cta_hint_2 or "").strip() or cta_hint_1

            await generate_cta_video(
                replicate_api_token=runtime.settings.replicate_api_token,
                model=runtime.settings.replicate_image_model,
                product_name=product_name.strip(),
                cta_hint_1=cta_hint_1,
                cta_hint_2=cta_hint_2,
                output_path=cta_path,
                tmp_dir=runtime.storage.job_tmp_dir(job.id),
                audio_path=Path(runtime.settings.cta_audio_path).resolve(),
                image_size=runtime.settings.openai_cta_image_size,
                image_quality=runtime.settings.openai_cta_image_quality,
            )
        else:
            # User-supplied CTA upload.
            assert cta is not None
            content = await cta.read()
            cta_path.write_bytes(content)

        job.cta_path = str(cta_path)
        response = clip_job_to_dict(job)

    if process_now:
        background_tasks.add_task(_process_job_background, response["id"])
    return response


@app.post("/api/jobs/{job_id}/process")
def process_job(job_id: str, background_tasks: BackgroundTasks) -> dict:
    with runtime.session_factory() as session:
        job = get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        job.status = JobStatus.QUEUED
        job.error = None
        session.commit()
    background_tasks.add_task(_process_job_background, job_id)
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    with runtime.session_factory() as session:
        job = get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return clip_job_to_dict(job)


@app.get("/api/clips")
def list_clips() -> dict:
    with runtime.session_factory() as session:
        videos = session.scalars(select(ClipVideo).order_by(ClipVideo.created_at.desc())).all()
        return {"data": [clip_video_to_dict(video) for video in videos]}


@app.get("/api/postbridge/accounts")
def postbridge_accounts(platform: list[str] | None = Query(default=None)) -> dict:
    client = runtime.postbridge()
    try:
        return client.get_social_accounts(platform)
    finally:
        client.close()


@app.get("/api/postbridge/posts")
def postbridge_posts() -> dict:
    client = runtime.postbridge()
    try:
        return client.get_posts()
    finally:
        client.close()


@app.get("/api/postbridge/results")
def postbridge_results() -> dict:
    client = runtime.postbridge()
    try:
        return client.get_post_results()
    finally:
        client.close()


@app.post("/api/postbridge/analytics/sync")
def sync_postbridge_analytics(platform: str | None = Query(default=None)) -> dict:
    client = runtime.postbridge()
    try:
        client.sync_analytics(platform)
        return {"ok": True}
    finally:
        client.close()


@app.get("/api/scheduled-posts")
def scheduled_posts() -> dict:
    with runtime.session_factory() as session:
        posts = session.scalars(select(ScheduledPost).order_by(ScheduledPost.scheduled_at.asc())).all()
        return {"data": [scheduled_post_to_dict(post) for post in posts]}


@app.post("/api/product-research")
async def product_research(
    request: Request,
    product_name: str = Form(...),
    product_url: str = Form(""),
    audience: str = Form(""),
    product_context: str = Form(""),
    files: list[UploadFile] | None = File(default=None),
) -> dict:
    if not runtime.settings.perplexity_api_key:
        raise HTTPException(status_code=500, detail="Add PERPLEXITY_API_KEY to .env first.")

    user = await _require_supabase_user(request)
    cleaned_name = product_name.strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="Product name is required.")

    uploaded_context, image_parts, file_records = await _read_research_uploads(user.id, files or [])
    prompt = _build_research_prompt(
        product_name=cleaned_name,
        product_url=product_url.strip(),
        audience=audience.strip(),
        product_context=product_context.strip(),
        uploaded_context=uploaded_context,
    )

    payload = {
        "model": runtime.settings.perplexity_agent_model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    *image_parts,
                ],
            }
        ],
        "instructions": (
            "You are a senior TikTok growth strategist and product researcher. "
            "Research first, then create concise, practical content assets for a seller."
        ),
        "tools": [
            {"type": "web_search", "max_tokens": 12000, "max_tokens_per_page": 2000},
            {"type": "fetch_url", "max_urls": 8},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "tiktok_product_content_plan",
                "description": "TikTok content plan for a product seller",
                "schema": _research_json_schema(),
                "strict": True,
            },
        },
        "max_steps": 5,
        "max_output_tokens": 6000,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            PERPLEXITY_AGENT_URL,
            headers={
                "Authorization": f"Bearer {runtime.settings.perplexity_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Perplexity request failed: {response.text[:500]}",
        )

    agent_response = response.json()
    if agent_response.get("status") == "failed" or agent_response.get("error"):
        error = agent_response.get("error") or {}
        raise HTTPException(status_code=502, detail=error.get("message") or "Perplexity research failed.")

    try:
        plan = _normalize_research_plan(
            _parse_agent_json(_extract_agent_text(agent_response)),
            cleaned_name,
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=502, detail="Perplexity returned malformed JSON.") from exc

    sources = _collect_agent_sources(agent_response)
    known_source_urls = {source["url"] for source in sources}
    plan["sourcesUsed"] = list(dict.fromkeys([
        *(plan.get("sourcesUsed") or []),
        *known_source_urls,
    ]))

    supabase = runtime.supabase()
    saved_project = await supabase.create_research_project({
        "user_id": user.id,
        "product_name": cleaned_name,
        "product_url": product_url.strip() or None,
        "audience": audience.strip() or None,
        "product_context": product_context.strip() or None,
        "plan": plan,
        "sources": sources,
        "uploads": uploaded_context,
        "model": agent_response.get("model"),
        "usage": agent_response.get("usage"),
    })
    saved_files = await supabase.create_research_files([
        {
            **file_record,
            "project_id": saved_project["id"],
            "user_id": user.id,
        }
        for file_record in file_records
    ])

    return {
        "id": saved_project["id"],
        "product_name": cleaned_name,
        "product_url": product_url.strip() or None,
        "audience": audience.strip() or None,
        "product_context": product_context.strip() or None,
        "data": plan,
        "sources": sources,
        "uploads": uploaded_context,
        "files": saved_files,
        "model": agent_response.get("model"),
        "usage": agent_response.get("usage"),
        "created_at": saved_project.get("created_at"),
        "updated_at": saved_project.get("updated_at"),
    }


@app.get("/api/product-research/projects")
async def list_product_research_projects(request: Request) -> dict:
    user = await _require_supabase_user(request)
    projects = await runtime.supabase().list_research_projects(user.id)
    return {"data": projects}


@app.get("/api/product-research/projects/{project_id}")
async def get_product_research_project(project_id: str, request: Request) -> dict:
    user = await _require_supabase_user(request)
    supabase = runtime.supabase()
    project = await supabase.get_research_project(user_id=user.id, project_id=project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Saved product not found.")
    files = await supabase.list_research_files(user_id=user.id, project_id=project_id)
    return _saved_project_response(project, files)


@app.delete("/api/product-research/projects/{project_id}")
async def delete_product_research_project(project_id: str, request: Request) -> dict:
    user = await _require_supabase_user(request)
    await runtime.supabase().delete_research_project(user_id=user.id, project_id=project_id)
    return {"ok": True}


@app.post("/api/schedule")
def schedule_clips(request: ScheduleRequest) -> dict:
    start_at = request.start_at or datetime.now(timezone.utc) + timedelta(hours=1)
    scheduled: list[dict] = []
    client = runtime.postbridge()
    try:
        for index, video_id in enumerate(request.video_ids):
            scheduled_at = start_at + timedelta(minutes=request.interval_minutes * index)
            with runtime.session_factory() as session:
                video = session.get(ClipVideo, video_id)
                if video is None:
                    raise HTTPException(status_code=404, detail=f"clip not found: {video_id}")
                if video.status != VideoStatus.COMPLETED or not video.output_path:
                    raise HTTPException(status_code=400, detail=f"clip is not ready: {video_id}")
                output_path = Path(video.output_path)
                if not output_path.is_absolute():
                    output_path = Path.cwd() / output_path

            try:
                media_id = client.upload_media_file(output_path)
                post = client.create_post(
                    caption=request.caption,
                    media_id=media_id,
                    social_account_ids=request.social_account_ids,
                    scheduled_at=scheduled_at,
                )
                local_status = ScheduledPostStatus.SCHEDULED
                error = None
                postbridge_post_id = post.get("id")
            except PostBridgeError as exc:
                media_id = None
                postbridge_post_id = None
                local_status = ScheduledPostStatus.FAILED
                error = str(exc)

            with session_scope(runtime.session_factory) as session:
                local_post = ScheduledPost(
                    video_id=video_id,
                    postbridge_post_id=postbridge_post_id,
                    postbridge_media_id=media_id,
                    caption=request.caption,
                    scheduled_at=scheduled_at,
                    social_accounts_json=json.dumps(request.social_account_ids),
                    status=local_status,
                    error=error,
                )
                session.add(local_post)
                session.flush()
                scheduled.append(scheduled_post_to_dict(local_post))

            # PostBridge keys allow 10 req/sec; scheduling does at least 2 requests per clip.
            time.sleep(0.25)
    finally:
        client.close()
    return {"data": scheduled}


def main() -> None:
    uvicorn.run("clip_creator.server:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    main()

