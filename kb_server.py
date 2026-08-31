#!/usr/bin/env python3
"""MCP personal knowledge base server.

Tools:
  - save_snippet: store a note/fact/bookmark with optional tags and source
  - search_kb: full-text search with relevance ranking (SQLite FTS5 + BM25)
  - list_snippets: list entries, optionally filtered by tag or source type
  - get_snippet: fetch one full entry by ID
  - delete_snippet: remove an entry by ID
  - list_tags: show every tag with entry counts
  - rename_tag: rename or merge a tag across all entries
  - ingest_notes: import .md/.txt notes from other agents' folders
  - kb_stats: summarize the knowledge base

Data is stored under MCP_KB_DIR (default: ~/MCPKnowledge) in a SQLite database.
SQLite's FTS5 extension provides ranked search; if it is unavailable the server
falls back to substring search automatically.

This server reads and writes only inside MCP_KB_DIR, plus any directories you
explicitly pass to ingest_notes. It never executes shell commands.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # pragma: no cover - compatibility with older SDK releases
    from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(os.environ.get("MCP_KB_DIR", str(Path.home() / "MCPKnowledge"))).expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "kb.db"

NOTE_SUFFIXES = {".md", ".markdown", ".txt"}
SKIP_DIR_NAMES = {".git", ".venv", "__pycache__", "node_modules"}
MAX_INGEST_BYTES = 1_000_000

mcp = FastMCP("knowledge-base")


# --------------------------------------------------------------------------- #
# Storage helpers
# --------------------------------------------------------------------------- #

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.Error:
        return False


def _init_db(conn: sqlite3.Connection) -> bool:
    """Create tables if needed. Returns True when FTS5 ranking is available."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snippets (
            id           TEXT PRIMARY KEY,
            title        TEXT NOT NULL DEFAULT '',
            content      TEXT NOT NULL,
            tags         TEXT NOT NULL DEFAULT '',
            source_url   TEXT NOT NULL DEFAULT '',
            source_path  TEXT NOT NULL DEFAULT '',
            source_type  TEXT NOT NULL DEFAULT 'manual',
            dedup_key    TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_tags ON snippets(tags)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snippets_source_type ON snippets(source_type)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_snippets_dedup ON snippets(dedup_key)")
    conn.commit()

    if not _fts_available(conn):
        return False

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
            title, content, tags,
            content='snippets',
            content_rowid='rowid',
            tokenize='porter unicode61'
        )
        """
    )
    # Triggers keep the index in sync automatically.
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS snippets_ai AFTER INSERT ON snippets BEGIN
            INSERT INTO snippets_fts(rowid, title, content, tags)
            VALUES (new.rowid, new.title, new.content, new.tags);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS snippets_ad AFTER DELETE ON snippets BEGIN
            INSERT INTO snippets_fts(snippets_fts, rowid, title, content, tags)
            VALUES ('delete', old.rowid, old.title, old.content, old.tags);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS snippets_au AFTER UPDATE ON snippets BEGIN
            INSERT INTO snippets_fts(snippets_fts, rowid, title, content, tags)
            VALUES ('delete', old.rowid, old.title, old.content, old.tags);
            INSERT INTO snippets_fts(rowid, title, content, tags)
            VALUES (new.rowid, new.title, new.content, new.tags);
        END
        """
    )
    conn.commit()
    return True


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO snippets_fts(snippets_fts) VALUES('rebuild')")
    conn.commit()


def _normalize_tags(tags: Any) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[,;]+", tags)
    elif isinstance(tags, (list, tuple)):
        raw = [str(t) for t in tags]
    else:
        return []
    cleaned = []
    for tag in raw:
        tag = tag.strip().lower().strip("#")
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return cleaned[:30]


def _tags_str(tags: Any) -> str:
    return ",".join(_normalize_tags(tags))


def _split_tags(value: str) -> list[str]:
    return [t for t in (value or "").split(",") if t]


def _content_hash(content: str) -> str:
    return hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()[:32]


def _row_to_dict(row: sqlite3.Row, content_chars: int | None = None) -> dict[str, Any]:
    content = row["content"]
    truncated = content_chars is not None and len(content) > content_chars
    return {
        "id": row["id"],
        "title": row["title"],
        "content": content[:content_chars] + "..." if truncated else content,
        "content_truncated": truncated,
        "content_chars": len(content),
        "tags": _split_tags(row["tags"]),
        "source_url": row["source_url"],
        "source_path": row["source_path"],
        "source_type": row["source_type"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _find_snippet(conn: sqlite3.Connection, snippet_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM snippets WHERE id = ?", (snippet_id,)).fetchone()


def _derive_title(content: str, fallback: str) -> str:
    for line in (content or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return fallback


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool()
def save_snippet(
    content: str,
    title: str = "",
    tags: str = "",
    source_url: str = "",
    source_path: str = "",
    source_type: str = "manual",
) -> dict[str, Any]:
    """Save a note, fact, quote, or bookmark to the knowledge base.

    Args:
        content: The text to store. Required.
        title: Optional short title. Derived from the first line if omitted.
        tags: Comma-separated tags such as 'python, mcp, research'.
        source_url: Optional URL this came from.
        source_path: Optional local file path this came from.
        source_type: One of manual, web, file, idea, bookmark.
    """
    try:
        if not (content or "").strip():
            return {"saved": False, "error": "content must not be empty."}

        allowed = {"manual", "web", "file", "idea", "bookmark"}
        if source_type not in allowed:
            return {"saved": False, "error": f"source_type must be one of: {', '.join(sorted(allowed))}"}

        now = _now()
        title = (title or "").strip() or _derive_title(content, "Untitled")
        dedup_key = f"manual:{_content_hash(content)}"

        with _connect() as conn:
            _init_db(conn)
            existing = conn.execute(
                "SELECT id FROM snippets WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if existing:
                return {
                    "saved": False,
                    "duplicate_of": existing["id"],
                    "message": "Identical content already exists; not saved again.",
                }

            snippet_id = f"kb-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO snippets
                    (id, title, content, tags, source_url, source_path,
                     source_type, dedup_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snippet_id,
                    title,
                    content,
                    _tags_str(tags),
                    (source_url or "").strip(),
                    (source_path or "").strip(),
                    source_type,
                    dedup_key,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = _find_snippet(conn, snippet_id)
            return {"saved": True, "snippet": _row_to_dict(row), "data_dir": str(DATA_DIR)}
    except sqlite3.Error as exc:
        return {"saved": False, "error": f"database error: {exc}"}


@mcp.tool()
def search_kb(
    query: str,
    tag: str = "",
    limit: int = 8,
    content_chars: int = 600,
) -> dict[str, Any]:
    """Search the knowledge base and return the most relevant entries first.

    Args:
        query: Search terms. Multiple words are ANDed. Use quotes for an exact phrase.
        tag: Optional tag to restrict results to.
        limit: Maximum results to return, from 1 to 50.
        content_chars: Characters of content to include per result, from 50 to 5000.
    """
    try:
        query = (query or "").strip()
        if not query:
            return {"results": [], "count": 0, "error": "query must not be empty."}

        limit = max(1, min(int(limit or 8), 50))
        content_chars = max(50, min(int(content_chars or 600), 5000))
        tag = (tag or "").strip().lower().strip("#")

        with _connect() as conn:
            use_fts = _init_db(conn)
            params: list[Any] = []
            used_fts = False

            if use_fts:
                # Preserve the user's raw query first so FTS5 operators (OR, AND,
                # NOT, "phrases", prefix*) keep working. Only if SQLite rejects the
                # syntax do we retry with every term quoted as a literal phrase.
                terms = re.findall(r'"[^"]+"|[^\s"]+', query)
                quoted = " ".join(t if t.startswith('"') else f'"{t.strip(chr(34))}"' for t in terms)

                where: list[str] = []
                extra: list[Any] = []
                if tag:
                    where.append("(',' || s.tags || ',') LIKE ?")
                    extra.append(f"%,{tag},%")
                tail = (" AND " + " AND ".join(where) if where else "") + " ORDER BY rank LIMIT ?"

                for candidate in (query, quoted):
                    if not candidate:
                        continue
                    try:
                        rows = conn.execute(
                            """
                            SELECT s.*, bm25(snippets_fts) AS rank
                            FROM snippets_fts
                            JOIN snippets s ON s.rowid = snippets_fts.rowid
                            WHERE snippets_fts MATCH ?"""
                            + tail,
                            [candidate, *extra, limit],
                        ).fetchall()
                        used_fts = True
                        break
                    except sqlite3.OperationalError:
                        rows = []

            if not used_fts:
                like = f"%{query}%"
                sql = """
                    SELECT *, 0 AS rank FROM snippets
                    WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?)
                """
                params = [like, like, like]
                if tag:
                    sql += " AND (',' || tags || ',') LIKE ?"
                    params.append(f"%,{tag},%")
                sql += " ORDER BY updated_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()

            return {
                "query": query,
                "tag": tag,
                "count": len(rows),
                "search_mode": "fts5_bm25" if used_fts else "substring",
                "data_dir": str(DATA_DIR),
                "results": [
                    {**_row_to_dict(r, content_chars), "score": round(r["rank"], 6)}
                    for r in rows
                ],
            }
    except sqlite3.Error as exc:
        return {"results": [], "count": 0, "error": f"database error: {exc}"}


@mcp.tool()
def list_snippets(
    tag: str = "",
    source_type: str = "",
    limit: int = 20,
    content_chars: int = 200,
) -> dict[str, Any]:
    """List knowledge base entries, most recently updated first.

    Args:
        tag: Optional tag filter.
        source_type: Optional filter: manual, web, file, idea, bookmark.
        limit: Maximum entries to return, from 1 to 200.
        content_chars: Characters of content to include per entry, from 50 to 5000.
    """
    try:
        limit = max(1, min(int(limit or 20), 200))
        content_chars = max(50, min(int(content_chars or 200), 5000))
        tag = (tag or "").strip().lower().strip("#")
        source_type = (source_type or "").strip()

        with _connect() as conn:
            _init_db(conn)
            sql = "SELECT * FROM snippets WHERE 1=1"
            params: list[Any] = []
            if tag:
                sql += " AND (',' || tags || ',') LIKE ?"
                params.append(f"%,{tag},%")
            if source_type:
                sql += " AND source_type = ?"
                params.append(source_type)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return {
                "count": len(rows),
                "tag": tag,
                "source_type": source_type,
                "data_dir": str(DATA_DIR),
                "snippets": [_row_to_dict(r, content_chars) for r in rows],
            }
    except sqlite3.Error as exc:
        return {"count": 0, "snippets": [], "error": f"database error: {exc}"}


@mcp.tool()
def get_snippet(snippet_id: str) -> dict[str, Any]:
    """Return one full knowledge base entry including its complete content.

    Args:
        snippet_id: Entry ID such as 'kb-1a2b3c4d'.
    """
    try:
        with _connect() as conn:
            _init_db(conn)
            row = _find_snippet(conn, (snippet_id or "").strip())
            if not row:
                return {"found": False, "error": f"No snippet with id '{snippet_id}'."}
            return {"found": True, "snippet": _row_to_dict(row), "data_dir": str(DATA_DIR)}
    except sqlite3.Error as exc:
        return {"found": False, "error": f"database error: {exc}"}


@mcp.tool()
def delete_snippet(snippet_id: str) -> dict[str, Any]:
    """Delete a knowledge base entry by ID.

    Args:
        snippet_id: Entry ID such as 'kb-1a2b3c4d'.
    """
    try:
        with _connect() as conn:
            _init_db(conn)
            row = _find_snippet(conn, (snippet_id or "").strip())
            if not row:
                return {"deleted": False, "error": f"No snippet with id '{snippet_id}'."}
            conn.execute("DELETE FROM snippets WHERE id = ?", (row["id"],))
            conn.commit()
            return {"deleted": True, "id": row["id"], "title": row["title"]}
    except sqlite3.Error as exc:
        return {"deleted": False, "error": f"database error: {exc}"}


@mcp.tool()
def list_tags() -> dict[str, Any]:
    """List every tag in the knowledge base with the number of entries using it."""
    try:
        counts: dict[str, int] = {}
        with _connect() as conn:
            _init_db(conn)
            for row in conn.execute("SELECT tags FROM snippets").fetchall():
                for tag in _split_tags(row["tags"]):
                    counts[tag] = counts.get(tag, 0) + 1
        return {
            "count": len(counts),
            "data_dir": str(DATA_DIR),
            "tags": sorted(
                ({"tag": t, "entries": n} for t, n in counts.items()),
                key=lambda item: (-item["entries"], item["tag"]),
            ),
        }
    except sqlite3.Error as exc:
        return {"count": 0, "tags": [], "error": f"database error: {exc}"}


@mcp.tool()
def rename_tag(old_tag: str, new_tag: str) -> dict[str, Any]:
    """Rename a tag, or merge one tag into another, across all entries.

    Args:
        old_tag: Existing tag to change.
        new_tag: Replacement tag. Entries already using it are merged, not duplicated.
    """
    try:
        old_tag = (old_tag or "").strip().lower().strip("#")
        new_tag = (new_tag or "").strip().lower().strip("#")
        if not old_tag or not new_tag:
            return {"renamed": False, "error": "Both old_tag and new_tag are required."}

        updated = 0
        now = _now()
        with _connect() as conn:
            _init_db(conn)
            for row in conn.execute("SELECT id, tags FROM snippets").fetchall():
                tags = _split_tags(row["tags"])
                if old_tag not in tags:
                    continue
                tags = [new_tag if t == old_tag else t for t in tags]
                merged = _normalize_tags(tags)
                conn.execute(
                    "UPDATE snippets SET tags = ?, updated_at = ? WHERE id = ?",
                    (",".join(merged), now, row["id"]),
                )
                updated += 1
            conn.commit()
        return {
            "renamed": updated > 0,
            "old_tag": old_tag,
            "new_tag": new_tag,
            "entries_updated": updated,
        }
    except sqlite3.Error as exc:
        return {"renamed": False, "error": f"database error: {exc}"}


@mcp.tool()
def ingest_notes(directories: list[str], tag: str = "", recursive: bool = True) -> dict[str, Any]:
    """Import Markdown and text notes into the knowledge base so they become searchable.

    Use this to pull in notes written by the other agents, for example
    ~/MCPWebResearch/notes, ~/MCPPlanner, and ~/MCPHealth. Re-running is safe:
    unchanged files are skipped and changed files are updated in place.

    Args:
        directories: List of folder paths to scan.
        tag: Optional tag applied to every imported entry.
        recursive: Whether to descend into subfolders. Skips .git, .venv, node_modules.
    """
    try:
        if not directories:
            return {"ingested": 0, "error": "directories must not be empty."}

        base_tags = _normalize_tags(tag)
        imported = 0
        updated = 0
        skipped = 0
        errors: list[str] = []

        with _connect() as conn:
            _init_db(conn)
            now = _now()

            for raw_dir in directories:
                folder = Path(str(raw_dir)).expanduser()
                if not folder.exists():
                    errors.append(f"missing: {folder}")
                    continue
                if folder.is_file():
                    folder = folder.parent

                files = folder.rglob("*") if recursive else folder.glob("*")
                for path in sorted(files):
                    if not path.is_file() or path.suffix.lower() not in NOTE_SUFFIXES:
                        continue
                    if any(part in SKIP_DIR_NAMES for part in path.parts):
                        continue
                    if path == DB_PATH:
                        continue
                    try:
                        if path.stat().st_size > MAX_INGEST_BYTES:
                            skipped += 1
                            continue
                        content = path.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError as exc:
                        errors.append(f"{path}: {exc}")
                        continue

                    if not content:
                        skipped += 1
                        continue

                    source_path = str(path)
                    dedup_key = f"file:{source_path}:{_content_hash(content)}"
                    tags = _normalize_tags(base_tags + [path.suffix.lower().lstrip(".")])

                    existing_path = conn.execute(
                        "SELECT id, dedup_key FROM snippets WHERE source_path = ?",
                        (source_path,),
                    ).fetchone()

                    if existing_path and existing_path["dedup_key"] == dedup_key:
                        skipped += 1
                        continue

                    title = _derive_title(content, path.stem)
                    if existing_path:
                        conn.execute(
                            """
                            UPDATE snippets
                               SET title = ?, content = ?, tags = ?,
                                   dedup_key = ?, updated_at = ?
                             WHERE id = ?
                            """,
                            (title, content, ",".join(tags), dedup_key, now, existing_path["id"]),
                        )
                        updated += 1
                    else:
                        conn.execute(
                            """
                            INSERT INTO snippets
                                (id, title, content, tags, source_url, source_path,
                                 source_type, dedup_key, created_at, updated_at)
                            VALUES (?, ?, ?, ?, '', ?, 'file', ?, ?, ?)
                            """,
                            (
                                f"kb-{uuid.uuid4().hex[:8]}",
                                title,
                                content,
                                ",".join(tags),
                                source_path,
                                dedup_key,
                                now,
                                now,
                            ),
                        )
                        imported += 1
            conn.commit()

        return {
            "imported": imported,
            "updated": updated,
            "skipped_unchanged": skipped,
            "errors": errors,
            "directories_scanned": [str(Path(str(d)).expanduser()) for d in directories],
            "data_dir": str(DATA_DIR),
        }
    except sqlite3.Error as exc:
        return {"ingested": 0, "error": f"database error: {exc}"}


@mcp.tool()
def kb_stats() -> dict[str, Any]:
    """Summarize the knowledge base: entry counts, tags, and source breakdown."""
    try:
        with _connect() as conn:
            use_fts = _init_db(conn)
            total = conn.execute("SELECT COUNT(*) AS n FROM snippets").fetchone()["n"]
            by_source = {
                r["source_type"]: r["n"]
                for r in conn.execute(
                    "SELECT source_type, COUNT(*) AS n FROM snippets GROUP BY source_type"
                ).fetchall()
            }
            tag_counts: dict[str, int] = {}
            chars = 0
            for row in conn.execute("SELECT tags, content FROM snippets").fetchall():
                chars += len(row["content"])
                for tag in _split_tags(row["tags"]):
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            newest = conn.execute(
                "SELECT created_at FROM snippets ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if use_fts:
                _rebuild_fts(conn)

        return {
            "data_dir": str(DATA_DIR),
            "db_path": str(DB_PATH),
            "search_backend": "fts5_bm25" if use_fts else "substring_fallback",
            "total_entries": total,
            "total_content_chars": chars,
            "by_source_type": by_source,
            "unique_tags": len(tag_counts),
            "top_tags": sorted(
                ({"tag": t, "entries": n} for t, n in tag_counts.items()),
                key=lambda item: (-item["entries"], item["tag"]),
            )[:10],
            "newest_entry_at": newest["created_at"] if newest else None,
        }
    except sqlite3.Error as exc:
        return {"total_entries": 0, "error": f"database error: {exc}"}


@mcp.resource("kb://data-directory")
def data_directory() -> str:
    """Return the knowledge base data directory."""
    return str(DATA_DIR)


if __name__ == "__main__":
    mcp.run(transport="stdio")
