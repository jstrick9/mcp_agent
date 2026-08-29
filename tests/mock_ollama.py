"""Mock Ollama OpenAI-compatible endpoint, used by tests/e2e_agents.py.

Emits one tool call on the first turn (whatever tool name is supplied via
MOCK_TOOL / MOCK_ARGS), then a plain final answer once a tool result comes back.
Every request is appended to the file named by MOCK_LOG as JSON.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "11435"))
TOOL = os.environ.get("MOCK_TOOL", "save_note")
ARGS = json.loads(os.environ.get("MOCK_ARGS", "{}"))
FINAL = os.environ.get("MOCK_FINAL", "Done.")
LOG = os.environ.get("MOCK_LOG", "/tmp/mock-ollama-log.jsonl")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence default stderr logging
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")

        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": self.path, "payload": payload}) + "\n")

        messages = payload.get("messages", [])
        saw_tool_result = any(m.get("role") == "tool" for m in messages)

        if not saw_tool_result:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_mock_1",
                        "type": "function",
                        "function": {"name": TOOL, "arguments": json.dumps(ARGS)},
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": FINAL}

        body = json.dumps(
            {"id": "mock-1", "object": "chat.completion", "model": payload.get("model"), "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
