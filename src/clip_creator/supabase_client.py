from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


class SupabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupabaseUser:
    id: str
    email: str | None = None


class SupabaseClient:
    def __init__(
        self,
        *,
        url: str | None,
        anon_key: str | None,
        service_role_key: str | None,
        storage_bucket: str,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.anon_key = anon_key or ""
        self.service_role_key = service_role_key or ""
        self.storage_bucket = storage_bucket

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.anon_key and self.service_role_key)

    def _require_configured(self) -> None:
        if not self.is_configured:
            raise SupabaseError("Supabase is not configured. Add SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY to .env.")

    def public_config(self) -> dict[str, str | bool | None]:
        return {
            "configured": bool(self.url and self.anon_key),
            "url": self.url or None,
            "anon_key": self.anon_key or None,
        }

    def _service_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        self._require_configured()
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
        }
        if extra:
            headers.update(extra)
        return headers

    async def verify_access_token(self, access_token: str) -> SupabaseUser:
        self._require_configured()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.url}/auth/v1/user",
                headers={
                    "apikey": self.anon_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
        if response.status_code >= 400:
            raise SupabaseError("Invalid or expired Supabase session.")
        payload = response.json()
        user_id = payload.get("id")
        if not user_id:
            raise SupabaseError("Supabase session did not include a user id.")
        return SupabaseUser(id=user_id, email=payload.get("email"))

    async def upload_research_file(
        self,
        *,
        user_id: str,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, str]:
        self._require_configured()
        safe_name = file_name.replace("/", "-") or "context-file"
        storage_path = f"{user_id}/{uuid.uuid4().hex}_{safe_name}"
        object_path = quote(f"{self.storage_bucket}/{storage_path}", safe="/")

        async with httpx.AsyncClient(timeout=60) as client:
            upload_response = await client.post(
                f"{self.url}/storage/v1/object/{object_path}",
                headers=self._service_headers({
                    "Content-Type": content_type,
                    "x-upsert": "false",
                }),
                content=content,
            )
            if upload_response.status_code >= 400:
                raise SupabaseError(f"Supabase Storage upload failed: {upload_response.text[:300]}")

            sign_response = await client.post(
                f"{self.url}/storage/v1/object/sign/{object_path}",
                headers=self._service_headers({"Content-Type": "application/json"}),
                json={"expiresIn": 60 * 60},
            )
            if sign_response.status_code >= 400:
                raise SupabaseError(f"Supabase signed URL failed: {sign_response.text[:300]}")

        signed_url = sign_response.json().get("signedURL") or ""
        if signed_url.startswith("/"):
            signed_url = f"{self.url}/storage/v1{signed_url}"
        return {
            "bucket": self.storage_bucket,
            "storage_path": storage_path,
            "signed_url": signed_url,
        }

    async def upload_asset(
        self,
        *,
        user_id: str,
        storage_path: str,
        content: bytes,
        content_type: str,
        expires_in_seconds: int = 60 * 60,
        upsert: bool = False,
    ) -> dict[str, str]:
        """
        Upload arbitrary generated assets (png/mp4/etc) to Supabase Storage.
        storage_path is relative to the configured bucket.
        """
        self._require_configured()
        cleaned_path = storage_path.lstrip("/").replace("..", "")
        if not cleaned_path:
            raise SupabaseError("storage_path is required")

        # Keep all assets under the user prefix.
        if not cleaned_path.startswith(f"{user_id}/"):
            cleaned_path = f"{user_id}/{cleaned_path}"

        object_path = quote(f"{self.storage_bucket}/{cleaned_path}", safe="/")
        async with httpx.AsyncClient(timeout=120) as client:
            upload_response = await client.post(
                f"{self.url}/storage/v1/object/{object_path}",
                headers=self._service_headers({
                    "Content-Type": content_type,
                    "x-upsert": "true" if upsert else "false",
                }),
                content=content,
            )
            if upload_response.status_code >= 400:
                raise SupabaseError(f"Supabase Storage upload failed: {upload_response.text[:300]}")

            sign_response = await client.post(
                f"{self.url}/storage/v1/object/sign/{object_path}",
                headers=self._service_headers({"Content-Type": "application/json"}),
                json={"expiresIn": int(expires_in_seconds)},
            )
            if sign_response.status_code >= 400:
                raise SupabaseError(f"Supabase signed URL failed: {sign_response.text[:300]}")

        signed_url = sign_response.json().get("signedURL") or ""
        if signed_url.startswith("/"):
            signed_url = f"{self.url}/storage/v1{signed_url}"
        return {
            "bucket": self.storage_bucket,
            "storage_path": cleaned_path,
            "signed_url": signed_url,
        }

    async def sign_asset(
        self,
        *,
        storage_path: str,
        expires_in_seconds: int = 60 * 60,
    ) -> str:
        """
        Create a signed URL for an existing object in the configured bucket.
        storage_path is relative to the configured bucket (and should already include the user prefix).
        """
        self._require_configured()
        cleaned_path = (storage_path or "").lstrip("/").replace("..", "")
        if not cleaned_path:
            raise SupabaseError("storage_path is required")

        object_path = quote(f"{self.storage_bucket}/{cleaned_path}", safe="/")
        async with httpx.AsyncClient(timeout=60) as client:
            sign_response = await client.post(
                f"{self.url}/storage/v1/object/sign/{object_path}",
                headers=self._service_headers({"Content-Type": "application/json"}),
                json={"expiresIn": int(expires_in_seconds)},
            )
        if sign_response.status_code >= 400:
            raise SupabaseError(f"Supabase signed URL failed: {sign_response.text[:300]}")

        signed_url = sign_response.json().get("signedURL") or ""
        if signed_url.startswith("/"):
            signed_url = f"{self.url}/storage/v1{signed_url}"
        return signed_url

    async def list_storage_objects(
        self,
        *,
        prefix: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """
        List objects in Supabase Storage bucket under a prefix.
        Returns dicts with at least: name, id, updated_at, metadata (Supabase dependent).
        """
        self._require_configured()
        cleaned_prefix = (prefix or "").lstrip("/").replace("..", "")
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.url}/storage/v1/object/list/{quote(self.storage_bucket)}",
                headers=self._service_headers({"Content-Type": "application/json"}),
                json={
                    "prefix": cleaned_prefix,
                    "limit": int(limit),
                    "offset": int(offset),
                    "sortBy": {"column": "updated_at", "order": "desc"},
                },
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Supabase Storage list failed: {response.text[:300]}")
        payload = response.json()
        return payload if isinstance(payload, list) else []

    async def create_research_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rest_insert("product_research_projects", payload)
        return rows[0]

    async def create_research_files(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        return await self._rest_insert("product_research_files", rows)

    async def list_research_projects(self, user_id: str) -> list[dict[str, Any]]:
        self._require_configured()
        query = (
            "select=id,product_name,product_url,audience,plan,created_at,updated_at"
            f"&user_id=eq.{quote(user_id)}&order=created_at.desc"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.url}/rest/v1/product_research_projects?{query}",
                headers=self._service_headers(),
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Failed to list saved products: {response.text[:300]}")
        return response.json()

    async def get_research_project(self, *, user_id: str, project_id: str) -> dict[str, Any] | None:
        self._require_configured()
        query = f"select=*&id=eq.{quote(project_id)}&user_id=eq.{quote(user_id)}&limit=1"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.url}/rest/v1/product_research_projects?{query}",
                headers=self._service_headers(),
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Failed to load saved product: {response.text[:300]}")
        rows = response.json()
        return rows[0] if rows else None

    async def list_research_files(self, *, user_id: str, project_id: str) -> list[dict[str, Any]]:
        self._require_configured()
        query = f"select=*&project_id=eq.{quote(project_id)}&user_id=eq.{quote(user_id)}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{self.url}/rest/v1/product_research_files?{query}",
                headers=self._service_headers(),
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Failed to load saved product files: {response.text[:300]}")
        return response.json()

    async def delete_research_project(self, *, user_id: str, project_id: str) -> None:
        files = await self.list_research_files(user_id=user_id, project_id=project_id)
        storage_paths = [file["storage_path"] for file in files if file.get("storage_path")]

        async with httpx.AsyncClient(timeout=30) as client:
            if storage_paths:
                delete_storage = await client.request(
                    "DELETE",
                    f"{self.url}/storage/v1/object/{quote(self.storage_bucket)}",
                    headers=self._service_headers({"Content-Type": "application/json"}),
                    json={"prefixes": storage_paths},
                )
                if delete_storage.status_code >= 400:
                    raise SupabaseError(f"Failed to delete stored files: {delete_storage.text[:300]}")

            response = await client.delete(
                f"{self.url}/rest/v1/product_research_projects?id=eq.{quote(project_id)}&user_id=eq.{quote(user_id)}",
                headers=self._service_headers({"Prefer": "return=minimal"}),
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Failed to delete saved product: {response.text[:300]}")

    async def _rest_insert(self, table: str, payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._require_configured()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.url}/rest/v1/{table}",
                headers=self._service_headers({
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                }),
                json=payload,
            )
        if response.status_code >= 400:
            raise SupabaseError(f"Failed to insert into {table}: {response.text[:300]}")
        return response.json()
