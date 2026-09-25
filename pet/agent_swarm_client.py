# -*- coding: utf-8 -*-
"""Agent Swarm（虫群）联动监视器：以 web 客户端身份接入 nexus /ws/nexus。

对接依据：agent_swarm 仓库 ``docs/desktop-client-nexus-integration.md``（v1）。
桌宠是「一个没有浏览器的 web 客户端」：WS 订阅事件流（简报模式客户端过滤），
REST 应答权限/提问（pet/swarm_responder.py）。

与 BaseAgentMonitor 的关系：文件型监视器基类的 _work_loop/_tailer 对网络型
监视器不适用，本类继承它只为复用信号契约（state_changed/state_event/approval_*
等）与 pause/resume 缓冲语义，覆写 start/stop/_poll 为 WS 轮询线程形态。
线程模型与 CollisionIpcSession 同纪律：facade 在 GUI 线程，WS 独占工作线程，
跨线程只经 Qt 队列信号（信号 emit 自线程安全，pause 缓冲在基类内加锁）。

网络策略（有意为之，勿改）：**跟随系统代理**——websockets 库经环境变量
HTTP(S)_PROXY 走系统代理，与 chat/余额/更新检查同一阵营（见
docs/NETWORK-PROXY-AND-VPN-2026-09-22.md）；歌词的 bypass 不适用于本模块。

安全护栏（AGENT_LINK_PROTOCOL.md §6.2 范围澄清后的网络型监视器条款）：
默认关；服务器 URL 用户显式配置；apikey 只存 keyring（SecretStore 模式）；
只连配置的那台服务器；不落明文。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal

from .agent_link import BaseAgentMonitor

log = logging.getLogger("dsh-pet-standalone")

# 终态集合（nexus_a2a TERMINAL_STATES 同口径）
TERMINAL_STATES = ("completed", "failed", "canceled")


def extract_input_required(payload: dict) -> dict | None:
    """从 A2A status-update 事件里抽 input-required 的 DataPart。

    形状见对接文档 §3.2①；返回 {"type", "requestId", "task_id", **data} 或 None。
    """
    if str(payload.get("kind") or "") != "status-update":
        return None
    status = payload.get("status") or {}
    if not isinstance(status, dict) or str(status.get("state") or "") != "input-required":
        return None
    message = status.get("message") or {}
    if not isinstance(message, dict):
        return None
    for part in (message.get("parts") or []):
        if not isinstance(part, dict) or part.get("kind") != "data":
            continue
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        dtype = str(data.get("type") or "")
        request_id = str(data.get("requestId") or data.get("request_id") or "")
        if dtype in ("permission", "question") and request_id:
            out = dict(data)
            out["type"] = dtype
            out["requestId"] = request_id
            out["task_id"] = str(payload.get("taskId") or data.get("taskId") or "")
            return out
    return None


def monitor_payload(payload: dict) -> dict | None:
    """规整前台监控轮事件（type: "monitor"，扁平形状）。

    只关心 permission/question/replied 三类；返回带 task_id（=roundKey）的
    dict 或 None。见对接文档 §3.2②。
    """
    mtype = str(payload.get("type") or "")
    if mtype not in ("permission", "question", "replied"):
        return None
    out = dict(payload)
    out["task_id"] = str(payload.get("roundKey") or payload.get("task_id") or "")
    request_id = str(payload.get("requestId") or payload.get("request_id") or "")
    out["requestId"] = request_id
    if mtype in ("permission", "question") and not request_id:
        return None
    return out


def brief_from_status(payload: dict) -> dict | None:
    """终态事件 → 简报载荷（对齐飞书 brief_card 的信息密度）。

    返回 {state, task_id, task_first_line, answer, error}：
    - task_first_line：指令首行（终态 message.parts 里 role=user 的首个 text，
      或 metadata.brief.task_first_line——服务端若已注入优先）
    - answer：artifact 全文（优先 metadata.brief.artifact，服务端从任务行解密
      注入；回退 artifact-update 帧的 parts）
    - error：失败原因（metadata.brief.error）
    canceled 返回 None（用户主动中断不打扰，对齐飞书 brief.py:98 白名单）。
    """
    kind = str(payload.get("kind") or "")
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    brief_meta = meta.get("brief") if isinstance(meta.get("brief"), dict) else {}
    if kind == "artifact-update":
        parts = ((payload.get("artifact") or {}).get("parts") or [])
        text = " ".join(
            str(p.get("text") or "") for p in parts
            if isinstance(p, dict) and p.get("kind") == "text"
        ).strip()
        return {"state": "completed", "task_id": str(payload.get("taskId") or ""),
                "task_first_line": str(brief_meta.get("task_first_line") or ""),
                "answer": text, "error": ""}
    if kind == "status-update":
        state = str((payload.get("status") or {}).get("state") or "")
        if state not in TERMINAL_STATES:
            return None
        if state == "canceled":
            return None  # 用户主动中断，不打扰（对齐飞书白名单）
        message = (payload.get("status") or {}).get("message") or {}
        task_first = str(brief_meta.get("task_first_line") or "")
        answer = ""
        if isinstance(message, dict):
            parts = message.get("parts") or []
            # 指令首行：role=user 的首个 text part；回答：assistant 的 text
            for part in parts:
                if not isinstance(part, dict) or part.get("kind") != "text":
                    continue
                text = str(part.get("text") or "").strip()
                if not text:
                    continue
                if str(message.get("role") or "") == "user" and not task_first:
                    task_first = text.splitlines()[0].strip() if text else ""
                elif str(message.get("role") or "") != "user" and not answer:
                    answer = text
        return {
            "state": state,
            "task_id": str(payload.get("taskId") or ""),
            "task_first_line": task_first,
            "answer": str(brief_meta.get("artifact") or "") or answer,
            "error": str(brief_meta.get("error") or ""),
        }
    return None


class SwarmMonitor(BaseAgentMonitor):
    """Agent Swarm nexus 监视器（agent_link.swarm 开关驱动）。

    每个勾选的工作区一条 WS 连接（服务端一条连接只订阅一个 wid）；线程内
    asyncio 事件循环跑多条连接与 REST 应答队列，与 GUI 只经基类信号交互。
    """

    # swarm 专有信号（基类信号契约之外的新增出口）
    connection_error = Signal(str, object)   # (agent_key, reason) —— hello 被拒等注册级失败
    brief_ready = Signal(str, object)        # (agent_key, {state, task_id, text, workspace_id})

    def __init__(self, agent_key: str, config_dir: Path, parent=None) -> None:
        super().__init__(agent_key, config_dir, parent)
        # 网络型监视器：无事件文件，_mkdir_on_start 关闭（基类 start 会 mkdir
        # 事件目录，本类覆写 start 不走那条路）
        self._mkdir_on_start = False
        # 连接参数（start 前由 Manager 注入）
        self._server_url = ""
        self._api_key = ""
        # 工作区 id 列表；["*"] = 通配订阅（属主全部工作区，服务端 2026-09-25 起
        # 支持——与飞书/微信「属主全局」简报语义对齐，一条连接即可）
        self._workspace_ids: list[str] = []
        # 待答 requestId 集合（线程内读写）——回放去重与 409 先答先算都按它判断
        self._pending_requests: set[str] = set()
        # task_id → 该任务产生过的 requestId 集合（终态清扫用）
        self._task_requests: dict[str, set[str]] = {}
        # 回放缓冲（对接文档 §5 去重）：[(task_id, request_id)]；已证实被答的 rid；
        # rid → 完整载荷（flush 时弹窗用）
        self._replay_buffer: list[tuple[str, str]] = []
        self._replay_answered: set[str] = set()
        self._replay_payloads: dict[str, dict] = {}
        # 应答任务队列：(args dict)；由 GUI 线程经 _submit_reply 线程安全投递
        self._reply_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_async = threading.Event()

    # ---------------------------------------------------------------- 配置

    def configure(self, server_url: str, api_key: str, workspace_ids: list[str]) -> None:
        """注入连接参数（Manager 在 apply_config/start 前调用；GUI 线程）。"""
        self._server_url = (server_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._workspace_ids = [w for w in (workspace_ids or []) if w]

    @property
    def is_configured(self) -> bool:
        return bool(self._server_url and self._api_key and self._workspace_ids)

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> bool:
        """覆写基类：不走文件 tailer，起 asyncio worker 线程。

        基类 start 的代次/destroyed 守卫逻辑保留（_gen/_emit_gen/_destroyed_conn），
        只替换 worker 载体。未配置（无 URL/key/工作区）时拒绝启动并告警。
        """
        if not self.is_configured:
            log.warning(
                "Agent 监视器 [%s] 未配置（server_url/apikey/workspaces 缺一不可），拒绝启动",
                self.agent_key,
            )
            return False
        if self._worker is not None and self._worker.is_alive():
            log.warning("Agent 监视器 [%s] 旧 worker 未退出，拒绝重启", self.agent_key)
            return False
        if self._destroyed_conn is None:
            self._destroyed_conn = self.destroyed.connect(
                lambda *_: BaseAgentMonitor._destroyed_guard(self)
            )
        with self._destroy_guard_lock:
            self._destroy_guard_ran = False
        self._worker_stop.set()
        self._worker_stop = threading.Event()
        self._stop_async = threading.Event()
        self._gen += 1
        self._emit_gen = self._gen
        self._running = True
        self._paused = False
        self._pending_requests.clear()
        gen = self._gen
        self._worker = threading.Thread(
            target=self._async_main, args=(gen,), daemon=True,
            name=f"agent-monitor-{self.agent_key}",
        )
        self._worker.start()
        log.info(
            "Agent 监视器 [%s] 已启动 (%s, %d 工作区)",
            self.agent_key, self._server_url, len(self._workspace_ids),
        )
        return True

    def begin_stop(self) -> None:
        self._stop_async.set()
        super().begin_stop()

    def stop(self) -> None:
        self._stop_async.set()
        super().stop()

    # ---------------------------------------------------------------- GUI 线程入口

    def submit_reply(
        self, *, workspace_id: str, task_id: str, kind: str, request_id: str,
        permission_reply: str = "", answers: list[list[str]] | None = None,
    ) -> None:
        """GUI 线程投递一条应答；worker 线程内经 REST 发送（绝不阻塞 GUI）。"""
        loop = self._loop
        if loop is None or loop.is_closed() or self._reply_queue is None:
            return
        item = {
            "workspace_id": workspace_id, "task_id": task_id, "kind": kind,
            "request_id": request_id, "permission_reply": permission_reply,
            "answers": answers or [],
        }
        try:
            loop.call_soon_threadsafe(self._reply_queue.put_nowait, item)
        except RuntimeError:
            pass  # 循环已在关闭

    # ---------------------------------------------------------------- worker 线程

    def _async_main(self, gen: int) -> None:
        """线程主体：临时事件循环 + 连接协程 + 应答消费协程。"""
        try:
            asyncio.run(self._run_all(gen))
        except Exception:  # noqa: BLE001
            log.debug("Agent 监视器 [%s] worker 异常退出", self.agent_key, exc_info=True)

    async def _run_all(self, gen: int) -> None:
        try:
            import websockets  # noqa: F401 —— 可用性探测；worker 里再用
        except ImportError:
            log.error("websockets 未安装，swarm 监视器不可用（pip install websockets）")
            return
        self._loop = asyncio.get_running_loop()
        self._reply_queue = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._reply_consumer(gen)),
            *(asyncio.create_task(self._ws_worker(wid, gen)) for wid in self._workspace_ids),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks + [asyncio.create_task(self._stop_waiter())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.wait(pending, timeout=3.0)
        finally:
            self._loop = None
            self._reply_queue = None

    async def _stop_waiter(self) -> None:
        while not self._stop_async.is_set() and not self._worker_stop.is_set():
            await asyncio.sleep(0.2)
        if not self._stop_async.is_set():
            self._stop_async.set()

    async def _ws_worker(self, workspace_id: str, gen: int) -> None:
        """单个工作区的 WS 会话：连接 → hello → subscribe → 收流，断线退避重连。"""
        import websockets

        base = self._server_url
        ws_url = (base.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
                  + "/ws/nexus")
        delay = 1.0
        while not (self._stop_async.is_set() or self._emit_gen != gen):
            try:
                async with websockets.connect(ws_url, open_timeout=8.0,
                                              ping_interval=25, ping_timeout=20,
                                              max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"type": "hello", "apikey": self._api_key}))
                    hello_raw = await asyncio.wait_for(ws.recv(), timeout=8.0)
                    hello = json.loads(hello_raw)
                    if hello.get("type") != "hello_ok":
                        # 鉴权失败等：注册被拒不会自愈，退避后整体重试（key 可能被用户改）
                        reason = str(hello.get("error") or "hello rejected")
                        log.warning("swarm hello 被拒 (%s): %s", workspace_id[:8], reason)
                        self._emit(self.connection_error, (self.agent_key, reason))
                        await asyncio.sleep(min(delay, 30.0))
                        delay = min(delay * 2, 30.0)
                        continue
                    await ws.send(json.dumps({"type": "subscribe", "workspace_id": workspace_id}))
                    delay = 1.0
                    await self._consume(ws, workspace_id, gen)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 —— 断线重连是常态路径
                if self._stop_async.is_set() or self._emit_gen != gen:
                    return
                log.debug("swarm ws 断线 (%s): %s", workspace_id[:8], exc)
            await asyncio.sleep(min(delay, 30.0))
            delay = min(delay * 2, 30.0)

    async def _consume(self, ws: Any, workspace_id: str, gen: int) -> None:
        """收流主循环：subscribed 回放 → 实时 event/monitor/task 帧。"""
        while not (self._stop_async.is_set() or self._emit_gen != gen):
            raw = await ws.recv()
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = str(msg.get("type") or "")
            if mtype == "subscribed":
                # 回放去重：先整批过 _process_payload(replay=True) 缓冲/标记，
                # 再把未证实被答的缓冲统一弹窗（flush）。
                # 通配订阅（workspace_id="*"）服务端不做回放（history 为空）——
                # 简报从上线时刻开始听，历史走 REST；flush 空缓冲是无害 no-op。
                for event in (msg.get("history") or []):
                    if isinstance(event, dict):
                        self._process_payload(event, workspace_id, gen, replay=True)
                self._flush_replay(workspace_id, gen)
            elif mtype in ("event", "monitor"):
                payload = msg.get("payload")
                if isinstance(payload, dict):
                    self._process_payload(payload, workspace_id, gen, replay=False)
            elif mtype == "task":
                task = msg.get("task") or {}
                self._process_task_snapshot(task, workspace_id, gen)
            # ping/pong 由库层处理；其他类型忽略

    # ---------------------------------------------------------------- 事件处理

    def _process_payload(self, payload: dict, workspace_id: str, gen: int, *,
                         replay: bool) -> None:
        """A2A event / monitor 载荷统一入口（worker 线程内）。

        回放去重（对接文档 §5）：回放里的 input-required 不能立即弹——顺着回放
        看到后续 working/终态/resolved 才算「已被应答」。实现：回放期间缓冲
        input-required（记序），遇到 working/终态帧就把**同任务此前所有缓冲的
        requestId** 标记已答；回放结束（subscribed 处理完）统一把未标记者弹窗。
        """
        # ① input-required（A2A 轮）
        req = extract_input_required(payload)
        if req is not None:
            rid = req["requestId"]
            if rid in self._pending_requests:
                return
            if replay:
                # 缓冲待判：同任务后续 working/终态 → 已答；载荷留到 flush 时弹
                self._replay_buffer.append((req.get("task_id") or "", rid))
                self._replay_payloads[rid] = dict(req)
                return
            self._pending_requests.add(rid)
            task_id = req.get("task_id") or ""
            if task_id:
                self._task_requests.setdefault(task_id, set()).add(rid)
            req = dict(req, workspace_id=workspace_id)
            if str(req.get("type")) == "permission":
                self._emit(self.approval_requested, (self.agent_key, req))
            else:
                self._emit(self.question_requested, (self.agent_key, req))
            return
        # ② 监控轮（扁平）
        mon = monitor_payload(payload)
        if mon is not None:
            rid = mon["requestId"]
            if mon["type"] in ("permission", "question"):
                if rid in self._pending_requests:
                    return
                if replay:
                    self._replay_buffer.append((mon.get("task_id") or "", rid))
                    self._replay_payloads[rid] = dict(mon)
                    return
                self._pending_requests.add(rid)
                round_task = mon.get("task_id") or ""
                if round_task:
                    self._task_requests.setdefault(round_task, set()).add(rid)
                mon = dict(mon, workspace_id=workspace_id)
                if mon["type"] == "permission":
                    self._emit(self.approval_requested, (self.agent_key, mon))
                else:
                    self._emit(self.question_requested, (self.agent_key, mon))
            elif mon["type"] == "replied":
                # 他人已答（web/IM）或自己答完的服务端确认：收气泡
                if not replay:
                    self._emit(self.approval_resolved, (self.agent_key, {
                        "requestId": rid, "workspace_id": workspace_id,
                    }))
                self._pending_requests.discard(rid)
                # 回放里的 replied：把同请求的缓冲标记已答（幂等）
                self._replay_answered.add(rid)
            return
        # ③ 状态与简报
        state = str((payload.get("status") or {}).get("state") or "")
        if replay and state in ("working", *TERMINAL_STATES):
            # 回放中的 working/终态：同任务此前缓冲的 input-required 已被应答
            for task_id, rid in self._replay_buffer:
                if task_id and task_id == str(payload.get("taskId") or ""):
                    self._replay_answered.add(rid)
        if state == "working":
            self._emit_state("working", gen)
            return
        if state == "input-required":
            return  # 已在 ① 处理
        brief = brief_from_status(payload)
        if brief is not None:
            brief = dict(brief, workspace_id=workspace_id)
            self._emit(self.brief_ready, (self.agent_key, brief))
            if brief["state"] in TERMINAL_STATES:
                self._emit_state("idle", gen)
                # 终态把该任务遗留的待答请求清掉（任务已结束不可能再答）
                task_id = brief["task_id"]
                if task_id:
                    self._pending_requests = {
                        r for r in self._pending_requests
                        if r not in self._task_requests.get(task_id, set())
                    }
            return
        if state:
            normalized = {"completed": "idle", "failed": "error", "canceled": "idle"}.get(state)
            if normalized:
                self._emit_state(normalized, gen)

    def _flush_replay(self, workspace_id: str, gen: int) -> None:
        """回放结束：把缓冲里未被证实已答的 input-required 统一弹窗。"""
        for task_id, rid in self._replay_buffer:
            if rid in self._replay_answered:
                continue
            self._pending_requests.add(rid)
            if task_id:
                self._task_requests.setdefault(task_id, set()).add(rid)
            # 缓冲里只有 rid，载荷在缓冲时被丢——弹窗载荷在缓冲时一并保存更好，
            # 但回放事件本身有完整 data；改为缓冲时就保存完整载荷（见 _replay_buffer append 处）
            payload = self._replay_payloads.get(rid)
            if payload is None:
                continue
            payload = dict(payload, workspace_id=workspace_id)
            if str(payload.get("type")) == "permission":
                self._emit(self.approval_requested, (self.agent_key, payload))
            else:
                self._emit(self.question_requested, (self.agent_key, payload))
        self._replay_buffer.clear()
        self._replay_payloads.clear()
        self._replay_answered.clear()

    def _process_task_snapshot(self, task: dict, workspace_id: str, gen: int) -> None:
        """task 快照：状态刷新（他人已答的收气泡由 resolved/replied 帧承担）。"""
        if not isinstance(task, dict):
            return
        status = str(task.get("status") or "")
        if status == "working":
            self._emit_state("working", gen)
        elif status in TERMINAL_STATES:
            self._emit_state("idle" if status != "failed" else "error", gen)

    # ---------------------------------------------------------------- 应答消费

    async def _reply_consumer(self, gen: int) -> None:
        """应答队列消费：逐条调 swarm_responder.reply（线程内阻塞可接受）。"""
        from . import swarm_responder

        while not (self._stop_async.is_set() or self._emit_gen != gen):
            try:
                item = await asyncio.wait_for(self._reply_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            ok, detail = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda it=item: swarm_responder.reply(
                    self._server_url, it["workspace_id"], self._api_key,
                    task_id=it["task_id"], kind=it["kind"],
                    request_id=it["request_id"],
                    permission_reply=it["permission_reply"],
                    answers=it["answers"] or None,
                ),
            )
            if ok:
                self._pending_requests.discard(item["request_id"])
                # 应答受理后服务端自持 input-required → working 并推 task 快照；
                # 本地先按 requestId 收气泡（快照随后会确认）
                self._emit(self.approval_resolved, (self.agent_key, {
                    "requestId": item["request_id"],
                    "workspace_id": item["workspace_id"],
                }))
            elif detail == "conflict":
                # 先答先算：别处已答——静默收气泡，不是错误
                log.info("swarm reply 409 先答先算 (%s)", item["request_id"][:12])
                self._pending_requests.discard(item["request_id"])
                self._emit(self.approval_resolved, (self.agent_key, {
                    "requestId": item["request_id"],
                    "workspace_id": item["workspace_id"],
                }))
            else:
                log.warning("swarm reply 失败: %s (%s)", detail, item["request_id"][:12])
                # 发送失败：保留 pending（气泡留着，服务端 resolved/replied 会再收）
