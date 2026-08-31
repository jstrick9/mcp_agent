#!/usr/bin/env python3
"""Local Ollama + MCP personal knowledge base agent.

Bridges Ollama chat completions to the local knowledge-base MCP server.

Examples:
  python kb_agent.py "Ingest my research notes, then find everything about MCP."
  python kb_agent.py --model qwen2.5:7b
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/v1/chat/completions")
PROJECT_DIR = Path(__file__).resolve().parent
SERVER_PATH = PROJECT_DIR / "kb_server.py"

DEFAULT_INGEST_DIRS = [
    str(Path.home() / "MCPWebResearch" / "notes"),
    str(Path.home() / "MCPPlanner"),
    str(Path.home() / "MCPHealth"),
]

SYSTEM_PROMPT = """You are a local personal knowledge base assistant.
Answer only from what the knowledge base tools actually return. Search before answering.
Cite the snippet id and its source_url or source_path for every fact you use.
If a search returns nothing useful, say so plainly instead of guessing or inventing content.
When saving, pick short lowercase tags and keep snippets focused on one idea each.
Confirm what was saved, updated, or imported."""


def _text_from_content(content: Any) -> str:
    parts: list[str] = []
    for item in content or []:
        kind = getattr(item, "type", None) or (item.get("type") if isinstance(item, dict) else None)
        if kind == "text":
            text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else "")
            if text:
                parts.append(str(text))
        else:
            if hasattr(item, "model_dump"):
                parts.append(json.dumps(item.model_dump(), ensure_ascii=False))
            elif isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
    return "\n".join(parts).strip()


def _mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if schema is None and isinstance(tool, dict):
        schema = tool.get("inputSchema")
    if not schema:
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or f"Tool {tool.name}",
            "parameters": schema,
        },
    }


async def _ollama_chat(client: httpx.AsyncClient, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    response = await client.post(
        OLLAMA_URL,
        json={
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
            "options": {"temperature": 0.1},
        },
        timeout=180.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Ollama returned HTTP {response.status_code}: {response.text[:1000]}")
    return response.json()


async def run_agent(prompt: str, model: str, data_dir: Path | None) -> str:
    env = os.environ.copy()
    if data_dir:
        env["MCP_KB_DIR"] = str(data_dir)

    server_params = StdioServerParameters(command=sys.executable, args=[str(SERVER_PATH)], env=env)

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_response = await session.list_tools()
            tools = [_mcp_tool_to_openai(t) for t in tools_response.tools]
            print("[agent] connected to knowledge-base MCP server; tools:", ", ".join(t.name for t in tools_response.tools), file=sys.stderr)
            print("[agent] using Ollama model:", model, file=sys.stderr)

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]

            async with httpx.AsyncClient() as http:
                for _ in range(14):
                    data = await _ollama_chat(http, model, messages, tools)
                    msg = data["choices"][0]["message"]
                    messages.append(msg)

                    tool_calls = msg.get("tool_calls") or []
                    if not tool_calls:
                        return (msg.get("content") or "").strip() or "[No response text returned by Ollama]"

                    for call in tool_calls:
                        name = call["function"]["name"]
                        raw_args = call["function"].get("arguments") or "{}"
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                        except json.JSONDecodeError:
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.get("id", name),
                                    "name": name,
                                    "content": json.dumps({"error": "Invalid tool-call JSON. Use a valid JSON object."}),
                                }
                            )
                            continue

                        print("[agent] calling", name, json.dumps(args, ensure_ascii=False), file=sys.stderr)
                        try:
                            result = await session.call_tool(name, args)
                            result_text = _text_from_content(result.content)
                            if getattr(result, "is_error", False):
                                result_text = f"[tool error]\n{result_text}"
                        except Exception as exc:
                            result_text = f"[exception while calling {name}] {exc}"

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", name),
                                "name": name,
                                "content": result_text,
                            }
                        )

            return "Agent stopped after reaching the tool-call step limit."


async def repl(model: str, data_dir: Path | None) -> None:
    print("Local Ollama MCP knowledge base. Type 'exit' or Ctrl-C to quit.", file=sys.stderr)
    while True:
        try:
            prompt = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return
        try:
            answer = await run_agent(prompt, model, data_dir)
            print("\nAssistant>")
            print(answer)
            print()
        except Exception as exc:
            print(f"[agent error] {exc}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local Ollama-powered MCP knowledge base agent.")
    parser.add_argument("prompt", nargs="?", help="Prompt to run once. Omit for interactive mode.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("MCP_KB_DIR", Path.home() / "MCPKnowledge")),
        help="Directory for the knowledge base database",
    )
    parser.add_argument(
        "--ingest-dirs",
        nargs="*",
        default=None,
        help="Optional folders to mention for ingest_notes (informational only)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.ingest_dirs:
        print("[agent] suggested ingest folders:", ", ".join(args.ingest_dirs), file=sys.stderr)
    if args.prompt:
        print(asyncio.run(run_agent(args.prompt, args.model, args.data_dir)))
    else:
        asyncio.run(repl(args.model, args.data_dir))


if __name__ == "__main__":
    main()
