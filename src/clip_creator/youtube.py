from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp


YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class ShortMetadata:
    youtube_id: str
    title: str | None
    source_url: str
    thumbnail_url: str | None
    view_count: int | None
    duration: float | None


def extract_video_id(value: str) -> str | None:
    if YOUTUBE_ID_RE.match(value):
        return value

    parsed = urlparse(value)
    path_parts = [part for part in parsed.path.split("/") if part]

    if parsed.netloc.endswith("youtu.be") and path_parts:
        candidate = path_parts[0]
        return candidate if YOUTUBE_ID_RE.match(candidate) else None

    if "youtube.com" in parsed.netloc:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            return candidate if candidate and YOUTUBE_ID_RE.match(candidate) else None
        if "shorts" in path_parts:
            index = path_parts.index("shorts")
            if len(path_parts) > index + 1:
                candidate = path_parts[index + 1]
                return candidate if YOUTUBE_ID_RE.match(candidate) else None

    return None


def canonical_video_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/shorts/{youtube_id}"


def _entry_to_metadata(entry: dict) -> ShortMetadata | None:
    youtube_id = entry.get("id") or extract_video_id(entry.get("url") or entry.get("webpage_url") or "")
    if not youtube_id or not YOUTUBE_ID_RE.match(youtube_id):
        return None
    return ShortMetadata(
        youtube_id=youtube_id,
        title=entry.get("title"),
        source_url=entry.get("webpage_url") or canonical_video_url(youtube_id),
        thumbnail_url=entry.get("thumbnail"),
        view_count=entry.get("view_count"),
        duration=entry.get("duration"),
    )


class YouTubeShortsClient:
    def discover_shorts(self, source_url: str, limit: int) -> list[ShortMetadata]:
        options = {
            "extract_flat": "in_playlist",
            "playlistend": limit,
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=False)

        entries = info.get("entries") or [info]
        shorts: list[ShortMetadata] = []
        seen: set[str] = set()
        for entry in entries:
            metadata = _entry_to_metadata(entry or {})
            if metadata is None or metadata.youtube_id in seen:
                continue
            seen.add(metadata.youtube_id)
            shorts.append(metadata)
            if len(shorts) >= limit:
                break
        return shorts

    def download_short(self, metadata: ShortMetadata, output_template: str) -> Path:
        options = {
            "format": "bv*[height<=1920]+ba/b[height<=1920]/best",
            "merge_output_format": "mp4",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(metadata.source_url, download=True)

        requested_downloads = info.get("requested_downloads") or []
        for download in requested_downloads:
            filepath = download.get("filepath")
            if filepath:
                return Path(filepath)

        filepath = info.get("filepath") or info.get("_filename")
        if filepath:
            return Path(filepath)

        expected = Path(output_template.replace("%(id)s", metadata.youtube_id).replace("%(ext)s", "mp4"))
        if expected.exists():
            return expected
        raise RuntimeError(f"yt-dlp finished but did not return a file path for {metadata.youtube_id}")

