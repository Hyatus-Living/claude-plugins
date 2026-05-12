#!/usr/bin/env bash
# Launch wrapper for the thread-finder MCP server.
# Fetches OPENAI_API_KEY from the macOS login keychain if it isn't already in the
# environment, then execs the Python MCP server under uv.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  if key=$(security find-generic-password -a "$USER" -s thread-finder-openai -w 2>/dev/null); then
    export OPENAI_API_KEY="$key"
  fi
fi

exec uv run --with mcp python "$SCRIPT_DIR/mcp_server.py"
