"""Async client for the Sprite Fusion API (https://www.spritefusion.com/docs/pixel-art-generator/api)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from PIL import Image as PILImage

BASE_URL = "https://www.spritefusion.com/api/v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

QUICK_SETUP = (
    "No Sprite Fusion API key found.\n\n"
    "Quick Setup:\n"
    "1. [Create your Sprite Fusion account](https://www.spritefusion.com/auth/sign-up?next=%2Faccount%2Fapi-keys)\n"
    "2. [Create and copy an API key](https://www.spritefusion.com/account/api-keys)\n"
    "3. Set SPRITE_FUSION_API_KEY (environment or the server's .env file), or send the key in chat "
    "so it can be passed to the set_api_key tool.\n\n"
    "Once the key is available, the original request will continue."
)

# Inline data URLs must stay under 1,000,000 decoded bytes each and 2,500,000 total;
# leave headroom so the JSON body stays under the 4,000,000-byte request limit.
INLINE_MAX_BYTES = 900_000
INLINE_MAX_TOTAL = 2_400_000
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv()

# httpx logs full URLs at INFO, and signed upload URLs must not be logged.
logging.getLogger("httpx").setLevel(logging.WARNING)


class SpriteFusionError(Exception):
    pass


@dataclass
class GenerationResult:
    request_id: str | None = None
    operation: str | None = None
    status: str | None = None
    output_count: int | None = None
    credits: dict[str, Any] = field(default_factory=dict)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error: Any = None
    completed: bool = False


_session_key: str | None = None


def set_session_key(key: str) -> None:
    global _session_key
    _session_key = key.strip()


def api_key() -> str:
    key = _session_key or os.environ.get("SPRITE_FUSION_API_KEY", "").strip()
    if not key:
        raise SpriteFusionError(QUICK_SETUP)
    return key


def _error_from_response(resp: httpx.Response, body: bytes) -> SpriteFusionError:
    code, message = "http_error", body.decode("utf-8", "replace")[:500]
    try:
        err = json.loads(body).get("error", {})
        code, message = err.get("code", code), err.get("message", message)
    except (ValueError, AttributeError):
        pass
    text = f"Sprite Fusion API error {resp.status_code} ({code}): {message}"
    if resp.status_code == 401:
        text += "\n\n" + QUICK_SETUP
    if resp.status_code == 429 and (retry := resp.headers.get("Retry-After")):
        text += f" (retry after {retry} seconds)"
    return SpriteFusionError(text)


class SpriteFusionClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30, read=900, write=120, pool=30),
            follow_redirects=True,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key()}"}

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._http.request(method, BASE_URL + path, headers=self._headers(), **kwargs)
        if resp.is_error:
            raise _error_from_response(resp, resp.content)
        return resp.json()

    async def credits(self) -> dict[str, Any]:
        return await self._json("GET", "/credits")

    async def list_assets(self, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._json("GET", "/assets", params=params)

    async def get_asset(self, asset_id: str) -> dict[str, Any]:
        return (await self._json("GET", f"/assets/{asset_id}"))["asset"]

    async def upload(self, data: bytes, content_type: str) -> str:
        """Stage a private temporary input and return its upload_id (valid for about a day)."""
        if len(data) > UPLOAD_MAX_BYTES:
            raise SpriteFusionError(f"Image is {len(data)} bytes; uploads are limited to 20 MiB.")
        desc = await self._json(
            "POST",
            "/uploads",
            json={
                "content_type": content_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            },
        )
        headers = desc.get("headers") or {"Content-Type": content_type}
        resp = await self._http.put(desc["upload_url"], headers=headers, content=data)
        if resp.is_error:
            raise SpriteFusionError(f"Upload PUT failed with status {resp.status_code}.")
        return desc["upload_id"]

    async def fetch(self, url: str) -> bytes:
        resp = await self._http.get(url)
        resp.raise_for_status()
        return resp.content

    async def resolve_inputs(self, refs: list[str]) -> tuple[list[dict[str, str]], list[str]]:
        """Turn user references into API input objects.

        Accepts asset IDs, upload IDs (upl_...), data URLs, http(s) image URLs and local
        file paths. Local and remote files go inline when small, otherwise via /uploads.
        Returns the inputs plus a log-safe description of each.
        """
        inputs: list[dict[str, str]] = []
        described: list[str] = []
        inline_total = 0
        for ref in refs:
            ref = ref.strip()
            if ref.startswith("data:"):
                inputs.append({"data_url": ref})
                described.append("data_url")
                continue
            if ref.startswith("upl_"):
                inputs.append({"upload_id": ref})
                described.append("upload")
                continue
            if ref.startswith(("http://", "https://")):
                data, label = await self.fetch(ref), ref
            elif (path := Path(ref).expanduser()).is_file():
                data, label = path.read_bytes(), str(path)
            else:
                inputs.append({"asset_id": ref})
                described.append(f"asset:{ref}")
                continue

            mime = sniff_mime(data, label)
            if len(data) <= INLINE_MAX_BYTES and inline_total + len(data) <= INLINE_MAX_TOTAL:
                inline_total += len(data)
                inputs.append({"data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"})
                described.append(f"inline:{label}")
            else:
                inputs.append({"upload_id": await self.upload(data, mime)})
                described.append(f"uploaded:{label}")
        return inputs, described

    async def generate(self, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """POST /generate and yield each SSE JSON event. Never retried: outputs may already be charged."""
        async with self._http.stream(
            "POST", BASE_URL + "/generate", headers=self._headers(), json=body
        ) as resp:
            if resp.is_error:
                raise _error_from_response(resp, await resp.aread())
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line == "":
                    if data_lines:
                        yield json.loads("\n".join(data_lines))
                        data_lines = []
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                yield json.loads("\n".join(data_lines))

    async def aclose(self) -> None:
        await self._http.aclose()


def sniff_mime(data: bytes, label: str) -> str:
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            fmt = img.format
    except Exception as exc:
        raise SpriteFusionError(f"Could not read image {label}: {exc}") from exc
    if fmt not in MIME_BY_FORMAT:
        raise SpriteFusionError(f"{label} is {fmt}; Sprite Fusion accepts only PNG, JPEG and WebP.")
    return MIME_BY_FORMAT[fmt]
