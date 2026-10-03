"""MCP server exposing the Sprite Fusion pixel art API."""

from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage
from pydantic import Field

from .client import (
    PROJECT_ROOT,
    GenerationResult,
    SpriteFusionClient,
    SpriteFusionError,
    set_session_key,
    sniff_mime,
)

INSTRUCTIONS = """\
Create, edit, style-match, make eight-direction views of, and animate pixel art with Sprite Fusion.

Authentication: the server reads SPRITE_FUSION_API_KEY from its environment or .env file. If the user
pastes a Sprite Fusion key in chat, call set_api_key with it and continue their original request
immediately. Only if a tool reports that no key exists, relay its Quick Setup message verbatim
(keep both links exactly) and ask the user to set SPRITE_FUSION_API_KEY or send the key in chat.

Pick the operation that matches the request, and do not substitute another:
- generate_sprite: a new sprite with no input image.
- edit_sprite: a targeted change that keeps the primary (first) image; later inputs are supporting sources.
- style_reference: a new subject guided by 1-20 example images.
- direction_set: exactly eight directional views of one sprite (no prompt).
- animate_sprite: animate one sprite. For walking, running, attacking, jumping and similar actions,
  first use edit_sprite to make a fitting starting pose, then animate that edited asset. Idle and
  subtle motions can use the original sprite.

Prompts: describe the visual result directly. Do not put pixel dimensions in the prompt (use size),
do not ask for a number of outputs (variations come back automatically), and never ask for a grid,
contact sheet, atlas or spritesheet that packs several assets into one image. Avoid filler such as
"transparent background" or "pixel-perfect". Animation prompts should describe motion, energy and
whether it loops; set frames and colors with output_frames and colors.

Ask the user only when a missing choice would materially change the result (what to make, which
image is primary, each reference's role, output size, what motion). Otherwise proceed.

Every generation costs credits and is never retried automatically. Outputs are downloaded to disk;
reuse a returned asset_id as input for follow-up edits, directions or animations. Inputs may be
asset IDs, local file paths, http(s) image URLs, data URLs, or upload IDs from upload_image.
"""

mcp = MCPServer("sprite-fusion", instructions=INSTRUCTIONS)
_client: SpriteFusionClient | None = None

GenSize = Literal[16, 32, 64]
StyleSize = Literal[16, 24, 32, 48, 64, 80, 96, 112, 128]
OutputDir = Annotated[
    str | None,
    Field(description="Folder to save files into (for example a game's asset folder). Defaults to the server's output folder."),
]
Preview = Annotated[bool, Field(description="Return upscaled preview images so the results can be viewed.")]
InputRef = Annotated[
    str,
    Field(description="Asset ID, local file path, http(s) image URL, data URL, or upload ID (upl_...)."),
]


def client() -> SpriteFusionClient:
    global _client
    if _client is None:
        _client = SpriteFusionClient()
    return _client


def default_output_dir() -> Path:
    return Path(os.environ.get("SPRITE_FUSION_OUTPUT_DIR") or PROJECT_ROOT / "output")


def _slug(text: str | None, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:limit].rstrip("-") or "sprite"


def _suffix(url: str, content_type: str | None) -> str:
    if content_type and "/" in content_type:
        return "." + content_type.split("/")[1].split(";")[0].replace("jpeg", "jpg")
    return Path(urlparse(url).path).suffix or ".png"


def _preview(data: bytes, target: int = 256, max_width: int = 1536) -> Image | None:
    """Nearest-neighbour upscale so tiny sprites are legible; first frame only for animations."""
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            img.seek(0)
            frame = img.convert("RGBA")
    except Exception:
        return None
    scale = max(1, target // max(frame.height, 1))
    scale = max(1, min(scale, max_width // max(frame.width, 1)))
    if scale > 1:
        frame = frame.resize((frame.width * scale, frame.height * scale), PILImage.Resampling.NEAREST)
    buf = io.BytesIO()
    frame.save(buf, format="PNG")
    return Image(data=buf.getvalue(), format="png")


async def _save_asset(
    asset: dict[str, Any], out_dir: Path, stem: str, previews: list[Image] | None
) -> dict[str, Any]:
    """Download an asset's files and return a summary record."""
    out_dir.mkdir(parents=True, exist_ok=True)
    asset_id = asset.get("id", "asset")
    record: dict[str, Any] = {
        "asset_id": asset_id,
        "type": asset.get("type"),
        "width": asset.get("width"),
        "height": asset.get("height"),
        "url": asset.get("assetUrl"),
    }
    for key in ("frameCount", "fps", "prompt"):
        if asset.get(key) is not None:
            record[key] = asset[key]

    files = [("assetUrl", "", asset.get("contentType"))]
    if asset.get("spritesheetUrl"):
        files.append(("spritesheetUrl", "-spritesheet", None))
    preview_bytes = None
    for key, tag, content_type in files:
        url = asset.get(key)
        if not url:
            continue
        data = await client().fetch(url)
        path = out_dir / f"{stem}-{asset_id}{tag}{_suffix(url, content_type)}"
        path.write_bytes(data)
        record["local_path" if key == "assetUrl" else "spritesheet_path"] = str(path)
        if key == "spritesheetUrl":
            record["spritesheet_url"] = url
        preview_bytes = data if key == "spritesheetUrl" or preview_bytes is None else preview_bytes
    if asset.get("sourceImageUrl"):
        record["source_image_url"] = asset["sourceImageUrl"]
    if previews is not None and preview_bytes and (img := _preview(preview_bytes)):
        previews.append(img)
    return record


def _log_history(entry: dict[str, Any]) -> None:
    out = default_output_dir()
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "history.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


async def _run(
    ctx: Context,
    body: dict[str, Any],
    input_desc: list[str],
    output_dir: str | None,
    preview: bool,
) -> list[str | Image]:
    out_dir = Path(output_dir).expanduser() if output_dir else default_output_dir()
    stem = f"{body['operation']}-{_slug(body.get('prompt') or body['operation'])}"
    result = GenerationResult(operation=body["operation"])
    previews: list[Image] | None = [] if preview else None

    try:
        async for event in client().generate(body):
            kind = event.get("type")
            if kind == "started":
                result.request_id = event.get("request_id")
                result.credits = event.get("credits") or {}
                await ctx.info(f"Started {result.request_id}; reserved {result.credits.get('reserved')} credits")
            elif kind == "progress":
                await ctx.report_progress(len(result.outputs), message=event.get("message"))
            elif kind == "output":
                record = await _save_asset(event.get("asset") or {}, out_dir, stem, previews)
                record["index"] = event.get("index")
                result.outputs.append(record)
                await ctx.report_progress(len(result.outputs), message=f"Saved output {len(result.outputs)}")
            elif kind == "completed":
                result.completed = True
                result.status = event.get("status")
                result.output_count = event.get("output_count")
                result.credits.update(event.get("credits") or {})
                result.error = event.get("error")
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        _log_history(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "request_id": result.request_id,
                "operation": body["operation"],
                "prompt": body.get("prompt"),
                "size": body.get("size"),
                "inputs": input_desc,
                "status": result.status if result.completed else "stream_interrupted",
                "outputs": result.outputs,
            }
        )

    summary: dict[str, Any] = {
        "request_id": result.request_id,
        "operation": result.operation,
        "status": result.status,
        "output_count": result.output_count,
        "credits_remaining": result.credits.get("remaining"),
        "output_dir": str(out_dir),
        "outputs": result.outputs,
    }
    if result.error:
        summary["error"] = result.error
    if not result.completed:
        summary["status"] = "stream_interrupted"
        summary["note"] = (
            "The stream ended before completion. Do not retry automatically: outputs may already be "
            "saved and charged. Check list_assets for persisted results."
        )
    elif result.output_count is not None and result.output_count != len(result.outputs):
        summary["note"] = f"Expected {result.output_count} outputs but received {len(result.outputs)}."
    if result.completed and result.status == "failed" and not result.outputs:
        raise ToolError(json.dumps(summary, indent=2))
    return [json.dumps(summary, indent=2), *(previews or [])]


async def _resolve(refs: list[str]) -> tuple[list[dict[str, str]], list[str]]:
    try:
        return await client().resolve_inputs(refs)
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def set_api_key(api_key: Annotated[str, Field(description="Sprite Fusion API key (sf_live_...).")]) -> str:
    """Use a Sprite Fusion API key the user supplied in chat for the rest of this session."""
    set_session_key(api_key)
    return "API key set for this session. Continue the user's original request."


@mcp.tool()
async def get_credits() -> str:
    """Return the account's available Sprite Fusion credits. Free."""
    try:
        return json.dumps(await client().credits())
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def list_assets(
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Field(description="nextCursor from a previous page.")] = None,
) -> str:
    """List saved Sprite Fusion assets, newest first. Free. Stop paging when nextCursor is null."""
    try:
        return json.dumps(await client().list_assets(limit, cursor), indent=2)
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def get_asset(asset_id: str) -> str:
    """Get one owned asset's details and file URLs. Free."""
    try:
        return json.dumps(await client().get_asset(asset_id), indent=2)
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def download_asset(asset_id: str, output_dir: OutputDir = None, preview: Preview = True) -> list[str | Image]:
    """Download an existing asset's files (and an animation's spritesheet) to disk. Free."""
    try:
        asset = await client().get_asset(asset_id)
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc
    previews: list[Image] | None = [] if preview else None
    out_dir = Path(output_dir).expanduser() if output_dir else default_output_dir()
    record = await _save_asset(asset, out_dir, _slug(asset.get("prompt") or asset.get("type")), previews)
    return [json.dumps(record, indent=2), *(previews or [])]


@mcp.tool()
async def upload_image(path: Annotated[str, Field(description="Local PNG, JPEG or WebP file, up to 20 MiB.")]) -> str:
    """Stage a large local image as a private temporary input and return its upload_id (valid about a day).

    Generation tools upload large files automatically; use this only to reuse one upload across requests.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        raise ToolError(f"{path} is not a readable file.")
    data = file.read_bytes()
    try:
        return await client().upload(data, sniff_mime(data, path))
    except SpriteFusionError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def generate_sprite(
    ctx: Context,
    prompt: Annotated[str, Field(max_length=10_000, description="One subject; variations come back automatically.")],
    size: GenSize = 32,
    output_dir: OutputDir = None,
    preview: Preview = True,
) -> list[str | Image]:
    """Generate new pixel art sprites from a text prompt (operation: generate). Costs credits."""
    body = {"operation": "generate", "prompt": prompt, "size": size}
    return await _run(ctx, body, [], output_dir, preview)


@mcp.tool()
async def edit_sprite(
    ctx: Context,
    prompt: Annotated[str, Field(description="The change to make and what must stay the same.")],
    inputs: Annotated[list[InputRef], Field(min_length=1, max_length=9, description="First item is the primary sprite; the rest are supporting sources.")],
    output_dir: OutputDir = None,
    preview: Preview = True,
) -> list[str | Image]:
    """Make a targeted edit to an existing sprite, keeping its size (operation: edit). Costs credits."""
    api_inputs, desc = await _resolve(inputs)
    body = {"operation": "edit", "prompt": prompt, "inputs": api_inputs}
    return await _run(ctx, body, desc, output_dir, preview)


@mcp.tool()
async def style_reference(
    ctx: Context,
    prompt: Annotated[str, Field(description="The new subject, plus each reference's role if not obvious.")],
    inputs: Annotated[list[InputRef], Field(min_length=1, max_length=20, description="Style example images.")],
    size: Annotated[StyleSize | None, Field(description="Omit to infer from the input sprites.")] = None,
    output_dir: OutputDir = None,
    preview: Preview = True,
) -> list[str | Image]:
    """Create a new sprite in the style of 1-20 reference images (operation: style-reference). Costs credits."""
    api_inputs, desc = await _resolve(inputs)
    body: dict[str, Any] = {"operation": "style-reference", "prompt": prompt, "inputs": api_inputs}
    if size is not None:
        body["size"] = size
    return await _run(ctx, body, desc, output_dir, preview)


@mcp.tool()
async def direction_set(
    ctx: Context,
    input: InputRef,
    size: GenSize | None = None,
    output_dir: OutputDir = None,
    preview: Preview = True,
) -> list[str | Image]:
    """Create exactly eight directional views of one sprite (operation: direction-set). Takes no prompt. Costs credits."""
    api_inputs, desc = await _resolve([input])
    body: dict[str, Any] = {"operation": "direction-set", "inputs": api_inputs}
    if size is not None:
        body["size"] = size
    return await _run(ctx, body, desc, output_dir, preview)


@mcp.tool()
async def animate_sprite(
    ctx: Context,
    prompt: Annotated[str, Field(max_length=2_000, description="The motion, its energy, and whether it loops.")],
    input: InputRef,
    output_frames: Annotated[int | None, Field(ge=2, le=16, multiple_of=2, description="Even number of frames, 2-16.")] = None,
    colors: Annotated[int | None, Field(ge=2, le=256, description="Palette size, 2-256.")] = None,
    output_dir: OutputDir = None,
    preview: Preview = True,
) -> list[str | Image]:
    """Animate one sprite (operation: animate). Saves the animation and its spritesheet. Costs credits.

    For walking, running, attacking or jumping, first use edit_sprite to make a fitting starting pose.
    """
    api_inputs, desc = await _resolve([input])
    body: dict[str, Any] = {"operation": "animate", "prompt": prompt, "inputs": api_inputs}
    if output_frames is not None:
        body["output_frames"] = output_frames
    if colors is not None:
        body["colors"] = colors
    return await _run(ctx, body, desc, output_dir, preview)
