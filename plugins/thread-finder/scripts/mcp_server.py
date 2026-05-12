#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from thread_finder_core import (
    DEFAULT_INDEX_DB,
    DEFAULT_MODEL,
    DEFAULT_PROJECTS_ROOT,
    extract_thread,
    index_threads,
    search_threads,
)


mcp = FastMCP("thread-finder")


@mcp.tool()
def thread_search(
    query: str,
    limit: int = 5,
    mode: str = "hybrid",
    project: str | None = None,
) -> list[dict]:
    """Search local Claude Code session transcripts with hybrid, vector, or text retrieval.

    Optionally filter results by project (substring match against the encoded project dir or decoded cwd).
    """
    args = argparse.Namespace(
        query=query,
        index_db=str(DEFAULT_INDEX_DB),
        model=None,
        limit=limit,
        mode=mode,
        candidate_limit=None,
        excerpt_chars=900,
        project=project,
    )
    return search_threads(args)


@mcp.tool()
def thread_index_sync(
    force: bool = False,
    limit: int | None = None,
    project: str | None = None,
) -> dict[str, int]:
    """Index completed or idle local Claude Code session transcripts into the vector database."""
    args = argparse.Namespace(
        projects_root=str(DEFAULT_PROJECTS_ROOT),
        index_db=str(DEFAULT_INDEX_DB),
        model=os.environ.get("THREAD_FINDER_EMBEDDING_MODEL", DEFAULT_MODEL),
        debounce_minutes=10,
        thread_id=None,
        project=project,
        limit=limit,
        force=force,
    )
    return index_threads(args)


@mcp.tool()
def thread_index_one(thread_id: str, force: bool = False) -> dict[str, int]:
    """Index or re-index one local Claude Code session by session ID (filename stem)."""
    args = argparse.Namespace(
        projects_root=str(DEFAULT_PROJECTS_ROOT),
        index_db=str(DEFAULT_INDEX_DB),
        model=os.environ.get("THREAD_FINDER_EMBEDDING_MODEL", DEFAULT_MODEL),
        debounce_minutes=0,
        thread_id=thread_id,
        project=None,
        limit=None,
        force=force,
    )
    return index_threads(args)


@mcp.tool()
def thread_extract(thread_id: str, include_transcript: bool = False) -> dict:
    """Return paste-ready context from a local Claude Code session transcript."""
    args = argparse.Namespace(
        thread_id=thread_id,
        projects_root=str(DEFAULT_PROJECTS_ROOT),
        include_transcript=include_transcript,
    )
    return extract_thread(args)


@mcp.tool()
def thread_finder_paths() -> dict[str, str]:
    """Return the local Claude Code projects root and thread-finder index paths."""
    return {
        "projects_root": str(DEFAULT_PROJECTS_ROOT),
        "index_db": str(DEFAULT_INDEX_DB),
        "embedding_model": os.environ.get("THREAD_FINDER_EMBEDDING_MODEL", DEFAULT_MODEL),
        "plugin_root": str(Path(__file__).resolve().parent.parent),
    }


if __name__ == "__main__":
    mcp.run()
