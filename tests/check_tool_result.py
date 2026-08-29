"""Assert the tool result the bridge agent sent back to Ollama is genuine.

Reads the mock Ollama request log and fails if the tool-role message is an
exception placeholder rather than real tool output.
"""

from __future__ import annotations

import json
import sys


def main(log_path: str, expect_fragment: str) -> int:
    tool_msgs: list[str] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                entry = json.loads(line)
                for msg in entry.get("payload", {}).get("messages", []):
                    if msg.get("role") == "tool":
                        tool_msgs.append(str(msg.get("content", "")))
    except FileNotFoundError:
        print(f"  FAIL: no mock log at {log_path}")
        return 1

    if not tool_msgs:
        print("  FAIL: agent never sent a tool result back to the model")
        return 1

    content = tool_msgs[0]
    print(f"  tool result -> model: {content[:200]!r}")

    if "[exception while calling" in content or "AttributeError" in content:
        print("  FAIL: tool result was an exception placeholder, not real tool output")
        return 1
    if "[tool error]" in content:
        print("  FAIL: tool returned an error")
        return 1
    if expect_fragment not in content:
        print(f"  FAIL: expected {expect_fragment!r} in tool result")
        return 1
    print("  tool result is genuine")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
