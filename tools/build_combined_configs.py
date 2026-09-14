#!/usr/bin/env python3
"""Assemble the all-in-one Claude Desktop / Cursor configs from the per-agent examples.

Each agent ships its own single-server example config. Those are the source of
truth, so the combined files are generated from them rather than hand-edited —
that way adding a sixth agent cannot leave the combined config stale.

Usage:
    ./.venv/bin/python tools/build_combined_configs.py          # regenerate
    ./.venv/bin/python tools/build_combined_configs.py --check   # verify only, non-zero on drift
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Each family's per-agent example files, and the combined file they produce.
FAMILIES = {
    "claude_desktop_config.all.example.json": [
        "claude_desktop_config.example.json",
        "claude_desktop_config.planner.example.json",
        "claude_desktop_config.health.example.json",
        "claude_desktop_config.kb.example.json",
        "claude_desktop_config.flashcards.example.json",
    ],
    "cursor-mcp.all.example.json": [
        "cursor-mcp.example.json",
        "cursor-mcp.planner.example.json",
        "cursor-mcp.health.example.json",
        "cursor-mcp.kb.example.json",
        "cursor-mcp.flashcards.example.json",
    ],
}

EXPECTED_SERVERS = {
    "web-research": "server.py",
    "local-planner": "planner_server.py",
    "health-tracker": "health_server.py",
    "knowledge-base": "kb_server.py",
    "flashcards": "flashcards_server.py",
}

EXPECTED_ENV = {
    "server.py": "MCP_NOTES_DIR",
    "planner_server.py": "MCP_PLANNER_DIR",
    "health_server.py": "MCP_HEALTH_DIR",
    "kb_server.py": "MCP_KB_DIR",
    "flashcards_server.py": "MCP_FLASHCARDS_DIR",
}

def env_var_declared_by(script_name: str) -> str | None:
    """Read the MCP_* env var the server script actually looks up."""
    path = REPO / script_name
    if not path.exists():
        return None
    match = re.search(r'os\.environ\.get\("(MCP_[A-Z_]+)"', path.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def build(sources: list[str]) -> dict:
    """Merge the per-agent examples into one mcpServers object."""
    combined: dict[str, dict] = {}
    for filename in sources:
        path = REPO / filename
        if not path.exists():
            raise SystemExit(f"missing source config: {filename}")
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", {})
        if not servers:
            raise SystemExit(f"{filename} declares no mcpServers")
        combined.update(servers)
    return {"mcpServers": combined}


def validate(combined: dict) -> list[str]:
    """Check the merged config against what the server scripts really expect."""
    problems: list[str] = []
    servers = combined.get("mcpServers", {})

    if set(servers) != set(EXPECTED_SERVERS):
        missing = set(EXPECTED_SERVERS) - set(servers)
        extra = set(servers) - set(EXPECTED_SERVERS)
        if missing:
            problems.append(f"missing server keys: {sorted(missing)}")
        if extra:
            problems.append(f"unexpected server keys: {sorted(extra)}")

    for key, entry in servers.items():
        script = Path(entry.get("args", [""])[-1]).name
        if EXPECTED_SERVERS.get(key) != script:
            problems.append(f"{key}: expected {EXPECTED_SERVERS.get(key)}, got {script}")

        if not str(entry.get("command", "")).endswith(".venv/bin/python"):
            problems.append(f"{key}: command should point at .venv/bin/python")

        env = entry.get("env", {})
        if not env:
            problems.append(f"{key}: no env block")
            continue

        (env_name,) = list(env)
        declared = env_var_declared_by(script)
        if declared is None:
            problems.append(f"{key}: could not find an MCP_* env var in {script}")
        elif declared != env_name:
            problems.append(f"{key}: config sets {env_name} but {script} reads {declared}")

        if EXPECTED_ENV.get(script) and EXPECTED_ENV[script] != env_name:
            problems.append(f"{key}: expected env {EXPECTED_ENV[script]}, got {env_name}")

    return problems


def render(combined: dict) -> str:
    # Plain JSON only. Claude Desktop and Cursor parse these strictly, and
    # // comments make json.loads fail, so provenance lives in the README.
    return json.dumps(combined, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build or verify the combined MCP client configs.")
    parser.add_argument("--check", action="store_true", help="verify the generated files are current; do not write")
    args = parser.parse_args()

    failures = 0
    for output_name, sources in FAMILIES.items():
        combined = build(sources)
        problems = validate(combined)

        # The rendered file must survive a strict JSON parse, or no client can load it.
        try:
            json.loads(render(combined))
        except json.JSONDecodeError as exc:
            problems.append(f"rendered output is not strict JSON: {exc}")
        if problems:
            failures += len(problems)
            print(f"{output_name}: INVALID")
            for problem in problems:
                print(f"  - {problem}")
            continue

        rendered = render(combined)
        out_path = REPO / output_name
        if args.check:
            if not out_path.exists():
                print(f"{output_name}: MISSING (run without --check to generate)")
                failures += 1
            elif out_path.read_text(encoding="utf-8") != rendered:
                print(f"{output_name}: OUT OF DATE (run without --check to regenerate)")
                failures += 1
            else:
                print(f"{output_name}: current, {len(combined['mcpServers'])} servers")
        else:
            out_path.write_text(rendered, encoding="utf-8")
            print(f"{output_name}: wrote {len(combined['mcpServers'])} servers")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
