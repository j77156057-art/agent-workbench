"""`POST /api/chat` 的 SSE 真问答链路：判定表（纯函数，零依赖，CI 跑得到）。

**为什么要有这一份**：这个端点的 SSE 包装层早就有 6 条用例，但它们**全部**在
`Agent.run` 那一层打了桩——也就是说经由 `/api/chat`，`agent.py` 的 ReAct 主循环、
`llm.py` 的 provider 分派、`tools.py` 的工具执行，一条都没真跑过。本模块给那半提供
**独立判据**：收到一个 `final` 事件**不等于**跑通一轮问答。

`agent.py` 里有六条**不调模型也能产出 `final`** 的早退路径（容量、预算熔断、限流、
pre_turn 钩子、歧义追问、`_evidence_final`），所以判据必须逐条把它们挑出来：

| 早退 | 特征 | 判据 |
|---|---|---|
| 容量不够直接答 | `final.offline = True` | `final_is_not_an_offline_capacity_reply` |
| 歧义追问 | `final.clarification_required = True` | `final_is_not_a_clarification_request` |
| 强制收尾拼答案 | `final.degraded = True` | `final_is_not_degraded` |
| 任何异常 | `final.status = "error"` + `error_kind` | `final_is_not_an_error` |
| 截断/空响应兜底 | 固定文案 | 由 `final_quotes_a_tool_observation` 挡掉 |

还有两条是**协议层**的假象：`route` 与 `done` 是**无条件**发出的（`api.py:2349`/`2430`），
所以"收到了 route + done"零信息量——本模块**不把它们算作证据**，只查收尾与增量。
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 会碰网络的工具。离线这一档里出现任何一个，整轮就不算"零网络跑通"了。
NETWORK_TOOLS = ("web_search", "http_fetch", "browse")

#: `mock` provider 在观察里看见这个词就改调 `web_search`（`llm.py:1108`）。
#: 它出现在观察里说明检索没命中——那不是"测试通过"，是这次跑已经联网去了。
MISS_MARKER = "未找到"

#: 引用判据的窗口长度：太短会撞上巧合（标点、常见词），太长会脆（空白/截断）。
QUOTE_WINDOW = 12


def parse_events(body: Any) -> list[dict[str, Any]]:
    """把 SSE 响应体解成事件表。

    本仓的 SSE **没有 `event:` 行**，类型都在 payload 的 `type` 字段里
    （`api.py` 全程 `data: {json}`）。`: keep-alve` 那种注释心跳不是事件，必须丢掉——
    把它当事件的话，"流有心跳"会被误读成"流在产出内容"。
    """
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) \
        else str(body or "")
    events: list[dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue                     # `:` 心跳、空行、以及任何非 data 行
        chunk = line[len("data:"):].strip()
        if not chunk:
            continue
        try:
            payload = json.loads(chunk)
        except ValueError:
            continue                     # 半截 JSON 不算事件，也不该把整轮带崩
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _chunks(body: Any) -> int:
    """响应体里 `data:` 行的条数——用来判"流式"而不是"一个大块"。

    必须数**原始行**而不是事件条数：一次完整的回答会被拆成几十个 `token` 事件，
    但如果整轮只吐了一个 `data:` 块，那也可能是被缓冲后一次性吐的——
    而"在第一个 token 就 break 的流"和"完整推理的流"在只看事件类型时长得一样（§9）。
    """
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) \
        else str(body or "")
    return sum(1 for line in text.splitlines() if line.strip().startswith("data:"))


def _tool_name(payload: dict[str, Any]) -> str:
    """从一个 `action` 事件里取出工具名。

    真实载荷是 `{"type":"action","text":"search_assets(像素风角色素材有哪些？)","step_cost":1}`
    —— 工具名在 `text` 里，不在独立字段上。第一版按 `payload["tool"]` 取，于是永远拿到
    `None`，`no_network_tool_was_called` 就成了**恒真**的空判据（§9：判据可以结构上永远为真）。
    所以这里必须真的从文本里解析出名字，解析不出来也要**当没解析出来**而不是当成"没有工具"。
    """
    for key in ("tool", "name", "action"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value.split("(", 1)[0].strip()
    text = str(payload.get("text") or "").strip()
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(", text)
    return match.group(1) if match else ""


def _observation_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "")
    return text.rsplit("Observation:", 1)[-1] if "Observation:" in text else text


def _quotes(final_text: str, observations: list[str]) -> bool:
    """`final` 里必须**原样**带着某条工具观察的一截——否则它可能是凭空写的。

    这里刻意**不**复刻 `mock` 的取片段方式（`llm.py:1117-1118` 那种 split/取首行/截 200），
    照抄会把判据和被测的桩绑成同一份代码：桩改了格式，判据跟着改，于是照样绿。
    改成"观察里任意一个 12 字窗口出现在 final 里"——两个独立来源互相印证（§5.10）。
    """
    body = str(final_text or "")
    for text in observations:
        for line in _observation_text({"text": text}).splitlines():
            line = line.strip()
            if len(line) < QUOTE_WINDOW:
                continue
            for start in range(0, len(line) - QUOTE_WINDOW + 1):
                if line[start:start + QUOTE_WINDOW] in body:
                    return True
    return False


def verdict(events: list[dict[str, Any]], *, chunk_count: int | None = None,
            tools_called: list[str] | None = None) -> dict[str, Any]:
    """判定表。返回 `{"checks": {...}, "failed": [...], "passed": bool, "why": str}`。"""
    types = [str(row.get("type") or "") for row in events]
    actions = [row for row in events if str(row.get("type") or "") == "action"]
    observations = [row for row in events if str(row.get("type") or "") == "observation"]
    tokens = [row for row in events if str(row.get("type") or "") == "token"]
    finals = [row for row in events if str(row.get("type") or "") == "final"]
    final = finals[-1] if finals else {}
    final_text = str(final.get("text") or "")
    called = [str(name) for name in (tools_called if tools_called is not None
                                     else [_tool_name(row) for row in actions])]
    # 解析不出工具名的 action 不当"没有工具"：那是"没读出来"，必须显式变成红项
    unnamed = [row for row in actions if not _tool_name(row)]
    obs_texts = [str(row.get("text") or "") for row in observations]
    reasons: list[str] = []

    checks = {
        # 协议形状
        "stream_had_more_than_one_chunk": (chunk_count is None or chunk_count >= 2),
        "tokens_were_streamed": len(tokens) >= 1,
        "done_terminates_the_stream": bool(types) and types[-1] == "done",
        # 工具往返：N 个 action 必须对应 N 条 observation，而不是"有 final 就算了"
        "at_least_one_tool_action": len(actions) >= 1,
        "every_action_has_an_observation": len(observations) >= len(actions),
        "no_network_tool_was_called": not [name for name in called if name in NETWORK_TOOLS],
        "every_action_names_its_tool": not unnamed,
        "no_miss_marker_in_observations": not [text for text in obs_texts
                                               if MISS_MARKER in text],
        # final 的六条早退
        "final_is_present": bool(finals),
        "final_is_not_an_error": (not final) or (str(final.get("status") or "") != "error"
                                                 and not final.get("error_kind")),
        "final_is_not_an_offline_capacity_reply": (not final) or not final.get("offline"),
        "final_is_not_degraded": (not final) or not final.get("degraded"),
        "final_is_not_a_clarification_request": (not final) \
            or not final.get("clarification_required"),
        # 凭据：final 必须引述工具观察得到的东西，而不是凭空一句话
        "final_quotes_a_tool_observation": _quotes(final_text, obs_texts),
    }

    if not checks["stream_had_more_than_one_chunk"]:
        reasons.append("整轮只吐了 %s 个 data 块——那不是流式" % chunk_count)
    if not checks["at_least_one_tool_action"]:
        reasons.append("没有任何 action：一轮问答没调工具，这条链路等于没被验到")
    elif not checks["every_action_has_an_observation"]:
        reasons.append("action %d 条但 observation 只有 %d 条" % (len(actions),
                                                                 len(observations)))
    net = [name for name in called if name in NETWORK_TOOLS]
    if net:
        reasons.append("这一轮调了会联网的工具 %s——它证明的不是零网络链路" % net)
    if unnamed:
        reasons.append("有 %d 个 action 读不出工具名：%s——这条链路不能证明它没联网"
                       % (len(unnamed), [str(row.get("text"))[:60] for row in unnamed]))
    if [text for text in obs_texts if MISS_MARKER in text]:
        reasons.append("观察里出现「%s」：检索没命中，mock 会改调 web_search" % MISS_MARKER)
    if not finals:
        reasons.append("没有 final 事件")
    else:
        for name, why in (("final_is_not_an_error", "final 是 error"),
                          ("final_is_not_an_offline_capacity_reply",
                           "final 是容量兜底（offline），一条模型都没调"),
                          ("final_is_not_degraded", "final 是强制收尾拼的（degraded）"),
                          ("final_is_not_a_clarification_request",
                           "final 只是追问，不是回答")):
            if not checks[name]:
                reasons.append(why)
        if not checks["final_quotes_a_tool_observation"]:
            reasons.append("final 没有原样引述任何一条工具观察——它可能不是从工具结果来的")

    failed = [name for name, ok in checks.items() if not ok]
    return {"checks": checks, "failed": failed, "passed": not failed,
            "observed": {"types": types, "actions": [_tool_name(row) for row in actions],
                         "n_observations": len(observations), "n_tokens": len(tokens),
                         "chunk_count": chunk_count, "final": final},
            "why": "；".join(reasons) or "判定全部成立"}


def render(result: dict[str, Any]) -> str:
    head = "问答 SSE 链路【%s】" % ("通过" if result.get("passed") else "未通过")
    rows = result.get("checks") or {}
    if not rows:
        return "%s：%s" % (head, result.get("why") or "没有可判的事件")
    body = "\n".join("  [%s] %s" % ("x" if ok else " ", name)
                     for name, ok in sorted(rows.items()))
    return "%s\n%s\n%s" % (head, body, result.get("why") or "")


__all__ = ["NETWORK_TOOLS", "MISS_MARKER", "QUOTE_WINDOW", "parse_events", "verdict", "render"]
