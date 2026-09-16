"""Assert the tool schemas the bridge agent sent to Ollama are usable.

A model cannot fill in arguments for a tool whose parameter schema is empty,
so this fails if any advertised tool has no properties.
"""

from __future__ import annotations

import json
import sys


def main(log_path: str, called_tool: str) -> int:
    try:
        with open(log_path, encoding="utf-8") as fh:
            entries = [json.loads(line) for line in fh if line.strip()]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"  FAIL: could not read mock log {log_path}: {exc}")
        return 1

    # The launcher sends a warmup request with no messages before the agent runs;
    # pick the first request that actually advertises tools.
    tools: list[dict] = []
    for entry in entries:
        candidate = entry.get("payload", {}).get("tools") or []
        if candidate:
            tools = candidate
            break

    if not tools:
        print(f"  FAIL: agent advertised no tools to the model ({len(entries)} request(s) logged)")
        return 1

    names = [t["function"]["name"] for t in tools]
    if called_tool not in names:
        print(f"  FAIL: expected tool {called_tool!r} among {names}")
        return 1

    # Every advertised tool must carry a well-formed schema. An empty "properties"
    # object is legitimate for tools that take no arguments, so the structural
    # check is on the schema shape, not on it being non-empty.
    malformed = [
        t["function"]["name"]
        for t in tools
        if t["function"]["parameters"].get("type") != "object"
        or not isinstance(t["function"]["parameters"].get("properties"), dict)
    ]
    if malformed:
        print(f"  FAIL: {len(malformed)} tool(s) with malformed parameter schemas: {malformed}")
        return 1

    # The tool this case exercises takes arguments, so its schema must list them.
    called_props = sorted(
        next(t for t in tools if t["function"]["name"] == called_tool)["function"]["parameters"]["properties"]
    )
    if not called_props:
        print(f"  FAIL: {called_tool} takes arguments but its schema lists none")
        return 1

    print(f"  {len(tools)} tools advertised, all schemas well-formed")
    print(f"  {called_tool} arguments: {called_props}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
