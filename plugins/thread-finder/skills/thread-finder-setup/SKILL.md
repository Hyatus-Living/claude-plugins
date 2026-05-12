---
name: thread-finder-setup
description: One-time configuration for the Thread Finder plugin. Use this skill when the user says "set up thread-finder", "configure thread-finder", "thread-finder isn't working", "thread-finder needs an OpenAI key", or when a Thread Finder MCP tool call fails with a missing-OPENAI_API_KEY error.
---

# Thread Finder Setup

This skill stores the OpenAI API key for Thread Finder in the macOS login keychain and verifies the MCP server is reachable.

## Why a key is required

Thread Finder runs hybrid BM25 + vector search. The vector half calls the OpenAI embeddings API (`text-embedding-3-large` by default), which needs an API key. BM25-only search (`mode="text"`) works without a key.

## Storage location

- Service name: `thread-finder-openai`
- Account: the current `$USER`
- Backing store: macOS login keychain (encrypted at rest, gated by your login session)
- Fetched at MCP server startup by [scripts/launch.sh](../../scripts/launch.sh)

## Setup steps

1. **Check whether a key is already stored:**

   ```bash
   security find-generic-password -a "$USER" -s thread-finder-openai -w
   ```

   - If it prints a key, you're done — skip to "Verify".
   - If it errors with "The specified item could not be found in the keychain", continue to step 2.

2. **Ask the user for the OpenAI API key.** Tell them: "Paste your OpenAI API key (starts with `sk-`). It will be stored in your macOS login keychain — nothing is written to disk in plaintext." Do not echo the key back.

3. **Store the key:**

   ```bash
   security add-generic-password -a "$USER" -s thread-finder-openai -w '<KEY>'
   ```

   If the user already has a key stored under this service and wants to overwrite, add `-U`:

   ```bash
   security add-generic-password -U -a "$USER" -s thread-finder-openai -w '<KEY>'
   ```

4. **Restart guidance.** Tell the user: "Restart Claude Code so the Thread Finder MCP server picks up the new key. After that, `thread_search` will work." The running MCP server holds the env it was launched with — it won't see the new key without a restart.

## Verify

After restart, call:

```
thread_finder_paths()
```

This is a no-key tool — confirms the MCP server is running. Then run a small smoke test:

```
thread_search("test", limit=1, mode="text")
```

`mode="text"` skips the embedding API, so this confirms the index is reachable without spending tokens. If you want to confirm the key works, run the same with `mode="hybrid"`.

## Removing the key

```bash
security delete-generic-password -a "$USER" -s thread-finder-openai
```

## Alternative storage paths (not used by default)

- **Plaintext `.mcp.json` env block**: edit [.mcp.json](../../.mcp.json) and add `"OPENAI_API_KEY": "sk-..."` inside `mcpServers.thread-finder.env`. Simple but the key sits in plaintext on disk and may leak if the plugin dir is shared or committed.
- **Shell rc**: `export OPENAI_API_KEY=...` in `~/.zshrc`. Visible to every process running as the user. Works but less contained than keychain.
- **No key at all**: use `mode="text"` exclusively. No vector search, just BM25 — surprisingly capable for technical content (IDs, paths, function names).
