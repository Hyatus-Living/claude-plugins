---
name: thread-finder
description: Search, index, and extract local Claude Code session history using Thread Finder hybrid search.
---

# Thread Finder

Use this skill when the user asks to search, inspect, extract, or update the local index of Claude Code sessions.

## Source Data

- Session transcripts: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
- Plugin-owned hybrid index: `~/.claude/plugins/data/thread-finder/index.sqlite`

Thread Finder only reads from `~/.claude/projects/`. It writes its derived chunks, embeddings, and FTS5 rows to its own index database.

## Defaults

- Embedding model: `text-embedding-3-large`
- Search mode: `hybrid`, combining vector cosine search with SQLite FTS5 BM25 through reciprocal rank fusion
- Update trigger: session files idle for at least 10 minutes (file mtime older than cutoff)
- Result count: 5

## MCP Tools

Prefer the MCP tools when available:

- `thread_search(query, limit=5, mode="hybrid", project=None)`
- `thread_index_sync(force=false, limit=null, project=None)`
- `thread_index_one(thread_id, force=false)`
- `thread_extract(thread_id, include_transcript=false)`
- `thread_finder_paths()`

Use `thread_search` in `hybrid` mode by default. Use `mode="text"` for exact IDs, paths, table names, stack traces, and function names. Use `mode="vector"` only when the user explicitly wants semantic-only matching.

Pass `project="accounting-analytics"` (or any substring of the project dir / cwd) to scope a search to one project. Project dir names are the encoded form Claude Code stores under `~/.claude/projects/`.

Use `thread_extract` after `thread_search` when the user wants to pull context from an older session into the current one. Return the `paste_ready_summary` to the user, and include `transcript` only when the user asks for the full readable transcript.

Tool calls (`tool_use`) and their outputs (`tool_result`) are included in the index, truncated, so queries about specific bash commands, file edits, or search results in past sessions will hit.

Sidechain (sub-agent) sessions are indexed and tagged via the `is_sidechain` field.

## CLI Usage

Update the index:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py index
```

Search:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "pricing deploy rollback" --mode hybrid
```

Scope a search to one project:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "rentals united import" --project accounting-analytics
```

Run one retrieval branch explicitly:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "multiunit_attributes 4699138" --mode text
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py search "that RU onboarding import session" --mode vector
```

Extract paste-ready context:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py extract SESSION_ID
```

Extract paste-ready context plus the full readable transcript:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py extract SESSION_ID --include-transcript
```

Re-index one session:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/thread_finder_core.py index --thread-id SESSION_ID --force
```
