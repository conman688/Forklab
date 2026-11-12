"""Local stub of the Anthropic Messages endpoint for offline payload tests.

Records every (path, raw body). Answers a request whose last message is the
result of `fork_id` with a live tool call `live_id`; answers anything else
with a final text reply.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FINAL_TEXT = "done"


def start_stub(fork_id: str, live_id: str, tool_name: str):
    received: list[tuple[str, bytes]] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                received.append((self.path, body))
            try:
                last = json.loads(body)["messages"][-1]
                ids = [b.get("tool_use_id") for b in last.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                self.send_response(400)
                self.end_headers()
                return
            if fork_id in ids:
                content = [{"type": "tool_use", "id": live_id, "name": tool_name, "input": {}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": FINAL_TEXT}]
                stop = "end_turn"
            out = json.dumps({
                "id": "msg_stub", "type": "message", "role": "assistant", "model": "stub",
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received
