"""Agent 工具集：知识库检索 / 素材筛选 / 计算器 / 联网搜索 / 代码执行 / 视频提示词生成。

每个工具含 description（给 LLM 看的说明）与 func（实际执行函数）。
新增工具：在 TOOLS 字典里追加一项即可，Agent 会自动识别。
"""
import ast
import base64
import contextvars
import io
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.request
import urllib.parse
import datetime
import hashlib
import html as html_lib
from html.parser import HTMLParser
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
import mcp_client
import mcp_capabilities
from artifact_tools import create_artifact
from textutil import as_text

from config import (TOP_K, COLLECTION_NAME, CODE_COLLECTION_NAME, CODE_ROOT, get_runtime, set_runtime,
                     edit_confirm_enabled, external_access_high, EXTERNAL_API_ALLOWLIST,
                     get_web_search_provider, get_web_search_api_key, get_web_search_api_url,
                     get_web_fetch_provider, get_web_fetch_api_key, get_web_fetch_api_url)
from config import STATE_ROOT
from embeddings import EmbeddingClient
from vectorstore import pretty_source
from agent_runtime.retrieval import get_retriever
from ingest import _CODE_EXT, _SKIP_DIRS
from data_formats import DataFormatError, read_file as read_data_file, write_data_file
from agent_runtime.tools import ToolResult, ToolSpec, coerce_tool_spec, upgrade_registry
from agent_runtime.tool_catalog import catalog_tool
from agent_runtime.enterprise_sandbox import SandboxUnavailable as EnterpriseSandboxUnavailable
from agent_runtime.enterprise_sandbox import (execution_mode, host_fallback_active, run_agent_command,
                                               stage_execution_active, stage_backend)


_PYTHON_EXEC_TIMEOUT = int(os.getenv("DOCMIND_PYTHON_EXEC_TIMEOUT", "60") or 60)


def _window_output(text: str) -> str:
    """Keep the head and tail of a command output; the tail holds the traceback."""
    from agent_runtime.process_runner import window_text
    return window_text(text)


def _shell_argv(command: str) -> list[str]:
    host_boundary = ((execution_mode() != "enterprise" and not stage_execution_active())
                     or (stage_execution_active() and stage_backend() == "host_compat")
                     or host_fallback_active())
    if os.name == "nt" and host_boundary:
        return ["cmd", "/c", command]
    return ["/bin/sh", "-lc", command]


def _self_verify_command(command: list[str], *, project_root: str, timeout: int):
    """Run deterministic verification under the bounded stage runner."""
    from agent_runtime.enterprise_sandbox import stage_execution_scope
    with stage_execution_scope():
        return run_agent_command(command, project_root=project_root, timeout=timeout)

_emb = None

# 联网搜索的轻量缓存只保存公开搜索摘要，不保存网页正文或 API 密钥。
# 缓存文件放在运行时状态根，最多 64 项、默认 10 分钟，写入失败不影响搜索。
_WEB_CACHE_FILE = os.path.join(STATE_ROOT, ".docmind_web_search_cache.json")
_WEB_CACHE_TTL = int(os.getenv("DOCMIND_WEB_CACHE_TTL", "600"))
_WEB_CACHE_MAX = 64
_WEB_CACHE_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# start_workflow 依赖注入
#
# 工具只负责解析参数/渲染指引；真正「组装项目、策略、LLM 回调并落工作流状态」
# 的 launcher 由 api_routes 在 build_router 时注册（与 WORKFLOWS 的执行回调
# 同风格）。tools 层不 import api_routes，避免循环依赖。
# ---------------------------------------------------------------------------
_WORKFLOW_LAUNCHER = None


def set_workflow_launcher(fn):
    """注册对话内发起工作流的 launcher：fn(goal, *, kind, web_enabled) -> 工作流 public dict。"""
    global _WORKFLOW_LAUNCHER
    _WORKFLOW_LAUNCHER = fn if callable(fn) else None


# 对话 SSE 联动：start_workflow 成功后把有界摘要投递到「当前 chat 流」的持有者，
# api.py 的 chat 流每轮工具执行后 take_pending_workflow(holder) 取走（取走即清），
# 向前端补发结构化 workflow 事件，由对话内工作流卡片直接接管，无需独立面板。
#
# 为什么不用 contextvars：chat 是同步生成器，Starlette/anyio 在线程池里每次 next()
# 都会 copy_context，工具线程里的 set 既可能对下一次迭代不可见、清除也可能不生效
# （实测会重复补发）。模块级活动流栈是跨线程共享的真实对象，append/pop 确定生效；
# 单机单用户桌面场景下并发发起工作流的竞态可忽略，工具固定投递到栈顶（最近注册）流。
_pending_streams: list = []
_orphan_pending: list = []


def push_pending_stream() -> list:
    """chat SSE 流开始时注册自己的待取队列，返回该队列持有者。"""
    holder: list = []
    _pending_streams.append(holder)
    return holder


def pop_pending_stream(holder) -> None:
    """chat SSE 流结束时注销（finally 调用，残留条目转交兜底队列）。"""
    try:
        _pending_streams.remove(holder)
    except ValueError:
        pass
    if holder:
        _orphan_pending.extend(holder[-1:] if len(holder) > 1 else holder)


def _notify_workflow_started(workflow: dict) -> None:
    try:
        summary = {
            "workflow_id": str(workflow.get("workflow_id") or ""),
            "status": str(workflow.get("status") or "awaiting_choice"),
            "phase": str(workflow.get("phase") or "clarify"),
            "kind": str(workflow.get("kind") or "generic"),
            "options_count": len(workflow.get("options") or []),
        }
        (_pending_streams[-1] if _pending_streams else _orphan_pending).append(summary)
    except Exception:
        pass


def take_pending_workflow(holder=None):
    """取走并清除指定流（或兜底队列）内待通知的工作流摘要（无则 None）。"""
    src = holder if holder is not None else _orphan_pending
    try:
        return src.pop(0)
    except (IndexError, TypeError):
        return None


# 本轮会话联网开关：Agent.run 开始时注入。start_workflow 未显式给 web 时缺省继承，
# 保证「对话里开了联网 → 工作流方案也能联网补资料」。
_session_web_enabled = contextvars.ContextVar(
    "docmind_session_web_enabled", default=False)


def set_session_web_enabled(enabled):
    """在当前 context 内设置会话联网缺省值，返回 reset token。"""
    return _session_web_enabled.set(bool(enabled))


# 本轮会话当前模型的视觉能力模式（native/unknown/none...）。云端按请求覆盖
# llm 时全局 runtime/环境变量反映不出该模型能力，web 抓图门据此与本轮模型对齐。
_session_vision_mode = contextvars.ContextVar(
    "docmind_session_vision_mode", default=None)


def set_session_vision_mode(mode):
    """在当前 context 内设置本轮模型的视觉能力画像，返回 reset token。"""
    return _session_vision_mode.set(mode or None)


def _web_cache_key(prefix, query):
    return f"{prefix}:{hashlib.sha256((query or '').encode('utf-8')).hexdigest()}"


def _web_cache_read(key):
    if os.getenv("DOCMIND_WEB_CACHE", "1").strip().lower() in ("0", "false", "no"):
        return None
    with _WEB_CACHE_LOCK:
        try:
            with open(_WEB_CACHE_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            item = data.get(key) if isinstance(data, dict) else None
            if not isinstance(item, dict) or time.time() - float(item.get("time", 0)) > _WEB_CACHE_TTL:
                return None
            return item.get("value")
        except (OSError, ValueError, TypeError):
            return None


def _web_cache_write(key, value):
    if os.getenv("DOCMIND_WEB_CACHE", "1").strip().lower() in ("0", "false", "no"):
        return
    with _WEB_CACHE_LOCK:
        try:
            os.makedirs(os.path.dirname(_WEB_CACHE_FILE), exist_ok=True)
            try:
                with open(_WEB_CACHE_FILE, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[key] = {"time": time.time(), "value": value}
            fresh = sorted(data.items(), key=lambda kv: float(kv[1].get("time", 0)), reverse=True)[:_WEB_CACHE_MAX]
            temporary = _WEB_CACHE_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as fh:
                json.dump(dict(fresh), fh, ensure_ascii=False)
            os.replace(temporary, _WEB_CACHE_FILE)
        except (OSError, ValueError, TypeError):
            return


def clear_web_search_cache():
    """清空摘要缓存；设置页或测试可调用，正文缓存从不落盘。"""
    with _WEB_CACHE_LOCK:
        try:
            os.unlink(_WEB_CACHE_FILE)
        except FileNotFoundError:
            pass


def _source_score(url, title="", snippet=""):
    """给检索结果一个可解释的 0~1 分数，不把分数当作事实正确性。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    score, reasons = 0.45, []
    if url.lower().startswith("https://"):
        score += .08; reasons.append("HTTPS")
    if host.endswith(("github.com", "bilibili.com", "zhihu.com", "tieba.baidu.com",
                      "xiaohongshu.com", "weibo.com", "csdn.net", "stackoverflow.com")):
        score += .18; reasons.append("平台原站")
    if any(x in host for x in ("docs.", "developer.", "dev.", "learn.")):
        score += .12; reasons.append("文档域名")
    if any(x in (title + " " + snippet).lower() for x in ("official", "官方", "documentation", "文档")):
        score += .08; reasons.append("标题含官方/文档")
    return round(min(score, .95), 2), "、".join(reasons) or "通用网页来源"


def _format_search_result(title, snippet, url, *, source="web"):
    score, reason = _source_score(url, title, snippet)
    return f"· {title}\n  {snippet[:240]}\n  {url}\n  可信度参考：{score:.2f}（{reason}；仅供排序，需核对正文）"


def _search_cache_enabled():
    # 单测会反复使用相同关键词并替换后端；缓存不能遮住这些调用。
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if getattr(spec, "name", None) == "unittest.__main__" or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return os.getenv("DOCMIND_WEB_CACHE", "1").strip().lower() not in ("0", "false", "no")


def _get_emb():
    global _emb
    if _emb is None:
        _emb = EmbeddingClient()
    return _emb

def dev_mcp_call(arg):
    """MCP 调用外层审计：允许用户在连接器边界插入断点/拦截。

    ## fail-closed（2026-10-14）：hook 异常不再被吞

    之前 `import hooks` 或 `before_mcp` 抛异常时一律 `_workflow_hooks = None`
    并继续执行实际 MCP 调用——hook 是审计/审批边界，崩溃后还照样跑
    等于把审计当空气。现在 hook 异常时返回明确失败，调用方知道
    「此次 MCP 没经过审计」；外层会按失败处理。
    """
    import hooks as _workflow_hooks
    try:
        before = _workflow_hooks.run_workflow("before_mcp", {
            "argument_chars": len(str(arg or "")),
        })
        if before.get("blocked"):
            return "MCP 调用已被钩子拦截：" + str(before.get("reason") or "需要人工审核")
    except Exception as exc:
        return ("MCP 调用前钩子异常（%s），拒绝执行以免绕过审计。"
                "请检查 hooks 配置后重试。" % type(exc).__name__)
    result = _dev_mcp_call_impl(arg)
    try:
        _workflow_hooks.run_workflow("after_mcp", {
            "ok": not str(result or "").startswith("MCP 调用失败"),
            "result_chars": len(str(result or "")),
        })
    except Exception as exc:
        # after_mcp 异常不影响 MCP 已完成的事实，但记日志让排查可见
        logging.getLogger(__name__).warning(
            "MCP 调用后钩子异常：%s", type(exc).__name__, exc_info=True)
    return result


def _dev_mcp_call_impl(arg):
    """Agent 受控调用已启用 MCP 连接器。输入 key/name/arguments(JSON)。"""
    root = get_runtime('code_root') or CODE_ROOT
    if not root: return 'MCP 调用失败：未配置代码库。'
    lines = str(arg or '').splitlines(); data={}; body=[]
    for line in lines:
        if ':' in line and not body:
            k,v=line.split(':',1); data[k.strip()]=v.strip()
        else: body.append(line)
    key=data.get('key',''); name=data.get('name',''); task_id=data.get('task_id','')
    if not key or not name: return 'MCP 调用失败：需要 key 和 name。'
    try:
        cfg=mcp_client.get_server_config(root,key)
        if not cfg.get('enabled'): return 'MCP 调用失败：连接器未启用，请先在工作台启用并审批。可调用 dev_route_connector 查看其它已启用连接器。'
        if task_id:
            from game_workbench import list_tasks
            task=next((t for t in list_tasks(root) if str(t.get('id'))==str(task_id)),None)
            if not task:
                return 'MCP 调用失败：task_id 不存在。'
            raw_args=data.get('arguments','{}'); check_args=raw_args
            if isinstance(raw_args,str):
                try: check_args=json.loads(raw_args) if raw_args else {}
                except Exception: check_args={}
            paths=[]
            if isinstance(check_args,dict):
                for k,v in check_args.items():
                    if k.lower() in ('path','file','scene','asset','script') and isinstance(v,str): paths.append(v.replace('res://','').lstrip('/'))
            if paths:
                from game_workbench import validate_task_scope
                scope=validate_task_scope(root,{**task,'files':paths})
                if not scope.get('ok'): return 'MCP 调用失败：参数路径超出任务分区。'
        args=data.get('arguments','{}')
        if isinstance(args,str): args=json.loads(args) if args else {}
        fallback = data.get('fallback_keys') or data.get('fallback') or []
        if isinstance(fallback, str):
            fallback = [item.strip() for item in fallback.split(',') if item.strip()]
        hint = data.get('hint') or data.get('task_hint') or ''
        side_effect = str(data.get('side_effect', '')).strip().lower() in ('1', 'true', 'yes', 'on')
        allow_side_effect_fallback = str(data.get('allow_side_effect_fallback', '')).strip().lower() in ('1', 'true', 'yes', 'on')
        try:
            result = mcp_client.call_tool_with_fallback(
                root, key, name, args, fallback_keys=fallback,
                task_hint=hint, timeout=mcp_client.CALL_TIMEOUT,
                side_effect=side_effect,
                allow_side_effect_fallback=allow_side_effect_fallback)
        except mcp_client.MCPError as exc:
            attempts = getattr(exc, 'attempts', [])
            suffix = (' 尝试记录：' + json.dumps(attempts, ensure_ascii=False)) if attempts else ''
            hint = _mcp_failure_hint(str(exc))
            return (f'MCP 调用失败：{exc}{suffix}'
                    + (f'（{hint}）' if hint else
                       '（可改用内置工具或人工检查连接器）'))
        return json.dumps(result, ensure_ascii=False)[:6000]
    except Exception as e: return f'MCP 调用失败：{e}（若怀疑是连接器选择有误，可先调用 dev_route_connector 重新挑选已启用连接器）'


def dev_list_connectors(arg):
    """列出已配置 MCP 连接器（key/label/engine/transport/启用状态/能力标签/适用说明），供 Agent 自主挑选。输入留空。"""
    root = get_runtime('code_root') or CODE_ROOT
    if not root: return '连接器列表失败：未配置代码库。'
    try:
        rows = mcp_client.connector_directory(root)
        return json.dumps({"ok": True, "connectors": rows}, ensure_ascii=False)
    except Exception as e:
        return f'连接器列表失败：{e}'


def dev_route_connector(arg):
    """按任务语义挑选最合适的【已启用】MCP 连接器。输入 hint（任务描述，如 'Godot 里打开 Main 场景并运行'）。

    返回排序候选与匹配理由；Agent 应取 top.key 作为 dev_mcp_call 的 key。无已启用连接器匹配时，
    提示改用具内工具（search_code/apply_edit/python_exec）或先在工作台启用对应引擎连接器。
    """
    root = get_runtime('code_root') or CODE_ROOT
    if not root: return '连接器路由失败：未配置代码库。'
    hint = str(arg or '').strip().splitlines()[0] if str(arg or '').strip() else ''
    try:
        ranked = mcp_client.select_connector(root, hint)
        if not ranked:
            return json.dumps({"ok": True, "hint": hint, "matches": [],
                               "message": "没有已启用的连接器匹配该任务；可改用内置工具（search_code/apply_edit/python_exec 等），或先在工作台启用对应引擎连接器。"}, ensure_ascii=False)
        return json.dumps({"ok": True, "hint": hint, "top": ranked[0]["key"], "matches": ranked}, ensure_ascii=False)
    except Exception as e:
        return f'连接器路由失败：{e}'


def _mcp_failure_hint(exc_text: str) -> str:
    """把 MCP 失败文本里的关键信号提炼成一句**可行动**的话。

    为什么需要：`mcp_client` 抛的 `MCPError` 带着子进程 stderr 尾迹（上限 800 字），
    真信号（`PORT_OCCUPIED` / `Port 8000 is occupied`）埋在 "Installed 73 packages"
    这类安装噪声里。Agent 拿到原始尾迹要自己翻，而这一步它在 60~70 秒的失败之后
    做——每翻错一次就是又一次同样的失败。

    实测现场（2026-10-16）：Godot 连接器的 `uvx godot-ai attach` 起不来，真因是
    它的 bridge 想用 **8000** 端口，而那是 DocMind 后端自己的端口。
    """
    low = str(exc_text or "")
    if "port_occupied" in low.lower() or "address already in use" in low.lower():
        return ("连接器要用的端口被占用了（很可能是 DocMind 后端自己占着）——"
                "改端口或停掉占用进程后再试；这类失败重试不会变好。")
    if "引擎未运行" in low or "编辑器处于打开状态" in low:
        return "目标引擎/编辑器没有打开——先打开它再重试。"
    return ""


def dev_list_connector_tools(arg):
    """列出某连接器暴露的工具（name/description/input_schema），确定 dev_mcp_call 的 name 与参数。

    输入 key: <连接器key>。仅对打算调用的连接器使用（godot 等 stdio 需先建立会话，引擎未开会失败）。
    """
    root = get_runtime('code_root') or CODE_ROOT
    if not root: return '工具清单失败：未配置代码库。'
    key = str(arg or '').strip()
    if key.startswith('key:'):
        key = key[len('key:'):].strip()
    if not key: return '工具清单失败：需要 key（key: <连接器key>）。'
    try:
        r = mcp_client.list_tools(root, key)
        return json.dumps(r, ensure_ascii=False)[:6000]
    except Exception as e:
        hint = _mcp_failure_hint(str(e))
        return (f'工具清单失败：{e}'
                + (f'（{hint}）' if hint else
                   '（连接器可能未启用或引擎未运行，可先用 dev_route_connector 换一个）'))


# ---------------------------------------------------------------------------
# MCP 连接器自助装配：搜索目录 → 审批后添加 → 探活 → 发现能力 → 审批路由
#
# 与设置页 UI 走同一套后端（mcp_capabilities / mcp_client），不另造逻辑。
# 两道人工可审计的审批门（game_workbench 审批台账，30 分钟有效）：
#   1) add/remove 连接器（action=mcp_server）：stdio 的 command 等于可执行任意命令，
#      审批 target 绑定 key+command+args+url 的哈希，换命令必须重新审批；
#   2) 批准能力路由（action=mcp_capability）：发现只生成 pending 候选，批准后才进路由器。
# ---------------------------------------------------------------------------

def _mcp_project_root():
    return get_runtime('code_root') or CODE_ROOT


def _mcp_server_approval_target(key, cfg):
    """把 add 审批绑定到具体连接参数，防止同 key 审批被换成别的 command/url 复用。"""
    raw = json.dumps({
        "transport": cfg.get("transport"),
        "command": (cfg.get("command") or "").strip(),
        "args": [str(a) for a in (cfg.get("args") or [])],
        "url": (cfg.get("url") or "").strip(),
    }, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha1((key.strip().lower() + "|" + raw).encode("utf-8")).hexdigest()[:10]
    return f"{key.strip()}:{digest}"


def _mcp_gate_blocked(root, action, target):
    """审批门未通过时返回统一阻断结构（None=放行）。"""
    from game_workbench import require_approval
    return require_approval(root, action, target)


def dev_mcp_search(arg):
    """搜索 MCP 连接方式：离线领域指引 + 用户允许联网时的真实 Registry 候选。

    输入：能力关键词（至少 2 个字符）。联网开关继承当前 Agent 会话；关时绝不查 Registry。
    Registry 候选先过 R1-R9，模型仍须展示候选、经审批后装配；搜索摘要不能直接执行。

    返回 JSON：ok / results[]（id,label,summary,capabilities,connection_options,
    setup_steps,template{key,label,transport,command,args,url},source_status）。
    注意 template.command 可能为空（离线指引类条目）；带 config 的 Registry 选项可直接预览。
    """
    root = _mcp_project_root()
    if not root:
        return 'MCP 搜索失败：未配置代码库。'
    query = str(arg or '').strip()
    if query.lower().startswith("query:"):
        query = query[6:].strip()
    try:
        web_enabled = _session_web_enabled.get()
        if web_enabled:
            from mcp_registry_bridge import registry_fn_for
            result = mcp_capabilities.search_directory(
                query, web_enabled=True, registry_fn=registry_fn_for(root))
        else:
            result = mcp_capabilities.search_directory(query, web_enabled=False)
    except Exception as e:  # noqa: BLE001
        return f'MCP 搜索失败：{type(e).__name__}: {e}'
    if not result.get("ok"):
        return f'MCP 搜索失败：{result.get("error") or "未知错误"}'
    items = []
    for item in result.get("results") or []:
        items.append({
            "id": item.get("id"),
            "label": item.get("label"),
            "summary": item.get("summary"),
            "capabilities": item.get("capabilities") or [],
            "connection_options": item.get("connection_options") or [],
            "setup_steps": item.get("setup_steps") or [],
            "template": item.get("template") or {},
            "source_status": item.get("source_status"),
        })
    return json.dumps({"ok": True, "query": query, "results": items}, ensure_ascii=False)


def dev_mcp_add(arg):
    """装配（新增/更新）一个 MCP 连接器配置。敏感操作：必须先通过 mcp_server 审批。

    输入（多行 key: value）：
      key: <连接器标识，仅字母数字/_/-，≤40>
      label: <显示名，可选>
      transport: <stdio|http，默认 stdio>
      # stdio 必填：
      command: <启动命令，如 uvx>
      args: <空格分隔的参数，如 kicad-mcp-pro --transport stdio>
      args_json: <可选，JSON 数组，优先于 args>
      # http 必填：
      url: <http(s)://.../mcp>
      enabled: <true|false，默认 true>

    首次调用未获用户确认时返回 blocked/approval_required，其中含 action 与 target；
    向用户展示阻断结构里的完整 command/args 或 URL，用户在 MCP 设置中确认后，
    再用【完全相同的参数】重试本工具。target 已绑定命令/URL，改参数需重新确认。
    成功返回 {ok, server:{key,transport,enabled,...}}，随后应调用 dev_mcp_probe 探活。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    f = _parse_keyed(str(arg or ""), ["key", "label", "transport", "command",
                                      "args", "args_json", "url", "enabled"])
    key = (f.get("key") or "").strip()
    if not key:
        return json.dumps({"ok": False, "error": "缺少 key（连接器标识）。"}, ensure_ascii=False)
    if not all(ch.isalnum() or ch in "_-" for ch in key) or len(key) > 40:
        return json.dumps({"ok": False,
                           "error": "key 仅允许字母数字、下划线、连字符（≤40）。"}, ensure_ascii=False)
    transport = (f.get("transport") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        return json.dumps({"ok": False, "error": "transport 仅支持 stdio 或 http。"}, ensure_ascii=False)
    cfg = {"label": (f.get("label") or "").strip() or key}
    if transport == "stdio":
        command = (f.get("command") or "").strip()
        if not command:
            return json.dumps({"ok": False,
                               "error": "stdio 连接器缺少 command；请先从官方文档确认启动命令。"},
                              ensure_ascii=False)
        cfg["command"] = command
        raw_args = (f.get("args_json") or "").strip()
        if raw_args:
            try:
                parsed = json.loads(raw_args)
                args = [str(a) for a in parsed] if isinstance(parsed, list) else None
            except ValueError:
                args = None
            if args is None:
                return json.dumps({"ok": False, "error": "args_json 必须是 JSON 数组。"},
                                  ensure_ascii=False)
        else:
            args = [a for a in (f.get("args") or "").split() if a]
        cfg["args"] = args
        cfg["env"] = {}
    else:
        url = (f.get("url") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            return json.dumps({"ok": False, "error": "http 连接器需要合法 http(s) URL。"},
                              ensure_ascii=False)
        cfg["url"] = url
    enabled_raw = (f.get("enabled") or "").strip().lower()
    cfg["enabled"] = enabled_raw not in ("0", "false", "no", "off")
    cfg["transport"] = transport

    target = _mcp_server_approval_target(key, cfg)
    gate = _mcp_gate_blocked(root, "mcp_server", target)
    if gate:
        gate["hint"] = ("请用户在 MCP 设置中确认该连接器（target: " + target
                        + "）；确认后用完全相同的参数重试 dev_mcp_add。")
        return json.dumps(gate, ensure_ascii=False)
    try:
        mcp_client.save_server(root, key, cfg)
        server = mcp_client.get_server_config(root, key)
    except mcp_client.MCPError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "action": "added",
                       "next": "调用 dev_mcp_probe 测试连接（initialize + tools/list）",
                       "server": {k: server.get(k) for k in
                                  ("key", "label", "transport", "enabled", "engine")}},
                      ensure_ascii=False)


def dev_mcp_probe(arg):
    """探活一个已装配连接器：完成 MCP initialize + tools/list，返回工具数量与名称。

    输入：连接器 key（可带 'key: ' 前缀）。stdio 首次冷启动（如 uvx 下载依赖）可能耗时较久。
    返回 {ok,server,transport,tool_count,tools[],elapsed_ms}；失败返回 {ok:false,error}，
    据此修正 command/args/url 后重新 dev_mcp_add，或 dev_mcp_remove 移除。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    key = str(arg or "").strip()
    if key.lower().startswith("key:"):
        key = key[4:].strip()
    if not key:
        return json.dumps({"ok": False, "error": "缺少连接器 key。"}, ensure_ascii=False)
    try:
        result = mcp_client.probe_server(root, key)
        # 工具列表可能很长，只回传名称，schema 用 dev_list_connector_tools 看
        result["tools"] = (result.get("tools") or [])[:60]
        result["next"] = "探活成功后调用 dev_mcp_discover 生成能力候选"
        return json.dumps(result, ensure_ascii=False)[:6000]
    except mcp_client.MCPError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def dev_mcp_discover(arg):
    """读取连接器工具并生成【待用户确认】能力候选（不会自动启用路由）。

    输入：连接器 key。前置：dev_mcp_add 已添加且 dev_mcp_probe 成功（引擎类 stdio 还需
    对应软件已打开）。返回候选摘要：domain/capabilities/tool_count/confidence/tool_mappings。
    之后必须向用户说明该连接器能做什么，用户确认后再 dev_mcp_decide(decision: approve)
    才允许 Agent 自动路由调用。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    key = str(arg or "").strip()
    if key.lower().startswith("key:"):
        key = key[4:].strip()
    if not key:
        return json.dumps({"ok": False, "error": "缺少连接器 key。"}, ensure_ascii=False)
    try:
        result = mcp_capabilities.discover(root, key)
    except mcp_client.MCPError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    if not result.get("ok"):
        return json.dumps(result, ensure_ascii=False)
    cand = result["candidate"]
    return json.dumps({"ok": True, "candidate": {
        "server": cand["server"], "domain": cand["domain"], "status": cand["status"],
        "capabilities": cand["capabilities"], "tool_count": cand["tool_count"],
        "confidence": cand["confidence"], "best_for": cand["best_for"],
        "tool_mappings": cand["tool_mappings"][:40],
    }, "next": ("向用户说明能力清单并取得确认；用户确认后再 "
                f"dev_mcp_decide(decision: approve, key: {key})")},
        ensure_ascii=False)[:6000]


def dev_mcp_decide(arg):
    """批准/拒绝连接器的能力候选。批准是敏感操作，需先通过用户确认。

    输入（多行 key: value）：
      decision: approve   # 或 reject
      key: <连接器key>
    批准后该连接器进入路由器，Agent 可经 dev_route_connector/dev_mcp_call 自动调用；
    拒绝则候选作废（不影响已保存的连接器配置，可用 dev_mcp_remove 彻底移除）。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    f = _parse_keyed(str(arg or ""), ["decision", "key", "approved"])
    key = (f.get("key") or "").strip()
    decision = (f.get("decision") or f.get("approved") or "").strip().lower()
    approved = decision in ("approve", "approved", "1", "true", "yes", "on")
    rejected = decision in ("reject", "rejected", "0", "false", "no", "off")
    if not key or not (approved or rejected):
        return json.dumps({"ok": False,
                           "error": "需要 key 与 decision(approve|reject)。"}, ensure_ascii=False)
    if approved:
        gate = _mcp_gate_blocked(root, "mcp_capability", key)
        if gate:
            gate["hint"] = (f"请用户在 MCP 设置中确认连接器 {key} 的能力，"
                            "确认后重试 dev_mcp_decide。")
            return json.dumps(gate, ensure_ascii=False)
    try:
        result = mcp_capabilities.approve(root, key, approved)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    if not result.get("ok"):
        return json.dumps(result, ensure_ascii=False)
    return json.dumps({"ok": True, "decision": "approved" if approved else "rejected",
                       "server": key,
                       "routing_enabled": approved}, ensure_ascii=False)


def dev_mcp_remove(arg):
    """移除（自定义）或禁用（内置预设）一个 MCP 连接器。敏感操作：需先通过用户确认。

    输入：连接器 key（可带 'key: ' 前缀）。会先关闭活动会话。审批 target 即 key 本身：
    用户在 MCP 设置中确认后重试。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    key = str(arg or "").strip()
    if key.lower().startswith("key:"):
        key = key[4:].strip()
    if not key:
        return json.dumps({"ok": False, "error": "缺少连接器 key。"}, ensure_ascii=False)
    gate = _mcp_gate_blocked(root, "mcp_server", key)
    if gate:
        gate["hint"] = (f"请用户在 MCP 设置中确认移除 {key}，确认后重试 dev_mcp_remove。")
        return json.dumps(gate, ensure_ascii=False)
    try:
        mcp_client.close_server(root, key)
        result = mcp_client.remove_server(root, key)
        servers = [{"key": s.get("key"), "enabled": s.get("enabled")}
                   for s in result.get("servers", [])]
        return json.dumps({"ok": True, "action": "removed_or_disabled",
                           "server": key, "servers": servers}, ensure_ascii=False)
    except mcp_client.MCPError as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def dev_mcp_discover_from_need(arg):
    """从自然语言需求发现可装配的 MCP 连接器候选（不写盘、不自动启用）。

    输入：需求描述，如「我需要能查高铁票的 MCP / 数据库 MCP / github MCP」。
    可选多行 web_enabled: true 开启联网（默认仅离线精选索引）。

    流程（离线优先；联网时先查官方 Registry，再回落 GitHub 域）：
      0) web_enabled 时查官方 MCP Registry（自主发现主路径，结果强制过 R1-R9）；
      1) 离线精选索引命中已知热门 server；
      2) web_enabled 时 GitHub 域限定搜索（platform:github）→ 仓库 README → 解析官方命令；
    候选均过 R1-R9 信任闸门。返回后须向用户展示候选，逐条经 dev_mcp_add（审批）落盘。
    """
    root = _mcp_project_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    raw = str(arg or "")
    web_enabled = False
    lines = []
    for ln in raw.splitlines():
        m = re.match(r"^\s*web_enabled\s*[:：]\s*(\S+)", ln, re.I)
        if m:
            web_enabled = m.group(1).lower() in ("1", "true", "yes", "on")
        else:
            lines.append(ln)
    need = re.sub(r"^(?:need|需求)\s*[:：]\s*", "", " ".join(lines).strip(), flags=re.I).strip() or raw.strip()
    if not need:
        return json.dumps({"ok": False, "error": "需求描述为空"}, ensure_ascii=False)
    try:
        import mcp_autoconnect
        result = mcp_autoconnect.discover_from_need(
            root, need, web_enabled=web_enabled,
            github_search_fn=(lambda q: web_search("platform:github " + q + " MCP server")),
            registry_fn=(mcp_autoconnect.registry_fn_for(root) if web_enabled else None))
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    if not result.get("ok"):
        return json.dumps({"ok": False, "candidates": [],
                           "search_error": result.get("search_error") or "未找到匹配的 MCP 连接器",
                           "next": "可换更具体的需求词，或 web_enabled: true 联网经 GitHub 搜索"},
                          ensure_ascii=False)
    cands = []
    for c in result.get("candidates") or []:
        cfg = c.get("config") or {}
        cands.append({
            "transport": cfg.get("transport"), "command": cfg.get("command"),
            "args": cfg.get("args"), "url": cfg.get("url"),
            "trust": c.get("trust"), "validation_errors": c.get("validation_errors"),
            "provenance": cfg.get("provenance"),
            "command_unresolved": cfg.get("command_unresolved"),
        })
    return json.dumps({"ok": True, "need": need, "source": result.get("source"),
                       "candidates": cands, "search_error": result.get("search_error"),
                       "next": "向用户展示候选；逐条 dev_mcp_add（key/command/args 或 url）经审批落盘，"
                               "再 dev_mcp_probe 探活、dev_mcp_discover 生成能力候选"},
                      ensure_ascii=False)[:8000]


def _skill_pending_dir():
    """待审批技能草稿目录；reload() 扫描排除以 '.' 开头的目录，故草稿不会自动生效。"""
    import skills as _skills_mod
    return os.path.join(os.path.abspath(_skills_mod.SKILLS_DIR), ".pending")


def dev_skill_create(arg):
    """起草用户技能（待审批，不会自动启用）—— skill 是*可执行行为*，必须经用户确认才激活。

    输入（多行 key: value）：name / description / body（技能正文，markdown）。
    写入 SKILLS_DIR/.pending/<name>/SKILL.md，返回完整正文供代理向用户展示。
    用户明确同意后才调用 dev_skill_approve 激活；严禁未经确认直接激活。
    """
    f = _parse_keyed(str(arg or ""), ["name", "description", "body"])
    name = (f.get("name") or "").strip()
    desc = (f.get("description") or "").strip()
    body = (f.get("body") or "").strip()
    if not name or not body:
        return json.dumps({"ok": False, "error": "name 与 body 必填"}, ensure_ascii=False)
    import skills as _skills_mod
    if _skills_mod.get(name):
        return json.dumps({"ok": False, "error": f"技能 {name} 已存在（活动态），勿覆盖"}, ensure_ascii=False)
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:80].strip(".-")
    if not clean:
        return json.dumps({"ok": False, "error": "技能名清洗后为空"}, ensure_ascii=False)
    pdir = _skill_pending_dir()
    tdir = os.path.join(pdir, clean)
    if os.path.exists(tdir):
        return json.dumps({"ok": False, "error": f"草稿 {clean} 已存在待审批，先 dev_skill_approve 或 dev_skill_reject"},
                          ensure_ascii=False)
    os.makedirs(tdir, exist_ok=True)
    raw = ("---\nname: %s\nversion: 0.1.0\ndescription: %s\nwhen_to_use: 用户确认后启用的复用工作流\n---\n\n%s\n"
           % (clean, desc.replace("\n", " "), body))
    try:
        with open(os.path.join(tdir, "SKILL.md"), "w", encoding="utf-8", newline="\n") as s:
            s.write(raw)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "name": clean, "status": "pending", "body": body,
                       "next": "向用户完整展示正文并取得明确同意后，调用 dev_skill_approve(name: %s) 激活" % clean},
                      ensure_ascii=False)


def dev_preview_adapter_create(arg):
    """让 Agent 为当前项目生成一个待审批的领域预览适配器清单。"""
    fields = _parse_keyed(str(arg or ""), [
        "id", "name", "label", "domain", "artifact_kinds", "capture_adapter",
        "refresh_tool", "connector_hint", "validation", "evidence", "runtime", "entrypoint", "source",
    ])
    adapter_id = (fields.get("id") or fields.get("name") or "").strip()
    if not adapter_id:
        return json.dumps({"ok": False, "error": "id 或 name 必填"}, ensure_ascii=False)
    def split_values(value):
        try:
            parsed = json.loads(value) if str(value).strip().startswith("[") else None
            if isinstance(parsed, list):
                return parsed
        except (ValueError, TypeError):
            pass
        return [item.strip() for item in str(value or "").split(",") if item.strip()]
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置当前项目"}, ensure_ascii=False)
    try:
        from agent_runtime.adapter_catalog import create_generated
        manifest = create_generated(root, {
            "id": adapter_id, "label": fields.get("label") or adapter_id,
            "domain": fields.get("domain") or "generic",
            "artifact_kinds": split_values(fields.get("artifact_kinds") or ""),
            "capture_adapter": fields.get("capture_adapter") or "agent_generated",
            "refresh_tool": fields.get("refresh_tool") or "",
            "connector_hint": fields.get("connector_hint") or "",
            "validation": split_values(fields.get("validation") or ""),
            "evidence": fields.get("evidence") or "模型生成的领域预览适配器",
            "runtime": fields.get("runtime") or "",
            "entrypoint": fields.get("entrypoint") or "adapt",
            "source": fields.get("source") or "",
        })
        return json.dumps({"ok": True, "manifest": manifest, "next":
                           "向用户展示适配器方案、代码摘要和验证条件；用户明确确认后再调用 dev_preview_adapter_approve"},
                          ensure_ascii=False)
    except (OSError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def dev_preview_adapter_approve(arg):
    """在用户明确确认后激活或拒绝 Agent 生成的项目适配器。"""
    fields = _parse_keyed(str(arg or ""), ["id", "decision", "approved"])
    adapter_id = (fields.get("id") or "").strip()
    decision = (fields.get("decision") or fields.get("approved") or "").strip().lower()
    if not adapter_id or decision not in {"approve", "approved", "yes", "true", "reject", "rejected", "no", "false"}:
        return json.dumps({"ok": False, "error": "需要 id 和 decision: approve|reject"}, ensure_ascii=False)
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置当前项目"}, ensure_ascii=False)
    try:
        from agent_runtime.adapter_catalog import approve_generated
        manifest = approve_generated(root, adapter_id, decision in {"approve", "approved", "yes", "true"})
        return json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False)
    except (OSError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


_PREVIEW_BUILTIN_REFRESH_TOOLS = frozenset({"preview_project", "game_screenshot", "dev_desktop_capture", "self_verify"})


def _generated_adapter_tool_result(adapter_id, source_tool, adapted, module=""):
    """Convert generated adapter JSON into a durable preview ToolResult.

    The adapter remains responsible only for data conversion.  This boundary
    adds the standard preview protocol metadata so the Agent event recorder
    and workflow preview builder can retain the result as evidence.
    """
    from agent_runtime.preview_adapters import normalize_artifact

    rows = []
    if isinstance(adapted, dict) and isinstance(adapted.get("artifacts"), list):
        rows = [item for item in adapted["artifacts"] if isinstance(item, dict)]
    elif isinstance(adapted, list):
        rows = [item for item in adapted if isinstance(item, dict)]
    elif isinstance(adapted, dict):
        artifact_keys = {"id", "kind", "artifact_kind", "path", "uri", "url", "label",
                         "summary", "description", "before", "after", "content", "evidence"}
        if artifact_keys.intersection(adapted):
            rows = [adapted]
    if not rows:
        rows = [{
            "id": "%s-result" % adapter_id,
            "kind": "structured",
            "label": "%s 适配器结果" % adapter_id,
            "summary": "生成适配器把 %s 的返回数据转换为结构化预览证据" % source_tool,
            "after": json.dumps(adapted, ensure_ascii=False, separators=(",", ":")),
        }]
    artifacts = []
    for index, row in enumerate(rows[:32], 1):
        item = dict(row)
        item.setdefault("adapter", adapter_id)
        metadata = dict(item.get("metadata") or {})
        metadata.update({"generated_adapter": "true", "source_tool": source_tool})
        if module:
            metadata.setdefault("module", module)
        item["metadata"] = metadata
        artifacts.append(normalize_artifact(item, fallback_id="%s-artifact-%s" % (adapter_id, index)))
    text = json.dumps(adapted, ensure_ascii=False, separators=(",", ":"))[:12000]
    return ToolResult(
        ok=True,
        text="适配器刷新完成（来源：%s），已生成 %d 条预览证据：%s" % (source_tool, len(artifacts), text),
        data=adapted,
        artifacts=artifacts,
    )


def _preview_refresh_reference(value):
    """Parse a generated adapter refresh reference without importing code."""
    ref = str(value or "").strip()
    if ref.startswith("builtin:"):
        name = ref[len("builtin:"):].strip()
        return ("builtin", name) if name in _PREVIEW_BUILTIN_REFRESH_TOOLS else None
    if ref.startswith("mcp:"):
        body = ref[len("mcp:"):].strip()
        key, sep, name = body.partition("/")
        if key and sep and name and re.fullmatch(r"[A-Za-z0-9_.-]+", key):
            return "mcp", key, name
    return None


def dev_preview_adapter_refresh(arg):
    """Execute an active adapter's approved refresh reference.

    Generated adapters can reuse existing preview tools or an enabled MCP
    tool. The manifest cannot point to a Python module, shell command, URL,
    or arbitrary callable.
    """
    fields = _parse_keyed(str(arg or ""), ["id", "arguments"])
    adapter_id = (fields.get("id") or "").strip().lower()
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置当前项目"}, ensure_ascii=False)
    if not adapter_id:
        return json.dumps({"ok": False, "error": "需要适配器 id"}, ensure_ascii=False)
    try:
        from agent_runtime.adapter_catalog import generated
        manifests = {str(item.get("id") or "").strip().lower(): item for item in generated(root)}
        manifest = manifests.get(adapter_id)
        if not manifest:
            return json.dumps({"ok": False, "error": "没有找到该项目适配器"}, ensure_ascii=False)
        if manifest.get("status") != "active":
            return json.dumps({"ok": False, "error": "适配器尚未激活，不能刷新"}, ensure_ascii=False)
        reference = _preview_refresh_reference(manifest.get("refresh_tool"))
        if reference is None:
            return json.dumps({
                "ok": False,
                "error": "refresh_tool 不是允许的 builtin:<tool> 或 mcp:<connector>/<tool> 引用",
            }, ensure_ascii=False)
        raw_args = (fields.get("arguments") or "").strip()
        if raw_args:
            try:
                json.loads(raw_args)
            except (TypeError, ValueError):
                return json.dumps({"ok": False, "error": "arguments 必须是 JSON 对象"}, ensure_ascii=False)
        else:
            raw_args = "{}"
        if reference[0] == "builtin":
            name = reference[1]
            spec = TOOLS.get(name)
            if spec is None:
                return json.dumps({"ok": False, "error": "内置刷新工具当前不可用"}, ensure_ascii=False)
            result = spec["func"](raw_args)
            if manifest.get("runtime"):
                from agent_runtime.generated_adapter_runtime import execute
                payload = result.data if isinstance(result, ToolResult) else result
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, ValueError):
                        payload = {"text": payload}
                adapted = execute(root, manifest, payload)
                return _generated_adapter_tool_result(
                    adapter_id, name, adapted["value"], adapted.get("module", ""))
            if isinstance(result, ToolResult):
                return result
            return json.dumps({"ok": True, "adapter": adapter_id, "tool": name,
                               "result": str(result)}, ensure_ascii=False)
        _, key, name = reference
        result = dev_mcp_call("key: %s\nname: %s\narguments: %s" % (key, name, raw_args))
        if manifest.get("runtime"):
            from agent_runtime.generated_adapter_runtime import execute
            payload = result
            try:
                payload = json.loads(str(result))
            except (TypeError, ValueError):
                pass
            adapted = execute(root, manifest, payload)
            return _generated_adapter_tool_result(
                adapter_id, "%s/%s" % (key, name), adapted["value"], adapted.get("module", ""))
        return json.dumps({"ok": not str(result).startswith("MCP 调用失败"),
                           "adapter": adapter_id, "connector": key, "tool": name,
                           "result": result}, ensure_ascii=False)
    except (OSError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def dev_preview_adapter_rollback(arg):
    """回滚当前项目生成适配器代码到上一个已激活版本。"""
    fields = _parse_keyed(str(arg or ""), ["id", "runtime"])
    adapter_id = (fields.get("id") or "").strip().lower()
    root = _get_code_root()
    if not root or not adapter_id:
        return json.dumps({"ok": False, "error": "需要当前项目和适配器 id"}, ensure_ascii=False)
    try:
        from agent_runtime.adapter_catalog import generated
        manifest = next((item for item in generated(root) if item.get("id") == adapter_id), None)
        if not manifest or not manifest.get("runtime"):
            return json.dumps({"ok": False, "error": "没有找到带代码的生成适配器"}, ensure_ascii=False)
        from game_workbench import require_approval
        target = "preview-adapter:%s" % adapter_id
        gate = require_approval(root, "preview_adapter_rollback", target)
        if gate:
            return json.dumps(gate, ensure_ascii=False)
        from agent_runtime.generated_adapter_runtime import rollback
        result = rollback(root, adapter_id, fields.get("runtime") or manifest["runtime"])
        return json.dumps({"ok": True, "adapter": adapter_id, **result}, ensure_ascii=False)
    except (OSError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def dev_preview_adapter_test(arg):
    """用固定 JSON 输入测试已激活适配器，不调用真实软件或 MCP。"""
    fields = _parse_keyed(str(arg or ""), ["id", "payload", "timeout"])
    adapter_id = (fields.get("id") or "").strip().lower()
    root = _get_code_root()
    if not root or not adapter_id:
        return json.dumps({"ok": False, "error": "需要当前项目和适配器 id"}, ensure_ascii=False)
    raw_payload = (fields.get("payload") or "{}").strip()
    try:
        payload = json.loads(raw_payload)
        timeout = max(1, min(30, int(fields.get("timeout") or 12)))
        from agent_runtime.adapter_catalog import generated
        manifest = next((item for item in generated(root)
                         if item.get("id") == adapter_id and item.get("status") == "active"), None)
        if not manifest or not manifest.get("runtime"):
            return json.dumps({"ok": False, "error": "没有找到已激活的代码适配器"}, ensure_ascii=False)
        from agent_runtime.generated_adapter_runtime import execute
        result = execute(root, manifest, payload, timeout=timeout)
        return json.dumps({"ok": True, "adapter": adapter_id, "fixture": True,
                           "result": result["value"], "module": result["module"]}, ensure_ascii=False)
    except json.JSONDecodeError:
        return json.dumps({"ok": False, "error": "payload 必须是 JSON 对象或数组"}, ensure_ascii=False)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False)


def dev_skill_approve(arg):
    """激活一个待审批技能：从 .pending 移到 SKILLS_DIR 并 reload（变为可用）。仅当用户已确认。"""
    name = str(arg or "").strip()
    if name.lower().startswith("name:"):
        name = name[5:].strip()
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:80].strip(".-")
    if not clean:
        return json.dumps({"ok": False, "error": "技能名无效"}, ensure_ascii=False)
    import skills as _skills_mod
    import shutil
    src = os.path.join(_skill_pending_dir(), clean, "SKILL.md")
    if not os.path.exists(src):
        return json.dumps({"ok": False, "error": f"无待审批草稿 {clean}"}, ensure_ascii=False)
    dst_dir = os.path.join(os.path.abspath(_skills_mod.SKILLS_DIR), clean)
    if os.path.exists(dst_dir):
        return json.dumps({"ok": False, "error": f"活动技能 {clean} 已存在"}, ensure_ascii=False)
    os.makedirs(os.path.abspath(_skills_mod.SKILLS_DIR), exist_ok=True)
    shutil.move(src, os.path.join(dst_dir, "SKILL.md"))
    try:
        os.rmdir(os.path.join(_skill_pending_dir(), clean))
    except OSError:
        pass
    _skills_mod.reload()
    return json.dumps({"ok": True, "name": clean, "status": "active",
                       "next": "已激活，可经 dev_use_skill 调用"}, ensure_ascii=False)


def dev_skill_reject(arg):
    """丢弃一个待审批技能草稿（不激活、不保留）。"""
    name = str(arg or "").strip()
    if name.lower().startswith("name:"):
        name = name[5:].strip()
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:80].strip(".-")
    if not clean:
        return json.dumps({"ok": False, "error": "技能名无效"}, ensure_ascii=False)
    import shutil
    tdir = os.path.join(_skill_pending_dir(), clean)
    if not os.path.isdir(tdir):
        return json.dumps({"ok": False, "error": f"无待审批草稿 {clean}"}, ensure_ascii=False)
    shutil.rmtree(tdir)
    return json.dumps({"ok": True, "name": clean, "status": "rejected"}, ensure_ascii=False)


def _dedup_docs(docs, metas):
    """向量检索偶发把同一 chunk 返回多次（同一 source+文本出现 N 遍，曾出现 4 次同片）。

    用 (source, 规范化文本) 作去重键只保留首次出现，避免重复片段浪费 token、
    并防止 Agent 误以为"信息很多"而空转。返回过滤后的 (docs, metas)。"""
    seen = set()
    out_d, out_m = [], []
    for d, m in zip(docs, metas):
        # 键用**展示用** source（已剥 uuid 前缀），不是原始 source。
        # 同一份文档被上传/索引 N 次时，原始 source 带 N 个不同 uuid 前缀，
        # 用原始键去重**全都留下来了**（实测一次检索把同一段文字返回 3 遍）。
        # 这个 docstring 写的意图正是防这个，键选错了意图就没兑现。
        raw_src = (m.get("source", "") if m else "")
        key = (pretty_source(raw_src) or raw_src, (d or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out_d.append(d)
        out_m.append(m)
    return out_d, out_m


def search_knowledge(query):
    """在已上传的知识库中检索与问题相关的文档片段。

    检索失败**必须以可读文本返回，而不是抛异常**：本函数是 Agent 的工具，
    裸异常会变成工具调用错误，让模型看到一段与「知识库里没有相关内容」完全不同
    的东西——于是在没有证据的情况下更倾向于编造，而不是说"没查到"。

    常见失败：向量维度与集合不符（Ollama 掉线后嵌入降级成哈希词袋）。
    """
    try:
        retrieved = get_retriever(collection=get_runtime("knowledge_collection") or COLLECTION_NAME,
                                  top_k=TOP_K, embedding_client=_get_emb()).invoke(query)
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)
        hint = ""
        if "维度" in detail or "dimension" in detail.lower():
            hint = ("向量维度与知识库索引不一致（多半是嵌入后端变了，或它曾不可用后降级）。"
                    "处理办法：启动 Ollama 并拉取嵌入模型后重新上传文档，"
                    "或在「⚙ 模型设置 → 知识库检索后端」里切换后端"
                    "（切换会自动清空旧向量）。")
        return "知识库检索失败：%s%s" % (detail, ("\n" + hint) if hint else "")
    docs = [item.page_content for item in retrieved]
    metas = [dict(item.metadata or {}) for item in retrieved]
    docs, metas = _dedup_docs(docs, metas)
    if not docs:
        return "知识库中未找到相关内容。"
    out = []
    for d, m in zip(docs, metas):
        src = pretty_source(m.get("source", "")) if m else ""
        if not src:
            src = "?"
        # 截断单个 chunk 的长度：避免把大段原文塞给小模型逼它"逐条抄"
        text = d if len(d) <= 500 else d[:500].rstrip() + "…"
        # 来源必须说清这是**知识库文档**，不是项目里的一个文件。
        # 实测（真跑 `wf-37f43a4c042f`）：输出形如 `[MiniMax_H3_提示词速查.md] ...`，
        # 输出里没有任何「这来自知识库」的标记，而文档正文还写着「**本机**节点」「**本地** ComfyUI 版」，
        # 模型据此认定「本项目有 ComfyUI 节点」，随后五步都在 read_file / grep
        # 追 `comfy_extras/nodes_minimax_h3.py`——那个文件根本不在项目里，
        # 而项目真正的 `calc.py` 一眼没看。
        # 知识库是**全局共享**集合（COLLECTION_NAME="docmind"），`projects.py` 只有
        # `code_collection`、没有知识库集合的概念，所以这里不做项目隔离，只把来源讲明白。
        out.append(f"[知识库文档：{src}] {text}")
    return "\n---\n".join(out)


_CALC_BAD = "表达式包含非法字符，仅支持数字、+ - * / % ** //、括号与比较运算 > < >= <= == !=。"
_CALC_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow, ast.FloorDiv,
    ast.USub, ast.UAdd,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Load,
)
# 幂运算防护：** 的指数必须「可静态求出」且是 |值| ≤ _CALC_MAX_POW_EXPONENT 的整数，
# 且结果位数不得超过 _CALC_MAX_RESULT_DIGITS。否则 9**9**9 之类会让 eval 长时间占满
# CPU/内存（实测 >6s 不返回）。前置拒绝，不依赖事后计时。
_CALC_MAX_POW_EXPONENT = 512
_CALC_MAX_RESULT_DIGITS = 4000
# 静态求值中间结果的数值上界（bit_length）：超过即放弃（≈ 1.1e12），
# 保证静态分析本身绝不发生指数爆炸。
_CALC_STATIC_MAX_BITS = 40
_CALC_POW_EXP_BAD = (
    f"不支持的 ** 指数：指数必须是可静态求值且 ≤{_CALC_MAX_POW_EXPONENT} 的整数"
    f"（如 2**10、2**3**2；不支持 9**9**9）。"
)
_CALC_RESULT_TOO_BIG = (
    f"计算结果过大（超过 {_CALC_MAX_RESULT_DIGITS} 位），已拒绝以免卡死。"
)


def _calc_static_value(node, _depth=0):
    """有界静态求值：把 AST 子表达式算成具体数值（int/float）；算不出返回 None。

    仅支持：字面量、一元 +/-、以及 + - * / // % ** 运算。其中 `**` 只在
    「底数与指数都是可静态求出的整数、且各自 |值| ≤ 32」时才递归求值——这样
    9**9 可算出（供 2**3**2 之类的合法指数），而 9**9**9 的指数 9**9**9 在静态
    阶段就会被限流拒绝。任何中间整数结果 bit_length > _CALC_STATIC_MAX_BITS 即
    放弃。用于判断 ** 的指数是否「可静态求出」。
    """
    if _depth > 8:
        return None
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        return v
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _calc_static_value(node.operand, _depth + 1)
        if v is None:
            return None
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        left = _calc_static_value(node.left, _depth + 1)
        right = _calc_static_value(node.right, _depth + 1)
        if left is None or right is None:
            return None
        try:
            if isinstance(node.op, ast.Pow):
                # 仅当两侧都是可静态求出的整数且各自 ≤32 才计算，避开指数爆炸
                if (isinstance(left, int) and not isinstance(left, bool)
                        and isinstance(right, int) and not isinstance(right, bool)
                        and abs(left) <= 32 and abs(right) <= 32):
                    out = left ** right
                else:
                    return None
            elif isinstance(node.op, ast.Add):
                out = left + right
            elif isinstance(node.op, ast.Sub):
                out = left - right
            elif isinstance(node.op, ast.Mult):
                out = left * right
            elif isinstance(node.op, ast.Div):
                out = left / right
            elif isinstance(node.op, ast.FloorDiv):
                out = left // right
            elif isinstance(node.op, ast.Mod):
                out = left % right
            else:
                return None
        except (ZeroDivisionError, ValueError, OverflowError):
            return None
        if isinstance(out, int) and out.bit_length() > _CALC_STATIC_MAX_BITS:
            return None
        return out
    return None


def calculate(expression):
    """对数学表达式求值。

    支持数字、+ - * / % ** //、括号，以及比较运算 > < >= <= == !=
    （比较结果转成「成立/不成立」，方便模型解读为自然语言结论）。
    用 AST 白名单求值，名称/属性/调用等任何非算术节点一律拒绝。

    幂运算加固：** 的指数必须「可静态求出」且为 |值| ≤512（`_CALC_MAX_POW_EXPONENT`）
    的整数，结果位数不得超过 4000（`_CALC_MAX_RESULT_DIGITS`）；超限直接返回中文错误，
    避免 9**9**9 这类表达式长时间占满 CPU。
    """
    expression = (expression or "").strip().strip("'\"").strip()
    if not expression:
        return "未提供表达式。"
    if len(expression) > 200:
        return "表达式过长（上限 200 字符）。"
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return _CALC_BAD
    if any(not isinstance(node, _CALC_ALLOWED_NODES) for node in ast.walk(tree)):
        return _CALC_BAD
    # 前置拒绝：遍历 ** 运算（BinOp 且 op 为 Pow，注意 ast.Pow 是运算符节点、
    # 本身没有 left/right），要求其指数「可静态求出」且是 ≤512 的整数
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exp = _calc_static_value(node.right)
            if (not isinstance(exp, int) or isinstance(exp, bool)
                    or abs(exp) > _CALC_MAX_POW_EXPONENT):
                return _CALC_POW_EXP_BAD
    try:
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})  # noqa: S307
    except Exception as e:  # noqa: BLE001
        return f"计算失败: {e}"
    if isinstance(result, bool):
        return "成立（True）" if result else "不成立（False）"
    # 结果规模兜底：即使指数合规，多重乘法也可能堆出超长整数。
    # str() 在超大整数上会触发 CPython 的 int↔str 转换上限（默认 4300 位）而抛
    # ValueError，这里把该异常也视为「结果过大」，顺带覆盖超过 4300 位的情形。
    if isinstance(result, int):
        try:
            too_big = len(str(result)) > _CALC_MAX_RESULT_DIGITS
        except ValueError:
            too_big = True
        if too_big:
            return _CALC_RESULT_TOO_BIG
    return str(result)


_SEARCH_EMPTY_MARKERS = ("搜索失败", "搜索未返回结果", "搜索结果相关性不足")


_SEARCH_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="docmind-search")


def _search_setting(name, default, low, high):
    try:
        return max(low, min(high, float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return default


def _search_result_limit():
    return int(_search_setting("DOCMIND_WEB_SEARCH_RESULTS", 30, 5, 50))


def _run_search_jobs(jobs):
    """在共享有界线程池并行发出请求，到总体时限就返回已完成结果。"""
    futures = {_SEARCH_POOL.submit(fn, *args): name for name, fn, args in jobs}
    results = {}
    try:
        for future in as_completed(futures, timeout=_search_setting("DOCMIND_WEB_SEARCH_WAIT_S", 5, 0.05, 15)):
            name = futures[future]
            try:
                value = future.result()
                results[name] = value[1] if isinstance(value, tuple) else value
            except Exception as exc:
                results[name] = f"搜索失败: {name}: {type(exc).__name__}"
    except FuturesTimeoutError:
        pass
    pending = [name for name, _fn, _args in jobs if name not in results]
    for future in futures:
        if not future.done():
            future.cancel()
    return results, pending


def _search_recency_query(q: str) -> str:
    """时效性查询偏好近一年结果；可用 env WEB_SEARCH_PREFER_RECENT=0 关闭，
    或查询已含 after:/before:/年份范围时跳过，避免重复拼接。"""
    if re.search(r"今天|今日|明天", q) and not re.search(r"\d{4}[-年/]\d{1,2}", q):
        day = datetime.datetime.now().astimezone().date()
        if "明天" in q:
            day += datetime.timedelta(days=1)
        q = f"{q} {day.isoformat()}"
    if os.getenv("WEB_SEARCH_PREFER_RECENT", "1").strip().lower() in ("0", "false", "no"):
        return q
    if re.search(r"\b(after|before):", q) or re.search(r"\b\d{4}\.\.\d{4}\b", q):
        return q
    if not re.search(r"今天|今日|明天|最新|近期|最近|目前|新闻|\b(?:latest|recent|news)\b", q, re.I):
        return q
    year = datetime.date.today().year - 1
    return f"{q} after:{year}"


# 平台别名 → 站点域名（web_search 的 platform: 参数自动映射，避免手敲 site:）。
_SEARCH_PLATFORMS = {
    "github": "github.com", "gh": "github.com",
    "b站": "bilibili.com", "bilibili": "bilibili.com", "哔哩哔哩": "bilibili.com",
    "微博": "weibo.com", "weibo": "weibo.com",
    "贴吧": "tieba.baidu.com", "tieba": "tieba.baidu.com",
    "百度": "baidu.com", "baidu": "baidu.com",
    "知乎": "zhihu.com", "zhihu": "zhihu.com",
    "小红书": "xiaohongshu.com",
    "xiaohongshu": "xiaohongshu.com", "xhs": "xiaohongshu.com",
    "csdn": "csdn.net",
    "stackoverflow": "stackoverflow.com", "so": "stackoverflow.com",
}


def _parse_web_search_arg(arg):
    """把 web_search 的单字符串入参解析成 (query, site)。

    - 多行 key: value：query:/site:/platform:（platform 自动映射为域名）；
    - 任何位置写 `site:github.com` 或 `platform:github`：自动从 query 剔除该标记；
    - 否则整串作为 query。
    返回 (query, site)，site 为 None 表示不限站。
    """
    text = (arg or "").strip()
    if not text:
        return "", None
    site = [None]

    def _take(key, val):
        val = val.strip().strip("'\"")
        site[0] = val if key == "site" else _SEARCH_PLATFORMS.get(val.lower(), val)

    q_parts = []
    for ln in text.splitlines():
        m = re.match(r"^\s*(site|platform)\s*[:：]\s*(.+?)\s*$", ln, re.I)
        if m:
            _take(m.group(1).lower(), m.group(2))
            continue
        # 行内 site:/platform:（与 query 同行）
        mi = re.search(r"\b(site|platform)\s*[:：]\s*(\S+)", ln, re.I)
        if mi:
            _take(mi.group(1).lower(), mi.group(2))
            ln = re.sub(r"\b(site|platform)\s*[:：]\s*\S+", "", ln, flags=re.I).strip()
        # 去掉本行可能的 query:/q:/keyword: 前缀
        ln = re.sub(r"^(?:query|q|keyword)\s*[:：]\s*", "", ln, flags=re.I).strip()
        if ln:
            q_parts.append(ln)
    q = re.sub(r"\s+", " ", " ".join(q_parts)).strip()
    return q, site[0]


# 自动发现的补充来源。通用搜索引擎负责广度，下面的站点负责不同类型的
# 一手资料、教程、社区口碑和中文经验；它们是候选来源，不代表内容天然可信。
_SEARCH_SOURCE_LABELS = {
    "github.com": "GitHub",
    "bilibili.com": "B 站",
    "zhihu.com": "知乎",
    "tieba.baidu.com": "百度贴吧",
    "xiaohongshu.com": "小红书",
    "weibo.com": "微博",
    "csdn.net": "CSDN",
    "stackoverflow.com": "Stack Overflow",
}

_SEARCH_SOURCE_RULES = (
    ("github.com", ("github", "代码", "源码", "仓库", "开源", "软件库", "api", "sdk", "mcp", "插件", "报错", "bug", "issue", "godot", "unity", "unreal", "easyeda", "cad")),
    ("bilibili.com", ("视频", "教程", "演示", "怎么做", "做法", "教学", "评测", "测评", "游戏", "剪辑", "编程", "旅游", "旅行", "攻略", "景点", "路线", "美食")),
    ("zhihu.com", ("为什么", "经验", "推荐", "评价", "口碑", "哪个好", "怎么选", "避坑", "值得", "原理", "方案", "旅游", "旅行", "攻略", "景点", "路线", "美食")),
    ("tieba.baidu.com", ("贴吧", "玩家", "经验", "求助", "推荐", "评价", "游戏", "旅游", "旅行", "攻略", "景点", "路线", "美食")),
    ("xiaohongshu.com", ("小红书", "种草", "探店", "好吃", "美食", "旅行", "旅游", "攻略", "景点", "路线", "穿搭", "护肤", "购物", "推荐", "避坑", "评价", "口碑")),
    ("weibo.com", ("热搜", "舆论", "事件", "明星", "品牌", "评价")),
    ("csdn.net", ("代码", "报错", "教程", "编程", "python", "java", "前端", "后端", "部署")),
    ("stackoverflow.com", ("error", "exception", "stack trace", "programming", "python", "javascript", "typescript", "sql", "api")),
)


def _infer_search_sources(query):
    """按查询语义挑选补充站点，限制数量避免把一次搜索放大成爬站。"""
    text = str(query or "").strip().lower()
    if not text:
        return []
    picked = []
    for domain, terms in _SEARCH_SOURCE_RULES:
        if any(term.lower() in text for term in terms):
            picked.append(domain)
    # 中文推荐/比较问题通常需要社区口碑；没有明确技术语义时优先这些来源。
    if any(term in text for term in ("推荐", "评价", "口碑", "好吃", "哪个好", "怎么选", "值得买", "避坑", "旅游", "旅行", "攻略", "景点", "路线", "美食")):
        for domain in ("zhihu.com", "xiaohongshu.com", "tieba.baidu.com", "bilibili.com"):
            if domain not in picked:
                picked.append(domain)
    try:
        limit = max(0, min(10, int(os.getenv("WEB_SEARCH_SOURCE_FANOUT", "8"))))
    except (TypeError, ValueError):
        limit = 4
    return picked[:limit]


def _needs_community_sources(query):
    """需要体验/口碑来源的问题，不接受单一 Bing 摘要直接作为答案候选。"""
    return bool(re.search(r"推荐|评价|口碑|好吃|旅游|旅行|攻略|景点|路线|美食|避坑", str(query or ""), re.I))


def _source_query_backend(domain):
    """选择更适合该站点的 HTML 搜索入口，失败仍由通用来源兜底。"""
    if domain in {"github.com", "stackoverflow.com"}:
        return "ddg"
    if domain in {"bilibili.com", "zhihu.com", "tieba.baidu.com", "xiaohongshu.com", "weibo.com", "csdn.net"}:
        return "baidu"
    return "ddg"


def _search_one_source(domain, query):
    """读取一个补充站点；专用 API 失败时退回 site: 搜索。"""
    if domain == "github.com":
        result = _github_search(query)
        if not result.startswith("GitHub API 暂不可用"):
            return "GitHub", result
    elif domain == "bilibili.com":
        result = _bilibili_search(query)
        if not result.startswith("B 站专用搜索暂不可用") and not result.startswith("B 站搜索未返回结果"):
            return "B 站", result
    provider = _source_query_backend(domain)
    backend = {"ddg": _ddg_search, "baidu": _baidu_search}.get(provider, _ddg_search)
    try:
        result = backend(f"{query} site:{domain}")
    except Exception as exc:  # noqa: BLE001
        result = f"搜索失败: {provider}: {type(exc).__name__}: {exc}"
    return _SEARCH_SOURCE_LABELS.get(domain, domain), result


def _search_auxiliary_sources(query, base_result):
    """并行搜索语义相关的社区/代码站点并与通用结果合并。"""
    domains = _infer_search_sources(query)
    if not domains:
        return base_result
    named = [("general", base_result)]
    results, _pending = _run_search_jobs([
        (_SEARCH_SOURCE_LABELS.get(domain, domain), _search_one_source, (domain, query)) for domain in domains])
    named.extend(results.items())
    merged = _merge_builtin_results(query, named)
    if not merged or any(merged.startswith(marker) for marker in _SEARCH_EMPTY_MARKERS):
        return base_result
    return merged


def _json_request(url, *, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "DocMind/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def web_weather(arg):
    """从结构化天气 API 读取当前日期的预报；城市和日期不能凭搜索结果猜测。"""
    try:
        data = json.loads(str(arg or ""))
    except (TypeError, ValueError):
        data = {"city": str(arg or "").strip()}
    if not isinstance(data, dict):
        return "天气查询失败：输入应为城市名或包含 city 的 JSON。"
    city = str(data.get("city") or "").strip()
    if not city or city in {"今天", "今天天气", "天气", "当地", "当前位置"}:
        return "天气查询失败：需要用户明确的城市，不能从搜索结果或 IP 猜测所在地。"
    today = datetime.datetime.now().astimezone().date().isoformat()
    try:
        geo_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
            {"name": city, "count": 5, "language": "zh", "format": "json"})
        locations = (_json_request(geo_url).get("results") or [])
        if not locations:
            return f"天气查询失败：未找到城市 {city}，请补充省份、国家或英文城市名。"
        country = str(data.get("country_code") or "").upper()
        if country:
            locations = [item for item in locations if item.get("country_code") == country]
        # 同名城市不能自动挑第一条。
        if len(locations) != 1:
            options = [{k: item.get(k) for k in ("name", "admin1", "country", "country_code")}
                       for item in locations]
            return "天气查询需要明确地点：" + json.dumps(options, ensure_ascii=False)
        place = locations[0]
        forecast_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": place["latitude"], "longitude": place["longitude"], "timezone": "auto",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "start_date": today, "end_date": today,
        })
        forecast = _json_request(forecast_url)
        daily = forecast.get("daily") or {}
        if daily.get("time") != [today]:
            return "天气查询失败：返回数据的日期与今天不一致，不能当作今日天气。"
        return json.dumps({"city": place.get("name"), "region": place.get("admin1"),
                           "country": place.get("country"), "date": today,
                           "timezone": forecast.get("timezone"), "current": forecast.get("current"),
                           "current_units": forecast.get("current_units"), "daily": daily,
                           "daily_units": forecast.get("daily_units"), "source": forecast_url,
                           "note": "Open-Meteo 模型预报，非气象站实测；current 的时间单独核对。"},
                          ensure_ascii=False)
    except Exception as exc:
        return f"天气查询失败：{type(exc).__name__}，可改用带明确城市及日期的权威天气来源核对。"


def _parse_transport_request(arg):
    """解析交通票价查询参数，统一支持 JSON 和多行 key: value。"""
    text = str(arg or "").strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        value = {}
        for line in text.splitlines():
            match = re.match(r"^\s*(origin|from|出发地|出发)\s*[:：]\s*(.+?)\s*$", line, re.I)
            if match:
                value["origin"] = match.group(2).strip()
                continue
            match = re.match(r"^\s*(destination|to|目的地|到达地|到达)\s*[:：]\s*(.+?)\s*$", line, re.I)
            if match:
                value["destination"] = match.group(2).strip()
                continue
            match = re.match(r"^\s*(date|departure_date|出发日期|日期)\s*[:：]\s*(.+?)\s*$", line, re.I)
            if match:
                value["departure_date"] = match.group(2).strip()
                continue
            match = re.match(r"^\s*(mode|交通方式|方式)\s*[:：]\s*(.+?)\s*$", line, re.I)
            if match:
                value["mode"] = match.group(2).strip()
    return value if isinstance(value, dict) else {}


def _normalize_transport_date(value):
    raw = str(value or "").strip()
    match = re.search(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?", raw)
    if not match:
        return ""
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def web_transport(arg):
    """查询指定日期的交通候选，并严格区分搜索摘要与实时票价。

    12306/航司的余票和实时价格通常需要交互式验证或登录，公开搜索摘要不能
    证明当前价格。因此这个工具只在参数完整后检索带日期的官方/票务来源，返回
    可核对的候选和 ``verified_fare=false``；没有实时票证时绝不输出估算价格。
    """
    request = _parse_transport_request(arg)
    origin = str(request.get("origin") or "").strip()
    destination = str(request.get("destination") or "").strip()
    departure_date = _normalize_transport_date(request.get("departure_date"))
    missing = [key for key, value in (("origin", origin), ("destination", destination),
                                      ("departure_date", departure_date)) if not value]
    if missing:
        return json.dumps({
            "ok": False, "needs": missing,
            "message": "交通票价查询必须提供出发地、目的地和出发日期（YYYY-MM-DD）；不能自行假设日期。",
        }, ensure_ascii=False)
    today = datetime.datetime.now().astimezone().date()
    selected = datetime.date.fromisoformat(departure_date)
    if selected < today:
        return json.dumps({"ok": False, "needs": ["departure_date"],
                           "message": f"出发日期 {departure_date} 已过去，请提供未来日期。"}, ensure_ascii=False)
    mode = str(request.get("mode") or "高铁 飞机").strip()
    queries = [
        f"{origin} 到 {destination} {departure_date} 高铁 车次 票价 12306",
        f"{origin} 到 {destination} {departure_date} 飞机 航班 票价 航空公司",
    ]
    if "高铁" not in mode and "火车" not in mode and "列车" not in mode:
        queries = queries[1:]
    if "飞机" not in mode and "航班" not in mode and len(queries) == 2:
        queries = queries[:1]
    evidence = web_search_batch(json.dumps({"queries": queries}, ensure_ascii=False))
    return json.dumps({
        "ok": True, "origin": origin, "destination": destination,
        "departure_date": departure_date, "mode": mode,
        "verified_fare": False,
        "evidence": evidence,
        "message": "搜索候选不是实时余票或最终支付价。搜索摘要不能当作当前价格；未取得官方实时票证前不得给出估算，请在 12306 或航司官方购票页核对。",
    }, ensure_ascii=False)


def _github_search(q):
    """GitHub 专用仓库搜索；无需 token，遇到 API 限流时明确回退通用搜索。"""
    key = _web_cache_key("github", q)
    if _search_cache_enabled():
        cached = _web_cache_read(key)
        if cached:
            return cached + "\n（缓存结果）"
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode({"q": q, "per_page": 10})
    try:
        data = _json_request(url, headers={"User-Agent": "DocMind/1.0", "Accept": "application/vnd.github+json"})
        rows = data.get("items") or []
        if not rows:
            return "搜索未返回结果，可能是网络受限或该关键词无结果。"
        lines = ["GitHub 专用搜索（仓库 API）："]
        for item in rows[:10]:
            desc = (item.get("description") or "").replace("\n", " ")
            meta = f"★{item.get('stargazers_count', 0)} · 最近更新 {item.get('updated_at', '')[:10]}"
            lines.append(_format_search_result(item.get("full_name") or item.get("name", ""), f"{desc}（{meta}）", item.get("html_url", "")))
        result = "\n".join(lines)
        if _search_cache_enabled():
            _web_cache_write(key, result)
        return result
    except Exception as exc:
        return f"GitHub API 暂不可用（{type(exc).__name__}: {exc}）。可重试通用站点搜索。"


def _bilibili_search(q):
    """B 站专用视频搜索；公共接口被风控时返回可理解的降级信息。"""
    key = _web_cache_key("bilibili", q)
    if _search_cache_enabled():
        cached = _web_cache_read(key)
        if cached:
            return cached + "\n（缓存结果）"
    params = urllib.parse.urlencode({"search_type": "video", "keyword": q, "page": 1, "page_size": 10})
    url = "https://api.bilibili.com/x/web-interface/search/type?" + params
    try:
        data = _json_request(url, headers={"User-Agent": "Mozilla/5.0 (DocMind/1.0)", "Referer": "https://www.bilibili.com/"})
        rows = ((data.get("data") or {}).get("result") or [])
        if not rows:
            return "B 站搜索未返回结果（可能需要验证码或该关键词无结果）。"
        lines = ["B 站专用视频搜索："]
        for item in rows[:10]:
            title = re.sub(r"<[^>]+>", "", item.get("title", ""))
            bvid = item.get("bvid") or ""
            link = "https://www.bilibili.com/video/" + bvid if bvid else item.get("arcurl", "")
            desc = re.sub(r"<[^>]+>", "", item.get("description", "") or "").replace("\n", " ")
            lines.append(_format_search_result(title, desc, link))
        result = "\n".join(lines)
        if _search_cache_enabled():
            _web_cache_write(key, result)
        return result
    except Exception as exc:
        return f"B 站专用搜索暂不可用（{type(exc).__name__}: {exc}）。可改用 platform: b站 的通用搜索。"


def _bilibili_id(url):
    text = str(url or "")
    m = re.search(r"/(BV[0-9A-Za-z]+)", text, re.I) or re.search(r"[?&]bvid=(BV[0-9A-Za-z]+)", text, re.I)
    if m:
        return "bvid", m.group(1)
    m = re.search(r"(?:av|aid=)(\d+)", text, re.I)
    return ("aid", m.group(1)) if m else ("", "")


def web_subtitles(arg):
    """提取公开 B 站视频字幕；登录、UP 主未发布字幕时返回明确原因。"""
    kind, value = _bilibili_id((arg or "").strip())
    if not value:
        return "字幕提取失败：请输入包含 BV 号或 av 号的 B 站视频 URL。"
    try:
        params = {kind: value}
        view = _json_request("https://api.bilibili.com/x/web-interface/view?" + urllib.parse.urlencode(params),
                             headers={"User-Agent": "Mozilla/5.0 (DocMind/1.0)"})
        data = view.get("data") or {}
        if not data:
            return "字幕提取失败：视频不存在、不可见或接口被风控。"
        cid = data.get("cid") or ((data.get("pages") or [{}])[0].get("cid"))
        if not cid:
            return "字幕提取失败：视频没有可读取的分 P。"
        query = urllib.parse.urlencode({"bvid": data.get("bvid", value) if kind == "bvid" else "", "aid": data.get("aid", value) if kind == "aid" else "", "cid": cid})
        player = _json_request("https://api.bilibili.com/x/player/v2?" + query,
                               headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
        entries = (((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or [])
        if not entries:
            return "该视频没有公开字幕，或字幕需要登录后才能读取。"
        sub = entries[0]
        sub_url = sub.get("subtitle_url", "")
        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url
        payload = _json_request(sub_url, headers={"User-Agent": "Mozilla/5.0"})
        body = payload.get("body") or []
        if not body:
            return "字幕接口返回空内容。"
        lines = [f"标题：{data.get('title', '')}", f"来源：https://www.bilibili.com/video/{data.get('bvid', value)}", "字幕："]
        lines.extend(f"[{row.get('from', 0):.1f}s] {row.get('content', '').strip()}" for row in body[:600] if row.get("content"))
        return "\n".join(lines)
    except Exception as exc:
        return f"字幕提取失败：{type(exc).__name__}: {exc}"


def web_search(query, _allow_aux=True):
    """联网搜索（按设置选择服务商，无需 Key 的内置后端默认可用）。

    入参为单字符串，可带站点限定：
    - `query: 关键词` 或直接写关键词；
    - 追加 `site: github.com` 或 `platform: github`（自动映射为 site:github.com），
      把结果收敛到指定站（GitHub / 哔哩哔哩 / 微博 / 百度贴吧 等）。

    服务商（设置中可切换）：auto（多来源同时检索、限时汇总）/ ddg / bing / baidu /
    exa / tavily / searxng / bocha / firecrawl / zhipu / querit / parallel / mcp_exa。
    API 类服务商需先在设置填 API Key（或自建实例地址）。自动模式最多返回 30 条候选。
    """
    q, site = _parse_web_search_arg(query)
    if not q:
        return "未提供搜索关键词。"
    # 对 GitHub / B 站提供专用结构化入口；失败时仍可用通用 HTML 搜索。
    _in_unit_test = getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", None) == "unittest.__main__"
    specialized_error = ""
    if site == "github.com" and not _in_unit_test:
        result = _github_search(q)
        if not result.startswith("GitHub API 暂不可用"):
            return _filter_search_results(q, result)
        # API 被限流时继续给出普通站点结果，不把一次 API 失败当成整个搜索失败。
        specialized_error = result
    if site == "bilibili.com" and not _in_unit_test:
        result = _bilibili_search(q)
        if not result.startswith("B 站专用搜索暂不可用"):
            return _filter_search_results(q, result)
        # B 站接口被验证码拦截时走 DDG/百度/Bing 的 site: 回退。
        specialized_error = result
    if site:
        q = f"{q} site:{site}"
    q = _search_recency_query(q)
    provider = get_web_search_provider()
    # 来源扇出和排序策略变更后必须换缓存命名空间，避免旧的 Bing 单源摘要继续生效。
    cache_key = _web_cache_key(f"search:v6:{provider}:{_allow_aux}:{_search_result_limit()}", q)
    if _search_cache_enabled():
        cached = _web_cache_read(cache_key)
        if cached:
            cached_result = cached if provider == "builtin_auto" else _filter_search_results(q, cached)
            return cached_result + "\n（缓存结果）"
    if provider in ("builtin_auto", "ddg", "bing", "baidu"):
        result = _builtin_search(provider, q, include_aux=(not site and _allow_aux))
    else:
        result = _api_web_search(provider, q)
    if specialized_error and not any(result.startswith(m) for m in _SEARCH_EMPTY_MARKERS):
        result = specialized_error + "\n\n通用搜索回退结果：\n" + result
    if provider != "builtin_auto":
        result = _filter_search_results(q, result)
    if _search_cache_enabled() and not any(result.startswith(m) for m in _SEARCH_EMPTY_MARKERS):
        _web_cache_write(cache_key, result)
    return result


def _parse_search_batch(arg):
    """解析 web_search_batch 的 JSON 或逐行查询输入。"""
    text = str(arg or "").strip()
    if not text:
        return [], []
    payload = None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        raw_queries = payload.get("queries") or payload.get("query") or []
        excludes = payload.get("exclude") or payload.get("exclude_urls") or []
        if isinstance(raw_queries, str):
            raw_queries = [raw_queries]
        if isinstance(excludes, str):
            excludes = [excludes]
    elif isinstance(payload, list):
        raw_queries, excludes = payload, []
    else:
        raw_queries = []
        excludes = []
        for line in text.splitlines():
            line = re.sub(r"^\s*(?:query|q|queries?)\s*[:：]\s*", "", line, flags=re.I).strip()
            if line:
                raw_queries.append(line)
    queries = list(dict.fromkeys(str(q).strip() for q in raw_queries if str(q).strip()))[:8]
    excludes = list(dict.fromkeys(str(v).strip().lower() for v in excludes if str(v).strip()))[:100]
    return queries, excludes


def web_search_batch(arg):
    """并行执行一组搜索，跨查询按 URL/标题去重并支持排除已看过的结果。

    输入 JSON：{"queries":["候选菜 评价","候选菜 做法"],"exclude":["已看过的 URL"]}；
    也支持每行一个查询。适合发现候选后并行查评价、教程、做法，再把结果交回
    Agent 决定是否继续下一轮。
    """
    queries, excludes = _parse_search_batch(arg)
    if not queries:
        return "未提供批量搜索关键词。"
    outputs = [None] * len(queries)
    with ThreadPoolExecutor(max_workers=min(8, len(queries)), thread_name_prefix="docmind-query") as pool:
        futures = {pool.submit(web_search, query, False): index for index, query in enumerate(queries)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                outputs[index] = future.result()
            except Exception as exc:  # noqa: BLE001
                outputs[index] = f"搜索失败：{type(exc).__name__}: {exc}"
    unique, diagnostics = {}, []
    for query, raw in zip(queries, outputs):
        if not raw:
            continue
        rows = _formatted_search_rows(raw)
        if not rows:
            diagnostics.append(f"{query}：{raw}")
            continue
        for row in rows:
            if not _query_relevant(query, row.get("title", "") + " " + row.get("snippet", ""),
                                   row.get("title", ""), row.get("url", "")):
                continue
            haystack = (row.get("url", "") + " " + row.get("title", "") + " " + row.get("snippet", "")).lower()
            if any(item in haystack for item in excludes):
                continue
            key = (row.get("url") or row.get("title") or "").strip().lower()
            if not key:
                continue
            score, reason = _source_score(row.get("url", ""), row.get("title", ""), row.get("snippet", ""))
            terms = _search_terms(query)
            matched = sum(term in (row.get("title", "") + " " + row.get("snippet", "")).lower() for term in terms)
            rank = matched / max(1, len(terms)) * 3 + score * 0.15
            candidate = {**row, "query": query, "rank": rank, "reason": reason}
            old = unique.get(key)
            if old is None or candidate["rank"] > old["rank"]:
                unique[key] = candidate
    if not unique:
        if diagnostics:
            return "\n".join(diagnostics)
        return "搜索结果相关性不足：批量候选均未达到查询主题匹配阈值。请改写查询或拆分主题。"
    lines = [f"批量搜索结果（{len(queries)} 个查询并行，已跨查询去重）："]
    for row in sorted(unique.values(), key=lambda item: item["rank"], reverse=True)[:_search_result_limit()]:
        snippet = row.get("snippet", "")[:120]
        query_hint = row.get("query", "")
        if query_hint:
            snippet = f"{snippet}（查询：{query_hint}）".strip()
        lines.append(_format_search_result(row.get("title", ""), snippet,
                                           row.get("url", ""), source="batch"))
    return "\n".join(lines)


def _builtin_search(provider, q, include_aux=False):
    """内置无 Key 后端。

    自动模式会并行取多个引擎的候选，再按主题匹配度、标题命中和来源质量
    去重排序；单一 provider 仍保持原来的单后端行为。
    """
    backends = {"ddg": _ddg_search, "bing": _bing_search, "baidu": _baidu_search}
    order = [provider] if provider in backends else ["ddg", "baidu", "bing"]
    if provider not in backends:
        try:
                fanout = max(1, min(len(order), int(os.getenv("WEB_SEARCH_FANOUT", "6"))))
        except (TypeError, ValueError):
            fanout = 3
        selected = order[:fanout]
        jobs = [(name, backends[name], (q,)) for name in selected]
        if include_aux:
            domains = _infer_search_sources(q)
            jobs.extend((_SEARCH_SOURCE_LABELS.get(domain, domain), _search_one_source, (domain, q))
                        for domain in domains)
        results, pending = _run_search_jobs(jobs)
        selected = [name for name, _fn, _args in jobs]
        # 只有一个后端真正返回候选时保留原始格式，兼容旧调用方；多个后端
        # 都有结果时才输出聚合头、去重和统一可信度字段。
        meaningful = [raw for raw in (results.get(name, "") for name in selected)
                      if _formatted_search_rows(raw)]
        meaningful_names = [name for name in selected if _formatted_search_rows(results.get(name, ""))]
        if _needs_community_sources(q) and meaningful_names == ["bing"]:
            return ("搜索结果相关性不足：当前只拿到 Bing 单一通用摘要，"
                    "未获得知乎/小红书/B 站/贴吧等体验来源；已停止把它直接当作推荐依据。"
                    "请换搜索服务商或稍后重试。")
        if len(meaningful) == 1 and len(_search_terms(q)) < 2:
            result = meaningful[0]
        else:
            result = _merge_builtin_results(q, [(name, results.get(name, "")) for name in selected])
        if result and not any(result.startswith(marker) for marker in _SEARCH_EMPTY_MARKERS):
            if len(meaningful) == 1 and len(_formatted_search_rows(meaningful[0])) == 1:
                result = meaningful[0]
            if pending:
                result += "\n（限时返回已有候选；未完成来源：" + "、".join(pending) + "；未读取候选正文。）"
            return result
        # 同一轮不再串行重试已请求过的后端，避免全失败时多等几十秒。
        if result.startswith("搜索结果相关性不足"):
            if pending:
                result += "\n未完成来源：" + "、".join(pending) + "。"
            return result
        failure_details = [f"{name}：{results[name]}" for name in selected
                           if name in results and not _formatted_search_rows(results[name])]
        if failure_details:
            all_unreachable = (not pending and len(failure_details) == len(selected) and all(
                str(results[name]).startswith("搜索失败") for name in selected
            ))
            summary = ("搜索失败：所有搜索来源均不可达或超时。来源诊断：" if all_unreachable else
                       "搜索失败：没有来源返回可用候选（可能是空结果、网络故障或超时）。来源诊断：")
            return (summary
                    + "；".join(failure_details[:6])
                    + ("；未完成来源：" + "、".join(pending) if pending else ""))
        last = next((results[name] for name in reversed(selected) if name in results), "搜索失败：检索超时，尚无候选。")
        if last.startswith("搜索失败"):
            return last + "（所选后端均不可达或超时，可换关键词或配置搜索 API）"
        return last
    last = ""
    for backend in order:
        try:
            r = backends[backend](q)
        except Exception as e:  # noqa: BLE001
            last = f"搜索失败: {backend}: {type(e).__name__}: {e}"
            continue
        if not any(r.startswith(m) for m in _SEARCH_EMPTY_MARKERS):
            return r
        last = r
    hard_error = last.startswith("搜索失败: ")
    return (last or "搜索失败：所有搜索后端均不可用。") + (
        "" if (not hard_error and last.startswith(_SEARCH_EMPTY_MARKERS)) else
        "（DuckDuckGo/百度/Bing 均不可达，请确认运行环境能访问外网，或配置带 Key 的搜索引擎）")


def _formatted_search_rows(text):
    """解析内置搜索器的统一三行格式，兼容标题/摘要缺失的结果。"""
    rows, current = [], None
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("· "):
            if current:
                rows.append(current)
            current = {"title": line[2:].strip(), "snippet": "", "url": ""}
            continue
        if current is None or not line or line.startswith("可信度参考"):
            continue
        match = re.search(r"https?://\S+", line)
        if match and not current["url"]:
            current["url"] = match.group(0).rstrip(".,;）)")
        elif not current["snippet"]:
            current["snippet"] = line
    if current:
        rows.append(current)
    return [row for row in rows if row.get("title") or row.get("url")]


def _search_terms(query):
    terms = []
    clean_query = re.sub(r"\b(?:site|after|before):\S+", " ", str(query or ""), flags=re.I)
    for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", clean_query.lower()):
        if len(word) > 2 and re.fullmatch(r"[\u4e00-\u9fff]+", word):
            terms.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            terms.append(word)
    return [term for term in dict.fromkeys(terms)
            if term not in {"official", "docs", "documentation", "推荐", "今天", "如何", "怎么", "最新"}]


def _query_relevant(query, text, title="", url=""):
    """Conservative lexical relevance gate shared by search, research, and batch paths."""
    terms = _search_terms(query)
    if len(terms) <= 1:
        return True
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "").lower())
    matched = sum(term in normalized for term in terms)
    if re.search(r"[\u4e00-\u9fff]", query):
        required = 1 if len(terms) <= 4 else max(2, min(3, (len(terms) * 3 + 9) // 10))
        if matched >= required:
            content_match = True
        else:
            query_text = re.sub(r"\b(?:site|after|before):\S+", " ", str(query or ""), flags=re.I).lower()
            phrases = re.findall(r"[\u4e00-\u9fff]{3,}", query_text)
            content_match = any(phrase[i:i + 3] in normalized
                                for phrase in phrases for i in range(len(phrase) - 2))
    else:
        required = max(2, (len(terms) * 3 + 9) // 10)
        content_match = matched >= required
    if content_match:
        return True
    # 标题/URL 是**另一条通过路径**，不是「摘要命中之后还要再过一关」。
    #
    # 原来是这么写的：摘要命中之后，**还要求**标题或 URL 路径也命中
    # `anchor_required`（3 词时需 2 个），否则 return False。而真实搜索结果的标题
    # 常常又短又通用（「维基百科」「官方公告」「GitHub - xxx」），URL 的拉丁 slug
    # 又对不上中文查询词——于是这一关几乎必然不通过。
    #
    # 实测（`.tmp/probe_candidate_drop.py`）：query=「深度求索 训练成本」+ 6 个**真实
    # 形态**的候选，原写法存活 **0/6**（5 个完全切题的也被丢）；只按内容判存活 5/6。
    # 英文查询同样中招（`deepseek training cost` + `Wikipedia` 也被丢）。
    #
    # 代价是连锁的：候选被**静默丢弃**（三处 `continue`，模型根本看不到）-> 全被丢
    # -> 工具返回「搜索未返回结果」-> 那条在 `_FAILURE_MARKERS` 里 -> 计入失败额度
    # -> 熔断。也就是说 agent 看到的「无匹配」有一部分是**这里制造出来的**。
    if not (title or url):
        return False
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        path = ""
    anchor_text = re.sub(r"[^\w\u4e00-\u9fff]+", "",
                         (str(title) + " " + path).lower())
    anchor_matches = sum(term in anchor_text for term in terms)
    anchor_required = 1 if len(terms) <= 2 else max(2, min(3, (len(terms) + 3) // 4))
    return anchor_matches >= anchor_required


def _search_excerpt(query, snippet, limit=180):
    text = re.sub(r"\s+", " ", str(snippet or "")).strip()
    if len(text) <= limit:
        return text
    terms = _search_terms(query)
    positions = [text.lower().find(term.lower()) for term in terms]
    positions = [position for position in positions if position >= 0]
    if positions:
        start = max(0, min(positions) - 32)
        if max(positions) >= start + limit:
            start = max(0, max(positions) - limit + 32)
    else:
        start = 0
    excerpt = text[start:start + limit]
    return ("…" if start else "") + excerpt + ("…" if start + limit < len(text) else "")


def _search_source_status(raw):
    value = str(raw or "").strip()
    if value.startswith("搜索失败"):
        return "请求失败"
    if value.startswith(("搜索未返回结果", "B 站搜索未返回结果")):
        return "无候选"
    if value.startswith("搜索结果相关性不足"):
        return "低相关"
    return "未返回结构化候选"


def _filter_search_results(query, raw):
    """Filter formatted candidates consistently, preserving a compact rejection diagnosis."""
    rows = _formatted_search_rows(raw)
    if not rows:
        return raw
    kept = [row for row in rows
            if _query_relevant(query, row.get("title", "") + " " + row.get("snippet", ""),
                               row.get("title", ""), row.get("url", ""))]
    if not kept:
        examples = "；".join(row.get("title", "")[:80] for row in rows[:3])
        return (f"搜索结果相关性不足：{len(rows)} 条候选均未达到查询主题匹配阈值。"
                f"候选标题：{examples or '（无标题）'}。请改写查询或更换来源。")
    if len(kept) == len(rows):
        return raw
    lines_raw = str(raw).splitlines()
    first_result = next((i for i, line in enumerate(lines_raw)
                         if line.lstrip().startswith("· ")), len(lines_raw))
    header = next((line.strip() for line in lines_raw[:first_result] if line.strip()), "")
    lines = [header] if header else []
    lines.extend(_format_search_result(row.get("title", ""), row.get("snippet", ""),
                                       row.get("url", "")) for row in kept)
    if len(kept) < len(rows):
        lines.append(f"相关性筛选：保留 {len(kept)}/{len(rows)} 条候选，过滤 {len(rows) - len(kept)} 条。")
    return "\n".join(lines)


def _merge_builtin_results(query, named_results):
    """合并自动模式候选，去重并把真正相关的结果排在前面。"""
    terms = _search_terms(query)
    unique = {}
    diagnostics = []
    for backend, raw in named_results:
        if not raw:
            continue
        if any(str(raw).startswith(marker) for marker in _SEARCH_EMPTY_MARKERS):
            diagnostics.append(str(raw))
            continue
        rows = _formatted_search_rows(raw)
        if not rows:
            diagnostics.append(str(raw))
            continue
        for row in rows:
            key = (row.get("url") or row.get("title") or "").strip().lower()
            if not key:
                continue
            haystack = (row.get("title", "") + " " + row.get("snippet", "")).lower()
            matched = sum(1 for term in terms if term in haystack)
            score, reason = _source_score(row.get("url", ""), row.get("title", ""), row.get("snippet", ""))
            # 相关性优先，域名/‘官方’标签只能小幅辅助，不能把跑题资料顶到前面。
            rank = (matched / max(1, len(terms))) * 3 + score * 0.15
            rank += sum(term in row.get("title", "").lower() for term in terms) * 0.03
            # 中文旅行/推荐/口碑问题中，通用 Bing 摘要经常只有泛化攻略标题；
            # 社区原站更适合承载体验和避坑信息。仅在这类意图下调整排序，
            # 技术文档和代码查询仍按主题命中优先。
            if re.search(r"推荐|评价|口碑|好吃|旅游|旅行|攻略|景点|路线|美食|避坑", str(query), re.I):
                if str(backend).lower() == "bing":
                    rank -= 0.12
                elif str(backend) in {"知乎", "小红书", "B 站", "百度贴吧"}:
                    rank += 0.08
            candidate = {**row, "backend": backend, "rank": rank, "reason": reason, "matched": matched}
            old = unique.get(key)
            if old is None or candidate["rank"] > old["rank"]:
                unique[key] = candidate
    if not unique:
        return diagnostics[0] if diagnostics else ""
    ranked = sorted(unique.values(), key=lambda row: row["rank"], reverse=True)
    candidate_count = len(ranked)
    ranked = [row for row in ranked
              if _query_relevant(query, row.get("title", "") + " " + row.get("snippet", ""),
                                 row.get("title", ""), row.get("url", ""))]
    if not ranked:
        source_names = "、".join(dict.fromkeys(row.get("backend", "") for row in unique.values()))
        source_statuses = [f"{name}={_search_source_status(raw)}"
                           for name, raw in named_results if not _formatted_search_rows(raw)]
        status_detail = "；来源状态：" + "、".join(source_statuses) if source_statuses else ""
        return (f"搜索结果相关性不足：{candidate_count} 条候选均未达到查询主题匹配阈值。"
                f"候选来源：{source_names or '未知'}{status_detail}；不得据此给出结论。")
    rows = ranked[:_search_result_limit()]
    sources = "、".join(name for name, raw in named_results if _formatted_search_rows(raw))
    lines = [f"聚合搜索结果（{len(rows)} 条候选；来源：{sources}；已去重排序，尚未核对正文）："]
    for row in rows:
        snippet = _search_excerpt(query, row.get("snippet", ""))
        source = row.get("backend", "")
        if source:
            snippet = (snippet + "（来源：%s）" % source).strip()
        lines.append(_format_search_result(row.get("title", ""), snippet,
                                           row.get("url", ""), source=source or "web"))
    return "\n".join(lines)


def _api_web_search(provider, q):
    """API 类搜索服务商（需 Key 或自建地址），失败给出清晰原因或回退内置。"""
    key = get_web_search_api_key()
    url = get_web_search_api_url()
    try:
        if provider == "exa":
            return _exa_search(q, key)
        if provider == "tavily":
            return _tavily_search(q, key)
        if provider == "searxng":
            return _searxng_search(q, url)
        if provider == "bocha":
            return _bocha_search(q, key)
        if provider == "firecrawl":
            return _firecrawl_search(q, key)
        # zhipu / querit / parallel / mcp_exa：通用 keyed POST，需自定义 API 地址
        if url:
            return _generic_api_search(provider, q, key, url)
        return ("该搜索服务商需要先在「网络搜索」设置里填写 API 地址或 API Key 才能使用；"
                "请填写后保存，或把服务商切回「自动（内置）」。")
    except Exception as e:  # noqa: BLE001
        return f"搜索失败（{provider}）：{type(e).__name__}: {e}"


def _exa_search(q, key):
    if not key:
        return "Exa 需要 API Key，请在「网络搜索」设置中填写后保存。"
    api_url = "https://api.exa.ai/search"
    payload = {"query": q, "numResults": 20}
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = data.get("results") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = []
    for it in results[:_search_result_limit()]:
        text = (it.get("text") or "").strip().replace("\n", " ")
        lines.append(f"· {it.get('title', '')}\n  {text[:200]}\n  {it.get('url', '')}")
    return "\n".join(lines)


def _tavily_search(q, key):
    if not key:
        return "Tavily 需要 API Key，请在「网络搜索」设置中填写后保存。"
    api_url = "https://api.tavily.com/search"
    payload = {"api_key": key, "query": q, "max_results": 20, "search_depth": "basic"}
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = data.get("results") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [f"· {it.get('title', '')}\n  {(it.get('content', '') or '')[:200]}\n  {it.get('url', '')}"
             for it in results[:_search_result_limit()]]
    return "\n".join(lines)


def _searxng_search(q, url):
    if not url:
        return "SearXNG 需要填写自建实例地址（API 地址），请在「网络搜索」设置中填写后保存。"
    api_url = f"{url.rstrip('/')}/search?q={urllib.parse.quote(q)}&format=json"
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0 (DocMind/1.0)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = data.get("results") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [f"· {it.get('title', '')}\n  {(it.get('content', '') or '')[:200]}\n  {it.get('url', '')}"
             for it in results[:_search_result_limit()]]
    return "\n".join(lines)


def _bocha_search(q, key):
    if not key:
        return "Bocha 需要 API Key，请在「网络搜索」设置中填写后保存。"
    api_url = "https://api.bochaai.com/openapi/v1/web-search"
    payload = {"query": q, "count": 30, "freshness": "noLimit"}
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = ((data.get("data") or {}).get("webPages") or {}).get("value") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [f"· {it.get('name', '')}\n  {(it.get('snippet', '') or '')[:200]}\n  {it.get('url', '')}"
             for it in results[:_search_result_limit()]]
    return "\n".join(lines)


def _firecrawl_search(q, key):
    if not key:
        return "Firecrawl 需要 API Key，请在「网络搜索」设置中填写后保存。"
    api_url = "https://api.firecrawl.dev/v1/search"
    payload = {"query": q, "limit": 30, "pageOptions": {"fetchPageContent": False}}
    req = urllib.request.Request(
        api_url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = data.get("data") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [f"· {it.get('title', '')}\n  {(it.get('description', '') or '')[:200]}\n  {it.get('url', '')}"
             for it in results[:_search_result_limit()]]
    return "\n".join(lines)


def _generic_api_search(provider, q, key, url):
    """zhipu/querit/parallel/mcp_exa 等未内置端点的服务商：通用 keyed POST。"""
    payload = {"query": q, "q": q, "numResults": 20, "max_results": 20, "count": 20}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (DocMind/1.0)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    results = data.get("results") or data.get("data") or []
    if isinstance(results, dict):
        results = results.get("results") or results.get("value") or []
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = []
    for it in results[:_search_result_limit()]:
        if isinstance(it, dict):
            lines.append(f"· {it.get('title', it.get('name', ''))}\n"
                         f"  {(it.get('snippet', it.get('content', it.get('description', '')) or '')[:200])}\n"
                         f"  {it.get('url', it.get('link', ''))}")
        else:
            lines.append(f"· {it}")
    return "\n".join(lines)


def _ddg_search(q):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; DocMind/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        page = r.read().decode("utf-8", "replace")

    def _clean(s):
        s = re.sub(r"<[^>]+>", "", s or "")
        return html_lib.unescape(s).strip()

    anchors = re.findall(r'<a\b([^>]*\bclass=["\'][^"\']*result__a[^"\']*["\'][^>]*)>(.*?)</a>', page, re.S)
    titles, links = [], []
    for attrs, title in anchors:
        href = re.search(r'\bhref=["\']([^"\']+)["\']', attrs, re.I)
        if href:
            titles.append(title)
            links.append(html_lib.unescape(href.group(1)))
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    if not titles:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"

    lines = []
    for i in range(min(10, len(titles))):
        t = _clean(titles[i])
        s = _clean(snippets[i]) if i < len(snippets) else ""
        link = links[i] if i < len(links) else ""
        # DDG 的跳转链接可能带 uddg= 编码，尽量还原
        if "uddg=" in link:
            try:
                link = urllib.parse.unquote(link.split("uddg=", 1)[1].split("&", 1)[0])
            except Exception:
                pass
        lines.append(_format_search_result(t, s, link))
    return "\n".join(lines)


def _bing_real_url(link):
    """把 Bing 的 ck/a 跳转链接还原成真实目标 URL；非跳转链接原样返回。

    形如 https://www.bing.com/ck/a?...&u=a1aHR0cHM6...&ntb=1，u 参数去掉
    前缀 a1 后是 URL 的 base64（urlsafe，常缺填充）。
    """
    if not link or "bing.com/ck/a" not in link:
        return link
    m = re.search(r"[?&]u=a1([A-Za-z0-9_\-]+)", link)
    if not m:
        return link
    import base64 as _b64
    raw = m.group(1)
    raw += "=" * (-len(raw) % 4)
    try:
        return _b64.urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return link


def _bing_search(q):
    """Bing HTML 搜索（无需 Key）。用于 DuckDuckGo 被屏蔽的网络环境。"""
    url = "https://www.bing.com/search?q=" + urllib.parse.quote(q)
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; DocMind/1.0)"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        page = r.read().decode("utf-8", "replace")

    def _clean(s):
        s = re.sub(r"<[^>]+>", "", s or "")
        return html_lib.unescape(s).strip()

    # 每个结果都在 <li class="b_algo"> 块内：
    #   标题 = <h2 ...><a ... href="URL">文本</a></h2>（注意 h2 可能带 class 属性）
    #   摘要 = <p class="b_lineclampN ...">文本</p>
    blocks = re.split(r'<li class="b_algo"', page)[1:]
    results = []
    for blk in blocks:
        hm = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>\s*</h2>',
            blk, re.S,
        )
        if not hm:
            continue
        link = _bing_real_url(html_lib.unescape(hm.group(1)))
        title = _clean(hm.group(2))
        sm = re.search(r'<p class="b_lineclamp[^"]*"[^>]*>(.*?)</p>', blk, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        results.append((title, snippet, link))
        if len(results) >= 10:
            break
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [_format_search_result(t, s, link) for (t, s, link) in results]
    return "\n".join(lines)


def _baidu_search(q):
    """百度 HTML 搜索（无需 Key）。国内网络通常比 Bing 更稳，作为 auto 兜底层之一。"""
    url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(q)
    req = urllib.request.Request(
        url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        page = r.read().decode("utf-8", "replace")

    def _clean(s):
        s = re.sub(r"<[^>]+>", "", s or "")
        return html_lib.unescape(s).strip()

    # 每个自然结果在 <div class="result ..."> 块内：标题 <h3 ...><a href="URL">文本</a></h3>，
    # 摘要 <div class="c-abstract ...">文本</div>。百度偶把跳转塞进链接参数，原样保留即可。
    blocks = re.split(r'<div class="result', page)[1:]
    results = []
    for blk in blocks:
        hm = re.search(
            r'<h3[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', blk, re.S
        )
        if not hm:
            continue
        link = hm.group(1)
        title = _clean(hm.group(2))
        sm = re.search(r'<div class="c-abstract[^"]*"[^>]*>(.*?)</div>', blk, re.S)
        if not sm:
            sm = re.search(r'class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>', blk, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        results.append((title, snippet, link))
        if len(results) >= 10:
            break
    if not results:
        return "搜索未返回结果，可能是网络受限或该关键词无结果。"
    lines = [_format_search_result(t, s, link) for (t, s, link) in results]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# 网页图片观察通道（仅 builtin 抓 HTML 时启用）
#
# 图片只是给视觉模型的【观察素材】，不是代码事实；下载与回传受四重约束：
#   1) DOCMIND_WEB_IMAGES 开关（默认 1）且当前会话模型具备看图途径才下载；
#   2) 仅 http/https，阻断内网/回环/链路本地地址（防 <img src=内网> SSRF）；
#   3) Content-Type 白名单 + Pillow 真实图头校验 + ≤800KB + 8s 超时；
#   4) web_fetch 单次 ≤2 张，web_research 跨源合计 ≤4，URL 去重。
# 搜索摘要缓存仍只存文本，任何缓存命中分支都不带图。
# ---------------------------------------------------------------------------
_WEB_FETCH_IMAGE_LIMIT = 2
_WEB_RESEARCH_IMAGE_LIMIT = 4
_WEB_IMAGE_MAX_BYTES = 800 * 1024
_WEB_IMAGE_TIMEOUT = 8.0
_WEB_IMAGE_CONTENT_TYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp")
_WEB_IMAGE_UA = "Mozilla/5.0 (DocMind research; image-observation)"
_IMG_ICON_WORDS = ("icon", "logo", "sprite", "avatar", "badge", "button", "1x1",
                   "spacer", "pixel", "favicon", "thumb", "placeholder", "loading",
                   "banner-ad", "advert", "ads/", "/ads", "tracker", "emoji")
_IMG_EXT_RE = re.compile(r"\.(?:png|jpe?g|webp)(?:[?#]|$)", re.I)
_TAG_IMG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_ATTR_RE_TMPL = r"""{name}\s*=\s*["']([^"']*)["']"""


def _web_images_enabled():
    """开关 + 当前模型看图能力（能力判定必须复用 config 画像/vision 层配置）。"""
    if os.getenv("DOCMIND_WEB_IMAGES", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    # 本轮会话模型优先（云端按请求 llm 覆盖时全局画像不代表当前模型）。
    session_mode = _session_vision_mode.get()
    if session_mode is not None:
        return session_mode == "native" or bool(
            (os.getenv("DOCMIND_VISION_MODEL") or "").strip())
    try:
        from llm import LLM_PROVIDER, LLM_MODEL
        from config import model_capability
        provider = get_runtime("llm_provider") or LLM_PROVIDER
        model = get_runtime("llm_model") or LLM_MODEL
        vision_mode = model_capability(provider, model).get("vision")
        return vision_mode == "native" or bool((os.getenv("DOCMIND_VISION_MODEL") or "").strip())
    except Exception:
        return False


def _ip_is_internal(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _image_host_blocked(host):
    """图片目标 host 的 SSRF 守卫：回环/内网/链路本地/解析失败一律拒绝。

    域名解析到的任一地址落在内网即拒绝（防 DNS 混地址 rebinding）。
    """
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except OSError:
            return True
        addrs = {info[4][0] for info in infos}
        return any(_ip_is_internal(addr) for addr in addrs) or not addrs
    else:
        return _ip_is_internal(host)


def _img_attr(tag, name):
    m = re.search(_ATTR_RE_TMPL.format(name=name), tag, re.I)
    return m.group(1).strip() if m else ""


def _query_terms(hint):
    """英文按词、中文按二元字组切查询词，用于 alt/文件名相关性打分。"""
    hint = (hint or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9+\-_.]{1,}", hint))
    cjk = re.findall(r"[\u4e00-\u9fff]+", hint)
    for run in cjk:
        for i in range(len(run) - 1):
            terms.add(run[i:i + 2])
        if len(run) == 1:
            terms.add(run)
    return {t for t in terms if len(t) >= 2 or re.match(r"[\u4e00-\u9fff]", t)}


def _looks_like_icon(url, alt):
    blob = ((url or "") + " " + (alt or "")).lower()
    return any(w in blob for w in _IMG_ICON_WORDS)


def _extract_og_image(html):
    for pattern in (
        r'<meta\b[^>]*property\s*=\s*["\']og:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
        r'<meta\b[^>]*content\s*=\s*["\']([^"\']+)["\'][^>]*property\s*=\s*["\']og:image["\']',
        r'<meta\b[^>]*name\s*=\s*["\']twitter:image["\'][^>]*content\s*=\s*["\']([^"\']+)["\']',
        r'<meta\b[^>]*content\s*=\s*["\']([^"\']+)["\'][^>]*name\s*=\s*["\']twitter:image["\']',
    ):
        m = re.search(pattern, html, re.I | re.S)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _extract_image_candidates(html, base_url, query_hint="", limit=2):
    """从 HTML 选取正文相关图片绝对 URL，按相关度排序（og:image 优先）。

    选取规则：og:image/twitter:image 头图优先并加分；正文 img 需有图片扩展名，
    过滤 data-uri、图标/广告/占位词、显式小尺寸；alt/title/文件名与查询词重合
    越多越靠前，同站加分；同 URL 去重。任何解析异常返回空列表。
    """
    if not html:
        return []
    terms = _query_terms(query_hint)
    base_host = (urllib.parse.urlparse(base_url).hostname or "").lower()
    candidates = []  # (score, order, url)
    seen = set()

    def _consider(raw, order, og=False):
        if not raw or raw.strip().lower().startswith("data:"):
            return
        abs_url = urllib.parse.urljoin(base_url, raw.strip())
        ok, _ = _url_scheme_ok(abs_url)
        if not ok or abs_url in seen:
            return
        path = urllib.parse.urlparse(abs_url).path.lower()
        if not og and not _IMG_EXT_RE.search(path):
            return
        if _looks_like_icon(abs_url, ""):
            return
        seen.add(abs_url)
        score = 2.0 if og else 0.0
        host = (urllib.parse.urlparse(abs_url).hostname or "").lower()
        if base_host and (host == base_host or host.endswith("." + base_host)):
            score += 0.5
        candidates.append((score, order, abs_url))

    _consider(_extract_og_image(html), 0, og=True)
    order = 1
    for tag in _TAG_IMG_RE.findall(html):
        src = _img_attr(tag, "src") or _img_attr(tag, "data-src")
        if not src:
            continue
        alt = _img_attr(tag, "alt")
        title = _img_attr(tag, "title")
        if _looks_like_icon(src, alt + " " + title):
            order += 1
            continue
        # 显式小尺寸（图标/sprite）直接丢；缺尺寸的不拦。
        dims = []
        for attr in ("width", "height"):
            val = _img_attr(tag, attr)
            if val.isdigit():
                dims.append(int(val))
        if dims and max(dims) < 64:
            order += 1
            continue
        before = len(candidates)
        _consider(src, order, og=False)
        if len(candidates) > before and terms:
            path = urllib.parse.urlparse(candidates[-1][2]).path.lower()
            overlap = sum(1 for t in terms
                          if t in (alt + " " + title).lower()
                          or t in urllib.parse.unquote(path))
            score, _order, url = candidates[-1]
            candidates[-1] = (score + overlap, _order, url)
        order += 1
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [url for _s, _o, url in candidates[:limit]]


def _encode_observation_image(data):
    """Pillow 校验真实图头并缩放到最长边 1600、JPEG q82，返回 data URL 或 None。"""
    try:
        from PIL import Image, UnidentifiedImageError
        try:
            img = Image.open(io.BytesIO(data))
            img.verify()
        except (UnidentifiedImageError, OSError, ValueError):
            return None
        img = Image.open(io.BytesIO(data))
        if img.format not in ("PNG", "JPEG", "WEBP"):
            return None
        img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        if img.mode == "L":
            img = img.convert("RGB")
        img.thumbnail((1600, 1600), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/jpeg;base64," + encoded
    except Exception:
        return None


class _ImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向每一跳都重新过 SSRF 守卫，禁止跨 scheme（防公网 302 到内网/file://）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in ("http", "https") or _image_host_blocked(parsed.hostname or ""):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# 显式组装 opener：build_opener 默认还带 File/FTP/Data handler，
# 图片下载通道不应有任何读本地文件/其它协议的能力（纵深防御，即使入口与
# 逐跳 scheme 白名单已经挡住 file/ftp，也不把这些处理器装进链里）。
_IMAGE_OPENER = urllib.request.OpenerDirector()
for _handler in (
    urllib.request.UnknownHandler(),
    _ImageRedirectHandler(),
    urllib.request.HTTPHandler(),
    urllib.request.HTTPSHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.HTTPErrorProcessor(),
):
    _IMAGE_OPENER.add_handler(_handler)
del _handler


def _download_observation_image(url, base_url=""):
    """下载并校验单张观察图片，成功返回 data URL；任何失败返回 None。"""
    abs_url = urllib.parse.urljoin(base_url or "", url)
    ok, _ = _url_scheme_ok(abs_url)
    if not ok:
        return None
    parsed = urllib.parse.urlparse(abs_url)
    if _image_host_blocked(parsed.hostname or ""):
        return None
    try:
        req = urllib.request.Request(abs_url, headers={"User-Agent": _WEB_IMAGE_UA})
        with _IMAGE_OPENER.open(req, timeout=_WEB_IMAGE_TIMEOUT) as resp:
            final = urllib.parse.urlparse(resp.geturl())
            # 服务器也可能不经过 302 而由代理层改写终点，终态再校验一次。
            if final.scheme not in ("http", "https") or _image_host_blocked(final.hostname or ""):
                return None
            content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in _WEB_IMAGE_CONTENT_TYPES:
                return None
            data = resp.read(_WEB_IMAGE_MAX_BYTES + 1)
        if not data or len(data) > _WEB_IMAGE_MAX_BYTES:
            return None
        encoded = _encode_observation_image(data)
        if not encoded:
            return None
        # 重编码 JPEG 必须仍是有界体积，防止原图绕过下载侧字节上限。
        if len(encoded) * 3 // 4 > _WEB_IMAGE_MAX_BYTES:
            return None
        return encoded
    except Exception:
        return None


def _collect_observation_images(html, base_url, query_hint, limit):
    """抽取候选 → 下载 → 去重，返回 (images, sources) 两个等长列表。"""
    images, sources = [], []
    candidates = _extract_image_candidates(html, base_url, query_hint=query_hint, limit=limit)
    for cand in candidates:
        if len(images) >= limit:
            break
        encoded = _download_observation_image(cand, base_url)
        if not encoded or encoded in images:
            continue
        images.append(encoded)
        sources.append(cand)
    return images, sources


def web_fetch(url):
    """读取公开网页正文的简化研究工具，返回标题、来源和清理后的文本。

    服务商（设置中可切换）：builtin（直接 urllib 抓 HTML 并清理）/ jina / firecrawl / custom。
    jina 与 firecrawl 需 API Key；custom 需填 API 地址（POST {url: ...} 取 markdown/text）。
    """
    ok, _ = _url_scheme_ok(url)
    if not ok: return "网页读取失败：只允许 http/https。"
    provider = get_web_fetch_provider()
    if provider == "builtin":
        return _fetch_page(url, image_budget=_WEB_FETCH_IMAGE_LIMIT)
    key = get_web_fetch_api_key()
    api_url = get_web_fetch_api_url()
    if provider == "jina":
        return _jina_fetch(url, key, api_url)
    if provider == "firecrawl":
        return _firecrawl_fetch(url, key, api_url)
    if provider == "custom":
        return _custom_fetch(url, key, api_url)
    return _fetch_page(url, image_budget=_WEB_FETCH_IMAGE_LIMIT)


def _jina_fetch(url, key, api_url):
    base = (api_url.rstrip("/") + "/") if api_url else "https://r.jina.ai/"
    target = base + url
    headers = {"User-Agent": "Mozilla/5.0 (DocMind research)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read(2_000_000).decode("utf-8", "replace")
        raw = raw[:8000]
        return f"来源：{url}\n正文：\n{raw}" + ("\n[正文已截断]" if len(raw) >= 8000 else "")
    except Exception as e:
        return f"网页读取失败：{type(e).__name__}: {e}"


def _firecrawl_fetch(url, key, api_url):
    if not key:
        return "Firecrawl 需要 API Key，请在「URL 获取」设置中填写后保存。"
    endpoint = (api_url.rstrip("/") if api_url else "https://api.firecrawl.dev") + "/v1/scrape"
    payload = {"url": url, "formats": ["markdown"]}
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        md = ((data.get("data") or {}).get("markdown") or "").strip()
        if not md:
            return "网页读取失败：Firecrawl 未返回正文。"
        md = md[:8000]
        return f"来源：{url}\n正文：\n{md}" + ("\n[正文已截断]" if len(md) >= 8000 else "")
    except Exception as e:
        return f"网页读取失败：{type(e).__name__}: {e}"


def _custom_fetch(url, key, api_url):
    if not api_url:
        return "自定义 URL 获取需要填写 API 地址，请在「URL 获取」设置中填写后保存。"
    payload = {"url": url}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (DocMind research)"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(api_url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read(2_000_000).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            md = data.get("markdown") or data.get("text") or data.get("content") or raw
        except Exception:
            md = raw
        md = md[:8000]
        return f"来源：{url}\n正文：\n{md}" + ("\n[正文已截断]" if len(md) >= 8000 else "")
    except Exception as e:
        return f"网页读取失败：{type(e).__name__}: {e}"


class _ReadableHTMLParser(HTMLParser):
    """Extract visible page text, preferring article/main regions over page chrome."""
    _SKIP_TAGS = {"head", "script", "style", "noscript", "svg", "nav", "footer", "aside",
                  "form", "button", "select", "textarea", "template"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                  "meta", "param", "source", "track", "wbr"}
    _BLOCK_TAGS = {"address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "header", "h1",
                   "h2", "h3", "h4", "h5", "h6", "li", "main", "ol", "p", "pre", "section",
                   "table", "td", "th", "tr", "ul"}
    _NOISE_RE = re.compile(
        r"(?:^|[\s_-])(?:navigation|navbar|header|footer|sidebar|menu|breadcrumb|cookie|consent|"
        r"advert|ads|promo|related|comments|social|share)(?:$|[\s_-])", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.body_parts = []
        self.primary_parts = []

    def _in_primary(self):
        return any(item[2] for item in self.stack)

    @staticmethod
    def _append(parts, text):
        if text:
            parts.append(text)

    def handle_starttag(self, tag, attrs):
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        marker = " ".join((attributes.get("id", ""), attributes.get("class", ""),
                           attributes.get("role", ""), attributes.get("aria-label", "")))
        inherited_skip = any(item[1] for item in self.stack)
        inside_primary = self._in_primary()
        skip = (inherited_skip or tag in self._SKIP_TAGS
                or (tag == "header" and not inside_primary)
                or attributes.get("role", "").lower() in {
                    "navigation", "complementary", "contentinfo", "search"}
                or bool(self._NOISE_RE.search(marker)))
        primary = not skip and (tag in {"main", "article"} or attributes.get("role", "").lower() == "main")
        if tag not in self._VOID_TAGS:
            self.stack.append((tag, skip, primary))
        if tag in self._BLOCK_TAGS:
            self._append(self.body_parts, "\n")
            if not skip and self._in_primary():
                self._append(self.primary_parts, "\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in self._BLOCK_TAGS:
            self._append(self.body_parts, "\n")
            if self._in_primary():
                self._append(self.primary_parts, "\n")

    def handle_data(self, data):
        if any(item[1] for item in self.stack):
            return
        self._append(self.body_parts, data)
        if self._in_primary():
            self._append(self.primary_parts, data)

    @staticmethod
    def _normalize(parts):
        lines = [re.sub(r"\s+", " ", line).strip()
                 for line in "".join(parts).splitlines()]
        return "\n".join(line for line in lines if line)

    def text(self):
        primary = self._normalize(self.primary_parts)
        return primary if len(primary) >= 120 else self._normalize(self.body_parts)


def _extract_readable_text(raw):
    parser = _ReadableHTMLParser()
    parser.feed(raw or "")
    parser.close()
    return parser.text()


def _fetch_page(url, *, image_budget=0, query_hint=""):
    """原 urllib 直抓 HTML 并清理的实现（builtin 服务商）。

    image_budget>0 且当前会话具备看图能力时，从同一份 HTML 抽取并下载相关图片，
    经 ToolResult.data 回传给 Agent 的统一视觉能力门；无图时返回纯字符串，
    保持与旧调用方的兼容。
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DocMind research)"})
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read(1_000_000).decode('utf-8','replace')
            final_url = r.geturl() or url
            content_type = r.headers.get('Content-Type', '')
        if 'html' not in content_type.lower() and '<html' not in raw[:500].lower():
            return f"来源：{final_url}\n内容类型：{content_type or '未知'}\n网页正文读取器仅支持 HTML 页面。"
        title = re.search(r'<title[^>]*>(.*?)</title>', raw, re.I|re.S)
        text = _extract_readable_text(raw)
        clean_title = html_lib.unescape(re.sub(r'<[^>]+>', '', title.group(1))).strip() if title else '未知'
        clipped = len(text) > 8000
        body = f"来源：{final_url}\n标题：{clean_title}\n正文：{text[:8000]}" + ("\n[正文已截断]" if clipped else "")
        if image_budget and _web_images_enabled():
            images, sources = _collect_observation_images(
                raw, final_url, query_hint or clean_title, image_budget)
            if images:
                return ToolResult(ok=True, text=body,
                                  data={"images": images, "image_sources": sources})
        return body
    except Exception as e: return f"网页读取失败：{type(e).__name__}: {e}"


def _builtin_fetch(url):
    """builtin 抓取的兼容入口（不带图片）；新代码请用 _fetch_page。"""
    return _fetch_page(url)

def web_research(query):
    """搜索并抓取多个公开来源，供 Agent 直接做联网研究。

    默认读取 5 个候选正文，可用 DOCMIND_WEB_RESEARCH_MAX_SOURCES 调整（1~8）。
    多轮不同查询由 Agent 决定，避免单次搜索样本不足就贸然下结论。
    """
    results = _filter_search_results(query, as_text(web_search(query)))
    urls = re.findall(r'https?://[^\s)]+', results)
    if not urls:
        return results
    out = [f"研究主题：{query}", "搜索摘要：", results, "\n来源正文："]
    seen = set()
    # 图片只在 builtin（本地能看到 HTML）时聚合；jina/firecrawl/custom 返回纯文本。
    collect_images = _web_images_enabled() and get_web_fetch_provider() == "builtin"
    images, image_sources = [], []
    try:
        max_sources = max(1, min(8, int(os.getenv("DOCMIND_WEB_RESEARCH_MAX_SOURCES", "5"))))
    except (TypeError, ValueError):
        max_sources = 5
    for url in urls[:max_sources]:
        url = url.rstrip('.,')
        if url in seen: continue
        seen.add(url)
        if collect_images:
            remaining = _WEB_RESEARCH_IMAGE_LIMIT - len(images)
            # 预算耗尽后必须走无图分支，避免 web_fetch 自己再抓 2 张突破总上限。
            page = (_fetch_page(url, image_budget=remaining, query_hint=query)
                    if remaining > 0 else _fetch_page(url))
            if isinstance(page, ToolResult):
                for img, src in zip(page.data.get("images") or [],
                                    page.data.get("image_sources") or []):
                    if img not in images and src not in image_sources:
                        images.append(img)
                        image_sources.append(src)
                page = page.text
            page = as_text(page)  # 与下方统一口径：确保进 join 的一定是 str
        else:
            # web_fetch 可能返回 ToolResult（图片分支未启用时）：先规整，避免 join 抛 TypeError
            page = as_text(web_fetch(url))
        out.append(page)
    conflict = _detect_source_conflicts(out[3:])
    if conflict:
        out.append("\n冲突提示（自动抽取，仅供复核）：\n" + conflict)
    text = "\n---\n".join(out)
    if images:
        return ToolResult(ok=True, text=text,
                          data={"images": images, "image_sources": image_sources})
    return text


def _eda_gate_schema_keys():
    """从注册表里 `eda_report_gate` 的 `input_schema.properties` 取字段名。

    单一真相：键名单跟着 schema 走，不在这里另抄一份——下一个人加/改字段只动 schema。
    spec 可能是裸 dict，也可能是 `upgrade_registry` 转出来的 `ToolSpec`，两种都读；
    取不到就回空表（调用方据此放弃兜底、退回旧的"只认 JSON"行为）。
    """
    try:
        meta = TOOLS.get("eda_report_gate")
        schema = (meta.get("input_schema") if isinstance(meta, dict)
                  else getattr(meta, "input_schema", None)) or {}
        props = schema.get("properties") or {}
        return [k for k in props if isinstance(k, str)]
    except Exception:  # noqa: BLE001 —— 取 schema 失败不该拖垮入参解析
        return []


def _coerce_eda_gate_value(key, raw):
    """把 `_parse_keyed` 取出的字符串按 schema 类型还原；只动结构化字段，其余原样返回。

    原生通道 `args_to_input` 用 `f"{v}"` 扁平化，dict/list/bool 会变成 Python repr
    （`{'verdict': 'pass'}` / `['timing_met']` / `True`），故先试 JSON 再试 `literal_eval`。
    解析不出类型的结构化字段一律返回 None（丢弃 → 上层按缺证据处理，fail-closed，绝不猜）。
    """
    s = (raw or "").strip()
    if key == "vlm_verified":
        low = s.lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
        return None
    if key in ("visual", "critical", "bbox_norm"):
        for loader in (json.loads, ast.literal_eval):
            try:
                v = loader(s)
            except (ValueError, SyntaxError, TypeError):
                continue
            if key == "visual" and isinstance(v, dict):
                return v
            if key in ("critical", "bbox_norm") and isinstance(v, list):
                return v
        if key == "critical":  # 兜底：逗号分隔的判定量名
            parts = [p.strip().strip("\"'") for p in s.split(",") if p.strip()]
            return parts or None
        return None
    return raw


def _parse_eda_report_gate_request(arg):
    """把工具入参规整成 dict：JSON 优先，失败时兜底解析 `key: value` 多行。

    A1 修复：`eda_report_gate` 要的是多字段 JSON，但模型 ReAct 首拍常把它写成换行
    `key: value`（YAML 风格），原生 function-calling 通道经 `llm.args_to_input` 也会把
    多字段 dict 扁平成同一形状（`tool: vivado\nkind: timing\nvisual: {...}`）。此前只认
    JSON，于是这两种**合理**写法一律被打回 `{}` → `needs[tool,kind]` → 首拍必坏、逼模型
    第二轮自纠。这里复用本仓库既有的通用 keyed 解析器 `_parse_keyed` 兜底，字段名从 schema
    取、结构化字段按类型还原。**仍 fail-closed**：兜底后认不出 tool/kind 就返回 `{}` 交给
    上层 needs 提示，绝不猜。JSON 路径行为一字未改（只在 json.loads 失败后才走兜底）。
    """
    if isinstance(arg, dict):
        return arg
    text = str(arg or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except (TypeError, ValueError):
        pass
    # ---- JSON 失败 → 兜底解析 `key: value` 多行（模型 YAML 风格 / 原生通道扁平化）----
    keys = _eda_gate_schema_keys()
    if not keys:
        return {}
    try:
        fields = _parse_keyed(text, keys)
    except Exception:  # noqa: BLE001 —— 兜底解析异常也退回 {}（旧行为），不抛
        return {}
    out = {k: _coerce_eda_gate_value(k, fields[k]) for k in keys if k in fields}
    # 只在认出了 tool/kind 之一时才接管；否则维持 {}（旧行为：交给 needs 提示，fail-closed）。
    if not out.get("tool") and not out.get("kind"):
        return {}
    return out


def _eda_report_gate_summary(r):
    """把 gate_online_report 的结果压成给模型看的紧凑观察：只留判定与依据，不塞原始正文。"""
    fact = r.get("fact")
    gate = r.get("gate") or {}
    merged = gate.get("merged") or {}
    decision = gate.get("decision") or {}
    state = merged.get("state")
    out = {
        "ok": bool(r.get("ok")),
        "source": r.get("source"),
        "url": r.get("url"),
        "fetch_failed": bool(r.get("fetch_failed")),
        "visual_source": r.get("visual_source"),
        "tool": r.get("tool"),
        "kind": r.get("kind"),
        "structured": None if not fact else {
            "fact_ok": fact.get("ok"),
            "metrics": fact.get("metrics"),
            "missing": fact.get("missing"),
            "provenance_count": len(fact.get("provenance") or []),
            "error": fact.get("error"),
        },
        "gate": {
            "structured_state": gate.get("structured_state"),
            "visual_state": gate.get("visual_state"),
            "state": state,
            "decision": decision.get("action"),
            "reason": decision.get("reason"),
        },
        "executes_rollback": False,
    }
    if state == "met" and decision.get("action") == "accept":
        out["message"] = ("结构化事实 + 视觉双绿 → met/accept。数值均带 provenance，"
                          "可回溯原始报告行；结论以真实报告为准。")
    elif state == "unmet":
        out["message"] = ("有信号明确否决 → unmet：建议回滚，但本工具不执行回滚，"
                          "须经带人工审批门的动作接口。")
    else:
        out["message"] = ("证据不足或未核实 → uncertain：升级人工/下一段核实，绝不静默放行。"
                          "在线正文不是可信结构化事实、或取页失败、或视觉结论 verified:false "
                          "时一律按不确定处理，不得据此当作『通过』。")
    return out


def eda_report_gate(arg):
    """抓取在线 EDA 报告/文档页 → M3 结构化解析 →（可选 M4/M5 视觉回归）→ M6 双信号合并门（**只判不执行回滚**）。

    输入为 JSON：`{"tool":"vivado","kind":"timing","url":"...","content":"...","fetch":"auto",
    "visual":{"verdict":"..."},"golden_key":"blkA","view":"layout","bbox_norm":[..],"capture":"auto",
    "vlm_verified":bool,"critical":[...]}`。
    - 取页源可切换、其余零改动：给 `content` 或 `fetch="inline"` 用调用方（例如通用浏览器）
      已抓回的正文；否则用 `web_fetch`(urllib) 抓 `url`。
    - 解析器按 (tool,kind) 选：vivado 的 timing/utilization、klayout 的 drc、verdi 的 simulation。
    - 视觉通道双路径、同样可插拔：直接透传已算好的 `visual` verdict；或给 `golden_key` + ROI
      （`view`/`bbox_norm`）+ 可选 `capture`(auto/gdi/x11/vnc)，内部复用 M4 取像 + M5 ROI golden
      diff **现算** verdict，喂进同一个 M6 门。没配视觉入参 / 取不到帧 / 没 golden → 视觉 uncertain。
    全程 fail-closed：取不到正文 → no_report_text；取到但非本类报告 → M3 判 fact_ok=false；
    两者都由 M6 归一为 uncertain → escalate，**绝不谎报通过**。只有 met 才 accept；
    unmet 的 rollback 只是建议信号，本工具从不执行任何 revert、也不存 golden。视觉/在线正文/
    未核实结论 verified:false 起步。
    """
    req = _parse_eda_report_gate_request(arg)
    tool = str(req.get("tool") or "").strip()
    kind = str(req.get("kind") or "").strip()
    url = str(req.get("url") or "").strip()
    content = req.get("content")
    has_content = bool(content and str(content).strip())

    if not tool or not kind:
        return json.dumps({
            "ok": False, "needs": ["tool", "kind"],
            "message": "必须指定 tool(vivado/klayout/verdi) 与 kind(timing/utilization/drc/simulation)。",
        }, ensure_ascii=False)
    if not url and not has_content:
        return json.dumps({
            "ok": False, "needs": ["url 或 content"],
            "message": "要么给在线报告页 url，要么给已抓回的正文 content（浏览器抓回走这条）。",
        }, ensure_ascii=False)

    try:
        from agent_runtime.eda_web_report import gate_online_report
    except Exception as exc:  # 链路不可用也如实回，不抛（抛了会被当异常吞成笼统失败）
        return json.dumps({
            "ok": False, "error": "chain_unavailable",
            "message": "EDA 报告门链路不可用：%s" % type(exc).__name__,
        }, ensure_ascii=False)

    fetch = str(req.get("fetch") or ("inline" if has_content else "auto")).strip().lower()
    visual = req.get("visual") if isinstance(req.get("visual"), dict) else None
    vlm_verified = req.get("vlm_verified") if isinstance(req.get("vlm_verified"), bool) else None
    critical = req.get("critical") if isinstance(req.get("critical"), list) else None

    # ---- 可选视觉回归通道：给了 golden_key + ROI 就复用路由的 make_frame_source 现算 M5 diff ----
    # fail-closed：没配代码库根 / 建源失败 → 不塞视觉入参，gate 按"视觉缺证据→uncertain"处理，绝不假绿。
    extra: dict = {}
    golden_key = str(req.get("golden_key") or "").strip()
    view = req.get("view")
    bbox_norm = req.get("bbox_norm") if isinstance(req.get("bbox_norm"), list) else None
    if golden_key and (view or bbox_norm):
        root = _get_code_root()
        if root:
            try:
                from api_routes.eda import make_frame_source, VisualRegressionReq
                vsr = VisualRegressionReq(
                    golden_key=golden_key, view=view, bbox_norm=bbox_norm,
                    capture=str(req.get("capture") or "auto"),
                    source=str(req.get("source") or "foreground"),
                    hwnd=req.get("hwnd"), window_id=req.get("window_id"),
                    vnc_host=req.get("vnc_host"), vnc_port=req.get("vnc_port"),
                    out_size=int(req.get("out_size") or 256))
                extra = {
                    "frame_source": make_frame_source(vsr),
                    "golden_root": os.path.join(root, ".docmind", "eda_golden"),
                    "golden_key": golden_key,
                    "view": view,
                    "bbox_norm": bbox_norm,
                    "out_size": int(req.get("out_size") or 256),
                    "ssim_threshold": float(req.get("ssim_threshold") or 0.98),
                    "delta_threshold": float(req.get("delta_threshold") or 8.0),
                    "grid": int(req.get("grid") or 8),
                }
            except Exception:
                extra = {}  # 建源失败：退回单通道，视觉 uncertain，不 500、不假绿

    result = gate_online_report(
        tool=tool, kind=kind, url=(url or None), content=(content if has_content else None),
        fetch=fetch, visual=visual, vlm_verified=vlm_verified, critical=critical, **extra)
    return json.dumps(_eda_report_gate_summary(result), ensure_ascii=False)


def _detect_source_conflicts(chunks):
    """标记不同来源对带单位数字的明显分歧，不替用户裁决事实。"""
    values = {}
    rx = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(GB|MB|KB|Hz|秒|分钟|帧|%|个|层)(?!\w)", re.I)
    for chunk in chunks:
        seen = set()
        for val, unit in rx.findall(chunk or ""):
            key = unit.lower()
            item = (val, unit)
            if item not in seen:
                values.setdefault(key, set()).add(val)
                seen.add(item)
    rows = [f"单位 {unit} 出现多个数值：{', '.join(sorted(vals, key=lambda x: float(x)))}；请打开来源核对上下文。"
            for unit, vals in values.items() if len(vals) > 1]
    return "\n".join(rows)


_ALLOWED_URL_SCHEMES = ("http", "https")


def _url_scheme_ok(url):
    """只允许 http/https：白名单配 * 时也要阻止 file:// 读本地文件、gopher/ftp 等协议。"""
    try:
        scheme = (urllib.parse.urlparse(url).scheme or "").lower()
    except Exception:
        return False, ""
    return scheme in _ALLOWED_URL_SCHEMES, scheme


def _host_allowed(url):
    """按 EXTERNAL_API_ALLOWLIST 校验 url 的 host（防 SSRF）。空白名单一律拒绝。

    支持三种写法（与文档一致）：
      *                          放行任意 host（scheme 仍限 http/https）
      api.example.com            精确匹配，同时匹配其子域
      *.example.com              仅匹配子域（含多级，如 a.b.example.com），不含裸 example.com
    """
    if not EXTERNAL_API_ALLOWLIST:
        return False, "未配置 EXTERNAL_API_ALLOWLIST，dev_http_request 已禁用（请在启动环境设置白名单，如 EXTERNAL_API_ALLOWLIST=api.example.com,*.example.com）。"
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return False, f"无法解析 URL：{url}"
    host = host.lower()
    for raw in EXTERNAL_API_ALLOWLIST:
        pat = (raw or "").strip().lower()
        if not pat:
            continue
        if pat == "*":
            return True, ""
        if pat.startswith("*."):
            suffix = pat[1:]  # ".example.com"
            if host.endswith(suffix) and len(host) > len(suffix):
                return True, ""
            continue
        if host == pat or host.endswith("." + pat):
            return True, ""
    return False, f"目标 host「{host}」不在 EXTERNAL_API_ALLOWLIST 白名单内，已拒绝（当前白名单：{', '.join(EXTERNAL_API_ALLOWLIST)}）。"


class _GuardRedirectHandler(urllib.request.HTTPRedirectHandler):
    """30x 跳转时对每一跳重新做 scheme + host 白名单校验。

    urllib 默认自动跟随重定向且不重新校验目标，白名单域若存在开放重定向，
    一跳即可访问 127.0.0.1 / 169.254.169.254 / 内网甚至跳到 file://，
    把响应内容回显给 Agent，等于绕过 SSRF 白名单。
    """

    # 301/303/307 在基类里都是 http_error_302 的别名；子类里重新绑定，确保全部走守卫
    def http_error_302(self, req, fp, code, msg, hdrs):
        # 注意：Python 3.13 起基类在调用 redirect_request 之前就会自行拒绝非 http(s)
        # 跳转（抛 HTTPError），这里提前校验是为了拿到统一、可读的拦截原因，
        # 并保证所有 3xx 状态码都先过本方法。
        loc = hdrs.get("location") or hdrs.get("uri") or ""
        newurl = urllib.parse.urljoin(req.full_url, loc)
        scheme_ok, _ = _url_scheme_ok(newurl)
        if not scheme_ok:
            raise urllib.error.URLError(f"重定向目标协议不允许（仅 http/https）：{newurl}")
        allowed, why = _host_allowed(newurl)
        if not allowed:
            raise urllib.error.URLError(f"重定向目标未通过白名单：{why}")
        return super().http_error_302(req, fp, code, msg, hdrs)

    http_error_301 = http_error_303 = http_error_307 = http_error_302

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        # 纵深防御：即便未来 Python 版本改变 302 处理链，这里仍逐跳复核
        scheme_ok, _ = _url_scheme_ok(newurl)
        if not scheme_ok:
            raise urllib.error.URLError(f"重定向目标协议不允许（仅 http/https）：{newurl}")
        allowed, why = _host_allowed(newurl)
        if not allowed:
            raise urllib.error.URLError(f"重定向目标未通过白名单：{why}")
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)


def dev_http_request(arg):
    """让 Agent 调用你自己的外部业务 API（REST/JSON）。

    受 EXTERNAL_API_ALLOWLIST 域名白名单约束（防止对内网/元数据地址做 SSRF），
    未配置白名单时本工具拒绝任何请求。输入（多行 key: value）：
        url: <必填，完整 URL>
        method: <GET|POST|PUT|PATCH|DELETE，默认 GET>
        timeout: <秒，默认 15>
        headers: <可选，单行 JSON 对象，如 {"Authorization":"Bearer x"}>
        body: <可选，请求体；与 method 配合，POST/PUT/PATCH 常用；可多行>
    返回：HTTP 状态码 + 响应头(部分) + 截断后的响应体（前 4000 字）。网络/解析错误会说明原因。
    """
    arg = (arg or "").lstrip("\n")
    keys = ["url", "method", "timeout", "headers", "body"]
    spans = []
    for key in keys:
        for m in re.finditer(r"^\s*" + key + r"\s*:\s*", arg, re.M):
            spans.append((m.start(), m.end(), key))
    spans.sort()
    fields = {}
    for i, (s, e, key) in enumerate(spans):
        val_end = spans[i + 1][0] if i + 1 < len(spans) else len(arg)
        val = arg[e:val_end]
        if val.startswith("\n"):
            val = val[1:]
        if val_end < len(arg):
            val = val.rstrip("\n")
        fields[key] = val

    url = (fields.get("url") or "").strip()
    if not url:
        return "参数缺失：请提供 url: <完整 URL>。"
    scheme_ok, scheme = _url_scheme_ok(url)
    if not scheme_ok:
        return f"安全拦截：仅允许 http/https 协议，拒绝「{scheme or '未知'}」。"
    ok, why = _host_allowed(url)
    if not ok:
        return f"安全拦截：{why}"

    method = (fields.get("method") or "GET").strip().upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return f"不支持的 HTTP 方法：{method}。"
    try:
        timeout = float((fields.get("timeout") or "15").strip())
    except ValueError:
        return "timeout 参数不是合法数字（秒）。"
    timeout = max(1.0, min(timeout, 60.0))

    headers = {}
    raw_h = (fields.get("headers") or "").strip()
    if raw_h:
        try:
            headers = json.loads(raw_h)
            if not isinstance(headers, dict):
                return "headers 必须是 JSON 对象（如 {\"Authorization\":\"Bearer x\"}）。"
        except Exception as e:  # noqa: BLE001
            return f"headers 解析失败（需单行 JSON 对象）：{e}"

    body = fields.get("body")
    data = None
    if body is not None and body != "" and method in ("POST", "PUT", "PATCH"):
        data = body.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    try:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        # 使用带跳转守卫的 opener：302 每一跳都重过 scheme + host 白名单
        opener = urllib.request.build_opener(_GuardRedirectHandler)
        with opener.open(req, timeout=timeout) as r:
            status = r.status
            resp_body = r.read().decode("utf-8", "replace")
            ctype = r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        try:
            resp_body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            resp_body = ""
        return f"HTTP {e.code} {e.reason}\n{resp_body[:4000]}"
    except Exception as e:  # noqa: BLE001
        return f"请求失败: {type(e).__name__}: {e}"

    preview = resp_body[:4000]
    tail = "" if len(resp_body) <= 4000 else f"\n…（截断，共 {len(resp_body)} 字）"
    return f"HTTP {status}  content-type: {ctype}\n{preview}{tail}"


# ---------------------------------------------------------------------------
# python_exec 文件系统牢笼（PEP-578 审计钩）——堵「绕过 dev_* 审批轨直写磁盘」的洞
#
# 背景（钻测 AA 实据）：python_exec 把代码交给 `python -c` 子进程跑。企业容器路径
# 本就把写丢进一次性副本（--read-only，天然隔离），但**无 Docker 的 Windows 默认
# 降级到全权限宿主解释器**（run_bounded_host_command），子进程里的
# `open(path,'w')` / `os.remove` 可以任意越出 code_root、覆盖用户真实文件、读取凭据。
# dev_* 写工具那一整套护栏（写意图门 + `_resolve_in_root` 路径牢 + `_hard_floor_reason`
# 凭据地板 + edit-confirm 暂存）对子进程**完全够不着**。
#
# 为什么不在 agent 循环里把 python_exec 纳入写意图门：`test_admin_and_exec_are_uncovered_by_design`
# 明确钉死「EXEC 不受写意图护栏」是**刻意设计**（写动词表会误伤「提交一下/回滚」这类），
# EXEC 的隔离按设计应落在沙箱边界。故本牢笼是「EXEC 另有控制」这一设计里缺的那一块，
# 装在**子进程内部**（审计钩无法被被审计代码移除），而非去动 `_is_write_tool`。
#
# 策略（保守、够用即止）：
#   · 凭据 / NT 设备命名空间路径：读+写一律拦（对齐 `_hard_floor_reason`「读写都拦」）。
#   · 写操作越出 code_root：拦——但放行落在临时/缓存目录（tempfile / ~/.cache）的写，
#     否则 pytest 临时文件、matplotlib/HF 缓存等合法脚本会连不动（过度拦 = agent 瘫）。
#   · 读越出 code_root：不拦（读 site-packages / 系统库是合法刚需；读的危险只在凭据）。
#   · 禁止再 `subprocess.Popen` 拉起 python 子解释器：那是最省事的「跳出牢笼」逃逸口。
#   · 校验自身出任何错 → 拒绝该操作（fail-closed，与全项目一致）。
# 已知残留（如实标注，不假装密不透风）：混淆构造路径、或 exec 原生写 syscall 仍可绕——
# 本钩针对的是 AA 那种「直接 open(...,'w')」的一阶绕过；真·对抗性逃逸仍需容器隔离兜底。
# ---------------------------------------------------------------------------

#: 装进子进程牢笼代码体里的策略占位由 `_python_exec_jail_prefix` 用 repr 注入。
#: 该字符串是**静态常量**（其内部的 %s/%r 属子进程运行期格式化，绝不能在构造时
#: 被外层 `%` 处理），故与注入头分离。
_PJ_JAIL_BODY = '''
import os as _pj_os, sys as _pj_sys

_PJ_WFLAGS = (_pj_os.O_WRONLY | _pj_os.O_RDWR | _pj_os.O_CREAT
              | _pj_os.O_TRUNC | _pj_os.O_APPEND)


def _pj_norm(p):
    if isinstance(p, (bytes, bytearray)):
        try:
            p = _pj_os.fsdecode(p)
        except Exception:
            p = p.decode("utf-8", "replace")
    p = str(p)
    try:
        rp = _pj_os.path.realpath(_pj_os.path.abspath(_pj_os.path.expanduser(p)))
    except Exception:
        return None
    return rp.replace("\\\\", "/").lower()


def _pj_floor(low):
    if low is None:
        return "路径无法解析"
    bs = low.replace("/", "\\\\")
    for pre in _PJ_NT:
        n = pre.replace("/", "\\\\").lower()
        if bs == n or bs.startswith(n):
            return "NT 设备命名空间路径（凭据泄漏风险）"
    parts = [x for x in low.split("/") if x] if low else []
    if any(d in parts for d in _PJ_CRED_DIRS):
        return "凭据存储目录"
    if parts and parts[-1] in _PJ_CRED_FILES:
        return "凭据文件"
    if low == "etc/sudoers" or low.endswith("/etc/sudoers"):
        return "特权配置文件"
    for c in _PJ_CRED_FULL:
        if low == c or low.startswith(c.rstrip("/") + "/"):
            return "凭据/令牌子目录"
    return None


def _pj_under_any(low, prefixes):
    for c in prefixes:
        cc = c.rstrip("/")
        if low == cc or low.startswith(cc + "/"):
            return True
    return False


def _pj_check(path, write):
    low = _pj_norm(path)
    f = _pj_floor(low)
    if f:
        raise PermissionError("python_exec 安全拦截：命中%s，禁止操作 %s。"
                              "（如需改项目文件请用 dev_* 写工具并走确认流程）" % (f, path))
    if write and _PJ_ROOT and not (low == _PJ_ROOT or low.startswith(_PJ_ROOT + "/")):
        if not _pj_under_any(low, _PJ_ALLOW):
            raise PermissionError(
                "python_exec 安全拦截：写操作越出代码根目录，禁止 %s。"
                "要修改项目文件请改用 apply_edit/create_file/dev_region_edit"
                "（会走写意图授权与确认暂存），不要在 python_exec 里直接落盘。" % path)


def _pj_is_write(mode, flags):
    for x in (mode, flags):
        if isinstance(x, str) and any(c in x for c in "wax+"):
            return True
        if isinstance(x, int) and x and (x & _PJ_WFLAGS):
            return True
    return False


_PJ_OS_WRITE = frozenset((
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.rmdir", "os.mkdir",
    "os.link", "os.symlink", "os.truncate", "os.chmod", "os.chown",
))


def _pj_is_python(args):
    try:
        exe = args[0]
        if isinstance(exe, (bytes, bytearray)):
            exe = _pj_os.fsdecode(exe)
        base = _pj_os.path.basename(str(exe)).lower()
        return base in ("python", "python.exe", "python3", "python3.exe", "py",
                        "py.exe", "pythonw", "pythonw.exe")
    except Exception:
        return False


def _pj_hook(event, args):
    if not args:
        return
    if event == "open":
        path = args[0]
        if isinstance(path, int):
            return
        writing = _pj_is_write(
            args[1] if len(args) > 1 else None,
            args[2] if len(args) > 2 else None)
        if writing:
            try:
                _pj_check(path, True)
            except PermissionError:
                raise
            except Exception as e:
                raise PermissionError(
                    "python_exec 安全拦截：写校验异常，按 fail-closed 拒绝：%r" % (e,))
        else:
            # 读：只认「故意地板拒绝」（凭据/NT）；校验自身出任何岔都放行——
            # 否则 import 期成百上千次只读 open 里任一次小异常都会把脚本整个跑挂。
            try:
                _pj_check(path, False)
            except PermissionError:
                raise
            except Exception:
                return
    elif event in _PJ_OS_WRITE:
        try:
            for p in args:
                if isinstance(p, int):
                    continue
                if isinstance(p, (str, bytes, bytearray)) or hasattr(p, "__fspath__"):
                    _pj_check(p, True)
        except PermissionError:
            raise
        except Exception as e:
            raise PermissionError(
                "python_exec 安全拦截：%s 校验异常，按 fail-closed 拒绝：%r" % (event, e,))
    elif event == "subprocess.Popen" and _pj_is_python(args):
        raise PermissionError(
            "python_exec 安全拦截：禁止再拉起未受约束的 Python 子解释器以逃逸沙箱。")


try:
    _pj_sys.addaudithook(_pj_hook)
except Exception:
    raise PermissionError("python_exec 安全拦截：文件系统审计钩安装失败，拒绝执行代码。")
'''


def _pj_child_policy():
    """为子进程牢笼算出当前作用域的策略（根目录 / 凭据地板 / 允许写越界的临时目录）。"""
    root = (_get_code_root() or "").strip()
    root_low = ""
    if root:
        try:
            root_low = os.path.realpath(os.path.abspath(root)).replace("\\", "/").lower()
        except Exception:
            root_low = os.path.normpath(root).replace("\\", "/").lower()
    cred_full = ["etc/sudoers"]
    appdata = os.getenv("APPDATA")
    if appdata:
        base = os.path.normpath(appdata).replace("\\", "/").lower()
        for sub in ("microsoft/credentials", "microsoft/crypto", "microsoft/protect",
                    "microsoft/systemcertificates", "gnupg", "gcloud", "azure", "powershell"):
            cred_full.append(base + "/" + sub)
    allow = []
    try:
        import tempfile
        t = os.path.realpath(tempfile.gettempdir()).replace("\\", "/").lower()
        if t:
            allow.append(t)
    except Exception:
        pass
    try:
        home = os.path.expanduser("~")
        if home:
            c = os.path.realpath(os.path.join(home, ".cache")).replace("\\", "/").lower()
            if c:
                allow.append(c)
    except Exception:
        pass
    return {
        "root": root_low,
        "creddirs": [".ssh", ".aws", ".kube", ".gnupg"],
        "credfiles": [".netrc"],
        "credfull": cred_full,
        "nt": list(_NT_DEVICE_PREFIXES),
        "allow": allow,
    }


def _python_exec_jail_prefix():
    """渲染牢笼 bootstrap：把策略以 repr 注入头部常量，再拼静态代码体。"""
    pol = _pj_child_policy()
    header = (
        "_PJ_ROOT=%(root)r\n"
        "_PJ_CRED_DIRS=%(creddirs)r\n"
        "_PJ_CRED_FILES=%(credfiles)r\n"
        "_PJ_CRED_FULL=%(credfull)r\n"
        "_PJ_NT=%(nt)r\n"
        "_PJ_ALLOW=%(allow)r\n" % pol
    )
    return header + _PJ_JAIL_BODY + "\n"


def python_exec(code):
    """执行 Python 代码，返回 stdout/stderr（截断到 1500 字）。

    用于数值计算、数据处理、文本变换等"让 agent 真正动手"的场景。
    企业模式在隔离的临时项目副本中运行；本地兼容模式以当前用户权限运行。
    """
    code = (code or "").strip()
    # 清理模型可能包裹的代码围栏 / 语言提示词，避免把 "```python" 当代码执行
    code = re.sub(r"^```(?:python|py)?\s*", "", code)
    code = re.sub(r"\s*```$", "", code)
    code = re.sub(r"^\s*(?:python|py)\s*$", "", code, flags=re.I | re.M)  # 去掉独立的提示行
    code = code.strip().strip("`").strip()
    if not code:
        return "未提供有效代码。"
    # 打包版 sys.executable 是 DocMind.exe（onedir 内无 python.exe），
    # 不能拿它当解释器跑代码（只会再启动一个应用实例）。
    if getattr(sys, "frozen", False):
        return "分发版（DocMind.exe）未内置 Python 解释器，python_exec 仅在源码/venv 环境可用。"
    # 弱模型常把多行代码写成单行、换行用字面量 \n/\t（Action Input 是单行字段）；
    # 首次编译失败且代码里没有真实换行时，反转义一次再执行（正常代码不受影响）。
    try:
        compile(code, "<python_exec>", "exec")
    except SyntaxError:
        if "\\n" in code and "\n" not in code:
            code = code.replace("\\n", "\n").replace("\\t", "\t")
    # 已索引代码库时在代码根目录内执行：脚本里的相对路径（如 open("app/src/...")）
    # 才能按项目语义解析；未配置代码库时维持旧行为（服务端目录）。
    cwd = _get_code_root() or os.path.dirname(__file__)
    # 文件系统牢笼：容器路径本就 --read-only 把写丢进一次性副本（天然隔离），且其内
    # 路径与宿主 code_root 不同，装了会误拦合法写 → 只在宿主/本地回退（真实可写盘）时注入。
    # 审计钩随 `python -c` 一起进子进程，先于用户代码装好，用户代码无法移除已注册的钩。
    run_code = code
    try:
        container_isolates = (execution_mode() == "enterprise"
                              and stage_execution_active() and stage_backend() == "container")
    except Exception:
        container_isolates = False
    if not container_isolates:
        try:
            run_code = _python_exec_jail_prefix() + code
        except Exception as e:  # noqa: BLE001
            # 牢笼构造失败 = 宁可拒绝执行，不放行无约束写（fail-closed）。
            return f"拒绝执行：文件系统牢笼初始化失败（{e!r}），请改用 dev_* 写工具。"
    try:
        command = ([sys.executable if stage_execution_active() and stage_backend() == "host_compat" else "python", "-c", run_code]
                   if execution_mode() == "enterprise"
                   else [sys.executable, "-c", run_code])
        proc = run_agent_command(command, project_root=cwd, timeout=_PYTHON_EXEC_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (f"代码执行超时（>{_PYTHON_EXEC_TIMEOUT}s），可能被死循环阻塞。"
                "需要跑更久的脚本，请改用 run_command（可加 background: true 转后台任务）。")
    except EnterpriseSandboxUnavailable as e:
        return f"拒绝执行：{e}"
    except Exception as e:  # noqa: BLE001
        return f"执行失败: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    # 试做区里说清相对路径落在哪——见 agent_runtime/tools.py:stage_cwd_report 的理由。
    # 只有作用域开着时才加这一行，平时运行不改变输出形状。
    try:
        from agent_runtime.tools import stage_cwd_report, stage_scope_active
        if stage_scope_active():
            out = stage_cwd_report(cwd) + "\n" + out
    except Exception:
        pass
    if not out.strip():
        return "（代码已执行，无输出）"
    # 保留开头与结尾：异常堆栈在末尾，单向截断会把真正的报错切掉。
    return _window_output(out)


# ---------------------------------------------------------------------------
# 视频提示词生成工具：按 MiniMax H3 的"三段结构"把创意描述转为可直接粘贴进
# ComfyUI 节点 (MiniMaxH3ImageToVideo) 的结构化提示词。
# 设计说明：三段骨架（integrated_multimodal_description / overall_soundscape /
# non_diegetic_music）由本工具确定性拼装，保证永远符合 H3 格式；LLM 仅用于润色
# Shot 1 的英文描述，失败则回退模板句。这样无论模型强弱都能产出可用提示词。
# ---------------------------------------------------------------------------
def _enrich_shot1(spec):
    """用 LLM 把中文创意扩写成一句英文分镜描述；失败时回退到确定性模板句。"""
    provider = get_runtime("llm_provider") or ""
    if provider and provider != "mock":
        try:
            from llm import LLMClient

            sys_p = (
                "把下面中文创意扩写成一句英文分镜描述（含主体动作、镜头运动、物理力），"
                "只输出这一句英文，不要解释，不超过 40 词。"
            )
            client = LLMClient()
            out = client.chat(
                [{"role": "system", "content": sys_p}, {"role": "user", "content": spec}],
                stream=False,
            )
            out = (out or "").strip().strip("`").strip()
            # 防御：若模型没按要求只给一句英文，回退模板
            if out and "integrated_multimodal" not in out and len(out) < 400:
                return out
        except Exception:  # noqa: BLE001
            pass
    return (
        f"{spec}。镜头缓慢推近，机位与主体视线平齐，"
        "注意点名物理力（重力/风/液体/布料张力）让画面更真实。"
    )


def gen_video_prompt(spec):
    """按 MiniMax H3 的三段结构，把一段创意描述生成结构化视频提示词。

    结构（integrated_multimodal_description / overall_soundscape / non_diegetic_music）
    由本工具确定性拼装，保证永远符合 H3 格式；LLM 仅用于润色 Shot 1 的英文描述。
    输出可直接粘贴进 ComfyUI 的 MiniMaxH3ImageToVideo 节点的 prompt 字段。
    """
    spec = (spec or "").strip().strip("'\"")
    if not spec:
        return "未提供创意描述，请描述你想生成的画面（主体/场景/动作/氛围）。"

    shot1 = _enrich_shot1(spec)

    # 关键词驱动的声音设计（diegetic），让画内音贴合场景
    amb = []
    if "雨" in spec:
        amb.append("steady rain")
    if "夜" in spec or "晚" in spec:
        amb.append("distant night traffic")
    if "风" in spec:
        amb.append("wind moving through the scene")
    if "海" in spec or "浪" in spec:
        amb.append("waves on the shore")
    if "城" in spec or "市" in spec:
        amb.append("low city hum")
    if "森" in spec or "林" in spec:
        amb.append("rustling leaves")
    if "雪" in spec:
        amb.append("soft snowfall")
    if not amb:
        amb.append("ambient room tone")
    sound = "画内音逐一点名：" + ", ".join(amb) + "。"

    music = (
        "配乐：pulsing synth arpeggio at a steady mid tempo over sustained low bass, "
        "with a filtered swell that builds then drops."
    )

    return (
        "integrated_multimodal_description:\n"
        f"[Shot 1] Live-action, cinematic. {shot1}\n"
        "[Shot 2] At 00:03.000，承接上一镜，引用 Shot 1 的角色与位置，推进后续动作与状态变化。\n"
        "overall_soundscape:\n"
        f"{sound}\n"
        "non_diegetic_music:\n"
        f"{music}\n\n"
        "（约束：无负面提示词；FPS 固定 24；时长 4-15s；提示词 < 7000 字符且需 >= 100 词则继续扩写；"
        "不说话的角色写 lips stay closed。）"
    )


# ---------------------------------------------------------------------------
# 素材筛选工具：在本地精选素材目录（Kenney CC0 等）中按关键词 / 类型 / 许可筛选。
# 设计说明：
#   - 素材库（Kenney / OpenGameArt / itch）没有稳定可用的公开 API，且多被
#     Cloudflare 保护，直接爬取易失败、也不礼貌。业界做法（如 Arcane Assets
#     MCP）同样是维护一份本地 / 远程的素材清单 JSON，由 Agent 在其上做检索。
#   - 因此这里用一份人工精选 + 持续可扩充的 assets_catalog.json，配合本工具的
#     关键词展开与打分排序，实现「根据提问筛选素材」。
# ---------------------------------------------------------------------------
_CATALOG_PATH = os.path.join(os.path.dirname(__file__), "assets_catalog.json")

# 中文关键词 -> 英文检索词 的同义展开表，使中文提问也能命中英文标签。
_CN_SYNONYMS = {
    "角色": "character sprite",
    "精灵": "sprite character",
    "人物": "character",
    "怪物": "monster creature",
    "动物": "animal",
    "龙": "dragon",
    "骑士": "knight",
    "巫师": "wizard",
    "战士": "knight warrior",
    "像素": "pixel",
    "地形": "tileset tile",
    "地块": "tileset tile",
    "贴图": "tile texture",
    "地图": "tile map",
    "等距": "isometric",
    "模型": "model 3d",
    "三维": "3d model",
    "界面": "ui interface",
    "图标": "icon ui",
    "按钮": "ui button",
    "音效": "sfx sound audio",
    "声音": "sound audio",
    "音乐": "music",
    "字体": "font",
    "特效": "particle vfx effect",
    "粒子": "particle",
    "动画": "animation",
    "战斗": "battle",
    "地牢": "dungeon",
    "城镇": "town",
    "小镇": "town",
    "城市": "city",
    "太空": "space",
    "宇宙": "space",
    "射击": "shooter",
    "坦克": "tank",
    "飞船": "ship",
    "平台": "platformer",
    "美术": "art asset",
    "资源": "asset",
    "素材": "asset",
}


def _expand_query(q):
    """把用户提问展开成一组检索 token（英文/数字原样保留，中文按同义词展开）。"""
    q = (q or "").lower()
    tokens = re.findall(r"[a-z0-9]+", q)  # 抽取英文 / 数字片段，中文串里也抓得到
    for cn, en in _CN_SYNONYMS.items():
        if cn in q:
            tokens.extend(en.split())
    # 去重保序
    seen, out = set(), []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search_assets(query, asset_type=None, license=None):
    """在精选游戏素材目录（Kenney CC0 等）中按关键词/类型/许可筛选素材。

    返回命中条目的可读列表；按关键词命中数量打分排序，最多 8 条。
    asset_type 例：'2d-sprites' / 'tilesets' / 'ui' / 'audio' / 'fonts' / '3d'
    license    例：'CC0' / 'CC-BY' / 'CC-BY-SA-3.0'
    """
    try:
        with open(_CATALOG_PATH, encoding="utf-8") as f:
            catalog = json.load(f)
    except Exception:
        return "未找到相关素材。"

    tokens = _expand_query(query)
    type_filter = (asset_type or "").lower().strip()
    lic_filter = (license or "").strip()

    scored = []
    for item in catalog:
        cat = (item.get("category") or "").lower()
        lic = item.get("license") or ""
        if type_filter and cat != type_filter:
            continue
        if lic_filter and lic != lic_filter:
            continue
        hay = " ".join([
            item.get("name", ""),
            " ".join(item.get("tags", [])),
            item.get("desc", ""),
            item.get("category", ""),
            lic,
        ]).lower()
        if tokens:
            score = sum(1 for t in tokens if t in hay)
            if score == 0:
                continue
        else:
            score = 1
        scored.append((score, item))

    if not scored:
        return "未找到相关素材。"
    scored.sort(key=lambda x: -x[0])
    lines = []
    for _, it in scored[:8]:
        lines.append(
            f"· {it['name']} ［{it['category']}｜{it['license']}］ {it.get('desc', '')}  → {it.get('url', '')}"
        )
    return "\n".join(lines)


def set_embedding_provider(provider):
    """切换检索用的 embedding provider（如 local <-> qwen），并清空缓存。"""
    set_runtime("embedding_provider", provider)
    global _emb
    _emb = None


# ---------------------------------------------------------------------------
# 代码工具：让 Agent 能检索 / 阅读 / 搜索已索引的代码库（代码问答模式）。
# 所有路径操作都限定在 code_root 之内，避免越界读取本机其它文件。
# ---------------------------------------------------------------------------
def _get_code_root():
    return (get_runtime("code_root") or CODE_ROOT or "").strip()


def _project_code_scope():
    """当前该查哪个代码集合、以及过滤命中时以哪个根为界。

    ## 为什么不能只信 `code_collection` 这个运行时键

    `/api/ingest_code` 把代码索引进 `projects.code_collection(pid)`
    （`docmind_code__<pid>`），而 `search_code` 读的是
    `get_runtime("code_collection") or CODE_COLLECTION_NAME`——而**全仓从来
    没有**任何地方 `set_runtime("code_collection", ...)`（扫遍 28 个
    `set_runtime` 调用点，没有它）。结果是：项目的代码被索引进它自己的集合，
    然后**永远不会被查询**，每一次 `search_code` 都在查 legacy 全局集合。

    实测（真跑 `wf-0c6374c4d612`）：在一个只有 `calc.py` / `test_calc.py` /
    `README.md` 的项目上，`search_code` 返回 `project.godot:L1-L39` /
    `.docmind/web/index.js` / `MiniMax_H3_提示词速查.md`——全是 DocMind 自己
    仓库的内容。子代理据此把调研结论写成了另一个项目，三连任务全 failed。

    ## 返回值

    `(collection_name, ingest_root)`；拿不到项目就返回 `(None, "")`，
    调用方据此**不做任何过滤**——宁可少一道防护，也不把工具打死。
    """
    explicit = get_runtime("code_collection")
    if explicit:
        return str(explicit), ""
    try:
        import projects as _projects
        if not _projects.enabled():
            return None, ""
        pid = _projects.current_project_id()
        if not pid:
            return None, ""
        record = _projects.get_project(pid) or {}
        return _projects.code_collection(pid), str(record.get("root") or "")
    except Exception:  # noqa: BLE001 —— 取不到就退化成不过滤，不阻断检索
        return None, ""


def _hit_within_root(source, root):
    """这条命中的文件是否落在 `root` 之内。

    `source` 既可能是相对 ingest 根的路径（`pkg/a.py`），也可能是绝对路径，
    两者都要认：只认相对路径会把合法的绝对路径命中误删掉。
    脏 source（None / 空串 / 非字符串）一律放过——宁可少一道防护，也不丢内容。
    """
    if not root or not isinstance(source, str) or not source.strip():
        return True
    try:
        target = source if os.path.isabs(source) else os.path.join(root, source)
        return _path_within_real_root(root, target)
    except (OSError, ValueError):
        return True



#: 分区编辑豁免标志：必须是**上下文级 ContextVar**，不能放进程全局 `_RUNTIME`。
#: 并行工具批用 `ThreadPoolExecutor + copy_context().run` 执行（见 agent 并行派发），
#: `copy_context` 只隔离 ContextVar、对裸字典 `_RUNTIME` 无能为力——若把此豁免存进
#: `_RUNTIME`，一条在飞的 `dev_region_edit` 就会把豁免广播给同批兄弟任务的普通
#: `apply_edit`/`create_file`，让它们越过分区护栏（fail-open）。ContextVar 让豁免只沿
#: `dev_region_edit → 其内部 apply_edit` 这条同步调用链生效，worker 间互不可见。
_REGION_EDIT_CTX: "contextvars.ContextVar" = contextvars.ContextVar(
    "docmind_region_edit_context", default=False)


def _region_write_allowed(target):
    """When regions are configured, ordinary write tools cannot bypass region tools."""
    if _REGION_EDIT_CTX.get():
        return True
    root = _get_code_root()
    if not root or not os.path.isfile(os.path.join(root, "regions.json")):
        return True
    try:
        from regions import get_region_map
        target = os.path.realpath(target)
        for meta in get_region_map(root).values():
            base = os.path.realpath(os.path.join(root, meta["dir"]))
            if target == base or target.startswith(base + os.sep):
                return False
    except Exception:
        return False
    return True


# 记录 Agent 本次会话内已用 read_file 读过的文件（绝对路径，normcase 归一），
# 用于 apply_edit 的"先读后写"安全护栏：未确认过内容的文件不允许整体重写。
_READ_FILES = set()
# 「未变化存根」的覆盖表在文件末尾定义（`_READ_COVERAGE`）。
#
# 上面这两个是**主对话**的记录。子代理走 `_READ_CONV` 里自己那一份——
# 原因见 `begin_read_conversation` 的说明，这里必须留默认那一份，
# 因为大量调用点与测试都直接用模块级名字。
_READ_CONV: "contextvars.ContextVar" = contextvars.ContextVar(
    "docmind_read_conversation", default=None)


def _read_store():
    """当前对话的 ``(已读文件集合, 区间覆盖表)``。

    没有子代理作用域时就是模块级那两份（主对话，行为与从前完全一致）。
    """
    conv = _READ_CONV.get()
    if conv is None:
        return _READ_FILES, _READ_COVERAGE
    return conv


def begin_read_conversation():
    """给子代理一份**独立**的读记录，返回用于还原的 Token。

    ## 为什么必须按对话隔离

    「未变化存根」的前提是「**本次对话**中读过，且直接引用那次结果」。而模块级
    全局会让**另一个子代理**拿到上一个子代理的读记录——那份内容它压根没见过。

    实测 `wf-3073fc5f2403`（连错三轮的真正原因）：`research` 读完
    `behaviors/enemy.gd` 全文后被反思判失败，重规划产生的
    `research_behaviors_r2` 是一个**全新上下文**的子代理，它再读同一文件拿到的
    却是——

        === behaviors\\enemy.gd（未变化）===
        整份文件已在本次对话中读过、且此后未改动；直接引用那次结果。

    文件确实没被改过，所以存根必然触发。于是**每一次重试都必然拿不到正文**，
    那个重试在结构上不可能成功，而界面上只看得到「子代理失败」。反思反复报的
    「read_file 观察仅返回『已读过、不要重复读取』的缓存提示，没有展示正文」
    就是它。

    这与「任务要写文件却派给只读角色」是同一族：**一个注定完成不了的任务被
    呈现成「模型没做好」**。

    ## 为什么用 ContextVar 而不是 `clear_read_files()`

    并行波次里多个子代理同时在跑。直接清全局会互相踩：一个子代理刚开始读，
    另一个就把它的记录清了，存根在**同一个子代理内部**也会失效——那就等于把
    「别重复读」这个功能整个关掉。ContextVar 天然随 `copy_context()` 复制，
    各子代理互不干扰。
    """
    return _READ_CONV.set((set(), {}))


def end_read_conversation(token) -> None:
    """还原到外层对话的读记录（与 `begin_read_conversation` 配对，严格 try/finally）。"""
    try:
        _READ_CONV.reset(token)
    except (ValueError, LookupError, RuntimeError):
        pass


def bind_read_conversation(store):
    """把一份**已存在**的读记录绑进当前上下文，返回用于还原的 Token。

    与 `begin_read_conversation` 的区别：后者总是新开一份空 `(set(), {})`，
    那是**子代理**要的语义（每个子代理都是全新对话，读记录不该从外层继承）。
    主对话要的粒度是「一个会话」而不是「一个回合」——同一会话跨轮重读同一文件
    应当省 token（命中存根），而不同会话之间绝不能互串。于是让会话的 Agent
    实例**持有一份长活 store**，每进一个 `run()` 就用本函数把它绑进 ContextVar、
    退出时 `end_read_conversation` 还原。store 的生命周期由实例决定（随会话销毁），
    ContextVar 只负责让 `run()` 期间的工具调用读到正确那一份。

    这样主对话与子代理共用同一套隔离机制：`_read_store()` 读 ContextVar，
    有人绑就用绑的、没人绑才回落到模块全局（兼容测试与临时单例）。
    """
    return _READ_CONV.set(store)


_REGION_LOCKS = {}
_FILE_LOCKS = {}
_REGION_LOCKS_GUARD = threading.Lock()


def _region_lock(key):
    with _REGION_LOCKS_GUARD:
        return _REGION_LOCKS.setdefault(key, threading.RLock())


def _file_lock(target):
    """Serialize final writes per absolute path across parallel subagents."""
    key = os.path.normcase(os.path.abspath(target))
    with _REGION_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


def _resolve_in_root(path):
    """把 path 解析为 code_root 内的绝对路径；越界返回 (None, root_abs, reason)。

    reason 在任一拒绝（越界或与 root 无关的地板）时给出人类可读原因，
    调用方应优先用 reason 作报错文案，避免把「凭据地板拦截」误报成「不在根目录内」。
    """
    root = _get_code_root()
    root_abs = os.path.normpath(root)
    target = os.path.normpath(path if os.path.isabs(path) else os.path.join(root_abs, path))
    floor = _hard_floor_reason(target)
    if floor:
        return None, root_abs, floor
    if not (target == root_abs or target.startswith(root_abs + os.sep)):
        alt = os.path.normpath(os.path.join(root_abs, path.lstrip("./\\")))
        if os.path.isfile(alt) and (alt == root_abs or alt.startswith(root_abs + os.sep)):
            alt_floor = _hard_floor_reason(alt)
            if alt_floor:
                return None, root_abs, alt_floor
            alt_resolved = _resolve_floor_checked(root_abs, alt)
            if alt_resolved:
                return None, root_abs, alt_resolved
            return alt, root_abs, None
        return None, root_abs, "拒绝访问：路径不在代码根目录内（禁止越界读/写）。"
    # ③ symlink 闭合（审计 F1/F3 统一入口）：字面地板 + 根检查已过，realpath 再跑一次
    resolved = _resolve_floor_checked(root_abs, target)
    if resolved:
        return None, root_abs, resolved
    return target, root_abs, None


def _clean_symbol(p):
    """从混入自然语言的输入里提取首个「代码标识符」token。

    弱模型常把工具输入写成「seekTo 进行进度跳转」这种「符号 + 中文描述」的形式，
    直接当正则/检索词会匹配失败。这里在检测到中文时只取第一个 ASCII 标识符
    （如 seekTo）；纯 ASCII 输入（可能是合法正则）原样保留。
    """
    p = (p or "").strip()
    if not p:
        return p
    if re.search(r"[\u4e00-\u9fff]", p):
        m = re.search(r"[A-Za-z_]\w*", p)
        if m:
            return m.group(0)
    return p


def _clean_search_query(q):
    """语义检索 query 只"剥壳"、不抽符号：保留多词自然语言/中英混合的完整语义。

    _clean_symbol 会把「collision 碰撞检测逻辑」缩成单个标识符，那是给 grep 正则用的；
    语义向量检索需要完整 query（如「score combo high score save」是合法英文短句）。
    这里仅去掉弱模型常误带的 query:/path:/关键词: 前缀与成对围栏、引号。
    """
    q = (q or "").strip()
    q = re.sub(
        r"^(?:query|q|path|关键词|检索词|搜索词)\s*[:：]\s*",
        "",
        q,
        flags=re.IGNORECASE,
    ).strip()
    if len(q) >= 2 and q[0] == q[-1] and q[0] in "`'\"":
        q = q[1:-1].strip()
    return q


# 符号种类 -> 检索结果标签动词（function 按语言再区分 def/func）
_SYMBOL_KW = {
    "class": "class", "signal": "signal", "enum": "enum",
    "const": "const", "var": "var",
}


def search_code(query):
    """在已索引的源代码/配置中检索相关函数、类、配置片段。"""
    if not _get_code_root():
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录后再问代码相关问题。"
    query = _clean_search_query(query)
    collection, ingest_root = _project_code_scope()
    retrieved = get_retriever(collection=collection or CODE_COLLECTION_NAME,
                              top_k=TOP_K,
                              embedding_client=_get_emb()).invoke(query)
    # 检索器内部已把候选切到 top_k=TOP_K，所以这里 len(retrieved)==TOP_K 就意味着
    # 「后面还有更靠后的相关片段被配额截掉了」——与 grep 的 _HIT_CAP 触顶同一性质。
    retrieved_count = len(retrieved)
    docs = [item.page_content for item in retrieved]
    metas = [dict(item.metadata or {}) for item in retrieved]
    docs, metas = _dedup_docs(docs, metas)
    # 集合切对了也可能命中项目外的内容：集合里留着上一次 ingest 的残留，或者
    # 查的本来就是别人的 legacy 集合。所以按**该集合自己的 ingest 根**再兜一道。
    #
    # 这里**不能**拿 `_get_code_root()` 当界：工作流开项目舞台时它被覆盖成
    # `.docmind/project_stages/<wid>/workspace` 这个临时副本，而索引是从项目
    # **原根**建的，两者路径不同——按它过滤会把所有命中全删光。
    dropped = 0
    if ingest_root:
        kept = [(d, m) for d, m in zip(docs, metas)
                if _hit_within_root(m.get("source"), ingest_root)]
        dropped = len(docs) - len(kept)
        docs = [d for d, _m in kept]
        metas = [m for _d, m in kept]
    if not docs:
        if dropped:
            # 不能报「未找到相关内容」——那句话会把用户引向「换个关键词再搜」，
            # 而真因是「命中全在项目外」。与本次故障同一性质：证据与结论对不上。
            return ("命中 %d 条，但都不在当前项目的代码目录内，已过滤。"
                    "换个关键词，或用 grep / read_file 直接看项目内的文件。" % dropped)
        return "代码库未找到相关内容，建议改用 grep 搜索关键词或 read_file 查看具体文件。"
    out = []
    for d, m in zip(docs, metas):
        src = m.get("source", "?")
        sym = m.get("symbol", "")
        lang = m.get("lang", "")
        kind = m.get("kind", "code")
        doc = m.get("doc", "")
        sl, el = m.get("start_line"), m.get("end_line")
        loc = f"{src}:L{sl}" + (f"-L{el}" if el and el != sl else "") if sl else src
        kw = _SYMBOL_KW.get(kind)
        verb = ("def" if lang == "python" else "func") if kind == "function" else kw
        if sym and verb:
            label = f"{loc} › {verb} {sym}"
        elif sym:
            label = f"{loc} › {sym}"
        else:
            label = loc
        if lang:
            label += f" ({lang})"
        shown = d if len(d) <= 600 else d[:600].rstrip() + "\n…（下略）"
        # 给片段逐行标行号（锚定 start_line），模型可直接引用精确行
        if sl:
            numbered = []
            for i, ln in enumerate(shown.split("\n")):
                if ln == "…（下略）":
                    numbered.append(ln)
                else:
                    numbered.append(f"{sl + i:>5}| {ln}")
            shown = "\n".join(numbered)
        if doc:
            shown = f"# 文档: {doc.splitlines()[0]}\n" + shown
        out.append(f"[{label}]\n{shown}")
    # 如实交代「我没看全」：语义检索按相关度最多返回 TOP_K 条，取满就意味着排序靠后的
    # 相关片段被配额截掉。不标注的话，模型会把这几条当成全部命中、据此报"总数/都找到了"
    # ——与本次故障同性质（grep 触顶那条教训的检索版）。
    note = ""
    if retrieved_count >= TOP_K:
        note = ("\n（提示：本次为语义检索，按相关度最多返回前 %d 条，已取满；"
                "排序靠后的相关代码未被返回，**不要把这几条当全部**，也不要据此统计总数——"
                "要精确计数或穷尽定位，请缩小关键词、提高 TOP_K，或改用 grep 直接匹配。）"
                % TOP_K)
    return "\n---\n".join(out) + note


def _path_within_real_root(root, target):
    try:
        root_real = os.path.realpath(root)
        return os.path.normcase(os.path.commonpath((root_real, os.path.realpath(target)))) == os.path.normcase(root_real)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 与 code_root 无关的地板（Hermes hardline blocklist 移植）
#
# 背景：所有写约束都依赖 `code_root`（一处根目录），没有一层与 root 无关的禁止项。
# 一旦 `code_root` 被设成 `$HOME` 或其上级，凭据文件与普通项目文件就无差别
# （见 docs/known-defect-no-write-path-floor.md §2 实测：code_root=$HOME 时能写
# ~/.ssh/id_rsa）。这一层不依赖 root，任何 root 下都生效，无 override。
#
# 两类硬拦：
#   ① 凭据存储类（读写都拦，无 override）：~/.ssh ~/.aws ~/.kube ~/.netrc ~/.gnupg、
#      /etc/sudoers，以及 Windows APPDATA 下的凭据/令牌子目录。即使 code_root 指到
#      HOME，也禁止读写这些路径（Hermes 同语义：safe-root 指到 $HOME 仍禁 ~/.ssh）。
#   ② NT 设备命名空间路径（读写都拦）：\??\ \\.\ \\?\GLOBALROOT \\?\UNC 等。
#      Hermes 明确：merely resolving such a path triggers outbound SMB
#      authentication and can leak the user's NTLM hash。我们此前是靠「前缀不匹配
#      本机 root」的副作用顺带拒（见同文档 §5 三次更正），这里改为显式前缀识别，
#      任何平台、任何 root 形态下都生效——消除「靠巧合成立」的设计债。
# ---------------------------------------------------------------------------

#: NT 设备命名空间前缀（按 Windows 实际写法，slash/backslash 都归一）。
#: 注意：这里用普通字符串、每个反斜杠转义两次，避免 raw string 以反斜杠结尾导致的
#: 「unterminated string literal」。顺序：更具体的（GLOBALROOT/UNC）在前。
_NT_DEVICE_PREFIXES = (
    "\\\\?\\GLOBALROOT",
    "\\\\?\\UNC",
    "\\\\?\\",
    "\\\\.\\",
    "\\??\\",
)


def _nt_device_prefix(p):
    """显式识别 NT 设备命名空间路径；命中返回规范前缀串，否则 None。"""
    if not p:
        return None
    variants = {p.replace("/", "\\")}
    try:
        variants.add(os.path.normpath(p).replace("/", "\\"))
    except (OSError, ValueError):
        pass
    for pre in _NT_DEVICE_PREFIXES:
        norm = pre.replace("/", "\\")
        for v in variants:
            if v == norm or v.startswith(norm):
                return pre
    return None


def _is_credential_store(target, low):
    """target 是否为凭据存储路径——与 code_root 无关，任意位置都拦。

    覆盖：① 路径中任意一级目录名为 .ssh/.aws/.kube/.gnupg（其下全部内容）；
    ② basename 为 .netrc（凭据文件）；③ /etc/sudoers；
    ④ Windows APPDATA 下的凭据/令牌子目录。这样无论 code_root 被设成 HOME、
    其上级、还是包含同名目录的项目，凭据路径都永远与普通文件有区别。
    low 为分隔符归一为 "/" 的小写串，故 Windows 下 /etc/sudoers 也能命中。
    """
    # ①/② 按路径分量命中（任意位置，分隔符无关）
    parts = [p for p in re.split(r"[\\/]", low) if p]
    for cred in (".ssh", ".aws", ".kube", ".gnupg"):
        if cred in parts:
            return True
    if parts and parts[-1] == ".netrc":
        return True
    # ③ /etc/sudoers（分隔符无关）
    if low == "etc/sudoers" or low.endswith("/etc/sudoers"):
        return True
    # ④ Windows APPDATA 下的凭据/令牌子目录
    appdata = os.getenv("APPDATA")
    if appdata:
        al = os.path.normpath(appdata).lower().replace("\\", "/")
        if low.startswith(al + "/"):
            for sub in ("microsoft/credentials", "microsoft/crypto",
                        "microsoft/protect", "microsoft/systemcertificates",
                        "gnupg", "gcloud", "azure", "powershell"):
                csub = os.path.normpath(os.path.join(appdata, sub)).lower().replace("\\", "/")
                if low == csub or low.startswith(csub + "/"):
                    return True
    return False


def _hard_floor_reason(target):
    """与 code_root 无关的地板：返回拒绝原因（str）或 None（放行）。

    target 应为 normpath 后的路径字符串。覆盖凭据存储类 + NT 设备命名空间两类，
    读写都拦，无 override。这是 Hermes hardline blocklist 的移植——在 root 约束
    之外再兜一层，使 code_root 被设成 HOME 或其上级时，凭据路径仍与普通文件有区别。
    """
    if not target:
        return None
    t = os.path.normpath(target)
    low = t.lower().replace("\\", "/")
    np = _nt_device_prefix(t)
    if np:
        return ("安全策略拒绝：NT 设备命名空间路径（%s…）会被系统尝试 SMB 认证并可能"
                "泄漏 NTLM 哈希，禁止任何解析。" % np)
    if _is_credential_store(t, low):
        return ("安全策略拒绝：凭据存储路径（%s）禁止读写——与 code_root 无关，无 override。"
                % os.path.basename(t))
    return None


def _floor_check_resolved(root_abs, target, check_root=True):
    """③ symlink 闭合（审计 F1 修复）：字面路径已过地板 + 根检查后，对 realpath 解析结果再跑一次。

    open() / os.path.isdir 按 OS 契约跟随符号链接，而 _resolve_* 与根检查基于 normpath 字面路径，
    故 code_root 内指向凭据 / 越界目标的 symlink 会绕过（审计 F1）。此处对 realpath(target)
    重跑 _hard_floor_reason + _path_within_real_root：凭据目的地（如 code_root=$HOME 下的
    ~/.ssh/id_rsa）被地板拦，越界目的地被根闭合拦；指向普通文件的合法 symlink 解析后仍命中
    普通路径 → 放行（不破坏合法 symlink）。realpath 解析失败（坏链 / 特权不足）一律按拒绝处理
    （fail-closed）。check_root=False 时（外部白名单路径）只重跑地板、不做根闭合。
    """
    try:
        real_target = os.path.realpath(target)
    except (OSError, ValueError):
        return "拒绝访问：路径解析失败（符号链接无法解析）。"
    floor = _hard_floor_reason(real_target)
    if floor:
        return floor
    if check_root and not _path_within_real_root(root_abs, real_target):
        return "拒绝访问：路径经符号链接指向代码根目录之外（或解析异常）。"
    return None


def _resolve_floor_checked(root_abs, target, check_root=True):
    """地板 + symlink 闭合的统一入口（审计 F3 重构，消除 6 个 _resolve_* 入口的重复）。

    先对字面 target 跑 _hard_floor_reason（凭据存储 / NT 设备命名空间），
    再对 realpath 解析结果跑 _hard_floor_reason + _path_within_real_root（F1 symlink 闭合）。
    根包容判定（root 变量 / 文案各异）由各入口在完成自身字面根检查后调用本函数，
    本函数只负责「凭据地板 + realpath 闭合」这一层——避免后续新增入口漏加 realpath 复核
    （F1 的根因正是 6 入口里写工具完全没做 realpath 复核）。返回拒绝原因或 None（放行）。
    """
    floor = _hard_floor_reason(target)
    if floor:
        return floor
    return _floor_check_resolved(root_abs, target, check_root=check_root)


#: 项目内 .env 类文件：写可读禁（Hermes 同语义），避免 agent 把用户凭据读回。
_ENV_READ_DENY = (".env", ".envrc")


def _env_read_denied(target):
    """项目内 .env 类文件禁止读取（写仍允许），返回 True 即拦。"""
    name = os.path.basename(os.path.normpath(target)).lower()
    if name in _ENV_READ_DENY:
        return True
    if name.startswith(".env."):
        return True
    return False


#: read_file 的行号区间参数。**行内与独占行都认**（`grep` 的解析器本来就这么做，
#: 两个工具不一致才是这次故障的根源）。
#:
#: 分隔符给到 空格/逗号/分号/换行 —— 模型实际输出的写法比工具说明里的示例更随意，
#: 收紧语法等于让模型在一个纯语法问题上反复试错，而每次失败的报错都把整串
#: 当成路径回显，模型只会去换路径分隔符（实测连试四次全失败）。
#:
#: 分隔符同时接受 `:` `：` `=` `＝`；参数名同时接受 `start` / `start_line` /
#: `startLine` / `start-line`。这是**把上一条修复贯彻到底**——只补 `:` 与
#: `start_line` 等于让模型把 `:` 敲成 `=` 就又掉回同一个坑，而那正是这一层
#: 要治的「文件明明在、报错说它不在」。实测三种死路形状完全一致。
_RE_READ_START = re.compile(
    r"(?:^|[\s,，;；])(?:start|start[_\-]?line)\s*(?:[:：=＝])\s*(\d+)", re.I)
_RE_READ_END = re.compile(
    r"(?:^|[\s,，;；])(?:end|end[_\-]?line)\s*(?:[:：=＝])\s*(\d+)", re.I)
#: 识别不出来的残留（`start=1`、`行1-55` 之类）——用于把语法问题与路径问题区分开。
#:
#: **`start_line:` / `end_line:` 是第四层**（前三层见下面 `_RE_PATH_KEY_LINE` 的说明）。
#: 实测（`wf-2c5612c6ee53`）：模型写
#: `read_file(path: project.godot, start_line: 6, end_line: 8)`，得到
#: `文件不存在：project.godot, start_line: 6, end_line: 8`
#: ——**那个文件明明就在那儿**，而报错断言它不存在。模型于是去找别的路径写法，
#: 连试四次全失败（与前三次同一形状）。
#:
#: 而重规划器给的任务里明写着「按行范围分段读取」——**它建议了一件工具做不到的事**。
#: 参数 schema 里写的是 `start`/`end`，但 `start_line:` 是同样自然的写法，
#: 而收紧语法等于让模型在一个纯语法问题上反复试错。所以两种都认。
#: 区间参数**认不出来**时残留在 path 里的片段——用于把语法问题与路径问题区分开。
#:
#: 这个正则的**唯一职责**是：宁可点名，也不让「参数写法不对」伪装成「文件不存在」。
#: 之前它只认两种形状（`start=1` / `行1-55`），于是实测还漏了四种：
#:
#:     path= b/a.gd start=1      → 文件不存在：path= b/a.gd      （键名的等号，见 _RE_PATH_KEY_LINE）
#:     path: b/a.gd start: abc   → 文件不存在：…start: abc        （值不是数字）
#:     path: b/a.gd 从1到5行      → 文件不存在：…从1到5行           （中文自然语言区间）
#:     path: b/a.gd start..5     → 文件不存在：…start..5          （省略号）
#:
#: 四种都撞在同一句报错上——**和这一层要治的病一模一样**，只是换了几种拼法。
#: 「放宽了主正则」不等于「覆盖了所有拼法」，所以这里按形状逐个补。
#: 误伤防护靠前导的「行首/空白/逗号」边界：真路径里的 `start` 不会独立成词
#: （`docs/start.md` 的 `start` 前面是 `/`、`start_v2.gd` 后面是 `_`），而中文那条
#: 靠 `从` 字——路径里出现 `从` 的概率与出现 `行1-55` 一样低。
_RE_READ_STRAY = re.compile(
    # ① 值不是数字：`start: abc` / `end= ?`
    r"(?:^|[\s,，;；])(?:start|end)(?:[_\-]?line)?\s*[:：=＝]\s*[^\s\d,，;；]"
    # ② 省略号区间：`start..5` / `start…5`
    r"|(?:^|[\s,，;；])(?:start|end)(?:[_\-]?line)?\s*(?:\.\.|\u2026)\s*\d+"
    # ③ 正常写法在主正则失配时的兜底（当前形状下主正则先吃，这里是安全网）
    r"|(?:^|[\s,，;；])(?:start|end)(?:[_\-]?line)?\s*(?:=|＝|：|:)\s*\d+"
    # ④ `行1-55` / `line 1-55` / `lines 1-55`
    r"|(?:^|[\s,，;；])(?:行|lines?)\s*\d+\s*[-—~]\s*\d+"
    # ⑤ 中文自然语言区间：`从1到5行`（不要求前导边界——`从` 本身就够罕见）
    r"|从\s*\d+\s*(?:到|至|~|-)\s*\d+\s*行", re.I)
#: 键名式路径参数：`path: a.gd` / `file: a.gd`。
#:
#: 这是同一个问题的**第三层**：上一轮修好了行内 start:/end:，模型随即改写成
#: `path: behaviors/player.gd start: 1 end: 55`，而 `path:` 这个**键名**没人剥——
#: 于是路径变成字面量 "path: behaviors/player.gd"，报「文件不存在」，
#: 而那个文件明明就在那儿。`grep`（_RE_GREP_PATH_LINE）与 `list_dir` 都能剥，
#: 只有 read_file 不能。
#:
#: 与其再加一条专用正则，不如让三个吃路径的工具对"路径该怎么写"保持一致，
#: 否则模型每修好一处就会在另一处撞墙（本次会话已连撞三层）。
#:
#: 注意 grep **故意**与这里不同：它的 `_RE_GREP_SCOPE_LINE` 只在 `i >= 1`（非首行）
#: 时才把 `path:` 当作用域，因为单行的 `path: TODO` 在 grep 里是要搜索的字面文本。
#: 那是另一个语义，不要为了"统一"而改grep——改了就搜不到含 "file: " 的正文。
#: 一致性由 tests/test_tool_arg_syntax_tolerance.py 的跨工具断言守住。
_RE_PATH_KEY_LINE = re.compile(r"^\s*(?:path|file|filename)\s*[:：=＝]\s*(.+?)\s*$", re.I)


def _strip_path_key(value: str) -> str:
    """剥掉 `path: ` / `file: ` 这样的键名前缀（整串只有一个键时）。

    只处理「键名 + 冒号 + 值」独占一行的形态，以及剥完区间参数后剩下的整串形态。
    不处理行内 `xxx, path: yyy` —— 那是 grep 的 scope 语义，由它自己的解析器负责。
    """
    match = _RE_PATH_KEY_LINE.match(value or "")
    return match.group(1).strip() if match else (value or "").strip()


def read_file(path):
    """读取代码库中的文件内容（path 为相对 code_root 的路径或文件名）。

    原生 function-calling 下 `path` / `start` / `end` 是**三个独立参数**
    （见 agent_runtime.tools._SCHEMA_OVERRIDES），不需要拼进一个字符串。
    文本协议下则用 `Action Input:` 传 `path: …`（可与 start/end 同行或分行）。
    两种形态都要认——模型实际两种都用过，而历史上三次翻车都是因为只认其中一种。

    同一区间在文件未变化时会返回一段短存根而不是重复内容（见 _unchanged_read_stub）。

    大文件默认只给前 12000 字；可在输入里附 `start: <1基行号>` 与可选 `end: <行号>`
    只看某个区间（如枚举/方法所在行段）。

    **区间参数三种写法都认**：独占一行（工具说明里的写法）、行内空格分隔、
    逗号/分号分隔。此前只认独占一行，而模型实际普遍写成行内
    （`player.gd start: 1 end: 55`），结果整串被当成文件名-> 「文件不存在」，
    且报错把整串原样回显，模型看不出是自己的语法问题，转而去换路径分隔符、
    连试四次全失败。实测（godot_sample 定位扣血逻辑）走的正是这条死路。
    对照：`grep` 的参数解析器本来就有行内处理（_RE_GREP_SCOPE_INLINE 等），
    两个工具不一致才是根源，所以这里按同一套宽松规则来。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    root_abs = os.path.normpath(root)
    start_line = end_line = None
    ms = _RE_READ_START.search(path)
    me = _RE_READ_END.search(path)
    cuts = []
    if ms:
        start_line = max(1, int(ms.group(1)))
        cuts.append(ms.start())
    if me:
        end_line = max(1, int(me.group(1)))
        cuts.append(me.start())
    if cuts:
        path = path[:min(cuts)]
    path = path.strip().strip(",，;；").strip()
    if start_line and end_line and end_line < start_line:
        end_line = start_line + 200
    # 剥掉 `path: ` / `file: ` 键名。与 grep / list_dir 同一套规则——
    # 模型写键名式参数是完全正常的（工具说明里其它工具都这么写）。
    path = _strip_path_key(path)
    # 参数没被识别出来（例如写成 start=1 / 行 1-55）。此时 path 里会残留这些片段，
    # 而「文件不存在：xxx start=1」这种报错会把语法问题伪装成路径问题——
    # 必须在这里点破，否则模型会去改路径/换分隔符，而真实原因是参数写法。
    leftover = _RE_READ_STRAY.search(path)
    target = os.path.normpath(path if os.path.isabs(path) else os.path.join(root_abs, path))
    # 与 code_root 无关的地板：凭据存储类 / NT 设备命名空间，读写都拦，无 override。
    floor = _hard_floor_reason(target)
    if floor:
        return floor
    if _env_read_denied(target):
        return "安全策略拒绝：.env 类凭据文件禁止读取（写可读禁，与 Hermes 一致）。"
    # 路径越界防护：只允许读取 code_root 目录内的文件
    if not (target == root_abs or target.startswith(root_abs + os.sep)):
        alt = os.path.normpath(os.path.join(root_abs, path.lstrip("./\\")))
        if os.path.isfile(alt) and (alt == root_abs or alt.startswith(root_abs + os.sep)):
            alt_floor = _hard_floor_reason(alt)
            if alt_floor:
                return alt_floor
            if _env_read_denied(alt):
                return "安全策略拒绝：.env 类凭据文件禁止读取（写可读禁，与 Hermes 一致）。"
            target = alt
        else:
            return f"拒绝访问：{path} 不在代码根目录内。"
    if not os.path.isfile(target):
        if leftover:
            # 错误文本要**短但不含糊**。Claude Code 在 limits.ts 里记着他们的实测：
            # 报错路径只花约 100 字节，而截断/重复返回内容要花上万 token。
            # 但"更短"不是唯一目标——早先那句「文件不存在：xxx start=1」就太短了，
            # 短到看不出是**参数写法**问题，模型于是去换路径分隔符、白试四次。
            # 所以目标是：**一句话说清错在哪 + 给出正确写法**，不含解释性铺垫。
            return ("区间参数未识别：%s。正确写法：`path start:20 end:45`"
                    "（空格/逗号/换行分隔）。" % leftover.group(0).strip())
        return f"文件不存在：{path}"
    # ③ symlink 闭合（审计 F1/F3 统一入口）：realpath 后既防越界（_path_within_real_root）
    # 也防凭据地板漏拦（_resolve_floor_checked 内含 _hard_floor_reason 复核 realpath）
    resolved = _resolve_floor_checked(root_abs, target)
    if resolved:
        return f"拒绝访问：{path} 经符号链接越界或被凭据地板拦截——{resolved}"
    # Godot 缓存和通用二进制资源不得用 errors="ignore" 当文本读取：
    # 这会静默丢字节，并让 Agent 误以为已核对完整文件内容。
    try:
        with open(target, "rb") as f:
            sample = f.read(8192)
        controls = sum(byte < 32 and byte not in (9, 10, 13) for byte in sample)
        is_binary = (os.path.splitext(target)[1].lower() in {".bin", ".scn"}
                     or b"\x00" in sample or controls > max(1, len(sample) // 100))
        if is_binary:
            parsed = read_data_file(target)
            return (f"=== {os.path.relpath(target, root_abs)}（只读二进制检查）===\n"
                    + json.dumps(parsed, ensure_ascii=False, default=str)[:12000])
    except (DataFormatError, OSError) as exc:
        return f"读取失败：{exc}"
    rel = os.path.relpath(target, root_abs)
    # 未变化短路：区间已被本次会话读过且文件此后未改，就返回短存根。
    # 放在二进制分支之后、真正读盘之前——存根只对文本读取有意义。
    _stub = _unchanged_read_stub(target, rel, start_line, end_line)
    if _stub:
        # 仍要登记 _READ_FILES：本次调用确实"读"过这个文件，
        # 否则 apply_edit 的"先读后写"护栏会误判成没读过。
        _read_store()[0].add(os.path.normcase(target))
        return "=== %s（未变化）===\n%s" % (rel, _stub)
    try:
        with open(target, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"读取失败: {e}"
    # 记录已读，供 apply_edit 的"先读后写"护栏使用
    _read_store()[0].add(os.path.normcase(target))
    if start_line:
        all_lines = content.splitlines(keepends=True)
        total = len(all_lines)
        if start_line > total:
            # 越界：此前输出「第 70-60 行」这种倒置区间且正文为空。
            # 短而具体：给出实际行数范围，让模型自己改。
            return (f"=== {rel}（共 {total} 行）===\n"
                    f"起始行 {start_line} 越界，合法范围 1-{total}。")
        if not end_line:
            end_line = start_line + 199  # 区间默认 200 行
        end_line = min(end_line, total)
        picked = all_lines[start_line - 1:end_line]
        shown = "".join(f"{i}: {ln}" for i, ln in enumerate(picked, start=start_line))
        out = f"=== {rel}（第 {start_line}-{end_line} 行，共 {total} 行）===\n{shown}"
        _note_read_range(target, start_line, end_line, len(out.encode("utf-8")))
        return out
    all_lines = content.splitlines(keepends=True)
    total = len(all_lines)
    if len(content) > 12000:
        # 抬头里的总行数按**完整文件**算，不是按截断后的片段——否则模型会把
        # 「这里只给了前 12000 字」误读成「文件就这么长」，而它同时又在结论里
        # 报一个自己数出来的行数。
        body = "".join(f"{i}: {ln}" for i, ln in enumerate(all_lines, start=1))
        shown = body[:12000]
        # 本次真正送达的**完整行数**：截断点多半落在某行中间，这里按换行数下取整，
        # 宁可少记一行也不高估覆盖（少记 → 那行顶多再真读一次，安全；
        #  多记 → 会把从没送达的尾段判成「已读过」，见下方注释）。
        last_shown_line = shown.count("\n")
        body = (shown
                + f"\n…（已截断，仅显示前 12000 字，共 {total} 行；"
                  "需要后续段落请用 start:/end: 指定行号）")
        out = f"=== {rel}（共 {total} 行，仅显示前 12000 字）===\n{body}"
        # 只登记**送达**的区间，不登记整文件。此前用 (None, None) → 折叠成
        # [(1, _FULL_FILE_END)]，等于宣称"全文件已读过"：后续任何 start:/end: 补读
        # 从未送达的尾段都会命中 `_unchanged_read_stub` 的假存根「已读过、不要重复
        # 读取」，模型被永久挡在正文之外、只能凭脑补作答（甚至谎称"来自真实读取"）。
        # 这违反 `_note_read_range` 自己的契约（记送达、不记请求）。
        if last_shown_line >= 1:
            _note_read_range(target, 1, last_shown_line, len(out.encode("utf-8")))
        # 首行就超 12000 字（极端单行文件）：一行都没送达，干脆不记覆盖，
        # 让后续任意区间都能真读。_READ_FILES 仍已在上面登记（供 apply_edit 护栏）。
        return out
    # 整文件读取**逐行编号**，与区间读统一形态。
    #
    # 实测（`wf-fd543f79c197`）：总行数补上后模型不再数错行数（报 53 / 49，与
    # 原文一致），但紧接着 `verify-constants` 的反思写下——
    #
    #     「结论中给出的行号在工具观察中并不存在 —— read_file 整文件读取返回的
    #       是裸正文，无行号，因此这些行号属于无依据的推测性标注」
    #
    # **那条指控是对的。** 而模型做的是「自己纠正上游的行号偏差」——它发现
    # 两份上游结论对同一段代码给了 `:31` 和 `:33` 两个值，因为两份都是各自
    # 数裸正文数出来的。
    #
    # ## 为什么这里推翻了上一轮「不加逐行行号」的克制
    #
    # 当时的理由是「判据靠有没有行号区分两种形态」。**那个理由是错的**：
    # 区分两种形态的目的是「知道能不能核验行号」，而不是「拒绝整文件读带行号」。
    # 拒绝的代价是判据永远无法核验整文件读的行号——而整文件读恰恰是工具说明里的
    # **推荐用法**，模型最常走的那条路。
    #
    # 前十层里九层是「已有的信息被丢掉」，这一层是「从来没有过」。补它比继续
    # 在判据上开豁免更根本：豁免只是让判据不去查，而给了行号它就能查。
    body = "".join(f"{i}: {ln}" for i, ln in enumerate(all_lines, start=1))
    out = f"=== {rel}（共 {total} 行）===\n{body}"
    _note_read_range(target, None, None, len(out.encode("utf-8")))
    return out


def inspect_data_file(arg):
    """识别并解析项目内的混乱数据文件，返回清洗后的结构化摘要。"""
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "尚未配置代码库根目录"}, ensure_ascii=False)
    raw = str(arg or "").strip()
    try:
        payload = json.loads(raw) if raw.startswith("{") else {"path": raw}
        path = str(payload.get("path") or "").strip()
        if not path:
            raise DataFormatError("path 不能为空")
        root_abs = os.path.abspath(root)
        target = os.path.abspath(path if os.path.isabs(path) else os.path.join(root_abs, path))
        floor = _hard_floor_reason(target)
        if floor:
            raise DataFormatError(floor)
        if _env_read_denied(target):
            raise DataFormatError(".env 类凭据文件禁止读取（写可读禁，与 Hermes 一致）。")
        if not (target == root_abs or target.startswith(root_abs + os.sep)):
            raise DataFormatError("路径越界")
        resolved = _resolve_floor_checked(root_abs, target)
        if resolved:
            raise DataFormatError(f"路径经符号链接越界或被凭据地板拦截——{resolved}")
        parsed = read_data_file(target)
        if isinstance(parsed.get("text"), str):
            parsed["text"] = parsed["text"][:16000]
        if isinstance(parsed.get("value"), (dict, list)):
            parsed["value"] = json.loads(json.dumps(parsed["value"], ensure_ascii=False, default=str))
        if isinstance(parsed.get("rows"), list):
            parsed["rows"] = parsed["rows"][:100]
        if isinstance(parsed.get("records"), list):
            parsed["records"] = parsed["records"][:100]
        parsed["relative_path"] = os.path.relpath(target, root_abs).replace(os.sep, "/")
        return json.dumps({"ok": True, "file": parsed}, ensure_ascii=False, default=str)[:24000]
    except (DataFormatError, OSError, ValueError, ImportError, json.JSONDecodeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def generate_data_file(arg):
    """让 Agent 在当前项目内生成并回读校验结构化数据文件。"""
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "尚未配置代码库根目录"}, ensure_ascii=False)
    try:
        payload = json.loads(str(arg or "").strip())
        if not isinstance(payload, dict):
            raise DataFormatError("参数必须是 JSON 对象")
        result = write_data_file(root, payload)
        result["relative_path"] = os.path.relpath(result["path"], os.path.abspath(root)).replace(os.sep, "/")
        return json.dumps(result, ensure_ascii=False, default=str)
    except (DataFormatError, OSError, ValueError, ImportError, json.JSONDecodeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


_RE_GREP_SCOPE_LINE = re.compile(r"^\s*path\s*[:：]\s*(.+?)\s*$", re.I)
_RE_GREP_SCOPE_INLINE = re.compile(r"\s*[,，]\s*path\s*[:：]\s*(.+?)\s*$", re.I)
_RE_GREP_PATTERN_KEY = re.compile(r"^\s*(?:pattern|regex|p)\s*[:：]\s*(.+)$", re.I)


def _strip_arg_quotes(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`":
        return text[1:-1].strip()
    return text


def read_external_file(path):
    """读取【项目外】白名单目录内的文件（受控越界读）。

    仅允许读取 DOCMIND_EXTERNAL_DIRS（分号分隔的绝对目录）之内的文件；其它路径一律拒绝。
    用于用户明确授权让 AI 访问代码库之外的资料（如另一个项目、下载目录里的文档）。
    输入 path 为文件绝对路径；返回文件内容（截断到 4000 字）。
    """
    if not external_access_high():
        return ("当前为安全模式（仅限项目内）。读取项目外文件需要在「模型设置」中"
                "切换为高权限模式（high）后方可执行。")
    dirs_raw = (os.getenv("DOCMIND_EXTERNAL_DIRS") or "").strip()
    if not dirs_raw:
        return ("未配置允许访问的外部目录。如需让 AI 读取项目外文件，请设置环境变量 "
                "DOCMIND_EXTERNAL_DIRS 为分号分隔的绝对路径白名单，例如 "
                "D:/OtherProject;D:/Downloads。未配置时出于安全默认拒绝越界读取。")
    allowed = [os.path.normpath(d) for d in re.split(r"[;；]", dirs_raw) if d.strip()]
    if not allowed:
        return "未配置允许访问的外部目录（DOCMIND_EXTERNAL_DIRS 为空）。"
    target = os.path.normpath(os.path.abspath(path))
    ok = any(target == d or target.startswith(d + os.sep) for d in allowed)
    if not ok:
        return f"拒绝访问：{path} 不在允许访问的外部目录白名单内（{', '.join(allowed)}）。"
    if not os.path.isfile(target):
        return f"文件不存在：{path}"
    try:
        with open(target, encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"读取失败: {e}"
    if len(content) > 4000:
        content = content[:4000] + "\n…（已截断，仅显示前 4000 字）"
    _READ_EXTERNAL_FILES.add(os.path.normcase(target))
    return f"=== {target} ===\n{content}"


# --- 受控越界读写：仅在「高权限模式」+ DOCMIND_EXTERNAL_DIRS 白名单内生效 ---
# 安全模式下以下三个工具一律拒绝（见 _resolve_external_path 的第一道门禁）。
# 设计意图：让用户可授权 AI 访问其它项目并增删改查，但路径被钉死在白名单目录，
# 既不会误伤系统目录，也不会因 mode 误开而"无限制越界"。
_READ_EXTERNAL_FILES = set()  # 本次会话内已用 read_external_file 读过的文件（整体重写护栏）


def _resolve_external_path(path):
    """解析受控越界路径：返回 (target, err)。err 非空即拒绝原因。

    门禁链：① 必须处于高权限模式（external_access_high）；② 必须配置
    DOCMIND_EXTERNAL_DIRS 白名单；③ 目标必须落在白名单目录内。任一不满足返回 err。
    """
    if not external_access_high():
        return None, ("当前为安全模式（仅限项目内）。对其他项目增删改查需要在「模型设置」中"
                      "切换为高权限模式（high）后方可执行。")
    dirs_raw = (os.getenv("DOCMIND_EXTERNAL_DIRS") or "").strip()
    if not dirs_raw:
        return None, ("未配置允许越界的外部目录白名单（DOCMIND_EXTERNAL_DIRS 为空）。"
                      "高权限模式下也必须在环境变量中显式列出允许访问的目录，"
                      "例如 D:/OtherProject;D:/Downloads。")
    allowed = [os.path.normpath(d) for d in re.split(r"[;；]", dirs_raw) if d.strip()]
    target = os.path.normpath(os.path.abspath(path))
    floor = _hard_floor_reason(target)
    if floor:
        # 即便目标落在外部白名单目录内，凭据存储类 / NT 设备命名空间仍硬拦（无 override）。
        return None, floor
    ok = any(target == d or target.startswith(d + os.sep) for d in allowed)
    if not ok:
        return None, f"拒绝访问：{path} 不在允许越界的目录白名单内（{', '.join(allowed)}）。"
    # ③ symlink 闭合（审计 F1/F3 统一入口）：外部白名单路径只重跑地板（check_root=False），
    # 不闭合回 code_root——外部本就是受控越界，但凭据地板仍硬拦。
    resolved = _resolve_floor_checked(None, target, check_root=False)
    if resolved:
        return None, resolved
    return target, None


def create_external_file(arg):
    """在白名单目录内【新建】文件（不覆盖已有文件），受控越界写。格式同 create_file。

    门禁：高权限模式 + DOCMIND_EXTERNAL_DIRS 白名单。输入 path: <绝对路径>，
    new_text: <内容>。护栏：不覆盖已存在；200KB 上限；.py 语法校验；「人工确认」开启时暂存。
    """
    path, _, content = _parse_edit_input(arg)
    if not path:
        return "参数缺失：请提供 path: <文件绝对路径> 与 new_text: <文件内容>。"
    if content is None:
        return "参数缺失：请提供 new_text: <文件内容（可多行）>。"
    target, err = _resolve_external_path(path)
    if err:
        return err
    if os.path.exists(target):
        return f"文件已存在：{path}（create_external_file 不覆盖已有文件；修改请用 edit_external_file）。"
    if len(content.encode("utf-8", "ignore")) > _APPLY_MAX_BYTES:
        return f"拒绝写入：新文件大小超过上限（{_APPLY_MAX_BYTES // 1024}KB）。"
    ext = os.path.splitext(target)[1].lower()
    if ext == ".py":
        import tempfile
        import py_compile

        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(content)
                tmp = tf.name
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            return f"语法校验失败，已取消创建：\n{e.msg}"
        except Exception as e:  # noqa: BLE001
            return f"语法校验异常，已取消创建：{e}"
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    if _edit_confirm_on():
        pid = stage_edit("create_file", target, path, None, content,
                         "新建外部文件：\n" + (content[:2000] + ("…" if len(content) > 2000 else "")))
        return f"待人工确认 #{pid}（未写入）。确认后才会真正创建文件。\n新建外部文件：{path}"
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return f"创建目录失败: {e}"
    try:
        with _file_lock(target):
            if os.path.exists(target):
                return "并行创建冲突：目标文件已被另一个代理创建，请改用 apply_edit。"
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:  # noqa: BLE001
        return f"写入失败: {e}"
    _READ_EXTERNAL_FILES.add(os.path.normcase(target))
    return f"已在白名单目录内创建 {path}（{len(content.encode('utf-8', 'ignore'))} 字节）。"


def edit_external_file(arg):
    """在白名单目录内修改【已存在】文件，受控越界写。格式同 apply_edit。

    门禁：高权限模式 + 白名单。两种用法：
      · 局部替换：path + old_text + new_text（唯一定位，匹配 0/>1 处拒绝）；
      · 整体重写：path + new_text（须先用 read_external_file 读过该文件确认内容）。
    护栏：200KB 上限；.py 语法校验失败回滚；「人工确认」开启时暂存。
    """
    path, old_text, new_text = _parse_edit_input(arg)
    if not path:
        return "参数缺失：请提供 path: <文件绝对路径> 与 new_text: <新内容>。"
    if new_text is None:
        return "参数缺失：请提供 new_text: <新内容>（局部替换还需 old_text: <旧片段>）。"
    target, err = _resolve_external_path(path)
    if err:
        return err
    if not os.path.isfile(target):
        return f"文件不存在：{path}（edit_external_file 只修改已存在文件）。"
    try:
        with open(target, encoding="utf-8", errors="ignore") as f:
            old_content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"读取原文件失败: {e}"
    if old_text is not None:
        if old_text == "":
            return "old_text 为空，无法确定替换范围（整体重写请省略 old_text；局部替换请提供精确片段）。"
        cnt = old_content.count(old_text)
        if cnt == 0:
            return "安全限制：未找到 old_text 的匹配，可能内容已变化。请重新 read_external_file 确认当前内容后再改。"
        if cnt > 1:
            return f"安全限制：old_text 匹配到 {cnt} 处，存在歧义。请提供更精确的 old_text（含前后上下文）。"
        new_content = old_content.replace(old_text, new_text, 1)
    else:
        if os.path.normcase(target) not in _READ_EXTERNAL_FILES:
            return ("安全限制：整体重写前请先调用 read_external_file 读取该文件确认内容，"
                    "或改用 old_text 参数做局部安全替换。")
        new_content = new_text
    if len(new_content.encode("utf-8", "ignore")) > _APPLY_MAX_BYTES:
        return f"拒绝写入：新文件大小超过上限（{_APPLY_MAX_BYTES // 1024}KB）。"
    ext = os.path.splitext(target)[1].lower()
    if ext == ".py":
        import tempfile
        import py_compile

        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(new_content)
                tmp = tf.name
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            return f"语法校验失败，已取消写入（原文件未改动）：\n{e.msg}"
        except Exception as e:  # noqa: BLE001
            return f"语法校验异常，已取消写入（原文件未改动）：{e}"
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    nbytes = len(new_content.encode("utf-8", "ignore"))
    summary = _diff_summary(old_content, new_content)
    if _edit_confirm_on():
        pid = stage_edit("apply_edit", target, path, old_content, new_content, summary)
        return f"待人工确认 #{pid}（未写入）。确认后才会真正修改文件。\n{summary}"
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write(new_content)
    except Exception as e:  # noqa: BLE001
        return f"写入失败: {e}"
    return f"已写入白名单目录内文件 {path}（{nbytes} 字节）。\n{summary}"


def delete_external_file(arg):
    """删除白名单目录内的【文件】（危险操作，受控越界删）。

    门禁：高权限模式 + 白名单。输入 path: <绝对路径> 且必须含 `confirm: yes`
    明确确认（防误删）。只删文件不删目录。「人工确认」开启时改为暂存待批准。
    """
    # delete 是单行命令式输入（path: 一行 + confirm: yes 一行），用独立解析避免
    # `confirm:` 被 _parse_edit_input 误并入 path 值（它只认 path/old_text/new_text）。
    m = re.search(r"(?im)^\s*path\s*:\s*(.+?)\s*$", arg or "")
    path = m.group(1).strip() if m else ""
    if not path:
        return "参数缺失：请提供 path: <文件绝对路径> 以及 confirm: yes。"
    target, err = _resolve_external_path(path)
    if err:
        return err
    if not re.search(r"(?im)^\s*confirm\s*:\s*yes\b", arg or ""):
        return "删除是危险操作：请在输入中加入 `confirm: yes` 以明确确认删除。"
    if os.path.isdir(target):
        return f"拒绝：{path} 是目录，delete_external_file 只删除单个文件。"
    if not os.path.isfile(target):
        return f"文件不存在：{path}"
    if _edit_confirm_on():
        pid = stage_edit("delete_external_file", target, path, None, "", f"删除外部文件：{path}")
        return f"待人工确认 #{pid}（未删除）。确认后才会真正删除文件：{path}"
    try:
        os.remove(target)
    except Exception as e:  # noqa: BLE001
        return f"删除失败: {e}"
    return f"已删除白名单目录内文件 {path}。"


def grep(pattern):
    """在代码库中按正则搜索文本/符号，返回匹配的文件路径与行号。

    可在输入末尾用 path: 限定搜索范围（相对 code_root 的文件或目录），避免全仓扫描：
      某正则
      path: app/src/main/java/.../A.java
    或一行内：某正则, path: app/src/main/java/...（目录）
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    # 解析可选 path: 作用域（独立行优先），剩余文本才是正则
    scope_rel = None
    kept = []
    for i, ln in enumerate((pattern or "").splitlines()):
        ms = _RE_GREP_SCOPE_LINE.match(ln)
        if ms and i >= 1:
            scope_rel = _strip_arg_quotes(ms.group(1))
        else:
            kept.append(ln)
    pattern = "\n".join(kept).strip()
    mi = _RE_GREP_SCOPE_INLINE.search(pattern)
    if mi:
        scope_rel = _strip_arg_quotes(mi.group(1))
        pattern = pattern[:mi.start()].strip()
    mp = _RE_GREP_PATTERN_KEY.match(pattern)
    if mp and "\n" not in pattern:
        pattern = _strip_arg_quotes(mp.group(1))
    pattern = _clean_symbol(pattern)
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"正则错误: {e}"
    root_abs = os.path.normpath(root)

    def _resolve_scope(rel):
        target = os.path.normpath(os.path.join(root_abs, rel))
        if not (target == root_abs or target.startswith(root_abs + os.sep)):
            alt = os.path.normpath(os.path.join(root_abs, rel.lstrip("./\\")))
            if os.path.exists(alt) and (alt == root_abs or alt.startswith(root_abs + os.sep)):
                target = alt
            else:
                return None, f"拒绝访问：{rel} 不在代码根目录内。"
        if not os.path.exists(target):
            return None, f"路径不存在：{rel}"
        return target, None

    scope_abs = None
    if scope_rel:
        scope_abs, err = _resolve_scope(scope_rel)
        if err:
            return err

    # 复用 glob 的**同一套**剪枝口径：`_SKIP_DIRS` 全局不扫，且只在 `.docmind/` 直接
    # 下一层跳过 DocMind 状态目录（project_stages / projects 等整仓影子副本），产物
    # 目录（Godot Web 导出的 `web/` 等）照旧可见。此前 grep 只按 `_SKIP_DIRS` 过滤，
    # 会一头扎进 `.docmind/projects/<id>/headcheck/…` 这类影子副本——和 glob 口径不一致，
    # 还命中过期副本。（`code_intel` 不引 tools、tools 这里惰性引它，无循环。）
    from agent_runtime import code_intel as _ci

    pruned_state: set[str] = set()

    def _iter_candidate_files():
        if scope_abs and os.path.isfile(scope_abs):
            if os.path.splitext(scope_abs)[1].lower() in _CODE_EXT:
                yield scope_abs
            return
        for dp, dns, fns in os.walk(scope_abs or root_abs, followlinks=False):
            rel_dir = os.path.relpath(dp, root_abs).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
            for d in _ci._prune(dns, rel_dir):
                # 只披露 `.docmind/` 下的状态目录（与 glob 一致）：`_SKIP_DIRS` 的通用项
                # （.git / .venv / node_modules…）是"任何项目都不扫"，不该被说成"DocMind 状态"。
                if (d in _ci._DOCMIND_STATE_DIRS
                        or d.startswith(_ci._DOCMIND_STATE_PREFIXES)):
                    pruned_state.add(d)
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in _CODE_EXT:
                    continue
                yield os.path.join(dp, fn)

    _HIT_CAP = 40
    hits = []
    skipped_big = 0
    truncated = False
    for fp in _iter_candidate_files():
        if os.path.getsize(fp) > 2_000_000:
            skipped_big += 1
            continue
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        hits.append(f"{os.path.relpath(fp, root_abs)}:{i}: {line.rstrip()}")
                        if len(hits) >= _HIT_CAP:
                            break
        except Exception:  # noqa: BLE001
            pass
        if len(hits) >= _HIT_CAP:
            truncated = True
            break

    # 如实交代"我没看全 / 没看哪里"——三处都必须显式带回，否则会被读成完整结论：
    #   · 触顶：命中数达到 40 就截断，实测模型把这 40 条当全部，把真值 196 报成 194
    #     还自称"准确总数"。不标注 = 默许这种过度声称。
    #   · 跳过大文件：>2MB 直接不搜，不说的话"没命中"其实是"没看"。
    #   · 被剪的 `.docmind` 状态目录：与 glob 同一条教训——"没有命中"不等于"这里没有"。
    notes = []
    if truncated:
        notes.append("已达 %d 条上限并**截断**，实际匹配可能更多——不要把这 %d 条当全部、"
                     "也不要据此统计总数；要精确计数请缩小 path: 范围或换更具体的正则。"
                     % (_HIT_CAP, _HIT_CAP))
    if skipped_big:
        notes.append("另有 %d 个 >2MB 的文件未搜索。" % skipped_big)
    if pruned_state:
        notes.append("已跳过 DocMind 状态目录（产物目录仍会搜索）：.docmind/%s。"
                     % "、.docmind/".join(sorted(pruned_state)))
    note_block = ("\n（提示：" + "；".join(notes) + "）") if notes else ""

    if not hits:
        scope_note = f"（范围：{scope_rel}）" if scope_rel else ""
        return f"代码库中未匹配到：{pattern}{scope_note}{note_block}"
    return "\n".join(hits[:_HIT_CAP]) + note_block


# ---------------------------------------------------------------------------
# 受控写工具 apply_edit：让 Agent 能按用户指令修改 code_root 内的已存在文件，
# 但绝不越界、绝不误删、绝不写坏 .py。护栏分四层：
#   1) 路径沙箱：复用 _resolve_in_root，越界直接拒（与 read_file 同逻辑）。
#   2) 先读后写：整体重写时必须已在本次会话 read_file 过该文件；
#      或提供 old_text 做"精确局部替换"（更安全，推荐）。
#   3) 体积上限：新文件 > 200KB 拒写。
#   4) .py 语法校验：写入前 py_compile 编译，失败回滚原文件并报告。
# ---------------------------------------------------------------------------
_APPLY_MAX_BYTES = 200 * 1024  # 单文件写入上限 200KB


def _parse_edit_input(inp):
    """解析 apply_edit 的 Action Input（多行 keyed 格式），返回 (path, old_text, new_text)。

    约定格式（字段顺序无所谓，old_text / new_text 的值可写在 key: 同行或下一行，且可多行）：
        path: <相对或绝对路径>
        old_text: <可选：要被替换的精确旧片段>
        new_text: <替换后的新内容（可多行）>
    解析策略：定位三个字段标记（行首 `key:`），每个字段的值 = 标记之后到下一个字段
    标记（或文本末尾）之间的内容。只去掉字段标记紧跟的那个换行分隔符；最后一个字段
    （通常是 new_text/文件内容）保留其尾部换行，不丢失文件内容。这样无论是同行值还是
    多行值都稳。
    """
    inp = (inp or "").lstrip("\n")
    keys = ["path", "old_text", "new_text"]
    spans = []
    for key in keys:
        for m in re.finditer(r"^\s*" + key + r"\s*:\s*", inp, re.M):
            spans.append((m.start(), m.end(), key))
    spans.sort()
    fields = {}
    for i, (s, e, key) in enumerate(spans):
        val_end = spans[i + 1][0] if i + 1 < len(spans) else len(inp)
        val = inp[e:val_end]
        # 去掉标记后紧跟的那个换行分隔符（key: 与值换行的情形）
        if val.startswith("\n"):
            val = val[1:]
        # 非最后一个字段：其尾部换行是字段间分隔符，应剥掉（避免 old_text 多带空行匹配失败）
        # 最后一个字段（通常是 new_text/文件内容）：保留尾部换行，不丢失文件内容
        if val_end < len(inp):
            val = val.rstrip("\n")
        fields[key] = val
    return fields.get("path"), fields.get("old_text"), fields.get("new_text")


def _diff_summary(a, b):
    """生成简短 unified-diff 摘要（最多 30 行变更），用于回显给 Agent 与用户。"""
    if a == b:
        return "（内容无变化）"
    import difflib

    a_lines = a.splitlines()
    b_lines = b.splitlines()
    diff = list(difflib.unified_diff(a_lines, b_lines, lineterm="", n=1))
    body = [d for d in diff if not d.startswith(("---", "+++"))]
    if len(body) > 30:
        body = body[:30] + [f"...（共 {len(body)} 行变更，已截断显示前 30 行）"]
    return "Diff:\n" + "\n".join(body)


# ---------------------------------------------------------------------------
# 待确认修改暂存：当「人工确认」模式开启时，apply_edit / create_file 不直接写盘，
# 而是校验通过后暂存到内存，返回 pending id + diff，由人工在界面上确认/拒绝。
# ---------------------------------------------------------------------------
_PENDING_EDITS = {}       # id -> {kind, target, rel, old_content, new_content, diff}
_PENDING_LOCK = threading.Lock()
_pending_seq = [0]


def _edit_confirm_on():
    return edit_confirm_enabled()


def stage_edit(kind, target, rel, old_content, new_content, diff):
    """暂存一次已校验但未落盘的修改，返回 pending id。"""
    with _PENDING_LOCK:
        _pending_seq[0] += 1
        pid = str(_pending_seq[0])
        _PENDING_EDITS[pid] = {
            "kind": kind,            # "apply_edit" | "create_file"
            "target": target,        # 绝对路径
            "rel": rel,
            "old_content": old_content,
            "new_content": new_content,
            "diff": diff,
        }
        return pid


def list_pending_edits():
    with _PENDING_LOCK:
        return [
            {
                "id": pid,
                "kind": e["kind"],
                "path": e["rel"],
                "diff": e["diff"],
                "size": len(e["new_content"].encode("utf-8", "ignore")),
            }
            for pid, e in _PENDING_EDITS.items()
        ]


def confirm_edit(pid):
    """落盘一次已暂存的修改；成功返回 (True, 摘要)，失败返回 (False, 原因)。

    若失败原因是「暂存内容已失效」（文件被改动/删除/已被创建），会同时丢弃该待确认项，
    避免界面残留一个永远无法确认的条目；仅「写入 I/O 错误」会保留以便重试。
    """
    with _PENDING_LOCK:
        e = _PENDING_EDITS.get(pid)
    if not e:
        return False, f"未找到待确认修改 #{pid}（可能已确认/拒绝或重启后失效）。"
    target, kind = e["target"], e["kind"]

    def _discard(reason):
        with _PENDING_LOCK:
            _PENDING_EDITS.pop(pid, None)
        return False, reason

    try:
        if kind == "create_file":
            with _file_lock(target):
                if os.path.exists(target):
                    return _discard(f"目标文件已存在：{e['rel']}（暂存后文件被创建，为避免覆盖已取消）。")
                parent = os.path.dirname(target)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                with open(target, "w", encoding="utf-8") as f:
                    f.write(e["new_content"])
            _read_store()[0].add(os.path.normcase(target))
        elif kind == "delete_external_file":
            if not os.path.isfile(target):
                return _discard(f"目标文件已不存在：{e['rel']}（暂存后已被删除，已取消）。")
            os.remove(target)
        else:  # apply_edit
            with _file_lock(target):
                if not os.path.isfile(target):
                    return _discard(f"目标文件已不存在：{e['rel']}")
                # 防「暂存后被改动」：内容不一致则拒绝，避免覆盖他人在暂存期间的修改
                try:
                    with open(target, encoding="utf-8", errors="ignore") as f:
                        cur = f.read()
                except Exception as ex:  # noqa: BLE001
                    return False, f"读取目标失败: {ex}"
                if cur != e["old_content"]:
                    return _discard("目标文件在暂存后被修改，内容已变化；请重新 read_file 后再改。")
                with open(target, "w", encoding="utf-8") as f:
                    f.write(e["new_content"])
            _read_store()[0].add(os.path.normcase(target))
    except Exception as ex:  # noqa: BLE001
        return False, f"写入/删除失败: {ex}"
    with _PENDING_LOCK:
        _PENDING_EDITS.pop(pid, None)
    if kind == "delete_external_file":
        return True, f"已删除 {e['rel']}。"
    nbytes = len(e["new_content"].encode("utf-8", "ignore"))
    return True, f"已应用修改 {e['rel']}（{nbytes} 字节）。"


def reject_edit(pid):
    with _PENDING_LOCK:
        return bool(_PENDING_EDITS.pop(pid, None))


def clear_read_files():
    """清空「已读文件」记录（重置代码库时调用，避免旧项目的读记录跨项目残留）。"""
    _read_store()[0].clear()
    _read_store()[1].clear()


# ---------------------------------------------------------------------------
# 未变化文件的短存根（借鉴 Claude Code 的 FILE_UNCHANGED_STUB）
# ---------------------------------------------------------------------------
#: 读过的文件 -> (变更指纹, 已覆盖的合并行区间列表)
#:
#: 指纹用 ``(st_mtime_ns, st_size)``：写入方改动文件必然改 mtime，
#: 所以「指纹相同」足以判定内容未变，不需要算哈希（那是额外一次全文件读）。
#:
#: 为什么记**区间**而不是整文件：实际浪费发生在「先整文件读一遍、再读某几个区间」
#: 这个组合上（本次会话实测：完整读过 enemy.gd/player.gd/hud.gd 之后，
#: 又按 6-10 / 40-46 / 36-45 / 16-30 重读一遍）。只按整文件记，这些重读一个都拦不住。
#:
#: 已知局限：若更早那次读取的内容已被上下文预算裁掉，存根会让模型去引用一个
#: 它已经看不到的内容。Claude Code 承担同样的风险；这里如实标注而不假装没有。
_READ_COVERAGE: dict[str, tuple[tuple, list]] = {}

#: 关掉存根：DOCMIND_READ_STUB=0
_READ_STUB_ENABLED = os.getenv("DOCMIND_READ_STUB", "1").strip() not in ("0", "false", "no")

#: 用一个够大的上界代表「整文件」，避免为整文件读单独维护一套状态。
_FULL_FILE_END = 1 << 30

#: 存根只在它**比被替代的内容更短**时才发。
#:
#: 实测：对 10 行的小文件，存根 152 字节而内容只有 83 字节——发存根反而更费。
#: 小文件的内容本来就便宜，重读的代价远低于解释"为什么这里没给你内容"。
_STUB_MIN_SAVING = 400


def _read_stamp(target):
    """文件变更指纹；取不到就返回 None（视为不可判定，不发存根）。"""
    try:
        st = os.stat(target)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _merge_ranges(ranges):
    """把行区间合并成不重叠的升序列表。"""
    out = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _range_covered(ranges, start, end):
    """``[start, end]`` 是否被 ``ranges`` 完全覆盖。"""
    return any(lo <= start and end <= hi for lo, hi in ranges)


def _unchanged_read_stub(target, rel, start_line, end_line):
    """若该区间已在本次会话读过、且文件此后未变，返回一段短存根；否则 ``None``。

    存根刻意保持**短**（Claude Code 的实测结论：报错/存根路径只花约 100 字节，
    而重复返回内容要花上万 token），并且**明确指向那次已有的读取**——
    它同时是一句「别再读了」的劝阻，这是治"验证躲避"的另一半。
    """
    if not _READ_STUB_ENABLED:
        return None
    entry = _read_store()[1].get(os.path.normcase(target))
    if not entry:
        return None
    stamp, ranges, served = entry[0], entry[1], entry[2]
    if stamp != _read_stamp(target):
        return None
    # 内容太小的话，存根比内容还长——那就不是节省。直接重读。
    if served < _STUB_MIN_SAVING:
        return None
    lo = start_line or 1
    hi = end_line if end_line else _FULL_FILE_END
    if not _range_covered(ranges, lo, hi):
        return None
    if start_line and end_line:
        where = "第 %d-%d 行" % (start_line, end_line)
    elif start_line:
        where = "第 %d 行起" % start_line
    else:
        where = "整份文件"
    # 文件名已经在上面的 `=== <rel>（未变化）===` 抬头里说过，这里不重复；
    # 「未变化」也不重复强调。Claude Code 的存根约 100 字节，我们对齐这个量级——
    # 存根的价值随文件增大而增大，但对小文件它本来就省不了多少。
    #
    # ②：把"直接引用"这句话做成**可证伪**的条件句。存根代码只看得到
    # (stamp, ranges, served_bytes)——它**无从知道**那次 observation 是否还留在
    # 当前上下文窗口里。① 已封死"截断读误记全覆盖"（送达之外的区间会真读），
    # 剩下的唯一残余是**更早已送达的正文被上下文预算裁走**：此时存根若仍无
    # 条件地喊"不要重复读取"，模型会被逼去 python_exec 兜底（实测单样本 42 步）。
    # 所以这里明确把成立条件交给模型自核——"你此刻是否真看得到"——看不到就按行号
    # 重读，以真正见到的内容为准。既保住省 token 的初衷（看得到时仍别重读），
    # 又不再把一个它已看不到的东西谎称"直接引用即可"。
    return ("%s已在本次对话中读过、且此后未改动；**前提是你此刻仍能在上下文里看到"
            "那段正文**——看得到就直接引用、**不要重复读取**；若它已被上下文预算裁走"
            "（你其实看不到它了），按行号重读即可，以你真正见到的内容为准。" % where)


def _note_read_range(target, start_line, end_line, served_bytes=0):
    """记录本次真正**送达**的行区间（不是请求的区间——请求可能超出文件长度，
    按请求记录会高估覆盖，让后续本该真读的行被存根挡掉）。

    ``served_bytes`` 记本次响应字节数，供存根判断"它比内容更短吗"。
    """
    key = os.path.normcase(target)
    stamp = _read_stamp(target)
    if stamp is None:
        return
    lo = start_line or 1
    hi = end_line if end_line else _FULL_FILE_END
    if hi == _FULL_FILE_END and lo == 1:
        ranges = [(1, _FULL_FILE_END)]
    else:
        old = _read_store()[1].get(key)
        ranges = _merge_ranges((old[1] if old else []) + [(lo, hi)])
    prev = _read_store()[1].get(key)
    best = max(served_bytes, prev[2] if prev and len(prev) > 2 else 0)
    _read_store()[1][key] = (stamp, ranges, best)


def read_served_ranges():
    """当前会话**实际送达**的行覆盖快照：``{normcase(绝对路径): [(lo, hi), ...]}``。

    这是 `_note_read_range` 攒下来的真值——记的是**送达**、不是请求（见其文档）。
    输出审计拿它做「来源可溯」核验：结论若声称某段正文"来自真实读取"，就去这里查
    那段区间本轮**是否真送达过**。根因①修好后，截断整读只记 `(1, last_shown_line)`，
    这里的数据因此可信：没送达的尾段不会被谎记成已覆盖。

    纯只读，不改动任何状态；审计侧拿不到（键对不齐）时应判「不可判定」而非 FAIL，
    所以这里如实返回当前 store 的内容即可，不做补全。
    """
    out = {}
    for key, entry in (_read_store()[1] or {}).items():
        try:
            ranges = entry[1]
        except (TypeError, IndexError):
            continue
        if ranges:
            out[key] = list(ranges)
    return out


def apply_edit(arg):
    """受控修改代码库中的【已存在】文件。详见 _parse_edit_input 的输入格式。

    两种用法：
      · 局部安全替换：提供 path + old_text（要匹配的精确旧片段）+ new_text；
        工具在文件中唯一匹配处替换，匹配 0 处或 >1 处都会拒绝以避免歧义/误改。
      · 整体重写：只提供 path + new_text（不带 old_text），但前提是你已用
        read_file 读取过该文件（确认过当前内容）。
    护栏：只能改 code_root 内已存在文件，不能新建、不能越界；写入后 .py 会做语法
    校验，失败自动回滚。返回：已写入 / 失败原因 / diff 摘要。
    """
    path, old_text, new_text = _parse_edit_input(arg)
    if not path:
        return "参数缺失：请提供 path: <文件路径> 与 new_text: <新内容>。"
    if new_text is None:
        return "参数缺失：请提供 new_text: <新内容>（若做局部替换，还需 old_text: <旧片段>）。"

    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"

    target, root_abs, reason = _resolve_in_root(path)
    if target is None:
        return reason or f"拒绝写入：{path} 不在代码根目录内（禁止越界写）。"
    if not _region_write_allowed(target):
        return "拒绝写入：该路径属于已配置分区，请使用 dev_region_edit 以确保分区边界和先读后写护栏。"
    if not os.path.isfile(target):
        return f"文件不存在：{path}（apply_edit 只修改已存在文件，不会新建文件）。"

    # 决策 7 第二层：落盘前反查 lane 归属；单用户（无 lane 上下文）零开销透传。
    from agent_runtime import lanes
    _rel = os.path.relpath(target, root_abs)
    _gate = lanes.write_gate(root_abs, _rel)
    if _gate:
        return _gate

    try:
        with open(target, encoding="utf-8", errors="ignore") as f:
            old_content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"读取原文件失败: {e}"

    # 决定新内容 + 局部/整体模式
    if old_text is not None:
        if old_text == "":
            return "old_text 为空，无法确定替换范围（整体重写请省略 old_text；局部替换请提供精确片段）。"
        cnt = old_content.count(old_text)
        if cnt == 0:
            return "安全限制：未找到 old_text 的匹配，可能内容已变化。请重新 read_file 确认当前内容后再改。"
        if cnt > 1:
            return f"安全限制：old_text 在文件中匹配到 {cnt} 处，存在歧义。请提供更精确的 old_text（含前后上下文）以唯一定位。"
        new_content = old_content.replace(old_text, new_text, 1)
    else:
        # 整体重写：必须先 read_file 确认过当前内容（防 Agent 凭空改写）
        if os.path.normcase(target) not in _read_store()[0]:
            return ("安全限制：整体重写前请先调用 read_file 读取该文件确认当前内容，"
                    "或改用 old_text 参数做局部安全替换。")
        new_content = new_text

    # 护栏：单文件体积上限
    if len(new_content.encode("utf-8", "ignore")) > _APPLY_MAX_BYTES:
        return f"拒绝写入：新文件大小超过上限（{_APPLY_MAX_BYTES // 1024}KB）。"

    # 护栏：.py 语法校验（失败回滚，原文件不动）
    ext = os.path.splitext(target)[1].lower()
    if ext == ".py":
        import tempfile
        import py_compile

        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(new_content)
                tmp = tf.name
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            return f"语法校验失败，已取消写入（原文件未改动）：\n{e.msg}"
        except Exception as e:  # noqa: BLE001
            return f"语法校验异常，已取消写入（原文件未改动）：{e}"
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    rel = os.path.relpath(target, root_abs)
    nbytes = len(new_content.encode("utf-8", "ignore"))
    summary = _diff_summary(old_content, new_content)

    # 「人工确认」模式：只暂存，不落盘，返回 diff 等人工批准
    if _edit_confirm_on():
        pid = stage_edit("apply_edit", target, rel, old_content, new_content, summary)
        return f"待人工确认 #{pid}（未写入）。确认后才会真正修改文件。\n{summary}"

    # 写回
    try:
        with _file_lock(target):
            # Parallel subagents may have written the file after the initial
            # read. Re-check the base before writing to fail closed.
            with open(target, encoding="utf-8", errors="ignore") as f:
                current = f.read()
            if current != old_content:
                return "并行修改冲突：文件在本次编辑准备后已发生变化，请重新 read_file 后再改。"
            with open(target, "w", encoding="utf-8") as f:
                f.write(new_content)
            lanes.record_write(
                root_abs, lanes.get_current_lane(), rel, "modify",
                hashlib.sha256(old_content.encode("utf-8")).hexdigest())
    except Exception as e:  # noqa: BLE001
        return f"写入失败: {e}"

    return f"已写入 {rel}（{nbytes} 字节，路径沙箱校验通过）。\n{summary}"


def _split_edit_blocks(text):
    """把多文件批量编辑输入切成若干编辑块。

    约定：块之间用单独一行 `---` 分隔；块内格式与 apply_edit 完全一致
    （path / old_text / new_text）。若某个分段不以 `path:` 开头，说明那行 `---`
    其实是文件内容的一部分（Markdown 分隔线等），会把该段并回上一块，
    避免误切导致写入内容损坏。
    """
    raw = (text or "").replace("\r\n", "\n")
    blocks = []
    for part in raw.split("\n---\n"):
        if not part.strip():
            continue
        starts_block = re.match(r"\s*path\s*[:：]", part) is not None
        if not starts_block:
            if blocks:
                blocks[-1] = blocks[-1] + "\n---\n" + part
            continue  # 首段是说明文字时直接忽略
        blocks.append(part)
    return blocks


def dev_apply_edits(arg):
    """一次提交多个文件的局部替换（原子：要么全改，要么一个都不改）。

    输入：多个编辑块，块之间用单独一行 `---` 分隔；块内格式与 apply_edit 一致：
        path: <文件>
        old_text: <精确旧片段>
        new_text: <新片段>

    护栏：逐块复用 apply_edit 的越界 / 分区写 / old_text 唯一匹配 / 体积 / .py 语法
    校验；任一失败则【不写入任何文件】。全部预检通过后再统一落盘，落盘中出错会
    回滚已写文件。批量编辑强制要求 old_text（整体重写请用 apply_edit 单文件走）。
    """
    blocks = _split_edit_blocks(arg)
    from agent_runtime import lanes
    if not blocks:
        return ("未解析到任何编辑块。格式：每块 `path:` + `old_text:` + `new_text:`，"
                "块之间用单独一行 `---` 分隔。")
    if not _get_code_root():
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"

    plans = []
    errors = []
    seen = set()
    for i, block in enumerate(blocks, 1):
        path, old_text, new_text = _parse_edit_input(block)
        path = (path or "").strip().strip("'\"")
        if not path:
            errors.append(f"块 {i}：缺少 path。")
            continue
        if not old_text:
            errors.append(f"块 {i}（{path}）：批量编辑必须提供精确 old_text（整体重写请用 apply_edit）。")
            continue
        if new_text is None:
            errors.append(f"块 {i}（{path}）：缺少 new_text。")
            continue
        target, root_abs, reason = _resolve_in_root(path)
        if target is None:
            errors.append(f"块 {i}（{path}）：{reason or '拒绝写入，路径不在代码根目录内。'}")
            continue
        key = os.path.normcase(target)
        if key in seen:
            errors.append(f"块 {i}（{path}）：同一文件出现多次，请合并为一块。")
            continue
        seen.add(key)
        if not _region_write_allowed(target):
            errors.append(f"块 {i}（{path}）：属于已配置分区，请改用 dev_region_edit。")
            continue
        # 决策 7 第二层：落盘前反查 lane 归属；单用户（无 lane 上下文）零开销透传。
        _rel = os.path.relpath(target, root_abs)
        _gate = lanes.write_gate(root_abs, _rel)
        if _gate:
            errors.append(f"块 {i}（{path}）：{_gate}")
            continue
        if not os.path.isfile(target):
            errors.append(f"块 {i}（{path}）：文件不存在（新建请用 create_file）。")
            continue
        try:
            with open(target, encoding="utf-8", errors="ignore") as fh:
                old_content = fh.read()
        except Exception as e:  # noqa: BLE001
            errors.append(f"块 {i}（{path}）：读取失败 {e}")
            continue
        cnt = old_content.count(old_text)
        if cnt == 0:
            errors.append(f"块 {i}（{path}）：未找到 old_text 匹配，内容可能已变化。")
            continue
        if cnt > 1:
            errors.append(f"块 {i}（{path}）：old_text 匹配 {cnt} 处，存在歧义。")
            continue
        new_content = old_content.replace(old_text, new_text, 1)
        if len(new_content.encode("utf-8", "ignore")) > _APPLY_MAX_BYTES:
            errors.append(f"块 {i}（{path}）：新内容超过 {_APPLY_MAX_BYTES // 1024}KB 上限。")
            continue
        if os.path.splitext(target)[1].lower() == ".py":
            import tempfile
            import py_compile
            tmp = None
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                                 encoding="utf-8") as tf:
                    tf.write(new_content)
                    tmp = tf.name
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as e:
                errors.append(f"块 {i}（{path}）：语法校验失败 {e.msg}")
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"块 {i}（{path}）：语法校验异常 {e}")
                continue
            finally:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
        plans.append({"path": path, "target": target, "root_abs": root_abs,
                      "old_text": old_text, "new_text": new_text,
                      "old_content": old_content, "new_content": new_content})

    if errors:
        return "未写入任何文件（原子性保护），请修正后重试：\n- " + "\n- ".join(errors)
    if not plans:
        return "未解析到有效编辑块，未写入任何文件。"

    # 「人工确认」模式：只暂存、不落盘（与 apply_edit 行为保持一致）
    if _edit_confirm_on():
        staged = []
        for p in plans:
            rel = os.path.relpath(p["target"], p["root_abs"])
            summary = _diff_summary(p["old_content"], p["new_content"])
            pid = stage_edit("apply_edit", p["target"], rel, p["old_content"],
                             p["new_content"], summary)
            staged.append(f"#{pid} {rel}")
        return "已暂存待人工确认（未写入）：\n- " + "\n- ".join(staged)

    written = []
    try:
        for p in plans:
            with _file_lock(p["target"]):
                with open(p["target"], encoding="utf-8", errors="ignore") as fh:
                    current = fh.read()
                if current != p["old_content"]:
                    raise RuntimeError(f"{p['path']}：并行修改冲突，文件在预检后已变化。")
                with open(p["target"], "w", encoding="utf-8") as fh:
                    fh.write(p["new_content"])
                lanes.record_write(
                    p["root_abs"], lanes.get_current_lane(),
                    os.path.relpath(p["target"], p["root_abs"]), "modify",
                    hashlib.sha256(p["old_content"].encode("utf-8")).hexdigest())
            written.append(p)
    except Exception as e:  # noqa: BLE001
        rolled = []
        for p in reversed(written):
            try:
                with open(p["target"], "w", encoding="utf-8") as fh:
                    fh.write(p["old_content"])
                rolled.append(p["path"])
            except Exception:  # noqa: BLE001
                pass
        msg = (f"写入失败：{e}\n已回滚 {len(rolled)}/{len(written)} 个已写文件"
               + (f"（{'、'.join(rolled)}）" if rolled else "")
               + f"\n其余 {len(plans) - len(written)} 个文件未写入，保持原样。")
        return msg

    out = [f"已原子写入 {len(written)} 个文件："]
    for p in written:
        rel = os.path.relpath(p["target"], p["root_abs"])
        out.append(f"- {rel}（{len(p['new_content'].encode('utf-8', 'ignore'))} 字节）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 受控"新建文件"工具 create_file：让 Agent 能在 code_root 内新增模块/分区文件，
# 但依然受沙箱、体积、语法护栏约束，且绝不覆盖已有文件（覆盖请用 apply_edit）。
# 设计意图：配合 search_code/grep，让"加新功能"走"先确认无重复 → 再新建"的
# 受控路径，而不是凭空生成导致堆叠。
# ---------------------------------------------------------------------------
def _resolve_create_path(path):
    """解析新建文件的绝对路径；越界返回 (None, root_abs, reason)。不要求文件已存在。"""
    root = _get_code_root()
    root_abs = os.path.normpath(root)
    target = os.path.normpath(path if os.path.isabs(path) else os.path.join(root_abs, path))
    floor = _hard_floor_reason(target)
    if floor:
        return None, root_abs, floor
    if not (target == root_abs or target.startswith(root_abs + os.sep)):
        return None, root_abs, "拒绝访问：路径不在代码根目录内（禁止越界写）。"
    # ③ symlink 闭合（审计 F1/F3 统一入口）：字面地板 + 根检查已过，realpath 再跑一次
    resolved = _resolve_floor_checked(root_abs, target)
    if resolved:
        return None, root_abs, resolved
    return target, root_abs, None


def create_file(arg):
    """在代码库内【新建】一个文件（不能覆盖已有文件）。详见 _parse_edit_input 输入格式。

    输入：path: <相对或绝对路径>，new_text: <文件内容（可多行）>。
    护栏：① 路径沙箱（必须落在 code_root 内）；② 不覆盖已有文件（改已有用 apply_edit）；
    ③ 父目录不存在时允许自动创建一层（仍在 code_root 内，便于新建分区）；
    ④ 单文件 200KB 上限；⑤ .py 写入前 py_compile 语法校验，失败取消创建。
    返回：已创建 / 失败原因。
    """
    path, _, content = _parse_edit_input(arg)
    if not path:
        return "参数缺失：请提供 path: <文件路径> 与 new_text: <文件内容>。"
    if content is None:
        return "参数缺失：请提供 new_text: <文件内容（可多行）>。"
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"

    target, root_abs, reason = _resolve_create_path(path)
    if target is None:
        return reason or f"拒绝写入：{path} 不在代码根目录内（禁止越界写）。"
    if not _region_write_allowed(target):
        return "拒绝写入：该路径属于已配置分区，请使用 dev_region_edit 以确保分区边界和先读后写护栏。"
    if os.path.exists(target):
        return f"文件已存在：{path}（create_file 不覆盖已有文件；要修改请用 apply_edit）。"

    # 决策 7 第二层：落盘前反查 lane 归属；单用户（无 lane 上下文）零开销透传。
    from agent_runtime import lanes
    _rel = os.path.relpath(target, root_abs)
    _gate = lanes.write_gate(root_abs, _rel)
    if _gate:
        return _gate

    if len(content.encode("utf-8", "ignore")) > _APPLY_MAX_BYTES:
        return f"拒绝写入：新文件大小超过上限（{_APPLY_MAX_BYTES // 1024}KB）。"

    ext = os.path.splitext(target)[1].lower()
    if ext == ".py":
        import tempfile
        import py_compile

        tmp = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tf:
                tf.write(content)
                tmp = tf.name
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            return f"语法校验失败，已取消创建：\n{e.msg}"
        except Exception as e:  # noqa: BLE001
            return f"语法校验异常，已取消创建：{e}"
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)

    rel = os.path.relpath(target, root_abs)
    nbytes = len(content.encode("utf-8", "ignore"))
    preview = content if len(content) <= 2000 else content[:2000] + "\n…（已截断，完整内容 %d 字节）" % nbytes

    # 「人工确认」模式：只暂存，不落盘
    if _edit_confirm_on():
        pid = stage_edit("create_file", target, rel, None, content, "新建文件：\n" + preview)
        return f"待人工确认 #{pid}（未写入）。确认后才会真正创建文件。\n新建文件：\n{preview}"

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            return f"创建目录失败: {e}"

    try:
        with _file_lock(target):
            if os.path.exists(target):
                return "并行创建冲突：目标文件已被另一个代理创建，请改用 apply_edit。"
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            lanes.record_write(root_abs, lanes.get_current_lane(), rel, "create", "")
    except Exception as e:  # noqa: BLE001
        return f"写入失败: {e}"

    # 自己刚创建的文件，视为已"确认内容"，允许后续整体重写而无需再 read_file
    _read_store()[0].add(os.path.normcase(target))
    return ToolResult(
        ok=True,
        text=f"已创建 {rel}（{nbytes} 字节，路径沙箱校验通过）。",
        artifacts=[{
            "path": rel,
            # 不写死 kind：预览侧有一份扩展名→类型映射（`infer_artifact_kind`），
            # 那才是唯一的口径。这里写死后 `.md` 会被当成代码。
            "label": os.path.basename(rel),
            "summary": "新建 %s（%d 字节）" % (rel, nbytes),
            "bytes": nbytes,
            "evidence": ["tool:create_file"],
        }],
    )


# ---------------------------------------------------------------------------
# 命令执行工具 run_command：在 code_root 内跑构建/测试/项目命令，返回输出。
# 护栏：cwd 锁 code_root；危险命令黑名单拦截；超时 12s（同 python_exec）；
# stdout+stderr 合并截断 1500 字。
# ---------------------------------------------------------------------------
# 命令护栏：结构化黑名单（按可执行词）+ 可选白名单（RUN_COMMAND_ALLOW）。
# 仅为启发式防护，非 OS 级沙箱；真正的隔离需外部容器/沙箱。
_BLOCKED_CMDS = {
    # 破坏性文件/系统命令
    "rm", "del", "erase", "rd", "rmdir", "format", "shutdown", "restart", "reboot",
    "mkfs", "fdisk", "diskpart", "dd", "chkdsk", "chown", "chmod", "cacls", "icacls",
    "shred", "wipe", "cipher",
    # 网络外联 / 下载（防数据外带）
    "curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm",
    "nc", "netcat", "ncat", "telnet", "ssh", "scp", "sftp", "ftp", "certutil",
    # 脚本解释器 / 子 shell / 系统操控（可执行任意命令，绕过黑名单）
    "powershell", "pwsh", "bash", "sh", "zsh", "cmd", "reg", "wmic", "mshta",
    "rundll32", "schtasks", "wsl", "taskkill", "net",
}
# 危险子串/参数模式（命令+参数级，比单一可执行词更细）
_DANGEROUS_PATTERNS = (
    "rm -rf", "rm -r ", "rm -fr", "del /s", "del /q", "rd /s", "rmdir /s",
    "format ", "shutdown", "mkfs", "dd if=", "> /dev/sd", "> /dev/null",
    "git clean", "git reset --hard", "git push -f", "git push --force",
    "chmod -r", "chmod 777", "chmod 666", ":(){", "fork bomb",
    "python -c", "py -c", "python3 -c", "node -e", "node --eval",
    "npm install -g", "npm i -g", "pip install", "rmdir /q",
)


def _first_command_token(low):
    """提取命令首个「可执行词」（去引号/前缀/环境变量），取 basename 用于黑/白名单匹配。"""
    s = low.strip()
    s = re.sub(r"^(?:cmd(?:\.exe)?\s+/[ck]\s+)", "", s)    # cmd /c ...
    s = re.sub(r"^(?:[a-z_][a-z0-9_]*=[^ ]*\s+)+", "", s)   # VAR=value 前缀
    s = re.sub(r"^call\s+", "", s)
    m = re.match(r'"?([^"\s]+)"?', s)
    if not m:
        return ""
    tok = os.path.basename(m.group(1)).lower()
    return tok[:-4] if tok.endswith(".exe") else tok


# git 写操作子命令：必须走分区审批门禁（dev_commit / dev_commit_all / dev_rollback），
# 不能经 run_command 直接执行——否则 Agent 可绕过审批、变更集台账与回滚链，
# push 还会把代码外带，与黑名单"防数据外带"的初衷矛盾。只读子命令（status/log/diff/show 等）放行。
_GIT_OPTS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--exec-path", "--super-prefix", "--list-submodules",
}
_GIT_WRITE_SUBCMDS = {
    "commit", "push", "merge", "rebase", "revert", "reset", "clean",
    "checkout", "switch", "apply", "cherry-pick", "stash", "am",
    "update-ref", "worktree",
}


def _git_subcommand(low):
    """从 `git ... <sub>` 命令中解析真正的子命令，跳过 -C <path> 等带值选项。

    识别不了（如自定义 alias git co）时返回 ''，由调用方按默认策略处理。
    """
    toks = re.sub(r"\s+", " ", low).strip().split(" ")
    if not toks or _first_command_token(low) != "git":
        return ""
    i = 1
    while i < len(toks):
        t = toks[i]
        if t in _GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t
    return ""


def _cmd_is_blocked(cmd):
    """返回 (True, 原因) 表示应拦截；否则 (False, '')。"""
    low = re.sub(r"\s+", " ", cmd.lower()).strip()
    for pat in _DANGEROUS_PATTERNS:
        if pat in low:
            return True, pat
    first = _first_command_token(low)
    # git 写操作门禁优先于可选白名单：即使用户把 git 加进 RUN_COMMAND_ALLOW 也不放行
    if first == "git":
        sub = _git_subcommand(low)
        if sub in _GIT_WRITE_SUBCMDS:
            return True, (f"git {sub}（git 写操作受分区审批门禁保护，"
                          "请改用 dev_commit / dev_commit_all / dev_rollback）")
    if first in _BLOCKED_CMDS:
        return True, first
    allow = (get_runtime("run_command_allow") or os.getenv("RUN_COMMAND_ALLOW", "")).strip()
    if allow:
        allowed = {a.strip().lower() for a in allow.split(",") if a.strip()}
        if first not in allowed:
            return True, f"{first}（不在白名单内）"
    return False, ""


_TRUE_WORDS = {"1", "true", "yes", "on", "是", "后台", "background"}
_RE_CMD_PREFIX = re.compile(r"^(?:cmd|command)\s*[:：]\s*(.*)$", re.I)
_RE_TIMEOUT_LINE = re.compile(r"^\s*timeout\s*[:：]\s*(.+?)\s*$", re.I)
_RE_BG_LINE = re.compile(r"^\s*background\s*[:：]\s*(.+?)\s*$", re.I)


def _parse_command_spec(arg):
    """Split an Action Input into (command, timeout, background).

    Accepts a bare command string for backward compatibility, and also the
    keyed form an agent naturally writes when it wants a longer budget::

        cmd: npm run build
        timeout: 180

    Only continuation lines are read as options, so a command that merely
    mentions ``timeout:`` on its first line still runs verbatim.
    """
    lines = str(arg or "").strip().splitlines()
    kept = []
    timeout = None
    background = False
    for index, line in enumerate(lines):
        if index > 0:
            matched_timeout = _RE_TIMEOUT_LINE.match(line)
            if matched_timeout:
                timeout = matched_timeout.group(1).strip()
                continue
            matched_bg = _RE_BG_LINE.match(line)
            if matched_bg:
                background = matched_bg.group(1).strip().strip("'\"").lower() in _TRUE_WORDS
                continue
        if index == 0:
            prefix = _RE_CMD_PREFIX.match(line)
            if prefix:
                line = prefix.group(1).strip()
        kept.append(line)
    command = "\n".join(kept).strip().strip("'\"")
    return command, timeout, background


def _render_command_result(result):
    """Render a bounded run report for the model, keeping the failure visible."""
    parts = [f"[exit code {result.get('exit_code')}]"]
    if result.get("elapsed") is not None:
        parts[0] += f" · {result['elapsed']}s"
    parts[0] += "\n"
    output = (result.get("output") or "").strip()
    if not output:
        parts.append("（命令已执行，无输出）")
    else:
        parts.append(output)
    if result.get("timed_out"):
        parts.append(f"\n注意：命令超过 {result.get('timeout_hint', '规定')} 被终止。"
                     "若它本来就更慢，请加一行 background: true 转后台任务，再用 dev_job_logs 取日志。")
    if result.get("error"):
        parts.append(f"\n错误：{result['error']}")
    return "\n".join(parts)


def run_command(cmd):
    """在项目根目录内执行命令（如 pytest / npm run build），返回合并输出。

    用于跑构建、跑测试、执行项目内命令来验证改动或查看结果。命令在 code_root 内执行。
    护栏：结构化黑名单（破坏性命令 / 网络外联 / 脚本解释器）+ 可选白名单（RUN_COMMAND_ALLOW）。

    输入形式：
      · 整条命令（向后兼容），如 `pytest -q`
      · 需要更长预算时换行追加 `timeout: <秒>`（上限 300s）
      · 预计更久则追加 `background: true`，转后台任务后用 dev_job_logs 取增量日志

    输出策略：合并 stdout/stderr，只保留开头与结尾窗口（中间省略会明确标注），
    保证编译器与测试的错误尾部不会被截断掉。
    """
    from agent_runtime import process_runner
    from agent_runtime.enterprise_sandbox import container_available

    command, timeout_raw, background = _parse_command_spec(cmd)
    if not command:
        return "未提供命令。"
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    blocked, why = _cmd_is_blocked(command)
    if blocked:
        return f"拒绝执行：命令被安全策略拦截（命中「{why}」）。"
    argv = _shell_argv(command)
    root = os.path.normpath(root)

    if background:
        seconds = process_runner.clamp_timeout(timeout_raw, process_runner.MAX_BACKGROUND_TIMEOUT, 300)
        started = process_runner.start_job(argv, cwd=root, timeout=seconds, command_text=command)
        if not started.get("ok"):
            return f"后台任务启动失败：{started.get('error') or '未知原因'}"
        return json.dumps({
            "ok": True,
            "job_id": started["job_id"],
            "state": started.get("state"),
            "timeout": started.get("timeout"),
            "command": started.get("command"),
            "how_to_read": f"用 dev_job_logs 输入 job_id: {started['job_id']}（可带 offset 续读增量）；"
                           f"用 dev_job_cancel 输入 job_id: {started['job_id']} 终止。",
        }, ensure_ascii=False)

    seconds = process_runner.clamp_timeout(timeout_raw, process_runner.MAX_TIMEOUT, 30)
    if container_available() and not host_fallback_active():
        try:
            proc = run_agent_command(argv, project_root=root, timeout=seconds)
        except subprocess.TimeoutExpired:
            return (f"命令执行超时（>{seconds}s）。若它本来就更慢，请加一行 background: true "
                    "转后台任务，再用 dev_job_logs 取日志。")
        except EnterpriseSandboxUnavailable as exc:
            return f"拒绝执行：{exc}"
        except Exception as exc:  # noqa: BLE001
            return f"执行失败: {exc}"
        result = process_runner.run_bounded_report(proc, seconds)
        return _render_command_result(result)

    result = process_runner.run_bounded(argv, cwd=root, timeout=seconds, command_text=command)
    result["timeout_hint"] = f"{seconds}s"
    return _render_command_result(result)


def dev_job_logs(arg=""):
    """读取后台命令任务的状态与增量输出。

    输入 job_id: <id>（可附加 offset: <字符偏移> 续读）；省略 job_id 则列出最近的后台任务。
    用于跑长构建、测试套件或本地服务时持续观察，而不是让前台调用一直阻塞。
    长日志加两样：**fold: true** 把重复行折成「重复 N 次」（按签名比，不是按整行——真
    uvicorn 的 access 行每条都带临时端口，逐行去重等于折不动）；**level: warn|error** 只看
    警告以上，且 traceback 正文会跟着它的标题走（逐行筛级别会把最该看的正文筛掉）。
    视图模式是【整块重算】，不能续读，会如实说；上游缓冲丢掉的中段也照实报，所以视图永远
    不许被读成「日志里没有错误」。
    """
    from agent_runtime import log_view, process_runner

    fields = _parse_keyed(arg or "", ["job_id", "id", "offset", "fold", "level"])
    job_id = str(fields.get("job_id") or fields.get("id") or "").strip()
    # 模型经常直接把 id 当整串输入（"dev_job_logs(abc123)"），此时首行就是 id。
    if not job_id and str(arg or "").strip():
        job_id = str(arg).strip().splitlines()[0].strip().strip("'\"")
    raw_offset = str(fields.get("offset") or "0").strip()
    try:
        offset = int(float(raw_offset))
    except (TypeError, ValueError):
        offset = 0
    if not job_id:
        listing = process_runner.job_logs("")
        jobs = listing.get("jobs") or []
        if not jobs:
            return "当前没有后台任务。需要跑长命令时用 run_command 加一行 background: true。"
        return json.dumps({"ok": True, "jobs": jobs}, ensure_ascii=False)
    report = process_runner.job_logs(job_id, offset=offset)
    if not report.get("ok"):
        return f"后台任务读取失败：{report.get('error')}"
    want_fold = str(fields.get("fold") or "").strip().lower()
    level = str(fields.get("level") or "").strip().lower()
    folding = want_fold in ("1", "true", "yes", "on") or bool(level)
    if not (folding or want_fold or level):
        # 原始增量读法：不改文本、不碰 offset，只在确实有重复/有缺口时附一句提示
        stats = log_view.summarize(str(report.get("logs") or ""))
        if stats["ok"] and (stats["folded_away"] >= 5 or stats["middle_omitted_chars"]):
            gap = ("，且缓冲已丢掉中段 %d 字符" % stats["middle_omitted_chars"]
                   if stats["middle_omitted_chars"] else "")
            report["view_hint"] = ("有 %d 行是重复的（加 fold: true 折成 %d 行；只看问题加 "
                                   "level: warn）%s"
                                   % (stats["folded_away"], stats["lines_out"], gap))
        return json.dumps(report, ensure_ascii=False)
    view = log_view.summarize(str(report.get("logs") or ""), level=level, fold=folding)
    if not view["ok"]:
        return "后台任务读取失败：%s" % view["error"]
    notes = []
    if offset:
        notes.append("视图模式整块重算，offset 被忽略（要增量请去掉 fold/level）")
    if view["middle_omitted_chars"]:
        notes.append("缓冲丢掉中段 %d 字符，本视图看不见" % view["middle_omitted_chars"])
    if view["dropped_failure_marks"]:
        notes.append("筛掉的 %d 段里有 %d 段带 4xx/5xx/失败 字样" % (
            view["dropped_runs"], view["dropped_failure_marks"]))
    if view["line_cap_hit"]:
        notes.append("只处理了前 %d 行" % log_view.MAX_LOG_LINES)
    report["logs"] = view["text"]
    report["view"] = {k: view[k] for k in (
        "lines_in", "lines_out", "folded_away", "markers_added", "dropped_runs",
        "dropped_lines", "dropped_failure_marks", "middle_omitted_chars", "levels_seen",
        "top_repeats")}
    report["note"] = "；".join(notes)
    if offset:
        report["next_offset"] = None      # 视图不是增量视图：留着旧 offset 会让人重复读同一段
    return json.dumps(report, ensure_ascii=False)


def dev_job_cancel(arg=""):
    """终止一个后台命令任务（保留已采集的输出供查看）。输入 job_id: <id>。"""
    from agent_runtime import process_runner

    fields = _parse_keyed(arg or "", ["job_id", "id"])
    job_id = str(fields.get("job_id") or fields.get("id") or "").strip()
    if not job_id and str(arg or "").strip():
        job_id = str(arg).strip().splitlines()[0].strip().strip("'\"")
    if not job_id:
        return "参数缺失：请提供 job_id: <后台任务 id>（可用 dev_job_logs 不带参数列出）。"
    report = process_runner.job_cancel(job_id)
    if not report.get("ok"):
        return f"后台任务终止失败：{report.get('error')}"
    return json.dumps({k: v for k, v in report.items() if k != "logs"}, ensure_ascii=False)


def init_regions_tool(arg):
    """初始化「分区开发」结构：在代码库根目录建 assets/values/bugs/behaviors 四个独立子目录，
    各 git init 独立仓库，并生成 DEV_INDEX.md 与 DOCMIND_RULES.md（分区契约，会被注入 Agent 系统提示）。
    用于游戏等分工开发，防止代码堆叠。输入留空即可。"""
    from regions import init_regions
    ok, msg = init_regions()
    return msg


# ---------------------------------------------------------------------------
# 分区开发工具层（2.0）：分区内受控读写 / 分区校验 / 跨区安全搬移 / 提交与变更集回滚。
# 所有路径都限定在对应分区子目录内，越区写被拦截（强化"防堆叠/防混乱"）。
# 写操作复用 apply_edit / create_file 的全部护栏（先读后写、.py 语法校验、200KB 上限、人工确认）。
# ---------------------------------------------------------------------------
def _parse_keyed(arg, keys):
    """通用 keyed 解析：从多行文本里提取指定字段（region/path/old_text/new_text/...）。
    字段值可同行或换行续写、可多行；用于分区工具输入解析，逻辑与 _parse_edit_input 一致。

    ⚠⚠ **未知键必须报出来，不许静默污染上一个字段**（2026-10-10 修）。

    原来的实现只认 `keys` 里的名字：某个已知键的值会一路吃到下一个已知键的
    开头，之后再没有已知键就吃到字符串末尾。于是写错一个键名时：

        action: open
        url: http://127.0.0.1:8099/
        width: 1280        ← keys 里没有这个键
        height: 720

    `url` 会取到 `'http://127.0.0.1:8099/\\nwidth: 1280\\nheight: 720'`，
    拼出 `http://127.0.0.1:8099/width: 1280height: 720` -> 404 -> 标题
    "Error response" -> 工具还回报「打开时就有 1 条 console 错误」，
    把账算在网页头上，而真正的原因是**参数里有个它不认识的键**。

    实测那次就是把 `preview_project` 的 width/height 喂给了
    `dev_page_action`（后者的 keys 里没有这两个）。

    这不是一个工具的问题：全仓 61 处调用 `_parse_keyed`，任何一处键名写错
    都会静默毁掉上一个字段的值。

    现在 `_parse_keyed` 额外在返回的 dict 里放一个特殊键 `"__unknown__"`
    （形如 `["width", "height"]`）。**原有的 61 处调用点一行都不用改** ——
    它们照旧从 dict 里取自己认识的键。是否理会未知键由各工具自己决定；
    本文件里 `dev_page_action` / `preview_project` / `dev_serve` 这几个
    "键名最容易被记混"的会显式检查并回报。
    """
    arg = (arg or "").lstrip("\n")
    spans = []
    for key in keys:
        for m in re.finditer(r"^\s*" + re.escape(key) + r"\s*:\s*", arg, re.M):
            spans.append((m.start(), m.end(), key))
    spans.sort()
    fields = {}
    for i, (s, e, key) in enumerate(spans):
        val_end = spans[i + 1][0] if i + 1 < len(spans) else len(arg)
        val = arg[e:val_end]
        if val.startswith("\n"):
            val = val[1:]
        if val_end < len(arg):
            val = val.rstrip("\n")
        fields[key] = val
    # 找出「长得像键但不在 keys 里」的行。判定：`^[ \t]*名字[ \t]*:` ——
    # 与已知键同一形状，只是名字不在名单上。
    #
    # ⚠ 不要试图用"位置"去排除已知键：已知键的正则是 `^\s*`（`\s` 连换行
    # 一起吃），这里的是 `^[ \t]*`（不吃换行），同一个键两种起点差一个 `\n`，
    # 位置永远对不上 —— 实测就把每个已知键都误报成了未知。直接比名字即可。
    #
    # 残留风险：某个**字段值**里若有独立的 `名字:` 行（如 new_text 里写 YAML），
    # 会被当成未知键。这只是个提示列表，是否理会由调用方决定；本文件里只有
    # dev_page_action / preview_project 会检查它，而它们的值都很短。
    unknown = []
    for m in re.finditer(r"^[ \t]*([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*:", arg, re.M):
        name = m.group(1)
        if name in keys or name in unknown:
            continue
        unknown.append(name)
    if unknown:
        fields["__unknown__"] = unknown
    return fields


def _require_regions():
    """返回 (root, rmap) 或 (None, error_msg)。rmap = {key: region_meta}。"""
    root = _get_code_root()
    if not root:
        return None, "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    try:
        from regions import get_region_map
        rmap = get_region_map(root)
    except Exception as e:  # noqa: BLE001
        return None, f"读取分区配置失败: {e}"
    if not rmap:
        return None, "尚未初始化分区（regions.json 为空）。请先调用 init_regions 初始化分区开发结构。"
    return (root, rmap), None


def _resolve_region_path(root, rmap, region_key, path):
    """把 path 解析到某分区目录内的绝对路径；越界返回 (None, reason)。
    允许 path 以分区目录开头（如 values/balance.json）或纯文件名（如 balance.json）。"""
    if region_key not in rmap:
        return None, f"未知分区：{region_key}（可选分区：{', '.join(rmap.keys())}）"
    region_abs = os.path.normpath(os.path.join(root, rmap[region_key]["dir"]))
    p = (path or "").strip().strip("'\"")
    if os.path.isabs(p):
        rel = os.path.relpath(os.path.normpath(p), root)
    else:
        rel = p
    rel = rel.replace("\\", "/")
    rdir = rmap[region_key]["dir"]
    if rel == rdir:
        rel = ""
    elif rel.startswith(rdir + "/"):
        rel = rel[len(rdir) + 1:]
    target = os.path.normpath(os.path.join(region_abs, rel)) if rel else region_abs
    floor = _hard_floor_reason(target)
    if floor:
        return None, floor
    if target != region_abs and not target.startswith(region_abs + os.sep):
        return None, f"拒绝写入：{path} 不在分区 {rdir}/ 内（禁止越区写）。"
    # ③ symlink 闭合（审计 F1/F3 统一入口）：分区目标越界/凭据由 realpath 复核兜底
    resolved = _resolve_floor_checked(root, target)
    if resolved:
        return None, resolved
    return target, None


def _emit_edit_arg(abs_path, old_text, new_text):
    """拼装给 apply_edit / create_file 的多行输入（复用其护栏：先读后写/.py 校验/人工确认/越区防护）。"""
    parts = ["path: " + abs_path]
    if old_text is not None:
        parts.append("old_text: " + old_text)
    parts.append("new_text: " + new_text)
    return "\n".join(parts)


def dev_list_regions(arg):
    """列出已配置分区的 key/名称/目录/依赖/导出/脏状态，供 Agent 选用正确分区。输入留空即可。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    from regions import list_regions
    rows = list_regions(root)
    lines = [f"已配置 {len(rows)} 个分区（code_root={root}）："]
    for r in rows:
        deps = ", ".join(r["depends_on"]) or "无"
        exports = ", ".join(r["exports"]) or "无"
        dirty = "（有未提交改动）" if r["dirty"] else ""
        lines.append(f"- {r['key']}｜{r['name']}（{r['dir']}/）：{r['desc']}；依赖：{deps}；导出：{exports}{dirty}")
    return "\n".join(lines)


def dev_region_read(arg):
    """读取某分区内的文件（分区作用域，越区读被拒）。
    输入：region: <分区key> 换行 path: <分区内相对路径>。
    读取后该文件可被 dev_region_edit 整体重写（满足先读后写护栏）。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg, ["region", "path"])
    region, path = (f.get("region") or "").strip(), (f.get("path") or "").strip()
    if not region or not path:
        return "参数缺失：请提供 region: <分区key> 与 path: <分区内相对路径>。"
    target, reason = _resolve_region_path(root, rmap, region, path)
    if target is None:
        return reason
    return read_file(target)


def dev_region_edit(arg):
    """受控修改/新建某分区内的文件（分区作用域，越区写被拒；复用 apply_edit/create_file 的全部护栏）。
    两种用法：① 局部安全替换——提供 region、path、old_text（精确旧片段）、new_text；
    ② 整体重写——提供 region、path、new_text（省略 old_text），前提是你已用 dev_region_read 读过该文件。
    若路径文件已存在则按修改处理，不存在则按新建处理。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg, ["region", "path", "old_text", "new_text"])
    region = (f.get("region") or "").strip()
    path = (f.get("path") or "").strip()
    old_text = f.get("old_text")
    new_text = f.get("new_text")
    if not region or not path:
        return "参数缺失：请提供 region: <分区key> 与 path: <分区内相对路径>。"
    if new_text is None:
        return "参数缺失：请提供 new_text: <新内容>（局部替换还需 old_text: <精确旧片段>）。"
    target, reason = _resolve_region_path(root, rmap, region, path)
    if target is None:
        return reason
    lock = _region_lock(region)
    with lock:
        _tok = _REGION_EDIT_CTX.set(True)
        try:
            if os.path.isfile(target):
                return apply_edit(_emit_edit_arg(target, old_text, new_text))
            return create_file(_emit_edit_arg(target, None, new_text))
        finally:
            _REGION_EDIT_CTX.reset(_tok)


def _run_region_cmd(region_dir_abs, cmd):
    """在分区目录内执行校验命令（受安全黑名单约束，超时 30s，输出截断 1500 字）。"""
    cmd = (cmd or "").strip().strip("'\"")
    if not cmd:
        return "未提供校验命令。"
    blocked, why = _cmd_is_blocked(cmd)
    if blocked:
        return f"拒绝执行：校验命令被安全策略拦截（命中「{why}」）。"
    try:
        command = _shell_argv(cmd)
        proc = run_agent_command(command, project_root=region_dir_abs, timeout=30)
    except subprocess.TimeoutExpired:
        return "校验命令执行超时（>30s）。"
    except EnterpriseSandboxUnavailable as e:
        return f"拒绝执行：{e}"
    except Exception as e:  # noqa: BLE001
        return f"执行失败: {e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    head = f"[exit code {proc.returncode}]\n"
    return head + _window_output(out)


def dev_region_verify(arg):
    """校验单个分区：若该分区配置了 verify 命令则在分区目录内执行；否则检查其导出接口文件是否齐全。
    输入：region: <分区key>。返回命令执行情况或契约自检结果。
    说明：verify 命令建议写为脚本文件（如 `pytest tests/`）而非 `python -c ...`（后者会被安全策略拦截）。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg, ["region"])
    region = (f.get("region") or "").strip()
    if not region:
        return "参数缺失：请提供 region: <分区key>。"
    if region not in rmap:
        return f"未知分区：{region}（可选分区：{', '.join(rmap.keys())}）"
    meta = rmap[region]
    region_abs = os.path.normpath(os.path.join(root, meta["dir"]))
    verify_cmd = (meta.get("verify") or "").strip()
    if verify_cmd:
        if execution_mode() == "enterprise":
            return _run_region_cmd(region_abs, verify_cmd)
        from regions import run_verify
        ok, output = run_verify(region_abs, verify_cmd)
        if ok is not None or output is not None:
            return f"运行 {meta['name']} 的内置校验（{verify_cmd}）：\n{output}"
        return f"运行 {meta['name']} 的 verify 命令：`{verify_cmd}`\n" + _run_region_cmd(region_abs, verify_cmd)
    exports = meta.get("exports") or []
    if not exports:
        return f"{meta['name']} 未配置 verify 命令，也无导出接口需校验，跳过（OK）。"
    miss = [ex for ex in exports if not os.path.isfile(os.path.join(region_abs, ex))]
    if miss:
        return f"{meta['name']} 导出接口缺失：{', '.join(miss)}（契约校验失败）。"
    return f"{meta['name']} 导出接口齐全（{', '.join(exports)}），契约自检通过（OK）。"


def dev_refactor(arg):
    """跨分区安全搬移：把某分区内的文件移动到另一分区（受依赖方向约束，不破坏 git 跟踪）。
    输入：
      src_region: <源分区key>
      src_path: <源文件在源分区内的相对路径>
      dst_region: <目标分区key>
      dst_path: <目标文件在目标分区内的相对路径>
    规则：目标文件不能已存在（不覆盖）；搬移后从源分区 git 仓库移除源文件（保留历史）。
    依赖方向：仅允许移动到 src 依赖的分区（dst ∈ src.depends_on）或前后无关的分区；
    禁止把代码挪进「已依赖 src」的分区（sr ∈ dst.depends_on），避免循环耦合 / 倒置分层。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg, ["src_region", "src_path", "dst_region", "dst_path"])
    sr, sp = (f.get("src_region") or "").strip(), (f.get("src_path") or "").strip()
    dr, dp = (f.get("dst_region") or "").strip(), (f.get("dst_path") or "").strip()
    if not (sr and sp and dr and dp):
        return "参数缺失：请提供 src_region/src_path/dst_region/dst_path。"
    if sr not in rmap or dr not in rmap:
        return f"未知分区：src={sr} dst={dr}（可选：{', '.join(rmap.keys())}）"
    src_deps = set(rmap[sr].get("depends_on") or [])
    # 依赖方向校验：禁止把 src 的代码挪进「已依赖 src」的分区（倒置分层 → 潜在循环耦合）
    if dr != sr and dr not in src_deps and sr in set(rmap[dr].get("depends_on") or []):
        return (f"拒绝搬移：{sr} → {dr} 会形成反向依赖（{dr} 已依赖 {sr}），"
                f"把 {sr} 的代码挪进 {dr} 会破坏依赖方向、可能引发循环耦合。"
                f"请改放到 {sr} 依赖的分区之一（{', '.join(src_deps) or '无'}），或做解耦重组。")
    src_target, reason = _resolve_region_path(root, rmap, sr, sp)
    if src_target is None:
        return reason
    if not os.path.isfile(src_target):
        return f"源文件不存在：{sp}（在分区 {sr} 内）。"
    dst_target, reason = _resolve_region_path(root, rmap, dr, dp)
    if dst_target is None:
        return reason
    if os.path.exists(dst_target):
        return f"目标文件已存在：{dp}（在分区 {dr} 内），dev_refactor 不覆盖，请先处理目标。"
    try:
        with open(src_target, encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
    except Exception as e:  # noqa: BLE001
        return f"读取源文件失败: {e}"
    create_res = create_file(_emit_edit_arg(dst_target, None, content))
    # 「人工确认」模式：仅暂存、未落盘 → 不要动源文件，等用户确认后再搬
    if "待人工确认" in create_res:
        return (f"目标已暂存、待人工确认，源文件未改动。请在界面确认写入后重新调用本工具，"
                f"届时本工具会从 {sr} 移除源文件。\n{create_res}")
    if not create_res.startswith("已创建"):
        return f"目标创建失败，已中止搬移：{create_res}"
    # 真正创建成功 → 从源分区 git 移除源文件（保留历史）；未跟踪则直接删除
    src_region_abs = os.path.normpath(os.path.join(root, rmap[sr]["dir"]))
    if os.path.isdir(os.path.join(src_region_abs, ".git")):
        rel_src = os.path.relpath(src_target, src_region_abs)
        proc = subprocess.run(
            ["git", "rm", "-q", rel_src], cwd=src_region_abs,
            capture_output=True, text=True,
        )
        if proc.returncode != 0 and "not found" in (proc.stdout or proc.stderr or "") \
                and os.path.isfile(src_target):
            try:
                os.remove(src_target)
            except Exception:
                pass
    elif os.path.isfile(src_target):
        try:
            os.remove(src_target)
        except Exception as e:  # noqa: BLE001
            return f"目标已创建，但删除源文件失败：{e}（请手动清理 {src_target}）"
    return f"已安全搬移 {sr}/{sp} → {dr}/{dp}（目标已创建，源文件从 {sr} 移除）。\n{create_res}"


def _split_path_list(raw):
    """把 `paths:` 的值切成路径列表（支持逗号、分号、换行分隔）。"""
    text = (raw or "").replace("\r\n", "\n")
    out = []
    for chunk in re.split(r"[,;\n]", text):
        p = chunk.strip().strip("'\"").strip()
        if p:
            out.append(p)
    return out


def dev_git_diff(arg):
    """只读预览「我刚改了什么」——工作树/暂存区相对 HEAD 的差异，绝不写入或提交。

    输入（多行 keyed，全部可选）：
      paths: tools.py, agent.py        # 限定文件/目录，逗号或换行分隔；省略=整个代码根目录
      staged: true                     # 看「已 git add 的改动」，默认 false=看工作树未暂存改动
      stat: true                       # 只要文件级统计，不要差异正文
      context: 3                       # 差异上下文行数，默认 3
      timeout: 20                      # 秒，上限 60
    也可以直接把路径当输入（如 `tools.py`）。

    返回：变更清单（区分已暂存/未暂存/未跟踪）、统计、差异正文（过长会窗口化，
    保留开头与结尾）。未跟踪的新文件不会出现在 diff 中，工具会单独列出提示。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["paths", "staged", "stat", "context", "timeout"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
    raw_paths = fields.get("paths") or ""
    paths = _split_path_list(raw_paths) if raw_paths else bare

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")

    try:
        ctx = int(str(fields.get("context") or "3").strip())
    except (TypeError, ValueError):
        ctx = 3
    try:
        timeout = int(str(fields.get("timeout") or "20").strip())
    except (TypeError, ValueError):
        timeout = 20

    from agent_runtime import code_intel
    res = code_intel.git_diff_preview(
        root, paths=paths, staged=_truthy(fields.get("staged")),
        stat_only=_truthy(fields.get("stat")), context=ctx, timeout=timeout,
    )
    return code_intel.render_git_diff(res)


def dev_glob(arg):
    """按文件名通配在代码库里找文件（grep 只能搜内容，list_dir 只能一层层翻）。

    输入（多行 keyed）：
      pattern: **/liveAudioControl.ts     # 必填；不含 / 时按文件名匹配
      scope: frontend/src                 # 可选，限定目录（相对代码根目录）
      limit: 40                           # 可选，返回条数，默认 80，上限 500
    也可以直接把 pattern 当输入（如 `*.vue`）。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["pattern", "scope", "limit"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
    pattern = (fields.get("pattern") or "").strip() or (bare[0] if bare else "")
    pattern = pattern.splitlines()[0].strip() if pattern else ""
    try:
        limit = int(str(fields.get("limit") or "80").strip())
    except (TypeError, ValueError):
        limit = 80

    from agent_runtime import code_intel
    res = code_intel.glob_files(root, pattern,
                                scope=(fields.get("scope") or "").strip(), limit=limit)
    return code_intel.render_glob(res)


def dev_git_log(arg):
    """【只读】看历史：谁在什么时候改了什么、某个文件被哪些提交动过、某人最近在做什么。

    和 dev_git_diff 的分工：diff 看「现在还没提交的改动」，log 看「已经进历史的那些」。

    输入（多行 keyed，全部可选）：
      paths: agent_runtime/realtime_omni.py, frontend/src   # 限定文件/目录，逗号或换行分隔
      limit: 20          # 条数，默认 20，上限 200
      since: 2026-09-25  # 起始时间（git 认的日期写法都行）
      until: 2026-09-29
      author: root       # 作者名片段（用来分辨是哪条 lane 干的）
      at: b78bd52        # 只看某一个提交（也可写 HEAD / 分支名 / a..b）
      files: true        # 每条提交附带它改动的文件清单
      timeout: 20
    也可以直接把路径当输入（如 `tools.py`），语义等同 paths:。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["paths", "limit", "since", "until", "author", "at", "files", "timeout"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
    raw_paths = fields.get("paths") or ""
    paths = _split_path_list(raw_paths) if raw_paths else bare

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")

    try:
        limit = int(str(fields.get("limit") or "20").strip())
    except (TypeError, ValueError):
        limit = 20
    try:
        timeout = int(str(fields.get("timeout") or "20").strip())
    except (TypeError, ValueError):
        timeout = 20

    from agent_runtime import code_intel
    res = code_intel.git_log_preview(
        root, paths=paths, limit=limit,
        since=(fields.get("since") or "").strip(),
        until=(fields.get("until") or "").strip(),
        author=(fields.get("author") or "").strip(),
        at=(fields.get("at") or "").strip(),
        files=_truthy(fields.get("files")), timeout=timeout,
    )
    return code_intel.render_git_log(res)


def dev_plan(arg):
    """把「正在做的事」外置成项目状态目录里的一份清单，让上下文压缩不再等于丢目标。

    为什么需要它：对话上下文会被摘要压缩，而**压缩是有损的**——摘要有取舍，摘得越多
    任务细节退化越明显。正解不是压得更狠，而是把任务状态搬出上下文：清单在磁盘上，
    每轮自动注入你的系统消息，于是"上下文被摘掉一大半"不再等于"目标丢了"。
    长任务、跨多轮、或被压缩过之后，先 `read` 看清单，接着做**下一件未完成的事**。

    输入（多行 keyed）：
      action: open|append|check|uncheck|read|clear   # 必填
      title: 修好登录超时                     # open 用：清单标题
      items: 先加日志, 再补测试, 最后跑一遍    # open/append 用；逗号或换行分隔
      item: 补测试                           # check/uncheck 用：条目 key 或文本前缀

    例子：
      action: open / title: 修登录超时 / items: 定位超时点, 加日志, 补测试, 跑一遍
      action: check / item: 加日志           # 做完一件勾掉
      action: read                           # 随时看当前进度与下一件

    清单写在项目状态目录，**不碰工作树**（`git status` 不会因此变脏）。同名条目会自动
    合并，所以你把计划重述一遍不会堆出三份重复项。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    from agent_runtime import plan_state

    keys = ["action", "title", "items", "item"]
    fields = _parse_keyed(arg, keys)

    def _one(key):
        """单值字段只取第一行：`action: read` 后面还能接着写裸内容。"""
        return (fields.get(key) or "").splitlines()[0].strip() if fields.get(key) else ""

    action = _one("action").lower() or "read"

    if action == "read":
        plan = plan_state.read(root)
        return plan_state.render(plan)

    if action == "clear":
        plan_state.clear(root)
        return "任务清单已清空。"

    if action in ("open", "append"):
        raw = fields.get("items") or ""
        items = [part for part in re.split(r"[,，\n;；]+", raw) if part.strip()]
        if not items:
            return "缺少 items：请给出要写入清单的条目，例如 " \
                   "items: 先定位问题, 再修复, 最后补测试。"
        if action == "open":
            plan = plan_state.open_plan(root, _one("title"), items)
            return "已新建清单（%d 项，旧的同名条目已合并）：\n%s" \
                   % (len(plan["items"]), plan_state.render(plan))
        plan = plan_state.append_items(root, items)
        return "已追加 %d 项（跳过 %d 个同名重复）：\n%s" \
               % (plan.get("_added", 0), plan.get("_skipped", 0), plan_state.render(plan))

    if action in ("check", "uncheck"):
        target = _one("item")
        if not target:
            return "缺少 item：请给出要勾选/取消的条目 key 或文本前缀（先 action: read 看一眼）。"
        plan = plan_state.set_done(root, target, action == "check")
        if not plan.get("_hit"):
            known = "、".join(i["key"] for i in plan.get("items", [])[:8]) or "（清单为空）"
            return "没找到匹配「%s」的条目。现有 key：%s" % (target, known)
        return "已%s：%s\n\n%s" % ("完成" if action == "check" else "取消完成",
                                  plan["_hit"]["text"], plan_state.render(plan))

    return "未知 action: %s。可用：open / append / check / uncheck / read / clear。" % action


def dev_lanes(arg):
    """多人同仓协作的认领登记表：谁在动哪些文件，冲突当场发现，过期自动可被接手。

    输入（多行 keyed）：
      action: claim|status|release|heartbeat|check|open|close   # 必填
      lane: AI-A                       # 认领人标识（除 status 外必填）
      owner: 本会话简称                 # 谁署名，默认与 lane 同名
      paths: frontend/src/workbench/** # claim 必填；逗号或换行分隔多个模式
      ttl_minutes: 120                 # 租约时长，5..1440
      note: 正在接 R2 的播放队列         # 给别的 lane 看的说明
      path: agent.py                   # check 用：问这一个文件现在归谁
      exclude_lane: AI-A               # check 时排除自己
      base_ref: HEAD                   # open 用：worktree 的起点
      discard: true                    # close 用：确认丢弃 worktree 里未提交的改动
      force: true                      # claim 用：明知冲突仍要共管（需在 HANDOFF 说明）

    开工前先 `action: status` 看别人在动哪块；动某文件前 `action: check` 问一句归属；
    领到任务就 `claim`，做完 `release`。长任务定期 `heartbeat` 续租，过期即视为放弃。
    需要物理隔离（别人收口提交不会把你的半成品卷进去）时 `open` 开通独立 worktree。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["action", "lane", "owner", "paths", "ttl_minutes", "note", "path",
            "exclude_lane", "base_ref", "discard", "force", "timeout"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
    def _one(key):
        """单值字段只取第一行：`action: check` 后面还能接着写裸路径。"""
        return (fields.get(key) or "").splitlines()[0].strip() if fields.get(key) else ""

    action = _one("action").lower()

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")

    try:
        timeout = int(str(_one("timeout") or "60").strip())
    except (TypeError, ValueError):
        timeout = 60
    from agent_runtime import lanes
    try:
        if action == "claim":
            raw_paths = fields.get("paths") or ""
            paths = _split_path_list(raw_paths) if raw_paths else bare
            res = lanes.claim(root, _one("lane"), paths,
                              owner=_one("owner"),
                              ttl_minutes=_one("ttl_minutes"),
                              note=(fields.get("note") or "").strip(),
                              force=_truthy(_one("force")))
        elif action == "status":
            res = lanes.status(root)
        elif action == "check":
            target = _one("path") or (bare[0] if bare else "")
            res = lanes.check(root, target, exclude_lane=_one("exclude_lane"))
        elif action in ("heartbeat", "release"):
            getter = lanes.heartbeat if action == "heartbeat" else lanes.release
            res = getter(root, _one("lane"), owner=_one("owner"))
        elif action == "open":
            res = lanes.open_worktree(root, _one("lane"),
                                      base_ref=_one("base_ref"), timeout=timeout)
        elif action == "close":
            res = lanes.close_worktree(root, _one("lane"),
                                       discard=_truthy(_one("discard")), timeout=timeout)
        else:
            return ("dev_lanes 需要 action，取值：" + "、".join(lanes.ACTIONS)
                    + "。例如 `action: status` 看现在谁在动哪些文件。")
    except TimeoutError as exc:
        return f"dev_lanes({action}) 未完成：{exc}"
    except ValueError as exc:
        return f"dev_lanes({action}) 未完成：项目状态路径不可用（{exc}）"
    return lanes.render(res)


def dev_agent_log(arg=""):
    """让 agent 自身**只读**地看自己的结构化日志 —— agent_log.event() 写入的「agent 看自己」事件。

    设计原则（与 dev_plan / dev_lanes 同族）：
    - **只读**：agent 不应能写自己的日志（写由 `_agent_log_event` 在关键失败点自动
      发生；让 agent 改自己日志容易 noise flood，且与 `verification.py` 的
      "absence of evidence is not success" 原则冲突）
    - **限上限**：limit 上限 200 条（防 agent 一次性把全文件读进 prompt 烧 token）
    - **kind 过滤**：默认只返回 5 个白名单 kind（self_verify_failed / tool_call_failed /
      policy_decision / agent_init / tool_call）；传 kind: 任意值可破白名单但仍受 limit 限制
    - **失败静默**：root 为空 / 文件不存在 / JSON 损坏一律返回可读字符串（LLM 友好）
    - **不抛异常**：try/except 全包，不影响主流程

    输入（多行 keyed）：
      action: read|summary   # 必填，默认 read
      kind: tool_call_failed # 可选，过滤事件类型（不传 = 5 个白名单）
      limit: 20              # 可选，最多返回条数（1..200，默认 20）

    例子：
      action: read                              # 最近 20 条白名单 kind 事件
      action: read / kind: self_verify_failed   # 只看自验证失败
      action: read / limit: 100                 # 拉多点
      action: summary                           # 紧凑字符串列表（给 UI 调试面板用）

    文件路径：项目状态目录下 `agent-log-v1.jsonl`（见 agent_runtime/agent_log.py）。
    该文件由 `Agent._agent_log_event`（agent.py:3571）在 self_verify 失败等关键点
    写入；agent 通过本工具读这些事件来「看自己」——例如「我最近为什么自验证失败」、
    「最近调了哪些工具失败」等。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"

    keys = ["action", "kind", "limit"]
    fields = _parse_keyed(arg, keys)

    def _one(key):
        return (fields.get(key) or "").splitlines()[0].strip() if fields.get(key) else ""

    action = _one("action").lower() or "read"

    # limit 上限 200（防 agent 一次性把全文件读进 prompt 烧 token）
    try:
        limit = int(_one("limit") or "20")
    except (TypeError, ValueError):
        limit = 20
    if limit < 1:
        limit = 1
    if limit > 200:
        limit = 200

    kind = _one("kind") or None

    from agent_runtime import agent_log as _agent_log

    try:
        if action == "summary":
            return json.dumps(_agent_log.tail_summary(root, limit=limit), ensure_ascii=False)
        if action == "read":
            events = _agent_log.read(root, kind=kind, limit=limit)
            payload = {
                "count": len(events),
                "limit": limit,
                "truncated": len(events) >= limit,
                "events": events,
            }
            return json.dumps(payload, ensure_ascii=False, default=str)
        # 未知 action：与 dev_plan 行为一致，fallback 到 read
        events = _agent_log.read(root, kind=kind, limit=limit)
        payload = {
            "count": len(events),
            "limit": limit,
            "truncated": len(events) >= limit,
            "events": events,
            "note": "未知 action: %s（已 fallback 到 read）" % action,
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 工具失败不应影响主流程
        return "dev_agent_log 未完成（%s: %s）。请稍后重试或检查 agent-log-v1.jsonl 是否可访问。" \
            % (type(exc).__name__, str(exc)[:200])


def dev_probe(arg=""):
    """给**后端与命令行**一条真观察凭据——浏览器那套（preview_project / dev_page_action /
    dev_vue / dev_apptest）对纯后端项目、一条 CLI、一个 worker 一样都够不着。

    两种模式：
      mode: http   # 真的发一个请求到回环服务
        url: http://127.0.0.1:8000/api/projects     # 只允许回环，和 dev_serve 同一道边界
        method: GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS
        body: {...}                                  # POST 等带正文时用
        headers: {"X-Test": "1"}                    # 可选，单行 JSON
        expect_status: 200                           # 期望状态码；不符会明确报出来
        expect_text: total                           # 正文里必须出现的字符串
      mode: cli    # 跑一条命令，除了退出码还看别的
        command: python -m mypkg.cli --check         # 解析与安全门复用 run_command 那一套
        expect_text: OK                              # 输出里必须出现
        forbid_text: Traceback                      # 输出里不许出现
        allow_silent: true                           # 这条命令本来就不出声（默认「没输出」判未通过）

    `cli` 比退出码多给的，都是「退出码 0 但事情没成」的形态：输出为空、有 ERROR/CRITICAL
    级日志行、有 traceback、有 `NameError: name 'x' is not defined`、输出被截断。
    判定 fail-closed：连不上、命令起不来、输出截断、扫描不全，每一种都是单独一条红，
    不会悄悄变成一个好看的结论。

    只读观察：只发回环请求、只跑已经过黑名单的项目内命令，不改任何项目文件。
    """
    from agent_runtime import svc_probe
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["mode", "url", "method", "body", "headers",
                                      "expect_status", "expect_text", "command",
                                      "forbid_text", "allow_silent", "timeout"])

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    def _truthy(key):
        return _one(key).lower() in ("1", "true", "yes", "y", "on", "是")

    mode = (_one("mode") or ("http" if _one("url") else "cli")).lower()
    if mode not in ("http", "cli"):
        return "dev_probe 只认 mode: http 或 mode: cli。"
    try:
        seconds = int(_one("timeout") or svc_probe.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        seconds = svc_probe.DEFAULT_TIMEOUT
    if mode == "http":
        headers = {}
        raw_headers = (fields.get("headers") or "").strip()
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
            except ValueError as exc:
                return "headers 要写成单行 JSON 对象（%s）。" % exc
            if not isinstance(parsed, dict):
                return "headers 必须是一个 JSON 对象。"
            headers = {str(k): str(v) for k, v in parsed.items()}
        try:
            expect_status = int(_one("expect_status") or 0)
        except (TypeError, ValueError):
            expect_status = 0
        try:
            result = svc_probe.http_probe(_one("url"), method=_one("method") or "GET",
                                          body=(fields.get("body") or "").strip(),
                                          headers=headers, timeout=seconds)
        except Exception as exc:  # noqa: BLE001 - 探针失败就是失败，不能报通过
            return "dev_probe(http) 未完成（%s: %s）——这次观察没有发生。" % (
                type(exc).__name__, str(exc)[:200])
        return svc_probe.render_http(result, expect_status=expect_status,
                                     expect_text=_one("expect_text"))
    command = (fields.get("command") or "").strip()
    if not command:
        bare = [ln.strip() for ln in (arg or "").splitlines()
                if ln.strip() and not re.match(r"^\s*(mode|url|method|body|headers|"
                                                r"expect_status|expect_text|forbid_text|"
                                                r"allow_silent|timeout|command)\s*[:：]", ln)]
        command = " ".join(bare).strip()
    if not command:
        return "dev_probe(cli) 需要一行 command:（要跑什么）。"
    try:
        result = svc_probe.cli_probe(
            root, command, timeout=seconds, expect_text=_one("expect_text"),
            forbid_text=_one("forbid_text"), allow_silent=_truthy("allow_silent"),
            # 复用 run_command 的解析与黑名单：自己 split() 会拆坏引号，更会绕开
            # 那道安全门——那等于给这条工具开后门。
            argv_of=_shell_argv, gate=_cmd_is_blocked)
    except Exception as exc:  # noqa: BLE001
        return "dev_probe(cli) 未完成（%s: %s）——这次观察没有发生。" % (
            type(exc).__name__, str(exc)[:200])
    return svc_probe.render_cli(result)


def dev_handoff(arg=""):
    """把交付这段补到能真的往前走一步：分支名建议 + commit message 草稿 + 范围体检。

    补的是 `dev_propose` 到草稿就停下的那一段——产物躺在状态目录里，人还得自己决定
    分支名、自己写 message、自己确认「这些改动里有没有混进别人的在途 WIP」。本工具
    算的是**决策材料**，不代替决策：**不 add、不 commit、不 push、不切分支**，
    只跑只读 git（rev-parse/status/diff/log）。

    输入 keyed 多行（全部可选）：
      files: agent_runtime/x.py, tests/test_x.py   # 只看这些路径（默认全部改动）
      title: 修好打断后不闭嘴的问题                # 有它才推得出像样的 message
      timeout: 30

    会回：当前分支与 HEAD、建议分支名（`type/scope-slug`）、建议 commit message、
    「这次提交会包含哪些文件」与规模，以及几条必须提醒的事：

    * **共享文件**（非 tests/ 非 .md 的主干文件）会被单独标出——本仓实测教训：
      `git commit -- <路径>` 会绕过索引，把别人在同一批文件里的在途改动一起扫进去
      （`b828d10` 就是这么连累 HEAD 的）。提交前请核对 `git diff --cached --stat`。
    * **未跟踪文件**单列：它们不在 diff 里，说成「已交付」是这类工具最容易骗人的地方。
    * 改了 40 个文件以上会明说「该拆成几个提交」——没人能 review 40 文件的提交。
    * 会看本仓最近的提交标题来判断该不该加 `type(scope):` 前缀，按仓库自己的习惯
      写，而不是强推一套外来的。
    """
    from agent_runtime import handoff
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["files", "title", "timeout"])

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    try:
        timeout = int(_one("timeout") or "30")
    except (TypeError, ValueError):
        timeout = 30
    try:
        result = handoff.analyze(root, files=_split_path_list(fields.get("files") or "") or None,
                                 title=(fields.get("title") or "").strip(), timeout=timeout)
        result["_root"] = root
    except Exception as exc:  # noqa: BLE001 - 交付材料生成失败就是失败，不能报「就绪」
        return "dev_handoff 未完成（%s: %s）——没有 git 就没有「提交」，本次未生成任何材料。" % (
            type(exc).__name__, str(exc)[:200])
    return handoff.render(result)


def dev_pricing(arg=""):
    """从 OpenRouter 官方目录**联网检索**模型单价并写入价格表——不手抄、不写死会过期的表。

    为什么需要它：`custom` / OpenAI 兼容中转的模型目录与价格随时会变，代码里写死一份
    过期表比"显示未知"更糟——运行台会报出一个看着精确、实际错误的金额。而不配置就恒为 0，
    那也和"真的免费"混成了一件事。

    输入 keyed 多行（全部可选）：
      model: stealth/space-bunny-alpha   # 留空 = 用当前运行时选中的模型
      action: fetch|apply                 # fetch=只查不写（默认）；apply=写入价格表
      timeout: 45                         # 检索超时秒数

    `fetch` 回：单价（已从美元换算成**元 / 1M tokens**，汇率用 `DOCMIND_USD_CNY` 覆盖）、
    缓存读写单价、以及**阶梯定价** `overrides`（OpenRouter 普遍在长上下文处翻倍，
    缺了它长对话会被系统性低估）。三种结果分开报，绝不混成 0：
      · `free: true`      —— 官方目录里这个模型标价就是 0（真的免费）
      · `error: …`        —— 没查到 / 检索失败（**不是**免费）
      · 正常单价
    `apply` 额外把该模型的单价写进 `.docmind_pricing.json`（原子写、只动这一个模型、
    写完立刻用 price_detail 复核并回显写前/写后差别）。

    **免费模型建议 apply**：写进去以后运行台会明说"该模型免费"，而不是让人以为
    "没配价格所以显示 0"。
    """
    # pricing 是仓库顶层的模块（与 agent / tools 平级），不在 agent_runtime 下——
    # 从 agent_runtime 导入会 ImportError。
    import pricing
    from config import get_runtime
    fields = _parse_keyed(arg or "", ["model", "action", "timeout"])

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    action = (_one("action") or "fetch").lower()
    if action not in ("fetch", "apply"):
        return "dev_pricing 只认 action: fetch（只查）或 action: apply（写入价格表）。"
    model = _one("model") or str(get_runtime("llm_model") or "").strip()
    if not model:
        return "没有可用的模型名：请给 `model: <id>`，或先在设置里选好模型。"
    provider = str(get_runtime("llm_provider") or "custom").strip() or "custom"
    try:
        seconds = int(_one("timeout") or "45")
    except (TypeError, ValueError):
        seconds = 45
    try:
        found = pricing.openrouter_pricing(model, timeout=max(5, min(seconds, 120)))
    except Exception as exc:  # noqa: BLE001 - 检索失败就是失败，不能当成免费
        return "dev_pricing 检索失败（%s: %s）——这是【没查到】，不是「该模型免费」。" % (
            type(exc).__name__, str(exc)[:200])
    if found.get("error"):
        return ("dev_pricing 没查到 %s：%s\n"
                "注意这与「该模型免费」是两件事——免费会回 `free: true`，"
                "而这里是检索失败或模型 id 拼错。" % (model, found["error"]))
    lines = ["OpenRouter 官方单价（%s）：" % model]
    if found.get("free"):
        lines.append("  **该模型在官方目录里标价就是 0 —— 它真的免费。**"
                     "（这与「没配价格」不同；建议 `action: apply` 写进去，"
                     "运行台就会明说免费，而不是显示一个含义不明的 0。）")
    else:
        lines.append("  输入      %s 元 / 1M tokens" % found.get("in"))
        lines.append("  输出      %s 元 / 1M tokens" % found.get("out"))
        lines.append("  缓存命中读 %s 元 / 1M tokens" % found.get("cache_read"))
        lines.append("  缓存写入  %s 元 / 1M tokens" % found.get("cache_creation"))
        lines.append("  （源数据为美元/1M，汇率 %s；可用 DOCMIND_USD_CNY 覆盖）"
                     % found.get("usd_rate"))
    tiers = found.get("tiers") or []
    if tiers:
        lines.append("  阶梯定价（超过阈值后单价会跳档，缺了它长对话会被低估）：")
        for tier in tiers[:6]:
            lines.append("    ≥%s 输入 token：in=%s out=%s cache_read=%s" % (
                tier.get("min_prompt_tokens"), tier.get("in"), tier.get("out"),
                tier.get("cache_read")))
    if action == "fetch":
        lines.append("（只查未写。要写进价格表用 `action: apply`。）")
        return "\n".join(lines)
    saved = pricing.save_model_pricing(provider, model, {
        "in": found.get("in"), "out": found.get("out"),
        "cache_read": found.get("cache_read"),
        "cache_creation": found.get("cache_creation"),
        "overrides": tiers,
        # 显式落一个 free 标记：否则「真的免费」与「没配置单价」在界面上都是 0，
        # 用户无从分辨——这正是这个入口存在的理由，不能只把数字写进去就完事。
        "free": bool(found.get("free")),
    })
    if not saved.get("ok"):
        return "dev_pricing 检索到了，但写入失败：%s" % saved.get("error")
    verified = saved.get("verified") or {}
    lines.append("已写入 %s（provider=%s / model=%s）" % (saved.get("file"), provider, model))
    lines.append("写入后复核：in=%s out=%s cache_read=%s cache_creation=%s" % (
        verified.get("in"), verified.get("out"),
        verified.get("cache_read"), verified.get("cache_creation")))
    if saved.get("previous") is not None:
        lines.append("（覆盖了原值：%s）" % json.dumps(saved["previous"], ensure_ascii=False))
    state = pricing.pricing_state(provider, model)
    if state["state"] == pricing.PRICING_STATE_FREE:
        lines.append("运行台现在会把这个模型的 0 显示成「¥0.0000（该模型免费）」，"
                     "而不是含义不明的 0。")
    elif state["state"] == pricing.PRICING_STATE_UNKNOWN:
        lines.append("注意：写入后仍判为「未配置单价」——单价表没被读到，请检查文件权限。")
    return "\n".join(lines)


def dev_deps(arg=""):
    """对账「代码 import 了什么」与「依赖清单声明了什么」，并能把缺的**追加**进去。

    输入（keyed，全部可选）：
      paths: agent_runtime/retrieval.py, api.py   # 只查这些文件/目录，留空=全仓
      action: check|apply                        # check=只读对账（默认）；apply=追加缺失行
      source: requirements.txt                   # apply 时指定写进哪个清单

    补的是 vibecoding 最常见的死法：你在工作树里 import 了一个新包，本地因为恰好装过
    而一切正常，可提交里没有依赖声明——别人 clone 下来、或 CI 拉一个干净检出，第一句
    import 就 ModuleNotFoundError。dev_headcheck 能**发现**这件事，但没有工具能修，
    从「知道缺了」到「补上」之间是断的。

    扫描走 `ast` 不是正则：注释里的 `# import numpy` 与字符串里的 `"import os"`
    不会被当成依赖。映射按可信度找证据——本机已安装的发行元数据（最硬，实测
    `websocket`→`websocket-client`、`yaml`→`PyYAML` 都靠它）→ 已声明依赖的 extras
    （`psycopg[pool]` 带出 `psycopg-pool`）→ 查表 → 同名（标成弱证据）。都不确定就
    归入 unknown 并明确报出来，**绝不猜一个名字写进清单**。

    写操作只有一处：往依赖清单**追加**缺失行，不改版本、不删行、不重排——那是人的决定。
    没有依赖清单时不替你新建。解析失败的文件、被上限截断的扫描都会让报告**不能**
    被读成「依赖没问题」。
    """
    from agent_runtime import deps_sync
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["paths", "action", "source"])

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    action = (_one("action") or "check").lower()
    if action not in ("check", "apply"):
        return "dev_deps 只认 action: check 或 apply（只读对账 / 追加缺失行）。"
    try:
        report = deps_sync.reconcile(root, paths=_split_path_list(fields.get("paths") or "") or None)
    except Exception as exc:  # noqa: BLE001 - 扫描要失败就失败，绝不能报通过
        return "dev_deps 扫描失败（%s: %s）——这是【没查成】，不是「依赖没问题」。" % (
            type(exc).__name__, str(exc)[:200])
    applied = None
    if action == "apply":
        try:
            applied = deps_sync.apply(root, report, source=_one("source"))
        except Exception as exc:  # noqa: BLE001
            applied = {"ok": False, "applied": [], "error": "%s: %s" % (type(exc).__name__, exc)}
    return deps_sync.render(report, applied)


def dev_propose(arg):
    """把本轮改动打包成【可评审产物】：补丁文件 + 提交/PR 草稿。不提交、不推送。

    输入（多行 keyed，全部可选）：
      title: fix: 修好打断后不闭嘴的问题     # 草稿标题，也用来命名产物
      body: 为什么这么改                     # 草稿正文
      files: agent_runtime/x.py, tests/      # 只打包这些路径（默认全部未暂存改动）
      staged: true                           # 打包暂存区而不是工作树
      test_plan: 跑 pytest -k realtime       # 复核步骤
      lane: AI-A                             # 署名，方便交接
    产物写在【项目状态目录】里（不在仓库工作树内），`git status` 不会因为打包变脏。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["title", "body", "files", "staged", "test_plan", "lane", "timeout"]
    fields = _parse_keyed(arg, keys)

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")

    try:
        timeout = int(_one("timeout") or "30")
    except (TypeError, ValueError):
        timeout = 30
    from agent_runtime import proposal
    try:
        res = proposal.build(root, title=_one("title"),
                             body=(fields.get("body") or "").strip(),
                             files=_split_path_list(fields.get("files") or ""),
                             staged=_truthy(_one("staged")),
                             test_plan=(fields.get("test_plan") or "").strip(),
                             lane=_one("lane"), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return "dev_propose 无法运行：%s: %s" % (type(exc).__name__, str(exc)[:200])
    return proposal.render(res)


def dev_ci_status(arg):
    """【只读】看 CI 跑到哪了：最近的 workflow runs、失败 job、以及本地 HEAD 有没有被 CI 跑到。

    输入（多行 keyed，全部可选）：
      branch: main            # 只看这个分支（默认当前分支范围）
      per_page: 5             # 最近几次，上限 20
      jobs: true              # 顺带把最近一次的 job 列表拉回来（能指出哪个 job 挂了）
      log_url: <日志地址>      # 单独抓一份 job 日志，只回错误行
    需要 GitHub token（DOCMIND_GITHUB_TOKEN / GITHUB_TOKEN / GH_TOKEN 任一）。本工具
    绝不触发或重跑 workflow；连不上或没权限时会明确说「未完成 + 原因」，不编状态。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["branch", "ref", "per_page", "jobs", "log_url", "timeout"]
    fields = _parse_keyed(arg, keys)

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on", "是")

    try:
        per_page = int(_one("per_page") or 5)
    except (TypeError, ValueError):
        per_page = 5
    try:
        timeout = float(_one("timeout") or 15)
    except (TypeError, ValueError):
        timeout = 15.0
    from agent_runtime import ci_status
    log_url = _one("log_url")
    if log_url:
        res = ci_status.failed_log(log_url, tok=ci_status.token(), timeout=timeout)
        if not res.get("ok"):
            return "dev_ci_status 未完成：" + res.get("error", "")
        lines = ["日志里的错误行（%d 条%s）：" % (len(res.get("errors") or []),
                                          "，已截断" if res.get("truncated") else "")]
        lines += ["  " + row for row in res.get("errors") or []]
        return "\n".join(lines) or "日志里没找到 Error/FAILED/fatal 行。"
    try:
        res = ci_status.fetch(root, branch=_one("branch"), ref=_one("ref"),
                              per_page=per_page, timeout=timeout,
                              fetch_jobs=_truthy(_one("jobs")))
    except Exception as exc:  # noqa: BLE001
        return "dev_ci_status 无法运行：%s: %s" % (type(exc).__name__, str(exc)[:200])
    return ci_status.summarize(res)


def dev_diagnostics(arg):
    """拿结构化诊断（文件:行:列 + 规则号），而不是「过/不过」加一坨尾部日志。

    改完一段代码、跑测试之前先用它；比 self_verify 更早发现问题，也比读构建日志更准。

    输入（多行 keyed，全部可选）：
      target: agent_runtime/realtime_omni.py, frontend/src/workbench   # 文件/目录，逗号或换行分隔
      scope: auto|py|frontend|all    # auto 按 target 的扩展名自动选引擎
      rules: ALL                      # ruff 规则覆盖；默认只查会咬人的（E9/F401/F8xx/E722）
      timeout: 120                    # 单引擎秒数，上限 300
      config: ruff.toml               # 可选，项目内的 ruff 配置文件（必须在代码根目录内）
    也可以直接把路径当输入。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["target", "scope", "rules", "timeout", "config"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
    raw_target = fields.get("target") or ""
    target = _split_path_list(raw_target) if raw_target else bare

    def _one(key):
        return (fields.get(key) or "").splitlines()[0].strip() if fields.get(key) else ""

    try:
        timeout = int(_one("timeout") or "120")
    except (TypeError, ValueError):
        timeout = 120
    from agent_runtime import diagnostics
    try:
        res = diagnostics.run(root, target, scope=_one("scope") or "auto",
                              timeout=timeout, config=_one("config"), rules=_one("rules"))
    except Exception as exc:  # noqa: BLE001
        return f"dev_diagnostics 无法运行：{type(exc).__name__}: {str(exc)[:200]}"
    return diagnostics.render(res)


def dev_patch(arg):
    """把一段 unified diff 原子地打到代码库里：全成才打，任一文件对不上就一个都不改。

    输入就是 diff 正文（`--- a/路径` / `+++ b/路径` / `@@ -l,c +l,c @@` / 空格、+、- 行）。
    首行可以加 `dry_run: true` 只做校验与语法检查、不写盘——大补丁先空跑一次。
    规则：路径必须是代码根目录内的相对路径（带 a/ b/ 前缀会被去掉）；上下文与
    被删行必须逐字相等（不做模糊匹配，宁可报错也不改错地方）；行号偏了但上下文
    一致时会自动对齐并在结果里说明；.py 打完还会过一遍语法检查；删除文件一律拒绝。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    text = (arg or "").replace("\r\n", "\n")
    first, sep, rest = text.partition("\n")
    dry_run = False
    if re.match(r"^\s*dry_run\s*[:：]", first):
        dry_run = first.split(":", 1)[-1].strip().strip("：").lower() in ("1", "true", "yes", "y", "on", "是")
        text = rest
    from agent_runtime import patch_apply
    try:
        res = patch_apply.apply_patch(root, text, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        return f"dev_patch 无法运行：{type(exc).__name__}: {str(exc)[:200]}"
    return patch_apply.render_patch(res)


def dev_move(arg):
    """移动/重命名代码库内的单个文件：被 git 跟踪的走 `git mv`（保历史与 blame），未跟踪的退回文件系统移动。

    输入（两行）：
      from: pkg/old_name.py
      to:   pkg/sub/new_name.py
    只做移动：不覆盖已存在的目标、不删目录、不越界。注意 run_command 只拦【历史类】
    git 子命令，`git mv` 本来就能跑——这个工具的价值是把移动约束成安全路径。移动后请用 dev_find_references / grep 检查旧路径的
    import 与引用，再用 dev_diagnostics 确认没有打断。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["from", "to", "src", "dst", "timeout"]
    fields = _parse_keyed(arg, keys)

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    src = _one("from") or _one("src")
    dst = _one("to") or _one("dst")
    try:
        timeout = int(_one("timeout") or "30")
    except (TypeError, ValueError):
        timeout = 30
    if not src or not dst:
        return "dev_move 需要两行：`from: <旧路径>` 与 `to: <新路径>`（相对代码根目录）。"
    from agent_runtime import patch_apply
    try:
        res = patch_apply.move_path(root, src, dst, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return f"dev_move 无法运行：{type(exc).__name__}: {str(exc)[:200]}"
    return patch_apply.render_move(res)


def dev_find_references(arg):
    """重构前定位符号的全部引用点（重命名/删函数/改签名前必用）。

    Python 走 ast 精确匹配——注释与字符串里的同名文本【不会被】误报；
    TS/TSX/JS/JSX/Vue 走 tree-sitter 语法匹配（注释、字符串与对象键都不误报，
    Vue 的行号映射回文件真实行；缺语法时自动回退正则并在 notes 里说明）；
    其他语言走词边界正则，并跳过纯注释行。
    输入（多行 keyed）：
      symbol: foo                      # 必填，函数名/类名/变量名
      scope: frontend/src              # 可选，限定目录或文件（相对代码根目录）
      kind: def|ref|all                # 可选，默认 all（只看定义 / 只看引用）
      limit: 50                        # 可选，返回条数上限，默认 80
    也可以直接把符号名当输入（如 `foo`）。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["symbol", "scope", "kind", "limit"]
    fields = _parse_keyed(arg, keys)
    symbol = (fields.get("symbol") or "").strip()
    if not symbol:
        bare = [ln.strip() for ln in (arg or "").splitlines()
                if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]
        symbol = bare[0] if bare else ""
    if not symbol:
        return "参数缺失：请提供 symbol: <符号名>。"
    symbol = _clean_symbol(symbol)  # 弱模型常写「foo 的调用点」，只取首個标识符
    scope_raw = (fields.get("scope") or "").strip()
    scope = _split_path_list(scope_raw) if scope_raw else None
    try:
        limit = int(str(fields.get("limit") or "80").strip())
    except (TypeError, ValueError):
        limit = 80

    from agent_runtime import code_intel
    res = code_intel.find_references(
        root, symbol, scope=scope,
        kind=(fields.get("kind") or "all").strip().lower(), limit=limit,
    )
    return code_intel.render_references(res)


def dev_commit(arg):
    """提交单个分区的改动（该分区独立 git 仓库内 commit）。
    输入：region: <分区key> 换行 message: <提交说明>。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg, ["region", "message"])
    region = (f.get("region") or "").strip()
    message = (f.get("message") or "docmind: update").strip() or "docmind: update"
    if not region:
        return "参数缺失：请提供 region: <分区key>。"
    from regions import commit_region
    ok, out = commit_region(root, region, message)
    return ("已提交" if ok else "提交失败") + f" 分区 {region}：" + out


def dev_verify_contracts(arg):
    """校验全部分区的契约：依赖方向无环、被依赖分区导出文件存在、依赖目标存在。
    输入留空即可。建议在大幅改动分区前后调用。返回 ok/错误列表/依赖图。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    from regions import verify_contracts
    r = verify_contracts(root)
    if r["ok"]:
        return "契约校验通过（依赖方向无环、依赖目标与导出接口均存在）。\n依赖图：" + json.dumps(r["graph"], ensure_ascii=False)
    return "契约校验失败：\n- " + "\n- ".join(r["errors"]) + "\n依赖图：" + json.dumps(r["graph"], ensure_ascii=False)


def dev_rebuild_index(arg):
    """依据当前分区配置重算 DEV_INDEX.md（分区目录/文件数/依赖变动后调用）。输入留空即可。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    from regions import rebuild_dev_index
    ok, msg = rebuild_dev_index(root)
    return msg


def dev_commit_all(arg):
    """把所有分区的改动各提交一次，并记进 dev_changesets.jsonl 作为一次绑定变更集（可整体回滚）。
    输入：message: <本次功能改动说明>。返回变更集 id 与各分区提交哈希。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    f = _parse_keyed(arg, ["message"])
    message = (f.get("message") or "docmind: update").strip() or "docmind: update"
    from regions import commit_all
    ok, info = commit_all(root, message)
    if not ok:
        return "变更集提交失败：\n" + "\n".join(f"- {k}: {v}" for k, v in (info.get("errors") or {}).items())
    cs_id = info.get("id", "")
    commits = info.get("commits", {})
    if not commits:
        return info.get("message", "没有可提交的分区改动。")
    lines = [f"已创建变更集 {cs_id}（message={message}）："]
    for k, v in commits.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + f"\n（回滚请调用 dev_rollback_changeset，changeset={cs_id}）"


def dev_list_changesets(arg):
    """列出已记录的跨区变更集（dev_changesets.jsonl）。输入留空即可。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    from regions import list_changesets
    cs = list_changesets(root)
    if not cs:
        return "暂无已记录的变更集。用过 dev_commit_all 后会在此列出。"
    lines = [f"共 {len(cs)} 个变更集："]
    for c in cs:
        lines.append(f"- id={c.get('id')} message={c.get('message','')} 分区={list((c.get('commits') or {}).keys())}")
    return "\n".join(lines)


def dev_rollback_changeset(arg):
    """整体回滚某变更集：对每个分区 revert 其记录的 commit（生成新提交撤销改动）。
    输入：changeset: <变更集id> 或 id: <变更集id>。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    f = _parse_keyed(arg, ["changeset", "id"])
    cs_id = (f.get("changeset") or f.get("id") or "").strip()
    if not cs_id:
        return "参数缺失：请提供 changeset: <变更集id>（先用 dev_list_changesets 查看）。"
    from regions import rollback_changeset
    ok, detail = rollback_changeset(root, cs_id)
    if not ok:
        return detail
    return f"已回滚变更集 {cs_id}：\n- " + "\n- ".join(detail)


# ---------------------------------------------------------------------------
# Agent 研判分区工具：勘察目录 → 获取基线建议 → 落地自定义方案 / 增补单分区。
# 默认 8 个分区仅作「初始建议」，真实分区由 Agent 依据代码库判断后应用。
# ---------------------------------------------------------------------------
def list_dir(arg):
    """浏览代码库内的目录结构（限定 code_root，供 Agent 研判代码库组织方式）。
    输入：可选 path: <相对 code_root 的目录，默认根目录>。返回该目录下子项（目录/文件）及大小/类型。
    这是 Agent 研判分区前的「勘察」工具：看清顶层有哪些模块/资源目录，再决定分区方案。"""
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    f = _parse_keyed(arg or "", ["path"])
    rel = (f.get("path") or "").strip().strip("'\"")
    if not rel:
        # 弱模型常按 list_dir(behaviors/) 裸参数调用（内联括号参数剥引号后无 path: 前缀）。
        # 整段输入若不含 key: 字段标记，就当作相对目录路径，避免静默回退根目录导致空转。
        bare = (arg or "").strip().strip("'\"")
        if bare and not re.search(r"^\s*[A-Za-z_]\w*\s*:", bare, re.M):
            rel = bare
    target, root_abs, reason = _resolve_in_root(rel if rel else root)
    if target is None:
        return reason or f"拒绝访问：{rel} 不在代码根目录内。"
    if not os.path.isdir(target):
        return f"不是目录：{rel or '(根目录)'}（请用 list_dir 浏览目录，不要传文件路径）。"
    try:
        entries = sorted(os.listdir(target))
    except Exception as e:  # noqa: BLE001
        return f"读取目录失败: {e}"
    rows = []
    for name in entries:
        p = os.path.join(target, name)
        if os.path.isdir(p):
            try:
                n = sum(1 for _ in os.scandir(p))
            except Exception:
                n = 0
            rows.append(f"[DIR ] {name}/  ({n} 项)")
        else:
            try:
                sz = os.path.getsize(p)
            except Exception:
                sz = 0
            rows.append(f"[FILE] {name}  ({sz} 字节)")
    rel_disp = os.path.relpath(target, root_abs)
    rel_disp = "(代码库根)" if rel_disp in (".", "") else rel_disp
    header = f"目录 {rel_disp} 共 {len(rows)} 项："
    return header + "\n" + "\n".join(rows) if rows else header + "（空）"


def dev_propose_regions(arg):
    """依据真实代码库结构，由 Agent 研判分区方案（默认 8 区仅作初始建议）。输入留空即可。
    返回：结构分析 + 建议分区清单（每区带 detected 证据与 included 建议）+ 代码库特有的可独立模块 + 中文结论。
    用法：先用 list_dir 勘察 → 调用本工具获取基线方案 → 据 detected/included 增删分区 → 用 dev_apply_regions 落地。"""
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    try:
        from regions import propose_regions
        res = propose_regions(root)
    except Exception as e:  # noqa: BLE001
        return f"研判分区失败: {e}"
    if not res.get("ok"):
        return res.get("error", "研判分区失败。")
    a = res["analysis"]
    lines = [res["summary"], ""]
    lines.append(f"结构分析：顶层目录 = {', '.join(a['top_level_dirs']) or '(无)'}")
    c = a["counts"]
    lines.append(f"资源计数：图像 {c['images']} / 音频 {c['audio']} / 模型 {c['models']} / 数据表 {c['data']} / 代码 {c['code']}")
    lines.append("")
    lines.append("建议分区（included=建议启用，detected=代码库检出信号）：")
    for r in res["proposed"]:
        flag = "✅启用" if r["included"] else "➖可选"
        det = "（已检出）" if r["detected"] else "（未检出）"
        ev = ("；证据：" + "、".join(r["evidence"])) if r["evidence"] else ""
        lines.append(f"- {r['key']}｜{r['name']}（{r['dir']}/） {flag}{det}：{r['reason']}{ev}")
    if res["custom_suggestions"]:
        lines.append("")
        lines.append("代码库特有的可独立分区（custom_suggestions，可据实际增删）：")
        for r in res["custom_suggestions"]:
            lines.append(f"- {r['key']}｜{r['name']}（{r['dir']}/）：{r['desc']}")
    lines.append("")
    lines.append("落地方式：把最终分区清单（JSON 数组，每项含 key/dir/name/depends_on 等）交给 dev_apply_regions 应用；"
                 "或仅追加一个分区用 dev_add_region。")
    return "\n".join(lines)


_APPLY_REGIONS_USAGE = (
    "参数缺失：请提供分区清单。可传 JSON 数组（或 {\"regions\":[...]} 对象），"
    "例如 regions: [{\"key\":\"values\",\"dir\":\"values\",\"name\":\"数值区\"}, ...]。"
)


def _parse_regions_payload(arg):
    """从工具入参里解析出分区清单。返回 (regions_list, err)。

    弱模型 / 原生 function-calling 给的入参格式很多样，这里全部兼容，避免因解析失败
    反复重试同一工具（这会耗尽上下文）：
      ① 整个 arg 就是 JSON：`{"regions":[...]}` 或裸数组 `[...]`（原生 tool_call 最常见）；
      ② `regions: <JSON>` 的 keyed 文本；
      ③ 上面都失败时用 ast.literal_eval 兜底 Python repr（单引号/True/None）形式。
    """
    whole = (arg or "").strip()
    if not whole:
        return None, _APPLY_REGIONS_USAGE
    keyed = (_parse_keyed(whole, ["regions"]).get("regions") or "").strip()
    # 先试 keyed 的值（`regions: [...]`），再试整段（裸 JSON），两者去重保序
    candidates = [c for c in (keyed, whole) if c]
    data, last_err = None, ""
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
    if data is None:
        import ast
        for cand in candidates:
            try:
                data = ast.literal_eval(cand)
                break
            except Exception:  # noqa: BLE001
                continue
    if data is None:
        return None, (f"regions 不是合法 JSON：{last_err}。请传入 JSON 数组"
                      "（可用 dev_propose_regions 的产出再裁减）。")
    if isinstance(data, dict) and isinstance(data.get("regions"), list):
        data = data["regions"]
    if not isinstance(data, list):
        return None, "regions 格式应为 JSON 数组，或 {\"regions\":[...]} 对象。"
    return data, ""


def dev_apply_regions(arg):
    """应用 Agent 研判后的自定义分区方案：把给定分区清单写入 regions.json 并初始化（建目录/每区 git/导出桩/规则）。
    输入：regions: <JSON 数组，或 {"regions":[...]} 对象>；也可直接给 JSON 数组 / 对象的原文。
    每个分区至少含 key 与 dir；可选 name/desc/depends_on/exports/verify。
    这是把「Agent 判断的分区」落地的关键一步；应用后 Agent 写操作即被约束到这些分区内。"""
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    regions_list, err = _parse_regions_payload(arg)
    if regions_list is None:
        return err
    from regions import init_regions
    ok, msg = init_regions(root, regions_list)
    if not ok:
        return "应用分区方案失败：" + msg
    # 重新注入分区契约，使 Agent 立即按新分区约束工作
    try:
        from ingest import load_project_rules
        rules = load_project_rules(os.path.abspath(root))
        set_runtime("project_rules", rules)
    except Exception:  # noqa: BLE001
        pass
    return "已应用 Agent 研判的分区方案。" + msg


def dev_add_region(arg):
    """向现有分区配置追加（或覆盖同名）一个分区，并立即初始化它（建目录/每区 git/规则）。
    输入：key: <分区key> 换行 dir: <目录> 换行 name: <中文名> 换行 [desc:] [access:] [depends_on:]（逗号分隔） [exports:]（逗号分隔）。
    用于 Agent 研判后按需增补单个分区，无需重传整个方案。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    f = _parse_keyed(arg or "", ["key", "dir", "name", "desc", "access", "depends_on", "exports"])
    key = (f.get("key") or "").strip()
    d = (f.get("dir") or "").strip()
    name = (f.get("name") or "").strip()
    if not key or not d:
        return "参数缺失：请提供 key: <分区key> 与 dir: <目录>（建议再给 name: <中文名>）。"
    from regions import load_region_config, init_regions
    cur = load_region_config(root)
    new_region = {
        "key": key,
        "dir": d.strip("/\\").replace("\\", "/"),
        "name": name or key,
        "desc": (f.get("desc") or "").strip(),
        "access": (f.get("access") or "").strip(),
        "depends_on": [x.strip() for x in (f.get("depends_on") or "").split(",") if x.strip()],
        "exports": [x.strip() for x in (f.get("exports") or "").split(",") if x.strip()],
        "verify": "",
    }
    cur = [r for r in cur if (r.get("key") or "").strip() != key]  # 覆盖同名
    cur.append(new_region)
    ok, msg = init_regions(root, cur)
    if not ok:
        return "新增/更新分区失败：" + msg
    try:
        from ingest import load_project_rules
        rules = load_project_rules(os.path.abspath(root))
        set_runtime("project_rules", rules)
    except Exception:  # noqa: BLE001
        pass
    return f"已新增/更新分区 {key}（{d}/）并写入 regions.json。" + msg


def dev_approve(arg):
    """审批敏感操作（提交/回滚/应用分区方案）前必须调用：记录一次审批，30 分钟内该操作放行。
    输入：action: <commit_region|commit_all|rollback_changeset|apply_regions|install_tool> 换行 target: <对象>
    target 精确匹配、不是通配符：commit_region 传分区 key（逐区审批，不能用 *）、
    rollback_changeset 传变更集 id、commit_all / apply_regions 固定传 *；
    MCP 的 mcp_server / mcp_capability 审批只能由用户在工作台确认，不能由 Agent 自己写入；
    Agent 遇到这两类阻断时应把候选、命令/URL 和能力清单展示给用户，等待用户操作。
    在调用 dev_commit / dev_commit_all / dev_rollback_changeset / dev_apply_regions / dev_add_region /
    分区类 dev_* 工具之前先调用本工具完成审批。
    MCP 工具返回 blocked / approval_required 时必须等待用户在工作台确认；
    不要调用本工具代替用户审批，也不要绕过阻断。"""
    f = _parse_keyed(arg or "", ["action", "target"])
    action = (f.get("action") or "").strip()
    target = (f.get("target") or "*").strip() or "*"
    if not action:
        return "参数缺失：请提供 action: <操作名>（如 commit_all / rollback_changeset / install_tool）。"
    if action in ("mcp_server", "mcp_capability"):
        return ("MCP 连接和能力路由必须由用户在工作台明确确认，Agent 不能自行审批。"
                "请展示 blocked 详情后等待用户在 MCP 设置中点击确认。")
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    from game_workbench import approval
    approval(root, action, "agent", approved=True, target=target)
    return f"已审批 {action}(target={target})，30 分钟内该操作放行。现在可执行对应的 dev_* 工具。"


def dev_approval_status(arg):
    """查询某敏感操作当前是否已通过审批。输入：action: <操作名> 换行 target: <对象>(默认 *)
    返回 approved: true/false。用于决定是否需要先调用 dev_approve。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, _ = res
    f = _parse_keyed(arg or "", ["action", "target"])
    action = (f.get("action") or "").strip()
    target = (f.get("target") or "*").strip() or "*"
    if not action:
        return "参数缺失：请提供 action: <操作名>。"
    from game_workbench import approval_status
    st = approval_status(root, action, target)
    return f"操作 {action}(target={target}) 审批状态：{'已通过' if st['approved'] else '未通过（需先调用 dev_approve）'}（有效期 {st['ttl_seconds'] // 60} 分钟）。"


def _tool_install_request(arg):
    text = str(arg or "").strip()
    if text.startswith("{"):
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    fields = _parse_keyed(text, ["manager", "package", "name", "version", "fallback_tools", "fallback"])
    if isinstance(fields.get("fallback_tools"), str):
        fields["fallback_tools"] = [x.strip() for x in fields["fallback_tools"].split(",") if x.strip()]
    if isinstance(fields.get("fallback"), str):
        fields["fallback"] = [x.strip() for x in fields["fallback"].split(",") if x.strip()]
    return fields


def dev_install_tool(arg):
    """在项目内隔离安装缺失工具；必须先通过 install_tool 审批。"""
    root = get_runtime("code_root") or CODE_ROOT
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    from agent_runtime.tool_install import ToolInstallManager, ToolInstallError
    request = _tool_install_request(arg)
    try:
        manager = ToolInstallManager(root)
        plan = manager.plan(request)
    except (ToolInstallError, TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
    from game_workbench import require_approval
    gate = require_approval(root, "install_tool", plan["approval_target"])
    if gate:
        return json.dumps({**gate, "plan": plan,
                           "alternatives": plan.get("alternatives", [])}, ensure_ascii=False)
    result = manager.install(request, approved=True)
    return json.dumps(result, ensure_ascii=False)[:6000]


def dev_tool_install_audit(arg):
    """查看项目内工具安装审计记录；不执行安装。"""
    root = get_runtime("code_root") or CODE_ROOT
    if not root:
        return json.dumps({"ok": False, "error": "未配置代码库"}, ensure_ascii=False)
    from agent_runtime.tool_install import ToolInstallManager
    fields = _parse_keyed(str(arg or ""), ["limit"])
    try:
        limit = int(fields.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return json.dumps({"ok": True, "items": ToolInstallManager(root).audit(limit)},
                      ensure_ascii=False)


def _region_file(root, rmap, key, rel_path):
    """Resolve a file inside a configured region and reject symlink/path escapes."""
    if key not in rmap:
        return None, f"未知分区：{key}"
    base = os.path.realpath(os.path.join(root, rmap[key]["dir"]))
    rel = (rel_path or "").strip().strip("'\"").replace("\\", "/")
    if not rel or os.path.isabs(rel) or any(p in ("", ".", "..") for p in rel.split("/")):
        return None, "素材路径必须是分区内的相对路径，禁止绝对路径和 ..。"
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        return None, "拒绝访问：路径不在目标分区内。"
    return target, None


def dev_asset_get(arg):
    """通过素材区接口取得素材引用；其它分区只能拿到素材区内的路径/元数据。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg or "", ["asset_id", "path", "consumer_region"])
    asset_id = (f.get("asset_id") or f.get("path") or "").strip()
    consumer = (f.get("consumer_region") or "").strip()
    if not asset_id:
        return "参数缺失：请提供 asset_id 或 path。"
    if consumer and consumer not in rmap:
        return f"未知调用分区：{consumer}"
    assets_dir = os.path.realpath(os.path.join(root, rmap.get("assets", {}).get("dir", "assets")))
    manifest = os.path.join(assets_dir, "manifest.json")
    rel = asset_id.replace("\\", "/").strip("/")
    # 支持 manifest 中的 id -> path 映射，也支持直接传相对路径。
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("assets", data) if isinstance(data, dict) else data
            if isinstance(entries, dict) and asset_id in entries:
                entry = entries[asset_id]
                rel = entry.get("path", "") if isinstance(entry, dict) else str(entry)
            elif isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("id") == asset_id:
                        rel = entry.get("path", "")
                        break
        except (OSError, ValueError):
            pass
    target, reason = _region_file(root, rmap, "assets", rel)
    if target is None:
        return reason
    if not os.path.isfile(target):
        return f"素材不存在：{rel}"
    return json.dumps({
        "ok": True, "asset_id": asset_id, "path": os.path.relpath(target, root).replace("\\", "/"),
        "consumer_region": consumer or None, "size": os.path.getsize(target),
        "extension": os.path.splitext(target)[1].lower(),
    }, ensure_ascii=False)


def dev_asset_register(arg):
    """注册素材到 assets/manifest.json；不复制文件，只建立稳定 ID。"""
    res, err = _require_regions()
    if res is None: return err
    root, rmap = res
    f = _parse_keyed(arg or "", ["asset_id", "path", "type", "license", "tags"])
    aid, rel = (f.get("asset_id") or "").strip(), (f.get("path") or "").strip()
    if not aid or not rel: return "参数缺失：请提供 asset_id 和 path。"
    target, reason = _region_file(root, rmap, "assets", rel)
    if target is None: return reason
    if not os.path.isfile(target): return f"素材不存在：{rel}"
    manifest = os.path.join(root, rmap.get("assets", {}).get("dir", "assets"), "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as fh: data = json.load(fh)
    except (OSError, ValueError): data = {}
    if not isinstance(data, dict): data = {}
    data.setdefault("assets", {})[aid] = {"path": rel.replace("\\", "/"), "type": f.get("type") or "unknown", "license": f.get("license") or "unknown", "tags": [x.strip() for x in (f.get("tags") or "").split(",") if x.strip()]}
    with open(manifest, "w", encoding="utf-8") as fh: json.dump(data, fh, ensure_ascii=False, indent=2)
    return json.dumps({"ok": True, "asset_id": aid, "path": rel}, ensure_ascii=False)


def dev_capture_bug(arg):
    """把异常堆栈/复现信息归档到 bugs 分区，返回可追踪的 Bug ID。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    f = _parse_keyed(arg or "", ["error", "exception", "traceback", "source_region", "reproduction", "severity", "title"])
    message = (f.get("error") or f.get("exception") or "未知异常").strip()
    source = (f.get("source_region") or "").strip()
    if source and source not in rmap:
        return f"未知来源分区：{source}"
    bug_id = "BUG-" + __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + __import__("uuid").uuid4().hex[:6]
    bugs_dir = os.path.realpath(os.path.join(root, rmap.get("bugs", {}).get("dir", "bugs")))
    os.makedirs(bugs_dir, exist_ok=True)
    record = {
        "id": bug_id, "title": (f.get("title") or message[:120]).strip(),
        "severity": (f.get("severity") or "error").strip(), "source_region": source or None,
        "error": message, "traceback": f.get("traceback") or "",
        "reproduction": f.get("reproduction") or "", "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "status": "open",
    }
    path = os.path.join(bugs_dir, bug_id + ".json")
    with open(path, "x", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return json.dumps({"ok": True, "bug_id": bug_id, "path": os.path.relpath(path, root).replace("\\", "/")}, ensure_ascii=False)


def dev_list_bugs(arg=""):
    """列出 bugs 分区中的结构化异常记录。"""
    res, err = _require_regions()
    if res is None:
        return err
    root, rmap = res
    base = os.path.join(root, rmap.get("bugs", {}).get("dir", "bugs"))
    rows = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    rows.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return json.dumps({"ok": True, "bugs": rows[:200]}, ensure_ascii=False)


def dev_update_bug(arg):
    """更新 Bug 状态（open/investigating/fixed/ignored）。"""
    res, err = _require_regions()
    if res is None: return err
    root, rmap = res
    f = _parse_keyed(arg or "", ["bug_id", "status"])
    bid, status = (f.get("bug_id") or "").strip(), (f.get("status") or "").strip().lower()
    if not bid or status not in {"open", "investigating", "fixed", "ignored"}:
        return "参数错误：bug_id 必填，status 必须是 open/investigating/fixed/ignored。"
    path = os.path.join(root, rmap.get("bugs", {}).get("dir", "bugs"), bid + ".json")
    if not os.path.isfile(path): return f"未找到 Bug：{bid}"
    with open(path, encoding="utf-8") as fh: record = json.load(fh)
    record["status"] = status
    record["updated_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh: json.dump(record, fh, ensure_ascii=False, indent=2); fh.write("\n")
    return json.dumps({"ok": True, "bug": record}, ensure_ascii=False)


def game_validate_data(arg=""):
    root = _get_code_root()
    from game_workbench import validate_data
    return json.dumps(validate_data(root), ensure_ascii=False) if root else "尚未配置代码库。"


def game_release_check(arg=""):
    root = _get_code_root()
    from game_workbench import release_check
    return json.dumps(release_check(root), ensure_ascii=False) if root else "尚未配置代码库。"


def game_upsert_task(arg):
    root = _get_code_root()
    if not root: return "尚未配置代码库。"
    from game_workbench import upsert_task
    f = _parse_keyed(arg or "", ["id", "title", "description", "region", "priority", "status", "owner", "files"])
    f["files"] = [x.strip() for x in (f.get("files") or "").split(",") if x.strip()]
    return json.dumps(upsert_task(root, f), ensure_ascii=False)

def game_simulate(arg):
    from game_workbench import simulate_growth
    f=_parse_keyed(arg or "",["levels","base","growth","model","k"])
    return json.dumps(simulate_growth(int(f.get("levels") or 50),float(f.get("base") or 100),float(f.get("growth") or 1.08),(f.get("model") or "geometric"),(float(f.get("k")) if f.get("k") not in (None,"") else None)),ensure_ascii=False)
def game_impact(arg):
    from game_workbench import impact_analysis
    f=_parse_keyed(arg or "",["query"]); return json.dumps(impact_analysis(_get_code_root(),f.get("query") or ""),ensure_ascii=False)
def game_playtest(arg):
    from game_workbench import playtest
    f=_parse_keyed(arg or "",["command","timeout"]); return json.dumps(playtest(_get_code_root(),f.get("command") or "",int(f.get("timeout") or 30)),ensure_ascii=False)


# ---------------------------------------------------------------------------
# game_screenshot：运行画面截图观察
#
# 优先级：已启用连接器的截图类 MCP 工具 → 当前项目嵌入引擎窗口 → 系统前台窗口。
# 截图是【观察素材】不是代码事实；无头/非 Windows/无窗口时返回明确文字失败，
# Agent 应改用运行日志、playtest 事件等证据，不允许臆测画面。
# ---------------------------------------------------------------------------
_SCREENSHOT_TOOL_KEYWORDS = ("screenshot", "capture_screen", "screen_capture",
                             "takescreenshot", "截图", "截屏", "抓屏", "画面捕获")
_SCREENSHOT_TOOL_BLOCKERS = ("video", "record", "录制", "录像")


def _is_screenshot_connector_tool(spec):
    blob = ((spec.get("name") or "") + " " + (spec.get("description") or "")).lower()
    if any(bad in blob for bad in _SCREENSHOT_TOOL_BLOCKERS):
        return False
    return any(k in blob for k in _SCREENSHOT_TOOL_KEYWORDS)


def _try_mcp_screenshot(root):
    """best-effort 调已启用连接器的截图类工具；任何失败/无图返回 None 降级。

    list_tools 可能冷启动 stdio 连接器（各带超时），多个连接器串行探测时
    用总时限封顶（DOCMIND_MCP_SHOT_PROBE_S，默认 8s）：连接器之间与截图调用
    均受剩余预算约束；极端情况下单次 stdio 冷启动本身可能超出预算，超时即
    回落本地抓窗，不影响截图主流程。
    """
    import time as _time
    try:
        probe_budget = float(os.getenv("DOCMIND_MCP_SHOT_PROBE_S", "8"))
    except (TypeError, ValueError):
        probe_budget = 8.0
    deadline = _time.monotonic() + max(1.0, probe_budget)
    try:
        connectors = mcp_client.connector_directory(root) or []
    except Exception:
        return None
    for conn in connectors:
        if not conn.get("enabled"):
            continue
        key = conn.get("key")
        if not key:
            continue
        remaining = deadline - _time.monotonic()
        if remaining <= 1.0:
            break
        try:
            listing = mcp_client.list_tools(root, key) or {}
            shot = next((t for t in (listing.get("tools") or [])
                         if _is_screenshot_connector_tool(t)), None)
            if not shot:
                continue
            if _time.monotonic() >= deadline:
                break
            # 单次截图调用也夹在剩余探测预算内（下限 1s），避免挂死连接器
            # 把整个截图动作拖到数分钟才回落本地抓窗。
            call_timeout = max(1.0, min(float(mcp_client.CALL_TIMEOUT),
                                        deadline - _time.monotonic()))
            resp = mcp_client.call_tool_with_fallback(
                root, key, shot["name"], {},
                task_hint="截取当前引擎运行画面截图",
                timeout=call_timeout)
        except Exception:
            continue
        if not resp.get("ok") or not resp.get("images"):
            continue
        first = resp["images"][0] or {}
        try:
            raw = base64.b64decode(first.get("data") or "", validate=False)
        except Exception:
            continue
        encoded = _encode_observation_image(raw)
        if encoded:
            return encoded, f"mcp:{key}/{shot['name']}"
    return None


def game_screenshot(arg=""):
    """截取当前运行画面作为视觉观察。输入可选 `target: embedded|foreground`（默认 embedded）。

    优先用已启用引擎连接器提供的截图能力；否则抓取工作台内嵌的引擎窗口，
    再不行抓系统前台窗口。返回图片保存路径（.docmind/screenshots/ 下的 JPEG）
    与图片观察内容。截图只反映某一瞬间的画面，不是代码事实；无窗口/无头环境会明确失败，
    此时请改用运行日志、受控 playtest 输出等证据。
    """
    fields = _parse_keyed(arg or "", ["target"])
    target = (fields.get("target") or "embedded").strip().lower()
    if target not in ("embedded", "foreground"):
        target = "embedded"
    root = _get_code_root()

    def preview_artifact(path, source):
        return [{
            "id": "game-screenshot",
            "kind": "image",
            "adapter": "game",
            "path": path,
            "label": "游戏运行画面",
            "summary": "由游戏截图工具捕获的当前运行画面",
            "evidence": ["game_screenshot", "source:" + str(source)],
            "metadata": {"capture_source": str(source)},
        }]

    if root:
        out_dir = os.path.join(root, ".docmind", "screenshots")
    else:
        out_dir = os.path.join(STATE_ROOT, "screenshots")

    if target == "embedded":
        hit = _try_mcp_screenshot(root) if root else None
        if hit:
            encoded, source = hit
            try:
                import screen_capture
                tag = source.replace("mcp:", "").replace("/", "-")
                saved_path = screen_capture.save_encoded_jpeg(encoded, out_dir, tag=tag)
            except Exception:  # noqa: BLE001
                saved_path = None
            if not saved_path:
                return "截图失败：连接器画面已返回但截图文件保存失败。请改用运行日志或受控 playtest 输出判断。"
            return ToolResult(
                ok=True,
                text=(f"截图完成（来源：连接器 {source}），已保存：{saved_path}\n"
                      "截图是观察素材，不是代码事实；请结合代码与日志复核。"),
                data={"images": [encoded], "image_sources": [saved_path]},
                artifacts=preview_artifact(saved_path, source))

    try:
        import screen_capture
    except Exception as exc:  # noqa: BLE001
        return f"截图失败：{type(exc).__name__}: {exc}（当前环境不支持画面捕获，请改用运行日志或受控 playtest 输出判断）"

    # 嵌入窗登记表按 project_id（prj-<sha1>）分桶，不是文件路径：
    # 传 root 会永不命中，多宿主时可能错截到别的项目窗口。
    project_pid = None
    try:
        from projects import current_project_id
        project_pid = current_project_id() or None
    except Exception:  # noqa: BLE001
        project_pid = None
    frame = None
    if target == "embedded":
        frame = screen_capture.grab_embedded(project_pid)
    if frame is None:
        frame = screen_capture.grab_foreground()
        source_kind = "foreground"
    else:
        source_kind = "embedded"
    if frame is None:
        return ("截图失败：未检测到可抓取的运行窗口（可能未运行、处于无头环境或非 Windows）。"
                "请先启动并嵌入引擎，或改用运行日志、受控 playtest 事件等证据，不要假设画面内容。")
    raw, width, height, _hwnd = frame
    saved = screen_capture.encode_and_save(raw, width, height, out_dir)
    if saved is None:
        return "截图失败：画面编码或保存失败。请改用运行日志或受控 playtest 输出判断。"
    encoded, path, size = saved
    return ToolResult(
        ok=True,
        text=(f"截图完成（来源：{source_kind} 窗口，原始 {width}x{height}，"
              f"输出 {size[0]}x{size[1]}）：{path}\n"
              "截图是观察素材，不是代码事实；请结合代码与运行日志复核。"),
        data={"images": [encoded], "image_sources": [path]},
        artifacts=preview_artifact(path, source_kind))


def dev_desktop_capture(arg=""):
    """Capture the current project's embedded or foreground desktop window.

    This is the runtime bridge for the desktop visual skill. It exposes
    observation only; state changes still require an app MCP or plugin.
    """
    fields = _parse_keyed(arg or "", ["target"])
    target = (fields.get("target") or "foreground").strip().lower()
    if target not in {"embedded", "foreground"}:
        return "桌面截图失败：target 只能是 embedded 或 foreground。"
    result = game_screenshot("target: " + target)
    if not isinstance(result, ToolResult):
        return result
    artifacts = []
    for artifact in list(result.artifacts or []):
        row = dict(artifact)
        row["adapter"] = "native"
        metadata = dict(row.get("metadata") or {})
        metadata["capture_adapter"] = "desktop_computer_use"
        metadata["target"] = target
        row["metadata"] = metadata
        row.setdefault("summary", "桌面自动化视觉技能捕获的当前窗口画面")
        artifacts.append(row)
    return ToolResult(
        ok=result.ok,
        text=result.text + ("\n画面仅用于观察；修改软件状态请调用已批准的应用 MCP 或插件。" if result.ok else ""),
        data=result.data, error_kind=result.error_kind, artifacts=artifacts,
        idempotency_key=result.idempotency_key, replayed=result.replayed)


def dev_desktop_action(arg=""):
    """Perform one approved click/type/drag/key action on a project window."""
    fields = _parse_keyed(str(arg or ""), [
        "action", "target", "x", "y", "to_x", "to_y", "text", "key",
    ])
    action = (fields.get("action") or "").strip().lower()
    target = (fields.get("target") or "embedded").strip().lower()
    if action not in {"click", "type", "drag", "key", "save"}:
        return json.dumps({"ok": False, "error": "action 只能是 click、type、drag、key 或 save。"}, ensure_ascii=False)
    if target not in {"embedded", "foreground"}:
        return json.dumps({"ok": False, "error": "target 只能是 embedded 或 foreground。"}, ensure_ascii=False)
    root = _get_code_root()
    if not root:
        return json.dumps({"ok": False, "error": "未配置当前项目"}, ensure_ascii=False)
    def integer(name):
        value = fields.get(name)
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("%s 必须是整数" % name)
    try:
        payload = {
            "action": action, "target": target,
            "x": integer("x"), "y": integer("y"),
            "to_x": integer("to_x"), "to_y": integer("to_y"),
            "key": (fields.get("key") or "")[:80],
            "text_sha256": hashlib.sha256((fields.get("text") or "").encode("utf-8")).hexdigest()[:16],
        }
        approval_target = "desktop:%s:%s:%s" % (
            action, target, hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12])
        gate = _mcp_gate_blocked(root, "desktop_action", approval_target)
        if gate:
            gate["hint"] = "请用户确认当前桌面动作；确认后必须用完全相同的参数重试。"
            return json.dumps(gate, ensure_ascii=False)
        import desktop_actions
        result = desktop_actions.perform(
            action, target=target, project_id=None,
            x=payload["x"], y=payload["y"], to_x=payload["to_x"], to_y=payload["to_y"],
            text=fields.get("text") or "", key=fields.get("key") or "")
        return json.dumps(result, ensure_ascii=False)
    except (OSError, TypeError, ValueError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def dev_serve(arg):
    """启动/停止本机开发服务器（vite、next、python -m http.server 等），并等到它真的能应答。

    输入（多行 keyed）：
      action: start|stop|status        # 默认 start
      cmd: npm run dev                 # start 必填；与 run_command 同一道命令黑名单
      cwd: frontend                    # 可选，代码根目录内的子目录
      port: 5173                       # 可选；不给就从服务自己的日志里认端口
      wait: 20                         # 就绪等待秒数，上限 120
      job_id: <id>                     # stop/status 用
    就绪之后用 `preview_project` 的 `url:` 打这个地址，就能拿到真实截图与 console/失败请求。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["action", "cmd", "cwd", "port", "wait", "job_id"]
    fields = _parse_keyed(arg, keys)
    bare = [ln.strip() for ln in (arg or "").splitlines()
            if ln.strip() and not re.match(r"^\s*(" + "|".join(keys) + r")\s*[:：]", ln)]

    def _one(key):
        value = fields.get(key) or ""
        return value.splitlines()[0].strip() if value else ""

    action = (_one("action") or "start").lower()
    from agent_runtime import dev_server
    if action in ("stop", "cancel"):
        job_id = _one("job_id") or (bare[0] if bare else "")
        if not job_id:
            return "dev_serve stop 需要 job_id: <后台任务 id>（start 的返回里就有）。"
        rep = dev_server.stop(job_id)
        if not rep.get("ok"):
            return "回收失败：" + str(rep.get("error"))
        return "已回收本机服务（job_id=%s，state=%s）。" % (rep.get("job_id"), rep.get("state"))
    if action == "status":
        job_id = _one("job_id") or (bare[0] if bare else "")
        if not job_id:
            return "dev_serve status 需要 job_id: <后台任务 id>。"
        view = dev_server.status(job_id)
        return json.dumps({key: value for key, value in (view or {}).items()
                           if key != "logs"}, ensure_ascii=False)

    command = _one("cmd") or (bare[0] if bare else "")
    if not command:
        return ("dev_serve start 需要 cmd，例如：\ncmd: npm run dev\ncwd: frontend\n"
                "（命令与 run_command 共用同一道黑名单，构建类命令请让它专心做服务。）")
    blocked, why = _cmd_is_blocked(command)
    if blocked:
        return "命令被门禁拦下（%s）：%s" % (why, command)
    try:
        port = int(_one("port") or 0)
    except (TypeError, ValueError):
        port = 0
    try:
        wait = float(_one("wait") or 20)
    except (TypeError, ValueError):
        wait = 20.0
    try:
        rep = dev_server.start(root, command, argv=_shell_argv(command),
                               cwd=_one("cwd"), port=port, wait=wait)
    except Exception as exc:  # noqa: BLE001
        return "dev_serve 无法运行：%s: %s" % (type(exc).__name__, str(exc)[:200])
    return dev_server.render(rep)


def dev_media(arg=""):
    """把合成的麦克风/摄像头内容注入真实浏览器：不用设备也能验语音回路。

    输入（多行 keyed）：
      action: build|run|voice      # 默认 run
      script: sil:0.3 talk:0.7 sil:1.3   # sil=精确静音 quiet=低于门限 talk=440Hz 0.4 幅度
      video: 6                     # 可选，>0 时同时伪造摄像头并推这么多帧
      out: fixtures/audio          # 可选，产物落在项目里的位置；默认 .docmind/media-fixture/<时间戳>
      wait: 10                     # run 的回环等待秒数
      tail: 10                     # 静音尾需要几片（默认 10＝1 秒）
    run：生成夹具 → 用假设备开关启动真实 Edge → 页面真的 getUserMedia → 回环 WS 探针按
    R0 协议逐包判定（包形 16k mono 100ms、内容对得上夹具、说完仍有 ≥10 片静音尾、静音尾
    让服务端收口、音频包先于视频包）。build：只出夹具文件与 `flags:` 参数，把它交给
    `preview_project` 就能让【项目自己的页面】在假设备上跑。
    voice：语音接线的行为回路——用前端自带的 esbuild 把生产模块 `liveAudioControl.ts` /
    `realtimeProtocol.ts` 编成 ESM 给页面 import（不是抄一份逻辑），胶水照抄开发舱的
    `handleLiveAudioFrame`/`sendNativeAudioReady`/`startLiveMic`，然后用假麦克风 + 真点击
    （连接/开麦/AI 开始说话）跑一遍。它能验 run 验不到的那一半：静音期门不开就一个包都不发、
    说完必须再推 ≥10 片静音然后【真的停下来】、AI 说话时开口要发出 cancel。
    判定说清边界：run 验的是采集→协议线上；voice 验的是生产函数被这样接起来时的行为，
    `.vue` 里的接线本身不是 import 出来的；能量 VAD 门限逻辑归前端 node:test 用例。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["action", "script", "video", "out", "wait", "tail"]
    fields = _parse_keyed(arg or "", keys)

    def _one(key, default=""):
        value = (fields.get(key) or "").strip()
        return value.splitlines()[0].strip() if value else default

    action = (_one("action") or "run").lower()
    from agent_runtime import media_fixture, page_action, voice_loop
    try:
        frames = int(_one("video") or 0)
    except (TypeError, ValueError):
        return "video 必须是整数帧数（0 表示不伪造摄像头）。"
    try:
        tail = int(_one("tail") or media_fixture.TAIL_CHUNKS)
    except (TypeError, ValueError):
        tail = media_fixture.TAIL_CHUNKS
    try:
        wait = float(_one("wait") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    try:
        if action == "build":
            fixtures = media_fixture.build_fixtures(root, script=_one("script"),
                                                    video_frames=frames, out=_one("out"))
            return media_fixture.render_fixtures(fixtures)
        if action == "voice":
            report = voice_loop.run_voice_loop(root, script=_one("script"), wait=wait,
                                               tail_min=tail)
            return voice_loop.render(report)
        if action != "run":
            return "dev_media 只支持 action: build|run|voice，收到 %r。" % action
        report = media_fixture.run_media_loop(root, script=_one("script"), video_frames=frames,
                                              wait=wait, tail_chunks=tail, out=_one("out"))
        return media_fixture.render(report)
    except media_fixture.MediaFixtureError as exc:
        return "媒体夹具无法构造：%s" % str(exc)[:300]
    except (voice_loop.VoiceLoopError, page_action.PageActionError) as exc:
        return "媒体回路未能开始：%s" % str(exc)[:300]
    except Exception as exc:  # noqa: BLE001
        return "dev_media 失败：%s: %s" % (type(exc).__name__, str(exc)[:240])


def dev_page_action(arg=""):
    """在本机网页里真的做动作并回报是否真的生效：点、填、按键、等、读。

    输入（多行 keyed）：
      action: open|click|type|press|wait|read|locate|list|close
      page: <会话 id>                # open 之外必填；open 的返回里就有
      url: http://127.0.0.1:<实际端口>/  # open 用；只允许本机回环。端口取 dev_serve 返回的那个，别写死
      entry: index.html              # open 的另一种：项目里的静态入口，临时起静态站
      selector: button.submit        # 定位一：CSS
      text: 保存                     # 定位二：可见文字（整串相等优先，落空才退到包含）
      nth: 0                         # 命中多个时【必须】显式指定要点第几个
      scope: .panel                  # 可选，限定 text 定位的搜索范围
      value: 要输入的文本            # type
      key: Enter                     # press；只支持固定键表，组合键请改用 click/type
      state: present|absent|visible|hidden|text   # wait
      mode: text|value|attr|html|count|contains   # read
      attr: href                    # read 的 mode: attr
      needle: probe_x.py            # read 的 mode: contains（selector 当容器，只回命中与周围小段）
      timeout: 4                    # wait 的等待秒数
      settle: 600                   # 动作后观察多久（毫秒，上限 5000）
      screenshot: true|false        # click/shot 是否需要截图
    动作后会给 effect：changed / navigated / reloaded / typed / error_seen / no_change，并附 DOM 变更、
    新 console 错误、新失败请求的计数。`no_change` 就是「没观察到变化」，不要当成功；
    `typed` 是输入框专用：value 不走 MutationObserver，DOM 计数恒 0，以回读到的内容为准。
    隐藏/禁用/歧义元素一律拒绝执行。用完必须 action: close 回收（同时最多 3 个会话）。
    `read` 的文本/结构输出有 4000 字上限（会明确标「已截断」），所以**别拿整页文本判断
    「这句话在不在」**：用 `mode: contains` + `selector: <容器>` + `needle: <词>`，命中数与周围
    几段小样本回来，返回量与页面大小无关；容器没匹配到或没扫完时会单独提醒「这不构成证据」。
    搜索按【可见文字】判定：`display:none` 里的命中不进 `hits`，另报 `hidden_hits`——「页面上有这句话」和「DOM 里有这句话」是两件事。
    """
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    keys = ["action", "page", "url", "entry", "selector", "text", "nth", "scope", "value",
            "key", "state", "mode", "attr", "needle", "timeout", "settle", "screenshot",
            # ⚠⚠ width / height 是 `preview_project` 的键，**不是本工具的**。
            # 实测有人把两者记混，喂进来后旧解析器把它们整块吞进 url，
            # 拼出 `http://127.0.0.1:8099/width: 1280height: 720` -> 404，
            # 而工具还回报「打开时就有 1 条 console 错误」，把账算在网页头上。
            # 现在列进来，好在下面**显式拒绝**并说清"那是 preview_project 的键"，
            # 而不是让它们悄悄毁掉 url。见 `_parse_keyed` 的说明。
            "width", "height"]
    fields = _parse_keyed(arg or "", keys)
    _unknown = fields.pop("__unknown__", None)
    _borrowed = [k for k in ("width", "height") if k in fields]
    if _borrowed:
        return ("dev_page_action 不接受 %s（那是 preview_project 的键，窗口大小由它管）。"
                "本工具只用 dev_serve 返回的那个回环地址，删掉 %s 再试。"
                % (", ".join(_borrowed), ", ".join(_borrowed)))
    if _unknown:
        return ("dev_page_action 有不认识的参数名：%s。"
                "可用：action/page/url/entry/selector/text/nth/scope/value/key/"
                "state/mode/attr/needle/timeout/settle/screenshot。"
                "（提示：写错的键名不会报错、会把上一个字段的值一起毁掉，"
                "所以这里直接拦下。）" % ", ".join(_unknown))

    def _one(key, default=""):
        value = fields.get(key)
        if value is None:
            return default
        text = str(value)
        return text.splitlines()[0].strip() if text.strip() and "\n" not in text.strip() else text.strip()

    def _flag(key, default):
        text = _one(key, "").strip().lower()
        if not text:
            return default
        return text in _BOOL_WORDS

    action = (_one("action") or "open").lower()
    from agent_runtime import page_action
    try:
        if action == "open":
            result = page_action.open_page(root, url=_one("url"), entry=_one("entry"))
        elif action == "click":
            result = page_action.click(root, page=_one("page"), selector=_one("selector"),
                                       text=_one("text"), nth=_one("nth"), scope=_one("scope"),
                                       settle=_one("settle", 600),
                                       screenshot=_flag("screenshot", True))
        elif action == "type":
            result = page_action.type_text(root, page=_one("page"), value=fields.get("value") or "",
                                           selector=_one("selector"), text=_one("text"),
                                           nth=_one("nth"), scope=_one("scope"),
                                           replace=_flag("replace", True),
                                           settle=_one("settle", 600),
                                           screenshot=_flag("screenshot", False))
        elif action == "press":
            result = page_action.press_key(root, page=_one("page"), key=_one("key"),
                                           selector=_one("selector"), settle=_one("settle", 600),
                                           screenshot=_flag("screenshot", False))
        elif action == "wait":
            result = page_action.wait_for(root, page=_one("page"), selector=_one("selector"),
                                          state=_one("state", "present"), value=_one("value"),
                                          timeout=_one("timeout", 4))
        elif action == "read":
            result = page_action.read_page(root, page=_one("page"), selector=_one("selector"),
                                           mode=_one("mode", "text"), attr=_one("attr"),
                                           nth=_one("nth"), needle=_one("needle"))
        elif action == "locate":
            result = page_action.locate(root, page=_one("page"), selector=_one("selector"),
                                        text=_one("text"), nth=_one("nth"), scope=_one("scope"))
        elif action in ("list", "status"):
            result = {"action": "list", "live": page_action.live_pages()}
        elif action == "close":
            result = page_action.close_page(_one("page"))
        else:
            return ("dev_page_action 不支持 action: %s（可用：open|click|type|press|wait|read|"
                    "locate|list|close）。" % action)
    except page_action.PageActionError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__
        if name == "VisualAcceptanceError":
            return "页面动作无法执行：%s" % str(exc)[:300]
        return "dev_page_action 失败：%s: %s" % (name, str(exc)[:240])
    text = page_action.render(result)
    shot = str(result.get("screenshot") or "")
    if not shot or not result.get("ok"):
        return text
    image = _screenshot_data_url(shot, root)
    if not image:
        return text
    return ToolResult(True, text, data={"images": [image], "image_sources": [shot]})


def dev_vue(arg=""):
    """把【真实的 .vue 组件】挂进真浏览器验接线：编译、渲染、真点击、收 emit。

    输入（多行 keyed）：
      component: src/workbench/components/WorkflowMembersDock.vue   # 相对前端目录
      props: {"active": true, "members": [...]}                     # JSON 对象
      clicks: .wmd-strip, .wmd-item#1                                # 顺序点击，#n 是第几个
      expect_present: .wmd-panel                                      # 点击后必须出现
      expect_absent: .wmd-head.old                                    # 必须不存在
      counts: .wmd-item=2                                             # 数量断言（选择器=数字）
      texts: 1/2                                                      # 渲染文本里必须有（按行分条）
      expect_emit: focus                                              # 必须是组件 defineEmits 声明过的
      expect_emit_args: ["t2"]                                        # 原样比较 emit 的实参
      composable: src/workbench/composables/workbench.ts              # 共享状态模块（同源自 import）
      store_call: useWorkbench                                        # 先取到那个单例对象
      setup: [{"call":"openRootMenu","args":[...]}]                   # 挂载前调它的方法
      timeout: 60
    这一条补的是最后一块没有守卫的接线层：`voice_loop` 里的胶水是我照 `AutonomousCockpit.vue`
    **转写**的，`.vue` 文件本身被改坏（props 改名、emit 从 id 变成 label、`v-if` 条件写反）
    以前没有任何测试会发现。这里挂的是**真组件**：用仓库自带的 `@vue/compiler-sfc`（vue-tsc
    带的，版本与 vue 一致）编、前端自己的 esbuild 打包、Wave13 的 `dev_page_action` 真点击。
    **零新依赖**——vitest 量化过不装（1 critical + 3 moderate + 1 high 公告，且 vitest3 要
    vite6/7 而本树是 vite5）。
    判定 fail-closed：什么都不断言（只挂不上判）＝未通过；`expect_emit` 写了组件没声明的名字
    ＝未通过（否则会退化成"永远等不到"的假守卫）；读状态那次点击抛错单独红，不让所有事实
    断言一起"莫名没成立"。
    边界：只证明这一个组件在这一组 props 下的行为，不证明整页集成，也不听真网关。
    """
    from agent_runtime import vue_component
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["component", "props", "clicks", "expect_present",
                                      "expect_absent", "counts", "texts", "expect_emit",
                                      "expect_emit_args", "composable", "store_call",
                                      "setup", "timeout"])

    def _one(key, default=""):
        value = (fields.get(key) or "").strip()
        if not value:
            return default
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return lines[0] if key in ("component", "timeout", "expect_emit") else "\n".join(lines)
    try:
        timeout = float(_one("timeout") or 60)
    except (TypeError, ValueError):
        return "timeout 必须是秒数（5~300）。"
    if not 5 <= timeout <= 300:
        return "timeout 只能在 5~300 秒之间。"
    try:
        report = vue_component.run_guard(
            root, component=_one("component"), props=_one("props"), clicks=_one("clicks"),
            expect_present=_one("expect_present"), expect_absent=_one("expect_absent"),
            counts=_one("counts"), texts=_one("texts"), expect_emit=_one("expect_emit"),
            expect_emit_args=_one("expect_emit_args"), composable=_one("composable"),
            store_call=_one("store_call"), setup=_one("setup"), timeout=timeout)
    except vue_component.VueComponentError as exc:
        return "组件守卫未能开始：%s" % str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - 挂不起来要报原因，不能报通过
        return "dev_vue 失败：%s: %s" % (type(exc).__name__, str(exc)[:240])
    return vue_component.render(report)


def dev_apptest(arg=""):
    """整页集成守卫：起【真后端】+ 真构建产物 + 真浏览器，断言页面真的从后端拿到了数据。

    输入（多行 keyed）：
      page: /workbench          # 站内路径（不带查询/片段）
      text: 资源管理器          # 页面上必须有的文字，按行分条（最多 8 条）
      tab: 素材                 # 要点开的工作区标签（默认「素材」）
      stream: 1                 # 再跑一档真流式：假麦克风 → 真 ws → scripted 离线适配器
      timeout: 120
    这一条补的是 `dev_vue` 明确不证明的那一段：组件守卫喂的是我自己写的 props，
    整页要的是 **真 uvicorn + 真 `web/` 构建产物 + 真 /api 往返**。做法：
    临时端口起 `api:app`（`DOCMIND_STATE_ROOT` 指到临时目录，绝不往仓库真登记表上写）→
    注册一个**只含这次夹具文件**的临时项目 → 打开页面 → 数外壳/标签、看文件树里有没有
    那个随机命名的文件（这就是"后端数据到了 DOM"的凭据，缓存冒充不了）→ 点工作区标签，
    要求活动标签与正文指纹都真的换了 → 拆服务、关页面、删沙箱并如实回报有没有删干净。
    `stream: 1` 再跑一档真流式：浏览器带**假麦克风**开页 → 进开发舱开摄像头（这条才会建 ws）
    → 开麦 → 要求页面字幕出现【由收到的音频算出来】的片数/字节数/峰值，并且字节数 ==
    片数×3200、助手回包引用同一个字节数、至少出两轮。适配器是 `scripted`（零网络零 key）。
    判定 fail-closed：后端没起来、状态根没隔离、console 有错、有失败请求、点击没换视图、
    正文被截断（`read_page` 有 4000 字上限，截断后的"没找到"不是证据）、现场没收回
    一律未通过。**`bundle_built_after_latest_source` 也会红**：`web/` 比 `frontend/src/**`
    旧时整页跑的是【旧构建】，绿了也不代表当前代码——先 `cd frontend && npm run build`。
    边界：证明的是集成那一段，不点真模型，也不替组件级断言背书；流式那一档证明的是
    **自家协议与页面接线**，厂商事件与打断语义仍归 R12 人跑。
    """
    from agent_runtime import app_test
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["page", "text", "tab", "stream", "timeout"])

    def _one(key, default=""):
        value = (fields.get(key) or "").strip()
        if not value:
            return default
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        return lines[0] if key in ("page", "tab", "timeout") else "\n".join(lines)
    try:
        timeout = float(_one("timeout") or 120)
    except (TypeError, ValueError):
        return "timeout 必须是秒数（20~600）。"
    if not 20 <= timeout <= 600:
        return "timeout 只能在 20~600 秒之间（整页要等后端冷启动）。"
    try:
        report = app_test.run_app_test(root, page=_one("page", "/workbench"),
                                       texts=_one("text"), tab=_one("tab", "素材"),
                                       stream=bool(_one("stream")), timeout=timeout)
    except app_test.AppTestError as exc:
        return "整页守卫未能开始：%s" % str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - 起不来要报原因，不能报通过
        return "dev_apptest 失败：%s: %s" % (type(exc).__name__, str(exc)[:240])
    return app_test.render(report)


def dev_testrun(arg=""):
    """跑 pytest 并回【结构化】判定：计数、逐条失败 nodeid、以及只重跑上次失败的。

    输入（多行 keyed）：
      paths: tests/test_a.py, tests/test_b.py   # 逗号分隔；留空＝全量
      keyword: voice                # 变成 `-k`，只当参数传，不拼进 shell
      only_failed: true             # 只重跑上一次记账里的失败 nodeid
      related: agent_runtime/x.py   # 按改动文件选出真实存在的相干测试
      coverage: true                # 附带覆盖率：哪些文件哪些行没被执行过
      timeout: 300                  # 秒，5~1500
    为什么不用 `run_command("pytest -q")` 再看文本：① pytest 在【一个用例都没收集到】时退出码
    是 5，读文本很容易当成"没看到 FAILED＝通过"，这里判 `tests_collected` 明确报未通过；
    ② 摘要说 N passed 而退出码不匹配时判 `counts_agree_with_exit`，防止输出被截断污染；
    ③ 本仓 `.pytest_cache` 写不进去（Permission denied），所以 pytest 自己的 `--lf` 不可靠，
    失败清单由本工具记账到项目状态目录，`only_failed` 用的是这份账；④ basetemp 一律钉在项目内
    （用户名带引号时默认 pytest-of-* 会 PermissionError）。
    覆盖率（`coverage: true`）：真跑一遍并回【哪些文件哪些行没被执行过】；配合 `related:`
    时按你改动的文件圈定范围——比一个全仓百分比有用得多，因为 87% 的横幅完全可能没碰到
    你刚写的三行分支。报告读不出来、或改动文件不在报告里，都明确说「覆盖率未知」而不是
    折算成数字。**覆盖率低不等于测试失败**，两者分开判；没装 coverage 时如实说缺依赖，
    不把「没测」报成 0%。选不出相干测试时报错而不是退化成跑全量。
    """
    from agent_runtime import test_run
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["paths", "keyword", "only_failed", "related",
                                      "coverage", "timeout"])

    def _one(key, default=""):
        value = (fields.get(key) or "").strip()
        return value.splitlines()[0].strip() if value else default

    def _flag(key):
        return _one(key).lower() in ("1", "true", "yes", "y", "on")
    try:
        timeout = float(_one("timeout") or test_run.DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        return "timeout 必须是秒数（5~1500）。"
    if not 5 <= timeout <= test_run.MAX_TIMEOUT:
        return "timeout 只能在 5~%s 秒之间；全量要更久请交给 CI 的 pytest lane。" % int(test_run.MAX_TIMEOUT)
    try:
        report = test_run.run(root, paths=_one("paths"), keyword=_one("keyword"),
                              only_failed=_flag("only_failed"), related=_one("related"),
                              coverage=_flag("coverage"), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 跑不成要报原因，不能报通过
        return "dev_testrun 失败：%s: %s" % (type(exc).__name__, str(exc)[:240])
    return test_run.render(report)


def dev_headcheck(arg=""):
    """把【提交本身】挂成干净检出跑一遍 import——专抓"工作树能跑、单独检出必红"。

    输入（多行 keyed）：
      ref: HEAD                  # 任何能解析成提交的写法；先钉成 sha 再验
      modules: agent, tools      # 逗号/换行分隔；默认 config,tools,agent
      timeout: 120               # 单步（建检出 / import 一个模块）的秒数上限
      gate: 1                    # 在那棵检出里真跑一遍 dev_eval 门禁整集（约 30~60 秒）
      tests: tests/test_a.py     # 逗号/换行分隔的仓内相对路径，同样在检出里跑（≤12 个）
      suite_timeout: 300         # 上面两档的单次秒数上限（30~300；跑批层被 process_runner 封顶）
    它为什么要独立存在：本仓 2026-10-01 出过一次 `git commit -- <路径>` 绕过索引，
    把别人未提交的 WIP 一起提交掉——引用那新文件的 import 进了 HEAD，新文件本身没进。
    于是【在这个工作树里跑测试永远是绿的】，只有单独检出 HEAD 才 ModuleNotFoundError，
    CI 和别人的 worktree 全红。工作树里跑不出这种错，所以只能回到"只有已提交内容"的树上跑。
    失败会归因：缺的模块【在磁盘上但不在提交里】= 别人未提交的新文件被提交引用；
    【两边都不在】= 漏提交、改名，或它本该是第三方依赖。检出用完一定收回，且没有本工具的
    归属标记就绝不删（不拆别人的检出）。
    为什么还要 `gate` / `tests`：2026-10-02 又付了一次同型的账——hunk 暂存时收进了别人那段
    的**尾巴**（6 条断言），而它们读的局部量还在别人未提交的上半段里。import 全绿、只验
    import 的自检也全绿，**行为**红。所以这一档在干净检出里真跑题集；红了先在**同一棵检出**
    里重跑一次（门禁题里有计时敏感的，抖一下不算红），重跑仍红才拿工作树跑同一条当对照组：
    「只有检出红」= 差别在没提交的那部分**或**未跟踪的工具链，报告会把失败项名字列出来让你
    分辨是哪一种；「两边都红」= 负载、缺工具链或真坏了，那不是自洽问题。
    边界：这验的是【只有已提交内容时能不能跑成】，不验行为对不对；入口之外的深层 import 要靠
    import 链真的走到才暴露。要未跟踪工具链的套件（`frontend/node_modules`、`web/`）在
    新检出里必然红，那是环境不是提交坏了——看对照组。
    """
    from agent_runtime import head_check
    root = _get_code_root()
    if not root:
        return "尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。"
    fields = _parse_keyed(arg or "", ["ref", "modules", "timeout", "gate", "tests",
                                      "suite_timeout"])

    def _one(key, default=""):
        value = (fields.get(key) or "").strip()
        return value.splitlines()[0].strip() if value else default

    names = [item.strip() for item in re.split(r"[,\s]+", _one("modules")) if item.strip()]
    targets = [item.strip() for item in re.split(r"[,\s]+", _one("tests")) if item.strip()]
    want_gate = _one("gate").lower() in ("1", "true", "yes", "on", "y")
    try:
        timeout = float(_one("timeout") or 120)
    except (TypeError, ValueError):
        return "timeout 必须是秒数。"
    if not 5 <= timeout <= 600:
        return "timeout 只能在 5~600 秒之间。"
    try:
        suite_timeout = float(_one("suite_timeout") or 300)
    except (TypeError, ValueError):
        return "suite_timeout 必须是秒数。"
    if not 30 <= suite_timeout <= head_check.SUITE_TIMEOUT_MAX:
        return "suite_timeout 只能在 30~%d 秒之间（跑批层的上限）。" % head_check.SUITE_TIMEOUT_MAX
    if len(targets) > head_check.MAX_TEST_TARGETS:
        return "tests 一次最多 %d 个目标。" % head_check.MAX_TEST_TARGETS
    try:
        report = head_check.check(root, ref=_one("ref", "HEAD"),
                                  modules=tuple(names) if names else None,
                                  timeout=timeout, gate=want_gate, tests=tuple(targets),
                                  suite_timeout=suite_timeout)
    except Exception as exc:  # noqa: BLE001 - 自检失败必须报原因，不能报通过
        return "dev_headcheck 失败：%s: %s" % (type(exc).__name__, str(exc)[:240])
    return head_check.render(report)


def _screenshot_data_url(relative: str, root: str):
    """把页面动作留下的截图读成 data URL；越界路径或不实图像直接放弃附图。"""
    base = os.path.realpath(root)
    path = os.path.realpath(os.path.join(base, *str(relative).replace("\\", "/").split("/")))
    if path != base and not path.startswith(base + os.sep):
        return None
    try:
        with open(path, "rb") as handle:
            return _encode_observation_image(handle.read())
    except OSError:
        return None


def preview_project(arg=""):
    """在正式开发舱中捕获当前项目的真实网页画面。

    这是视觉验收适配器的通用入口：截图会同时作为模型视觉观察和工作流
    preview artifact 保存。非网页项目应改用领域工具（如 game_screenshot 或
    已启用的 EDA MCP），不能把缺少画面误报成通过。

    两种打法：① 不给 url 时，把项目目录当静态站临时服务并打开 entry（默认
    index.html / web/index.html）；② 给 `url: http://127.0.0.1:<实际端口>/` 时直接打这个
    【已经在跑的本机服务】——Vite/Vue 这类必须起 dev server 的前端要用这种，
    服务由 dev_serve 起来。url 只允许回环地址，且**端口要用 dev_serve 返回的那个**：
    端口被占时 vite 会往后挪（5173→5174），写死会打到死地址，或打到**别人的**服务上、
    拍到别的项目还报成功。给定地址不通时会报错并列出在应答的候选端口，由你核对。

    还能注入合成媒体：`flags:` 后面每行一条浏览器启动参数，直接用 dev_media
    （action: build）返回的那几条，把麦克风/摄像头换成 WAV/Y4M 夹具的内容，这样项目
    自己的页面在没有真设备时也能跑语音/摄像头回路。参数只接受 `--开关` 形态，且
    `--user-data-dir`、`--load-extension` 一类能改写浏览器行为的一律拒绝。
    """
    root = _get_code_root()
    if not root:
        return ToolResult(False, "真实预览失败：尚未配置当前项目。", error_kind="configuration")
    fields = _parse_keyed(arg or "", ["entry", "width", "height", "timeout", "url", "flags"])
    extra_flags = [line.strip() for line in (fields.get("flags") or "").splitlines()
                   if line.strip()]
    try:
        from agent_runtime.visual_acceptance import capture_project_preview
        report = capture_project_preview(
            root,
            entry=fields.get("entry") or "",
            width=int(fields.get("width") or 960),
            height=int(fields.get("height") or 540),
            timeout=float(fields.get("timeout") or 25),
            url=(fields.get("url") or "").splitlines()[0].strip() if fields.get("url") else "",
            extra_flags=extra_flags,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(False, f"真实预览失败：{type(exc).__name__}：{str(exc)[:300]}",
                          error_kind="visual_capture")
    screenshot = report.get("screenshot") or ""
    console_errors = list(report.get("console_errors") or [])
    failed_requests = list(report.get("failed_requests") or [])
    runtime_errors = list(report.get("runtime_errors") or [])
    signals = (f"运行时异常 {len(runtime_errors)} 条 / console 错误 {len(console_errors)} 条 / "
               f"失败请求 {len(failed_requests)} 个")
    text = signals + "\n" + json.dumps({key: value for key, value in report.items() if key != "image"},
                                       ensure_ascii=False)
    # A failed or domain-specific adapter may return a bounded report without
    # an image. Keep the tool failure observable instead of raising a secondary
    # KeyError while constructing the result, and never claim that a screenshot
    # reached the model when it did not.
    image = str(report.get("image") or "").strip()
    data = {"images": [image] if image else [],
            "image_sources": [screenshot] if screenshot else []}
    return ToolResult(
        bool(report.get("passed")),
        ("真实浏览器预览已完成：" if report.get("passed") else "真实浏览器预览未通过：") + text,
        data=data,
        artifacts=list(report.get("artifacts") or []),
        error_kind="" if report.get("passed") else "visual_acceptance_failed",
    )


# ---------------------------------------------------------------------------
# start_workflow：对话内发起【跨窗口持久开发工作流】
#
# 与 delegate / orchestrate 的边界：
# - delegate：一个相对独立的子任务，一轮内取回结论；
# - orchestrate：一轮内当场并行调度任务图并合成，会话结束即消散；
# - start_workflow：多阶段/多角色/含副作用阶段/需要人工方案门+审批门+跨窗口
#   恢复时才升级。只创建状态并停在方案选择门，绝不直接执行有副作用任务。
# ---------------------------------------------------------------------------
_BOOL_WORDS = {"1", "true", "yes", "on", "是", "要", "联网", "开"}


def start_workflow(arg=""):
    """发起一个跨窗口持久、带人工门的开发工作流（通用开发/游戏/EDA 等领域）。

    输入（keyed 多行，也可直接把整段目标作为入参）：
      goal: 完整目标（必填；未写字段名时整段入参即目标）
      kind: generic|game|eda（缺省 generic 通用开发；game=游戏开发；eda=原理图/PCB 电子设计）
      web:  true|false（缺省继承本轮会话联网开关）

    工具只负责创建工作流：它停在「方案选择门」，用户在对话中选择方案、
    确认任务 DAG 并审批后，多个子代理才会开始执行；本工具不会替用户
    执行任何有副作用任务，也不能跳过人工审批。无代码库根目录时明确失败且不落任何记录。
    """
    fields = _parse_keyed(arg or "", ["goal", "request", "kind", "web", "web_enabled"])
    goal = (fields.get("goal") or fields.get("request") or "").strip()
    if not goal:
        goal = (arg or "").strip()
    if not goal:
        return ("start_workflow 失败：缺少目标。请在 goal: 后写清要推进的完整目标"
                "（多阶段、可验收），或直接把目标整段作为入参。")

    kind_raw = (fields.get("kind") or "").strip().lower()
    from agent_runtime.workflow_profiles import get_profile, normalize_kind
    kind = normalize_kind(kind_raw) if kind_raw else "generic"
    profile = get_profile(kind)

    web_raw = fields.get("web")
    if web_raw is None:
        web_raw = fields.get("web_enabled")
    if web_raw is not None and str(web_raw).strip():
        web_enabled = str(web_raw).strip().lower() in _BOOL_WORDS
    else:
        web_enabled = _session_web_enabled.get()

    root = _get_code_root()
    if not root:
        return ("start_workflow 失败：当前没有配置项目代码库根目录，无法创建可跨窗口"
                "恢复的开发工作流。请先在工作台打开/索引项目后再发起；本次未创建任何工作流记录。")
    if _WORKFLOW_LAUNCHER is None:
        return ("start_workflow 失败：工作流启动通道未初始化（工作台路由未注册 launcher）。"
                "请改由 AI 运行台的「开发工作流」面板手动发起。")

    try:
        workflow = _WORKFLOW_LAUNCHER(goal, kind=kind, web_enabled=bool(web_enabled))
    except Exception as exc:  # noqa: BLE001 - 工具观察必须给出明确失败而不是抛断回合
        return f"start_workflow 失败：{type(exc).__name__}: {exc}。请改由 AI 运行台手动发起。"
    if not isinstance(workflow, dict) or not workflow.get("workflow_id"):
        return "start_workflow 失败：启动器未返回有效工作流状态，请改由 AI 运行台手动发起。"

    wid = workflow.get("workflow_id")
    status = workflow.get("status") or "awaiting_choice"
    _notify_workflow_started(workflow)
    lines = [
        f"已创建开发工作流（领域：{profile.display_name}；状态：{status}）。",
        "工作流当前停在【方案选择门】：尚未执行任何任务，工具不会替你执行有副作用的操作。",
        "请在对话中选择方案、确认任务 DAG 并审批后才会开始执行；",
        "审批前可修改任务与依赖，执行失败会按策略重规划，进度可跨窗口恢复。",
    ]
    options = workflow.get("options") or []
    if options:
        lines.append("可选方案：")
        for opt in options:
            if not isinstance(opt, dict):
                continue
            lines.append("· %s：%s — %s" % (
                opt.get("id", ""), opt.get("title", ""), opt.get("summary", "")))
    lines.append(f"工作流卡片已在对话中展开（ID：{wid}），等待用户选择方案。")
    return "\n".join(lines)


# ===========================================================================
# 写后自验证工具 self_verify（Phase 1/2「写后自验证闭环」收尾门）
# 详见 docs/agent-self-verify-memory.md。Agent 在 apply_edit / create_file 等写操作
# 成功后自动调用本工具对改动做轻量校验；失败信息回填模型、触发 ReAct 自修
# （复用 agent.py 既有 _FAILURE_MARKERS / _TOOL_FAIL_LIMIT / _TOTAL_FAIL_LIMIT 护栏）。
# 校验策略按改动文件类型分 scope（auto 模式自动选）：
#   backend  (.py)            → py_compile 每个改动文件 + 命中则跑对应 tests/test_<module>.py
#   frontend (.ts/.vue)       → npm run typecheck（严禁 build，并发铁律）
#   web      (.html/.htm/.css)  → HTML 标签闭合（stdlib html.parser）+ 内联 <script>
#                                 node --check 语法校验（无 node 则降级 skip）。
#                                 单文件 HTML 是 vibecoding 最高频产物，原先没有校验器
#                                 → status 判 skipped → 收尾门把最终答复整段换成
#                                 「不能确认任务成功」，用户看着文件已落盘却被判未验证。
#   visual   (.html/.htm 网页)   → 真浏览器截图 + console 错误/失败请求门（web 门之上
#                                 的第二级：只验画面与运行期异常）。auto 档与 web 门
#                                 一起挂起，DOCMIND_SELF_VERIFY_VISUAL=0 可关，起不了
#                                 浏览器 fail-closed（无像素证据绝不判通过）。
#   engine  (引擎嵌入模块)    → 需真 Godot GUI，默认仅 scope 显式指定 / scope:all /
#                              开 DOCMIND_SELF_VERIFY_ENGINE=1 时运行 verify_engine_embed.py，
#                              否则自动策略里降级为 skip（避免每次写都拉起 Godot 打断并发写入者）
# 任何异常一律降级为「跳过该 scope」，绝不因校验器自身故障阻断问答主流程。
# 入参与文本协议 Action Input 同形：单行 "scope: auto" + 多行 "files: a.py\nfiles: b.py"
# （前端自验证也可用 "files: frontend/src/x.ts"）。
# ===========================================================================

def _parse_self_verify_arg(arg):
    """把 self_verify 的入参字符串解析为 (scope, files)。"""
    arg = (arg or "").strip()
    scope = "auto"
    files_parts = []
    has_scope = False
    if not arg:
        return scope, None
    for line in arg.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("scope:"):
            scope = (s[len("scope:"):].strip() or "auto")
            has_scope = True
        elif low.startswith("files:"):
            rest = s[len("files:"):].strip()
            if rest:
                files_parts.append(rest)
    files = None
    if files_parts:
        files = [x.strip() for part in files_parts for x in re.split(r"[,\n]", part) if x.strip()]
    # 纯文本（无 scope:/files: 关键字）→ 整段按文件列表处理
    if files is None and not has_scope:
        cand = [x.strip() for x in re.split(r"[,\n]", arg) if x.strip()]
        if cand:
            files = cand
    return scope, files


def _sv_changed_via_git(root):
    """用 git diff 自动发现 code_root 内最近改动文件（staged + unstaged），返回绝对路径列表。"""
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", root],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        files = [os.path.abspath(os.path.join(root, l.strip())) for l in (out.stdout or "").splitlines() if l.strip()]
        out2 = subprocess.run(
            ["git", "diff", "--name-only", "--cached", "--", root],
            capture_output=True, text=True, timeout=15, cwd=root,
        )
        for l in (out2.stdout or "").splitlines():
            if l.strip():
                ap = os.path.abspath(os.path.join(root, l.strip()))
                if ap not in files:
                    files.append(ap)
        return files
    except Exception:  # noqa: BLE001 —— git 不可用/失败一律退化为空（由调用方决定）
        return []


def _sv_classify(targets):
    """把文件列表分到各 scope 的桶里，返回 (py, frontend, scene, engine, web) 绝对路径列表。

    web 桶 = 前端**静态产物**（.html/.htm/.css），定义唯一真源是 `_sv_web_files`
    （合并两侧实现时统一：一个「web 集合」两种说法会让 auto 档的判定出现分叉）。
    静态产物在 auto 档会挂两级门：`web`（离线结构/语法）与——仅 .html/.htm——
    `visual`（真浏览器画面），见 self_verify 的 scope 选择。
    """
    import re as _re
    py, fe, scene, engine = [], [], [], []
    for t in targets:
        p = str(t)
        low = p.lower()
        if low.endswith(".py"):
            py.append(p)
        if low.endswith((".ts", ".tsx", ".vue", ".js")) or "/frontend/" in low:
            fe.append(p)
        if _re.search(r"scene", low):
            scene.append(p)
        if "engine_embed" in low or ("embed" in low and "desktop" in low) or low.endswith("desktop_bridge.py"):
            engine.append(p)
    return py, fe, scene, engine, _sv_web_files(targets)


# 前端**静态产物**扩展名（单文件 HTML / 样式表）——web 桶的唯一真源，
# `_sv_classify` 与 `web` scope 都从这里取，避免两个「web 集合」各说各话。
_WEB_EXTS = (".html", ".htm", ".css")
# 能截出画面的子集：样式表没有「画面」可验，visual 门只对网页跑。
_WEB_PAGE_EXTS = (".html", ".htm")


def _sv_web_files(targets):
    """挑出可用纯前端方式校验的静态产物（.html/.htm/.css）。

    为什么必须单列：`.html` 既不是 backend(.py) 也不是 frontend(frontend/**)，
    原先一个桶都不进 → `ran` 为空 → status 判 skipped → 收尾门把最终答复整段
    替换成「修改尚未完成验证，不能确认任务成功」。用户明明看着文件落盘了，却被
    判未验证。单文件 HTML 是 vibecoding 最高频的产物，这个洞卡在主路上。
    """
    return [str(t) for t in targets if str(t).lower().endswith(_WEB_EXTS)]


def _sv_web_pages(targets):
    """从 web 桶里挑出**有画面**的网页（.html/.htm），供 visual 门截图。

    .css 也在 web 桶里（有花括号平衡校验器），但它没有画面，起浏览器截图对它
    无意义 —— 不给它挂 visual，否则「改一行样式就开一次浏览器」。
    """
    return [str(t) for t in targets if str(t).lower().endswith(_WEB_PAGE_EXTS)]


def _sv_verify_backend(py_files, root, result):
    """后端校验：py_compile 每个改动 .py；命中则跑对应单测（有界到改动模块）。"""
    import py_compile
    for f in py_files:
        # 待校验文件不存在 → 记为失败（Agent 让校验一个不存在的文件，说明写/路径出错），
        # 不能降级成 skip 假通过（否则 self_verify 永远 passed:true，失去校验意义）。
        if not os.path.isfile(f):
            result["failures"].append({
                "scope": "backend", "file": f,
                "error": "待校验文件不存在：" + f,
            })
            continue
        try:
            py_compile.compile(f, doraise=True)
            result["ran"].append("py_compile:" + (os.path.relpath(f, root) if root else f))
        except py_compile.PyCompileError as e:
            result["failures"].append({
                "scope": "backend", "file": f,
                "error": "语法校验失败：" + str(getattr(e, "msg", e)),
            })
            continue
        # 命中单测：只跑确实存在且与改动模块对应的 tests/test_<module>.py，
        # 避免改一处就跑全仓 1000+ 用例（全量回归由 CI 门负责）。
        base = os.path.splitext(os.path.basename(f))[0]
        test_path = os.path.join(root or os.getcwd(), "tests", "test_" + base + ".py")
        if not os.path.isfile(test_path):
            continue
        try:
            interpreter = (sys.executable if stage_execution_active() else "python") \
                if execution_mode() == "enterprise" else sys.executable
            r = _self_verify_command(
                [interpreter, "-B", "-m", "unittest", "tests.test_" + base, "-v"],
                project_root=root or os.getcwd(), timeout=120,
            )
            if r.returncode != 0:
                tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-1200:]
                result["failures"].append({
                    "scope": "backend", "file": f,
                    "error": "单测 tests.test_%s 未通过：\n%s" % (base, tail),
                })
            else:
                result["ran"].append("unittest:tests.test_" + base)
        except subprocess.TimeoutExpired:
            result["failures"].append({
                "scope": "backend", "file": f,
                "error": "单测 tests.test_%s 超时（>120s）未结束。" % base,
            })
        except Exception as e:  # noqa: BLE001
            result["failures"].append({"scope": "backend", "file": f,
                                       "error": "单测无法运行：" + str(e)[:200]})


def _sv_verify_frontend(fe_files, root, result):
    """前端校验：仅 npm run typecheck（严禁 build，并发铁律）。"""
    import shutil
    for cand in ("frontend", "web", "ui"):
        d = os.path.join(root, cand) if root else cand
        if os.path.isdir(d):
            fe_dir = d
            break
    else:
        result["ran"].append("frontend:skip（未找到前端目录）")
        return
    pkg = os.path.join(fe_dir, "package.json")
    if not os.path.isfile(pkg):
        result["ran"].append("frontend:skip（无 package.json）")
        return
    try:
        with open(pkg, encoding="utf-8") as f:
            scripts = (json.load(f).get("scripts") or {})
    except Exception:  # noqa: BLE001
        scripts = {}
    if "typecheck" not in scripts:
        result["ran"].append("frontend:skip（无 typecheck 脚本）")
        return
    npm = os.getenv("DOCMIND_NPM_BIN") or shutil.which("npm")
    if not npm and execution_mode() != "enterprise":
        result["ran"].append("frontend:skip（未找到 npm）")
        return
    try:
        executable = "npm" if execution_mode() == "enterprise" else npm
        r = _self_verify_command([executable, "run", "typecheck"],
                                 project_root=fe_dir, timeout=180)
        if r.returncode != 0:
            tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-1200:]
            result["failures"].append({
                "scope": "frontend", "file": fe_dir,
                "error": "typecheck 未通过：\n" + tail,
            })
        else:
            result["ran"].append("frontend:typecheck")
    except subprocess.TimeoutExpired:
        result["failures"].append({"scope": "frontend", "file": fe_dir,
                                   "error": "typecheck 超时（>180s）。"})
    except Exception as e:  # noqa: BLE001
        result["failures"].append({"scope": "frontend", "file": fe_dir,
                                   "error": "typecheck 无法运行：" + str(e)[:200]})


def _sv_verify_web(web_files, root, result):
    """静态产物校验：HTML 标签闭合（stdlib）+ 内联脚本 node --check（无 node 则降级）。

    刻意不 build、不联网、不写仓库：这是**每次写后**都跑的收尾门，任何副作用都会
    打断并发写入者（frontend scope 只跑 typecheck 也是同一个理由）。
    `node --check` 只做语法解析、不执行，可以安全地作用在刚生成的脚本上。
    """
    import shutil
    import tempfile
    from html.parser import HTMLParser

    # HTML5 允许省略结束标签的元素。不把它们算进「未闭合」，否则 `<p>a<p>b`
    # 这种完全合法的写法会被判失败 —— 收尾门一旦假红，模型就会去改本来正确的
    # 文件，比漏检更糟。
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}
    optional_end = {"html", "head", "body", "p", "li", "dt", "dd", "rt", "rp",
                    "option", "optgroup", "thead", "tbody", "tfoot", "tr", "td",
                    "th", "colgroup", "caption", "frameset", "legend"}

    class _Balance(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.errors = []
            self.scripts = []          # [(起始行号, 正文)]

        def handle_starttag(self, tag, attrs):
            if tag in void_tags:
                return
            # 同名标签重复出现时，前一个由 HTML 规范自动闭合（<p>a<p>b、<td>x<td>y）
            if tag in optional_end and self.stack and self.stack[-1][0] == tag:
                self.stack.pop()
            self.stack.append((tag, self.getpos()[0]))

        def handle_endtag(self, tag):
            if tag in void_tags:
                return
            # 结束标签到来时，栈顶的「可省略结束标签」元素按规范隐式闭合：
            # `<p>a<p>b</body>` 里两个 <p> 都是被 </body> 关掉的。不先弹就会把
            # 合法 HTML 判成错配 —— 收尾门假红会诱导模型去改本来正确的文件。
            while (self.stack and self.stack[-1][0] != tag
                   and self.stack[-1][0] in optional_end):
                self.stack.pop()
            if not self.stack:
                if tag not in optional_end:
                    self.errors.append("第 %d 行多余的 </%s>" % (self.getpos()[0], tag))
                return
            if self.stack[-1][0] != tag:
                self.errors.append(
                    "第 %d 行 </%s> 与第 %d 行未闭合的 <%s> 错配"
                    % (self.getpos()[0], tag, self.stack[-1][1], self.stack[-1][0]))
                # 栈里找同名标签并连它上面的全部出栈，避免一处错配连锁报 N 条
                for i in range(len(self.stack) - 1, -1, -1):
                    if self.stack[i][0] == tag:
                        del self.stack[i:]
                        return
                return
            self.stack.pop()

        def handle_data(self, data):
            # script/style 走 CDATA 模式，这里拿到的是未解析标签的原文
            if self.stack and self.stack[-1][0] == "script":
                self.scripts.append((self.getpos()[0], data))

    for f in web_files:
        rel = os.path.relpath(f, root) if root else f
        if not os.path.isfile(f):
            # 与 backend scope 同判据：让校验一个不存在的文件 = 写/路径出错，
            # 不能降级成 skip 假通过。
            result["failures"].append({"scope": "web", "file": f,
                                       "error": "待校验文件不存在：" + f})
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:  # noqa: BLE001
            result["failures"].append({"scope": "web", "file": f,
                                       "error": "读取失败：" + str(e)[:200]})
            continue
        if not text.strip():
            result["failures"].append({"scope": "web", "file": f,
                                       "error": "文件为空，无法确认产物有效"})
            continue

        if f.lower().endswith(".css"):
            if text.count("{") != text.count("}"):
                result["failures"].append({
                    "scope": "web", "file": f,
                    "error": "CSS 花括号不闭合（{ %d 个 / } %d 个）"
                             % (text.count("{"), text.count("}"))})
            else:
                result["ran"].append("css_balance:" + rel)
            continue

        parser = _Balance()
        try:
            parser.feed(text)
            parser.close()
        except Exception as e:  # noqa: BLE001
            result["failures"].append({
                "scope": "web", "file": f,
                "error": "HTML 解析失败：%s：%s" % (type(e).__name__, str(e)[:200])})
            continue
        for tag, line in parser.stack:
            if tag not in optional_end:
                parser.errors.append("第 %d 行的 <%s> 未闭合" % (line, tag))
        if parser.errors:
            result["failures"].append({
                "scope": "web", "file": f,
                "error": "HTML 结构校验失败：" + "；".join(parser.errors[:4])})
        else:
            result["ran"].append("html_structure:" + rel)

        scripts = [(ln, s) for ln, s in parser.scripts if s.strip()]
        if not scripts:
            continue
        node = os.getenv("DOCMIND_NODE_BIN") or shutil.which("node")
        if not node:
            # 与 frontend scope 对 npm 的处理一致：工具缺失是 skip，不是失败
            result["ran"].append(
                "web:skip（未找到 node，跳过 %d 段内联脚本语法校验）" % len(scripts))
            continue
        for idx, (line, src) in enumerate(scripts, 1):
            # 含 import/export 的内联脚本按 ESM 解析（.mjs），否则 node --check
            # 会把合法的模块语法当成语法错误
            suffix = ".mjs" if re.search(r"^\s*(import|export)\s", src, re.M) else ".js"
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="docmind_sv_")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(src)
                r = _self_verify_command([node, "--check", tmp],
                                         project_root=root or os.getcwd(), timeout=60)
                if r.returncode != 0:
                    tail = ((r.stdout or "") + (r.stderr or "")).strip()[-600:]
                    result["failures"].append({
                        "scope": "web", "file": f,
                        "error": "第 %d 行起的内联脚本 #%d 语法错误：\n%s"
                                 % (line, idx, tail)})
                else:
                    result["ran"].append("node_check:%s#%d" % (rel, idx))
            except subprocess.TimeoutExpired:
                result["failures"].append({
                    "scope": "web", "file": f,
                    "error": "内联脚本 #%d 语法校验超时（>60s）。" % idx})
            except Exception as e:  # noqa: BLE001
                result["failures"].append({
                    "scope": "web", "file": f,
                    "error": "内联脚本 #%d 语法校验无法运行：%s" % (idx, str(e)[:200])})
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass


def _sv_verify_script(script_name, kind, root, result):
    """运行 verify_* 脚本（scene/engine，仅 scope 显式指定时调用，需 display）。"""
    if root is None:
        result["ran"].append(kind + ":skip（无 code_root）")
        return
    script = os.path.join(root, script_name)
    if not os.path.isfile(script):
        result["ran"].append(kind + ":skip（无 " + script_name + "）")
        return
    try:
        interpreter = (sys.executable if stage_execution_active() else "python") \
            if execution_mode() == "enterprise" else sys.executable
        r = _self_verify_command([interpreter, script_name], project_root=root, timeout=180)
        if r.returncode != 0:
            tail = (r.stdout or "")[-1000:] + (r.stderr or "")[-1000:]
            result["failures"].append({
                "scope": kind, "file": script,
                "error": "%s 未通过（exit=%d）：\n%s" % (script_name, r.returncode, tail),
            })
        else:
            result["ran"].append(kind + ":" + script_name)
    except subprocess.TimeoutExpired:
        result["failures"].append({"scope": kind, "file": script,
                                   "error": script_name + " 超时（>180s）。"})
    except Exception as e:  # noqa: BLE001
        result["ran"].append(kind + ":skip（%s）" % type(e).__name__)


_SV_VISUAL_MAX_PAGES = 3


def _sv_verify_visual(root, result, pages=None):
    """真实浏览器画面验收（web 门之上的第二级）。

    `pages` = 本次触发它的网页（auto 档传入）。传了就**逐个当入口截图** —— 否则
    visual 门只在项目里找 index.html，而 vibecoding 的产物叫 snake.html / game.html，
    找不到入口会直接 fail-closed 判未通过，把「文件已落盘却被判未验证」的收尾门
    bug 换个形式放回来。没传（显式 `scope: visual`）时沿用原入口发现逻辑。
    """
    try:
        from agent_runtime.visual_acceptance import capture_project_preview
    except Exception as exc:  # noqa: BLE001
        result["failures"].append({
            "scope": "visual", "file": root,
            "error": "真实浏览器画面验收不可用：%s：%s" % (type(exc).__name__, str(exc)[:200]),
        })
        return

    targets = []
    for p in (pages or []):
        rel = os.path.relpath(p, root) if root else os.path.basename(p)
        targets.append(rel.replace("\\", "/"))
    targets = targets[:_SV_VISUAL_MAX_PAGES]
    if not targets:
        targets = [""]

    for entry in targets:
        label = "visual:browser_preview" + (":" + entry if entry else "")
        try:
            report = capture_project_preview(root, entry=entry)
            result["ran"].append(label)
            result.setdefault("visual", {}).update({
                "checks": dict(report.get("checks") or {}),
                "screenshot": report.get("screenshot", ""),
            })
            if not report.get("passed"):
                result["failures"].append({
                    "scope": "visual", "file": entry or root,
                    "error": "真实浏览器画面验收未通过：" + json.dumps(
                        report.get("checks") or {}, ensure_ascii=False),
                })
        except Exception as exc:  # noqa: BLE001 —— 起不了浏览器/截图失败一律 fail-closed
            result["failures"].append({
                "scope": "visual", "file": entry or root,
                "error": "真实浏览器画面验收失败：%s：%s" % (type(exc).__name__, str(exc)[:240]),
            })


def self_verify(arg=""):
    """对代码库最近改动做轻量自验证，返回结构化 JSON 字符串（与文本协议 Action Input 同形）。

    入参（单行/多行 key: value）：
      scope: auto(默认, 按改动文件自动选) / backend / frontend / scene / engine / visual / all / skip
      files: 显式指定待校验文件（相对/绝对路径，可多行或逗号分隔）；缺省时用 git diff 自动发现
    返回 JSON：{"scope","ran":[...],"passed":bool,"failures":[...],"note":""}
      status 区分 passed/failed/skipped/partial；无检查或跳过不视为通过。
    """
    scope, files = _parse_self_verify_arg(arg)
    from agent_runtime.verification import finalize_verification
    root = _get_code_root()
    result = {"scope": scope, "ran": [], "passed": True, "failures": [], "note": ""}

    if scope == "skip":
        result["note"] = "已跳过自验证（scope=skip）。"
        return json.dumps(finalize_verification(result), ensure_ascii=False)

    # 1) 决定待校验文件集合
    if files:
        targets = []
        for f in files:
            f = str(f)
            # 相对路径按 code_root 解析（Agent 收尾门传的是 root 相对路径），
            # 避免按 cwd 解析导致文件"看起来不存在"被误判为跳过。
            if root and not os.path.isabs(f):
                f = os.path.join(root, f)
            targets.append(os.path.abspath(f))
    else:
        targets = _sv_changed_via_git(root) if root else []
    if not targets and scope != "visual":
        result["note"] = "未发现可校验的改动（无 git 改动或显式文件）。"
        return json.dumps(finalize_verification(result), ensure_ascii=False)

    # Resolve symlinks before checks: verification must not read/compile outside the project.
    safe_targets = []
    for target in targets:
        try:
            real_root = os.path.realpath(root) if root else None
            real_target = os.path.realpath(target)
            inside = bool(real_root and os.path.commonpath([real_root, real_target]) == real_root)
        except (ValueError, OSError):
            inside = False
        if inside:
            safe_targets.append(real_target)
        else:
            result["failures"].append({"scope": "permission", "file": target,
                                       "error": "校验目标超出当前项目范围"})
    targets = safe_targets

    py_files, fe_files, scene_files, engine_files, web_files = _sv_classify(targets)
    web_pages = _sv_web_pages(targets)

    # 2) 选 scope：auto 按改动文件类型自动选策略。
    #    backend(.py) / frontend(.ts/.vue) / scene(场景子系统，无 GUI 可自动) 始终纳入；
    #    web(.html/.htm/.css) 静态产物**两级门**，auto 档一次挂两个 scope：
    #      - `web`：离线、零依赖，HTML 标签闭合 + 内联 <script> node --check + CSS
    #        花括号平衡。永远跑（无浏览器/无 node 的机器也有结论）。
    #      - `visual`：只有真网页(.html/.htm)才挂，真起浏览器截图 + console 门，
    #        验的是 `web` 门看不出的黑屏与运行期异常。起不了浏览器一律 fail-closed；
    #        显式 DOCMIND_SELF_VERIFY_VISUAL=0 则 skip 并在 note 里写明「未截图
    #        不等于画面已验证」（此时结论只来自 web 门，不再退回「无任何校验器」
    #        的未验证态 —— 那正是 vibecoding 收尾门被整段替换的根因）。
    #    engine(引擎嵌入) 需真 Godot GUI 且会拉起引擎进程，默认不自动跑（避免每次写都启
    #    Godot 打断并发写入者）——仅当用户显式开 DOCMIND_SELF_VERIFY_ENGINE=1 才纳入自动，
    #    或经 scope:engine / scope:all 显式触发。其余情况优雅降级为 skip（见 _sv_verify_script）。
    if scope == "auto":
        scopes = []
        if py_files:
            scopes.append("backend")
        if fe_files:
            scopes.append("frontend")
        if scene_files:
            scopes.append("scene")
        if web_files:
            scopes.append("web")
        if web_pages and os.getenv("DOCMIND_SELF_VERIFY_VISUAL") != "0":
            scopes.append("visual")
        elif web_pages:
            result.setdefault("skipped", []).append("visual:skip（自动浏览器自审已禁用）")
            result["note"] = ((result.get("note") or "") +
                              "（检测到 .html/.htm 网页改动，浏览器画面自审默认会跑，但已被 "
                              "DOCMIND_SELF_VERIFY_VISUAL=0 关闭；此时 web 门只验了结构与脚本"
                              "语法，未截图不等于画面已验证）")
        if engine_files and os.getenv("DOCMIND_SELF_VERIFY_ENGINE") == "1":
            scopes.append("engine")
        elif engine_files:
            result.setdefault("skipped", []).append("engine:skip（自动引擎检查未启用）")
            result["note"] = ((result.get("note") or "") +
                              "（engine 域自检需真 Godot 且默认关闭，已跳过；"
                              "显式 scope:engine 或开 DOCMIND_SELF_VERIFY_ENGINE=1 可启用）")
    elif scope == "all":
        scopes = ["backend", "frontend", "scene", "web", "engine", "visual"]
    else:
        scopes = [scope]

    for sc in scopes:
        try:
            if sc == "backend":
                _sv_verify_backend(py_files, root, result)
            elif sc == "frontend":
                _sv_verify_frontend(fe_files, root, result)
            elif sc == "scene":
                _sv_verify_script("verify_scene_canvas.py", "scene", root, result)
            elif sc == "engine":
                _sv_verify_script("verify_engine_embed.py", "engine", root, result)
            elif sc == "web":
                _sv_verify_web(web_files, root, result)
            elif sc == "visual":
                # auto 档把本次网页传进去当截图入口（产物不叫 index.html 也要验得到画面）；
                # 显式 scope:visual 不传，沿用项目入口发现。
                _sv_verify_visual(root, result, pages=web_pages if scope == "auto" else None)
            else:
                result["ran"].append("skip:未知 scope=" + str(sc))
        except Exception as e:  # noqa: BLE001 —— 单个 scope 校验器故障绝不阻断整体
            result["ran"].append("%s:skip（校验器异常 %s）" % (sc, type(e).__name__))

    if not result["ran"]:
        result["note"] = result["note"] or "无可执行的校验（改动文件类型无对应校验器）。"
    return json.dumps(finalize_verification(result), ensure_ascii=False)


def memory_save(arg):
    """保存当前项目的有用事实、明确用户偏好或可复用流程。"""
    import agent_memory as memory
    try:
        data = json.loads(str(arg)) if str(arg).lstrip().startswith("{") else _parse_keyed(str(arg), ["kind", "title", "content", "evidence"])
        scope, user_text = memory.current_scope()
        kind = str(data.get("kind") or "fact")
        evidence = str(data.get("evidence") or "")
        if kind == "preference" and (not evidence or evidence not in user_text):
            return json.dumps({"ok": False, "error": "用户偏好必须引用当前或最近会话中用户明确说过的原文，不允许推测画像。"}, ensure_ascii=False)
        mid = memory.remember(scope, kind, data.get("title", ""), data.get("content", ""), evidence,
            source="user_explicit" if kind == "preference" else "agent")
        return json.dumps({"ok": True, "id": mid, "kind": kind}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": memory.scrub(str(exc), 500)}, ensure_ascii=False)


def memory_search(arg=""):
    import agent_memory as memory
    try:
        rows = memory.recall(memory.current_scope()[0], str(arg or ""), limit=20)
        return json.dumps({"ok": True, "items": rows, "advisory": True}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": memory.scrub(str(exc), 500)}, ensure_ascii=False)


def memory_forget(arg):
    import agent_memory as memory
    try:
        mid = _parse_keyed(str(arg), ["id"]).get("id") or str(arg).strip()
        return json.dumps({"ok": memory.forget(memory.current_scope()[0], mid)}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"ok": False, "error": memory.scrub(str(exc), 500)}, ensure_ascii=False)


def recall_experience(arg=""):
    """召回与当前任务相似的历史经验（跨会话经验记忆，Phase 3 建议性上下文）。

    入参（纯文本）：自然语言问题描述，如"改 Vue 组件后 typecheck 报错"；留空则退化为通用召回。
    返回结构化 JSON：{query, count, items:[{outcome, action_summary, decision, lesson, confidence, stale}]}。

    经验永远是「建议」，不强制覆盖当前真实证据（检索到的代码 / 校验结果优先级更高）；
    陈旧经验置信度已被打折，低置信不参与决策。任何故障静默降级为空结果。
    """
    try:
        from experience import recall_similar
        try:
            from projects import current_project_id
            project_id = current_project_id()
        except Exception:  # noqa: BLE001
            project_id = "default"
        q = (arg or "").strip() or "通用改动经验"
        hits = recall_similar(project_id, q, k=5)
        items = [{
            "outcome": h["metadata"].get("outcome"),
            "action_summary": h["metadata"].get("action_summary"),
            "decision": h["metadata"].get("decision"),
            "lesson": h["metadata"].get("lesson"),
            "confidence": h["confidence"],
            "stale": h["stale"],
        } for h in hits]
        return json.dumps({"query": q, "count": len(items), "items": items},
                          ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"query": arg, "count": 0, "items": [],
                           "note": "经验召回不可用（%s）" % type(e).__name__},
                          ensure_ascii=False)


TOOLS = {
    "web_weather": {"description": "查询今日结构化天气。先确认用户城市，禁止用搜索结果或 IP 猜位置。输入城市名或 JSON {\"city\":\"城市名\",\"country_code\":\"CN\"}；同名地点会要求澄清，返回有效日期、地点、天气字段、单位及来源 URL。失败可改用带城市和当前日期的权威来源，不得编造实时数据。", "func": web_weather},
    "web_transport": {"description": "查询指定日期的高铁/飞机交通候选。输入 JSON 或多行 origin/destination/departure_date/mode；缺出发地、目的地或日期会返回 needs 并要求补充。必须使用未来的具体日期，返回 verified_fare=false；搜索摘要不是实时余票或支付价，禁止据此估算或编造价格。", "func": web_transport},
    "web_research": {"description": "联网研究：先搜索，再读取多个公开网页正文（默认最多 5 个，可配置），返回来源和证据。适合教程、GitHub、引擎文档和需要最新资料的问题。推荐采用多轮策略：第一轮发现候选；看到候选后用 web_search_batch 并行查评价、口碑、教程或做法；若评价一般或证据不足，排除已见 URL/标题后提交下一批候选，最后再对关键来源调用 web_fetch。输入研究主题。", "func": web_research},
    "web_fetch": {"description": "读取公开网页正文并返回来源、标题和清理后的文本。输入完整 http/https URL。联网研究时先 web_search，再对关键来源调用。", "func": web_fetch},
    "eda_report_gate": {
        "description": "抓取在线 EDA 报告/文档页 → M3 结构化解析（Vivado 时序/面积、KLayout DRC、Verdi 仿真）→（可选 M4/M5 视觉回归）→ M6 双信号合并门，输出三态结论(met/unmet/uncertain)与验收建议(accept/rollback/escalate)。输入 JSON：{tool:vivado|klayout|verdi, kind:timing|utilization|drc|simulation, url?, content?, fetch?:auto|web_fetch|inline, visual?:{verdict}, golden_key?, view?, bbox_norm?, capture?:auto|gdi|x11|vnc, vlm_verified?:bool, critical?}。取页源可切换：给 content 或 fetch=inline 用调用方（如通用浏览器）已抓回的正文；否则用 web_fetch 抓 url。视觉通道双路径：直接透传已算好的 visual verdict；或给 golden_key+ROI(view/bbox_norm)+取像后端，内部复用 M4 取像 + M5 ROI golden diff 现算 verdict 喂进同一个门。全程 fail-closed：取不到正文、非本类报告、取不到帧或没 golden 一律判 uncertain，绝不谎报通过；只有 met 才 accept；unmet 的 rollback 只是建议、本工具从不执行回滚、也不存 golden。视觉/在线正文/未核实结论一律 verified:false，不得当作通过。",
        "func": eda_report_gate,
        # ② 证据豁免标记：本工具返回的 verdict(met/unmet/rollback) 就是终答的权威证据，
        # 由真实取像/报告解析算出，不需要 repo 里的「文件:行号」引用。output_audit 据此在
        # 「正文无引用 + 本轮有成功的本类工具观察」时不再按定位门判 FAIL（见 audit_evidence）。
        "verdict_authoritative": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "vivado | klayout | verdi"},
                "kind": {"type": "string", "description": "timing | utilization | drc | simulation"},
                "url": {"type": "string", "description": "在线报告/文档页地址（走抓取时用）"},
                "content": {"type": "string", "description": "已抓回的正文（浏览器抓取走这条，优先于 url）"},
                "fetch": {"type": "string", "description": "auto | web_fetch | inline"},
                "visual": {"type": "object", "description": "可选视觉信号 verdict dict {verdict: pass|identical|regression|localized|...}；透传优先于现算"},
                "golden_key": {"type": "string", "description": "给了就内部现算 M5 ROI golden diff（与 /api/eda/visual-regression 同源）；需配合 view/bbox_norm"},
                "view": {"type": "string", "description": "layout | waveform | schematic | heatmap（ROI 预设）"},
                "bbox_norm": {"type": "array", "items": {"type": "number"}, "description": "显式 [x1,y1,x2,y2] 归一框，覆盖 view"},
                "capture": {"type": "string", "description": "auto | gdi | x11 | vnc：平台可切换取像后端（现算视觉时用）"},
                "vlm_verified": {"type": "boolean", "description": "仅上层明确 VLM 已核实通过才置 true"},
                "critical": {"type": "array", "items": {"type": "string"}, "description": "纳入门禁的判定量键（默认 timing_met/drc_clean/lvs_clean）"},
            },
            "required": ["tool", "kind"],
        },
    },
    "web_subtitles": {"description": "读取公开 B 站视频字幕。输入包含 BV 号或 av 号的完整视频 URL；没有公开字幕、需要登录或被风控时返回明确原因。", "func": web_subtitles},
    "dev_mcp_call": {"description": "调用已启用的 MCP 游戏引擎连接器。输入 key: 服务器key、name: 工具名、arguments: JSON；可选 fallback_keys 或 hint 触发有界故障转移。先用 dev_list_connectors 看清可用连接器、用 dev_route_connector 按任务语义挑 top 作为 key、用 dev_list_connector_tools 确认 name 与参数；外部连接器需已启用并遵守审批。涉及写入时传 side_effect: true，默认不会跨连接器重试；只有明确传 allow_side_effect_fallback: true 才允许。", "func": dev_mcp_call},
    "dev_list_connectors": {"description": "列出已配置 MCP 连接器（key/label/engine/transport/启用状态/能力标签/适用说明），供 Agent 自主挑选最合适的引擎连接器。输入留空。", "func": dev_list_connectors},
    "dev_route_connector": {"description": "按任务语义挑选最合适的【已启用】连接器：输入 hint（任务描述，如 'Godot 里打开 Main 场景并运行'），返回排序候选与匹配理由（top.key 即 dev_mcp_call 的 key）。某连接器不可用或调用失败时，用它重新挑选其它已启用连接器。", "func": dev_route_connector},
    "dev_list_connector_tools": {"description": "列出某连接器暴露的工具（name/description/input_schema），确定 dev_mcp_call 的 name 与参数。输入 key: <连接器key>；仅对打算调用的连接器使用（godot 等 stdio 需先建立会话）。", "func": dev_list_connector_tools},
    "dev_mcp_search": {"description": "自助装配 MCP 第 1 步：按领域或能力词搜索连接方式。会话允许联网时查询官方 MCP Registry，返回经安全校验的真实候选配置；关闭联网时只返回离线指引。模型须展示候选，装配仍需审批，不能把搜索摘要当作安装命令。输入能力关键词。", "func": dev_mcp_search},
    "dev_mcp_add": {"description": "自助装配 MCP 第 2 步（用户确认门）：新增/更新连接器配置。多行输入 key/label/transport(stdio|http)，stdio 给 command+args（或 args_json），http 给 url。未确认时返回含 action/target 的 blocked；向用户展示完整参数，用户在 MCP 设置中确认后用相同参数重试。成功后调用 dev_mcp_probe。", "func": dev_mcp_add},
    "dev_mcp_probe": {"description": "自助装配 MCP 第 3 步：探活已装配连接器（MCP initialize + tools/list），返回工具数量与名称。输入连接器 key；stdio 首次冷启动可能较慢。失败时按 error 修正参数后重新 dev_mcp_add。", "func": dev_mcp_probe},
    "dev_mcp_discover": {"description": "自助装配 MCP 第 4 步：读取连接器工具并生成【待用户确认】能力候选（不会自动启用路由）。输入连接器 key。向用户说明候选能力后，等待用户在 MCP 设置中确认，再调用 dev_mcp_decide(decision: approve)。", "func": dev_mcp_discover},
    "dev_mcp_decide": {"description": "自助装配 MCP 第 5 步：批准/拒绝能力候选。多行输入 decision: approve|reject 与 key: <连接器key>；approve 只有用户在 MCP 设置中确认后才会放行。批准后 Agent 才能经 dev_route_connector/dev_mcp_call 自动调用该连接器。", "func": dev_mcp_decide},
    "dev_mcp_remove": {"description": "移除自定义 MCP 连接器（内置预设则禁用），用户确认后才会放行。输入连接器 key；会先关闭活动会话。", "func": dev_mcp_remove},
    "dev_mcp_discover_from_need": {"description": "从自然语言需求发现可装配的 MCP 连接器候选（不写盘、不自动启用）。输入需求描述（如『我需要查高铁票的 MCP』），可选多行 web_enabled: true 开启联网。流程：离线精选索引 →（联网时）GitHub 域限定搜索取仓库 README 解析官方命令，候选过 R1-R9 信任闸门。返回后须向用户展示候选，逐条 dev_mcp_add（审批）落盘，再 dev_mcp_probe 探活、dev_mcp_discover 生成能力候选。", "func": dev_mcp_discover_from_need},
    "dev_skill_create": {"description": "起草用户技能（待审批，不会自动启用）。skill 是*可执行行为*，必须经用户确认才激活。多行输入 name/description/body；写入 SKILLS_DIR/.pending/<name>/SKILL.md（草稿不生效）。返回完整正文，代理须向用户完整展示并取得明确同意后，再 dev_skill_approve 激活。", "func": dev_skill_create, "capability": "write_local", "side_effect": "mutating", "parallel_safe": False},
    "dev_preview_adapter_create": {"description": "让 Agent 为当前项目生成领域预览适配器草稿（不会自动启用）。多行输入 id/label/domain/artifact_kinds/capture_adapter/refresh_tool/connector_hint/validation/evidence；若需要真正代码，再提供 runtime: python|node、entrypoint: adapt、source: 完整模块源码。系统只允许 adapt(payload) 把已批准工具结果转换为 JSON，静态检查通过后仍需用户确认；代码写入 .docmind/preview-adapters/.pending。", "func": dev_preview_adapter_create},
    "dev_preview_adapter_approve": {"description": "激活或拒绝 Agent 生成的项目预览适配器。只有用户明确确认适配器方案、刷新工具和验证条件后才传 decision: approve；否则只用 reject。输入 id 与 decision。", "func": dev_preview_adapter_approve},
    "dev_preview_adapter_refresh": {"description": "执行已激活项目预览适配器的受控刷新。适配器可使用已批准的 builtin/MCP 获取原始结果，再交给隔离 Python/Node 的 adapt(payload) 转换为预览产物；禁止适配器自行联网、启动命令或读写项目。", "func": dev_preview_adapter_refresh},
    "dev_preview_adapter_rollback": {"description": "回滚当前项目生成的 Python/Node 领域适配器到上一个已激活版本。首次调用需要用户批准 preview_adapter_rollback，批准后使用相同 id 重试。", "func": dev_preview_adapter_rollback},
    "dev_preview_adapter_test": {"description": "用固定 JSON fixture 测试已激活的 Python/Node 领域适配器，不调用真实软件、MCP 或网络。输入 id、payload(JSON)、可选 timeout；适合先验证 EasyEDA/Godot/CAD 返回数据的转换逻辑。", "func": dev_preview_adapter_test},
    "dev_skill_approve": {"description": "激活待审批技能：把 .pending/<name>/SKILL.md 移到 SKILLS_DIR 并 reload 生效。仅当用户已明确确认该技能正文安全时调用。输入 name: <技能名>。", "func": dev_skill_approve},
    "dev_skill_reject": {"description": "丢弃待审批技能草稿（不激活、不保留）。输入 name: <技能名>。", "func": dev_skill_reject},
    "search_knowledge": {
        "description": "在已上传的知识库中检索相关文档片段。输入应为检索关键词或问题。",
        "func": search_knowledge,
    },
    "search_assets": {
        "description": "在精选游戏素材目录（Kenney CC0 等）中按关键词/类型/许可筛选素材，适合回答'找素材/美术资源/角色精灵/tileset/UI/音效'类问题。",
        "func": search_assets,
    },
    "calculate": {
        "description": "对数学表达式求值，例如 '23*45+12'；也支持比较运算，例如 '9.9 > 9.11'（结果为「成立/不成立」）。仅支持数字、+ - * / % ** //、括号与 > < >= <= == !=。",
        "func": calculate,
    },
    "web_search": {
        "description": "当知识库不足或需要时效性/外部信息时，联网搜索。自动模式会并行查询 DuckDuckGo/百度/Bing，并按问题语义补充 GitHub、B 站、知乎、贴吧、小红书、CSDN 等相关来源；结果附可解释的可信度排序参考，不代表事实已证实。看到候选后可用 web_search_batch 并行查评价、做法或最新状态。",
        "func": web_search,
    },
    "web_search_batch": {
        "description": "并行执行多个联网查询并跨查询去重。输入 JSON：{\"queries\":[\"候选1 评价\",\"候选1 做法\"],\"exclude\":[\"已看过的 URL 或标题片段\"]}，也支持每行一个查询。适合先发现候选，再并行查评价/教程/做法；结果不足时排除旧结果后继续提交下一批。",
        "func": web_search_batch,
    },
    "dev_http_request": {
        "description": "调用你自己的外部业务 API（REST/JSON）。受 EXTERNAL_API_ALLOWLIST 域名白名单约束（防止 SSRF），未配置白名单则拒绝。输入（多行 key: value）：url: <完整URL> 换行 method: <GET/POST/...默认GET> 换行 可选 timeout: <秒> 换行 可选 headers: <单行JSON对象> 换行 可选 body: <请求体，可多行>。返回 HTTP 状态码 + 响应头 + 截断响应体。",
        "func": dev_http_request,
    },
    "python_exec": {
        "description": "在受限子进程中执行 Python 代码并返回输出（超时 60s）。适合数值计算、数据处理、文本变换、小规模绘图数据生成等'让 agent 真正动手'的任务。输入为完整 Python 代码（不要加代码围栏）。输出保留开头与结尾，异常堆栈不会被截断；需要跑更久的脚本请改用 run_command 并加 background: true。",
        "func": python_exec,
    },
    "create_artifact": {
        "description": "创建并校验 Word、PDF、PowerPoint 或 Excel 文件。输入 JSON：format 为 docx/pdf/pptx/xlsx，filename、title/subtitle；docx/pdf 使用 sections（heading/level/paragraphs/bullets/table）；pptx 使用 slides；xlsx 使用 sheets。介绍当前项目时必须设置 purpose=project_intro，提供至少两个已读取的当前项目相对路径 source_files，正文至少四章、700 字；工具核验项目文件并自动附来源，缺失或跨项目会拒绝。文件写入当前项目 artifacts 目录，返回路径和校验结果。调用前先用 dev_use_skill 读取对应技能。",
        "func": create_artifact,
    },
    "inspect_data_file": {
        "description": "识别并解析当前项目内的数据文件。输入路径或 JSON {\"path\":\"data/items.csv\"}。支持文本、无扩展名配置、脏 CSV/JSON、Office/PDF；.bin/.scn 等二进制返回格式、大小、SHA-256、文件头、十六进制和可见字符串预览。无法安全解析时明确报告原因，不把乱码当正常文本。",
        "func": inspect_data_file,
        "capability": "read_local",
        "group": "code",
    },
    "generate_data_file": {
        "description": "在当前项目内生成并回读校验结构化文件。输入 JSON，含 path/format/data 等字段；支持 csv/tsv/json/jsonl/yaml/toml/xml/txt/md，.gitignore 用 format=txt，.bin 用 format=binary 与唯一的 base64 或 hex 字节内容。仅当提供真实 Godot 资源字节且文件头有效时允许 .scn；场景开发优先生成 .tscn 源文件并交给引擎。默认不覆盖，原子写入并回读校验。",
        "func": generate_data_file,
        "capability": "write_local",
        "side_effect": "mutating",
        "parallel_safe": False,
        "group": "developer",
    },
    "gen_video_prompt": {
        "description": "按 MiniMax H3 的三段结构（integrated_multimodal_description / overall_soundscape / non_diegetic_music）把一段创意描述生成为结构化视频提示词，可直接粘贴进 ComfyUI 的 MiniMaxH3ImageToVideo 节点。输入为自然语言创意（主体/场景/动作/氛围）。",
        "func": gen_video_prompt,
    },
    "search_code": {
        "description": "在已索引的源代码/配置中检索相关函数、类、配置片段。回答'某功能在哪实现/某函数做什么/某配置怎么写'等关于代码库的问题。输入为搜索关键词。",
        "func": search_code,
    },
    "read_file": {
        # 措辞借鉴 Claude Code 的 OFFSET_INSTRUCTION_DEFAULT：默认**劝整文件读**，
        # 区间只作为「已经知道要看哪一段」时的例外。理由是区间会同时放大两件事——
        # 参数面变大（path/start/end 三个字段任一写错都失败）与重复读取变多
        #（整文件读过之后再按区间读，内容其实已在上下文里）。
        # 另：同一文件未被改动时重复读取只会返回一段短存根，不会重复给内容。
        "description": (
            "读取代码库中的文件（path 为相对代码根目录的路径）。"
            "**默认直接读整个文件**——这是最省事也最不容易出错的用法。"
            "仅当文件很大、且你已经确切知道要看哪一段时，才用 start/end 指定行号"
            "（start 为 1 基起始行，end 可选）。"
            "同一文件若自上次读取以来未变化，重复读取只返回一段短提示，不会重复给内容。"
        ),
        "func": read_file,
    },
    "read_external_file": {
        "description": "读取【项目之外】白名单目录内的文件（受控越界读）。仅允许读取 DOCMIND_EXTERNAL_DIRS（分号分隔的绝对目录）之内的文件，越界拒绝；用于用户授权 AI 访问仓库外的资料（如其它项目、下载目录文档）。输入为文件绝对路径。未配置白名单时提示如何开启。注意：只有在「高权限模式」下越界读才被放行；安全模式下本工具会拒绝。",
        "func": read_external_file,
        # 越界读必须与「读项目内」分开记账，否则只租用了项目内读的工作流
        # 也能把文件读出去：实测租约只含 read_local 时它照常通过
        # （`Agent._lease_blocked` 是按 capability 字面值比对的）。
        # Capability.READ_EXTERNAL 早就定义了、前端也备好了标签
        # （AutonomousCockpit.vue 的「读取外部资料」），只是没有任何工具落进去。
        # 默认租约授全部 Capability（game_workflow 两处建租约都不传能力），
        # 所以正常路径不受影响；只有显式窄租约会开始拦它 —— 那正是本意。
        "capability": "read_external",
    },
    "create_external_file": {
        "description": "在白名单目录内【新建】文件（受控越界写，不覆盖已有文件）。仅在「高权限模式」+ 已配置 DOCMIND_EXTERNAL_DIRS 白名单时生效；安全模式或被列出白名单外一律拒绝。用于给其他项目新增文件/模块。输入同 create_file：第一行 path: <绝对路径>，最后 new_text: <文件内容（可多行）>。受单文件 200KB 上限与 .py 语法校验约束；「人工确认」开启时只暂存不落盘。",
        "func": create_external_file,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_external",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "edit_external_file": {
        "description": "在白名单目录内修改【已存在】文件（受控越界写）。仅在「高权限模式」+ 白名单内生效。两种用法：① 局部替换——path + old_text（精确旧片段）+ new_text；② 整体重写——path + new_text（须先用 read_external_file 读过该文件）。受 200KB 上限与 .py 语法校验约束；「人工确认」开启时暂存。",
        "func": edit_external_file,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_external",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "delete_external_file": {
        "description": "删除白名单目录内的【单个文件】（危险操作，受控越界删）。仅在「高权限模式」+ 白名单内生效。输入需含 path: <绝对路径> 与 confirm: yes 明确确认；只删文件不删目录；「人工确认」开启时改为暂存待批准。用于清理其它项目的无用文件。",
        "func": delete_external_file,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_external",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "grep": {
        "description": "在代码库中按正则表达式搜索文本/符号，返回匹配的文件路径与行号。定位某段代码、某变量、某错误出现位置时用。输入为正则表达式。",
        "func": grep,
    },
    "apply_edit": {
        "description": "受控修改代码库中【已存在】的文件（不能新建、不能越界写）。两种用法：① 局部安全替换——提供 path、old_text（要被替换的【精确】旧片段）、new_text（替换后内容），工具在文件中唯一匹配处替换；② 整体重写——只提供 path 与 new_text（省略 old_text），但前提是你已用 read_file 读取过该文件。修改前请先 read_file 确认当前内容；.py 写入后会做语法校验，不通过自动回滚。Action Input 按多行格式写：第一行 path: <路径>，可选 old_text: <精确旧片段>，最后 new_text: <新内容（可多行）>。",
        "func": apply_edit,
    },
    "dev_apply_edits": {
        "description": "一次提交【多个文件】的局部替换（原子：要么全改，要么一个都不改）。改一个 API 要动多处调用点时用它，避免逐个 apply_edit 改到一半失败留下半成品。Action Input：多个编辑块，块之间用单独一行 `---` 分隔；每块格式与 apply_edit 相同（path: <文件> / old_text: <精确旧片段> / new_text: <新片段>），批量编辑强制要求 old_text。任一文件的 old_text 匹配不到/不唯一/越界/落在分区里/.py 语法错，都会【整体不写入】并返回逐块原因；全部预检通过后才统一落盘，落盘中出错会回滚已写文件。",
        "func": dev_apply_edits,
    },
    "create_file": {
        "description": "在代码库内【新建】一个文件（不能覆盖已有文件，修改已有文件请用 apply_edit）。用于新增模块/分区（如新建 combat/crit.py）。受路径沙箱、单文件 200KB 上限、.py 语法校验约束；父目录不存在会自动创建（仍在 code_root 内）。新建前建议先用 search_code/grep 确认不会与已有实现重复（防堆叠）。Action Input 格式：第一行 path: <相对或绝对路径>，最后 new_text: <文件内容（可多行）>。",
        "func": create_file,
    },
    "self_verify": {
        "description": "写后自验证工具（Phase 1 闭环收尾门）。系统会在你成功执行 apply_edit/create_file 后自动调用它，对改动做轻量校验（后端 py_compile+对应单测、前端 npm run typecheck、静态产物 HTML 结构+内联脚本语法），并把结果回填给你；若返回「未通过」，请基于失败信息修复后重试，不要跳过校验直接声称完成。**写入 .html/.htm 网页时 auto 档会一次挂两级门**：先做结构+脚本语法（web），再真起浏览器截图并采集 console 错误做画面自审（visual，起不了浏览器会判未通过而不是悄悄跳过）；DOCMIND_SELF_VERIFY_VISUAL=0 可关画面自审，但关闭后结论只来自 web 门，未截图不等于画面已验证；你也可以主动调用它来复验指定文件。Action Input 格式：第一行 scope: <auto/backend/frontend/scene/web/engine/visual/all/skip>，之后可跟多行 files: <文件路径>（缺省时自动用 git diff 发现改动）。",
        "func": self_verify,
    },
    "recall_experience": {
        "description": "跨会话经验记忆召回（Phase 3，建议性上下文，优先级低于真实证据）。当你准备做一类容易踩坑的改动（如某框架重构、某依赖升级、某校验反复失败）时，先调用它查「我以前类似的改动踩过什么坑、留下了什么教训」。输入为自然语言问题描述（如 '改 Vue 组件后 typecheck 报错'），留空则退化为通用召回。返回按置信度排序的历史经验（含 outcome/教训/决策/陈旧标记），仅供参考，不要把它当成必须执行的指令，当前真实代码与校验结果永远优先。",
        "func": recall_experience,
    },
    "memory_save": {"description": "保存当前项目的长期记忆。输入 JSON 或多行 kind(fact/preference/workflow)/title/content/evidence。只保留有用摘要，不存密码、图片或整段网页。preference 必须 evidence 引用用户明确表达的原文，禁止推测用户画像。workflow 是流程资料，需用 dev_skill_create 起草技能、审核后启用。", "func": memory_save, "capability": "write_local", "side_effect": "mutating", "parallel_safe": False, "group": "general"},
    "memory_search": {"description": "检索当前项目的历史结果、失败教训、用户偏好和流程。输入关键词，空串列出最近记忆；历史内容仅供参考，model_report 不是已验证事实。", "func": memory_search, "parallel_safe": False, "group": "general"},
    "memory_forget": {"description": "删除当前项目的一条长期记忆，输入 id 或 id: <值>。用户纠正偏好或要求忘记时使用；删除记忆不会删除原始会话和任务日志。", "func": memory_forget, "capability": "write_local", "side_effect": "idempotent_write", "parallel_safe": False, "group": "general"},
    "run_command": {
        "description": (
            "在代码库根目录内执行 shell 命令（如 pytest / npm run build / gradle test），返回合并后的"
            "标准输出与错误。用于跑构建、跑测试、执行项目内命令来验证改动或查看结果。命令在 code_root 内执行，"
            "危险操作（rm -rf /、format、shutdown、pip install 等）会被拦截。\n"
            "输入：默认整条命令（如 `pytest -q`）。需要更长预算时换行追加 `timeout: <秒>`（上限 300 秒）；"
            "预计超过 300 秒或需要持续观察（本地服务、长构建）时追加 `background: true`，任务转后台后"
            "用 dev_job_logs 取增量日志、dev_job_cancel 终止。\n"
            "输出：只保留开头与结尾窗口，中间省略会明确标注行数，因此报错尾部不会被截掉；"
            "超时会明确报告并提示转后台，不要靠猜。"
        ),
        "func": run_command,
    },
    "dev_job_logs": {
        "description": (
            "读取后台命令任务的运行状态与输出。输入 `job_id: <id>` 查看该任务（可附加 "
            "`offset: <字符偏移>` 只取新增部分）；省略 job_id 则列出最近后台任务。任务结束或"
            "失败都会保留已采集的输出供排查。\n"
            "长日志加两样：`fold: true` 把重复行折成「重复 N 次」——比的是【签名】而不是整行，"
            "真 uvicorn 的 access 行每条都带临时客户端端口，逐行去重实测折不动任何一行；"
            "`level: warn|error` 只看警告以上，且 traceback 正文跟随它的标题走（逐行筛级别会"
            "把最该看的正文一起筛掉）。\n"
            "视图模式是整块重算、**不能续读**（带了 offset 会明说被忽略，`next_offset` 置空），"
            "并如实报 `lines_in/lines_out/folded_away/dropped_*`。上游缓冲本来就是「头 4000 + "
            "尾 4000，丢中段」，所以视图会报 `middle_omitted_chars`，被级别筛掉的行里若还有 "
            "4xx/5xx/失败字样也会报 `dropped_failure_marks`——这份视图永远不该被读成「日志里"
            "没有错误」。原始读法（不给 fold/level）不改文本，只在确有重复或确有缺口时附一句 "
            "`view_hint`。"
        ),
        "func": dev_job_logs,
    },
    "dev_job_cancel": {
        "description": "终止一个后台命令任务（保留已采集输出）。输入 `job_id: <id>`；id 可用 dev_job_logs 不带参数列出。用于停掉跑飞的长构建、卡死的服务或启动参数写错的本地服务。",
        "func": dev_job_cancel,
    },
    "dev_asset_get": {
        "description": "通过素材区接口取得素材引用。输入 asset_id 或 path，可选 consumer_region；只允许读取 assets 分区内的文件，不复制或内联素材。",
        "func": dev_asset_get,
    },
    "dev_asset_register": {
        "description": "将 assets 分区内已存在的文件注册到 manifest.json，输入 asset_id/path/type/license/tags。",
        "func": dev_asset_register,
    },
    "dev_capture_bug": {
        "description": "把异常、堆栈、来源分区和复现步骤写入 bugs 分区，生成唯一 Bug ID。输入 error/traceback/source_region/reproduction/title/severity。",
        "func": dev_capture_bug,
    },
    "dev_list_bugs": {
        "description": "列出 bugs 分区中已经归档的历史异常记录，返回 Bug ID、严重等级、来源和时间；不能替代当前项目代码审查或运行验证。",
        "func": dev_list_bugs,
    },
    "dev_update_bug": {
        "description": "更新 Bug 状态，输入 bug_id 和 status(open/investigating/fixed/ignored)。",
        "func": dev_update_bug,
    },
    "game_validate_data": {"description": "校验项目 JSON/YAML/TOML 配置格式。", "func": game_validate_data},
    "game_release_check": {"description": "执行发布前检查：配置、翻译和敏感 .env 文件。", "func": game_release_check},
    "game_upsert_task": {"description": "创建或更新游戏开发任务，输入 title/region/priority/status 等字段。", "func": game_upsert_task},
    "game_simulate": {"description": "模拟数值成长曲线（等级经验/经济平衡推演），输入 levels/base/growth，可选 model=geometric|linear|logistic|diminishing、k=承载上限。", "func": game_simulate},
    "game_impact": {"description": "按符号或关键词分析代码影响文件，输入 query。", "func": game_impact},
    "game_playtest": {"description": "在项目根目录运行 Playtest 命令，输入 command/timeout。", "func": game_playtest},
    "game_screenshot": {"description": "截取当前引擎运行画面作为视觉观察：可选输入 target: embedded|foreground（默认 embedded，嵌入窗口不可用时自动改抓前台窗口）。优先使用已启用引擎连接器的截图能力，其次抓取工作台内嵌窗口；JPEG 保存到项目 .docmind/screenshots/ 并回传图片。截图只是某一瞬间的观察，不是代码事实；无窗口/无头环境会明确失败，那时改用运行日志或受控 playtest 证据，不要臆测画面。", "func": game_screenshot},
    "dev_desktop_capture": {"description": "桌面自动化视觉技能的安全观察入口：截取当前项目嵌入窗口或前台窗口，输入 target: embedded|foreground。只返回真实画面和 native 预览 artifact，不直接点击、输入或修改软件状态；状态修改必须调用已批准的应用 MCP、插件或其他明确工具。", "func": dev_desktop_capture},
    "dev_desktop_action": {"description": "执行一个经过项目审批的桌面动作：action=click|type|drag|key|save，target=embedded|foreground。click/drag 使用窗口客户区 x/y（drag 另给 to_x/to_y），type 使用 text，key 使用安全键名或 Control_L+s。首次调用会返回 desktop_action 审批请求；用户确认后必须用完全相同参数重试。禁止终端、Win 键、任意 HWND 和窗口枚举。", "func": dev_desktop_action},
    "dev_serve": {
        "description": (
            "启动/停止【本机开发服务器】并等它真的能应答：vite、next、`python -m http.server` 之类。"
            "harness 原本只会把项目目录当静态站临时服务一下，Vue/React 这类必须跑 dev server 的前端"
            "不 build 就预览不到——本工具补的就是这一段。\n"
            "输入 keyed 多行：`action: start|stop|status`（默认 start）、`cmd: npm run dev`、"
            "`cwd: frontend`（代码根目录内的子目录）、`port: 5173`（可省，不给就从服务自己的日志里认端口）、"
            "`wait: 20`（就绪等待秒数，上限 120）、`job_id: <id>`（stop/status 用）。\n"
            "命令与 run_command 共用同一道黑名单，`rm -rf`/`git` 写操作之类会被直接拦下；进程只在 code_root "
            "或其子目录内启动。就绪判定以【实际连通】为准：先探端口，再发一次 GET；4xx/5xx 也算已在响应，"
            "但只有进程起来却没探测到就绪时会分开如实报告，不会谎称就绪。\n"
            "拿到 URL 后立刻用 `preview_project` 的 `url:` 打它，就能拿到真实截图 + console 错误 + 失败请求。"
            "用完必须 `action: stop` 回收（后台任务额度有限，上限 8 个）。"
        ),
        "func": dev_serve,
    },
    "dev_page_action": {
        "description": (
            "在本机网页里【真的做动作】：点、填、按键、等条件、读状态。harness 原来只能看画面"
            "（preview_project），所以「点一下才复现」的 bug 只能靠猜——本工具补的就是这一半。\n"
            "输入 keyed 多行：`action: open|click|type|press|wait|read|locate|list|close`、"
            "`page: <会话 id>`（open 之外必填）、`url: http://127.0.0.1:<实际端口>/` 或 `entry: index.html`、"
            "`selector:`/`text:`/`nth:` 定位、`value:` 文本、`key: Enter`、`state: present|absent|visible|hidden|text`、"
            "`mode: text|value|attr|html|count|contains`、`attr: href`、`needle: 要找的词`"
            "（contains 用）、`settle: 600`、`screenshot: true|false`。\n"
            "`read` 的文本有 4000 字上限（回包会标「已截断」），所以判断「这句话在不在」时别读整段"
            "文本，改用 `mode: contains` + `selector: 容器` + `needle: 词`——只回命中数与周围小段，"
            "返回量与页面大小无关；容器没匹配到或没扫完时会明说「这次搜索不构成证据」；"
            "搜索按【可见文字】判定，`display:none` 里的命中另报 `hidden_hits`，不混进 `hits`。\n"
            "**每个动作都回报是否真的生效**：effect 取 `changed`/`navigated`/`reloaded`/`error_seen`/`no_change`，"
            "并附 DOM 变更、新 console 错误、新失败请求的计数。`no_change` 就是没观察到变化，"
            "不要当成成功；`error_seen` 要按报错修。隐藏、禁用、或命中多个却没给 `nth` 的元素一律拒绝执行，"
            "并把候选清单原样回给你——点错东西还报生效，是最贵的一类假干净。\n"
            "只允许本机回环地址；不接受任意 JS 执行；按键只认固定键表。会话持有真实浏览器，"
            "同时最多 3 个，用完必须 `action: close`（回收会回报作业对象与临时目录是否真的释放）。\n"
            "页面里的按钮可能真的会改你的项目或发请求——那是你的开发服务，不是沙箱里的玩具。"
        ),
        "func": dev_page_action,
    },
    "dev_vue": {
        "description": (
            "把【真实的 .vue 组件】挂进真浏览器验它的接线：编译 → 渲染 → 真点击 → 收 emit。\n"
            "输入 keyed 多行：`component: src/workbench/components/Xxx.vue`（相对前端目录）、"
            "`props:` 一个 JSON 对象、`clicks:` 顺序点击（`.wmd-item#1` 里的 `#1` 是第几个）、"
            "`expect_present:` / `expect_absent:` / `counts: .wmd-item=2` / `texts:`（按行分条）、"
            "`expect_emit: focus` + `expect_emit_args: [\"t2\"]`（实参原样比较）、`timeout: 60`。\n"
            "第二层是共享状态驱动：`composable: src/workbench/composables/workbench.ts` + "
            "`store_call: useWorkbench` + `setup: [{\"call\":\"openRootMenu\",\"args\":[...]}]`。"
            "本仓前端的组合式是【模块级单例】（实测全仓没有 provide/inject），所以入口与组件同源 "
            "import 同一份就是同一份状态，挂载前可以先把它开到某个状态。名字校验分两种："
            "`store_call` 与不带 store_call 的 setup 名按模块真实导出【静态】校验；给了 "
            "`store_call` 时方法名来自运行期才拿得到的对象，静态放行但运行期取不到函数就把 "
            "`setup_applied` 判红——静默少做一件事是最坏的一种假干净。\n"
            "这是循环里最后没被守住的一段：`dev_media action: voice` 里的胶水是照 `AutonomousCockpit.vue`"
            "**转写**的，`.vue` 本体被改坏（props 改名、emit 从 id 变成 label、`v-if` 条件写反）以前"
            "没有测试会发现。这里挂的是真组件——用仓库自带的 `@vue/compiler-sfc`（vue-tsc 带的，"
            "与 vue 同版本）编、前端自己的 esbuild 打包、`dev_page_action` 真点击，**零新依赖**"
            "（vitest 量化过不装：1 critical + 3 moderate + 1 high 公告，且 vitest3 要 vite6/7 而本树 vite5）。\n"
            "判定 fail-closed：不写任何断言＝未通过（挂上了不是证据）；`expect_emit` 写组件没声明过的"
            "名字＝未通过（否则永远等不到也看不出来）；读状态那次点击抛错单独红。"
            "边界：只证明这一个组件在这一组 props 下的行为，不证明整页集成，也不听真网关。\n"
        ),
        "func": dev_vue,
    },
    "dev_apptest": {
        "description": (
            "整页集成守卫：起【真后端】+ 真构建产物 + 真浏览器，断言页面真的从后端拿到了数据。\n"
            "输入 keyed 多行：`page: /workbench`（站内路径）、`text:`（页面上必须有的文字，"
            "按行分条，最多 8 条）、`tab: 素材`（要点开的工作区标签）、`stream: 1`"
            "（再跑一档真流式）、`timeout: 120`。\n"
            "流式那一档：浏览器带假麦克风开页 → 进开发舱开摄像头（这才建 ws）→ 开麦 →"
            " 要求页面字幕里出现**由收到的音频算出的**片数/字节数/峰值，且字节数 == 片数×3200、"
            "助手回包引用同一个字节数、至少两轮；适配器是 `scripted`，零网络零 key。\n"
            "这一条补的是 `dev_vue` 明确不证明的那一段：组件守卫喂的是我自己写的 props，"
            "整页要的是真 uvicorn + 真 `web/` 产物 + 真 `/api` 往返。临时端口起 `api:app`，"
            "`DOCMIND_STATE_ROOT` 指到临时目录（绝不往仓库真登记表上写），注册一个只含"
            "【这次夹具文件】的临时项目，然后要求文件树里出现那个随机命名的文件——"
            "这就是\"后端数据到了 DOM\"的凭据，缓存与旧页面冒充不了。\n"
            "判定 fail-closed：后端没起来 / 状态根没隔离 / console 有错 / 有失败请求 / "
            "点了导航但活动标签或正文没换 / 正文被截断 / 现场没收回，一律未通过。"
            "`bundle_built_after_latest_source` 也会红：`web/` 比 `frontend/src/**` 旧时"
            "整页跑的是【旧构建】，绿了也不代表当前代码——先 `cd frontend && npm run build`。\n"
            "边界：证明集成那一段，不点真模型、不听真网关，也不替组件级断言背书。\n"
        ),
        "func": dev_apptest,
    },
    "dev_testrun": {
        "description": (
            "跑 pytest 并回【结构化】判定，替代 `run_command(\"pytest -q\")` + 自己读一整屏文本。\n"
            "输入 keyed 多行：`paths: tests/test_a.py, tests/test_b.py`（逗号分隔，留空＝全量）、"
            "`keyword: voice`（走 `-k`，只当参数传）、`only_failed: true`（只重跑上次记账里的失败）、"
            "`related: agent_runtime/x.py,tools.py`（按改动文件选出真实存在的相干测试；"
            "同时也是 `coverage: true` 时的圈定范围）、"
            "`coverage: true`（附带覆盖率）、`timeout: 300`（5~1500 秒）。"
            "输出是 counts / 逐条 FAILED nodeid + 原因 / 范围回显。\n"
            "为什么不能只看退出码：pytest 在【一个用例都没收集到】时退出码是 5——那绝不是通过，"
            "本工具用 `tests_collected` 明确判未通过；摘要与退出码对不上时判 "
            "`counts_agree_with_exit`（输出被截断或被污染）；读不到摘要判 `summary_parsed`。\n"
            "`--lf` 在本仓不可用（`.pytest_cache` 是 Permission denied），所以失败清单由本工具"
            "记到项目状态目录，`only_failed` 用的是这份账；失败超过 200 条会拒绝逐条重跑"
            "（那是整片红，该跑全量）。`basetemp` 一律钉在项目内。\n"
            "覆盖率（`coverage: true`）：会真跑一遍并回【哪些文件哪些行没被执行过】，"
            "配合 `related:` 时按你改动的文件圈定范围——这比一个全仓百分比有用得多，"
            "因为一条 87% 的横幅完全可能没碰到你刚写的三行分支。"
            "报告读不出来、或你给的改动文件压根不在报告里，都明确说「覆盖率未知」而"
            "不是折算成数字（有语法坏掉的文件时 coverage 会跳过它，那些文件的覆盖率"
            "既不算已覆盖也不算未覆盖，会单独列出来）。"
            "**覆盖率低不等于测试失败**，两者分开判。没装 `coverage` 时如实说缺依赖，"
            "不把「没测」报成 0%，也不许当成通过。"
            "选不出相干测试时报错并列出没命中的文件名，不悄悄退化成跑全量或跑 0 个。\n"
        ),
        "func": dev_testrun,
    },
    "dev_headcheck": {
        "description": (
            "把【提交本身】挂成一份干净检出，在那棵树里真 import 入口模块——用来抓一种"
            "在工作树里永远看不见的错：提交引用了没入库的文件。共享工作树里多个 AI 同时改"
            "同一批文件，一次 `git commit -- <路径>`（它会绕过索引）或一次漏 `git add`，都会让"
            "引用新模块的 import 进了 HEAD、新模块本身还在磁盘上未跟踪：这里测试全绿，"
            "但 CI、别人的 `git worktree add`、任何 clone 都会在第一批 import 上红。\n"
            "输入 keyed 多行：`ref: HEAD`（任何能解析成提交的写法，先钉成 sha 再验，"
            "报告里回显这个 sha）、`modules: agent, tools`（默认 config,tools,agent）、"
            "`timeout: 120`、`gate: 1`、`tests: tests/test_a.py,...`、`suite_timeout: 300`"
            "（跑批层单次秒数，30~300，上限由 `process_runner` 封顶）。"
            "只读 + `worktree add --detach/remove`：不占分支、不动索引、"
            "不碰你的工作树；临时检出用完就收回，且没有本工具的归属标记就绝不删别人的目录。\n"
            "失败会归因，两种红不一样：缺的模块【在磁盘上但不在提交里】= 别人未提交的新文件"
            "被提交引用（先去把那个文件提交掉，或把引用摘出来）；【两边都不在】= 漏提交、改名，"
            "或它本该在 requirements 里。\n"
            "还有第二档：`gate: 1` 在那棵检出里真跑 dev_eval 门禁整集，`tests: a.py,b.py` 真跑"
            "指定测试（仓内相对路径，≤12 个）。为什么需要它：本仓 2026-10-02 出过一次 hunk 暂存"
            "收进了别人那段的【尾巴】——6 条断言进了 HEAD，它们读的局部量还在别人未提交的上半段里。"
            "import 全绿，只验 import 的自检也全绿，**行为**红。红了先在**同一棵检出**里重跑一次"
            "（门禁题里有计时敏感的，抖一下不算红），重跑仍红才拿工作树跑同一条当对照组："
            "「只有检出红」= 差别在没提交的那部分**或**未跟踪的工具链，报告会列出失败项名字让你"
            "分辨哪一种；「两边都红」= 负载、缺工具链或真坏了，不是自洽问题。\n"
            "边界：这验的是【只有已提交内容时能不能跑成】，不验行为对不对——真跑全套件仍是 CI 的活；"
            "需要未跟踪工具链（`frontend/node_modules`、`web/`）的套件在新检出里必然红，那不是提交"
            "坏了。提交完之后、推之前跑一次，比事后 debug 便宜。\n"
        ),
        "func": dev_headcheck,
    },
    "dev_media": {
        "description": (
            "合成媒体夹具：把「对着麦克风说话/对着摄像头」换成一个确定性的 WAV/Y4M 文件，"
            "喂进【真实浏览器】的采集链路，再用回环 WS 探针按 R0 线上协议逐包判定。"
            "R12 的语音回路原来只能在真机上有人说话才验得到，这一条把它变成可以不插电、"
            "不装 Playwright、 CI 里也能跑的检查。\n"
            "输入 keyed 多行：`action: build|run|voice`（默认 run）、"
            "`script: sil:0.3 talk:0.7 sil:1.3`（sil=精确静音 quiet=低于门限 talk=440Hz 0.4 幅度）、"
            "`video: 6`（>0 时同时伪造摄像头并推这么多帧）、`out: fixtures/audio`（产物落项目里的位置，"
            "默认写进已 gitignore 的 .docmind/media-fixture/<时间戳>）、`wait: 10`、`tail: 10`。\n"
            "run 判定的是线上可观测的事实：片形是不是 16k mono 100ms（3200 字节）、内容对不对得上夹具、"
            "说完之后是否仍推 ≥10 片静音尾、静音尾有没有让服务端收口、音频包是否先于视频包。"
            "畸形包会记成协议违规而不是被丢掉；浏览器不可用时如实报未通过，绝不报通过。\n"
            "run 那条回路【不接能量门】（探针页面逐帧无条件推流，为了内容对齐确定），"
            "它只断言夹具的逐帧能量确实跨过 startRms/endRms。要看门限与接线的行为就用 "
            "`action: voice`：用前端自带的 esbuild 把生产模块 `liveAudioControl.ts`/`realtimeProtocol.ts`"
            "编成 ESM 给页面 import（不是抄一份逻辑），胶水照抄开发舱的 `handleLiveAudioFrame`/"
            "`sendNativeAudioReady`，再用假麦克风 + 真点击跑一遍，断言：静音期门不开就一个包都不发、"
            "说完再推 ≥10 片然后【真的停下来】、AI 说话时开口发出 cancel、片形仍是 3200 字节"
            "（顺带证明浏览器里那次 48k→16k 重采样真的做了）。\n"
            "action: build 只出夹具与假设备启动参数，把那些参数逐行放进 `preview_project` 的 `flags:`，"
            "就能让项目自己的页面（比如开发舱的语音回路）在假设备上跑。"
        ),
        "func": dev_media,
    },
    "preview_project": {
        "description": (
            "正式开发舱真实视觉验收：用真实 Edge/Chromium 截取当前画面并把截图"
            "送给视觉模型，同时登记工作流预览证据。输入可选 entry/index.html、width、height、timeout；"
            "也可以给 `url: http://127.0.0.1:<实际端口>/` 直接打【已经在跑的本机服务】（服务由 dev_serve 起，"
            "url 只允许回环地址，端口取 dev_serve 返回的那个，别写死）——Vite/Vue 这类必须跑 dev server 的前端要用这种。\n"
            "还能注入合成媒体：`flags:` 后面每行一条浏览器启动参数，用 dev_media（action: build）"
            "返回的那几条，把麦克风/摄像头换成 WAV/Y4M 夹具的内容，没有真设备也能跑语音/摄像头回路。"
            "参数只接受 `--开关` 形态，`--user-data-dir`、`--load-extension` 一类会改写浏览器行为的一律拒绝。\n"
            "除截图外还会返回运行时异常、console 错误（含报错文本）与失败请求（4xx/5xx/网络失败，含 URL），"
            "这些信号与截图同权：只要存在 console 错误或失败请求，验收判定为未通过。"
            "看到报错就按报错修，不要因为截图「看起来正常」就判定通过。"
            "修改网页后必须再次调用；截图失败或项目不是网页时如实报告并改用领域专用工具。"
        ),
        "func": preview_project,
    },
    "start_workflow": {"description": (
        "当目标是【长链路开发流程】时升级为跨窗口持久开发工作流：满足多阶段/多角色协作、"
        "含写码或命令等副作用阶段、需要人工方案门与审批门、或可能跨窗口中断恢复之一即应使用"
        "（通用开发、游戏、EDA 等任意领域，不局限于游戏）。输入 keyed 多行："
        "goal: 完整可验收目标（也可不写字段名、整段作为目标）；"
        "kind: generic|game|eda（缺省 generic 通用开发；game=游戏开发；eda=原理图/PCB 电子设计）；"
        "web: true|false（缺省继承本轮联网开关）。"
        "它只创建工作流并停在【方案选择门】：工作流卡片会直接在对话中展开，必须由用户在对话内选择方案、"
        "确认任务 DAG 并审批后，多个子代理才开始执行；本工具不直接执行任何有副作用任务，人工门不可跳过。"
        "边界：单个独立子任务用 delegate；一轮内当场并行出结果、无需持久化与人工门用 orchestrate；"
        "需要持久化状态、人工方案/审批门、失败重规划与跨窗口恢复才用 start_workflow。子代理不得调用本工具。"
    ), "func": start_workflow},
    "init_regions": {
        "description": "初始化「分区开发」结构：在代码库根目录建 assets/（素材区）、values/（数值区）、bugs/（bug 区）、behaviors/（角色行为区）等独立子目录（具体分区以 regions.json 为准），每个目录 git init 独立仓库，并生成 DEV_INDEX.md、DOCMIND_RULES.md（分区契约，会被注入 Agent 系统提示，强制越区写被拦截）。用于游戏等分工开发，防止代码堆叠与混乱。输入留空即可；需先配置代码库根目录（/api/ingest_code）。",
        "func": init_regions_tool,
    },
    "dev_list_regions": {
        "description": "列出已配置分区的 key/名称/目录/依赖/导出/脏状态，供你选用正确分区。输入留空即可。在调用 dev_region_read/edit/verify/commit/refactor 前先调用它确认分区 key。",
        "func": dev_list_regions,
    },
    "dev_region_read": {
        "description": "读取某分区内的文件（分区作用域，越区读被拒）。输入：第一行 region: <分区key>，第二行 path: <分区内相对路径>。读取后该文件可被 dev_region_edit 整体重写（满足先读后写护栏）。",
        "func": dev_region_read,
    },
    "dev_region_edit": {
        "description": "受控修改/新建某分区内的文件（分区作用域，越区写被拒；复用 apply_edit/create_file 的全部护栏：先读后写、.py 语法校验、200KB 上限、人工确认）。输入格式：第一行 region: <分区key>，第二行 path: <分区内相对路径>，可选 old_text: <精确旧片段>，最后 new_text: <新内容（可多行）>。提供 old_text 做局部安全替换；省略 old_text 且已 dev_region_read 过该文件则整体重写；文件不存在则按新建处理。",
        "func": dev_region_edit,
    },
    "dev_region_verify": {
        "description": "校验单个分区：若该分区配置了 verify 命令（regions.json 的 verify 字段）则在分区目录内执行；否则检查其导出接口文件是否齐全。输入：region: <分区key>。verify 命令建议写为脚本（如 `pytest tests/`），不要写 `python -c ...`（会被安全策略拦截）。",
        "func": dev_region_verify,
    },
    "dev_refactor": {
        "description": "跨分区安全搬移：把某分区内的文件移动到另一分区（受依赖方向约束，不破坏 git 跟踪）。输入：src_region: <源key>、src_path: <源相对路径>、dst_region: <目标key>、dst_path: <目标相对路径>。目标不能已存在（不覆盖）；搬移后从源分区 git 移除源文件。禁止把代码挪进「已依赖源分区」的分区（避免循环耦合/倒置分层）。",
        "func": dev_refactor,
    },
    "dev_git_diff": {
        "description": "【只读】预览你刚改了什么：工作树或暂存区相对 HEAD 的差异，绝不写入/暂存/提交。改完代码、提交前、以及想确认「自己到底动了哪些文件」时用它。输入可留空（=整个代码根目录），也可限定：paths: <文件或目录，逗号/换行分隔>、staged: true（看已 git add 的，默认看未暂存）、stat: true（只要文件级统计，不要正文）、context: <上下文行数，默认 3>、timeout: <秒，上限 60>；直接把路径当输入也可以。返回变更清单（区分已暂存/未暂存/未跟踪）+ 统计 + 差异正文；正文过长会窗口化并标注，此时加 paths: 限定到单文件再看。注意：未跟踪的新文件不会出现在 diff 正文里，工具会单独列出提示。",
        "func": dev_git_diff,
    },
    "dev_glob": {
        "description": "按文件名通配在代码库里【找文件】（grep 只能搜内容、list_dir 只能一层层翻，开局勘察结构时用它）。输入：pattern: <必填>、scope: <限定目录>、limit: <条数，默认 80，上限 500>；直接把 pattern 当输入也可以。规则：pattern 不含 `/` 时按文件名匹配（`*.vue`、`test_realtime_*.py`、`Dockerfile`）；含 `/` 时按相对路径匹配（`frontend/src/**/*.ts`，`**` 可用单层通配理解）；`**/xxx.ts` 等价于按文件名找任意深度。全小写的 pattern 也会匹配大小写不同的名字，一旦含大写字母就严格区分。返回按「浅层优先」排序的相对路径清单，命中过多会截断并提示收窄。这里【不】限制扩展名（找 lockfile、*.yml、Dockerfile 正是它的用途），但始终跳过 .git/.venv/node_modules/__pycache__ 等噪声目录。",
        "func": dev_glob,
    },
    "dev_git_log": {
        "description": "【只读】看提交历史：谁在什么时候改了什么、某个文件被哪些提交动过、某条 lane 最近在干什么。与 dev_git_diff 的分工——diff 回答「还没提交的改动」，log 回答「已经进历史的那些」；判断重复劳动、确认某改动是否已入库、找回被覆盖前的实现都必须用它。输入（全部可选）：paths: <文件或目录，逗号/换行分隔>、limit: <条数，默认 20，上限 200>、since: <日期，如 2026-09-25>、until: <日期>、author: <作者名片段>、at: <某个提交，如 HEAD 或 b78bd52，也可 a..b>、files: true（每条附带改动文件清单）、timeout: <秒，上限 60>；直接把路径当输入也可以。只走 rev-parse/status/diff/log 四个只读子命令，绝不写入、绝不提交；取值里以 `-` 或 `=` 开头、含控制字符的一律拒绝（防参数注入）。返回 `hash 日期 作者 标题` 流水并标注当前 HEAD。",
        "func": dev_git_log,
    },
    "dev_plan": {
        "description": "把「正在做的事」外置成项目状态目录里的一份可勾选清单——让上下文压缩不再等于丢目标。对话上下文会被摘要压缩，而压缩是有损的（摘要有取舍，摘得越多细节退化越明显）；把任务状态搬到磁盘上、每轮自动注入你的系统消息，于是「上下文被摘掉一大半」不再等于「目标丢了」。输入 keyed 多行：`action: open|append|check|uncheck|read|clear`（默认 read）、`title:` 清单标题、`items:` 条目（逗号或换行分隔）、`item:` 要勾选的条目 key 或文本前缀。典型用法：`action: open / title: 修登录超时 / items: 定位超时点, 加日志, 补测试, 跑一遍` → 每做完一件 `action: check / item: 加日志` → 随时 `action: read` 看进度与「下一件未完成的事」。**长任务、跨多轮、或发现对话已被压缩过时，先 read 再继续，不要凭记忆接着做。** 清单只写项目状态目录、不碰工作树（git status 不会因此变脏）；同名条目自动合并，重述计划不会堆出重复项。",
        "func": dev_plan,
        "capability": "admin",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_lanes": {
        "description": "多人同仓协作的【认领登记表】（带 TTL 的租约）：谁在动哪些文件，冲突当场发现，过期自动可被接手。开工前先 `action: status` 看别的 lane 在动哪块，避免重复造轮子；动某个文件前 `action: check` + `path: <相对路径>` 问一句归属；领到任务 `action: claim` + `lane: AI-A` + `paths: frontend/src/workbench/**, tests/test_live_*.py`（+ 可选 `owner:`、`ttl_minutes:` 默认 120、`note:` 给别人看的说明）；做完 `action: release`，长任务定期 `action: heartbeat` 续租。需要物理隔离（别人收口提交不会把你的半成品卷进 HEAD）时 `action: open`（开通独立 worktree 并把路径回给你，之后的读写与 run_command 都用它），收回用 `action: close`（默认拒绝删除有未提交改动的 worktree，确认丢弃才加 `discard: true`）。排他按目录前缀保守判定：`*.vue` 这种无目录前缀的模式等于占下全仓，会与其他 lane 冲突，请用 `frontend/src/**/*.vue` 收窄。明知无重复但要共管时加 `force: true`，并在 HANDOFF 说明。所有写入只落在项目状态目录，不碰 git 历史。",
        "func": dev_lanes,
        # 认领登记表只写项目**状态目录**（project_state/lanes.json），不碰源码，
        # 所以不该被"用户有没有要求改文件"拦住——它是协作登记，不是改代码。
        # 但它确实改共享状态（认领/心跳/释放），必须 mutating 且不可并发：
        # 两个 lane 并发认领会互相覆盖。与 dev_mcp_add/remove 同类，归 ADMIN。
        # 曾经声明为 read_local/pure/parallel_safe，那是三个错一起犯。
        "capability": "admin",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_agent_log": {
        "description": (
            "让你（agent）**只读**地看自己最近的结构化日志：每次自验证失败、工具调用失败、"
            "关键决策点会被自动记录到项目状态目录的 `agent-log-v1.jsonl`（`Agent._agent_log_event`"
            "在 `agent.py:5581` 等位置自动写入），本工具是这个文件的只读入口。"
            "**何时该调**：（1）用户问「你刚才为什么失败」「你最近做了什么」「自验证为什么没过」"
            "——先调它看证据再回答；（2）连续多轮 LLM 决策后想「复盘自己最近 20 步的走向」"
            "——按 `kind:` 过滤看特定类型事件。**不该调**：用户没问 agent 自身、"
            "也不是复盘场景时不要主动拉日志——日志通常与用户当前任务无关，会烧 token。\n"
            "**只读纪律**：本工具不允许 agent 写自己的日志（写由自动钩子完成，"
            "让 agent 改自己日志容易 noise flood，且与 verification 的 'absence of evidence is not success' "
            "原则冲突）。如果想记录「用户接受了我的方案」之类的人为标记，"
            "请走任务清单 dev_plan / handoff 文档，不要走本工具。\n"
            "**限上限**：`limit` 上限 200 条（默认 20）；防 agent 一次性把全文件读进 prompt 烧 token。"
            "**kind 过滤**：传 `kind: <具体值>` 仅返回该 kind；不传则按写入顺序返回。\n"
            "输入 keyed：`action: read|summary`（默认 read）、`kind: <可选过滤>`、`limit: <1..200>`。"
            "例子：`action: read`（最近 20 条）/ `action: read / kind: self_verify_failed`"
            "（只看过往失败）/ `action: read / limit: 100`（拉多点）/ `action: summary`"
            "（UI 调试面板用的紧凑字符串列表）。"
        ),
        "func": dev_agent_log,
        "capability": "read_local",
        "side_effect": "pure",
        "parallel_safe": True,
    },
    "dev_diagnostics": {
        "description": "拿【结构化诊断】而不是「过/不过」加一坨尾部日志：Python 走 ruff、前端走 `npm run typecheck`（vue-tsc），输出归一成 文件:行:列 + 规则号 + 说明 + 严重度。改完一段代码、跑测试之前先调用它，比 self_verify 更早发现问题；重命名/搬文件之后用它确认没留下未定义引用。输入：target: <文件或目录，逗号/换行分隔，留空=按 scope 扫整个代码根目录>、scope: auto|py|frontend|all（auto 按扩展名选引擎）、rules: <ruff 规则覆盖，默认只查会咬人的 E9,F401,F811,F821,F822,F841,E722；要风格全检写 rules: ALL 或 rules: E,W,F>、timeout: <秒，上限 300>、config: <项目内 ruff 配置文件>；直接把路径当输入也可以。只读：从不传 --fix，从不写文件。工具没装时会【明确说「未安装」并给出安装命令】，绝不把「没跑成」报成「没问题」；输出被截断导致解析不出条目时同样如实报，不会谎称干净。前端诊断需要在 frontend/ 里有 typecheck 脚本且能找到 npm（可用 DOCMIND_NPM_BIN 指定）。",
        "func": dev_diagnostics,
    },
    "dev_probe": {
        "description": "给【后端与命令行】一条真观察凭据——浏览器那一套（preview_project / dev_page_action / dev_vue / dev_apptest）对纯后端项目、一条 CLI、一个 worker 一样都够不着，那段闭环只能退回 `run_command` 加一屏文本由模型自己读，而「没看到报错」很容易被读成「没报错」、「跑完了」很容易被读成「跑对了」。两种模式：`mode: http` 真的发一个请求到回环服务（`url:`、`method:`、`body:`、`headers:`、`expect_status:` 期望状态码、`expect_text:` 正文必须含某串）——**只允许回环**，和 dev_serve / dev_page_action 同一道边界，连不上就明确说「没应答」而不是当成服务正常；5xx 既是「服务在应答」的凭据也是「这次不该通过」的证据，两个意思分开报。`mode: cli` 跑一条命令（`command:`、`expect_text:` 必须出现、`forbid_text:` 不许出现、`allow_silent: true` 这条命令本来就不出声），**除了退出码**还回：ERROR/CRITICAL 级日志行、真 traceback（认 `Traceback (most recent call last)` 与带类型的抛出，不拿正文里出现的 error 字样充数）、`NameError: name 'x' is not defined` 的名字、以及「输出为空」这件事本身——退出码 0 的一个什么都没打印的 CLI 和一个真干了活的 CLI 在退出码上无法区分。命令解析与安全黑名单**复用 run_command 那一套**（自己 split() 会把 `-c \"print(1)\"` 拆坏，更会绕开黑名单）。判定 fail-closed：连不上、命令起不来、超时、输出被截断，每一种都是单独一条红，不会悄悄变成一个好看的结论；access log 那种每行带随机端口的噪声会被折掉，不会挤掉真正的错误。只读观察：不改任何项目文件。",
        "func": dev_probe,
    },
    "dev_handoff": {
        "description": "把【交付】这段补到能真的往前走一步：建议分支名 + commit message 草稿 + 这次提交会包含哪些文件的范围体检。补的是 dev_propose 到「草稿」就停下的那一段——产物躺在状态目录里，人还得自己决定分支名、自己写 message、自己确认改动里有没有混进别人的在途 WIP。本工具算的是**决策材料**，不代替决策：**不 git add、不 commit、不 push、不切分支**，只跑只读 git 子命令（rev-parse/status/diff/log）。输入 keyed 多行（全部可选）：`files: agent_runtime/x.py, tests/test_x.py`（只看这些路径，默认全部改动）、`title: 修好打断后不闭嘴的问题`（有它才推得出像样的 message）、`timeout:`。输出含：当前分支与 HEAD、建议分支名（`type/scope-slug`）、建议 commit message、会包含的文件清单与规模，以及几条必须提醒的事——**共享文件**（非 tests/ 非 .md 的主干文件）单独标出，因为本仓实测教训是 `git commit -- <路径>` 会绕过索引把别人在同一批文件里的在途改动一起扫进去（`b828d10` 就是这么连累 HEAD 的），提交前请核对 `git diff --cached --stat` 或先跑 dev_headcheck；**未跟踪文件**单列，因为它们不在 diff 里，说成「已交付」是这类工具最容易骗人的地方；改了 40 个文件以上会明说「该拆成几个提交」（没人能 review 40 文件的提交）；会看本仓最近的提交标题来判断该不该加 `type(scope):` 前缀，按仓库自己的习惯写而不是强推外来的。不是 git 工作树时明说「没有 git 就没有提交这一步」，不生成任何看起来就绪的材料。",
        "func": dev_handoff,
    },
    "dev_pricing": {
        "description": "从 OpenRouter 官方目录**联网检索**模型单价并写入价格表——不手抄、不写死会过期的表。补的是「custom / OpenAI 兼容中转没有内置单价 → cost_cny 恒为 0」这个洞，而那个 0 与「模型真的免费」在界面上完全一样。输入 keyed 多行（全部可选）：`model: stealth/space-bunny-alpha`（留空＝用当前选中的模型）、`action: fetch|apply`（fetch 只查不写，默认；apply 写进 `.docmind_pricing.json`）、`timeout:` 检索超时秒数。输出单价已从**美元换算成元 / 1M tokens**（汇率可用 `DOCMIND_USD_CNY` 覆盖），含缓存命中读与缓存写入单价，以及**阶梯定价 overrides**（OpenRouter 普遍在长上下文处翻倍，缺了它长对话会被系统性低估一半且看不出错）。三种结果**分开报、绝不混成 0**：`free: true` 表示官方目录标价就是 0（真的免费，建议 apply 写进去，运行台就会明说免费）；`error:` 表示没查到或检索失败（**不是**免费）；否则是正常单价。apply 只动这一个模型、原子写、写完立刻用 price_detail 复核并回显写前/写后差别。",
        "func": dev_pricing,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_local",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_deps": {
        "description": "对账「代码 import 了什么」与「依赖清单声明了什么」，并能把缺的**追加**进去——补的是 vibecoding 最常见的死法：你在工作树里 import 了一个新包，本地因为恰好装过而一切正常，可提交里没有依赖声明，别人 clone 下来或 CI 拉干净检出时第一句 import 就 ModuleNotFoundError。dev_headcheck 能**发现**这件事，但没有工具能修；这条补的就是「知道缺了」到「补上」之间那一段。输入 keyed 多行（全部可选）：`paths: agent_runtime/retrieval.py, api.py` 只查这些文件/目录（留空=全仓）、`action: check|apply`（check=只读对账，默认；apply=把缺失行追加进清单）、`source: requirements.txt`（apply 时指定写进哪个清单）。扫描走 `ast` 不是正则，所以注释里的 `# import numpy` 和字符串里的 `\"import os\"` 不会被当成依赖；相对 import（`from . import x`）一律跳过。import 名与发行名的映射按可信度找证据：本机已安装的发行元数据最硬（实测 `websocket`→`websocket-client`、`yaml`→`PyYAML`、`webview`→`pywebview` 都靠它）→ 已声明依赖的 extras（`psycopg[binary,pool]` 带出 `psycopg-pool`）→ 内置查表 → 同名（标成**弱证据**并注明本机没装）。都不确定就归入 unknown 并明确报出来，**绝不猜一个名字写进清单**——猜错等于装了个不相干的包而真正缺的那个还缺着。本地包不会被误报：`import tools` 指向本仓 tools.py、`game_demo/main.py` 里 `from starvoyager.config import …` 指向 `game_demo/starvoyager/`（按「导入方自己的目录」逐个判，同一个模块对 A 文件是本地、对 B 文件可以是真第三方）。写操作只有一处：往**已存在**的清单追加缺失行（每行带「谁 import 了它」的注释），不改版本、不删行、不重排；没有依赖清单时不替你新建。「声明了但没扫到 import」只作为**线索**列出（pytest/ruff 是命令行用的，不算多余依赖），不影响判定。fail-closed：解析失败的文件、被扫描上限截断、没有依赖清单，都会让报告**不能**被读成「依赖没问题」。",
        "func": dev_deps,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_local",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_propose": {
        "description": "把本轮改动打包成【可评审产物】：一份真 `git diff` 补丁 + 一份提交/PR 草稿，写完把两个路径回给你。**不提交、不推送**（提交与推送是人的决定，命令黑名单也拦着），补的是「改完了却只能口头描述」这段交付空洞。输入 keyed 多行（全部可选）：`title:` 草稿标题兼产物命名、`body:` 为什么这么改、`files: a.py, tests/` 只打包这些路径（默认全部未暂存改动）、`staged: true` 打包暂存区、`test_plan:` 复核步骤、`lane: AI-A` 署名、`timeout:`。产物写在【项目状态目录】里而不是仓库工作树——多人同仓时，把临时文件写进工作树等于给别人埋雷，所以 `git status` 不会因为打包变脏。未跟踪的新文件【不伪装成补丁】，只在草稿里列成清单并写明「需确认后再入库」。补丁可直接喂给 dev_patch 重放。",
        "func": dev_propose,
        # 只读工作树（git diff）、产物只写项目**状态目录**，不动源码，
        # 因此不该被写意图护栏拦——"把改动打包成评审产物"不是改代码。
        # 但它确实落盘，标 mutating + 不可并发。与 dev_lanes 同类，归 ADMIN。
        "capability": "admin",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_ci_status": {
        "description": "【只读】看 CI：最近的 workflow runs、结论、失败 job，以及【本地 HEAD 到底有没有被 CI 跑到】——后者在「push 得在用户机器上跑」的环境里尤其重要，没 push 时它会直说「这些 run 里没有本地 HEAD」而不是让你以为绿了。输入 keyed 多行（全部可选）：`branch: main`、`ref: <sha>`、`per_page: 5`（上限 20）、`jobs: true`（把最近一次的 job 列表拉回来，能指出是哪个 job 挂的）、`log_url: <地址>` 单独抓一份 job 日志并只回错误行、`timeout:`。需要 GitHub token（DOCMIND_GITHUB_TOKEN / GITHUB_TOKEN / GH_TOKEN 任一）。本工具绝不触发或重跑 workflow；没 token、401/403、404 或连不上时明确回「未完成 + 原因」，不会编一个状态出来。仓库归属直接读 .git/config 的 origin，不调 git 也不猜。",
        "func": dev_ci_status,
    },
    "dev_patch": {
        "description": "把一段【unified diff】原子地打到代码库里：全成才打，任一文件对不上就一个都不改、树上不留半成品。输入就是 diff 正文（`--- a/路径`、`+++ b/路径`、`@@ -起行数,行数 +起行数,行数 @@`、空格/`+`/`-` 行），首行可加 `dry_run: true` 只做校验与语法检查不写盘（大补丁先空跑一次）。护栏：路径必须是代码根目录内的相对路径（`a/`、`b/` 前缀会自动去掉，`../` 与绝对路径拒绝）；上下文行与被删行必须逐字相等——【不做模糊匹配】，宁可报错也不改错地方；声明行号偏了但上下文一致时会自动对齐并在结果里说明落到了第几行；.py 打完还会过一遍 ast 语法检查，语法不过直接整体拒绝；删除文件一律不让补丁做（会用带确认的删除工具）。一次最多 20 个文件、单文件 200KB。改多处调用点、落地外部评审意见、回滚某段改动时优先用它，比连续多次 apply_edit 安全。",
        "func": dev_patch,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_local",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_move": {
        "description": "移动/重命名代码库内的【单个文件】：被 git 跟踪的走 `git mv`（保留历史与 blame，git 会显示为 R），未被跟踪的退回文件系统移动。输入两行：`from: <旧路径>`、`to: <新路径>`（相对代码根目录），可选 `timeout: <秒>`。只做移动：目标已存在一律拒绝不覆盖、不删目录、不越界、单文件 200KB 上限。注意 `run_command` 只拦【历史类】git 子命令（commit/push/reset/checkout/worktree 等），`git mv` 本来就能跑——这个工具的价值是把移动约束成安全路径（越界拒绝、不覆盖、强制回归提示），而不是绕过禁令。移动之后必须用 dev_find_references / grep 检查旧路径的 import 与引用，再用 dev_diagnostics 确认没打断。",
        "func": dev_move,
        # 写盘工具：曾被声明为 read_local/pure/parallel_safe，
        # 于是绕过写意图护栏、拿不到幂等账本、还能和只读工具并发写。
        "capability": "write_local",
        "side_effect": "mutating",
        "parallel_safe": False,
    },
    "dev_find_references": {
        "description": "重构前定位符号的全部引用点（重命名/删函数/改签名/搬文件前必用）。Python 走 ast 精确匹配——注释与字符串里的同名文本【不会】被误报；TS/TSX/JS/JSX/Vue 走 tree-sitter 语法匹配，注释、字符串字面量与对象字面量的键都不算引用，Vue 会解析 <script> 块并把行号映射回文件真实行（模板里的同名文本用词边界正则补充，并在 notes 说明）；其他语言走词边界正则并跳过纯注释行。语法包缺失时自动回退正则，不会静默丢结果。输入：symbol: <符号名>（必填），可选 scope: <限定目录或文件>、kind: def|ref|all（默认 all）、limit: <条数上限，默认 80>；直接把符号名当输入也可以。返回定义处与引用处（file:行号 + 代码行，按文件聚合计数），低置信的字符串/键名单列。命中过多时请用 scope 缩小范围。",
        "func": dev_find_references,
    },
    "dev_commit": {
        "description": "提交单个分区的改动（该分区独立 git 仓库内 commit）。输入：region: <分区key> 换行 message: <提交说明>。",
        "func": dev_commit,
    },
    "dev_verify_contracts": {
        "description": "校验全部分区的契约：依赖方向无环、被依赖分区导出文件存在、依赖目标存在。输入留空即可。建议在大幅改动分区前后调用，确认架构约束未被破坏。",
        "func": dev_verify_contracts,
    },
    "dev_rebuild_index": {
        "description": "依据当前分区配置重算 DEV_INDEX.md（分区目录/文件数/依赖变动后调用）。输入留空即可。",
        "func": dev_rebuild_index,
    },
    "dev_commit_all": {
        "description": "把所有分区的改动各提交一次，并记进 dev_changesets.jsonl 作为一次绑定变更集（可整体回滚）。输入：message: <本次功能改动说明>。返回变更集 id 与各分区提交哈希。",
        "func": dev_commit_all,
    },
    "dev_list_changesets": {
        "description": "列出已记录的跨区变更集（dev_changesets.jsonl）。输入留空即可。返回各变更集 id/message/涉及分区，供 dev_rollback_changeset 选用。",
        "func": dev_list_changesets,
    },
    "dev_rollback_changeset": {
        "description": "整体回滚某变更集：对每个分区 revert 其记录的 commit（生成新提交撤销改动）。输入：changeset: <变更集id> 或 id: <变更集id>。先用 dev_list_changesets 查看 id。",
        "func": dev_rollback_changeset,
    },
    "list_dir": {
        "description": "浏览代码库内的目录结构（限定 code_root，供 Agent 研判代码库组织方式）。输入：目录相对路径，可直接写 behaviors/ 或 behaviors（也兼容 path: behaviors/）；留空列根目录。返回子项（目录/文件）及大小/类型。在研判分区方案前先用它勘察顶层有哪些模块/资源目录。",
        "func": list_dir,
    },
    "dev_propose_regions": {
        "description": "依据真实代码库结构，由你研判分区方案（默认 8 个分区仅作初始建议，实际分区由你判断）。输入留空即可。返回结构分析 + 建议分区清单（每区带 detected 证据与 included 启用建议）+ 代码库特有的可独立模块 + 中文结论。先用 list_dir 勘察，再调用它拿基线，据 detected/included 增删分区，最后用 dev_apply_regions 落地。",
        "func": dev_propose_regions,
    },
    "dev_apply_regions": {
        "description": "应用你研判后的自定义分区方案：把给定分区清单写入 regions.json 并初始化（建目录/每区 git/导出桩/规则），此后写操作被约束在这些分区内。输入：regions: <JSON 数组，或 {\"regions\":[...]} 对象>；每项至少含 key 与 dir，可选 name/desc/depends_on/exports/verify。可由 dev_propose_regions 的产出裁减得到。",
        "func": dev_apply_regions,
    },
    "dev_add_region": {
        "description": "向现有分区配置追加（或覆盖同名）一个分区并立即初始化（建目录/每区 git/规则）。输入：key: <分区key> 换行 dir: <目录> 换行 name: <中文名> 换行 [desc:] [access:] [depends_on:]（逗号分隔） [exports:]（逗号分隔）。用于研判后按需增补单个分区，无需重传整个方案。",
        "func": dev_add_region,
    },
    "dev_approve": {
        "description": "审批分区提交/回滚或隔离工具安装等敏感操作前必须调用。MCP 安装和能力路由不能由 Agent 自批，必须由用户在工作台确认。",
        "func": dev_approve,
    },
    "dev_approval_status": {
        "description": "查询某敏感操作当前是否已通过审批。输入：action: <操作名> 换行 target: <对象>(默认 *)。返回已通过/未通过，用于决定是否需要先调用 dev_approve。",
        "func": dev_approval_status,
    },
    "dev_install_tool": {
        "description": "缺失工具的隔离安装：输入 manager: python|node 换行 package: 包名 换行 version: 可选版本 换行 fallback_tools: 失败时可替代的内置工具。只安装到项目 .docmind/tool_envs，必须先用 dev_approve 审批 action=install_tool、target=<manager>:<package[==version]>；安装后会验证实际版本和沙箱路径，失败会返回替代方案和审计记录。",
        "func": dev_install_tool,
    },
    "dev_tool_install_audit": {
        "description": "查看项目内工具安装审计记录。输入可选 limit: 50；不会执行安装。",
        "func": dev_tool_install_audit,
    },
    "tool_catalog": {
        "description": (
            "按大类查工具清单与用法。内置工具超过百个，系统提示里只放两级目录"
            "（14 个大类 + 定位），不逐条展开——确定要哪一类再查这一类，省上下文。"
            "输入大类 code：files / verify / frontend / preview_adapter / mcp / "
            "plan / task / git / data / jobs / web / knowledge / game / net；"
            "留空返回目录本身。**查工具用法前先调它**，别凭名字猜参数。"
        ),
        "func": catalog_tool,
    },
}

# Convert the built-in registry once at import time.  Extensions and tests may
# still add legacy dict entries later; consumers call ``coerce_tool_spec`` at
# their boundary so those remain compatible.
upgrade_registry(TOOLS)


def tool_schemas(names=None, registry=None):
    """把 TOOLS 注册表导出为 OpenAI 风格函数 schema（原生 function-calling 用）。

    所有工具统一暴露单个 `input` 字符串参数，与文本协议的 `Action Input` **同形**，
    因此原生通道与文本通道共用同一套入参归一化（`_normalize_tool_arg`）与全部护栏。
    `names` 可限定子集（子代理的工具白名单就用它）。
    """
    out = []
    source = registry if registry is not None else TOOLS
    for name, raw_meta in source.items():
        if names and name not in names:
            continue
        meta = coerce_tool_spec(name, raw_meta)
        desc = (meta.description or "").strip()
        parameters = dict(meta.input_schema)
        properties = dict(parameters.get("properties") or {})
        properties.setdefault("input", {
            "type": "string",
            "description": "兼容旧调用的原始 Action Input；与结构化字段二选一。",
        })
        parameters["properties"] = properties
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc[:1024],
                "parameters": parameters,
            },
        })
    return out


def evidence_authority_tool_names():
    """返回"自带结构化判定、可替代 文件:行号 作为终答证据"的工具名集合。

    判据是 ToolSpec 上的 `verdict_authoritative` 标记（目前只有 `eda_report_gate`）。
    这类工具的观察本身就是一条算出来的验收结论（met/unmet/rollback，来自真取像/报告解析），
    终答的证据就是这条观察，不该再被 `output_audit` 的定位门要求"给出可核验的仓库引用"。
    从这里取集合再传给 `audit_evidence(authority_tools=…)`，避免 output_audit 反向 import
    tools 造成循环依赖。spec 既可能是裸 dict，也可能是 ToolSpec 对象，两种都读。
    """
    names = set()
    for name, spec in TOOLS.items():
        try:
            flag = (spec.get("verdict_authoritative")
                    if isinstance(spec, dict)
                    else getattr(spec, "verdict_authoritative", False))
        except Exception:
            flag = False
        if flag:
            names.add(name)
    return names
