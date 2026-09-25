# -*- coding: utf-8 -*-
"""pet/agent_swarm_client.py focused tests。

两层：
- 纯函数协议解析（extract_input_required / monitor_payload / brief_from_status）；
- SwarmMonitor 会话行为：本地 ``websockets.serve`` 起真 nexus 桩（hello/subscribe/
  回放/实时推送），断言信号映射与回放去重——真实事件循环 + 真套接字，不做 mock。

时序纪律（AGENTS.md CI cost discipline）：全部用 Event/宽预算轮询，禁固定 sleep
赌时序；测试服务器名走 127.0.0.1 回环随机端口（无名字长度问题）。
"""
from __future__ import annotations

import asyncio
import json
import threading
from urllib.request import urlopen

import pytest
from PySide6.QtCore import QCoreApplication, QTimer

from pet.agent_swarm_client import (
    SwarmMonitor,
    brief_from_status,
    extract_input_required,
    monitor_payload,
)


# ============================================================================
# 1. 纯函数协议解析
# ============================================================================
class TestExtractInputRequired:
    def test_permission_data_part(self):
        payload = {
            "kind": "status-update",
            "taskId": "T1",
            "status": {
                "state": "input-required",
                "message": {"role": "assistant", "parts": [
                    {"kind": "data", "data": {
                        "type": "permission", "requestId": "perm_1",
                        "permission": "bash", "patterns": ["D:\\x"],
                    }},
                ]},
            },
        }
        req = extract_input_required(payload)
        assert req is not None
        assert req["type"] == "permission"
        assert req["requestId"] == "perm_1"
        assert req["task_id"] == "T1"
        assert req["permission"] == "bash"

    def test_question_with_options(self):
        payload = {
            "kind": "status-update", "taskId": "T2",
            "status": {"state": "input-required", "message": {"parts": [
                {"kind": "data", "data": {
                    "type": "question", "requestId": "q1",
                    "question": "选一个", "options": [{"label": "A", "value": "a"}],
                }},
            ]}},
        }
        req = extract_input_required(payload)
        assert req is not None and req["type"] == "question"
        assert req["options"] == [{"label": "A", "value": "a"}]

    def test_non_input_required_is_none(self):
        assert extract_input_required({"kind": "status-update",
                                       "status": {"state": "working"}}) is None
        assert extract_input_required({"kind": "artifact-update"}) is None
        assert extract_input_required({}) is None

    def test_missing_request_id_is_none(self):
        payload = {"kind": "status-update", "status": {"state": "input-required",
                   "message": {"parts": [{"kind": "data", "data": {"type": "permission"}}]}}}
        assert extract_input_required(payload) is None


class TestMonitorPayload:
    def test_permission_flat_shape(self):
        mon = monitor_payload({
            "roundKey": "mon-abc", "type": "permission", "requestId": "p1",
            "title": "run cmd", "patterns": ["a"],
        })
        assert mon is not None
        assert mon["task_id"] == "mon-abc" and mon["requestId"] == "p1"

    def test_replied_round(self):
        mon = monitor_payload({"roundKey": "mon-abc", "type": "replied", "requestId": "p1"})
        assert mon is not None and mon["type"] == "replied"

    def test_ignores_stream_types(self):
        assert monitor_payload({"roundKey": "r", "type": "text", "text": "x"}) is None
        assert monitor_payload({"roundKey": "r", "type": "tool"}) is None
        assert monitor_payload({"type": "permission", "requestId": ""}) is None


class TestBriefFromStatus:
    def test_terminal_completed_takes_answer(self):
        payload = {
            "kind": "status-update", "taskId": "T9",
            "status": {"state": "completed", "message": {
                "role": "assistant",
                "parts": [{"kind": "text", "text": "干完了"}],
            }},
        }
        brief = brief_from_status(payload)
        assert brief == {"state": "completed", "task_id": "T9",
                         "task_first_line": "", "answer": "干完了", "error": ""}

    def test_metadata_brief_preferred(self):
        """服务端注入的 metadata.brief（任务行解密）优先于 message parts。"""
        payload = {
            "kind": "status-update", "taskId": "T9",
            "metadata": {"brief": {"artifact": "回答全文", "task_first_line": "帮我做 x"}},
            "status": {"state": "completed", "message": {"parts": [
                {"kind": "text", "text": "旧文本"}],
            }},
        }
        brief = brief_from_status(payload)
        assert brief["answer"] == "回答全文"
        assert brief["task_first_line"] == "帮我做 x"

    def test_failed_takes_error(self):
        payload = {
            "kind": "status-update", "taskId": "T9",
            "metadata": {"brief": {"error": "boom"}},
            "status": {"state": "failed", "message": {"parts": []}},
        }
        brief = brief_from_status(payload)
        assert brief["state"] == "failed" and brief["error"] == "boom"

    def test_canceled_brief_card(self):
        """canceled 也出简报卡（用户手动取消的任务同样要有结果反馈）。"""
        payload = {"kind": "status-update", "taskId": "T9",
                   "status": {"state": "canceled", "message": {"parts": []}}}
        brief = brief_from_status(payload)
        assert brief is not None
        assert brief["state"] == "canceled"

    def test_artifact_update_maps_completed(self):
        payload = {"kind": "artifact-update", "taskId": "T9",
                   "artifact": {"parts": [{"kind": "text", "text": "答案全文"}]}}
        brief = brief_from_status(payload)
        assert brief is not None and brief["answer"] == "答案全文"
        assert brief["state"] == "completed"

    def test_working_is_not_brief(self):
        assert brief_from_status({"kind": "status-update",
                                  "status": {"state": "working"}}) is None


# ============================================================================
# 3. AgentLinkManager 集成（swarm payload → 气泡/应答分流）
# ============================================================================
class TestManagerSwarmIntegration:
    def _make_manager(self, tmp_path):
        import pytest as _pytest
        from pet.agent_link import AgentLinkManager
        from pet.config import Config

        cfg = Config(base=tmp_path)
        mgr = AgentLinkManager(None, cfg)
        return cfg, mgr

    def test_swarm_monitor_registered(self, tmp_path):
        cfg, mgr = self._make_manager(tmp_path)
        assert "swarm" in mgr.monitors
        assert mgr.agent_names["swarm"] == "Agent Swarm"
        mgr.shutdown()

    def test_swarm_approval_payload_registers_interaction(self, tmp_path):
        cfg, mgr = self._make_manager(tmp_path)
        mgr._on_approval_request("swarm", {
            "type": "permission", "requestId": "p1", "workspace_id": "w1",
            "task_id": "T1", "title": "bash 权限",
            "patterns": ["D:\\x\\y.ps1"],
        })
        pending = mgr.pending_interactions_for("swarm")
        assert len(pending) == 1
        item = list(pending.values())[0]
        assert item["kind"] == "approval"
        assert item["request_id"] == "p1"
        assert item["workspace_id"] == "w1"
        assert item["task_id"] == "T1"
        assert item["interactive"] is True
        mgr.shutdown()

    def test_swarm_question_payload_registers_interaction(self, tmp_path):
        cfg, mgr = self._make_manager(tmp_path)
        mgr._on_question_request("swarm", {
            "type": "question", "requestId": "q1", "workspace_id": "w1",
            "task_id": "T2", "question": "选哪个部署方案？",
            "options": [{"label": "A"}, {"label": "B"}],
        })
        pending = mgr.pending_interactions_for("swarm")
        assert len(pending) == 1
        item = list(pending.values())[0]
        assert item["kind"] == "question"
        assert item["interactive"] is True
        assert item["questions"][0]["options"][1]["label"] == "B"
        mgr.shutdown()

    def test_swarm_resolved_by_request_id(self, tmp_path):
        cfg, mgr = self._make_manager(tmp_path)
        mgr._on_approval_request("swarm", {
            "type": "permission", "requestId": "p9", "workspace_id": "w1",
            "task_id": "T1", "title": "x",
        })
        assert len(mgr.pending_interactions_for("swarm")) == 1
        # nexus replied/task 快照 → approval_resolved(requestId) → 精确关闭
        mgr._on_approval_resolved("swarm", {"requestId": "p9"})
        assert mgr.pending_interactions_for("swarm") == {}
        mgr.shutdown()

    def test_swarm_brief_persistent_card(self, tmp_path, monkeypatch):
        """简报卡降级路径（无独立气泡）：sticky 常驻 + 标题带工作区名 + artifact 截 1500。"""
        cfg, mgr = self._make_manager(tmp_path)
        monkeypatch.setattr(mgr, "_swarm_bubble", lambda: None)
        alerts = []

        class _Win:
            def show_alert(self, text, *, subtitle="", duration_ms=0, sticky=False,
                           buttons=None, alert_type="", **kw):
                alerts.append({"text": text, "subtitle": subtitle, "sticky": sticky,
                               "duration_ms": duration_ms, "buttons": buttons,
                               "alert_type": alert_type})
            def hide_bubble(self):
                pass

        mgr.win = _Win()
        long_answer = "很长的回答" * 500  # 2500 字 > 1500
        mgr._on_swarm_brief("swarm", {
            "state": "completed", "workspace_id": "oWYak3uat54",
            "task_first_line": "帮我修 bug",
            "answer": long_answer, "error": "",
        })
        assert len(alerts) == 1
        card = alerts[0]
        assert card["sticky"] is True          # 方案 A：常驻
        assert card["duration_ms"] == 0
        assert card["alert_type"] == "swarm_brief"
        assert "✅ 任务完成" in card["subtitle"]
        assert "任务：帮我修 bug" in card["text"]
        assert card["text"].endswith("…") and len(card["text"]) < 1600  # 1500 截断
        # 降级路径不带按钮（SwarmBubble 主路径自带「知道了」；降级时 sticky 也无按钮）
        assert card["buttons"] is None
        mgr.shutdown()

    def test_swarm_brief_failed_shows_error(self, tmp_path):
        cfg, mgr = self._make_manager(tmp_path)
        alerts = []

        class _Win:
            def show_alert(self, text, *, subtitle="", **kw):
                alerts.append((subtitle, text))
            def hide_bubble(self):
                pass

        mgr.win = _Win()
        mgr._on_swarm_brief("swarm", {
            "state": "failed", "workspace_id": "w1",
            "task_first_line": "跑测试", "answer": "", "error": "boom",
        })
        subtitle, text = alerts[0]
        assert "❌ 任务失败" in subtitle
        assert "失败原因：boom" in text

    def test_swarm_brief_canceled_card(self, tmp_path):
        """canceled 出「⏹️ 任务已取消」常驻卡（用户手动取消也要有反馈）。"""
        cfg, mgr = self._make_manager(tmp_path)
        alerts = []

        class _Win:
            def show_alert(self, text, *, subtitle="", **kw):
                alerts.append((subtitle, text))
            def hide_bubble(self):
                pass

        mgr.win = _Win()
        mgr._on_swarm_brief("swarm", {
            "state": "canceled", "workspace_id": "w1",
            "task_first_line": "被你手动取消的任务", "answer": "",
        })
        subtitle, text = alerts[0]
        assert "⏹️ 任务已取消" in subtitle
        assert "任务：被你手动取消的任务" in text
        mgr.shutdown()

    def test_swarm_brief_title_falls_back_to_patterns(self, tmp_path):
        """perm_card 对齐：title 缺省时用 patterns 拼「执行：…」。"""
        cfg, mgr = self._make_manager(tmp_path)
        mgr._on_approval_request("swarm", {
            "type": "permission", "requestId": "p2", "workspace_id": "w1",
            "task_id": "T1", "permission": "bash", "title": "",
            "patterns": ["D:\\x\\run.ps1"],
        })
        item = list(mgr.pending_interactions_for("swarm").values())[0]
        assert "bash — " in item["text"] or "执行：" in item["text"] or "bash：" in item["text"]
        assert "run.ps1" in item["text"]
        mgr.shutdown()

    def test_swarm_approval_three_buttons(self, tmp_path):
        """swarm 审批卡三按钮：同意 / 本会话允许 / 拒绝（对齐飞书 perm_card）。"""
        cfg, mgr = self._make_manager(tmp_path)
        mgr._on_approval_request("swarm", {
            "type": "permission", "requestId": "p3", "workspace_id": "w1",
            "task_id": "T1", "title": "x",
        })
        iid = next(iter(mgr.pending_interactions_for("swarm")))
        buttons = mgr._interaction_buttons(iid)
        labels = [b[0] for b in buttons]
        assert labels == ["同意", "本会话允许", "拒绝"]
        # DSH 审批仍是两按钮（不回归）
        mgr._register_interaction("dsh", kind="approval", text="t", interactive=True,
                                  rpc_id="rpc1")
        dsh_iid = next(iter(mgr.pending_interactions_for("dsh")))
        dsh_labels = [b[0] for b in mgr._interaction_buttons(dsh_iid)]
        assert dsh_labels == ["同意", "拒绝"]
        mgr.shutdown()

    def test_swarm_reply_decision_mapping(self, tmp_path, monkeypatch):
        cfg, mgr = self._make_manager(tmp_path)
        submitted = []

        class _StubMonitor:
            def submit_reply(self, **kw):
                submitted.append(kw)

            def begin_stop(self):
                pass

            def finish_stop(self, deadline=None):
                pass

            _worker = None

        mgr.monitors["swarm"] = _StubMonitor()
        pending = {
            "agent_key": "swarm", "kind": "approval", "request_id": "p1",
            "workspace_id": "w1", "task_id": "T1",
        }
        mgr._respond_swarm_interaction(pending, "allowed-once")
        assert submitted == [{
            "workspace_id": "w1", "task_id": "T1", "kind": "permission",
            "request_id": "p1", "permission_reply": "once",
        }]
        mgr._respond_swarm_interaction(pending, "allowed-always")
        assert submitted[-1]["permission_reply"] == "always"
        mgr._respond_swarm_interaction(pending, "rejected")
        assert submitted[-1]["permission_reply"] == "reject"
        q_pending = {
            "agent_key": "swarm", "kind": "question", "request_id": "q1",
            "workspace_id": "w1", "task_id": "T2",
            "questions": [{"id": 0, "options": [{"label": "A"}, {"label": "B"}]}],
        }
        mgr._respond_swarm_interaction(q_pending, {"answers": [{"id": 0, "selected": ["B"]}]})
        assert submitted[-1] == {
            "workspace_id": "w1", "task_id": "T2", "kind": "question",
            "request_id": "q1", "answers": [["B"]],
        }
        mgr.shutdown()

    def test_swarm_api_key_roundtrip_memory_fallback(self, tmp_path, monkeypatch):
        # keyring 不可用时 resolve 走内存态 api_key 字段（不落盘由
        # _redacted_data 剔除保障）。SecretStore 打桩成永远 set 失败/get 空，
        # 测试不依赖本机 keyring 的真实状态（联调可能已写入真 key）。
        from pet.config import Config
        from pet.chat import models as chat_models

        class _NoKeyringStore:
            def __init__(self, *a, **k):
                pass
            available = False
            def get(self, ref):
                return ""
            def set(self, ref, value):
                return False

        monkeypatch.setattr(chat_models, "SecretStore", _NoKeyringStore)

        cfg = Config(base=tmp_path)
        ag = dict(cfg.data["agent_link"])
        ag["swarm_config"] = {"server_url": "http://x", "workspaces": [],
                              "api_key": "as_mem"}
        cfg.data["agent_link"] = ag
        assert cfg.resolve_swarm_api_key() == "as_mem"

    def test_swarm_api_key_keyring_priority(self, tmp_path, monkeypatch):
        """keyring 有值时优先于内存态（ resolve 序：keyring → 内存）。"""
        from pet.config import Config
        from pet.chat import models as chat_models

        class _FakeStore:
            def __init__(self, *a, **k):
                pass
            available = True
            def get(self, ref):
                return "as_from_keyring" if ref == "swarm_api_key" else ""
            def set(self, ref, value):
                return True

        monkeypatch.setattr(chat_models, "SecretStore", _FakeStore)

        cfg = Config(base=tmp_path)
        ag = dict(cfg.data["agent_link"])
        ag["swarm_config"] = {"server_url": "http://x", "workspaces": [],
                              "api_key": "as_mem"}
        cfg.data["agent_link"] = ag
        assert cfg.resolve_swarm_api_key() == "as_from_keyring"

    def test_apply_config_defaults_to_wildcard(self, tmp_path):
        """简报语义默认：未勾工作区（workspaces 空）→ 注入 ["*"] 通配。"""
        from pet.config import Config
        from pet.agent_link import AgentLinkManager

        cfg = Config(base=tmp_path)
        ag = dict(cfg.data["agent_link"])
        ag["swarm"] = True
        ag["swarm_config"] = {"server_url": "http://127.0.0.1:1", "workspaces": []}
        ag["swarm_config"]["api_key"] = "as_x"  # 内存态 key（keyring 不可用兜底）
        cfg.data["agent_link"] = ag
        mgr = AgentLinkManager(None, cfg)
        mon = mgr.monitors.get("swarm")
        assert mon is not None
        assert mon._workspace_ids == ["*"]
        assert mon._server_url == "http://127.0.0.1:1"
        mgr.shutdown()
class _NexusStub:
    """最小 /ws/nexus 桩：hello(apikey) → subscribe → 回放+推送。"""

    def __init__(self) -> None:
        self.api_key_seen = ""
        self.subscribed_wids: list[str] = []
        self.live_events: list[dict] = []       # subscribe 后推的事件（每客户端一份）
        self.history: list[dict] = []
        self._server = None
        self.port = 0

    async def _handler(self, ws):
        hello = json.loads(await ws.recv())
        if hello.get("type") != "hello" or not hello.get("apikey"):
            await ws.send(json.dumps({"type": "hello_err", "error": "invalid api key"}))
            return
        self.api_key_seen = hello["apikey"]
        await ws.send(json.dumps({"type": "hello_ok"}))
        sub = json.loads(await ws.recv())
        if sub.get("type") != "subscribe":
            return
        wid = str(sub.get("workspace_id") or "")
        self.subscribed_wids.append(wid)
        await ws.send(json.dumps({
            "type": "subscribed", "workspace_id": wid,
            "plugin_online": True, "history": self.history, "first_id": 1,
        }))
        idx = 0
        while True:
            if idx < len(self.live_events):
                await ws.send(json.dumps({"type": "event", "payload": self.live_events[idx]}))
                idx += 1
            await asyncio.sleep(0.02)

    def start(self):
        self._ready = threading.Event()

        async def _run():
            import websockets

            self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._ready.set()
            stop_event = asyncio.Event()
            await stop_event.wait()  # serve until stop() sets it

        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop_runner(_run), daemon=True).start()
        self._ready.wait(timeout=5.0)

    def _loop_runner(self, coro_factory):
        def _run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                # coro 工厂在目标线程内创建 coroutine（跨线程传递 coroutine 会
                # 绑定错误的事件循环，run_until_complete 报
                # "An asyncio.Future, a coroutine or an awaitable is required"）
                loop.run_until_complete(coro_factory())
            except BaseException as exc:  # noqa: BLE001
                print("RUNNER ERR:", type(exc).__name__, exc)
            finally:
                loop.close()
        return _run

    def stop(self):
        loop = getattr(self, "_loop", None)
        server = getattr(self, "_server", None)
        if server and loop and not loop.is_closed():
            async def _aclose():
                server.close()
                await server.wait_closed()
                asyncio.get_running_loop().stop()
            try:
                asyncio.run_coroutine_threadsafe(_aclose(), loop)
            except RuntimeError:
                pass  # loop 已停


@pytest.fixture()
def nexus_stub():
    stub = _NexusStub()
    stub.start()
    yield stub
    stub.stop()


def _make_monitor(server_url: str, wid: str = "*") -> SwarmMonitor:
    app = QCoreApplication.instance() or QCoreApplication([])
    mon = SwarmMonitor("swarm", _tmp_config_dir())
    mon.configure(server_url, "as_test_key", [wid])
    return mon


def _tmp_config_dir():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp(prefix="swarm-mon-"))


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    """Qt 事件循环驱动的宽预算轮询（禁固定 sleep 赌时序）。"""
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        if predicate():
            return True
        threading.Event().wait(0.02)
    return False


import time  # noqa: E402  （_wait_until 之后统一导入，保持文件头整洁）


class TestSwarmMonitorSession:
    def test_hello_subscribe_and_permission_signal(self, nexus_stub):
        nexus_stub.live_events.append({
            "kind": "status-update", "taskId": "T1",
            "status": {"state": "input-required", "message": {"parts": [
                {"kind": "data", "data": {"type": "permission", "requestId": "p1",
                                          "command": "rm -rf /"}},
            ]}},
        })
        mon = _make_monitor(f"http://127.0.0.1:{nexus_stub.port}")
        approvals = []
        mon.approval_requested.connect(lambda k, p: approvals.append(p))
        assert mon.start() is True
        try:
            assert _wait_until(lambda: bool(approvals) and nexus_stub.subscribed_wids), \
                "未在预算内收到 subscribed+approval"
            assert nexus_stub.api_key_seen == "as_test_key"
            # 通配订阅：workspace_id="*" 原样送达服务端（属主全局简报语义）
            assert nexus_stub.subscribed_wids == ["*"]
            assert approvals[0]["requestId"] == "p1"
            assert approvals[0]["workspace_id"] == "*"
            # 去重：同一 requestId 不重弹
            assert _wait_until(lambda: True)
            before = len(approvals)
            threading.Event().wait(0.15)
            assert len(approvals) == before
        finally:
            mon.stop()

    def test_replay_dedup_already_answered(self, nexus_stub):
        # 回放里 input-required 已被 resolved 跟随：不弹
        nexus_stub.history = [
            {"kind": "status-update", "taskId": "T2",
             "status": {"state": "input-required", "message": {"parts": [
                 {"kind": "data", "data": {"type": "permission", "requestId": "p2"}}]}}},
            {"kind": "status-update", "taskId": "T2",
             "status": {"state": "working"}},
        ]
        mon = _make_monitor(f"http://127.0.0.1:{nexus_stub.port}")
        approvals = []
        states = []
        mon.approval_requested.connect(lambda k, p: approvals.append(p))
        mon.state_changed.connect(lambda k, s: states.append(s))
        assert mon.start() is True
        try:
            assert _wait_until(lambda: nexus_stub.subscribed_wids and states)
            threading.Event().wait(0.2)
            assert approvals == []  # 已被应答的历史不重弹
        finally:
            mon.stop()

    def test_brief_task_first_line_from_working_cache(self, nexus_stub):
        """completed 帧不带指令文本时，用 working 阶段缓存的指令首行补全。"""
        wid = "w9"
        # 1) working 帧带 role=user 的任务回显（A2A 轮标准形状）
        nexus_stub.live_events.append({
            "kind": "status-update", "taskId": "T7",
            "status": {"state": "working", "message": {"role": "user", "parts": [
                {"kind": "text", "text": "帮我在桌面写个文件"}]}},
        })
        # 2) completed 帧只有 assistant 的回答
        nexus_stub.live_events.append({
            "kind": "status-update", "taskId": "T7",
            "status": {"state": "completed", "message": {"role": "assistant", "parts": [
                {"kind": "text", "text": "写好了"}]}},
        })
        mon = _make_monitor(f"http://127.0.0.1:{nexus_stub.port}", wid)
        briefs = []
        raws = []
        mon.brief_ready.connect(lambda k, b: briefs.append(b))
        orig_process = mon._process_payload

        def spy(payload, ws_id, gen, **kw):
            raws.append((str(payload.get("kind")), str((payload.get("status") or {}).get("state") or "")))
            return orig_process(payload, ws_id, gen, **kw)

        mon._process_payload = spy
        assert mon.start() is True
        try:
            assert _wait_until(lambda: len(briefs) >= 1 and nexus_stub.subscribed_wids), \
                "未在预算内收到简报"
            assert _wait_until(lambda: len(raws) >= 2), f"帧不足: {raws}"
            assert _wait_until(lambda: len(briefs) >= 1), \
                f"briefs={[dict(b, answer=str(b.get('answer'))[:20]) for b in briefs]} raws={raws}"
            final = briefs[-1]
            assert final["state"] == "completed"
            assert final["task_first_line"] == "帮我在桌面写个文件"
            assert final["answer"] == "写好了"
        finally:
            mon.stop()

    def test_monitor_round_idle_brief(self, nexus_stub):
        """monitor 轮（扁平形状）idle 帧出简报：user-text 缓存指令首行 +
        brief.artifact 作回答。回归：monitor 帧无 kind 字段曾被 brief_from_status
        静默漏掉（2026-09-25 用户 TUI 对话的完成简报从未出卡）。"""
        wid = "w9"
        # 真实帧序（摘自 swarm_frames.jsonl 录制）：user → user-text → ... → idle(brief)
        nexus_stub.live_events.append({
            "roundKey": "mon-ses_f5fd-0E0G0k0C9nEq",
            "sessionId": "ses_x", "type": "user", "text": "",
            "messageId": "msg_1",
        })
        nexus_stub.live_events.append({
            "roundKey": "mon-ses_f5fd-0E0G0k0C9nEq",
            "sessionId": "ses_x", "type": "user-text", "text": "你好",
        })
        nexus_stub.live_events.append({
            "roundKey": "mon-ses_f5fd-0E0G0k0C9nEq",
            "sessionId": "ses_x", "type": "text",
            "partId": "p:0", "text": "你好！当前状态速览…",
        })
        nexus_stub.live_events.append({
            "roundKey": "mon-ses_f5fd-0E0G0k0C9nEq",
            "sessionId": "ses_x", "type": "idle",
            "brief": {"artifact": "你好！当前状态速览：\n\n- **代码**：dev 分支干净"},
        })
        mon = _make_monitor(f"http://127.0.0.1:{nexus_stub.port}", wid)
        briefs = []
        mon.brief_ready.connect(lambda k, b: briefs.append(b))
        assert mon.start() is True
        try:
            assert _wait_until(lambda: len(briefs) >= 1), "monitor idle 简报未到"
            b = briefs[-1]
            assert b["state"] == "completed"
            assert b["task_id"] == "mon-ses_f5fd-0E0G0k0C9nEq"
            assert b["task_first_line"] == "你好"
            assert b["answer"].startswith("你好！当前状态速览")
        finally:
            mon.stop()

    def test_monitor_heartbeat_idle_no_card(self, nexus_stub):
        """无 brief 且无文本的裸 idle（心跳）不出卡。"""
        wid = "w9"
        nexus_stub.live_events.append({"roundKey": "mon-x", "type": "idle"})
        mon = _make_monitor(f"http://127.0.0.1:{nexus_stub.port}", wid)
        briefs = []
        mon.brief_ready.connect(lambda k, b: briefs.append(b))
        assert mon.start() is True
        try:
            assert _wait_until(lambda: nexus_stub.subscribed_wids)
            import time as _t
            deadline = _t.monotonic() + 1.5
            while _t.monotonic() < deadline:
                pass
            assert not briefs
        finally:
            mon.stop()

    def test_unconfigured_rejects_start(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        mon = SwarmMonitor("swarm", _tmp_config_dir())
        assert mon.start() is False

    def test_hello_rejected_emits_connection_error(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        mon = SwarmMonitor("swarm", _tmp_config_dir())
        assert hasattr(mon, "connection_error")
        assert hasattr(mon, "brief_ready")
