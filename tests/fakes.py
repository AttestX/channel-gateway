"""Stand-ins for the Telegram Bot API and for an agent, for the image tests."""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT = {"id": 1000, "is_bot": True, "first_name": "Test", "username": "test_bot",
       "can_join_groups": True, "can_read_all_group_messages": False,
       "supports_inline_queries": False}


def _serve(handler_class) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FakeTelegram:
    """Enough of the Bot API for a polling bot: getMe, getUpdates, sendMessage, edits."""

    def __init__(self, token: str):
        self.token = token
        self.updates: list[dict] = []
        self.sent: list[dict] = []  # every sendMessage / editMessageText, as asked
        self.calls: list[str] = []
        self.lock = threading.Lock()
        self.next_id = 1
        self.message_id = 500
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                fake._handle(self)

            def do_GET(self):
                fake._handle(self)

        self.server = _serve(Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/bot"

    def close(self) -> None:
        self.server.shutdown()

    def message_from(self, user_id: int, text: str) -> None:
        with self.lock:
            self.updates.append({
                "update_id": self.next_id,
                "message": {
                    "message_id": self.next_id, "date": int(time.time()),
                    "chat": {"id": user_id, "type": "private", "first_name": f"user{user_id}"},
                    "from": {"id": user_id, "is_bot": False, "first_name": f"user{user_id}"},
                    "text": text,
                },
            })
            self.next_id += 1

    def texts_to(self, chat_id: int) -> list[str]:
        with self.lock:
            return [m.get("text", "") for m in self.sent if str(m.get("chat_id")) == str(chat_id)]

    def _params(self, h: BaseHTTPRequestHandler) -> dict:
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h.path).query))
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        kind = h.headers.get("Content-Type", "")
        if body and "json" in kind:
            query.update(json.loads(body))
        elif body and "x-www-form-urlencoded" in kind:
            query.update(dict(urllib.parse.parse_qsl(body.decode())))
        elif body and "multipart/form-data" in kind:
            pass  # file uploads: not needed here
        return query

    def _reply(self, h: BaseHTTPRequestHandler, result, ok: bool = True, status: int = 200) -> None:
        data = json.dumps({"ok": ok, "result": result} if ok else
                          {"ok": False, "error_code": status, "description": result}).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        try:
            h.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the gateway stopped mid long-poll

    def _message(self, params: dict) -> dict:
        with self.lock:
            self.message_id += 1
            mid = self.message_id
        chat = params.get("chat_id")
        return {"message_id": int(params.get("message_id") or mid), "date": int(time.time()),
                "chat": {"id": int(chat) if str(chat).lstrip("-").isdigit() else chat, "type": "private"},
                "from": BOT, "text": params.get("text", "")}

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.urlsplit(h.path).path
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return self._reply(h, "Unauthorized", ok=False, status=401)
        method = path[len(prefix):]
        params = self._params(h)
        with self.lock:
            self.calls.append(method)
        if method == "getMe":
            return self._reply(h, BOT)
        if method == "getUpdates":
            offset = int(params.get("offset") or 0)
            deadline = time.time() + min(float(params.get("timeout") or 0), 1.0)
            while True:
                with self.lock:
                    ready = [u for u in self.updates if u["update_id"] >= offset]
                if ready or time.time() >= deadline:
                    return self._reply(h, ready)
                time.sleep(0.1)
        if method in ("sendMessage", "editMessageText"):
            with self.lock:
                self.sent.append(params)
            return self._reply(h, self._message(params))
        if method in ("getMyCommands", "getMyDescription"):
            return self._reply(h, [] if method == "getMyCommands" else {"description": ""})
        if method == "getChat":
            return self._reply(h, {"id": int(params.get("chat_id", 0)), "type": "private"})
        return self._reply(h, True)


class FakeAgent:
    """An agent's chat endpoint that answers slowly, with SSE comment heartbeats."""

    def __init__(self, key: str, answer: str, seconds: float = 3.0):
        self.key = key
        self.answer = answer
        self.seconds = seconds
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                fake._handle(self)

        self.server = _serve(Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        body = json.loads(h.rfile.read(int(h.headers.get("Content-Length") or 0)) or b"{}")
        with self.lock:
            self.requests.append({"path": h.path, "auth": h.headers.get("Authorization"), "body": body})
        if h.path != "/v1/chat/completions" or h.headers.get("Authorization") != f"Bearer {self.key}":
            h.send_response(401)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Connection", "close")
        h.end_headers()

        def chunk(delta: dict, finish=None) -> None:
            frame = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()),
                     "model": "agent", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            h.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
            h.wfile.flush()

        chunk({"role": "assistant", "content": "Working on it.\n"})
        end = time.time() + self.seconds
        while time.time() < end:
            h.wfile.write(b": still running\n\n")  # a heartbeat: no text for the chat
            h.wfile.flush()
            time.sleep(0.5)
        chunk({"content": self.answer})
        chunk({}, "stop")
        h.wfile.write(b"data: [DONE]\n\n")
        h.wfile.flush()

    def user_messages(self) -> list[str]:
        with self.lock:
            out = []
            for r in self.requests:
                msgs = [m for m in r["body"].get("messages", []) if m.get("role") == "user"]
                if msgs:
                    out.append(str(msgs[-1].get("content")))
            return out
