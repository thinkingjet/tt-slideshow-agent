from __future__ import annotations

import mimetypes
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


class PostBridgeError(RuntimeError):
    pass


class PostBridgeClient:
    def __init__(self, *, api_key: str | None, base_url: str) -> None:
        if not api_key:
            raise PostBridgeError("POSTBRIDGE_API_KEY is not set")
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(4):
            response = self.client.request(method, path, **kwargs)
            if response.status_code != 429:
                break
            retry_after = response.headers.get("retry-after")
            sleep_seconds = float(retry_after) if retry_after else 2**attempt
            time.sleep(sleep_seconds)
        else:
            raise PostBridgeError("PostBridge rate limit did not clear after retries")

        if response.is_error:
            detail = response.text
            raise PostBridgeError(f"PostBridge {method} {path} failed: {response.status_code} {detail}")
        if not response.content:
            return None
        return response.json()

    def get_social_accounts(self, platforms: list[str] | None = None) -> dict:
        params: list[tuple[str, str | int]] = [("offset", 0), ("limit", 100)]
        for platform in platforms or []:
            params.append(("platform", platform))
        return self._request("GET", "/social-accounts", params=params)

    def get_posts(self, *, limit: int = 100) -> dict:
        return self._request("GET", "/posts", params={"offset": 0, "limit": limit})

    def get_post_results(self, *, limit: int = 100) -> dict:
        return self._request("GET", "/post-results", params={"offset": 0, "limit": limit})

    def sync_analytics(self, platform: str | None = None) -> None:
        params = {"platform": platform} if platform else None
        self._request("POST", "/analytics/sync", params=params)

    def create_upload_url(self, path: Path) -> dict:
        mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        payload = {
            "name": path.name,
            "mime_type": mime_type,
            "size_bytes": path.stat().st_size,
        }
        return self._request("POST", "/media/create-upload-url", json=payload)

    def upload_media_file(self, path: Path) -> str:
        upload = self.create_upload_url(path)
        media_id = upload["media_id"]
        upload_url = upload["upload_url"]
        mime_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        with path.open("rb") as file:
            response = httpx.put(upload_url, headers={"Content-Type": mime_type}, content=file, timeout=300)
        if response.is_error:
            raise PostBridgeError(f"PostBridge media upload failed: {response.status_code} {response.text}")
        return media_id

    def create_post(
        self,
        *,
        caption: str,
        media_id: str,
        social_account_ids: list[int],
        scheduled_at: datetime | None,
    ) -> dict:
        payload: dict[str, Any] = {
            "caption": caption,
            "media": [media_id],
            "social_accounts": social_account_ids,
            "processing_enabled": True,
        }
        if scheduled_at is not None:
            payload["scheduled_at"] = scheduled_at.isoformat()
        return self._request("POST", "/posts", json=payload)

