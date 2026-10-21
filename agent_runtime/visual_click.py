"""视觉定位点击：让模型能「看着画面找目标」，而不只是按 DOM 锚点找。

## 这层补的是什么

Agent 工作台 里模型操控界面有两条路：

* **DOM 锚点**（`dev_page_action`）：用 CSS 选择器或可见文字定位。准、稳、可复核，
  但**对 canvas/图形界面完全无效** —— 游戏画面、图表、位图按钮在 DOM 里是空的。
* **视觉定位**（本模块）：把画面交给视觉模型，让它给出目标的 bbox。

后者的能力早就有（`/api/vision/locate-click` + `/api/vision/execute-click`），
但那条链只有 **HTTP 端点 + 前端**，**没有任何 agent 工具** —— 于是模型能看图、
能截图，却**不能看着画面找到要点的那个按钮**。本模块把这份能力接给模型。

## 一处实现，两个出口

`locate()` / `execute()` 是唯一实现；HTTP 端点与 agent 工具都调它：

* HTTP 出口的「用户确认」来自**前端点了确认**（`confirmed=true`）；
* agent 出口的「用户确认」来自**审批台账**（用户在工作台的审核队列里批准，
  agent 随后用**完全相同**的参数重试）。

两条出口共用同一份提案存储、同一套画面指纹复核、同一套复验判据 —— 所以
「UI 定位的提案」和「模型定位的提案」是同一种东西，谁都能执行。

## 三道闸，缺一不可

1. **确认**：HTTP 侧是 UI 点确认；agent 侧是审批台账里 `desktop_action` 的
   approved 记录。**模型自己说「我确认了」不算** —— 那等于自我授权。
   审批 target 绑定了点击参数哈希，批准之后改坐标必须重新批准。
2. **画面复核**：执行前重新抓帧，窗口句柄要一致、目标位置的像素指纹要一致。
   定位时看到的是这个按钮、点的时候已经不是了 —— 这时必须不点。
3. **提案单次使用**：标记必须在真正发点击**之前**，否则重复请求能连点两次。

## 视觉模型从哪来

按顺序找，找到一个**能力画像确认为原生视觉**的就用：

1. **开发舱固定模型**（`/api/vision/*` 端点用的那个）；
2. **当前主模型**（若它本身就是原生视觉模型）；
3. **Harness 视觉模型**（`DOCMIND_VISION_MODEL`，provider/key/base_url 同前缀环境变量）。

第 3 条是纯文本主模型时唯一能走通的路。**能力画像没确认视觉的一律不用**
（除非显式 `DOCMIND_VISION_ALLOW_UNVERIFIED`）—— 拿一个只会读文本的模型去猜坐标，
产出的是"看起来像"的位置，点下去就是点错地方。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from agent_runtime.visual_targeting import (
    parse_visual_target,
    parse_visual_verification,
    target_fingerprint,
    target_still_matches,
)

#: 提案只在内存里活一小会儿。模型定位到的窗口和像素会随画面随时失效，
#: 把提案长期存下来等于给「点错地方」留后门。
PROPOSAL_TTL_SECONDS = 120.0
MAX_PROPOSALS = 32
#: 点击后等界面反应再复验，否则截到的还是点击前的画面。
SETTLE_SECONDS = 0.35

#: action 名复用 `desktop_action`：审核队列（`cockpit_policy.RISK_LEVELS`）只认
#: 已登记的 action，而 `desktop_action` 正是「桌面点击/输入/保存」那类动作，
#: 用户在工作台的审核队列里点的就是它 —— 视觉点击与坐标点击在同一个面板批准，
#: 体验一致，也不会为了新造一个 action 而漏出队列。
GATE_ACTION = "desktop_action"


#: 提案存储（进程内内存）。
#:
#: 刻意**不放磁盘**：提案的有效性绑在"用户就在眼前看着这块画面"上，
#: 跨进程持久化既没必要也危险（过期提案会变成点错的后门）。
#:
#: 公开是有意的：HTTP 端点（api.py）与 agent 工具共用同一份，测试也要能清空它。
#: **必须是同一个对象**——搬成副本就变成"两份提案存储"，那是最坑的一种分叉：
#: UI 定位的提案会被模型说成"不存在"，模型定位的提案又没法在 UI 上确认。
PROPOSALS: dict[str, dict[str, Any]] = {}


def prune(now: float | None = None) -> None:
    """清掉过期提案，并给内存里存的提案数封顶。"""
    moment = float(now if now is not None else time.time())
    for key, entry in list(PROPOSALS.items()):
        if moment - float(entry.get("created_at") or 0) > PROPOSAL_TTL_SECONDS:
            PROPOSALS.pop(key, None)
    while len(PROPOSALS) > MAX_PROPOSALS:
        oldest = min(PROPOSALS, key=lambda item: PROPOSALS[item]["created_at"])
        PROPOSALS.pop(oldest, None)


# --------------------------------------------------------------------------- 提示词

def target_prompt(description: str) -> str:
    """让视觉模型在截图里找目标，只回一个 JSON。"""
    return (
        "你在看一张软件窗口截图。请找到用户描述的目标，并只返回一个 JSON 对象：\n"
        '{"found": true/false, "label": "目标名称", "evidence": "图上哪些可见文字或形状'
        '让你确认位置", "bbox": [x1, y1, x2, y2], "confidence": 0.0-1.0}\n'
        "bbox 用 0 到 1 的相对坐标，且必须框住目标本身，不要框整块区域。"
        "看不清、不确定、或图上没有这个目标时，found 填 false。"
        "evidence 必须引用画面上真实可见的内容，不要写推测。\n"
        f"用户要找的目标：{description}"
    )


def verification_prompt(goal: str, label: str) -> str:
    """点击后复验：两张同一窗口的图，判断目标是否达成。"""
    return (
        "给你两张同一窗口的截图：第一张是点击前，第二张是点击后。\n"
        "请判断下面这个目标是否已经达成，只返回一个 JSON 对象：\n"
        '{"status": "met"/"unmet"/"uncertain", "evidence": "两张图里能证明结论的具体'
        '差异", "confidence": 0.0-1.0, "next_target": "若未达成，建议下一个可点击目标"}\n'
        "只在第二张图确实出现了达成证据时才填 met；没有变化或看不出就填 unmet/uncertain。"
        "证据必须是画面上真实可见的内容，不要根据期望推断。\n"
        f"刚点击的目标：{label}\n验收条件：{goal}"
    )


# --------------------------------------------------------------------------- 视觉模型

def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _capability_is_vision(client: Any) -> bool:
    try:
        return (getattr(client, "capability", None) or {}).get("vision") == "native"
    except Exception:
        return False


def resolve_vision_llm(pid: str) -> tuple[Any, str, str]:
    """找一个能看图的 LLM 客户端。返回 (client, 来源, 失败原因)。

    来源分别是 `cockpit`（开发舱固定模型）/ `main`（当前主模型本身是视觉模型）/
    `harness`（`DOCMIND_VISION_MODEL`）。失败时 client 为 None，第三个返回值
    是**可执行**的中文说明（该去哪儿配、配什么）。
    """
    # ① 开发舱固定模型：HTTP 端点用的就是它，行为必须一致。
    try:
        from api import _get_cockpit_llm
        _preset, llm = _get_cockpit_llm(pid)
        if llm is not None:
            return llm, "cockpit", ""
    except Exception:
        pass
    # ② 当前主模型：主模型本身就是原生视觉模型时，直接用它。
    try:
        from config import get_runtime
        provider = str(get_runtime("llm_provider") or "").strip()
        model = str(get_runtime("llm_model") or "").strip()
        if provider and model:
            from llm import LLMClient
            client = LLMClient(provider=provider, model=model,
                               api_key=get_runtime("llm_api_key") or None,
                               base_url=get_runtime("llm_base_url") or None)
            if _capability_is_vision(client):
                return client, "main", ""
    except Exception:
        pass
    # ③ Harness 视觉模型：纯文本主模型时唯一能走通的路。
    try:
        model = str(os.getenv("DOCMIND_VISION_MODEL") or "").strip()
        if model:
            from llm import LLMClient
            provider = str(os.getenv("DOCMIND_VISION_PROVIDER") or "ollama").strip()
            client = LLMClient(provider=provider, model=model,
                               api_key=os.getenv("DOCMIND_VISION_API_KEY") or None,
                               base_url=os.getenv("DOCMIND_VISION_BASE_URL") or None)
            if _capability_is_vision(client):
                return client, "harness", ""
            if not _truthy(os.getenv("DOCMIND_VISION_ALLOW_UNVERIFIED")):
                return None, "", (
                    "Harness 视觉模型 %s/%s 未被能力画像确认支持图片识别，为避免误识别已跳过；"
                    "可在环境变量中显式允许未验证模型。"
                    % (provider, model))
    except Exception as exc:  # noqa: BLE001 - 配置坏了要能说清坏在哪
        return None, "", "构造 Harness 视觉模型失败：%s: %s" % (type(exc).__name__, str(exc)[:160])
    return None, "", (
        "当前没有可用的视觉模型。视觉定位需要模型能看图，三条路任选其一："
        "① 在开发舱指定带视觉能力的固定模型；② 把主模型换成原生视觉模型；"
        "③ 配置 DOCMIND_VISION_MODEL（如 qwen3-vl:8b）。"
        "不要用只读文本的模型去猜坐标 —— 那只会点错地方。")


# --------------------------------------------------------------------------- 定位

def locate(pid: str, *, description: str, goal: str = "") -> dict[str, Any]:
    """在嵌入窗口里视觉定位一个目标，登记一次性提案。**这一步不点击。**"""
    description = str(description or "").strip()[:200]
    if not description:
        return {"ok": False, "status": 400, "error": "description 不能为空。"}
    try:
        import screen_capture
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "画面捕获不可用：%s: %s" % (type(exc).__name__, str(exc)[:160])}
    try:
        frame = screen_capture.grab_embedded(pid, strict_project=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "抓取嵌入窗口失败：%s: %s" % (type(exc).__name__, str(exc)[:160])}
    if not frame:
        return {"ok": False, "status": 400,
                "error": "当前项目没有已登记的嵌入窗口，无法定位目标。"
                          "请先启动并嵌入项目窗口，或用 game_screenshot 之类的观察工具。"}
    raw, width, height, hwnd = frame
    try:
        image = screen_capture.encode_frame(raw, width, height)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "桌面帧编码失败：%s: %s" % (type(exc).__name__, str(exc)[:160])}
    if not image:
        return {"ok": False, "status": 502, "error": "桌面帧编码失败。"}
    llm, source, reason = resolve_vision_llm(pid)
    if llm is None:
        return {"ok": False, "status": 400, "error": reason}
    try:
        answer = llm.chat([{"role": "user", "content": target_prompt(description),
                            "images": [image]}],
                          stream=False, enable_thinking=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "视觉模型调用失败：%s: %s" % (type(exc).__name__, str(exc)[:200])}
    target = parse_visual_target(answer)
    if target is None:
        return {"ok": False, "status": 422,
                "error": "模型没有给出可信的目标位置（需要明确的 bbox、画面依据和 ≥0.8 的把握）。"
                          "换更具体的描述再试，例如「右上角那个蓝色的保存按钮」。"}
    bbox = target["bbox"]
    point = {"x": int(((bbox[0] + bbox[2]) / 2) * width),
             "y": int(((bbox[1] + bbox[3]) / 2) * height)}
    fingerprint = target_fingerprint(raw, width, height, bbox)
    now = time.time()
    prune(now)
    proposal_id = uuid.uuid4().hex
    PROPOSALS[proposal_id] = {
        "project_id": pid, "hwnd": int(hwnd), "width": int(width), "height": int(height),
        "bbox": bbox, "point": point, "fingerprint": fingerprint,
        "label": target["label"], "evidence": target["evidence"], "goal": str(goal or ""),
        "image": image, "created_at": now, "used": False,
    }
    return {"ok": True, "proposal_id": proposal_id, "image": image, "bbox": bbox,
            "point": point, "label": target["label"], "evidence": target["evidence"],
            "confidence": target["confidence"], "vision_source": source,
            "window": {"width": int(width), "height": int(height)},
            "expires_in": int(PROPOSAL_TTL_SECONDS)}


# --------------------------------------------------------------------------- 执行

def approval_target(point: dict[str, Any]) -> str:
    """审批 target：与 `dev_desktop_action` **同一套格式**。

    这样两件事在审核队列里长得一样（用户看到的都是"在某窗口点一下"），
    而且 target 绑定了坐标哈希 —— 批准之后换坐标必须重新批准。
    """
    payload = {
        "action": "click", "target": "embedded",
        "x": int(point.get("x") or 0), "y": int(point.get("y") or 0),
        "to_x": None, "to_y": None, "key": "",
        "text_sha256": hashlib.sha256("".encode("utf-8")).hexdigest()[:16],
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return "desktop:click:embedded:%s" % digest


def execute(pid: str, root: str, *, proposal_id: str, confirmed: bool = False,
            gate: bool = False) -> dict[str, Any]:
    """执行一个已登记的提案。

    - `confirmed=True`：HTTP 出口用（前端已让用户点过确认）。
    - `gate=True`：agent 出口用 —— 确认来自**审批台账**（用户在审核队列批准过）。
      模型自己说的「确认」不算，所以这里不接受任何形式的自我授权。
    """
    proposal_id = str(proposal_id or "").strip()
    if not proposal_id:
        return {"ok": False, "status": 400, "error": "缺少 proposal_id。"}
    prune()
    entry = PROPOSALS.get(proposal_id)
    if entry is None or entry["project_id"] != pid:
        # 别的项目的提案在这里「不存在」，不回 403：不向调用方泄露它是否存在。
        return {"ok": False, "status": 404, "error": "提案不存在或已过期，请重新定位。"}
    if entry["used"]:
        return {"ok": False, "status": 409, "error": "该提案已经执行过一次，请重新定位。"}
    if not confirmed and not gate:
        return {"ok": False, "status": 400,
                "error": "点击需要用户确认（confirmed=true，或先在审核队列里批准）。"}
    # 闸①：agent 出口的确认来自审批台账。
    if gate:
        try:
            from game_workbench import require_approval
            blocked = require_approval(root, GATE_ACTION, approval_target(entry["point"]))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 502,
                    "error": "审批门检查失败：%s: %s" % (type(exc).__name__, str(exc)[:160])}
        if blocked:
            blocked = dict(blocked)
            # 补 `ok: False`：`require_approval` 的阻断结构里没有这个键，
            # 而本函数所有其它失败分支都带它 —— 契约不统一的话，调用方
            # （agent 工具、HTTP 端点）就得为这一种失败单独写判断。
            blocked["ok"] = False
            blocked["status"] = 403
            blocked["hint"] = ("请用户在工作台的审核队列里批准这次点击；"
                               "批准后必须用完全相同的参数重试。")
            return blocked
    try:
        import screen_capture
        frame = screen_capture.grab_embedded(pid, strict_project=True)
        if not frame:
            return {"ok": False, "status": 409, "error": "目标窗口已不可见，请重新定位。"}
        raw, width, height, hwnd = frame
        if int(hwnd) != int(entry["hwnd"]):
            return {"ok": False, "status": 409,
                    "error": "画面已变化（目标窗口已切换），请重新定位。"}
        if not target_still_matches(entry["fingerprint"],
                                    target_fingerprint(raw, width, height, entry["bbox"])):
            return {"ok": False, "status": 409,
                    "error": "画面已变化（目标位置的内容已不同），请重新定位。"}
        before = screen_capture.encode_frame(raw, width, height)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "点击前复核失败：%s: %s" % (type(exc).__name__, str(exc)[:200])}
    try:
        import desktop_actions
        row = None
        try:
            import game_workbench
            row = game_workbench.approval(root, GATE_ACTION, "agent", approved=True,
                                          target=entry["label"])
        except Exception:
            row = None
        if not (row or {}).get("approved"):
            return {"ok": False, "status": 403, "error": "这次点击没有获得批准。"}
        # 标记必须在真正发点击之前：否则重复请求能连点两次。
        entry["used"] = True
        result = desktop_actions.perform("click", target="embedded", project_id=pid,
                                         x=entry["point"]["x"], y=entry["point"]["y"],
                                         expected_hwnd=entry["hwnd"])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 502,
                "error": "点击失败：%s: %s" % (type(exc).__name__, str(exc)[:200])}
    if not (result or {}).get("ok"):
        return {"ok": False, "executed": False, "before": before, "used": True,
                "status": 502,
                "error": str((result or {}).get("error") or "点击未能送达目标窗口。")}
    time.sleep(SETTLE_SECONDS)
    after = None
    try:
        after_frame = screen_capture.grab_embedded(pid, strict_project=True)
        # 窗口换了就不拿别的窗口的画面当「点击结果」——那会验证出一件没发生的事。
        if after_frame and int(after_frame[3]) == int(entry["hwnd"]):
            after = screen_capture.encode_frame(after_frame[0], after_frame[1], after_frame[2])
    except Exception:
        after = None
    llm, _source, _reason = resolve_vision_llm(pid)
    # 「复验不了」和「没要求复验」是两件事：前者要明说，后者不必报。
    unavailable = ""
    if after is None:
        unavailable = "点击后没有拿到同一窗口的画面，本轮无法复验。"
    elif entry["goal"] and not (before and llm is not None):
        unavailable = "缺少可用的视觉模型或点击前画面，本轮无法复验。"
    verification = None
    if unavailable:
        verification = {"status": "unavailable", "evidence": unavailable,
                        "confidence": 0.0, "next_target": ""}
    elif entry["goal"]:
        verdict = None
        try:
            answer = llm.chat([{"role": "user",
                                "content": verification_prompt(entry["goal"], entry["label"]),
                                "images": [before, after]}],
                              stream=False, enable_thinking=False)
            verdict = parse_visual_verification(answer)
        except Exception:
            verdict = None
        verification = verdict or {"status": "uncertain",
                                   "evidence": "模型没有给出可信的复验结论。",
                                   "confidence": 0.0, "next_target": ""}
    return {"ok": True, "executed": True, "before": before, "after": after,
            "label": entry["label"], "point": entry["point"], "used": True,
            "verification": verification}


__all__ = ["GATE_ACTION", "MAX_PROPOSALS", "PROPOSALS", "PROPOSAL_TTL_SECONDS", "SETTLE_SECONDS",
           "approval_target", "execute", "locate", "prune", "resolve_vision_llm",
           "target_prompt", "verification_prompt"]
