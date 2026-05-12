# Thread Finder (Claude Code edition)

Thread Finder makes your local Claude Code session history reusable. It scans `~/.claude/projects/`, indexes the JSONL transcripts (including tool calls and tool results), and searches them with hybrid BM25 + vector retrieval. It can also extract a prior session into paste-ready context for the current conversation.

This is a port of the Codex thread-finder plugin, rewritten against Claude Code's filesystem-based session storage.

## What It Indexes

- All `*.jsonl` files under `~/.claude/projects/<encoded-cwd>/`
- `user` and `assistant` events, with text, `tool_use` (name + truncated input), and `tool_result` (truncated content) blocks. `thinking` blocks are skipped.
- Sidechain (sub-agent) sessions are included and tagged via `is_sidechain`.

Sessions are eligible for indexing once their transcript file mtime is older than 10 minutes (avoid re-indexing the live session).

## Install

This plugin lives in a local marketplace at `~/.claude/plugins/marketplaces/local/`. Add the marketplace and install:

```text
/plugin marketplace add ~/.claude/plugins/marketplaces/local
/plugin install thread-finder@local
```

Then set your OpenAI key (used for embeddings) — either in your shell or in the plugin's `.mcp.json` `env` block:

```bash
export OPENAI_API_KEY=sk-...
```

Restart the Claude Code session so the MCP server picks up the new env.

## MCP Tools

### `thread_search(query, limit=5, mode="hybrid", project=None)`

Searches indexed local sessions and returns the best matches. Modes:

- `hybrid` (default): vector cosine + FTS5/BM25, fused with reciprocal rank fusion.
- `text`: FTS5/BM25 only. Use for exact IDs, filenames, paths, table names, function names.
- `vector`: embedding cosine only. Use for vague, semantic queries.

`project` is a substring filter against the encoded project dir or the decoded cwd.

### `thread_index_sync(force=false, limit=null, project=None)`

Indexes idle local sessions into the vector database. `force=true` re-embeds even unchanged content.

### `thread_index_one(thread_id, force=false)`

Indexes or re-indexes one session by ID (filename stem).

### `thread_extract(thread_id, include_transcript=false)`

Returns paste-ready context for a session:

- `thread_id`, `title`, `cwd`, `project_dir`, `transcript_path`, `is_sidechain`
- `message_count`, `paste_ready_summary`
- `transcript` when `include_transcript=true`

### `thread_finder_paths()`

Returns the projects root, vector index path, embedding model, and plugin root.

## CLI Usage

Index sessions:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py index
```

Search sessions:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "rentals united import" --mode hybrid
```

Scope to a project:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "rentals united import" --project accounting-analytics
```

Exact-term search without embeddings:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "multiunit_attributes 4699138" --mode text
```

Extract paste-ready context:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py extract SESSION_ID
```

Re-index one session:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py index --thread-id SESSION_ID --force
```

## Data Sources & Storage

- Read from: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
- Written to: `~/.claude/plugins/data/thread-finder/index.sqlite`

The plugin does not modify any Claude Code session file.

## Differences from the Codex Version

| | Codex | Claude Code |
|---|---|---|
| Metadata source | `state_5.sqlite` `threads` table | Filesystem scan of `~/.claude/projects/` |
| Title | DB column | `type:"ai-title"` event in the JSONL |
| cwd | DB column | Decoded from the project dir name (best effort), or from a `cwd` field on any event |
| Archived flag | Yes | Replaced with `is_sidechain` |
| Content blocks | `input_text` / `output_text` | `text`, `tool_use`, `tool_result` (truncated); `thinking` skipped |
| OpenAI key | `~/.codex/.env` | `OPENAI_API_KEY` env var |

## Pulling Context Into The Current Session

Recommended workflow:

1. `thread_search` with the topic the user remembers.
2. Pick the matching `thread_id` (session ID).
3. `thread_extract(thread_id)`.
4. Return the `paste_ready_summary` in the current session.
5. If the user asks for the full transcript, run `thread_extract(thread_id, include_transcript=true)`.

Treat extracted context as historical. Re-check live code, DB rows, credentials, deploy state, and filesystem paths before treating any operational detail as current.
