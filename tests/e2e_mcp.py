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

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
