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
from typing import Any

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


FLASHCARD_TOOLS = {
    "create_deck",
    "list_decks",
    "delete_deck",
    "add_card",
    "edit_card",
    "delete_card",
    "list_cards",
    "get_due_cards",
    "record_review",
    "get_review_session",
    "get_stats",
}


def test_sm2_math() -> int:
    """Verify the SM-2 recurrence directly against hand-computed values."""
    import importlib.util

    os.environ["MCP_FLASHCARDS_DIR"] = tempfile.mkdtemp(prefix="mcp-sm2-")
    spec = importlib.util.spec_from_file_location("fc_server", REPO / "flashcards_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    failures = 0

    def check(label: str, actual: Any, expected: Any) -> None:
        nonlocal failures
        ok = actual == expected
        print(f"    {'ok  ' if ok else 'FAIL'}  {label}: got {actual}" + ("" if ok else f", expected {expected}"))
        if not ok:
            failures += 1

    # First successful review always schedules 1 day out.
    r = mod._sm2(5, 0, 2.5, 0)
    check("q5 first review interval", r["interval"], 1)
    check("q5 first review repetitions", r["repetitions"], 1)
    check("q5 ease rises 2.5 -> 2.6", r["ease_factor"], 2.6)

    # Second successful review jumps to 6 days.
    r = mod._sm2(4, 1, 2.6, 1)
    check("q4 second review interval", r["interval"], 6)
    check("q4 leaves ease unchanged (delta is exactly 0)", round(r["ease_factor"], 4), 2.6)

    # Third successful review multiplies by the current ease factor.
    r = mod._sm2(5, 2, 2.5, 6)
    check("q5 third review interval = round(6 * 2.5)", r["interval"], 15)

    # Any grade below 3 is a lapse: repetitions reset, back to 1 day.
    for q in (0, 1, 2):
        r = mod._sm2(q, 7, 2.5, 90)
        check(f"q{q} lapse resets repetitions", r["repetitions"], 0)
        check(f"q{q} lapse resets interval", r["interval"], 1)

    # Ease factor must never fall below the 1.3 floor.
    ef = 2.5
    for _ in range(40):
        ef = mod._sm2(0, 5, ef, 10)["ease_factor"]
    check("ease floor clamps at 1.3", ef, 1.3)

    # Repeated hard-but-passing reviews decay ease toward the floor.
    ef = 2.5
    for _ in range(5):
        ef = mod._sm2(3, 3, ef, 10)["ease_factor"]
    check("q3 decays ease below 2.0", ef < 2.0, True)

    # Quality is clamped into the valid 0-5 range rather than erroring.
    check("quality clamped high", mod._sm2(99, 0, 2.5, 0)["quality"], 5)
    check("quality clamped low", mod._sm2(-5, 0, 2.5, 0)["quality"], 0)

    return failures


async def test_flashcards(tmp: Path) -> int:
    """Exercise the flashcards server end to end, including SM-2 rescheduling."""
    failures = 0
    from datetime import date, timedelta

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"    {'ok  ' if condition else 'FAIL'}  {label}" + (f" [{detail}]" if detail and not condition else ""))
        if not condition:
            failures += 1

    fc_dir = tmp / "flashcards"

    def params() -> StdioServerParameters:
        return StdioServerParameters(
            command=str(REPO / ".venv" / "bin" / "python"),
            args=[str(REPO / "flashcards_server.py")],
            env={**os.environ, "MCP_FLASHCARDS_DIR": str(fc_dir)},
        )

    async with stdio_client(params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"  [flashcards_server.py] {len(names)} tools: {', '.join(names)}")
            check("all 11 flashcard tools exposed", FLASHCARD_TOOLS == set(names))

            async def call(tool: str, args: dict) -> dict:
                res = await session.call_tool(tool, args)
                check(f"{tool}() did not error", not res.is_error)
                return json.loads(txt(res))

            today = date.today()

            # --- decks ---
            deck = await call("create_deck", {"name": "MCP Basics", "description": "Protocol fundamentals"})
            check("create_deck succeeded", deck.get("created") is True)
            deck_id = deck["deck"]["id"]

            dupe = await call("create_deck", {"name": "MCP Basics"})
            check("duplicate deck rejected", dupe.get("created") is False)

            blank = await call("create_deck", {"name": "   "})
            check("blank deck name rejected", blank.get("created") is False)

            # --- cards ---
            card_a = await call(
                "add_card",
                {"deck": "MCP Basics", "front": "What transport does a local MCP server use?", "back": "stdio", "tags": "mcp, transport"},
            )
            check("add_card succeeded", card_a.get("created") is True)
            card_a_id = card_a["card"]["id"]
            check("new card is due today", card_a["card"]["due_date"] == today.isoformat())
            check("new card starts at default ease", card_a["card"]["ease_factor"] == 2.5)

            await call("add_card", {"deck": "MCP Basics", "front": "What does MCP stand for?", "back": "Model Context Protocol", "tags": "mcp"})

            auto = await call("add_card", {"deck": "Brand New Deck", "front": "q", "back": "a"})
            check("add_card auto-creates a missing deck", auto.get("created") is True)

            bad = await call("add_card", {"deck": "MCP Basics", "front": "only front", "back": "  "})
            check("card with empty back rejected", bad.get("created") is False)

            # --- due cards hide answers by default ---
            due = await call("get_due_cards", {"limit": 10})
            check("get_due_cards returns the new cards", due["count"] == 3)
            check("answers hidden by default", all("back" not in c for c in due["cards"]))
            check("cards flagged as new", all(c["is_new"] for c in due["cards"]))
            revealed = await call("get_due_cards", {"limit": 10, "include_back": True})
            check("include_back reveals answers", all("back" in c for c in revealed["cards"]))

            # --- SM-2 rescheduling through the live server ---
            r5 = await call("record_review", {"card_id": card_a_id, "quality": 5})
            check("record_review accepted", r5.get("reviewed") is True)
            check("q5 schedules 1 day out", r5["next_due"] == (today + timedelta(days=1)).isoformat(), r5["next_due"])
            check("ease rose to 2.6", r5["after"]["ease_factor"] == 2.6)

            r0 = await call("record_review", {"card_id": card_a_id, "quality": 0})
            check("lapse schedules 1 day out", r0["next_due"] == (today + timedelta(days=1)).isoformat())
            check("lapse resets repetitions to 0", r0["after"]["repetitions"] == 0)
            check("lapse increments lapse counter", r0["lapses"] == 1)

            bad_q = await call("record_review", {"card_id": card_a_id, "quality": 7})
            check("out-of-range quality rejected", bad_q.get("reviewed") is False)
            bad_id = await call("record_review", {"card_id": "card-nope", "quality": 4})
            check("unknown card id rejected", bad_id.get("reviewed") is False)

            # --- session + stats ---
            sess = await call("get_review_session", {"for_date": today.isoformat()})
            check("session counted 2 reviews", sess["reviews"] == 2)
            check("session accuracy is 50%", sess["accuracy_pct"] == 50.0, str(sess.get("accuracy_pct")))

            stats = await call("get_stats", {})
            check("stats sees 3 cards", stats["cards"] == 3)
            check("stats sees 2 decks", stats["decks"] == 2)
            check("stats retention is 50%", stats["retention_pct"] == 50.0, str(stats.get("retention_pct")))
            check("streak is 1 after reviewing today", stats["current_streak_days"] == 1, str(stats.get("current_streak_days")))
            check("lapsed card listed as hardest", any(c["id"] == card_a_id for c in stats["hardest_cards"]))

            # --- edit / list / delete ---
            edited = await call("edit_card", {"card_id": card_a_id, "back": "stdio (JSON-RPC over pipes)"})
            check("edit_card updated the back", edited["card"]["back"].startswith("stdio ("))
            check("edit_card preserved scheduling", edited["card"]["ease_factor"] == r0["after"]["ease_factor"])

            by_tag = await call("list_cards", {"tag": "transport"})
            check("tag filter works", by_tag["count"] == 1)
            by_deck = await call("list_cards", {"deck": "MCP Basics"})
            check("deck filter works", by_deck["count"] == 2)
            bad_deck = await call("list_cards", {"deck": "Nope"})
            check("unknown deck filter reports error", "error" in bad_deck)

            deleted = await call("delete_card", {"card_id": card_a_id})
            check("delete_card removed the card", deleted.get("deleted") is True)
            check("card count drops to 2", (await call("list_cards", {}))["count"] == 2)

            gone = await call("delete_deck", {"deck": "Brand New Deck"})
            check("delete_deck removed the deck", gone.get("deleted") is True)
            check("delete_deck removed its cards", gone["cards_removed"] == 1)

    # --- due-date filtering needs a restarted server reading edited data ---
    cards_file = fc_dir / "cards.json"
    stored = json.loads(cards_file.read_text(encoding="utf-8"))
    for card in stored:
        card["due_date"] = (today - timedelta(days=5)).isoformat()
        card["last_reviewed"] = (today - timedelta(days=6)).isoformat()
    cards_file.write_text(json.dumps(stored, indent=2), encoding="utf-8")

    async with stdio_client(params()) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, args: dict) -> dict:
                res = await session.call_tool(tool, args)
                check(f"{tool}() (restart) did not error", not res.is_error)
                return json.loads(txt(res))

            overdue = await call("get_due_cards", {"limit": 10})
            check("overdue card resurfaces after restart", overdue["count"] == 1)
            check("no card is still flagged new", all(not c["is_new"] for c in overdue["cards"]))

            none_due = await call("list_cards", {"due_only": False, "limit": 10})
            check("list_cards still returns everything", none_due["count"] == 1)

            empty = await call("get_review_session", {"for_date": "2020-01-01"})
            check("session for an unstudied date is empty", empty["reviewed_cards"] == 0)

            bad_date = await call("get_review_session", {"for_date": "not-a-date"})
            check("invalid date rejected", "error" in bad_date)

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

    # ---- 5. Flashcards server ----
    print("\n5) flashcards_server.py -- SM-2 algorithm math")
    failures += test_sm2_math()
    print("\n6) flashcards_server.py -- decks, cards, reviews")
    failures += await test_flashcards(tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
