from __future__ import annotations

import json
import logging
import os
import random
import re
from typing import Any

import httpx

from .settings import Settings


OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_COMMUNITY_COLLECTION_BASE_URL = "https://cdn.reelstacks.ai/community-collections/surrealism"
DEFAULT_COMMUNITY_COLLECTION_FILES = [f"{index}.jpg" for index in range(1, 301)]
DEFAULT_COMMUNITY_COLLECTION_NAME = "surrealism"
DEFAULT_FALLBACK_COLLECTION_BASE_URL = "https://cdn.reelstacks.ai/community-collections/surrealism"
DEFAULT_FALLBACK_COLLECTION_FILES = [
    "1.jpg",
    "4.jpg",
    "5.jpg",
    "7.jpg",
    "8.jpg",
    "9.jpg",
    "10.jpg",
    "11.jpg",
    "12.jpg",
    "13.jpg",
    "14.jpg",
    "15.jpg",
    "16.jpg",
    "17.jpg",
    "18.jpg",
    "19.jpg",
    "20.jpg",
    "21.jpg",
    "22.jpg",
    "23.jpg",
    "24.jpg",
    "25.jpg",
    "26.jpg",
    "27.jpg",
    "28.jpg",
    "29.jpg",
    "30.jpg",
    "31.jpg",
    "32.jpg",
    "33.jpg",
]
logger = logging.getLogger("clip_creator")


def _strip_json(text: str) -> str:
    cleaned = (text or "").strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if match:
        cleaned = match.group(1).strip()
    return cleaned


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [str(item).strip() for item in value if str(item).strip()]
    return cleaned[: max(0, int(limit))]


def _community_image_urls() -> list[str]:
    base = os.getenv("CLIP_CREATOR_COMMUNITY_COLLECTION_BASE_URL", DEFAULT_COMMUNITY_COLLECTION_BASE_URL)
    base = (base or DEFAULT_COMMUNITY_COLLECTION_BASE_URL).rstrip("/")
    raw_files = os.getenv("CLIP_CREATOR_COMMUNITY_COLLECTION_FILES", "").strip()
    if raw_files:
        files = [item.strip() for item in raw_files.split(",") if item.strip()]
    else:
        files = DEFAULT_COMMUNITY_COLLECTION_FILES
    return [f"{base}/{name}" for name in files]


def _fallback_image_urls() -> list[str]:
    return [f"{DEFAULT_FALLBACK_COLLECTION_BASE_URL}/{name}" for name in DEFAULT_FALLBACK_COLLECTION_FILES]


def _research_summary_text(project: dict[str, Any]) -> str:
    plan = project.get("plan") if isinstance(project.get("plan"), dict) else {}
    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}

    product_name = str(project.get("product_name") or product.get("name") or "").strip() or "this product"
    summary = str(product.get("summary") or "").strip()
    positioning = str(product.get("positioning") or "").strip()
    audience = str(project.get("audience") or "").strip()
    context = str(project.get("product_context") or "").strip()

    target_customers = _string_list(product.get("targetCustomers"), 5)
    buying_triggers = _string_list(product.get("buyingTriggers"), 5)
    objections = _string_list(product.get("objections"), 5)
    hooks = _string_list(plan.get("hookBank"), 8)
    captions = _string_list(plan.get("captions"), 5)
    ctas = _string_list(plan.get("ctas"), 5)
    angles = plan.get("contentAngles") if isinstance(plan.get("contentAngles"), list) else []

    angle_summaries: list[str] = []
    for angle in angles[:4]:
        if not isinstance(angle, dict):
            continue
        title = str(angle.get("title") or "").strip()
        insight = str(angle.get("insight") or "").strip()
        hook_ideas = _string_list(angle.get("hookIdeas"), 2)
        pieces = [p for p in [title, insight] if p]
        if hook_ideas:
            pieces.append("hooks: " + "; ".join(hook_ideas))
        if pieces:
            angle_summaries.append(" | ".join(pieces))

    lines = [
        f"Product name: {product_name}",
    ]
    if summary:
        lines.append(f"Summary: {summary}")
    if positioning:
        lines.append(f"Positioning: {positioning}")
    if audience:
        lines.append(f"Audience: {audience}")
    if context:
        lines.append(f"Seller context: {context}")
    if target_customers:
        lines.append("Target customers: " + "; ".join(target_customers))
    if buying_triggers:
        lines.append("Buying triggers: " + "; ".join(buying_triggers))
    if objections:
        lines.append("Objections: " + "; ".join(objections))
    if hooks:
        lines.append("Hook bank: " + " | ".join(hooks))
    if captions:
        lines.append("Captions: " + " | ".join(captions))
    if ctas:
        lines.append("CTAs: " + " | ".join(ctas))
    if angle_summaries:
        lines.append("Content angles: " + " || ".join(angle_summaries))

    return "\n".join(lines).strip()


def _build_hook_prompt(summary: str) -> str:
    return (
        "You are a TikTok hook writer for product slideshows.\n"
        "Write a single scroll-stopping hook for a slideshow about the product.\n\n"
        "Rules:\n"
        "- 8 to 12 words\n"
        "- lowercase only\n"
        "- no apostrophes\n"
        "- no emojis, no hashtags\n"
        "- no quotes around the hook\n"
        "- avoid overclaiming or guaranteed outcomes\n\n"
        "Return JSON only in this shape: {\"hook\": \"...\"}\n\n"
        "Research summary:\n"
        f"{summary}\n"
    )


def _build_slideshow_prompt(summary: str, hook: str) -> str:
    return (
        "You are creating a TikTok slideshow using a specific hook and product research.\n"
        "Return a single JSON object and nothing else. Do not include markdown or code fences.\n\n"
        "Hook (use this exact text for slide 1):\n"
        f"{hook}\n\n"
        "Research summary:\n"
        f"{summary}\n\n"
        "Create a 6-slide deck.\n"
        "- Slide 1: hook (exact text above, nothing else).\n"
        "- Slide 2: frame the pain and validate the viewer.\n"
        "- Slides 3-5: teach or explain the mechanism in short, punchy lines.\n"
        "- Slide 6: CTA that bridges to the product with a natural 'link in bio' line.\n\n"
        "Tone rules:\n"
        "- lowercase, short sentences, no fluff\n"
        "- no apostrophes (dont, cant, wont, ur, uve)\n"
        "- second person (you, your, u, ur)\n"
        "- no emojis, no hashtags\n"
        "- no em dashes or fancy punctuation\n"
        "- no bullet points inside the slide text\n\n"
        "Output schema (camelCase):\n"
        "{\n"
        "  \"slides\": [\n"
        "    {\n"
        "      \"order\": 1,\n"
        "      \"duration_s\": 3,\n"
        "      \"textElements\": [\n"
        "        {\n"
        "          \"text\": \"...\",\n"
        "          \"position\": { \"x\": 20, \"y\": 160 },\n"
        "          \"width\": 344,\n"
        "          \"height\": 80,\n"
        "          \"font_size\": 25,\n"
        "          \"font_color\": \"#FFFFFF\",\n"
        "          \"text_align\": \"center\"\n"
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Layout constraints:\n"
        "- Canvas is 384x480. Keep text inside x 20..364 and y 20..460.\n"
        "- Use x=20 and width=344 for full-width text.\n"
        "- Use 1 or 2 text elements per slide. Keep them readable.\n"
        "- If using 2 text elements, place them around y=80 and y=220.\n"
    )


def _normalize_text_elements(elements: list[dict[str, Any]], slide_index: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for element_index, element in enumerate(elements):
        text = str(element.get("text") or "").strip()
        if not text:
            continue
        position = element.get("position") if isinstance(element.get("position"), dict) else {}
        x = 20
        y = int(position.get("y") or (160 if element_index == 0 else 240))
        width = 344
        height = int(element.get("height") or 80)
        font_size = int(element.get("font_size") or (25 if slide_index == 0 else 18))
        font_color = str(element.get("font_color") or "#FFFFFF")
        text_align = "center"
        normalized.append(
            {
                "id": element.get("id") or f"text_{slide_index + 1}_{element_index + 1}",
                "text": text,
                "position": {"x": x, "y": y},
                "width": width,
                "height": height,
                "font_size": font_size,
                "font_color": font_color,
                "text_align": text_align,
            }
        )
    return normalized


def _normalize_slides(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for slide_index, slide in enumerate(slides):
        elements = slide.get("text_elements") if isinstance(slide.get("text_elements"), list) else None
        if elements is None:
            elements = slide.get("textElements") if isinstance(slide.get("textElements"), list) else []
        normalized_elements = _normalize_text_elements(elements, slide_index)
        if not normalized_elements:
            normalized_elements = _normalize_text_elements(
                [{"text": "", "position": {"x": 20, "y": 160}}],
                slide_index,
            )
        normalized.append(
            {
                "id": slide.get("id") or f"slide_{slide_index + 1}",
                "order": int(slide.get("order") or (slide_index + 1)),
                "duration_s": int(slide.get("duration_s") or 3),
                "text_elements": normalized_elements,
            }
        )
    return normalized


def _assign_images(slides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    images = _community_image_urls()
    fallbacks = _fallback_image_urls()
    if not images:
        return slides
    random.shuffle(images)
    if fallbacks:
        random.shuffle(fallbacks)
    for index, slide in enumerate(slides):
        slide["imageUrl"] = images[index % len(images)]
        if fallbacks:
            slide["fallbackImageUrl"] = fallbacks[index % len(fallbacks)]
    return slides


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "").strip()


async def _openai_json(
    settings: Settings,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise RuntimeError("Add OPENAI_API_KEY to .env first (required for slideshow generation).")

    payload = {
        "model": settings.openai_text_model,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            OPENAI_CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise RuntimeError(f"OpenAI request failed: {response.text[:400]}")

    content = _extract_openai_text(response.json())
    if not content:
        raise RuntimeError("OpenAI did not return content.")

    try:
        return json.loads(_strip_json(content))
    except json.JSONDecodeError as exc:
        logger.exception("Failed to parse OpenAI JSON response")
        raise RuntimeError("OpenAI returned invalid JSON.") from exc


async def generate_slideshow_hook(settings: Settings, research_project: dict[str, Any]) -> str:
    summary = _research_summary_text(research_project)
    prompt = _build_hook_prompt(summary)
    payload = await _openai_json(settings, prompt, max_tokens=120, temperature=0.8)

    hook = str(payload.get("hook") or "").strip()
    if not hook:
        raise RuntimeError("OpenAI did not return a hook.")
    return hook


async def generate_slideshow_from_research(
    settings: Settings,
    research_project: dict[str, Any],
    *,
    hook: str,
) -> dict[str, Any]:
    summary = _research_summary_text(research_project)
    prompt = _build_slideshow_prompt(summary, hook)
    payload = await _openai_json(settings, prompt, max_tokens=2200, temperature=1.0)

    raw_slides = payload.get("slides") if isinstance(payload.get("slides"), list) else None
    if raw_slides is None:
        raise RuntimeError("OpenAI returned an invalid slideshow format.")

    normalized_slides = _assign_images(_normalize_slides(raw_slides))
    payload["slides"] = normalized_slides
    payload["image_collection"] = os.getenv(
        "CLIP_CREATOR_COMMUNITY_COLLECTION_NAME",
        DEFAULT_COMMUNITY_COLLECTION_NAME,
    )

    return payload
