"""LLM 客户端：用 OpenAI 兼容协议统一封装 通义千问 / DeepSeek / Ollama / mock。

设计意图（与 Android 端 AIApiClient 一致）：把不同厂商的大模型收敛到同一套
OpenAI chat/completions 接口后面，运行时切换 provider 即可，业务代码无需改动。
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from gpu_coordinator import acquire as _gpu_acquire, release as _gpu_release, note_activity as _gpu_note_activity

from openai import OpenAI

from config import (
    LLM_PROVIDER, PROVIDERS, LLM_MODEL, LLM_API_KEY,
    LLM_MAX_TOKENS, LLM_ENABLE_THINKING, PROMPT_TOKEN_BUDGET, get_runtime,
    model_capability, prompt_token_budget, get_context_window_override,
    output_token_budget, resolve_context_from_docs, _PROVIDER_CONTEXT_DEFAULT,
)
from agent_runtime.local_runtime import local_llm_slot
from textutil import as_text

# 默认单次 LLM 调用超时（秒）与重试策略（可用环境变量覆盖）
LLM_TIMEOUT = float(os.getenv("DOCMIND_LLM_TIMEOUT", "180"))
# 总尝试次数（含首次）。
#
# 为什么从 3 提到 4：**这不是“多试一次”的好意，而是放宽退避的算术前提**。
# 退避发生在第 i 次失败之后，i 只取 0..attempts-2，所以 attempts=3 只允许 2 段退避。
# 实测（2026-10-03 账本）免费中转的“空响应”故障窗口约 8–9 秒才恢复，而旧的
# 0.8s→1.6s 两段退避合计仅约 2.4 秒——**3 次尝试全部落在同一个故障窗口内**，
# 于是重试等于没重试，整轮照样失败（实测 elapsed 2153ms / 4279ms，
# 与“3 次秒回空信封 + 短退避”的推算吻合）。
# attempts=4 才给出第 3 段退避，把总覆盖拉到 15–27 秒。
LLM_RETRIES = int(os.getenv("DOCMIND_LLM_RETRIES", "4"))       # 总尝试次数（含首次）
# 指数退避基数（秒）。配合下面的上限，3 段退避合计约 15–27 秒（含抖动）。
# 这是**全局**放宽：付费托管 API 真出故障时会多等一会儿才放弃，是有意接受的代价。
LLM_RETRY_BASE = float(os.getenv("DOCMIND_LLM_RETRY_BASE", "3.0"))
# 单段退避上限（秒）。原先硬编码 8.0 藏在 retry_call 里，调参时根本看不见。
LLM_RETRY_CAP = float(os.getenv("DOCMIND_LLM_RETRY_CAP", "12.0"))
# 默认采样温度；golden 回归实验用 DOCMIND_LLM_TEMPERATURE=0 关闭随机性以测稳定性。
# 默认 0.3 与历史行为一致；仅在显式覆盖时改变，不影响其它调用方。
LLM_TEMPERATURE = float(os.getenv("DOCMIND_LLM_TEMPERATURE", "0.3"))

# 可重试的错误特征：限流 / 5xx / 网络与超时。鉴权/参数类（401/403/404）不重试。
_RETRYABLE_HINTS = (
    "429", "rate limit", "rate_limit", "too many requests",
    "500", "502", "503", "504", "server error", "internal error",
    "timed out", "timeout", "temporarily unavailable", "overloaded",
    "connection", "reset by peer", "econnreset", "broken pipe", "eof",
)


def _status_of(exc):
    for attr in ("status_code", "code", "status", "http_status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    return None


def is_retryable(exc) -> bool:
    """判断异常是否值得重试（限流、5xx、网络/超时）。"""
    # 上游空响应按定义就是「这次没给、下次可能给」，所以必须重试。
    # 它尤其需要这一条：HTTP 层看到的是 200，`_status_of` 取不到任何状态码，
    # 不显式认它的话就会一路落到下面的字符串启发式——而那句消息里并没有
    # 任何网络/限流关键词，于是**一次都不会重试**，等于白抛。
    if isinstance(exc, EmptyProviderResponse):
        return True
    st = _status_of(exc)
    if isinstance(st, int) and (st == 429 or st >= 500):
        return True
    if st is not None and 400 <= st < 500:
        return False  # 明确的客户端错误不重试
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    msg = str(exc).lower()
    return any(h in msg for h in _RETRYABLE_HINTS)


def retry_call(fn, deadline=None, attempts=None, base=None):
    """带指数退避的重试包装。

    - 仅对 is_retryable 的异常重试；其余立即抛出（不浪费退避时间）。
    - deadline 为 time.monotonic() 基准的绝对截止时刻；逼近或到达即停止重试。
    - 流式场景：本包装只覆盖"建立连接"阶段；一旦开始吐 token 就不再重试
      （已发出的 token 无法撤回，宁可让上层走续写纠偏）。

    **退避总跨度必须覆盖上游的真实故障窗口，否则重试是假的。**
    实测免费中转的空响应窗口约 8–9 秒；旧参数（attempts=3 / base=0.8 / cap=8）
    三次尝试只铺开约 2.4 秒，于是每次重试都落在同一个故障窗口内，整轮仍失败——
    账本里那两条 `elapsed_ms` 2153 / 4279 就是这么来的。
    现在（attempts=4 / base=3 / cap=12）铺开 15–27 秒，覆盖得住。

    注意 `i` 只取到 `attempts - 2`：**只调大 base 而不动 attempts 是无效的**，
    attempts=3 时最多也只有 2 段退避（这是本文件顶上把 3 改成 4 的原因）。
    """
    attempts = int(attempts or LLM_RETRIES)
    base = LLM_RETRY_BASE if base is None else float(base)
    last = None
    for i in range(max(1, attempts)):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("LLM 调用已超出本轮截止时间")
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if i >= attempts - 1 or not is_retryable(e):
                raise
            delay = min(base * (2 ** i), LLM_RETRY_CAP) * (0.7 + 0.6 * random.random())
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)
    raise last  # pragma: no cover


def _fn_get(fn, key, default=""):
    """兼容 dict / pydantic 对象两种形态读取 function 字段。"""
    if fn is None:
        return default
    if isinstance(fn, dict):
        return fn.get(key, default)
    return getattr(fn, key, default)


def normalize_tool_calls(raw):
    """把 OpenAI / Ollama 的 tool_calls 归一为 [{id, name, arguments(字符串)}]。"""
    out = []
    for c in raw or []:
        fn = c.get("function") if isinstance(c, dict) else getattr(c, "function", None)
        if fn is None and isinstance(c, dict):
            fn = c                       # 有些服务端直接给 {name, arguments}
        name = _fn_get(fn, "name", "") or ""
        args = _fn_get(fn, "arguments", "") or ""
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        cid = c.get("id") if isinstance(c, dict) else getattr(c, "id", "")
        if name:
            out.append({"id": cid or "", "name": name, "arguments": args})
    return out


def args_to_input(args_str):
    """把原生 function-calling 的 arguments 映射回文本协议的多行 Action Input。

    工具 schema 统一只暴露一个 `input` 字符串参数，所以绝大多数情况直接取它；
    若模型给了多个具名参数，则转成 `key: value` 多行文本，交给既有的
    `_normalize_tool_arg` 继续处理（护栏完全复用）。
    """
    if not args_str:
        return ""
    try:
        obj = json.loads(args_str)
    except (ValueError, TypeError):
        return str(args_str)
    if isinstance(obj, dict):
        if list(obj.keys()) == ["input"]:
            return str(obj["input"])
        if "input" in obj and len(obj) == 1:
            return str(obj["input"])
        return "\n".join(f"{k}: {v}" for k, v in obj.items())
    return str(obj)


class StreamChat:
    """包装 OpenAI 流式响应：迭代时只产出正文 content token，
    结束后可从 finish_reason 判断是否因长度被截断（"length"）。

    思考型模型（如 qwen3 系列）的 reasoning_content 不计入正文，但累计
    长度到 reasoning_chars，供上层判断"只有思考、没有正文"的情况。
    """

    def __init__(self, stream, usage_sink=None, tool_sink=None, reasoning_sink=None,
                 cancel_event=None):
        self._stream = stream
        self._usage = usage_sink if usage_sink is not None else {}
        self._tool_sink = tool_sink if tool_sink is not None else []
        self._reasoning_sink = reasoning_sink
        self._cancel_event = cancel_event
        self._close_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._closed = False
        self._partial = {}          # index -> {"id":.., "name":.., "arguments":..}
        self.finish_reason = None
        self.reasoning_chars = 0
        if cancel_event is not None:
            threading.Thread(target=self._watch_cancel,
                             name="openai-stream-cancel", daemon=True).start()

    def _watch_cancel(self):
        try:
            while not self._watch_stop.is_set():
                if self._cancel_event is not None and self._cancel_event.wait(0.25):
                    self._close_stream()
                    return
        except Exception:
            # 清理辅助线程不能覆盖主流的网络异常。
            pass

    def _close_stream(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._watch_stop.set()
        try:
            close = getattr(self._stream, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    def _capture_tool_deltas(self, choice):
        """OpenAI 流式 tool_calls 是按 index 分片的，逐片累加。"""
        delta = getattr(choice, "delta", None)
        calls = getattr(delta, "tool_calls", None) if delta is not None else None
        if not calls:
            return
        for c in calls:
            idx = getattr(c, "index", 0) or 0
            slot = self._partial.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            cid = getattr(c, "id", None)
            if cid:
                slot["id"] = cid
            fn = getattr(c, "function", None)
            nm = _fn_get(fn, "name", "")
            if nm:
                slot["name"] = nm
            frag = _fn_get(fn, "arguments", "")
            if frag:
                # 个别 OpenAI 兼容服务端（含部分本地推理后端）把流式 function.arguments
                # 直接给成对象而非 JSON 字符串；直接 `str += dict` 会抛
                # TypeError: can only concatenate str (not "dict") to str。
                # 与 normalize_tool_calls 保持同一归一：对象 → JSON 字符串。
                if not isinstance(frag, str):
                    frag = json.dumps(frag, ensure_ascii=False) if isinstance(
                        frag, (dict, list)) else str(frag)
                slot["arguments"] += frag

    def _flush_tools(self):
        for idx in sorted(self._partial):
            slot = self._partial[idx]
            if slot.get("name"):
                self._tool_sink.append({"id": slot["id"], "name": slot["name"],
                                        "arguments": slot["arguments"]})

    def _capture_usage(self, chunk):
        """尾包（choices 为空）带 usage：OpenAI 需请求时开 include_usage。

        同时抽取 prompt caching 字段（cache_read_input_tokens / cached_tokens，含
        prompt_tokens_details.* 嵌套口径），供计费按折扣价结转（修复"计费漏算缓存命中"）。
        """
        u = getattr(chunk, "usage", None)
        if u is None:
            return
        dump = u.model_dump() if hasattr(u, "model_dump") else (u if isinstance(u, dict) else {})
        if not isinstance(dump, dict):
            return
        for k in ("prompt_tokens", "completion_tokens",
                  "cache_read_input_tokens", "cache_creation_input_tokens",
                  "cache_read_tokens", "cache_creation_tokens", "cached_tokens"):
            v = dump.get(k)
            if isinstance(v, int):
                self._usage[k] = v
        details = dump.get("prompt_tokens_details")
        if isinstance(details, dict):
            for k in ("cached_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if isinstance(details.get(k), int):
                    self._usage[k] = details[k]

    def __iter__(self):
        try:
            for chunk in self._stream:
                self._capture_usage(chunk)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    self.finish_reason = choice.finish_reason
                self._capture_tool_deltas(choice)
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    self.reasoning_chars += len(reasoning)
                    if self._reasoning_sink is not None:
                        self._reasoning_sink.append(reasoning)
                content = getattr(delta, "content", None)
                if content:
                    yield content
                elif reasoning and self._reasoning_sink is not None:
                    # 唤醒 Agent 消费思考队列，不必等到第一段正文才显示思考。
                    yield ""
        finally:
            # 消费方提前中断（如客户端断连）也要把已收到的工具调用交给上层
            self._flush_tools()
            self._close_stream()


class _OllamaStream:
    """ollama /api/chat 流式（NDJSON）适配器：接口与 StreamChat 对齐。

    每行一个 JSON 片段：message.content 增量、message.thinking 思考增量；
    末行 done=true 带 done_reason（"stop" / "length"），映射到 finish_reason。
    """

    def __init__(self, resp, on_close=None, usage_sink=None, tool_sink=None,
                 reasoning_sink=None, cancel_event=None):
        self._resp = resp
        self._on_close = on_close
        self._usage = usage_sink if usage_sink is not None else {}
        self._tool_sink = tool_sink if tool_sink is not None else []
        self._reasoning_sink = reasoning_sink
        self._seen_tools = set()
        self._cancel_event = cancel_event
        self._close_lock = threading.Lock()
        self._watch_stop = threading.Event()
        self._closed = False
        self.finish_reason = None
        self.reasoning_chars = 0
        if cancel_event is not None:
            # urllib 的响应迭代可能阻塞在 socket read；监听取消事件并关闭
            # response，才能让底层 IO 尽快返回，而不是只在上层设置标记。
            threading.Thread(target=self._watch_cancel,
                             name="ollama-stream-cancel", daemon=True).start()

    def _watch_cancel(self):
        try:
            while not self._watch_stop.is_set():
                if self._cancel_event is not None and self._cancel_event.wait(0.25):
                    self._close_response()
                    return
        except Exception:
            # 取消监听是清理辅助线程，不能覆盖主流的异常。
            pass

    def _close_response(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._watch_stop.set()
        try:
            self._resp.close()
        except Exception:
            pass

    def __iter__(self):
        try:
          for raw in self._resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = obj.get("message") or {}
            thinking = msg.get("thinking")
            if thinking:
                self.reasoning_chars += len(thinking)
                if self._reasoning_sink is not None:
                    self._reasoning_sink.append(thinking)
            # ollama 的 tool_calls 是"完整"对象（非分片），去重后并入 sink
            for call in normalize_tool_calls(msg.get("tool_calls")):
                sig = (call["name"], call["arguments"])
                if sig not in self._seen_tools:
                    self._seen_tools.add(sig)
                    self._tool_sink.append(call)
            content = msg.get("content")
            if content:
                yield content
            elif thinking and self._reasoning_sink is not None:
                yield ""
            # ollama 原生 /api/chat 的末行带 prompt_eval_count / eval_count
            for k in ("prompt_eval_count", "eval_count"):
                v = obj.get(k)
                if isinstance(v, int):
                    self._usage[k] = v
            if obj.get("done"):
                # ollama done_reason: stop/length；无该字段时按 stop 处理
                self.finish_reason = obj.get("done_reason") or "stop"
        finally:
            self._close_response()
            if self._on_close: self._on_close()


# ---- Ollama 实时上下文窗口探测（/api/show）----
# 内置画像对未登记的本地模型只能按 16384 保守处理；Ollama 的 /api/show 返回的
# model_info.<arch>.context_length 才是该模型的真实最大窗口。探测结果进程内缓存，
# 模型未拉取/服务不可达时静默返回 None，由画像兜底；env DOCMIND_OLLAMA_PROBE=0 可关。
_OLLAMA_CTX_CACHE = {}


def _ollama_native_base(base_url: str = "") -> str:
    """OpenAI 兼容地址（.../v1）还原成 Ollama 原生地址（去掉末尾 /v1）。"""
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3].rstrip("/")
    return b or os.getenv("OLLAMA_BASE", "http://127.0.0.1:11434")


def parse_ollama_show_context(data) -> int:
    """从 /api/show 响应 JSON 中解析模型最大 context_length；无法识别返回 None。

    新版字段为 model_info.llama.context_length，旧版按架构命名
    （qwen2.context_length / gemma2.context_length…），取其中最大值。
    """
    if not isinstance(data, dict):
        return None
    best = None
    info = data.get("model_info")
    if isinstance(info, dict):
        for k, v in info.items():
            if isinstance(k, str) and k.endswith(".context_length") and isinstance(v, int) and v >= 512:
                best = v if best is None else max(best, v)
    if best:
        return best
    v = data.get("context_length")
    return int(v) if isinstance(v, int) and v >= 512 else None


def probe_ollama_context(model: str, base_url: str = "", timeout: float = 3.0,
                         force: bool = False):
    """实时探测 Ollama 模型的上下文窗口（token）；不可用返回 None。"""
    name = (model or "").strip()
    if not name:
        return None
    key = (_ollama_native_base(base_url), name.lower())
    if not force and key in _OLLAMA_CTX_CACHE:
        return _OLLAMA_CTX_CACHE[key]
    win = None
    try:
        payload = json.dumps({"name": name}).encode("utf-8")
        req = urllib.request.Request(
            key[0] + "/api/show", data=payload,
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        win = parse_ollama_show_context(data)
    except Exception:  # noqa: BLE001 —— 探测只做锦上添花，任何失败都退回画像
        win = None
    # 只缓存成功结果：失败（Ollama 暂时不可达 / 模型冷启动）若也缓存 None，
    # 之后新建的会话 Agent 在整个进程生命周期内都只能拿到画像默认窗口（十几 k），
    # 即使 Ollama 已恢复——正是「上下文一直按小窗口算」的来源之一。
    if win is not None:
        _OLLAMA_CTX_CACHE[key] = win
    return win


def _doc_context_resolver(query: str) -> str:
    """LLMClient 默认文档检索回调：搜索官方资料并抓取首条结果正文。

    延迟导入 tools，避免 llm <-> tools 循环依赖；任何失败都静默返回空串（文档解析
    只是锦上添花，绝不能因联网失败阻断主流程）。
    """
    try:
        from tools import web_search, web_fetch
    except Exception:
        return ""
    try:
        # web_search 可能返回 ToolResult 等非 str 包装对象：先规整再正则，绝不上抛。
        res = as_text(web_search(query))
    except Exception:
        return ""
    if not res:
        return ""
    urls = re.findall(r"https?://[^\s)\"'`]+", res)
    for u in urls[:3]:
        try:
            body = as_text(web_fetch(u))
        except Exception:
            body = ""
        if body:
            return body
    return res


class EmptyProviderResponse(RuntimeError):
    """provider 用 HTTP 200 回了一个错误信封，而不是一次正常的补全。

    OpenRouter 兼容端点在**状态码是 200** 的情况下把上游失败包成
    `{"choices": null, "error": {"message": "Provider returned an empty
    response", "code": 502}}`。实测本机用 `custom/stealth/space-bunny-alpha`
    连打三个问题，第二个就命中（2026-10-02）。

    为什么值得单独一个异常类型：原来的写法是 `resp.choices[0].message`，它抛的是
    `TypeError: 'NoneType' object is not subscriptable`——一句与真实原因毫无关系的
    消息，`api.py:2418` 又按 `type(e).__name__` 把它显示成 `⚠ TypeError`，于是用户
    看到的是"服务内部类型错误"，而真实情况是"上游这次没返回内容，重试可能就好了"。
    两件事必须分开：**说清是谁没给内容**，并让上层知道这类错误**值得重试**。
    """


def _response_error(resp) -> dict:
    """把 provider 的错误信封挖出来（`error` 可能是 dict 或嵌套信封）。"""
    dump = None
    if hasattr(resp, "model_dump"):
        try:
            dump = resp.model_dump()
        except Exception:  # noqa: BLE001 - 取不到就按「没有信封」处理
            dump = None
    if not isinstance(dump, dict):
        dump = resp if isinstance(resp, dict) else {}
    error = dump.get("error")
    if isinstance(error, dict):
        inner = error.get("error")
        if isinstance(inner, dict):
            error = inner
        return error
    return {}


def _first_message(resp):
    """取 `choices[0].message`，并把「没有 choices」变成一句能读懂的错。

    fail-closed 的两个分支都在这里：
    * `choices` 是 `None` / 空 —— 上游用 200 回了个错误信封（见 EmptyProviderResponse）；
    * `message` 是 `None` —— 有 choice 但没内容，同样不算一次成功的补全。
    两者都**不**返回空字符串冒充答案——空串会被 Agent 当成"模型什么都没说"，
    然后触发续写纠偏转 8 次，比直接报错难查得多。

    调用位置在 `retry_call` **内部**（见 chat()）：空响应按定义是"这次没给、
    下次可能给"，所以它必须走重试，否则这个异常只是把旧的真错误换了个好听的名字。
    """
    choices = getattr(resp, "choices", None)
    if not choices:
        detail = _response_error(resp)
        message = str(detail.get("message") or "").strip()
        code = detail.get("code")
        where = "%s（%s）" % (message, code) if message and code else (message or "无错误说明")
        raise EmptyProviderResponse(
            "模型服务没有返回任何候选内容：%s。这是上游/中转的临时故障，"
            "与代码无关，直接重试通常即可。" % where)
    first = choices[0]
    msg = getattr(first, "message", None)
    if msg is None:
        raise EmptyProviderResponse(
            "模型服务返回了一条空的候选消息（finish_reason=%r）。这是上游/中转的"
            "临时故障，与代码无关，直接重试通常即可。"
            % getattr(first, "finish_reason", None))
    return msg


class LLMClient:
    def __init__(self, provider=None, model=None, api_key=None, base_url=None, web_fn=None):
        self.provider = provider or get_runtime("llm_provider") or LLM_PROVIDER
        if self.provider not in PROVIDERS:
            # 未知 provider 不静默回退：明确报错，避免把请求发到意料之外的服务
            raise ValueError(f"未知模型服务：{self.provider}")
        cfg = PROVIDERS[self.provider]
        self.model = model or get_runtime("llm_model") or LLM_MODEL or cfg["default_model"]
        # 最近一次调用的 token 用量（由 chat()/流式包装器原地更新），供 trace 账本读取
        self.last_usage = {}
        # 最近一次调用返回的原生 tool_calls（[{id,name,arguments}]），供 agent 的原生通道读取
        self.last_tool_calls = []
        # ------------------------------------------------------------------
        # 「上一次调用到底发生了什么」的诊断字段——`last_usage` /
        # `last_tool_calls` 之外的第三组。
        #
        # 实测阻断（`wf-2c5612c6ee53` / `wf-87c0df3224aa`）：原生 FC 通道上模型
        # **第 1 轮正常、第 2 轮起不产出可解析输出**，连续两轮即触发格式停滞诊断。
        # 此前已排除：通道可用、最简 schema 可用、111 个真实工具可用、schema 形状
        # 不畸形、`output_budget` 充足、`last_tool_calls` 形状正确。
        #
        # 也就是说：**手上每一个字段都「正常」，而现象不正常。** 唯一还没量的是
        # **出站请求本身**——第 2 轮发给模型的消息到底长什么样。
        #
        # 而原有字段全都答不了这个问题：
        #
        # - `last_tool_calls` 为空——「没返回 tool call」和「返回了但我们没接住」
        #   在这里长得一模一样；
        # - `last_usage` 只有 token 数——看得出「有没有生成」，看不出「**生成到了
        #   哪一步被掐掉**」；
        # - `finish_reason` 只在流对象上（消费完就随流一起没了），非流式路径上根本
        #   没有这个信息。
        #
        # 所以补三样：**finish_reason、原始首段、以及完整出站消息**。
        # `last_messages` 只在 `DOCMIND_PROBE_LLM=1` 时真的留一份拷贝，
        # 否则每次调用白付一次 `list(messages)` 的钱。
        #
        # 为什么 `last_raw_head` 要单独留：「模型返回了空字符串」和「模型返回了
        # 一句 4000 字的开场白然后被掐断」，对下一步的动作要求完全相反。而
        # `completion_tokens` 只能告诉你后者**可能**发生——留一段真实文本，
        # 才能一眼看出是哪一种。
        self.last_finish_reason = None   # 非流式路径原本完全没有这个信息
        self.last_stream_ref = None      # 流式下**记流对象本身**（值要消费完才定）
        self.last_raw_head = ""          # 分辨「空」与「有开场白但被掐」
        self.last_call_probe = bool(os.getenv("DOCMIND_PROBE_LLM", "").strip()
                                    in ("1", "true", "yes"))
        self.last_messages = []          # 出站消息快照，仅探针开启时真的留
        self.max_retries = LLM_RETRIES
        self.retry_base = LLM_RETRY_BASE
        self.timeout = LLM_TIMEOUT
        # 解析出来的 key 留档：子代理需要用它新建**独立**的 LLMClient（避免共享实例的
        # last_usage / last_tool_calls 在并发下互相覆盖）
        self.api_key = ""
        # 自定义 OpenAI 兼容端点：显式传入 > 运行时覆盖 > LLM_BASE_URL 环境变量
        self.base_url = (
            base_url
            or (get_runtime("llm_base_url") if self.provider == "custom" else "")
            or (os.getenv("LLM_BASE_URL", "") if self.provider == "custom" else "")
            or cfg["base_url"]
        )

        # 模型能力画像：决定 num_ctx 扩充、思考参数能否透传（前端也据此渲染开关）。
        # 窗口来源：custom=用户手填覆盖 > probe=Ollama 实时探测 > profile=内置画像。
        live_window = None
        if (self.provider == "ollama"
                and os.getenv("DOCMIND_OLLAMA_PROBE", "1").strip() not in ("0", "false", "no")
                and not get_context_window_override(self.provider, self.model)):
            # 注意：此处会发起一次 ≤3s 的**同步**探活网络请求（probe_ollama_context）。
            # 调用方若在事件循环线程内构造 LLMClient，必须放到线程池
            # （api.py 的 async 处理函数用 `await run_in_threadpool(...)`），否则会阻塞整个 asyncio。
            live_window = probe_ollama_context(self.model, self.base_url)
        self.context_source = (
            "custom" if get_context_window_override(self.provider, self.model)
            else ("probe" if live_window else "profile")
        )
        self.capability = model_capability(self.provider, self.model, context_window=live_window)
        # 按真实窗口缩放的单轮 prompt token 预算（Agent 裁剪/压缩与 ollama num_ctx 共用）
        self.prompt_budget = prompt_token_budget(
            self.provider, self.model, context_window=self.capability["context_window"])
        # 云端模型且静态画像只落到「厂商默认窗口」（没精确到具体模型）时，联网检索官方文档
        # 校正真实窗口（如 DeepSeek 实际达 1M），结果缓存复用；local/mock 不触发联网。
        self._web_fn = web_fn
        if self._web_fn is None and self.capability.get("cloud"):
            self._web_fn = _doc_context_resolver
        if (self._web_fn
                and self.capability.get("cloud")
                and not get_context_window_override(self.provider, self.model)
                and self.capability["context_window"] == _PROVIDER_CONTEXT_DEFAULT.get(self.provider, 0)):
            docs_win = resolve_context_from_docs(self.provider, self.model, self._web_fn)
            if docs_win:
                self.context_source = "docs"
                self.capability = model_capability(
                    self.provider, self.model, context_window=docs_win)
                self.prompt_budget = prompt_token_budget(
                    self.provider, self.model, context_window=docs_win)
        # 单次补全可输出上限：由真实窗口派生（大窗口云端模型给足预算，不再被 3072 限死）
        self.output_budget = output_token_budget(
            self.provider, self.model, context_window=self.capability["context_window"])

        if self.provider == "mock":
            # 离线演示模式：不发起任何网络请求
            self.client = None
            return

        if self.provider == "custom" and not self.base_url:
            raise ValueError("自定义 OpenAI 兼容服务缺少 base_url，请在模型设置中填写接口地址。")

        # 解析 API Key：优先显式传入 -> 运行时覆盖 -> 环境变量 LLM_API_KEY -> provider 专用变量
        key = api_key or get_runtime("llm_api_key") or LLM_API_KEY
        if cfg["api_key_env"]:
            key = key or os.getenv(cfg["api_key_env"], "")
        self.api_key = key or ""
        self.client = OpenAI(base_url=self.base_url, api_key=key or "EMPTY", timeout=self.timeout)

    def clone(self):
        """复制一份**独立**的客户端（同 provider/model/key），供并发子代理使用。"""
        return LLMClient(provider=self.provider, model=self.model, api_key=self.api_key,
                         base_url=self.base_url or None, web_fn=getattr(self, "_web_fn", None))

    def _resolve_thinking(self, enable_thinking):
        """本次调用是否开启思考：显式参数 > 运行时开关 > 全局环境变量。

        返回 (enabled, reason)：
        - native 思考模型（reasoner/qwq…）始终为 True，开关只控制前端是否展示思考流；
        - toggle 家族（qwen3）按开关透传 enable_thinking；
        - 不支持思考的模型恒 False（不能给服务端发陌生参数）。
        """
        rt = get_runtime("llm_enable_thinking")
        if enable_thinking is None:
            enable_thinking = (str(rt) in ("1", "true", "yes", "on")) if rt is not None \
                else LLM_ENABLE_THINKING
        mode = self.capability.get("thinking", "none")
        if mode == "native":
            return True
        if mode == "toggle":
            return bool(enable_thinking)
        return False

    def chat(self, messages, stream=False, temperature=LLM_TEMPERATURE, timeout=None, deadline=None, tools=None,
             enable_thinking=None, reasoning_sink=None, cancel_event=None, max_output_tokens=None):
        """统一的对话入口。stream=True 时返回一个 token 生成器。

        timeout：单次调用超时（秒），默认 self.timeout。
        deadline：time.monotonic() 基准的绝对截止时刻；到达即不再重试并抛 TimeoutError。
        tools：OpenAI 风格函数 schema 列表；给出则走原生 function-calling 通道。
        enable_thinking：思考开关（None=按运行时/全局配置）；仅对画像为 toggle/native
                         的模型生效。
        reasoning_sink：可选 list，流式思考片段（reasoning_content / thinking）实时追加，
                        供 Agent 转成 SSE 事件给前端"深度思考"窗口。
        cancel_event：可选 threading.Event；流式和带取消事件的非流式请求收到取消后
                     会关闭底层响应并终止本轮生成。
        每次调用都重置 self.last_usage / self.last_tool_calls / last_finish_reason /
        last_raw_head；成功后由 agent 读取。可重试错误（429/5xx/网络/超时）按指数
        退避自动重试。
        """
        self.last_usage = {}
        self.last_tool_calls = []
        self.last_finish_reason = None
        self.last_stream_ref = None
        self.last_raw_head = ""
        self.last_messages = list(messages) if self.last_call_probe else []
        if reasoning_sink is not None:
            reasoning_sink.clear()
        if cancel_event is not None and cancel_event.is_set():
            raise GeneratorExit
        thinking_on = self._resolve_thinking(enable_thinking)
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("本轮已超出截止时间")

        if self.provider == "mock":
            return self._mock_chat(messages, stream=stream)

        if self.provider == "ollama":
            # ollama 的 /v1 OpenAI 兼容层会静默丢弃 options.num_ctx（实测 14584
            # 被忽略、加载仍为 4096）和 chat_template_kwargs，长 prompt 会被截断。
            # 改走原生 /api/chat：num_ctx / think / num_predict 全部服务端生效。
            # 消息内的 images: [base64...] 也是 ollama 原生多模态格式，直接透传。
            # 工作流子代理通常请求非流式结果，但用户仍可能在生成中点击停止。
            # 这时用 Ollama 的 NDJSON 流聚合成原有的字符串返回值，才能在 socket
            # read 阻塞时由 _OllamaStream 的 watcher 关闭连接。普通非流式调用仍走
            # 单响应路径，保留原有延迟和兼容性。
            wire_stream = bool(stream or cancel_event is not None)
            call = lambda: retry_call(
                lambda: self._ollama_chat(
                    messages, stream=wire_stream, temperature=temperature,
                    timeout=timeout, usage_sink=self.last_usage,
                    tool_sink=self.last_tool_calls, tools=tools,
                    thinking_on=thinking_on, reasoning_sink=reasoning_sink,
                    cancel_event=cancel_event,
                    max_output_tokens=max_output_tokens,
                ),
                deadline=deadline, attempts=self.max_retries, base=self.retry_base,
            )
            # Streaming ownership lasts beyond this function and is already
            # guarded by Agent's child slot.  Non-stream requests can safely
            # protect the whole HTTP generation here, including direct API use.
            if stream:
                return call()
            with local_llm_slot(self.provider, self.model):
                result = call()
                if cancel_event is not None:
                    return self._collect_cancelable_stream(result, cancel_event)
                return result

        # OpenAI 兼容路径：把 ollama 风格的 images 字段转成多模态 content parts，
        # 否则 SDK 的 pydantic 序列化会因未知字段报错。
        oai_messages = self._to_openai_messages(messages)

        # max_tokens 兜底：思考型模型偶发不按格式收尾而无限生成，到顶后由
        # finish_reason=length 触发 Agent 的续写纠偏，避免单轮烧几分钟/上万 token。
        # 由模型真实窗口派生（output_budget），云端大模型给足输出余量，不再写死 3072。
        output_limit = self.output_budget
        if max_output_tokens is not None:
            output_limit = min(output_limit, max(1, int(max_output_tokens)))
        kwargs = {"max_tokens": output_limit}
        # qwen3 toggle 家族（含 DashScope 兼容模式/自建端点）：按开关透传 enable_thinking。
        # 不支持思考的模型绝不带这个参数，避免 400；native 模型本身始终推理，无需传。
        #
        # ⚠⚠ **参数形状按 provider 分支，不能统一发 qwen 那一种。**
        # DeepSeek V4/V4.1 的开关是 `{"thinking": {"type": "enabled"|"disabled"}}`
        # （一手依据：api-docs.deepseek.com/guides/thinking_mode，示例直接用
        # `model="deepseek-flash"`）。发成 `chat_template_kwargs` 的话，
        # 上游要么忽略要么 400 —— 表现为「开关能点了但思考没开」，
        # 比诚实地报「不支持」更难排查。
        #
        # `disabled` 是官方文档明确列出的合法值（api-docs.deepseek.com/api/create-chat-completion：
        # "Possible values: [enabled, disabled]"），不是我们推的。
        if self.capability.get("thinking") == "toggle":
            if (self.provider or "").strip().lower() == "deepseek":
                kwargs["extra_body"] = {"thinking": {"type": "enabled" if thinking_on else "disabled"}}
            else:
                kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(thinking_on)}}

            # ⚠⚠⚠ **开思考时的两条硬约束，都来自 DeepSeek 官方 API 文档**
            # （api-docs.deepseek.com/api/create-chat-completion）。改这段之前先读：
            #
            # **1) `tool_choice="required"` / 具名函数 + 思考模式 = 整个请求 400。**
            #    文档原文："required and named tool choices are not supported in
            #    thinking mode; the API returns a 400 error. Disable thinking
            #    mode first to use them."
            #
            #    → **现在没踩到，只因为全仓从不发 `tool_choice`**（只发 `tools`，
            #    走默认 auto）。这是运气，不是设计。哪天有人为了「强制让模型用工具」
            #    在上面的 `kwargs` 里加 `tool_choice`，**所有开了思考的用户会全线
            #    400，且报错信息不会提示是思考模式的冲突**。
            #    → 真要加 tool_choice 时，必须先判 `thinking_on` 并二选一，
            #    或者把工具调用降级为非思考轮。
            #
            # **2) `temperature` / `presence_penalty` / `frequency_penalty`
            #    在思考模式下被静默忽略。**
            #    文档原文："...will not trigger an error but will also have no effect."
            #
            #    → 下面 `create()` 是**无条件**发 `temperature=temperature` 的。
            #    用户设了 1.0 想让它有创造性，开思考后实际用的是 provider 默认值，
            #    **不报错、没有任何信号**。已立 TODO（见 docs/next-steps.md §7），
            #    待决定：抑制该参数并在 UI 说明，还是接受这个偏差。
        call_timeout = self.timeout if timeout is None else timeout
        if tools:
            kwargs["tools"] = tools

        if stream or cancel_event is not None:
            def _open(with_usage):
                extra = {"stream_options": {"include_usage": True}} if with_usage else {}
                resp = self.client.chat.completions.create(
                    model=self.model, messages=oai_messages, stream=True,
                    temperature=temperature, timeout=call_timeout, **kwargs, **extra
                )
                return StreamChat(resp, usage_sink=self.last_usage,
                                  tool_sink=self.last_tool_calls,
                                  reasoning_sink=reasoning_sink,
                                  cancel_event=cancel_event)
            def _open_with_fallback():
                try:
                    return retry_call(lambda: _open(True), deadline=deadline,
                                      attempts=self.max_retries, base=self.retry_base)
                except Exception as e:  # noqa: BLE001
                    # 服务端不认 stream_options 这类参数错误：退回不带 usage 的调用；
                    # 网络/限流类错误则如实抛出（重试已用尽）。
                    if is_retryable(e):
                        raise
                    return _open(False)

            if stream:
                stream_obj = _open_with_fallback()
                if self.last_call_probe:
                    # 流式下 finish_reason 只存在于流对象上，而它消费完就随流消失；
                    # 第 2 轮起「不产出可解析输出」时，唯一能分辨「被长度掐断」与
                    # 「模型自己停了」的就是它。**刻意记流对象本身**而不是当时的值。
                    self.last_stream_ref = stream_obj
                return stream_obj
            # 只有提供取消事件时才进入此分支；保持 chat(stream=False) 的
            # 字符串返回值，并让 local_llm_slot 覆盖整个生成生命周期。
            with local_llm_slot(self.provider, self.model):
                wrapped = _open_with_fallback()
                return self._collect_cancelable_stream(wrapped, cancel_event)


        with local_llm_slot(self.provider, self.model):
            # 取 content **也在重试范围内**：`_first_message` 抛的
            # EmptyProviderResponse 是可重试的（上游空响应 = 这次没给、下次可能给），
            # 而 HTTP 200 的错误信封不会让 HTTP 层报错——所以不把检查放进重试里，
            # 这个异常只是把旧的 TypeError 换了个好听的名字，一次都不会重试。
            # 实测本机打 6 个问题命中 1 次空响应，重试后拿到了正常内容。
            def _complete():
                resp = self.client.chat.completions.create(
                    model=self.model, messages=oai_messages, stream=False,
                    temperature=temperature, timeout=call_timeout, **kwargs
                )
                self._record_usage(resp)
                msg = _first_message(resp)
                self.last_tool_calls = normalize_tool_calls(getattr(msg, "tool_calls", None))
                # 非流式路径原本**完全没有** finish_reason，而「第 2 轮起不产出
                # 可解析输出」正是要靠它分辨：模型自己停了（stop）还是被长度掐了
                # （length）——两者的下一步动作完全相反。
                choice = (getattr(resp, "choices", None) or [None])[0]
                self.last_finish_reason = getattr(choice, "finish_reason", None)
                content = getattr(msg, "content", None)
                if self.last_call_probe and content:
                    # 留第一段原文：空串与「有开场白但被掐」必须能一眼分开。
                    self.last_raw_head = str(content)[:400]
                return content

            return retry_call(_complete, deadline=deadline, attempts=self.max_retries,
                              base=self.retry_base)

    def _record_usage(self, resp):
        """把 usage 里的 token 计数搬进 last_usage（供 trace 与计价用）。"""
        u = getattr(resp, "usage", None)
        if u is None:
            return
        dump = u.model_dump() if hasattr(u, "model_dump") else (u if isinstance(u, dict) else {})
        if not isinstance(dump, dict):
            return
        for key in ("prompt_tokens", "completion_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens",
                    "cache_read_tokens", "cache_creation_tokens", "cached_tokens"):
            if isinstance(dump.get(key), int):
                self.last_usage[key] = dump[key]
        details = dump.get("prompt_tokens_details")
        if isinstance(details, dict):
            for key in ("cached_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if isinstance(details.get(key), int):
                    self.last_usage[key] = details[key]

    @staticmethod
    def _collect_cancelable_stream(stream, cancel_event):
        """聚合可取消的流式响应，维持非流式 chat() 的返回语义。"""
        chunks = []
        try:
            for token in stream:
                chunks.append(token)
                if cancel_event is not None and cancel_event.is_set():
                    raise GeneratorExit
        except BaseException:
            # urllib/OpenAI SDK 关闭 socket 后可能把底层异常带回迭代器；
            # 对调用方它仍然是一次用户取消，不应被记录成模型故障。
            if cancel_event is not None and cancel_event.is_set():
                raise GeneratorExit
            raise
        finally:
            # StreamChat/_OllamaStream 自己负责关闭底层响应；这里仅确保消费方
            # 提前退出时也触发迭代器的 finally。
            if cancel_event is not None and cancel_event.is_set():
                close = getattr(stream, "_close_stream", None) or getattr(stream, "_close_response", None)
                if callable(close):
                    close()
        if cancel_event is not None and cancel_event.is_set():
            raise GeneratorExit
        return "".join(chunks)

    @staticmethod
    def _raw_b64(image):
        """把 data URL / 纯 base64 统一成**纯 base64**。

        内部来源格式不一致：tools/web_fetch、screen_capture 等产出
        ``data:image/jpeg;base64,...``（data URL），而 ollama 原生 /api/chat 的
        ``images`` 只接受纯 base64——传 data URL 会被拒（实测 400
        ``illegal base64 data at input byte 4``）；OpenAI 兼容端点则需自行拼 data URL。
        故统一在入口剥掉前缀，两条链路都不再受来源格式影响。
        """
        text = str(image or "")
        if text.startswith("data:"):
            _, _, payload = text.partition(",")
            return payload
        return text

    @staticmethod
    def _sniff_image_mime(b64):
        """按 base64 解码头部魔数判断图片 MIME（供 OpenAI data URL 使用）。"""
        import base64
        b64 = LLMClient._raw_b64(b64)
        try:
            head = base64.b64decode(b64[:32] + "==")
        except Exception:
            return "image/jpeg"
        if head.startswith(b"\x89PNG"):
            return "image/png"
        if head.startswith(b"\xff\xd8"):
            return "image/jpeg"
        if head.startswith(b"GIF8"):
            return "image/gif"
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return "image/webp"
        return "image/jpeg"

    def _to_openai_messages(self, messages):
        """把内部消息（ollama 风格 images: [b64...]）转成 OpenAI 多模态格式：
        content 变为 [{"type":"text"...}, {"type":"image_url","image_url":{"url":"data:..."}}]。
        """
        out = []
        for m in messages:
            imgs = m.get("images")
            if not imgs:
                out.append(m)
                continue
            parts = [{"type": "text", "text": m.get("content") or ""}]
            for raw in imgs:
                b64 = self._raw_b64(raw)
                mime = self._sniff_image_mime(b64)
                parts.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
                )
            nm = dict(m)
            nm.pop("images", None)
            nm["content"] = parts
            out.append(nm)
        return out

    # ollama 原生 /api/chat 单次请求的连接/读超时。默认 180s：旧值 600s 会让一次卡死的
    # 生成长达 10 分钟占着唯一推理槽，把同机其它请求一起拖死。流式下每收到一个 token
    # 都会刷新读，只有真正的"沉默"才触发，故 180s 足够（冷加载 22GB 实测 ~27s）。
    # 可用 DOCMIND_OLLAMA_TIMEOUT 覆盖（不设则回落 DOCMIND_LLM_TIMEOUT / 180）。
    _OLLAMA_TIMEOUT = float(os.getenv("DOCMIND_OLLAMA_TIMEOUT",
                                      os.getenv("DOCMIND_LLM_TIMEOUT", "180")))

    def _ollama_url(self, path):
        root = str(self.client.base_url).rstrip("/").removesuffix("/v1").rstrip("/")
        return root + path

    def _ollama_chat(self, messages, stream=False, temperature=LLM_TEMPERATURE, timeout=None,
                     usage_sink=None, tool_sink=None, tools=None,
                     thinking_on=False, reasoning_sink=None, cancel_event=None, max_output_tokens=None):
        if not _gpu_acquire("ollama", 2): raise RuntimeError("GPU 正忙：ComfyUI 正在使用中，请稍后重试。")
        # 打点：空闲卸载计时器以"真正发起 Ollama 推理"为活动依据，
        # 仅持有租约（排队等待）不算活动，避免把等待误判成模型在用。
        _gpu_note_activity("ollama")
        # num_ctx 按模型画像的真实窗口缩放：大窗口模型不再被 14.5k 限死，
        # 小窗口模型也不会盲目塞爆（被服务端拒绝或挤爆显存）。
        budget = int(getattr(self, "prompt_budget", 0) or PROMPT_TOKEN_BUDGET)
        # 输出预算按模型真实窗口派生（大窗口模型给足，小窗口模型物理封顶）
        out_budget = int(getattr(self, "output_budget", LLM_MAX_TOKENS))
        want_ctx = budget + out_budget + 512
        win = int(self.capability.get("context_window") or 16384)
        num_ctx = min(want_ctx, win)
        # 窗口放得下完整预算时输出给满派生预算；放不下（小窗口模型）时
        # 至少保 512 输出，其余额度让给 prompt。
        if num_ctx >= want_ctx:
            num_predict = out_budget
        else:
            # 窗口放不下「完整 prompt 预算 + 派生输出预算」时：prompt 吃满其预算，
            # 输出拿窗口剩余余量（窗口 - prompt预算 - 512 预留），至少保底 512。
            # 注意用 budget 而非 num_ctx：num_ctx 已被窗口封顶，win - num_ctx 恒≈512，
            # 会错误地把输出掐到地板；按真实 prompt 占用给输出留量才是正确裁剪。
            num_predict = min(out_budget, max(512, win - budget - 512))
        # 请求级输出上限不改变 num_ctx，避免每次简单查询重载模型。
        if max_output_tokens is not None:
            num_predict = min(num_predict, max(1, int(max_output_tokens)))
        # Ollama 原生 /api/chat 的 images 只接受**纯 base64**；内部来源（tools/web_fetch、
        # screen_capture 等）会产出 data URL。不剥前缀会被 Ollama 直接拒绝（实测 400
        # "illegal base64 data at input byte 4"），整轮问答因此失败、历史也一并丢失。
        if isinstance(messages, (list, tuple)):
            messages = [
                ({**m, "images": [self._raw_b64(x) for x in m["images"]]}
                 if isinstance(m, dict) and m.get("images") else m)
                for m in messages
            ]
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": bool(stream),
            # ollama 默认 num_ctx=4096，会把 ~11000 token 的 prompt 静默截断
            "options": {
                "num_ctx": num_ctx,
                "num_predict": num_predict,
                "temperature": temperature,
                # qwen3.6 模型 Modelfile 自带 presence_penalty=1.5，实测会诱发
                # 空 Action Input / JSON 参数等格式退化；显式归零贴近 OpenAI 默认
                "presence_penalty": 0.0,
                "frequency_penalty": 0.0,
            },
            # 显式 think：qwen3 toggle 家族按用户开关；原生思考模型 ollama 自行决定；
            # 不支持的模型给 false 也安全（实测 ollama 对无思考模板的模型忽略该字段）。
            "think": bool(thinking_on),
        }
        # Ollama 会默认尝试把尽可能多的层放进 GPU。对 35B 级 MoE 模型，
        # 这在显存接近临界值时可能直接触发 runner 启动失败；允许用户通过
        # DocMind 的环境配置指定一个较小的 GPU 层数，剩余层由 CPU 承担。
        # 这是请求级选项，不会改动 Ollama 守护进程的全局设置。
        raw_num_gpu = os.getenv("DOCMIND_OLLAMA_NUM_GPU", "").strip()
        if raw_num_gpu:
            try:
                num_gpu = int(raw_num_gpu)
            except ValueError:
                num_gpu = 0
            if num_gpu >= 0:
                payload["options"]["num_gpu"] = num_gpu
        if tools:
            # ollama 原生 /api/chat 的 tools 直接收 function 对象列表（不含 type 包装）
            payload["tools"] = [t.get("function", t) for t in tools]
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._ollama_url("/api/chat"), data=data,
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        _t = self._OLLAMA_TIMEOUT if timeout is None else float(timeout)
        try:
            resp = opener.open(req, timeout=_t)
        except urllib.error.HTTPError as exc:
            # 部分本地模型/端点不接受图片。仅在服务端明确报告图片错误时
            # 退回文本一次，不重新执行工具，也不把未读到的图片当作证据。
            body = exc.read(4096).decode("utf-8", "replace")
            image_error = any(word in body.lower() for word in ("image", "base64", "vision", "multimodal"))
            if exc.code == 400 and image_error and any(m.get("images") for m in messages):
                text_messages = []
                for message in messages:
                    copy = dict(message)
                    if copy.pop("images", None):
                        copy["content"] = str(copy.get("content") or "") + "\n（图片被模型服务拒绝，本次只能核对文本；不能声称看过图片。）"
                    text_messages.append(copy)
                _gpu_release("ollama")
                return self._ollama_chat(
                    text_messages, stream=stream, temperature=temperature, timeout=timeout,
                    usage_sink=usage_sink, tool_sink=tool_sink, tools=tools,
                    thinking_on=thinking_on, reasoning_sink=reasoning_sink,
                    cancel_event=cancel_event, max_output_tokens=max_output_tokens)
            _gpu_release("ollama")
            raise RuntimeError(f"Ollama 请求被拒绝（HTTP {exc.code}），需核对输入或模型服务配置。") from exc
        except Exception:
            _gpu_release("ollama")
            raise
        if stream:
            def _stream_done():
                # 流式收尾再打一次点：长回答结束时间作为"最后活动"更准确
                _gpu_note_activity("ollama")
                _gpu_release("ollama")
            return _OllamaStream(resp, _stream_done, usage_sink=usage_sink,
                                 tool_sink=tool_sink, reasoning_sink=reasoning_sink,
                                 cancel_event=cancel_event)
        body = json.loads(resp.read().decode("utf-8"))
        _gpu_note_activity("ollama")
        _gpu_release("ollama")
        if usage_sink is not None:
            for k in ("prompt_eval_count", "eval_count"):
                v = body.get(k)
                if isinstance(v, int):
                    usage_sink[k] = v
        msg = body.get("message") or {}
        if tool_sink is not None:
            tool_sink.extend(normalize_tool_calls(msg.get("tool_calls")))
        return msg.get("content", "")

    def count_tokens(self, text):
        """估算/精算文本 token 数，用于上下文预算裁剪。
        llamacpp/ollama 走本地服务的 /tokenize 精算；任何失败或其它 provider
        退回到按 CJK/代码分别估的保守启发式。
        """
        if not text:
            return 0
        try:
            if self.provider == "llamacpp":
                root = str(self.client.base_url).rstrip("/").removesuffix("/v1").rstrip("/")
                url = root + "/tokenize"
                payload = json.dumps({"content": text}).encode("utf-8")
            elif self.provider == "ollama":
                root = str(self.client.base_url).rstrip("/").removesuffix("/v1").rstrip("/")
                url = root + "/api/tokenize"
                payload = json.dumps({"model": self.model, "prompt": text}).encode("utf-8")
            else:
                return self._heuristic_tokens(text)
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            tokens = data.get("tokens")
            return len(tokens) if tokens is not None else self._heuristic_tokens(text)
        except Exception:
            return self._heuristic_tokens(text)

    @staticmethod
    def _heuristic_tokens(text):
        # 保守估算：CJK 约 1.1 token/字，ASCII（代码/英文）约 3.2 字符/token
        cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
        other = len(text) - cjk
        return int(cjk * 1.1 + other / 3.2) + 40

    # ------------------------------------------------------------------
    # 离线 mock：模拟一个 ReAct 风格 Agent 的多步推理，便于无 key 演示与端到端验证
    # 支持：检索命中 -> 直接回答；检索未命中 -> 自我反思并换用联网搜索
    # ------------------------------------------------------------------
    def _mock_chat(self, messages, stream=False):
        # mock 没有视觉能力：用户带图时明确告知，不假装能识别图片内容
        has_image = any(bool(m.get("images")) for m in messages)
        # mock 无服务端 usage：用启发式估算填账本，保证离线演示下 trace 也有 token 数字
        _prompt_est = self._heuristic_tokens(
            "\n".join(m.get("content") for m in messages if isinstance(m.get("content"), str))
        )

        def _mark(text):
            self.last_usage = {
                "prompt_tokens": _prompt_est,
                "completion_tokens": self._heuristic_tokens(text or ""),
            }
            return text
        if has_image:
            answer = (
                "Thought: 用户附带了图片，mock 离线模型没有视觉能力，需如实告知。\n"
                "Final Answer: （演示模式·mock LLM）检测到您附带了图片，但 mock 离线模型"
                "不具备视觉能力，无法识别图片内容。请在 ⚙ 模型设置 中切换到支持视觉的"
                "本地模型（如 Ollama qwen3.6 系列）后重试。"
            )
            _mark(answer)
            if stream:
                def g_img():
                    for ch in answer:
                        yield ch
                return g_img()
            return answer

        last = messages[-1]["content"] if messages else ""

        # 取「当前」问题：取最后一条不含 Observation/Reflection 的 user 消息。
        # 注意要取最后一条而非第一条 —— HTTP 服务复用同一个 Agent 单例，
        # history 里会累积历史提问，取第一条会导致后续轮次一直套用旧问题意图。
        question = messages[-1]["content"] if messages else ""
        for m in reversed(messages):
            if m.get("role") == "user" and "Observation:" not in m["content"] and "Reflection:" not in m["content"]:
                question = m["content"]
                break

        # 素材检索意图识别：命中则优先调用 search_assets
        ASSET_KW = (
            "素材", "美术", "资源", "asset", "sprite", "精灵", "角色",
            "图标", "像素", "pixel", "tileset", "tile", "地形", "贴图",
            "ui", "音效", "音乐", "字体", "font", "cc0", "kenney",
            "opengameart", "itch", "模型", "3d", "动画",
        )
        is_asset = any(k in question.lower() for k in ASSET_KW)

        if "未找到" in last or "Reflection:" in last:
            # 检索未命中 / 被要求反思 -> 换用联网搜索（演示自我反思+换工具重试）
            answer = (
                "Thought: 前面的工具没有命中，我换用联网搜索来补充信息。\n"
                "Action: web_search\n"
                f"Action Input: {question}"
            )
        elif "Observation:" in last:
            # 已拿到有效检索结果，给出最终回答（演示用，引用首段观察内容）
            obs = last.split("Observation:")[-1]
            snippet = obs.strip().split("\n")[0][:200]
            # **必须带 `Final Answer:`**：这一支原来吐的是裸文本，而 `agent.py:3443`
            # 的格式失守判定要求"要么 `Action:` 要么 `Final Answer:`"，于是它连拒两轮、
            # 发一条 `final(status="error")` ——即 **mock provider 结构上收不了一轮问答**，
            # 离线演示模式（本仓唯一零网络的模型路径）同样跑不完。另外两支都带 `Thought:`
            # 与标记，只有这支漏了。
            answer = (
                "Thought: 已拿到检索结果\n"
                "Final Answer: （演示模式·mock LLM）根据检索到的内容："
                f"{snippet} …… 综上所述，这就是与您问题相关的说明。"
            )
        else:
            if is_asset:
                # 用户想找游戏美术/素材，先在精选素材目录中筛选
                answer = (
                    "Thought: 用户想找游戏美术/素材，我先在精选素材目录中筛选。\n"
                    "Action: search_assets\n"
                    f"Action Input: {question}"
                )
            else:
                # 第一步：先决定检索知识库
                answer = (
                    "Thought: 我需要先检索知识库来获取准确信息。\n"
                    "Action: search_knowledge\n"
                    f"Action Input: {question}"
                )

        _mark(answer)
        if stream:
            def g():
                for ch in answer:
                    yield ch

            return g()
        return answer
