# -*- coding: utf-8 -*-
"""Agent Swarm nexus 应答模块（审批/问题的决策经 REST 交还 swarm 服务器）。

桌宠气泡内点选「同意/拒绝/选项」后，以 web 中枢同款协议 POST 给
``/api/nexus/{workspace_id}/reply``（Bearer apikey 鉴权）。只在后台线程调用，
绝不阻塞 Qt 主线程；失败/409 返回 (ok, reason) 由上层处置——409 是
「先答先算」（web/TUI/飞书/微信已有人先答），**不是错误**，上层应静默收气泡。

网络策略（有意为之，勿改）：跟随系统代理——与 chat/余额/更新检查同一阵营
（pet/urllib 默认行为即读系统/环境代理），**不是**歌词那种 deliberate bypass；
见 docs/NETWORK-PROXY-AND-VPN-2026-09-22.md。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("dsh-pet-standalone")

TIMEOUT_S = 8.0


def _request_json(
    url: str, api_key: str, body: dict, *, timeout_s: float | None = None,
) -> tuple[int, dict]:
    """POST 一条 JSON 请求，返回 (http_status, parsed_json)。

    网络错误抛异常；HTTP 4xx/5xx **不**抛（返回状态码让调用方区分 409），
    响应体解析失败按空 dict 处理。
    """
    timeout = TIMEOUT_S if timeout_s is None else max(0.01, float(timeout_s))
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536).decode("utf-8", "replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 带错误体返回（409 先答先算的语义就靠状态码传递）
        raw = exc.read(65536).decode("utf-8", "replace")
        status = exc.code
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return int(status), parsed


def reply(
    server_url: str,
    workspace_id: str,
    api_key: str,
    *,
    task_id: str,
    kind: str,
    request_id: str,
    permission_reply: str = "",
    answers: list[list[str]] | None = None,
    timeout_s: float | None = None,
) -> tuple[bool, str]:
    """应答一条权限/提问，返回 (ok, detail)。

    - kind="permission"：permission_reply ∈ {"once", "always", "reject"}
    - kind="question"：answers 为非空二维数组（web 同款 string[][]）
    - 200 → (True, "ok")；409 → (False, "conflict")（先答先算，上层静默收气泡）；
      其它状态码/网络错误 → (False, 原因串)。
    """
    if not server_url or not workspace_id or not api_key:
        return False, "not-configured"
    base = server_url.rstrip("/")
    url = f"{base}/api/nexus/{workspace_id}/reply"
    body: dict = {"task_id": task_id, "type": kind, "request_id": request_id}
    if kind == "permission":
        if permission_reply not in ("once", "always", "reject"):
            return False, "bad-permission-reply"
        body["reply"] = permission_reply
    elif kind == "question":
        if not answers:
            return False, "bad-answers"
        body["answers"] = [[str(a) for a in row] for row in answers]
    else:
        return False, "bad-kind"
    try:
        status, parsed = _request_json(url, api_key, body, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 —— 后台线程绝不把异常带进调用方
        log.debug("swarm reply 网络失败: %s", exc)
        return False, str(exc)
    if status == 200:
        return True, "ok"
    if status == 409:
        # 先答先算：任务已被 web/TUI/IM 应答（或插件离线）。区分细节供日志。
        reason = str(parsed.get("detail") or parsed.get("error") or "conflict")
        return False, "conflict" if "not online" not in reason else "plugin-offline"
    detail = str(parsed.get("detail") or parsed.get("error") or "")
    return False, f"http-{status}" + (f": {detail}" if detail else "")
