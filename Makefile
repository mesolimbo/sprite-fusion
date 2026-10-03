SHELL := bash
.DEFAULT_GOAL := help

NAME  ?= sprite-fusion
SCOPE ?= user
DIR   := $(CURDIR)

.PHONY: help install env build run check credits inspect register unregister reinstall status \
	upgrade clean clean-output distclean uninstall

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "  Variables: NAME=$(NAME) SCOPE=$(SCOPE) (for example: make register SCOPE=project)"

install: ## Create the virtualenv and install dependencies
	uv sync

env: ## Create .env from .env.example if it is missing
	@if [ -f .env ]; then echo ".env already exists"; else cp .env.example .env && echo "Created .env; add your SPRITE_FUSION_API_KEY"; fi

build: install ## Build the wheel and sdist into dist/
	uv build

run: ## Run the server on stdio (for debugging)
	uv run sprite-fusion-mcp

check: ## Load the server and list its tools
	@uv run python -c "import asyncio; from sprite_fusion_mcp.server import mcp; tools = asyncio.run(mcp.list_tools()); print(len(tools), 'tools:', ', '.join(t.name for t in tools))"

credits: ## Check the API key by fetching the credit balance
	@uv run python -c "import asyncio; from sprite_fusion_mcp.client import SpriteFusionClient; print(asyncio.run(SpriteFusionClient().credits()))"

inspect: ## Open the server in the MCP Inspector (needs Node.js)
	npx @modelcontextprotocol/inspector uv run --directory "$(DIR)" sprite-fusion-mcp

register: install ## Register the server with Claude Code
	claude mcp add --scope $(SCOPE) $(NAME) -- uv run --directory "$(DIR)" sprite-fusion-mcp

unregister: ## Remove the server from Claude Code
	-claude mcp remove $(NAME) -s $(SCOPE)

reinstall: unregister register ## Re-register the server with Claude Code

status: ## Show the server's Claude Code registration and connection status
	claude mcp get $(NAME)

upgrade: ## Upgrade dependencies within pyproject.toml constraints
	uv lock --upgrade
	uv sync

clean: ## Remove build artifacts and caches
	rm -rf dist build
	find . -path ./.venv -prune -o -type d -name __pycache__ -exec rm -rf {} +

clean-output: ## Delete generated assets in output/ (requires CONFIRM=yes)
	@if [ "$(CONFIRM)" = "yes" ]; then rm -rf output && echo "Deleted output/"; else echo "This deletes all generated assets and history. Run: make clean-output CONFIRM=yes"; fi

distclean: clean ## Remove build artifacts and the virtualenv
	rm -rf .venv

uninstall: unregister distclean ## Unregister from Claude Code and remove the virtualenv
