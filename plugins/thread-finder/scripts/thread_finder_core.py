#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator


DEFAULT_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
DEFAULT_INDEX_DB = Path.home() / ".claude" / "plugins" / "data" / "thread-finder" / "index.sqlite"
DEFAULT_MODEL = "text-embedding-3-large"
DEFAULT_DEBOUNCE_MINUTES = 10
MAX_CHUNK_CHARS = 7000
EMBED_BATCH_SIZE = 24
CHUNK_OVERLAP_CHARS = 600
RRF_K = 60
SEARCH_CANDIDATE_MULTIPLIER = 20
TOOL_USE_INPUT_LIMIT = 800
TOOL_RESULT_LIMIT = 1500
NOISE_USER_PREFIXES = (
    "<system-reminder>",
    "<environment_context>",
    "[Request interrupted",
    "Caveat: The messages below were generated",
    "<command-name>",
    "<local-command-stdout>",
)


@dataclass
class SessionRow:
    id: str
    title: str
    cwd: str
    project_dir: str
    transcript_path: Path
    created_at: int
    updated_at: int
    updated_at_ms: int
    is_sidechain: int
    first_user_message: str


@dataclass
class TranscriptMessage:
    role: str
    text: str
    timestamp: str | None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists threads (
            thread_id text primary key,
            title text not null,
            cwd text not null,
            project_dir text not null,
            transcript_path text not null,
            created_at integer not null,
            updated_at integer not null,
            updated_at_ms integer not null,
            is_sidechain integer not null,
            first_user_message text not null,
            summary text not null,
            content_hash text not null,
            embedding_model text not null,
            indexed_at integer not null
        );

        create table if not exists chunks (
            chunk_id text primary key,
            thread_id text not null,
            chunk_index integer not null,
            content text not null,
            embedding_json text not null,
            token_estimate integer not null,
            foreign key(thread_id) references threads(thread_id) on delete cascade
        );

        create table if not exists index_runs (
            id integer primary key autoincrement,
            started_at integer not null,
            finished_at integer,
            embedding_model text not null,
            scanned integer not null default 0,
            indexed integer not null default 0,
            skipped integer not null default 0
        );

        create index if not exists idx_chunks_thread_id on chunks(thread_id);
        create index if not exists idx_threads_updated_at on threads(updated_at desc);
        create index if not exists idx_threads_project_dir on threads(project_dir);

        create virtual table if not exists chunk_fts using fts5(
            chunk_id unindexed,
            thread_id unindexed,
            project_dir,
            title,
            cwd,
            content,
            tokenize = 'unicode61'
        );
        """
    )
    conn.execute(
        """
        insert into chunk_fts(chunk_id, thread_id, project_dir, title, cwd, content)
        select c.chunk_id, c.thread_id, t.project_dir, t.title, t.cwd, c.content
        from chunks c
        join threads t on t.thread_id = c.thread_id
        where not exists (
            select 1 from chunk_fts f where f.chunk_id = c.chunk_id
        )
        """
    )


def decode_project_dir(name: str) -> str:
    if not name:
        return name
    if name.startswith("-"):
        return "/" + name[1:].replace("-", "/")
    return name.replace("-", "/")


def parse_iso_timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp())
    except (ValueError, TypeError):
        return None


def extract_block_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = (block.get("text") or "").strip()
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "tool")
            raw_input = block.get("input", {})
            try:
                input_str = json.dumps(raw_input, separators=(",", ":"), default=str)
            except (TypeError, ValueError):
                input_str = str(raw_input)
            if len(input_str) > TOOL_USE_INPUT_LIMIT:
                input_str = input_str[:TOOL_USE_INPUT_LIMIT] + "..."
            parts.append(f"[tool_use {name}: {input_str}]")
        elif btype == "tool_result":
            raw = block.get("content", "")
            if isinstance(raw, list):
                pieces: list[str] = []
                for sub in raw:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        pieces.append(sub.get("text", ""))
                raw = "\n".join(pieces)
            elif not isinstance(raw, str):
                try:
                    raw = json.dumps(raw, separators=(",", ":"), default=str)
                except (TypeError, ValueError):
                    raw = str(raw)
            raw = raw.strip()
            if raw:
                if len(raw) > TOOL_RESULT_LIMIT:
                    raw = raw[:TOOL_RESULT_LIMIT] + "..."
                parts.append(f"[tool_result: {raw}]")
    return "\n\n".join(p for p in parts if p)


def parse_session(path: Path) -> tuple[SessionRow, list[TranscriptMessage]]:
    title = ""
    first_user_message = ""
    is_sidechain: bool | None = None
    cwd_from_events: str | None = None
    messages: list[TranscriptMessage] = []

    with path.open() as handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "ai-title" and event.get("aiTitle"):
                title = event["aiTitle"]
                continue
            if etype not in {"user", "assistant"}:
                continue
            if cwd_from_events is None and event.get("cwd"):
                cwd_from_events = event["cwd"]
            if is_sidechain is None and event.get("isSidechain") is not None:
                is_sidechain = bool(event.get("isSidechain"))
            msg = event.get("message") or {}
            role = msg.get("role") or etype
            if role not in {"user", "assistant"}:
                continue
            text = extract_block_text(msg.get("content"))
            if not text:
                continue
            if role == "user" and text.startswith(NOISE_USER_PREFIXES):
                continue
            messages.append(
                TranscriptMessage(role=role, text=text, timestamp=event.get("timestamp"))
            )
            if not first_user_message and role == "user":
                first_user_message = text

    stat = path.stat()
    project_dir = project_dir_for(path, DEFAULT_PROJECTS_ROOT)
    is_subagent_path = "subagents" in path.parts
    if is_subagent_path and is_sidechain is None:
        is_sidechain = True
    decoded_cwd = cwd_from_events or decode_project_dir(project_dir)
    first_ts = parse_iso_timestamp(messages[0].timestamp) if messages else None
    created_at = first_ts if first_ts is not None else int(stat.st_mtime)
    updated_at = int(stat.st_mtime)
    updated_at_ms = int(stat.st_mtime * 1000)
    display_title = title or shorten(first_user_message, 100) or "(untitled session)"

    row = SessionRow(
        id=path.stem,
        title=display_title,
        cwd=decoded_cwd,
        project_dir=project_dir,
        transcript_path=path,
        created_at=created_at,
        updated_at=updated_at,
        updated_at_ms=updated_at_ms,
        is_sidechain=int(bool(is_sidechain)),
        first_user_message=first_user_message,
    )
    return row, messages


def shorten(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def build_thread_text(row: SessionRow, messages: list[TranscriptMessage]) -> tuple[str, str, list[str]]:
    first_user = next((m.text for m in messages if m.role == "user"), row.first_user_message)
    final_assistant = next((m.text for m in reversed(messages) if m.role == "assistant"), "")
    summary = f"{shorten(first_user, 500)}"
    if final_assistant:
        summary = f"{summary}\nOutcome: {shorten(final_assistant, 700)}"

    header = (
        f"Session title: {row.title}\n"
        f"Session id: {row.id}\n"
        f"Working directory: {row.cwd}\n"
        f"Project dir: {row.project_dir}\n"
        f"Sidechain: {bool(row.is_sidechain)}\n"
        f"First user message: {shorten(first_user, 1200)}\n"
    )
    paragraphs = [header]
    for message in messages:
        paragraphs.append(f"{message.role.upper()}:\n{message.text.strip()}")

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for part in split_long_text(paragraph, MAX_CHUNK_CHARS, CHUNK_OVERLAP_CHARS):
            candidate = f"{current}\n\n{part}".strip() if current else part
            if len(candidate) <= MAX_CHUNK_CHARS:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    full_text = "\n\n".join(chunks)
    return summary, full_text, chunks


def split_long_text(text: str, limit: int, overlap: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        parts.append(text[start:end])
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return parts


def openai_embeddings(inputs: list[str], model: str) -> list[list[float]]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set. Export it in your shell or in the plugin .mcp.json env block."
        )
    request = urllib.request.Request(
        "https://api.openai.com/v1/embeddings",
        data=json.dumps({"model": model, "input": inputs}).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode()
        raise SystemExit(f"OpenAI embeddings request failed: HTTP {error.code}: {detail}") from error
    return [item["embedding"] for item in body["data"]]


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def connect_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    conn.commit()
    return conn


SESSION_NAME_RE = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|agent-[0-9a-f]+)$")


def iter_session_paths(projects_root: Path) -> Iterator[Path]:
    if not projects_root.exists():
        return
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        for path in sorted(project_dir.rglob("*.jsonl")):
            if not SESSION_NAME_RE.match(path.stem):
                # skip non-transcript files (e.g. skill-injections.jsonl)
                continue
            yield path


def project_dir_for(path: Path, projects_root: Path) -> str:
    try:
        return path.relative_to(projects_root).parts[0]
    except (ValueError, IndexError):
        return path.parent.name


def load_candidate_sessions(
    projects_root: Path,
    debounce_minutes: int,
    thread_id: str | None,
    limit: int | None,
    project: str | None,
) -> list[Path]:
    cutoff = time.time() - debounce_minutes * 60
    selected: list[tuple[float, Path]] = []
    for path in iter_session_paths(projects_root):
        if thread_id and path.stem != thread_id:
            continue
        if project and project not in path.parent.name and project not in decode_project_dir(path.parent.name):
            continue
        mtime = path.stat().st_mtime
        if not thread_id and mtime > cutoff:
            continue
        selected.append((mtime, path))
    selected.sort(key=lambda item: item[0], reverse=True)
    if limit:
        selected = selected[:limit]
    return [path for _, path in selected]


def load_session_by_id(projects_root: Path, thread_id: str) -> Path:
    for path in iter_session_paths(projects_root):
        if path.stem == thread_id:
            return path
    raise SystemExit(f"Session not found under {projects_root}: {thread_id}")


def render_transcript(messages: list[TranscriptMessage]) -> str:
    sections: list[str] = []
    for message in messages:
        header = message.role.upper()
        if message.timestamp:
            header += f" ({message.timestamp})"
        sections.append(f"## {header}\n\n{message.text.strip()}")
    return "\n\n".join(sections)


def is_conversational(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("[tool_result:") and "]" in stripped and stripped.count("[tool_") == 1:
        return False
    if stripped.startswith("[tool_use ") and "]" in stripped and stripped.count("[tool_") == 1:
        return False
    return True


def build_paste_ready_summary(row: SessionRow, messages: list[TranscriptMessage]) -> str:
    user_messages = [m.text.strip() for m in messages if m.role == "user" and is_conversational(m.text)]
    assistant_messages = [m.text.strip() for m in messages if m.role == "assistant" and is_conversational(m.text)]
    first_user = user_messages[0] if user_messages else row.first_user_message
    final_assistant = assistant_messages[-1] if assistant_messages else ""

    sections = [
        "# Imported Claude Code Session Context",
        "## Source Session",
        f"- Session ID: `{row.id}`",
        f"- Title: `{row.title}`",
        f"- Working directory: `{row.cwd}`",
        f"- Project dir: `{row.project_dir}`",
        f"- Transcript path: `{row.transcript_path}`",
        f"- Sidechain: `{bool(row.is_sidechain)}`",
        "",
        "## Original Request",
        first_user.strip(),
    ]

    followups = user_messages[1:]
    if followups:
        sections.append("")
        sections.append("## User Follow-ups")
        sections.extend(f"- {shorten(item, 500)}" for item in followups)

    if final_assistant:
        sections.extend(["", "## Final State / Outcome", final_assistant.strip()])

    if assistant_messages:
        sections.extend(["", "## Assistant Response Timeline"])
        for index, message in enumerate(assistant_messages, start=1):
            sections.append(f"{index}. {shorten(message, 900)}")

    sections.extend(
        [
            "",
            "## How To Use This In The Current Session",
            "Treat this as imported context from a prior Claude Code session. Re-check live code, DB rows, credentials, deploy state, and filesystem paths before treating any operational detail as current.",
        ]
    )
    return "\n".join(sections).strip()


def extract_thread(args: argparse.Namespace) -> dict[str, Any]:
    projects_root = Path(args.projects_root).expanduser()
    path = load_session_by_id(projects_root, args.thread_id)
    row, messages = parse_session(path)
    paste_ready_summary = build_paste_ready_summary(row, messages)
    result: dict[str, Any] = {
        "thread_id": row.id,
        "title": row.title,
        "cwd": row.cwd,
        "project_dir": row.project_dir,
        "transcript_path": str(row.transcript_path),
        "is_sidechain": bool(row.is_sidechain),
        "message_count": len(messages),
        "paste_ready_summary": paste_ready_summary,
    }
    if args.include_transcript:
        result["transcript"] = render_transcript(messages)
    return result


def index_threads(args: argparse.Namespace) -> dict[str, int]:
    model = args.model
    projects_root = Path(args.projects_root).expanduser()
    paths = load_candidate_sessions(
        projects_root,
        args.debounce_minutes,
        args.thread_id,
        args.limit,
        args.project,
    )
    conn = connect_index(Path(args.index_db).expanduser())
    run_id = conn.execute(
        "insert into index_runs(started_at, embedding_model) values(?, ?)",
        (int(time.time()), model),
    ).lastrowid
    scanned = indexed = skipped = 0
    try:
        for path in paths:
            scanned += 1
            row, messages = parse_session(path)
            if not messages:
                skipped += 1
                continue
            summary, full_text, chunks = build_thread_text(row, messages)
            content_hash = hash_text(full_text)
            existing = conn.execute(
                "select content_hash, embedding_model from threads where thread_id = ?",
                (row.id,),
            ).fetchone()
            if (
                existing
                and existing["content_hash"] == content_hash
                and existing["embedding_model"] == model
                and not args.force
            ):
                skipped += 1
                continue

            conn.execute("delete from chunks where thread_id = ?", (row.id,))
            conn.execute("delete from chunk_fts where thread_id = ?", (row.id,))
            embeddings: list[list[float]] = []
            for start in range(0, len(chunks), EMBED_BATCH_SIZE):
                embeddings.extend(openai_embeddings(chunks[start : start + EMBED_BATCH_SIZE], model))

            conn.execute(
                """
                insert into threads(
                    thread_id, title, cwd, project_dir, transcript_path,
                    created_at, updated_at, updated_at_ms, is_sidechain,
                    first_user_message, summary, content_hash, embedding_model, indexed_at
                )
                values(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(thread_id) do update set
                    title = excluded.title,
                    cwd = excluded.cwd,
                    project_dir = excluded.project_dir,
                    transcript_path = excluded.transcript_path,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    updated_at_ms = excluded.updated_at_ms,
                    is_sidechain = excluded.is_sidechain,
                    first_user_message = excluded.first_user_message,
                    summary = excluded.summary,
                    content_hash = excluded.content_hash,
                    embedding_model = excluded.embedding_model,
                    indexed_at = excluded.indexed_at
                """,
                (
                    row.id,
                    row.title,
                    row.cwd,
                    row.project_dir,
                    str(row.transcript_path),
                    row.created_at,
                    row.updated_at,
                    row.updated_at_ms,
                    row.is_sidechain,
                    row.first_user_message,
                    summary,
                    content_hash,
                    model,
                    int(time.time()),
                ),
            )
            for chunk_index, (chunk, embedding) in enumerate(zip(chunks, embeddings, strict=True)):
                chunk_id = f"{row.id}:{chunk_index}"
                conn.execute(
                    """
                    insert into chunks(chunk_id, thread_id, chunk_index, content, embedding_json, token_estimate)
                    values(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        row.id,
                        chunk_index,
                        chunk,
                        json.dumps(embedding, separators=(",", ":")),
                        max(1, len(chunk) // 4),
                    ),
                )
                conn.execute(
                    """
                    insert into chunk_fts(chunk_id, thread_id, project_dir, title, cwd, content)
                    values(?, ?, ?, ?, ?, ?)
                    """,
                    (chunk_id, row.id, row.project_dir, row.title, row.cwd, chunk),
                )
            conn.commit()
            indexed += 1
        return {"scanned": scanned, "indexed": indexed, "skipped": skipped}
    finally:
        conn.execute(
            """
            update index_runs
            set finished_at = ?, scanned = ?, indexed = ?, skipped = ?
            where id = ?
            """,
            (int(time.time()), scanned, indexed, skipped, run_id),
        )
        conn.commit()
        conn.close()


def cosine(left: list[float], right: list[float]) -> float:
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def fts_query(text: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_./:-]+", text)
    if not terms:
        return "\"\""
    quoted_terms: list[str] = []
    for term in terms[:12]:
        escaped = term.replace('"', '""')
        quoted_terms.append(f'"{escaped}"')
    return " OR ".join(quoted_terms)


def looks_exact_heavy(query: str) -> bool:
    exact_patterns = [
        r"`[^`]+`",
        r"\b[0-9]{4,}\b",
        r"\b[0-9a-f]{8}-[0-9a-f-]{13,}\b",
        r"[A-Za-z0-9]+_[A-Za-z0-9_]+",
        r"[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+",
        r"\.[A-Za-z0-9]{1,8}\b",
        r"[A-Za-z_][A-Za-z0-9_]*\(",
    ]
    return any(re.search(pattern, query) for pattern in exact_patterns)


def hybrid_weights(query: str) -> tuple[float, float]:
    if looks_exact_heavy(query):
        return 0.85, 1.35
    return 1.15, 1.0


def project_filter_sql(project: str | None) -> tuple[str, list[Any]]:
    if not project:
        return "", []
    return " and (t.project_dir like ? or t.cwd like ?) ", [f"%{project}%", f"%{project}%"]


def vector_candidates(
    conn: sqlite3.Connection,
    query: str,
    model: str,
    candidate_limit: int,
    excerpt_chars: int,
    project: str | None,
) -> list[dict[str, Any]]:
    query_embedding = openai_embeddings([query], model)[0]
    extra_sql, extra_params = project_filter_sql(project)
    rows = conn.execute(
        f"""
        select c.chunk_id, c.thread_id, c.chunk_index, c.content, c.embedding_json,
               t.title, t.cwd, t.project_dir, t.transcript_path, t.updated_at,
               t.is_sidechain, t.summary
        from chunks c
        join threads t on t.thread_id = c.thread_id
        where t.embedding_model = ? {extra_sql}
        """,
        [model, *extra_params],
    ).fetchall()

    scored: list[dict[str, Any]] = []
    for row in rows:
        score = cosine(query_embedding, json.loads(row["embedding_json"]))
        scored.append(search_result(row, score, "vector", excerpt_chars))
    scored.sort(key=lambda item: item["vector_score"], reverse=True)
    return scored[:candidate_limit]


def text_candidates(
    conn: sqlite3.Connection,
    query: str,
    candidate_limit: int,
    excerpt_chars: int,
    project: str | None,
) -> list[dict[str, Any]]:
    query_text = fts_query(query)
    if query_text == "\"\"":
        return []
    extra_sql, extra_params = project_filter_sql(project)
    rows = conn.execute(
        f"""
        select f.chunk_id, f.thread_id, c.chunk_index, c.content,
               t.title, t.cwd, t.project_dir, t.transcript_path, t.updated_at,
               t.is_sidechain, t.summary,
               bm25(chunk_fts) as bm25_score
        from chunk_fts f
        join chunks c on c.chunk_id = f.chunk_id
        join threads t on t.thread_id = f.thread_id
        where chunk_fts match ? {extra_sql}
        order by bm25_score
        limit ?
        """,
        [query_text, *extra_params, candidate_limit],
    ).fetchall()
    return [search_result(row, row["bm25_score"], "text", excerpt_chars) for row in rows]


def search_result(row: sqlite3.Row, score: float, branch: str, excerpt_chars: int) -> dict[str, Any]:
    result = {
        "score": score,
        "chunk_id": row["chunk_id"],
        "thread_id": row["thread_id"],
        "title": shorten(row["title"], 240),
        "cwd": row["cwd"],
        "project_dir": row["project_dir"],
        "transcript_path": row["transcript_path"],
        "updated_at": row["updated_at"],
        "is_sidechain": bool(row["is_sidechain"]),
        "summary": row["summary"],
        "matched_chunk_index": row["chunk_index"],
        "match_summary": shorten(row["content"], 420),
        "matched_excerpt": shorten(row["content"], excerpt_chars),
    }
    if branch == "vector":
        result["vector_score"] = score
    else:
        result["bm25_score"] = score
    return result


def rank_candidates(candidates: list[dict[str, Any]], score_key: str, reverse: bool) -> dict[str, int]:
    ranked = sorted(candidates, key=lambda item: item[score_key], reverse=reverse)
    return {item["chunk_id"]: rank for rank, item in enumerate(ranked, start=1)}


def best_by_thread(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_thread: dict[str, dict[str, Any]] = {}
    for item in candidates:
        existing = by_thread.get(item["thread_id"])
        if existing is None or item["score"] > existing["score"]:
            by_thread[item["thread_id"]] = item
        if len(by_thread) >= limit:
            break
    return list(by_thread.values())


def fuse_candidates(
    vector_items: list[dict[str, Any]],
    text_items: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    vector_ranks = rank_candidates(vector_items, "vector_score", True)
    text_ranks = rank_candidates(text_items, "bm25_score", False)
    vector_weight, text_weight = hybrid_weights(query)

    by_chunk: dict[str, dict[str, Any]] = {}
    for item in vector_items + text_items:
        chunk_id = item["chunk_id"]
        existing = by_chunk.get(chunk_id)
        if existing is None:
            existing = dict(item)
            by_chunk[chunk_id] = existing
        else:
            existing.update({key: value for key, value in item.items() if key.endswith("_score")})

        score = 0.0
        vector_rank = vector_ranks.get(chunk_id)
        text_rank = text_ranks.get(chunk_id)
        if vector_rank is not None:
            score += vector_weight / (RRF_K + vector_rank)
            existing["vector_rank"] = vector_rank
        if text_rank is not None:
            score += text_weight / (RRF_K + text_rank)
            existing["bm25_rank"] = text_rank
        existing["score"] = score
        existing["fusion"] = "rrf"
        existing["search_mode"] = "hybrid"

    ranked = sorted(by_chunk.values(), key=lambda item: item["score"], reverse=True)
    return best_by_thread(ranked, limit)


def search_threads(args: argparse.Namespace) -> list[dict[str, Any]]:
    mode = args.mode
    if mode not in {"hybrid", "vector", "text"}:
        raise SystemExit(f"Unsupported search mode: {mode}")
    candidate_limit = max(args.limit, args.candidate_limit or args.limit * SEARCH_CANDIDATE_MULTIPLIER)
    conn = connect_index(Path(args.index_db).expanduser())
    try:
        model_row = conn.execute(
            "select embedding_model from threads order by indexed_at desc limit 1"
        ).fetchone()
        model = args.model or (model_row["embedding_model"] if model_row else DEFAULT_MODEL)
        vector_items: list[dict[str, Any]] = []
        text_items: list[dict[str, Any]] = []
        if mode in {"hybrid", "vector"}:
            vector_items = vector_candidates(conn, args.query, model, candidate_limit, args.excerpt_chars, args.project)
        if mode in {"hybrid", "text"}:
            text_items = text_candidates(conn, args.query, candidate_limit, args.excerpt_chars, args.project)
    finally:
        conn.close()

    if mode == "vector":
        for item in vector_items:
            item["search_mode"] = "vector"
        return best_by_thread(vector_items, args.limit)
    if mode == "text":
        for rank, item in enumerate(text_items, start=1):
            item["bm25_rank"] = rank
            item["score"] = 1 / rank
            item["search_mode"] = "text"
        return best_by_thread(text_items, args.limit)
    return fuse_candidates(vector_items, text_items, args.query, args.limit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index and search local Claude Code sessions.")
    sub = parser.add_subparsers(dest="command", required=True)

    index_parser = sub.add_parser("index")
    index_parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT))
    index_parser.add_argument("--index-db", default=str(DEFAULT_INDEX_DB))
    index_parser.add_argument("--model", default=os.environ.get("THREAD_FINDER_EMBEDDING_MODEL", DEFAULT_MODEL))
    index_parser.add_argument("--debounce-minutes", type=int, default=DEFAULT_DEBOUNCE_MINUTES)
    index_parser.add_argument("--thread-id")
    index_parser.add_argument("--project")
    index_parser.add_argument("--limit", type=int)
    index_parser.add_argument("--force", action="store_true")
    index_parser.set_defaults(func=index_threads)

    search_parser = sub.add_parser("search")
    search_parser.add_argument("query")
    search_parser.add_argument("--index-db", default=str(DEFAULT_INDEX_DB))
    search_parser.add_argument("--model")
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.add_argument("--mode", choices=["hybrid", "vector", "text"], default="hybrid")
    search_parser.add_argument("--candidate-limit", type=int)
    search_parser.add_argument("--excerpt-chars", type=int, default=900)
    search_parser.add_argument("--project")
    search_parser.set_defaults(func=search_threads)

    extract_parser = sub.add_parser("extract")
    extract_parser.add_argument("thread_id")
    extract_parser.add_argument("--projects-root", default=str(DEFAULT_PROJECTS_ROOT))
    extract_parser.add_argument("--include-transcript", action="store_true")
    extract_parser.set_defaults(func=extract_thread)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = args.func(args)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
