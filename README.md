# Sprite Fusion MCP

An MCP server (Python, `mcp` SDK v2) for the [Sprite Fusion API](https://www.spritefusion.com/docs/pixel-art-generator/api). It creates, edits, style-matches, makes eight-direction views of, and animates pixel art, and saves every result to disk.

## Setup

1. Run `make env` and set `SPRITE_FUSION_API_KEY` in `.env`. You can also paste a key in chat; the assistant passes it to `set_api_key`.
2. Run `make register` to install dependencies and register the server with Claude Code. Run `make credits` to check the key.

Run `make` to list all upkeep targets (build, upgrade, clean, uninstall and more).

## Tools

| Tool | Operation | Credits |
| --- | --- | --- |
| `generate_sprite` | New sprite from a prompt (size 16, 32 or 64) | Yes |
| `edit_sprite` | Targeted edit; first input is primary, up to 9 | Yes |
| `style_reference` | New subject in the style of 1–20 images | Yes |
| `direction_set` | Eight directional views of one sprite | Yes |
| `animate_sprite` | Animation plus spritesheet (2–16 even frames, 2–256 colors) | Yes |
| `get_credits`, `list_assets`, `get_asset`, `download_asset` | Account and library | No |
| `upload_image` | Stage a large local image for reuse | No |
| `set_api_key` | Use a key supplied in chat | No |

Image inputs accept an asset ID, a local file path, an http(s) URL, a data URL, or an upload ID. Local and remote files are sent inline when small and through a temporary upload when large.

Results go to `./output` (or `SPRITE_FUSION_OUTPUT_DIR`, or a per-call `output_dir`). Each request is recorded in `history.jsonl` in the default output folder. Generations are never retried automatically, because a broken stream may already have saved and charged outputs.
