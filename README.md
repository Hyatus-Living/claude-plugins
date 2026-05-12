# Hyatus Living — Claude Code Plugins

Dedicated source-of-truth repository for Claude Code plugins maintained by Hyatus Living. This is the Claude Code counterpart to [Hyatus-Living/custom-plugins](https://github.com/Hyatus-Living/custom-plugins) (the Codex plugin repo).

## Included plugins

- [`thread-finder`](plugins/thread-finder/) — hybrid BM25 + vector search over your local Claude Code session history (`~/.claude/projects/`). Includes a sub-agent-aware indexer, paste-ready context extractor, and macOS Keychain integration for the OpenAI API key.

## Repository layout

- `.claude-plugin/marketplace.json` — repo-local Claude Code marketplace manifest
- `plugins/<plugin>/` — plugin source

## Install

Add this repo as a marketplace and install the plugin:

```text
/plugin marketplace add Hyatus-Living/claude-plugins
/plugin install thread-finder@hyatus-claude-plugins
```

Or from a local clone:

```text
/plugin marketplace add ~/path/to/claude-plugins
/plugin install thread-finder@hyatus-claude-plugins
```

After install, run the included setup skill once to wire up the OpenAI API key (stored in macOS Keychain — nothing in plaintext on disk):

```text
/thread-finder-setup
```

Restart Claude Code so the MCP server picks up the new environment.

## Per-plugin requirements

### `thread-finder`

- **OS**: macOS (uses `security` CLI to read the OpenAI key from the login keychain)
- **Runtime**: [`uv`](https://github.com/astral-sh/uv) on `PATH`
- **API**: OpenAI embeddings (`text-embedding-3-large` by default; configurable via `THREAD_FINDER_EMBEDDING_MODEL`)
- **Data**: reads `~/.claude/projects/*.jsonl`, writes `~/.claude/plugins/data/thread-finder/index.sqlite`

See [plugins/thread-finder/README.md](plugins/thread-finder/README.md) for full details.

## License

UNLICENSED — internal use only.
