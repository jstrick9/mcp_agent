"""End-to-end check: spawn each MCP server over stdio and actually call its tools.

Run with the project venv:
    ./.venv/bin/python tests/e2e_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parent.parent


def txt(result) -> str:
    """Flatten an MCP CallToolResult into plain text."""
    parts = []
    for block in result.content:
        if getattr(block, "type", "") == "text":
            parts.append(block.text)
    return "\n".join(parts)


async def run_server(script: str, env: dict, plan: list[tuple[str, dict]]):
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "python"),
        args=[str(REPO / script)],
        env={**os.environ, **env},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"  [{script}] {len(names)} tools: {', '.join(names)}")
            results = []
            for tool, args in plan:
                res = await session.call_tool(tool, args)
                body = txt(res)
                status = "ERROR" if res.is_error else "ok"
                results.append((tool, status, res.is_error))
                print(f"    -> {tool}() {status}: {body[:220]}")
            return names, results


KB_TOOLS = {
    "save_snippet",
    "search_kb",
    "list_snippets",
    "get_snippet",
    "delete_snippet",
    "list_tags",
    "rename_tag",
    "ingest_notes",
    "kb_stats",
}


async def test_kb(tmp: Path) -> int:
    """Exercise the knowledge base, asserting on ranking, dedup, and idempotency."""
    failures = 0

    def check(label: str, condition: bool) -> None:
        nonlocal failures
        print(f"    {'ok  ' if condition else 'FAIL'}  {label}")
        if not condition:
            failures += 1

    # Fixture notes shaped like the ones the other agents write.
    notes = tmp / "kb-notes"
    (notes / "sub").mkdir(parents=True, exist_ok=True)
    (notes / "mcp-architecture.md").write_text(
        "# MCP transport notes\n\n"
        "The Model Context Protocol server speaks JSON-RPC over a stdio transport.\n"
        "Clients spawn the server as a subprocess and exchange newline-delimited messages.\n",
        encoding="utf-8",
    )
    (notes / "sourdough.md").write_text(
        "# Sourdough starter\n\nFeed the starter weekly and discard half before feeding.\n",
        encoding="utf-8",
    )
    (notes / "sub" / "nested-stdio-note.md").write_text(
        "# Nested stdio note\n\nA deeper file that also mentions the stdio transport.\n",
        encoding="utf-8",
    )

    kb_dir = tmp / "kb"
    params = StdioServerParameters(
        command=str(REPO / ".venv" / "bin" / "python"),
        args=[str(REPO / "kb_server.py")],
        env={**os.environ, "MCP_KB_DIR": str(kb_dir)},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"  [kb_server.py] {len(names)} tools: {', '.join(names)}")
            check("all 9 kb tools exposed", KB_TOOLS == set(names))

            async def call(tool: str, args: dict) -> dict:
                res = await session.call_tool(tool, args)
                check(f"{tool}() did not error", not res.is_error)
                return json.loads(txt(res))

            # --- save + dedup ---
            saved = await call(
                "save_snippet",
                {
                    "content": "FastMCP exposes Python functions as MCP tools via a decorator.",
                    "title": "FastMCP decorator",
                    "tags": "python, mcp",
                    "source_type": "idea",
                },
            )
            check("save_snippet saved", saved.get("saved") is True)
            snippet_id = saved["snippet"]["id"]
            check("tags normalised to list", saved["snippet"]["tags"] == ["python", "mcp"])

            dupe = await call(
                "save_snippet",
                {"content": "FastMCP exposes Python functions as MCP tools via a decorator."},
            )
            check("identical content is deduplicated", dupe.get("saved") is False and dupe.get("duplicate_of") == snippet_id)

            bad_type = await call("save_snippet", {"content": "x", "source_type": "nonsense"})
            check("invalid source_type rejected", bad_type.get("saved") is False)

            empty = await call("save_snippet", {"content": "   "})
            check("empty content rejected", empty.get("saved") is False)

            # --- search ranking ---
            hit = await call("search_kb", {"query": "FastMCP decorator"})
            check("search finds the saved snippet", hit["count"] >= 1 and hit["results"][0]["id"] == snippet_id)
            check("search reports fts5_bm25 backend", hit["search_mode"] == "fts5_bm25")
            check("result carries a bm25 score", isinstance(hit["results"][0].get("score"), float))

            miss = await call("search_kb", {"query": "zzzznotaword"})
            check("unmatched query returns zero results", miss["count"] == 0)

            tagged = await call("search_kb", {"query": "FastMCP", "tag": "mcp"})
            check("tag filter narrows results", tagged["count"] >= 1)
            off_tag = await call("search_kb", {"query": "FastMCP", "tag": "cooking"})
            check("non-matching tag filter excludes result", off_tag["count"] == 0)

            # --- ingest: pulls in the other agents' notes ---
            ing = await call("ingest_notes", {"directories": [str(notes)], "tag": "imported"})
            check("ingest imported 3 files (incl. nested)", ing["imported"] == 3)

            again = await call("ingest_notes", {"directories": [str(notes)], "tag": "imported"})
            check("re-ingest is idempotent", again["imported"] == 0 and again["skipped_unchanged"] == 3)

            # FTS5 operators must keep working, and malformed input must degrade
            # gracefully instead of raising. Compare OR against its own AND
            # counterpart so the assertion does not depend on unrelated fixtures.
            and_query = await call("search_kb", {"query": "FastMCP AND sourdough"})
            or_query = await call("search_kb", {"query": "FastMCP OR sourdough"})
            check(
                "OR operator widens the result set beyond AND",
                or_query["search_mode"] == "fts5_bm25"
                and and_query["count"] == 0
                and or_query["count"] >= 2,
            )
            or_titles = " ".join(r["title"] for r in or_query["results"])
            check("OR returns matches for both terms", "FastMCP" in or_titles and "Sourdough" in or_titles)

            phrase = await call("search_kb", {"query": '"stdio transport"'})
            check("quoted phrase search works", phrase["count"] >= 1)

            weird = await call("search_kb", {"query": "mcp &&& )))"})
            check("malformed FTS5 syntax degrades without error", "error" not in weird)
            check("malformed input still searches (quoted fallback)", weird["count"] >= 1)

            ranked = await call("search_kb", {"query": "stdio transport"})
            titles = [r["title"] for r in ranked["results"]]
            check("ranked search surfaces the stdio notes", len(titles) >= 2 and any("MCP transport" in t for t in titles))
            check("irrelevant note not returned for stdio", not any("Sourdough" in t for t in titles))

            # Changing a source file should update in place, not duplicate.
            (notes / "sourdough.md").write_text(
                "# Sourdough starter\n\nFeed weekly. Hydration 100 percent.\n", encoding="utf-8"
            )
            changed = await call("ingest_notes", {"directories": [str(notes)]})
            check("changed file updated in place", changed["updated"] == 1 and changed["imported"] == 0)

            gone = await call("ingest_notes", {"directories": [str(tmp / "does-not-exist")]})
            check("missing directory reported, not fatal", gone["imported"] == 0 and len(gone["errors"]) == 1)

            # --- tags ---
            tags = await call("list_tags", {})
            tag_names = {t["tag"] for t in tags["tags"]}
            check("list_tags includes imported + manual tags", {"imported", "mcp", "python"} <= tag_names)

            renamed = await call("rename_tag", {"old_tag": "python", "new_tag": "py"})
            check("rename_tag updated 1 entry", renamed["entries_updated"] == 1)
            tags2 = await call("list_tags", {})
            names2 = {t["tag"] for t in tags2["tags"]}
            check("old tag gone after rename", "python" not in names2 and "py" in names2)

            # --- read / list / stats / delete ---
            got = await call("get_snippet", {"snippet_id": snippet_id})
            check("get_snippet returns full entry", got["found"] is True and "FastMCP" in got["snippet"]["content"])
            missing = await call("get_snippet", {"snippet_id": "kb-nope"})
            check("get_snippet handles unknown id", missing["found"] is False)

            listing = await call("list_snippets", {"limit": 50})
            check("list_snippets returns everything", listing["count"] == 4)
            filtered = await call("list_snippets", {"source_type": "file"})
            check("source_type filter works", filtered["count"] == 3)

            stats = await call("kb_stats", {})
            check("kb_stats counts 4 entries", stats["total_entries"] == 4)
            check("kb_stats reports fts5 backend", stats["search_backend"] == "fts5_bm25")
            check("kb_stats breaks down by source", stats["by_source_type"].get("file") == 3)

            deleted = await call("delete_snippet", {"snippet_id": snippet_id})
            check("delete_snippet removed entry", deleted["deleted"] is True)
            after = await call("kb_stats", {})
            check("count drops after delete", after["total_entries"] == 3)

            # Deleted entries must leave the FTS index too.
            stale = await call("search_kb", {"query": "FastMCP decorator"})
            check("deleted entry no longer searchable", stale["count"] == 0)

    return failures


async def main() -> int:
    failures = 0
    tmp = Path(tempfile.mkdtemp(prefix="mcp-e2e-"))
    print(f"scratch dirs under: {tmp}\n")

    # ---- 1. Web research server (no network calls in this test) ----
    print("1) server.py -- web research")
    notes = tmp / "notes"
    names, results = await run_server(
        "server.py",
        {"MCP_NOTES_DIR": str(notes)},
        [
            ("save_note", {"filename": "test-note.md", "content": "# Hello\n\nE2E note."}),
        ],
    )
    expected = {"search_web", "fetch_url", "save_note"}
    missing = expected - set(names)
    if missing:
        print(f"    MISSING TOOLS: {missing}")
        failures += 1
    saved = list(notes.glob("*test-note*")) if notes.exists() else []
    print(f"    note file on disk: {[p.name for p in saved]}")
    if not saved:
        print("    FAILED: save_note wrote nothing to MCP_NOTES_DIR")
        failures += 1

    # ---- 2. Planner server ----
    print("\n2) planner_server.py -- local planner")
    planner = tmp / "planner"
    names, results = await run_server(
        "planner_server.py",
        {"MCP_PLANNER_DIR": str(planner)},
        [
            ("create_project", {"name": "Launch Blog", "description": "E2E test project"}),
            (
                "create_task",
                {
                    "project": "Launch Blog",
                    "title": "Draft first post",
                    "priority": "high",
                    "due_date": "2026-09-05",
                },
            ),
            ("list_projects", {}),
            ("list_tasks", {"project": "Launch Blog"}),
            ("get_daily_focus", {}),
        ],
    )
    expected = {
        "create_project",
        "list_projects",
        "create_task",
        "list_tasks",
        "update_task",
        "complete_task",
        "delete_task",
        "save_project_note",
        "get_daily_focus",
    }
    missing = expected - set(names)
    if missing:
        print(f"    MISSING TOOLS: {missing}")
        failures += 1
    failures += sum(1 for _, _, err in results if err)

    # ---- 3. Health server ----
    print("\n3) health_server.py -- health & habit tracker")
    health = tmp / "health"
    names, results = await run_server(
        "health_server.py",
        {"MCP_HEALTH_DIR": str(health)},
        [
            ("create_habit", {"name": "Water", "target_per_week": 7, "unit": "glasses"}),
            ("log_habit", {"habit_name": "Water", "value": 8, "log_date": "2026-08-29"}),
            (
                "log_workout",
                {
                    "activity": "Run",
                    "duration_minutes": 32,
                    "log_date": "2026-08-29",
                    "intensity": "moderate",
                },
            ),
            (
                "log_meal",
                {
                    "description": "Oatmeal + berries",
                    "meal_type": "breakfast",
                    "log_date": "2026-08-29",
                    "calories": 320,
                },
            ),
            ("log_measurement", {"weight_kg": 78.4, "log_date": "2026-08-29"}),
            ("list_habits", {}),
            ("list_logs", {"limit": 5}),
            ("get_daily_summary", {"for_date": "2026-08-29"}),
            ("get_weekly_report", {"for_date": "2026-08-29"}),
        ],
    )
    expected = {
        "create_habit",
        "list_habits",
        "log_habit",
        "log_workout",
        "log_meal",
        "log_measurement",
        "list_logs",
        "delete_log",
        "save_health_note",
        "get_daily_summary",
        "get_weekly_report",
    }
    missing = expected - set(names)
    if missing:
        print(f"    MISSING TOOLS: {missing}")
        failures += 1
    failures += sum(1 for _, _, err in results if err)

    # ---- 4. Knowledge base server ----
    print("\n4) kb_server.py -- personal knowledge base")
    failures += await test_kb(tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
