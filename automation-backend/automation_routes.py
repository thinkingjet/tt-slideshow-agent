from datetime import datetime, timedelta
import json
import mimetypes
import os
import random
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client

from celery_worker import celery_app, redis_client

router = APIRouter(prefix="/automation", tags=["automation"])

PERPLEXITY_AGENT_URL = "https://api.perplexity.ai/v1/agent"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
AUTOMATION_SLIDESHOW_COUNT = 7
POSTBRIDGE_BASE_URL = os.getenv("POSTBRIDGE_API_BASE_URL", "https://api.post-bridge.com/v1").rstrip("/")


class ProductSlideshowRequest(BaseModel):
    source: str = Field(pattern="^(new|research|product)$")
    product_name: Optional[str] = None
    product_url: Optional[str] = None
    audience: Optional[str] = None
    product_context: Optional[str] = None
    research_project_id: Optional[str] = None
    product_id: Optional[str] = None
    image_collection_id: Optional[str] = None
    selected_hook: Optional[str] = None
    deduct_credits: bool = True


class ProductSlideshowStatusResponse(BaseModel):
    generated_slideshow: Dict[str, Any]


class ProductSlideshowBatchStatusResponse(BaseModel):
    batch: Dict[str, Any]
    items: List[Dict[str, Any]]


class TikTokPostBatchRequest(BaseModel):
    social_account_ids: List[Any]
    caption: Optional[str] = None
    scheduled_start_at: Optional[str] = None
    schedule_frequency: str = Field(default="daily", pattern="^(daily|interval)$")
    interval_minutes: int = Field(default=1440, ge=0, le=1440)


def get_supabase():
    supabase_url = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        raise HTTPException(status_code=500, detail="Supabase environment is not configured.")
    return create_client(supabase_url, service_key)


def get_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authorization bearer token is required.")
    return authorization.split(" ", 1)[1].strip()


def get_user_id(authorization: Optional[str]) -> str:
    token = get_bearer_token(authorization)
    supabase = get_supabase()
    try:
        response = supabase.auth.get_user(token)
        user = getattr(response, "user", None)
        user_id = getattr(user, "id", None)
        if user_id:
            return user_id
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Invalid Supabase token: {exc}")
    raise HTTPException(status_code=401, detail="Invalid Supabase token.")


def get_postbridge_api_key() -> str:
    api_key = os.getenv("POSTBRIDGE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="POSTBRIDGE_API_KEY is not configured on the automation backend.")
    return api_key


def postbridge_request(method: str, path: str, **kwargs: Any) -> Any:
    headers = {
        "Authorization": f"Bearer {get_postbridge_api_key()}",
        **kwargs.pop("headers", {}),
    }
    url = f"{POSTBRIDGE_BASE_URL}{path}"
    for attempt in range(4):
        response = requests.request(method, url, headers=headers, timeout=120, **kwargs)
        if response.status_code != 429:
            break
        retry_after = response.headers.get("retry-after")
        time.sleep(float(retry_after) if retry_after else 2 ** attempt)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PostBridge {method} {path} failed: {response.text[:500]}")
    if not response.content:
        return None
    return response.json()


def postbridge_request_optional(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    try:
        data = postbridge_request(method, path, **kwargs)
        return {
            "data": data.get("data", []) if isinstance(data, dict) else [],
            "meta": data.get("meta", {}) if isinstance(data, dict) else {},
            "warning": None,
        }
    except HTTPException as exc:
        return {
            "data": [],
            "meta": {},
            "warning": exc.detail if isinstance(exc.detail, str) else "PostBridge request failed.",
        }


def image_url_from_generated_image(image: Dict[str, Any]) -> str:
    return str(image.get("image_url") or image.get("url") or "").strip()


def upload_postbridge_media_from_url(image_url: str, name: str) -> str:
    image_response = requests.get(image_url, timeout=120)
    if image_response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Could not download exported slide image: {image_response.status_code}")
    content = image_response.content
    mime_type = image_response.headers.get("content-type", "").split(";")[0].strip()
    if not mime_type or mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(name)[0] or "image/png"

    upload = postbridge_request(
        "POST",
        "/media/create-upload-url",
        headers={"Content-Type": "application/json"},
        json={"name": name, "mime_type": mime_type, "size_bytes": len(content)},
    )
    media_id = upload.get("media_id")
    upload_url = upload.get("upload_url")
    if not media_id or not upload_url:
        raise HTTPException(status_code=502, detail="PostBridge did not return a media upload URL.")
    upload_response = requests.put(upload_url, headers={"Content-Type": mime_type}, data=content, timeout=300)
    if upload_response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"PostBridge media upload failed: {upload_response.text[:500]}")
    return media_id


def create_postbridge_post(caption: str, media_ids: List[str], social_account_ids: List[Any], scheduled_at: Optional[datetime]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "caption": caption,
        "media": media_ids,
        "social_accounts": social_account_ids,
        "processing_enabled": True,
    }
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at.isoformat()
    return postbridge_request("POST", "/posts", headers={"Content-Type": "application/json"}, json=payload)


def row_data(response: Any) -> List[Dict[str, Any]]:
    data = getattr(response, "data", None)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return [data]


def one_row(response: Any) -> Optional[Dict[str, Any]]:
    rows = row_data(response)
    return rows[0] if rows else None


def clean_strings(items: Any, limit: int = 10) -> List[str]:
    if not isinstance(items, list):
        return []
    cleaned = []
    seen = set()
    for item in items:
        text = str(item or "").strip()
        key = text.lower()
        if text and key not in seen:
            cleaned.append(text)
            seen.add(key)
        if len(cleaned) >= limit:
            break
    return cleaned


def fallback_plan(product_name: str, product_context: str = "") -> Dict[str, Any]:
    product_name = product_name or "Product"
    return {
        "product": {
            "name": product_name,
            "summary": product_context or f"{product_name} helps customers solve a specific, annoying problem.",
            "targetCustomers": ["busy online buyers", "TikTok shoppers", "people comparing product options"],
            "buyingTriggers": ["wants a faster solution", "feels stuck with the current option", "needs a clear next step"],
            "objections": ["not sure it works", "worried it is complicated", "does not want to waste money"],
            "positioning": f"{product_name} is the obvious next step after the viewer understands the problem.",
        },
        "marketInsights": [
            "Specific pain points outperform broad product claims.",
            "Social proof and concrete use cases make product content feel less like an ad.",
            "Short hooks with a clear tension point help stop scrolling.",
            "A low-friction CTA works best after the slideshow teaches something useful.",
        ],
        "contentAngles": [
            {
                "title": "Problem-aware buyer",
                "insight": "Lead with the pain the buyer already feels.",
                "whyItWorks": "It makes the viewer feel understood before the product appears.",
                "hookIdeas": [
                    f"before you buy {product_name}, watch this",
                    f"the real reason {product_name} is getting attention",
                    f"if u struggle with this, {product_name} might help",
                ],
                "talkingPoints": ["name the painful problem", "explain why it happens", "make the product the next step"],
                "proofPoints": ["clear product summary", "specific customer trigger"],
                "cta": "check the link in bio",
            }
        ],
        "hookBank": [
            f"before you buy {product_name}, watch this",
            f"3 things nobody tells you about {product_name}",
            f"the real reason {product_name} is getting attention",
            f"if u struggle with this, {product_name} might help",
            f"stop buying alternatives until you see {product_name}",
        ],
        "ctas": ["check the link in bio", "grab it in bio", "tap the link in bio"],
        "researchNotes": ["Fallback plan generated because live research was unavailable."],
        "sourcesUsed": [],
    }


def normalize_plan(plan: Any, product_name: str, product_context: str = "") -> Dict[str, Any]:
    if not isinstance(plan, dict):
        return fallback_plan(product_name, product_context)
    fallback = fallback_plan(product_name, product_context)
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    plan["product"] = {
        **fallback["product"],
        **{key: value for key, value in product.items() if value},
    }
    plan["hookBank"] = clean_strings(plan.get("hookBank"), 12) or fallback["hookBank"]
    plan["contentAngles"] = plan.get("contentAngles") if isinstance(plan.get("contentAngles"), list) and plan.get("contentAngles") else fallback["contentAngles"]
    plan["marketInsights"] = clean_strings(plan.get("marketInsights"), 8) or fallback["marketInsights"]
    plan["ctas"] = clean_strings(plan.get("ctas"), 6) or fallback["ctas"]
    plan["researchNotes"] = clean_strings(plan.get("researchNotes"), 5) or fallback["researchNotes"]
    plan["sourcesUsed"] = clean_strings(plan.get("sourcesUsed"), 10)
    return plan


def build_research_prompt(product_name: str, product_url: str = "", audience: str = "", product_context: str = "") -> str:
    return f"""
Research this product and return TikTok slideshow-ready JSON.

Product name: {product_name}
Product URL: {product_url or "Not provided"}
Audience: {audience or "Infer from product and market context"}
Seller context: {product_context or "No extra context provided"}

Return only valid JSON with these keys:
product {{ name, summary, targetCustomers, buyingTriggers, objections, positioning }},
marketInsights, contentAngles, hookBank, ctas, researchNotes, sourcesUsed.
Each content angle should include title, insight, whyItWorks, hookIdeas, talkingPoints, proofPoints, cta.
"""


def run_product_research(product_name: str, product_url: str = "", audience: str = "", product_context: str = "") -> Dict[str, Any]:
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        return normalize_plan({}, product_name, product_context)

    payload = {
        "model": os.getenv("PERPLEXITY_AGENT_MODEL", "perplexity/sonar"),
        "input": [{
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": build_research_prompt(product_name, product_url, audience, product_context)}],
        }],
        "instructions": "You are a senior TikTok growth strategist and product researcher. Return only JSON.",
        "tools": [{"type": "web_search", "max_tokens": 8000, "max_tokens_per_page": 2000}],
        "max_steps": 4,
        "max_output_tokens": 5000,
    }

    response = requests.post(
        PERPLEXITY_AGENT_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    body = response.json()
    raw = body.get("output_text") or body.get("output") or ""
    if isinstance(raw, list):
        raw = json.dumps(raw)
    raw_text = str(raw).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`").replace("json\n", "", 1).strip()
    try:
        parsed = json.loads(raw_text)
    except Exception:
        parsed = {}
    return normalize_plan(parsed, product_name, product_context)


def save_research_project(user_id: str, request: ProductSlideshowRequest, plan: Dict[str, Any]) -> Dict[str, Any]:
    supabase = get_supabase()
    response = supabase.table("product_research_projects").insert({
        "user_id": user_id,
        "product_name": request.product_name,
        "product_url": request.product_url or None,
        "audience": request.audience or None,
        "product_context": request.product_context or None,
        "plan": plan,
        "sources": [{"url": source} for source in clean_strings(plan.get("sourcesUsed"), 10)],
        "uploads": [],
        "model": os.getenv("PERPLEXITY_AGENT_MODEL", "perplexity/sonar"),
        "usage": None,
    }).execute()
    saved = one_row(response)
    if not saved:
        raise HTTPException(status_code=502, detail="Failed to save product research project.")
    saved["data"] = saved.get("plan", plan)
    return saved


def load_research_project(user_id: str, project_id: str) -> Dict[str, Any]:
    response = get_supabase().table("product_research_projects").select("*").eq("id", project_id).eq("user_id", user_id).limit(1).execute()
    project = one_row(response)
    if not project:
        raise HTTPException(status_code=404, detail="Product research project not found.")
    project["data"] = project.get("plan", {})
    return project


def load_saved_product(user_id: str, product_id: str) -> Dict[str, Any]:
    response = get_supabase().table("user_products").select("*").eq("id", product_id).eq("user_id", user_id).limit(1).execute()
    product = one_row(response)
    if not product:
        raise HTTPException(status_code=404, detail="Saved product not found.")
    return product


def ranked_hooks(plan: Dict[str, Any]) -> List[str]:
    hooks = clean_strings(plan.get("hookBank"), 20)
    angle_hooks: List[str] = []
    for angle in plan.get("contentAngles") or []:
        if isinstance(angle, dict):
            angle_hooks.extend(clean_strings(angle.get("hookIdeas"), 5))
    return clean_strings([*hooks, *angle_hooks], 25)


def select_batch_hooks(plan: Dict[str, Any], preferred_hook: Optional[str] = None) -> List[str]:
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    product_name = product.get("name") or "this product"
    hooks = ranked_hooks(plan)
    if preferred_hook:
        hooks = clean_strings([preferred_hook, *hooks], 30)
    hooks = clean_strings([
        *hooks,
        f"before you buy {product_name}, watch this",
        f"3 things nobody tells you about {product_name}",
        f"the real reason {product_name} is getting attention",
        f"if u struggle with this, {product_name} might help",
        f"stop buying alternatives until you see {product_name}",
        f"this is why people are switching to {product_name}",
        f"{product_name} explained in 20 seconds",
    ], 30)
    if not hooks:
        raise HTTPException(status_code=502, detail="No usable hooks were generated.")
    while len(hooks) < AUTOMATION_SLIDESHOW_COUNT:
        hooks.append(f"{hooks[len(hooks) % len(hooks)]} - angle {len(hooks) + 1}")
    return hooks[:AUTOMATION_SLIDESHOW_COUNT]


def choose_collection(user_id: str, preferred_collection_id: Optional[str] = None) -> Dict[str, Any]:
    supabase = get_supabase()
    if preferred_collection_id:
        collections = row_data(supabase.table("slideshow_collections").select("*").eq("id", preferred_collection_id).execute())
    else:
        community = row_data(supabase.table("slideshow_collections").select("*").eq("is_community", True).order("name").execute())
        mine = row_data(supabase.table("slideshow_collections").select("*").eq("user_id", user_id).eq("is_community", False).order("created_at", desc=True).execute())
        collections = [*community, *mine]

    for collection in collections:
        if collection.get("is_virtual") or collection.get("name") in ["Uncollected", "Hooks"]:
            continue
        images = row_data(
            supabase.table("slideshow_images")
            .select("id,image_url,name")
            .eq("collection_id", collection["id"])
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        if images:
            return {**collection, "images": images, "image_count": len(images)}
    raise HTTPException(status_code=400, detail="No non-empty slideshow image collection is available.")


def research_context(plan: Dict[str, Any]) -> str:
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    angle = (plan.get("contentAngles") or [{}])[0] if isinstance(plan.get("contentAngles"), list) else {}
    lines = [
        f"Product summary: {product.get('summary', '')}",
        f"Positioning: {product.get('positioning', '')}",
        f"Target customers: {', '.join(clean_strings(product.get('targetCustomers'), 5))}",
        f"Buying triggers: {', '.join(clean_strings(product.get('buyingTriggers'), 5))}",
        f"Objections: {', '.join(clean_strings(product.get('objections'), 5))}",
        f"Market insights: {'; '.join(clean_strings(plan.get('marketInsights'), 5))}",
        f"Primary angle: {angle.get('title', '') if isinstance(angle, dict) else ''}",
        f"Talking points: {', '.join(clean_strings(angle.get('talkingPoints') if isinstance(angle, dict) else [], 5))}",
        f"Proof points: {', '.join(clean_strings(angle.get('proofPoints') if isinstance(angle, dict) else [], 4))}",
        f"CTA options: {', '.join(clean_strings(plan.get('ctas'), 5))}",
    ]
    return "\n".join(line for line in lines if not line.endswith(": "))


def call_openai_json(prompt: str, max_tokens: int = 12000) -> Dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured.")
    response = requests.post(
        OPENAI_CHAT_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": os.getenv("OPENAI_SLIDESHOW_MODEL", "gpt-4.1-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 1,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def split_text_chunks(text: str, max_chars: int, max_chunks: int) -> List[str]:
    text = " ".join(str(text or "").replace("\u2013", "-").replace("\u2014", "-").split())
    if not text:
        return []

    sentences = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            sentences.append(current)
        current = word
    if current:
        sentences.append(current)

    chunks = sentences[:max_chunks]
    if len(sentences) > max_chunks and chunks:
        chunks[-1] = chunks[-1].rstrip(".") + "..."
    return chunks


def layout_text_elements(slide_index: int, raw_elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    max_chars = 82 if slide_index == 0 else 105
    max_chunks = 1 if slide_index == 0 else 3
    chunks: List[str] = []

    for element in raw_elements:
        if len(chunks) >= max_chunks:
            break
        chunks.extend(split_text_chunks(element.get("text", ""), max_chars, max_chunks - len(chunks)))

    if not chunks:
        chunks = [" "]

    count = len(chunks)
    if slide_index == 0:
        slots = [(150, 130, 25)]
    elif count == 1:
        slots = [(150, 150, 20)]
    elif count == 2:
        slots = [(82, 115, 18), (242, 115, 18)]
    else:
        slots = [(46, 96, 17), (184, 96, 17), (322, 96, 17)]

    elements = []
    for text_index, text in enumerate(chunks):
        y, height, font_size = slots[min(text_index, len(slots) - 1)]
        elements.append({
            "id": f"text_{slide_index + 1}_{text_index + 1}",
            "text": text,
            "position": {"x": 20, "y": y},
            "font_size": font_size,
            "width": 344,
            "height": height,
            "font_color": "#FFFFFF",
            "text_align": "center",
        })
    return elements


def complete_slides_from_ai(slides: List[Dict[str, Any]], collection: Dict[str, Any]) -> List[Dict[str, Any]]:
    images = list(collection["images"])
    random.shuffle(images)
    completed_slides = []
    for index, slide in enumerate(slides[:6]):
        text_elements = slide.get("text_elements") or slide.get("textElements") or []
        normalized = layout_text_elements(
            index,
            [element for element in text_elements if isinstance(element, dict)],
        )
        completed_slides.append({
            "id": f"slide_{index + 1}",
            "order": index + 1,
            "duration_s": int(slide.get("duration_s", 3 if index == 0 else 5)),
            "imageUrl": images[index % len(images)]["image_url"],
            "text_elements": normalized,
        })
    return completed_slides


def generate_slideshow_data(selected_hook: str, plan: Dict[str, Any], collection: Dict[str, Any], research_project_id: str, variant_number: int = 1) -> Dict[str, Any]:
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    prompt = f"""
You are creating a TikTok 4:5 hook + product slideshow.
Return one valid JSON object with exactly this shape:
{{ "slides": [{{ "order": 1, "duration_s": 3, "textElements": [{{ "text": "...", "position": {{"x": 20, "y": 160}}, "width": 344, "height": 100, "font_size": 25, "font_color": "#FFFFFF", "text_align": "center" }}] }}] }}

HOOK FOR SLIDE 1, USE EXACTLY:
"{selected_hook}"

VARIANT:
This is slideshow variant {variant_number} of {AUTOMATION_SLIDESHOW_COUNT}. Make the angle and examples feel distinct from the other variants while staying grounded in the same research.

PRODUCT RESEARCH:
{research_context(plan)}

Rules:
- exactly 6 slides
- slide 1 is only the exact hook
- slides 2-5 teach around the hook topic and product research
- slide 6 makes the product the natural next step and ends with a link in bio CTA
- lowercase, punchy, conversational tiktok style
- use x 20 and width 344 for all text
- each slide may have 1-3 text elements
- each text element must be 1 short sentence, max 105 characters
- never put more than 3 text elements on one slide
- keep total slide copy short enough to read at a glance
- leave clear vertical space between text elements; no overlapping boxes
- never use em dashes
"""
    ai_json = call_openai_json(prompt)
    slides = ai_json.get("slides") if isinstance(ai_json, dict) else None
    if not isinstance(slides, list) or not slides:
        raise HTTPException(status_code=502, detail="OpenAI returned invalid slideshow JSON.")

    completed_slides = complete_slides_from_ai(slides, collection)

    return {
        "prompt": f"Automated hook+product slideshow with hook: {selected_hook}",
        "hookText": selected_hook,
        "slideshowType": "hook_product_slideshow",
        "automationVariant": variant_number,
        "productResearchProjectId": research_project_id,
        "selectedResearchHook": selected_hook,
        "productName": product.get("name"),
        "productDescription": product.get("summary"),
        "imageCollectionId": collection["id"],
        "aspectRatio": "4:5",
        "slides": completed_slides,
    }


def generate_batch_slideshow_data(hooks: List[str], plan: Dict[str, Any], collection: Dict[str, Any], research_project_id: str) -> List[Dict[str, Any]]:
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    hook_lines = "\n".join([f"{index}. {hook}" for index, hook in enumerate(hooks, start=1)])
    prompt = f"""
You are creating {len(hooks)} distinct TikTok 4:5 hook + product slideshows in one response.
Return one valid JSON object with exactly this shape:
{{ "slideshows": [{{ "hook": "...", "slides": [{{ "order": 1, "duration_s": 3, "textElements": [{{ "text": "...", "position": {{"x": 20, "y": 160}}, "width": 344, "height": 100, "font_size": 25, "font_color": "#FFFFFF", "text_align": "center" }}] }}] }}] }}

HOOKS:
{hook_lines}

PRODUCT RESEARCH:
{research_context(plan)}

Rules:
- return exactly {len(hooks)} slideshows, in the same order as the hooks above
- every slideshow has exactly 6 slides
- slide 1 uses its assigned hook exactly and nothing else
- slides 2-5 teach around that hook topic and product research
- slide 6 makes the product the natural next step and ends with a link in bio CTA
- make each slideshow feel meaningfully different from the others
- lowercase, punchy, conversational tiktok style
- use x 20 and width 344 for all text
- each slide may have 1-3 text elements
- each text element must be 1 short sentence, max 105 characters
- never put more than 3 text elements on one slide
- keep total slide copy short enough to read at a glance
- leave clear vertical space between text elements; no overlapping boxes
- never use em dashes
"""
    ai_json = call_openai_json(prompt, max_tokens=30000)
    raw_slideshows = ai_json.get("slideshows") if isinstance(ai_json, dict) else None
    if not isinstance(raw_slideshows, list) or len(raw_slideshows) < len(hooks):
        raise HTTPException(status_code=502, detail="OpenAI returned invalid batch slideshow JSON.")

    batch = []
    for index, hook in enumerate(hooks, start=1):
        raw_slideshow = raw_slideshows[index - 1] if index - 1 < len(raw_slideshows) else {}
        slides = raw_slideshow.get("slides") if isinstance(raw_slideshow, dict) else None
        if not isinstance(slides, list) or not slides:
            raise HTTPException(status_code=502, detail=f"OpenAI returned invalid slideshow JSON for variant {index}.")
        completed_slides = complete_slides_from_ai(slides, collection)
        batch.append({
            "prompt": f"Automated hook+product slideshow with hook: {hook}",
            "hookText": hook,
            "slideshowType": "hook_product_slideshow",
            "automationVariant": index,
            "productResearchProjectId": research_project_id,
            "selectedResearchHook": hook,
            "productName": product.get("name"),
            "productDescription": product.get("summary"),
            "imageCollectionId": collection["id"],
            "aspectRatio": "4:5",
            "slides": completed_slides,
        })
    return batch


def deduct_credits(user_id: str, credit_type: str, amount: int, transaction_type: str, description: str) -> bool:
    try:
        get_supabase().rpc("deduct_credits", {
            "p_user_id": user_id,
            "p_amount": amount,
            "p_credit_type": credit_type,
            "p_transaction_type": transaction_type,
            "p_description": description,
        }).execute()
        return True
    except Exception as exc:
        print(f"Credit deduction failed for {credit_type}: {exc}")
        return False


def refund_credits(user_id: str, credit_type: str, amount: int, transaction_type: str, reason: str):
    try:
        get_supabase().rpc("award_credits", {
            "p_user_id": user_id,
            "p_amount": amount,
            "p_credit_type": credit_type,
            "p_transaction_type": "refund",
            "p_description": f"Refund for {transaction_type}: {reason}",
        }).execute()
    except Exception as exc:
        print(f"Credit refund failed for {credit_type}: {exc}")


def try_insert_catalog_batch(batch_id: str, user_id: str, research_project: Dict[str, Any], collection: Dict[str, Any], hooks: List[str], metadata: Dict[str, Any]) -> Optional[str]:
    try:
        batch = one_row(get_supabase().table("automation_slideshow_batches").insert({
            "id": batch_id,
            "user_id": user_id,
            "product_research_project_id": research_project.get("id"),
            "product_name": research_project.get("product_name") or (research_project.get("data") or {}).get("product", {}).get("name"),
            "status": "processing",
            "selected_collection_id": collection.get("id"),
            "selected_collection_name": collection.get("name"),
            "ranked_hooks": hooks,
            "metadata": metadata,
        }).execute())
        return batch.get("id") if batch else None
    except Exception as exc:
        print(f"Automation batch catalog insert skipped: {exc}")
        return None


def try_insert_catalog_item(user_id: str, batch_id: Optional[str], position: int, hook: str, slideshow_id: str, generated: Dict[str, Any], metadata: Dict[str, Any]):
    if not batch_id:
        return
    try:
        get_supabase().table("automation_slideshow_items").insert({
            "id": metadata.get("fallbackItemId"),
            "batch_id": batch_id,
            "user_id": user_id,
            "position": position,
            "hook": hook,
            "slideshow_id": slideshow_id,
            "generated_slideshow_id": generated.get("id"),
            "job_id": generated.get("job_id"),
            "status": generated.get("status", "processing"),
            "metadata": metadata,
        }).execute()
    except Exception as exc:
        print(f"Automation item catalog insert skipped: {exc}")


def get_generated_rows_for_batch(user_id: str, batch_id: str) -> List[Dict[str, Any]]:
    supabase = get_supabase()
    rows = row_data(
        supabase
        .table("user_generated_slideshows")
        .select("*")
        .eq("user_id", user_id)
        .contains("processing_metadata", {"automationBatchId": batch_id})
        .order("created_at")
        .execute()
    )
    if rows:
        return rows

    try:
        catalog_batch = one_row(
            supabase.table("automation_slideshow_batches")
            .select("id")
            .eq("user_id", user_id)
            .contains("metadata", {"fallbackBatchId": batch_id})
            .limit(1)
            .execute()
        )
        catalog_batch_id = catalog_batch.get("id") if catalog_batch else None
        if catalog_batch_id and catalog_batch_id != batch_id:
            return row_data(
                supabase
                .table("user_generated_slideshows")
                .select("*")
                .eq("user_id", user_id)
                .contains("processing_metadata", {"automationBatchId": catalog_batch_id})
                .order("created_at")
                .execute()
            )
    except Exception as exc:
        print(f"Automation batch fallback lookup skipped: {exc}")
    return rows


def update_catalog_from_generated(user_id: str, batch_id: str, generated_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    completed = len([row for row in generated_rows if row.get("status") == "completed"])
    failed = len([row for row in generated_rows if row.get("status") == "failed"])
    total = len(generated_rows)
    status = "completed" if total and completed == total else "failed" if total and failed == total else "partial" if failed else "processing"

    for row in generated_rows:
        metadata = row.get("processing_metadata") or {}
        item_id = metadata.get("automationItemId")
        if item_id:
            try:
                get_supabase().table("automation_slideshow_items").update({
                    "status": row.get("status"),
                    "generated_images": row.get("generated_images") or [],
                    "error_message": row.get("error_message"),
                }).eq("id", item_id).eq("user_id", user_id).execute()
            except Exception as exc:
                print(f"Automation item catalog update skipped: {exc}")

    try:
        get_supabase().table("automation_slideshow_batches").update({
            "status": status,
            "metadata": {
                "total": total,
                "completed": completed,
                "failed": failed,
                "lastCheckedAt": datetime.utcnow().isoformat(),
            },
        }).eq("id", batch_id).eq("user_id", user_id).execute()
    except Exception as exc:
        print(f"Automation batch catalog update skipped: {exc}")

    return {"id": batch_id, "status": status, "total": total, "completed": completed, "failed": failed}


def parse_schedule_start(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


def default_tiktok_caption(row: Dict[str, Any], position: int) -> str:
    metadata = row.get("processing_metadata") or {}
    hook = metadata.get("automationHook") or row.get("title") or "new product slideshow"
    return f"{hook}\n\nlink in bio"


def update_generated_postbridge_metadata(user_id: str, generated_id: str, postbridge_data: Dict[str, Any]):
    supabase = get_supabase()
    row = one_row(
        supabase.table("user_generated_slideshows")
        .select("processing_metadata")
        .eq("id", generated_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    metadata = row.get("processing_metadata") if row else {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["postbridge"] = postbridge_data
    supabase.table("user_generated_slideshows").update({"processing_metadata": metadata}).eq("id", generated_id).eq("user_id", user_id).execute()


def post_generated_slideshow_to_tiktok(user_id: str, row: Dict[str, Any], social_account_ids: List[Any], caption: Optional[str], scheduled_at: Optional[datetime], position: int) -> Dict[str, Any]:
    generated_images = row.get("generated_images") or []
    image_urls = [image_url_from_generated_image(image) for image in generated_images if isinstance(image, dict)]
    image_urls = [url for url in image_urls if url]
    if not image_urls:
        raise HTTPException(status_code=400, detail=f"Generated slideshow {row.get('id')} has no exported images to post.")

    media_ids = []
    for image_index, image_url in enumerate(image_urls, start=1):
        media_ids.append(upload_postbridge_media_from_url(image_url, f"slideshow-{row.get('id')}-slide-{image_index}.png"))
        time.sleep(0.15)

    final_caption = caption or default_tiktok_caption(row, position)
    post = create_postbridge_post(final_caption, media_ids, social_account_ids, scheduled_at)
    postbridge_data = {
        "status": "scheduled" if scheduled_at else "created",
        "postId": post.get("id"),
        "mediaIds": media_ids,
        "socialAccountIds": social_account_ids,
        "caption": final_caption,
        "scheduledAt": scheduled_at.isoformat() if scheduled_at else None,
        "createdAt": datetime.utcnow().isoformat(),
    }
    update_generated_postbridge_metadata(user_id, row["id"], postbridge_data)
    return {
        "generated_slideshow_id": row["id"],
        "position": position,
        "postbridge_post_id": post.get("id"),
        "media_ids": media_ids,
        "scheduled_at": postbridge_data["scheduledAt"],
        "post": post,
    }


def insert_failed_batch_rows(user_id: str, batch_id: str, error_message: str):
    supabase = get_supabase()
    for index in range(1, AUTOMATION_SLIDESHOW_COUNT + 1):
        try:
            supabase.table("user_generated_slideshows").insert({
                "user_id": user_id,
                "title": f"Automated Slideshow {index}",
                "description": "Automation failed before export enqueue.",
                "status": "failed",
                "job_id": f"automation_failed_{int(time.time() * 1000)}_{index}",
                "total_slides": 0,
                "completed_slides": 0,
                "error_message": error_message,
                "processing_metadata": {
                    "source": "automation-backend",
                    "automationBatchId": batch_id,
                    "automationPosition": index,
                    "automationHook": "",
                    "failedBeforeEnqueue": True,
                },
            }).execute()
        except Exception as exc:
            print(f"Failed to insert automation failure row: {exc}")


def enqueue_slideshow(user_id: str, slideshow_id: str, slideshow_data: Dict[str, Any], title: str, deduct_video_credit: bool, batch_id: Optional[str] = None, item_id: Optional[str] = None, position: Optional[int] = None, hook: Optional[str] = None) -> Dict[str, Any]:
    if deduct_video_credit and not deduct_credits(user_id, "video_generation", 1, "slideshow_generation", "Automated slideshow export"):
        raise HTTPException(status_code=402, detail="Not enough video generation credits.")

    job_id = f"slideshow_{int(time.time() * 1000)}_{uuid.uuid4().hex[:9]}"
    supabase = get_supabase()
    try:
        generated = one_row(supabase.table("user_generated_slideshows").insert({
            "user_id": user_id,
            "original_slideshow_id": slideshow_id,
            "title": title,
            "description": slideshow_data.get("prompt", ""),
            "status": "processing",
            "job_id": job_id,
            "total_slides": len(slideshow_data.get("slides", [])),
            "completed_slides": 0,
            "processing_metadata": {
                "source": "automation-backend",
                "automationBatchId": batch_id,
                "automationItemId": item_id,
                "automationPosition": position,
                "automationHook": hook,
                "original_slideshow_data": slideshow_data,
                "processing_started_at": datetime.utcnow().isoformat(),
            },
        }).execute())
        if not generated:
            raise RuntimeError("Insert returned no generated slideshow row.")

        timestamp = int(job_id.split("_")[1])
        priority = -timestamp
        celery_app.send_task(
            "process_slideshow_async",
            args=[{
                "jobId": job_id,
                "userId": user_id,
                "slideshowId": slideshow_id,
                "slideshowData": slideshow_data,
                "title": title,
            }],
            task_id=job_id,
            priority=priority,
            queue="video_processing",
        )
        return {
            "jobId": job_id,
            "generatedSlideshowId": generated["id"],
            "queuePosition": redis_client.llen("video_processing") + 1,
        }
    except Exception:
        if deduct_video_credit:
            refund_credits(user_id, "video_generation", 1, "slideshow_generation", "Automation enqueue failed")
        raise


def resolve_research_for_request(user_id: str, request: ProductSlideshowRequest) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if request.source == "research":
        if not request.research_project_id:
            raise HTTPException(status_code=400, detail="research_project_id is required.")
        research_project = load_research_project(user_id, request.research_project_id)
        plan = normalize_plan(research_project.get("plan"), research_project.get("product_name") or "Product", research_project.get("product_context") or "")
    elif request.source == "product":
        if not request.product_id:
            raise HTTPException(status_code=400, detail="product_id is required.")
        product = load_saved_product(user_id, request.product_id)
        plan = run_product_research(product.get("name") or "Product", "", "", product.get("description") or "")
        research_project = save_research_project(user_id, ProductSlideshowRequest(
            source="new",
            product_name=product.get("name"),
            product_context=product.get("description"),
        ), plan)
    else:
        if not request.product_name:
            raise HTTPException(status_code=400, detail="product_name is required.")
        plan = run_product_research(request.product_name, request.product_url or "", request.audience or "", request.product_context or "")
        research_project = save_research_project(user_id, request, plan)
    return research_project, plan


def run_batch_workflow(user_id: str, request_data: Dict[str, Any], batch_id: str):
    request = ProductSlideshowRequest(**request_data)
    try:
        research_project, plan = resolve_research_for_request(user_id, request)
        hooks = select_batch_hooks(plan, request.selected_hook)
        collection = choose_collection(user_id, request.image_collection_id)
        catalog_batch_id = try_insert_catalog_batch(
            batch_id,
            user_id,
            research_project,
            collection,
            hooks,
            {"requestedCount": AUTOMATION_SLIDESHOW_COUNT, "fallbackBatchId": batch_id},
        )

        batch_slideshow_data = generate_batch_slideshow_data(hooks, plan, collection, research_project["id"])
        for index, (hook, slideshow_data) in enumerate(zip(hooks, batch_slideshow_data), start=1):
            if request.deduct_credits and not deduct_credits(user_id, "slideshow_draft", 1, "slideshow_draft_creation", f"Automated slideshow draft {index}/7"):
                raise RuntimeError(f"Not enough slideshow draft credits for slideshow {index}.")

            title = f"{slideshow_data.get('productName') or research_project.get('product_name') or 'Product'} - Automated Slideshow {index}"
            slideshow = one_row(get_supabase().table("slideshows").insert({
                "user_id": user_id,
                "status": "draft",
                "slideshow_data": slideshow_data,
            }).execute())
            if not slideshow:
                raise RuntimeError(f"Failed to create slideshow draft {index}.")

            item_id = str(uuid.uuid4())
            export_job = enqueue_slideshow(
                user_id,
                slideshow["id"],
                slideshow_data,
                title,
                request.deduct_credits,
                batch_id,
                item_id,
                index,
                hook,
            )
            generated_placeholder = {
                "id": export_job["generatedSlideshowId"],
                "job_id": export_job["jobId"],
                "status": "processing",
            }
            try_insert_catalog_item(user_id, catalog_batch_id, index, hook, slideshow["id"], generated_placeholder, {"fallbackItemId": item_id})
    except Exception as exc:
        print(f"Automation batch workflow failed: {exc}")
        insert_failed_batch_rows(user_id, batch_id, str(exc))


@router.get("/products/context")
async def products_context(authorization: Optional[str] = Header(default=None)):
    user_id = get_user_id(authorization)
    supabase = get_supabase()
    research_projects = row_data(supabase.table("product_research_projects").select("id,product_name,product_url,audience,plan,created_at,updated_at").eq("user_id", user_id).order("created_at", desc=True).execute())
    products = row_data(supabase.table("user_products").select("*").eq("user_id", user_id).order("created_at", desc=True).execute())
    return {"researchProjects": research_projects, "products": products}


@router.get("/postbridge/tiktok-accounts")
async def postbridge_tiktok_accounts(authorization: Optional[str] = Header(default=None)):
    get_user_id(authorization)
    response = postbridge_request(
        "GET",
        "/social-accounts",
        params=[("offset", 0), ("limit", 100), ("platform", "tiktok")],
    )
    return {"data": response.get("data", []) if isinstance(response, dict) else []}


@router.get("/postbridge/posts")
async def postbridge_posts(authorization: Optional[str] = Header(default=None)):
    get_user_id(authorization)
    response = postbridge_request(
        "GET",
        "/posts",
        params=[("offset", 0), ("limit", 100)],
    )
    return {
        "data": response.get("data", []) if isinstance(response, dict) else [],
        "meta": response.get("meta", {}) if isinstance(response, dict) else {},
    }


@router.get("/postbridge/post-results")
async def postbridge_post_results(authorization: Optional[str] = Header(default=None)):
    get_user_id(authorization)
    return postbridge_request_optional(
        "GET",
        "/post-results",
        params=[("offset", 0), ("limit", 100), ("platform", "tiktok")],
    )


@router.post("/product-slideshow")
async def product_slideshow(request: ProductSlideshowRequest, authorization: Optional[str] = Header(default=None)):
    user_id = get_user_id(authorization)
    batch_id = str(uuid.uuid4())
    request_data = request.model_dump()
    threading.Thread(target=run_batch_workflow, args=(user_id, request_data, batch_id), daemon=True).start()

    product_name = request.product_name or "Product"
    if request.source == "product" and request.product_id:
        try:
            product = load_saved_product(user_id, request.product_id)
            product_name = product.get("name") or product_name
        except Exception:
            pass
    if request.source == "research" and request.research_project_id:
        try:
            research_project = load_research_project(user_id, request.research_project_id)
            product_name = research_project.get("product_name") or product_name
        except Exception:
            pass

    return {
        "success": True,
        "batchId": batch_id,
        "slideshowCount": AUTOMATION_SLIDESHOW_COUNT,
        "researchProject": {
            "id": request.research_project_id or batch_id,
            "product_name": product_name,
            "data": {"product": {"name": product_name}},
        },
        "selectedHook": request.selected_hook or "Generating top hooks...",
        "rankedHooks": [],
        "selectedCollection": {"id": request.image_collection_id or "", "name": "Auto-picked collection"},
        "slideshows": [],
        "draft": {"id": "", "slideshow_data": {"slides": []}},
        "generatedSlideshowId": "",
        "jobId": "",
    }


@router.get("/product-slideshow/{generated_slideshow_id}", response_model=ProductSlideshowStatusResponse)
async def product_slideshow_status(generated_slideshow_id: str, authorization: Optional[str] = Header(default=None)):
    user_id = get_user_id(authorization)
    generated = one_row(
        get_supabase()
        .table("user_generated_slideshows")
        .select("*")
        .eq("id", generated_slideshow_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not generated:
        raise HTTPException(status_code=404, detail="Generated slideshow not found.")
    return {"generated_slideshow": generated}


@router.get("/product-slideshow-batches/{batch_id}", response_model=ProductSlideshowBatchStatusResponse)
async def product_slideshow_batch_status(batch_id: str, authorization: Optional[str] = Header(default=None)):
    user_id = get_user_id(authorization)
    generated_rows = get_generated_rows_for_batch(user_id, batch_id)
    if not generated_rows:
        raise HTTPException(status_code=404, detail="Automation slideshow batch not found.")
    batch = update_catalog_from_generated(user_id, batch_id, generated_rows)
    items = []
    for row in generated_rows:
        metadata = row.get("processing_metadata") or {}
        items.append({
            "position": metadata.get("automationPosition"),
            "hook": metadata.get("automationHook"),
            "generated_slideshow": row,
            "generated_slideshow_id": row.get("id"),
            "job_id": row.get("job_id"),
            "status": row.get("status"),
            "generated_images": row.get("generated_images") or [],
            "error_message": row.get("error_message"),
        })
    items.sort(key=lambda item: item.get("position") or 999)
    return {"batch": batch, "items": items}


@router.post("/product-slideshow-batches/{batch_id}/post-to-tiktok")
async def post_product_slideshow_batch_to_tiktok(batch_id: str, request: TikTokPostBatchRequest, authorization: Optional[str] = Header(default=None)):
    user_id = get_user_id(authorization)
    if not request.social_account_ids:
        raise HTTPException(status_code=400, detail="Select at least one connected TikTok account.")

    generated_rows = get_generated_rows_for_batch(user_id, batch_id)
    if not generated_rows:
        raise HTTPException(status_code=404, detail="Automation slideshow batch not found.")

    incomplete = [row.get("id") for row in generated_rows if row.get("status") != "completed"]
    if incomplete:
        raise HTTPException(status_code=400, detail="All slideshow exports must complete before posting to TikTok.")

    scheduled_start = parse_schedule_start(request.scheduled_start_at)
    posts = []
    for index, row in enumerate(generated_rows, start=1):
        metadata = row.get("processing_metadata") or {}
        existing_postbridge = metadata.get("postbridge") if isinstance(metadata, dict) else None
        if isinstance(existing_postbridge, dict) and existing_postbridge.get("postId"):
            posts.append({
                "generated_slideshow_id": row.get("id"),
                "position": metadata.get("automationPosition") or index,
                "postbridge_post_id": existing_postbridge.get("postId"),
                "skipped": True,
            })
            continue

        if scheduled_start and request.schedule_frequency == "daily":
            scheduled_at = scheduled_start + timedelta(days=index - 1)
        elif scheduled_start:
            scheduled_at = scheduled_start + timedelta(minutes=request.interval_minutes * (index - 1))
        else:
            scheduled_at = None
        posts.append(post_generated_slideshow_to_tiktok(
            user_id,
            row,
            request.social_account_ids,
            request.caption,
            scheduled_at,
            metadata.get("automationPosition") or index,
        ))
        time.sleep(0.3)

    return {"success": True, "batchId": batch_id, "posts": posts}
