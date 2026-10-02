"""离线 OpenAI 兼容 LLM 服务：让整条问答链路在**零网络**下跑到**生产 provider 那条路**。

**它补的是什么**：`tests/test_chat_sse.py` 里的真问答回合是往 `api._agent_for` 的
Agent 上**注入**一个剧本 LLM——那验的是 ReAct 循环、工具执行与 SSE 包装，但**不经过**
`llm.py` 的 provider 选择、`base_url` 解析、OpenAI SDK 那条真实的客户端路径。
而 `dev_apptest` 把 uvicorn 起成**子进程**，注入法在那里根本不适用。

**做法**（与 Wave19a 的 `realtime_scripted` 同型：一个只在显式选中时才生效的测试双）：
`llm.py:567-571` 对 `provider == "custom"` 会采纳 `LLM_BASE_URL` 环境变量，所以子进程只要带
`LLM_PROVIDER=custom` + `LLM_BASE_URL=http://127.0.0.1:<port>/v1` 就能连到本模块起的
假服务——**不碰任何共享文件**（`llm.py` / `api.py` / `config.py` 一个字没动），
`custom` 本来就是产品里的正式 provider。

**凭据设计照 §5.10**：剧本回合的内容是从**收到的请求**现算的（引述工具观察里的一截），
所以"回包里有这些东西"只能解释为"这条路真的跑通了"，而不是"桩写死了一句话"。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: 第一轮要求它去查精选素材目录（读仓内 `assets_catalog.json`，零网络）。
#: 用素材类问题是为了让工具落在本地目录那条路上——`mock` 在观察里看见「未找到」会改调
#: 联网搜索，这里同理不能让剧本走到联网分支。
ASSET_QUERY = "像素风角色"

#: 引用窗口：与 `chat_sse.QUOTE_WINDOW` 同一个用意（太短会撞巧合，太长会脆）。
QUOTE_WINDOW = 12

#: ReAct 正文里的动作行（`Action: search_assets`）。**不要**按事件形状的
#: `name(args)` 写——那样在这段剧本上一个都匹配不到（实测：返回空串，
#: 而引用判据要拿它做断言）。
ACTION_RE = re.compile(r"^Action:\s*([A-Za-z_][A-Za-z0-9_]*)", re.M)


def last_user_text(messages: Any) -> str:
    """取最后一条用户消息的正文（OpenAI 格式里 content 可能是字符串或分片列表）。"""
    for row in reversed(list(messages or [])):
        if str((row or {}).get("role") or "") != "user":
            continue
        content = (row or {}).get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str((part or {}).get("text") or "") for part in content
                           if isinstance(part, dict))
    return ""


#: 每种动作"它的输出行长什么样"。凭据必须落在被引述的那一行里，所以**不能**靠
#: 位置（首行/末行）挑——实测两条都不行：`list_dir` 的首行是表头
#: （`目录 (代码库根) 共 1 项：`），而观察的**末行常常是 harness 的提示**
#: （`（请基于观察继续，或给出 Final Answer）`），引述它等于把凭据换成一句套话。
_LINE_HINTS = {
    "search_assets": re.compile(r"^[·•]"),
    "list_dir": re.compile(r"^\[(FILE|DIR)\]"),
}

#: `list_dir` 输出里带文件名的那些行——用来回看"这一轮到底列出了哪个文件"。
#: 扫**所有**消息是安全的：`[FILE] ` 这种形状不会出现在系统提示里
#: （与 `Observation:` 不同，见 `observation_snippet` 的注释）。
FILE_RE = re.compile(r"\[FILE\]\s+(\S+)")

#: harness 的纠正提示前缀。桩要**听懂**它们，否则会一直重复同一个工具调用、直到被强制
#: 收尾——实测那样一轮问答跑了 6 轮，最后靠"模型反复空转"的兜底才停，而且渲染出来的
#: 是兜底证据块而不是一条干净的答复。这些前缀一出现就说明"该收尾了，别再调工具"。
CORRECTIVE = ("Evidence check:", "Reflection:", "Nudge:")


def observation_snippet(messages: Any, *, action: str = "search_assets") -> str:
    """取**最后一条**消息里、**这个工具真正产出**的那一行——Final Answer 要原样引述它。

    **只看最后一条**，不要在所有消息里搜 `Observation:`：系统提示里本身就有一大段
    解释 ReAct 格式的文字（含 "Observation:" 这个词），搜全局会把那段提示当成工具结果，
    于是第一轮就直接作答、一个工具都不调。`_mock_chat` 用的就是"只看 last"这条窄规则
    （`llm.py:1108`），这里照它——实测第一版搜全局就是这么翻车的。

    **按动作找行，不按位置找行**：见 `_LINE_HINTS` 上的注释——首行与末行实测各错一次。
    找不到该工具的签名行时退回首行非空行（宁可弱一点，也不要静默变成空串）。
    """
    rows = list(messages or [])
    text = str((rows[-1] or {}).get("content") or "") if rows else ""
    if "Observation:" not in text:
        return ""
    tail = text.rsplit("Observation:", 1)[-1]
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    hint = _LINE_HINTS.get(str(action or ""))
    if hint is not None:
        for line in lines:
            if hint.match(line):
                return line[:200]
    return lines[0][:200] if lines else ""


def reply_for(messages: Any, *, action: str = "search_assets") -> str:
    """一个回合的正文：先调工具，再引述工具结果作答。

    两条分支与 `agent.py` 的契约对齐：**必须带 `Final Answer:`**——
    `_mock_chat` 就是因为裸文本收尾而被格式失守判定连拒两轮（见 docs §6 缺口 14），
    这里不重蹈。

    `action` 决定第一轮调哪个工具：`search_assets` 走精选素材目录（问答页的示范问题多半
    是代码类，命中不了）；`list_dir` 列代码库根，**返回里带着那一次运行独有的随机文件名**
    ——所以它才是页面那一层要的凭据（照 §5.8 那条"夹具是随机命名的文件"的做法）。
    """
    rows = list(messages or [])
    last = str((rows[-1] or {}).get("content") or "") if rows else ""
    # 听懂纠正提示：它一出现就必须收尾，别再调工具（否则空转到被强制收尾）。
    if any(mark in last for mark in CORRECTIVE):
        known = ""
        for row in reversed(rows):
            found = FILE_RE.search(str((row or {}).get("content") or ""))
            if found:
                known = "[FILE] %s" % found.group(1)
                break
        return ("Thought: 就用已有证据\n"
                "Final Answer: 根据检索到的内容，%s —— 以上就是可用清单。"
                % (known or "当前无法确认"))
    snippet = observation_snippet(messages, action=action)
    if snippet:
        return ("Thought: 证据够了\n"
                "Final Answer: 根据检索到的内容，%s —— 以上就是可用清单。" % snippet)
    if action == "list_dir":
        return ("Thought: 先看一眼代码库根\n"
                "Action: list_dir\n"
                "Action Input: .")
    question = last_user_text(messages) or ASSET_QUERY
    return ("Thought: 先查精选素材目录\n"
            "Action: search_assets\n"
            "Action Input: %s" % (ASSET_QUERY if "素材" in question or "像素" in question
                                  else question[:80]))


def tool_name_in(text: str) -> str:
    """从剧本正文里取出它将要调用的工具名——解析不出就是空串。"""
    match = ACTION_RE.search(str(text or ""))
    return match.group(1) if match else ""


def sse_frames(text: str, *, model: str = "stub-1", usage: dict | None = None) -> bytes:
    """把一段正文渲染成 OpenAI 的 SSE 分片。

    形状照 `llm.py:274-295` 的消费方式写：每个分片 `choices[0].delta.content` 带一小段
    正文；末尾一个 `choices` 为**空**的分片带 `usage`（`if not chunk.choices: continue`
    正是为它准备的）；最后 `data: [DONE]`。
    """
    base = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": model}
    out: list[str] = []
    step = 24                                    # 每片 24 个字符，够真流式、又不至于几千片
    for start in range(0, len(text), step):
        piece = text[start:start + step]
        out.append(json.dumps({**base, "choices": [
            {"index": 0, "delta": {"content": piece}, "finish_reason": None}]},
            ensure_ascii=False))
    out.append(json.dumps({**base, "choices": [
        {"index": 0, "delta": {}, "finish_reason": "stop"}]}, ensure_ascii=False))
    out.append(json.dumps({**base, "choices": [], "usage": usage or {
        "prompt_tokens": 40, "completion_tokens": max(1, len(text) // 3),
        "total_tokens": 40 + max(1, len(text) // 3)}}, ensure_ascii=False))
    return ("".join("data: %s\n\n" % row for row in out) + "data: [DONE]\n\n").encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:   # noqa: D102 - 静音，别淹测试输出
        pass

    def do_GET(self) -> None:                    # noqa: N802 - BaseHTTPRequestHandler 的约定
        if self.path.rstrip("/").endswith("/models"):
            payload = json.dumps({"object": "list", "data": [
                {"id": "stub-1", "object": "model", "owned_by": "docmind-stub"}]}).encode()
            self._send(200, "application/json", payload)
            return
        self._send(404, "application/json", b'{"error":"not found"}')

    def do_POST(self) -> None:                   # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send(404, "application/json", b'{"error":"not found"}')
            return
        text = reply_for(body.get("messages"),
                         action=str(getattr(self.server, "stub_action", "search_assets")))
        if body.get("stream"):
            self._send(200, "text/event-stream", sse_frames(text))
            return
        payload = json.dumps({"id": "chatcmpl-stub", "object": "chat.completion",
                              "model": body.get("model") or "stub-1",
                              "choices": [{"index": 0, "message": {"role": "assistant",
                                                                  "content": text},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 40,
                                        "completion_tokens": max(1, len(text) // 3),
                                        "total_tokens": 40 + max(1, len(text) // 3)}},
                             ensure_ascii=False).encode("utf-8")
        self._send(200, "application/json", payload)

    def _send(self, status: int, ctype: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start(*, action: str = "search_assets") -> dict[str, Any]:
    """在回环上起一个假服务，返回 `{"url", "port", "server", "thread", "action"}`。

    `action` 决定剧本第一轮调哪个工具（见 `reply_for`）：问答页那一层用 `list_dir`，
    因为它的返回里带着那一次运行独有的随机文件名。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.stub_action = action
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="docmind-llm-stub",
                              daemon=True)
    thread.start()
    port = int(server.server_address[1])
    return {"url": "http://127.0.0.1:%d/v1" % port, "port": port,
            "server": server, "thread": thread}


def stop(handle: dict[str, Any] | None) -> None:
    if not handle:
        return
    try:
        handle["server"].shutdown()
    finally:
        handle["server"].server_close()
        handle["thread"].join(timeout=3)


def env_for(handle: dict[str, Any], *, model: str = "stub-1") -> dict[str, str]:
    """把子进程要带的环境变量装好（`custom` provider + 回环 base_url）。"""
    return {"LLM_PROVIDER": "custom", "LLM_BASE_URL": str(handle["url"]),
            "LLM_MODEL": model, "LLM_API_KEY": "stub-key",
            # 别让探活去碰本机的 Ollama：这一档要的是零网络
            "DOCMIND_OLLAMA_PROBE": "0", "EMBEDDING_PROVIDER": "local"}


__all__ = ["ASSET_QUERY", "QUOTE_WINDOW", "last_user_text", "observation_snippet",
           "reply_for", "tool_name_in", "sse_frames", "start", "stop", "env_for"]
