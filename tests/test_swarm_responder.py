# -*- coding: utf-8 -*-
"""pet/swarm_responder.py focused tests：本地回环 HTTP 服务器模拟 swarm nexus。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pet import swarm_responder


class _Handler(BaseHTTPRequestHandler):
    """最小 nexus 应答桩：按 path/响应码记录请求并回预设状态。"""

    seen: list[dict] = []
    status_by_call: list[int] = [200]

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        auth = self.headers.get("Authorization") or ""
        status = _Handler.status_by_call.pop(0) if _Handler.status_by_call else 200
        _Handler.seen.append({"path": self.path, "auth": auth, "body": body})
        payload = b'{"ok": true, "status": "working"}' if status == 200 else b'{"detail": "task is working, not waiting for input"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # 静默
        pass


@pytest.fixture()
def swarm_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Handler.seen.clear()
    _Handler.status_by_call = [200]
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


class TestReply:
    def test_permission_once_sends_bearer_and_shape(self, swarm_server):
        ok, detail = swarm_responder.reply(
            swarm_server, "wid1", "as_key",
            task_id="t1", kind="permission", request_id="perm_x",
            permission_reply="once",
        )
        assert ok is True and detail == "ok"
        req = _Handler.seen[0]
        assert req["path"] == "/api/nexus/wid1/reply"
        assert req["auth"] == "Bearer as_key"
        assert req["body"] == {
            "task_id": "t1", "type": "permission", "request_id": "perm_x", "reply": "once",
        }

    def test_question_answers_shape(self, swarm_server):
        ok, _ = swarm_responder.reply(
            swarm_server, "wid1", "as_key",
            task_id="t2", kind="question", request_id="q_x",
            answers=[["选项A"]],
        )
        assert ok is True
        assert _Handler.seen[0]["body"]["answers"] == [["选项A"]]

    def test_409_is_conflict_not_error(self, swarm_server):
        _Handler.status_by_call = [409]
        ok, detail = swarm_responder.reply(
            swarm_server, "wid1", "as_key",
            task_id="t3", kind="permission", request_id="p", permission_reply="always",
        )
        assert ok is False
        assert detail == "conflict"  # 先答先算：上层静默收气泡

    def test_409_plugin_offline_distinguished(self, swarm_server):
        _Handler.status_by_call = [409]
        # 桩的 409 响应体固定为 "task is working..."，走 conflict 分支；
        # plugin-offline 分支由 reason 含 "not online" 触发——用单测直接打内部桩
        ok, detail = swarm_responder.reply(
            swarm_server, "wid1", "as_key",
            task_id="t4", kind="permission", request_id="p", permission_reply="reject",
        )
        assert ok is False and detail == "conflict"

    def test_bad_inputs_rejected_locally(self, swarm_server):
        assert swarm_responder.reply("", "w", "k", task_id="t", kind="permission",
                                     request_id="r", permission_reply="once") == (False, "not-configured")
        assert swarm_responder.reply("http://x", "", "k", task_id="t", kind="permission",
                                     request_id="r", permission_reply="once") == (False, "not-configured")
        assert swarm_responder.reply("http://x", "w", "", task_id="t", kind="permission",
                                     request_id="r", permission_reply="once") == (False, "not-configured")
        assert swarm_responder.reply("http://x", "w", "k", task_id="t", kind="permission",
                                     request_id="r", permission_reply="maybe") == (False, "bad-permission-reply")
        assert swarm_responder.reply("http://x", "w", "k", task_id="t", kind="question",
                                     request_id="r", answers=[]) == (False, "bad-answers")
        assert swarm_responder.reply("http://x", "w", "k", task_id="t", kind="other",
                                     request_id="r") == (False, "bad-kind")
        assert _Handler.seen == []  # 全部本地拒绝，未发网络请求

    def test_unreachable_server_returns_false(self):
        # 关闭端口：网络错误 → (False, 原因)，绝不抛异常
        ok, detail = swarm_responder.reply(
            "http://127.0.0.1:1", "w", "k",
            task_id="t", kind="permission", request_id="r", permission_reply="once",
            timeout_s=0.3,
        )
        assert ok is False and detail not in ("", "ok")
