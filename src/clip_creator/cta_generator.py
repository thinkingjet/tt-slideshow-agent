from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Final

import httpx
from PIL import Image, ImageDraw, ImageFont

from .processor import ensure_ffmpeg_available, run_command

REPLICATE_PREDICTIONS_URL: Final[str] = "https://api.replicate.com/v1/predictions"
logger = logging.getLogger("clip_creator")


def _background_prompt(*, product_name: str) -> str:
    return (
        "Create a photorealistic, premium TikTok/Reels ad background for a mobile app.\n"
        "Make it look like a real high-end UGC ad still (shot on phone), NOT AI/3D/sci-fi.\n"
        "Style: natural, aesthetic, premium, minimal, tasteful; soft lighting; clean color grading.\n"
        "Composition:\n"
        "- Leave a clean empty margin at the very top for headline text (but DO NOT render any text).\n"
        "- Center area: a realistic smartphone mockup (subtle reflections), with a tasteful content grid / app vibe.\n"
        "- Background: a real person reacting with mild surprise (subtle, not exaggerated), lifestyle setting, premium.\n"
        f"- Add a subtle physical sign/label in the scene that says ONLY: \"{product_name}\" (no other text).\n"
        "- Avoid busy patterns.\n"
        "- No other readable text anywhere (no extra words, no logos, no watermarks, no UI labels).\n"
        f"- Product context: {product_name}\n"
        "Negative: cartoon, illustration, 3D render, cyberpunk, neon vortex, distorted hands, gibberish text.\n"
    )

def _choose_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    # Try common macOS font paths first for crisp bold text; fall back to Pillow default.
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
        "/System/Library/Fonts/SFNS.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_bbox(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    stroke_width: int,
) -> tuple[int, int, int, int]:
    # textbbox accounts for stroke width and any negative left bearing.
    return draw.textbbox((0, 0), text or "", font=font, stroke_width=int(stroke_width))


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int | None = None,
    stroke_width: int = 0,
) -> list[str]:
    words = (text or "").replace("\n", " ").split()
    if not words:
        return [""]
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        trial = " ".join([*current, word]).strip()
        bbox = _text_bbox(draw, trial, font=font, stroke_width=stroke_width)
        width = bbox[2] - bbox[0]
        if width <= max_width or not current:
            current.append(word)
            continue
        lines.append(" ".join(current))
        current = [word]
    if current:
        lines.append(" ".join(current))
    if max_lines is None:
        return lines
    return lines[: max(1, int(max_lines))]

def _fit_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    font: ImageFont.ImageFont,
    stroke_width: int,
    max_width: int,
    max_height: int,
    line_height: int,
) -> list[str]:
    """
    Wrap text to any number of lines, but ensure the full block fits in max_width/max_height.
    Caller is expected to shrink font and retry until it fits.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return [""]

    # Respect intentional newlines first, but still wrap long lines.
    parts = [p.strip() for p in cleaned.split("\n") if p.strip()]
    if not parts:
        parts = [cleaned.replace("\n", " ")]

    lines: list[str] = []
    for part in parts:
        lines.extend(_wrap_text(draw, part, font, max_width, max_lines=None, stroke_width=stroke_width))
    if not lines:
        lines = [""]

    # Validate fit; if it doesn't fit, caller should shrink and retry.
    block_height = line_height * max(1, len(lines))
    if block_height > max_height:
        return lines

    for line in lines:
        bbox = _text_bbox(draw, line, font=font, stroke_width=stroke_width)
        if (bbox[2] - bbox[0]) > max_width:
            return lines

    return lines


def _render_overlay_png(base_size: tuple[int, int], text: str, output_path: Path) -> None:
    canvas = Image.new("RGBA", base_size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    w, h = base_size
    top_padding = int(h * 0.065)
    side_padding = int(w * 0.08)
    safe_side = int(w * 0.06)
    max_width = w - (safe_side * 2)
    max_height = int(h * 0.30)  # allocate more top space so multi-line captions always fit

    # Smaller, more natural headline sizing.
    font_size = int(h * 0.045)
    font = _choose_font(font_size)
    stroke_width = max(2, int(font_size * 0.07))
    line_height = int(font_size * 1.2)
    lines = _fit_text_block(
        draw,
        text,
        font=font,
        stroke_width=stroke_width,
        max_width=max_width,
        max_height=max_height,
        line_height=line_height,
    )

    # Shrink font until the whole block fits.
    for _ in range(16):
        stroke_width = max(2, int(font_size * 0.07))
        line_height = int(font_size * 1.2)
        lines = _fit_text_block(
            draw,
            text,
            font=font,
            stroke_width=stroke_width,
            max_width=max_width,
            max_height=max_height,
            line_height=line_height,
        )

        widths = []
        for line in lines:
            bbox = _text_bbox(draw, line, font=font, stroke_width=stroke_width)
            widths.append(bbox[2] - bbox[0])
        widest = max(widths) if widths else 0

        block_height = line_height * max(1, len(lines))
        if widest <= max_width and block_height <= max_height:
            break

        font_size = max(12, int(font_size * 0.9))
        font = _choose_font(font_size)

    line_height = int(font_size * 1.2)

    y = top_padding
    for line in lines:
        stroke_width = max(2, int(font_size * 0.07))
        bbox = _text_bbox(draw, line, font=font, stroke_width=stroke_width)
        line_w = bbox[2] - bbox[0]
        # Center using true bbox (accounts for negative left bearing).
        x = ((w - line_w) / 2) - bbox[0]
        # Clamp into safe bounds to guarantee no clipping.
        x = max(safe_side, min(x, w - safe_side - line_w))
        # Stroke for contrast.
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=stroke_width,
            stroke_fill=(0, 0, 0, 220),
        )
        y += line_height

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")


def _plan_cta_copy_from_research(research_project: dict) -> dict[str, str]:
    """
    Fully offline planning (no OpenAI): use the research plan's existing hooks/ctas
    to produce two overlay lines.
    """
    product_name = str(research_project.get("product_name") or "").strip() or "this app"
    plan = research_project.get("plan") if isinstance(research_project.get("plan"), dict) else {}

    product = plan.get("product") if isinstance(plan.get("product"), dict) else {}
    summary = str(product.get("summary") or "").strip()
    positioning = str(product.get("positioning") or "").strip()

    hooks = plan.get("hookBank") if isinstance(plan.get("hookBank"), list) else []
    hooks = [str(x).strip() for x in hooks if str(x).strip()]
    ctas = plan.get("ctas") if isinstance(plan.get("ctas"), list) else []
    ctas = [str(x).strip() for x in ctas if str(x).strip()]

    frame1 = hooks[0] if hooks else f"Try {product_name}"
    # Keep frame1 short-ish.
    frame1_words = frame1.replace("?", "").split()
    if len(frame1_words) > 7:
        frame1 = " ".join(frame1_words[:7]) + "?"

    # Frame 2 should be direct + outcome driven (no "positioning" language).
    frame2_main = f"Use {product_name} to capture missed sales."
    # Keep it punchy and fit in 2 lines reliably.
    if len(frame2_main) > 46:
        frame2_main = "Capture missed sales with " + product_name
    frame2 = f"{frame2_main}\nLink in bio"

    return {"frame1_text": frame1.strip(), "frame2_text": frame2.strip()}


def _model_to_owner_name(model: str) -> tuple[str, str]:
    cleaned = (model or "").strip().strip("/")
    if "/" not in cleaned:
        raise ValueError("Replicate model must be like 'owner/name'.")
    owner, name = cleaned.split("/", 1)
    return owner, name


async def _replicate_generate_image_bytes(
    *,
    replicate_api_token: str,
    model: str,
    prompt: str,
    aspect_ratio: str = "2:3",
    output_format: str = "webp",
) -> bytes:
    owner, name = _model_to_owner_name(model)
    headers = {
        "Authorization": f"Token {replicate_api_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=180) as client:
        create = await client.post(
            f"https://api.replicate.com/v1/models/{owner}/{name}/predictions",
            headers=headers,
            json={
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "output_format": output_format,
                }
            },
        )
        if create.status_code >= 400:
            raise RuntimeError(f"Replicate image generation failed: {create.text[:400]}")
        prediction = create.json()
        pred_id = prediction.get("id")
        if not pred_id:
            raise RuntimeError("Replicate did not return a prediction id.")
        logger.info("[replicate] created prediction id=%s model=%s/%s aspect_ratio=%s format=%s", pred_id, owner, name, aspect_ratio, output_format)

        # Poll
        for _ in range(240):
            poll = await client.get(f"{REPLICATE_PREDICTIONS_URL}/{pred_id}", headers=headers)
            if poll.status_code >= 400:
                raise RuntimeError(f"Replicate poll failed: {poll.text[:400]}")
            payload = poll.json()
            status = payload.get("status")
            if status == "succeeded":
                output = payload.get("output")
                url: str | None = None
                if isinstance(output, list) and output:
                    url = output[0] if isinstance(output[0], str) else None
                elif isinstance(output, str):
                    url = output
                if not url:
                    raise RuntimeError("Replicate returned no output URL.")
                logger.info("[replicate] succeeded id=%s url=%s", pred_id, url)
                img = await client.get(url)
                img.raise_for_status()
                return img.content
            if status in ("failed", "canceled"):
                err = payload.get("error") or payload.get("logs") or "Replicate prediction failed."
                raise RuntimeError(str(err)[:600])
            await asyncio.sleep(0.5)

        raise RuntimeError("Replicate prediction timed out.")


def _assemble_10s_video_single_background(
    *,
    background: Path,
    overlay_first: Path,
    overlay_second: Path,
    audio_path: Path | None,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # One background for 10 seconds. Overlay 1 for 0-3s, overlay 2 for 3-10s.
    filter_complex = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,fps=30[bg];"
        "[1:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[ov1];"
        "[2:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[ov2];"
        "[bg][ov1]overlay=0:0:enable='between(t,0,3)'[tmp];"
        "[tmp][ov2]overlay=0:0:enable='between(t,3,10)'[outv]"
    )

    command: list[str] = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-t",
        "10",
        "-i",
        str(background),
        "-loop",
        "1",
        "-t",
        "10",
        "-i",
        str(overlay_first),
        "-loop",
        "1",
        "-t",
        "10",
        "-i",
        str(overlay_second),
    ]

    if audio_path is not None and audio_path.exists():
        # Loop audio if needed, then trim to exactly 10s in filtergraph so it never cuts out mid-video.
        command.extend(["-stream_loop", "-1", "-i", str(audio_path)])
        filter_complex = filter_complex + ";[3:a]atrim=0:10,asetpts=N/SR/TB[aout]"

    command.extend(["-filter_complex", filter_complex, "-map", "[outv]"])

    if audio_path is not None and audio_path.exists():
        command.extend(
            [
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                "-ar",
                "44100",
                "-ac",
                "2",
                "-b:a",
                "192k",
            ]
        )

    command.extend(
        [
            "-t",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-r",
            "30",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    run_command(command)


async def generate_cta_video(
    *,
    replicate_api_token: str,
    model: str,
    product_name: str,
    cta_hint_1: str,
    cta_hint_2: str,
    output_path: Path,
    tmp_dir: Path,
    audio_path: Path | None = None,
    image_size: str = "1088x1920",
    image_quality: str = "medium",
) -> Path:
    ensure_ffmpeg_available()
    tmp_dir.mkdir(parents=True, exist_ok=True)

    background_path = tmp_dir / "cta_background.png"
    overlay_1_path = tmp_dir / "cta_overlay_1.png"
    overlay_2_path = tmp_dir / "cta_overlay_2.png"

    prompt = _background_prompt(product_name=product_name)
    logger.info("[cta] generating background via replicate")
    bg_raw = await _replicate_generate_image_bytes(
        replicate_api_token=replicate_api_token,
        model=model,
        prompt=prompt,
        aspect_ratio="2:3",
        output_format="webp",
    )
    background_bytes = _webp_to_png_bytes(bg_raw)
    background_path.write_bytes(background_bytes)

    img = Image.open(background_path)
    _render_overlay_png(img.size, cta_hint_1, overlay_1_path)
    _render_overlay_png(img.size, cta_hint_2, overlay_2_path)
    logger.info("[cta] wrote bg=%s ov1=%s ov2=%s", background_path, overlay_1_path, overlay_2_path)
    logger.info("[cta] overlay text (timed) frame1=%r frame2=%r", cta_hint_1, cta_hint_2)

    _assemble_10s_video_single_background(
        background=background_path,
        overlay_first=overlay_1_path,
        overlay_second=overlay_2_path,
        audio_path=audio_path,
        output_path=output_path,
    )
    logger.info("[cta] assembled mp4 %s", output_path)
    return output_path


async def generate_cta_video_from_research(
    *,
    image_model: str,
    replicate_api_token: str,
    research_project: dict,
    output_path: Path,
    tmp_dir: Path,
    audio_path: Path | None = None,
    image_size: str = "1088x1920",
    image_quality: str = "medium",
) -> dict[str, str]:
    planned = _plan_cta_copy_from_research(research_project)
    product_name = str(research_project.get("product_name") or "").strip() or "Mobile app"
    await generate_cta_video(
        replicate_api_token=replicate_api_token,
        model=image_model,
        product_name=product_name,
        cta_hint_1=planned["frame1_text"],
        cta_hint_2=planned["frame2_text"],
        output_path=output_path,
        tmp_dir=tmp_dir,
        audio_path=audio_path,
        image_size=image_size,
        image_quality=image_quality,
    )
    return planned


def _webp_to_png_bytes(image_bytes: bytes) -> bytes:
    import io

    img = Image.open(io.BytesIO(image_bytes))
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG")
    return out.getvalue()

