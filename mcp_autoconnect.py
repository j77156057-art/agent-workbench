"""MCP 自动连接：抽取 → 信任闸门 → 试连 → 注册代管。

核心不变量（见 ARCHITECTURE.md §2.2 / IMPL-PLAN §1）：
- 抽取层**只产出数据**，绝不调用 subprocess（`subprocess.Popen` 只在 `mcp_client.py:405`）。
- 真实执行只有两处：`probe_candidate`（受控命令探 tools/list，用毕即关）与
  `confirm`（用户显式 `user_ack` 后 `save_server` 写盘）。
- 不可信网页/截图正文先过 `parse_install_command` 确定性解析，再过 R1-R9 信任闸门，
  到不了 `Popen`。

本模块全部为纯函数 + 编排；浏览器代管 `browser_register` 的 playwright 仅在被调用时
惰性 import，不影响其它路径。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import concurrent.futures
import base64
import urllib.request
import urllib.parse
from typing import Any, Callable, Optional

import mcp_client
import secrets_store
import project_state
import temp_state
from textutil import as_text as _as_text
from mcp_server_index import match_curated_server, curated_entry_to_config
from mcp_registry_bridge import _registry_layer
# 属性取用（不是 from-import）：`tools.py:988` 写的是 `mcp_autoconnect.registry_fn_for(root)`，
# 静态检查看不见「通过模块对象拿这个名字」这条路，删掉本行会当场炸它。
from mcp_registry_bridge import registry_fn_for  # noqa: F401
# 属性取用：`tests/test_mcp_registry.py:175` 断言 `mcp_registry.ALLOWED_LAUNCHERS ==
# mcp_autoconnect.ALLOWED_LAUNCHERS`（两份白名单必须一致），走的是本模块的属性。
from mcp_validation import ALLOWED_LAUNCHERS  # noqa: F401
from mcp_validation import (
    SECRET_INLINE_RE,
    ALLOWED_URL_RE, HTTP_ENDPOINT_RE,
    parse_install_command, _domain_of,
    is_trusted_domain, _safe_dir, _looks_like_secret,
    validate_extracted_config as _validate_extracted_config,
)
# 属性取用：`tests/test_mcp_autoconnect.py:205` 调 `mcp_autoconnect.render_diagnostic_svg(...)`，
# `:208` 读 `mcp_autoconnect.DEFAULT_DIAG_STAGES` —— 都是走本模块对象取名字。
from mcp_candidates import DEFAULT_DIAG_STAGES, render_diagnostic_svg  # noqa: F401
from mcp_candidates import (
    build_candidate,
    _candidate_view, _dedupe,
)
# 属性取用：`tests/test_mcp_autoconnect.py:862/863` 读 `mcp_autoconnect.PROVIDER_ADAPTERS[...]`
# 来断言各家适配器不许默认自动提交，走的是本模块对象而不是 from-import。
from mcp_providers import PROVIDER_ADAPTERS  # noqa: F401
from mcp_providers import (
    DEFAULT_USER_PROMPT, CHALLENGE_KEYWORDS, CHALLENGE_URL_MARKERS,
    select_provider_adapter, _adapter_by_url, _provider_url_trusted,
)

_log = logging.getLogger("docmind.mcp_autoconnect")


# 浏览器级 UA：GitHub 等站点对朴素 UA 直接 403 / 反爬；贴近 Chrome 才能稳定取正文。
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class AutoConnectError(Exception):
    """抽取/校验/注册层的可展示错误（非 MCPError）。"""


# ---------------------------------------------------------------- 纯函数（无网络、无 subprocess、可离线单测）

def validate_extracted_config(cand: dict[str, Any]) -> tuple[bool, list[str]]:
    """保留主模块调用入口和安全目录判定注入点。"""
    return _validate_extracted_config(cand, safe_dir=_safe_dir)

# ---------------------------------------------------------------- 编排（不执行、不写盘；仅 search/fetch/解析/校验）

def _github_readme_markdown(url: str) -> str:
    """把 github.com 仓库/子路径链接改写为 api.github.com readme/contents，解码 base64 → markdown。

    live 路径兜底：官方文档多是 JS 渲染 HTML，urllib 剥离 script 后无可读文本；改用 GitHub
    REST API 取 README 原文（已验证 api.github.com 对无鉴权 GET 返回 200 + base64 markdown）。
    失败（非 github 链接 / 网络异常 / 解码失败）返回 ''，由调用方继续走原 fetch 或放弃。
    """
    m = re.match(r"https?://github\.com/([^/?#]+)/([^/?#]+)(/?.*)?", url or "")
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    sub = (m.group(3) or "").strip("/")
    branch = None
    if sub:
        parts = sub.split("/")
        if parts and parts[0] in ("main", "master", "dev"):
            branch = parts[0]
            sub = "/".join(parts[1:])
    try:
        if sub:
            api = f"https://api.github.com/repos/{owner}/{repo}/contents/{sub}"
            if branch:
                api += f"?ref={branch}"
        else:
            api = f"https://api.github.com/repos/{owner}/{repo}/readme"
            if branch:
                api += f"?ref={branch}"
        req = urllib.request.Request(
            api, headers={"User-Agent": _BROWSER_UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        # contents 目录返回列表：挑 README.* 再取一次
        if isinstance(data, list):
            readme = next((d for d in data if d.get("name", "").lower().startswith("readme")), None)
            if not readme:
                return ""
            req2 = urllib.request.Request(readme["url"], headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req2, timeout=12) as r2:
                data = json.loads(r2.read().decode("utf-8", "replace"))
        content = data.get("content")
        if not content:
            return ""
        return base64.b64decode(content).decode("utf-8", "replace")
    except Exception:
        return ""


def auto_connect_pipeline(root: str, query: str, *,
                          search_fn: Optional[Callable[[str], str]] = None,
                          fetch_fn: Optional[Callable[[str], str]] = None,
                          llm_fn: Optional[Callable[[str, str], str]] = None,
                          registry_fn: Optional[Callable[[str], dict[str, Any]]] = None) -> dict[str, Any]:
    """搜索 → fetch 官方正文 → 抽取 → 校验 → 返回候选。内部吞 search_fn/fetch_fn 异常。

    命中顺序（互补、互不阻塞）：
      0) 官方 Registry（自主主路径；仅当调用方注入 registry_fn）；
      1) 离线精选索引（瞬时、可信、无需联网，对「未联网」路径也生效）；
      2) 联网搜索 → fetch 官方正文（github 链接兜底走 API readme，绕过 JS 渲染壳）；
      3) 仍无正文 → 退化 LLM 直给命令（若提供 llm_fn）。

    返回 {'ok': bool, 'candidates': list[dict], 'search_error': str, 'source': str}。
    抛 AutoConnectError 仅用于致命配置错误（如 root 无效）。
    """
    if not root or not os.path.isdir(root):
        raise AutoConnectError("无效的项目根目录")
    candidates: list[dict[str, Any]] = []
    search_error = ""
    sources: list[str] = []

    # 0) 官方 Registry（自主发现主路径）：调用方注入 registry_fn 才联网，测试/离线不触发。
    if registry_fn is not None:
        reg_views, reg_err, reg_src = _registry_layer(registry_fn, query)
        if reg_views:
            return {"ok": True, "candidates": _dedupe(reg_views), "search_error": "",
                    "source": reg_src or "registry"}
        if reg_err:
            search_error = reg_err

    # 1) 离线精选索引兜底：已知 server 直接出候选，省去联网抓取与 JS 渲染失败。
    curated = match_curated_server(query)
    for entry in curated:
        cfg = curated_entry_to_config(entry)
        ok, errs = validate_extracted_config(cfg)
        if ok:
            domain = cfg["provenance"].get("domain", "")
            trust = "trusted" if is_trusted_domain(domain) else "source_untrusted"
            candidates.append(_candidate_view(cfg, trust, errs))
    if candidates:
        return {"ok": True, "candidates": _dedupe(candidates), "search_error": "", "source": "curated"}

    try:
        if search_fn:
            raw = _as_text(search_fn(f"{query} MCP server official GitHub installation connection usage"))
            sources = list(dict.fromkeys(re.findall(r"https?://[^\s)]+", raw)))[:8]
    except Exception as exc:
        search_error = str(exc)[:300]

    docs: list[tuple[str, str]] = []
    for url in sources:
        # 逐 URL 兜底：任一来源返回非字符串 / 抛错都只跳过该条，绝不打崩整个请求。
        # （曾因 fetch_fn 返回 ToolResult 而 `body[:20]` 抛 TypeError → HTTP 500。）
        try:
            body = _as_text(fetch_fn(url)) if fetch_fn else ""
        except Exception:
            body = ""
        # github 链接兜底：JS 渲染 HTML 抓不到正文，改写走 API readme 取 markdown
        if not body or "网页读取失败" in body[:20] or "读取失败" in body[:20]:
            try:
                gh = _github_readme_markdown(url)
            except Exception:
                gh = ""
            if gh:
                body = gh
        if body and "网页读取失败" not in body[:20] and "读取失败" not in body[:20]:
            docs.append((url, body))

    # 无来源则无法安全抽取（保留 :117 安全语义：正文才成候选，不凭摘要捏命令）
    if not docs and llm_fn:
        try:
            # llm_fn 返回值可能是 ToolResult 等非 str 包装对象，先规整再做切片/正则。
            hint = _as_text(llm_fn(query, "请只给出官方安装命令，如 uvx <pkg> 或 npx -y <pkg>"))
            if hint:
                docs.append(("", hint))
        except Exception:
            pass

    for url, body in docs:
        domain = _domain_of(url) if url else ""
        trusted = is_trusted_domain(domain) if domain else False
        # http 端点
        m = HTTP_ENDPOINT_RE.search(body or "")
        if m:
            url_cand = m.group(0)
            cand = {
                "transport": "http", "command": "", "args": [], "url": url_cand,
                "env": {}, "headers": {}, "provenance": {"url": url, "domain": domain},
                "command_unresolved": False,
            }
            ok, errs = validate_extracted_config(cand)
            if ok:
                candidates.append(_candidate_view(cand, "trusted" if trusted else "source_untrusted", errs))
        # stdio 命令
        parsed = parse_install_command(body or "")
        if parsed:
            cand = build_candidate(parsed, provenance={"url": url, "domain": domain})
            ok, errs = validate_extracted_config(cand)
            if ok:
                # R8：不可信来源不自动填参，仅标记 source_untrusted 供前端走手动确认
                trust = "trusted" if trusted else "source_untrusted"
                candidates.append(_candidate_view(cand, trust, errs))

    # 诊断：空候选时给出可展示原因，避免前端只有空态无解释。
    if not candidates and not search_error:
        if not search_fn:
            search_error = "未启用联网搜索，无法从官方文档提取连接命令（可勾选“允许联网补充来源”后重试）"
        elif not sources:
            search_error = "联网搜索未返回可用的官方文档链接"
        elif not docs:
            search_error = "官方文档链接均未能读取正文（可能被网络/反爬拦截）"
        else:
            search_error = "已读取官方文档，但未从中解析出可信的安装命令（请手动填写）"

    source = "web" if candidates else "none"
    return {"ok": bool(candidates), "candidates": _dedupe(candidates),
            "search_error": search_error, "source": source}


def discover_from_need(root: str, need: str, *, web_enabled: bool = False,
                       github_search_fn: Optional[Callable[[str], str]] = None,
                       registry_fn: Optional[Callable[[str], dict[str, Any]]] = None) -> dict[str, Any]:
    """从自然语言需求发现可装配的 MCP 连接器候选（不写盘、不自动启用路由）。

    命中顺序：
      0) 官方 Registry（自主主路径；`web_enabled` 且注入 `registry_fn` 时）；
      1) 离线精选索引 match_curated_server(need)：已知热门 server 秒级、可信；
      2) web_enabled 且提供 github_search_fn 时：GitHub 域限定搜索（platform:github）→
         取仓库 URL → _github_readme_markdown 取 README 原文（绕过 JS 渲染壳）→
         parse_install_command 抽取命令 → R1-R9 信任闸门校验。

    每个候选带 provenance（来源域 / 链接）与 trust 标记；调用方须向用户展示候选，
    逐条经 dev_mcp_add（审批闸门）落盘，绝不可静默写入。
    返回 {'ok': bool, 'candidates': list[dict], 'search_error': str, 'source': str}。
    """
    if not root or not os.path.isdir(root):
        raise AutoConnectError("无效的项目根目录")
    need = (need or "").strip()
    if not need:
        return {"ok": False, "candidates": [], "search_error": "需求描述为空", "source": "none"}

    candidates: list[dict[str, Any]] = []
    search_error = ""

    # 0) 官方 Registry（自主主路径）：registry 是联网源，故仍需 web_enabled 才触发。
    if web_enabled and registry_fn is not None:
        reg_views, reg_err, reg_src = _registry_layer(registry_fn, need)
        if reg_views:
            return {"ok": True, "candidates": _dedupe(reg_views),
                    "search_error": "", "source": reg_src or "registry"}
        if reg_err:
            search_error = reg_err

    # 1) 离线精选索引兜底：已知 server 直接出候选，省去联网与 JS 渲染失败。
    for entry in match_curated_server(need):
        cfg = curated_entry_to_config(entry)
        ok, errs = validate_extracted_config(cfg)
        if ok:
            domain = cfg.get("provenance", {}).get("domain", "")
            trust = "trusted" if is_trusted_domain(domain) else "source_untrusted"
            candidates.append(_candidate_view(cfg, trust, errs))

    # 2) 联网：仅 GitHub 域（site 限定 + GitHub API readme 兜底），绝不走泛搜索。
    search_error = ""
    if not candidates and web_enabled and github_search_fn:
        try:
            raw = _as_text(github_search_fn(need))
        except Exception as exc:
            raw = ""
            search_error = f"GitHub 搜索失败：{type(exc).__name__}: {exc}"[:300]
        urls = list(dict.fromkeys(re.findall(r"https?://github\.com/[^\s)'\"<>]+", raw)))[:5]
        for url in urls:
            md = _github_readme_markdown(url)
            if not md:
                continue
            parsed = parse_install_command(md)
            if not parsed:
                continue
            cand = build_candidate(parsed, provenance={
                "url": url, "domain": "github.com", "github_repo": url, "source": "github_search"})
            ok, errs = validate_extracted_config(cand)
            if ok:
                candidates.append(_candidate_view(cand, "trusted", errs))
        if not candidates and not search_error:
            search_error = ("GitHub 搜索未返回可用仓库链接" if not urls
                            else "已读取 GitHub README，但未解析出可信安装命令（请手动确认）")

    source = ("offline" if (candidates and not web_enabled)
              else "github" if candidates else "none")
    return {"ok": bool(candidates), "candidates": _dedupe(candidates),
            "search_error": search_error, "source": source}


def resolve_secret_refs(cfg: dict[str, Any], root: str) -> dict[str, Any]:
    """@secret:<provider> → secrets_store.load(root, provider) 明文。

    仅返回新 dict，不改原 cfg。支持**值内嵌** `@secret:`（如 `Bearer @secret:smithery_api_key`
    → `Bearer <明文>`）；整值 `@secret:NAME` 仍等价（向后兼容）。env 与 headers 同走此规则。
    引用了未存入的 provider 时抛 AutoConnectError（可读，不静默）。
    """
    specs = ((cfg.get("provenance") or {}).get("secret_specs")
             if isinstance(cfg.get("provenance"), dict) else None)
    optional = {str(s.get("name")) for s in (specs or [])
                if isinstance(s, dict) and s.get("name") and not s.get("required")}
    out = dict(cfg)
    for sec in ("env", "headers"):
        src = cfg.get(sec) or {}
        if not src:
            continue
        out[sec] = {k: _substitute_secret_refs(v, root, optional) for k, v in src.items()}
    return out


def _substitute_secret_refs(value: Any, root: str,
                            optional: Optional[set[str]] = None) -> str:
    """把值里所有 `@secret:<provider>` 就地替换为明文；可选且未存储的引用为空串。"""
    optional = optional or set()
    def _repl(m: re.Match[str]) -> str:
        provider = m.group(1)
        plain = secrets_store.load(root, provider)
        if not plain:
            if provider in optional:
                return ""
            raise AutoConnectError(f"凭证提供方 {provider} 未存入 secrets_store，无法解析 @secret 引用")
        return plain
    return SECRET_INLINE_RE.sub(_repl, str(value))


def http_headers_for(cfg: dict[str, Any], root: str) -> dict[str, str]:
    """http 传输可发送的自定义请求头（含 @secret 解密）。带**受信域安全闸**。

    - url 非 https / 主机非受信域 → 返回 {}（忽略请求头，并记日志），防凭证外泄到明文/陌生主机；
    - 否则用 resolve_secret_refs 解析 env/headers 里的 @secret；**未存入的 provider 会抛
      AutoConnectError（可读，不静默）**。
    注：本函数在 mcp_autoconnect 侧（调用方）裁决可信度，以避免 mcp_client 反向 import 造成循环。
    """
    raw = dict(cfg.get("headers") or {})
    if not raw:
        return {}
    url = str(cfg.get("url") or "")
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if ((parts.scheme or "").lower() != "https" or not ALLOWED_URL_RE.match(url)
            or not is_trusted_domain(host)):
        _log.warning("忽略 http 请求头：主机非受信域或非 https（host=%s）", host or "?")
        return {}
    provenance = cfg.get("provenance")
    specs = (provenance.get("secret_specs") or []) if isinstance(provenance, dict) else []
    optional = {str(spec.get("name")) for spec in specs
                if isinstance(spec, dict) and spec.get("name") and not spec.get("required")}
    raw = {name: value for name, value in raw.items()
           if not any(match.group(1) in optional
                      and not secrets_store.load(root, match.group(1))
                      for match in SECRET_INLINE_RE.finditer(str(value)))}
    resolved = dict(resolve_secret_refs(
        {"headers": raw, "provenance": cfg.get("provenance")}, root
    ).get("headers") or {})
    return {k: v for k, v in resolved.items() if str(v).strip()}


def probe_candidate(root: str, cand: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """临时进程 initialize + tools/list，读后 close()（不持久化、不路由）。

    复用 mcp_client._StdioSession（stdio）/ _http_initialize + _http_post（http）。
    失败抛 mcp_client.MCPError。
    """
    cfg = dict(cand)
    cfg["command"] = mcp_client.resolve_command(cfg.get("command") or "")
    if cfg.get("transport") == "http":
        from mcp_client import _http_initialize, _http_post  # 惰性，避免顶层依赖私有符号
        # 凭证缺失/非受信域：mcp_client._http_headers 抛 MCPError（可读）或返回 {}（不发头）。
        headers = mcp_client._http_headers(cfg, root)
        _http_initialize(cfg, headers=headers)
        result = _http_post(cfg["url"], {"jsonrpc": "2.0", "id": 2,
                                         "method": "tools/list", "params": {}},
                            headers=headers) or {}
        names = [t.get("name") for t in result.get("tools", [])]
        return {"ok": True, "probe_ok": True, "tools": names, "error": ""}
    sess = mcp_client._StdioSession(cfg, cwd=os.path.abspath(root))
    try:
        sess.initialize(timeout=timeout)
        tools = sess.request("tools/list", {}, timeout=min(timeout, 30)) or {}
        names = [t.get("name") for t in tools.get("tools", [])]
        result = {"ok": True, "probe_ok": True, "tools": names, "error": ""}
        # EasyEDA 桥接进程本身可以连通，但编辑器扩展可能未安装。已知只读状态工具
        # 可区分这两层，避免向用户误报「已能设计电路」。其他 MCP 不猜测健康状态。
        if "easyeda_live_status" in names:
            try:
                health = sess.request("tools/call", {
                    "name": "easyeda_live_status", "arguments": {},
                }, timeout=min(timeout, 10)) or {}
                status = (health.get("structuredContent") or {}).get("status") or {}
                if isinstance(status.get("connected"), bool):
                    result["ready"] = status["connected"]
                    if not status["connected"]:
                        result["readiness_message"] = (
                            "MCP 服务已连接，但 EasyEDA Pro 扩展未连接。"
                            "请打开 EasyEDA Pro、安装对应扩展并允许外部交互。")
            except (mcp_client.MCPError, ValueError, TypeError):
                pass
        return result
    finally:
        sess.close()


def vision_extract_params(image_b64: str, root: str) -> dict[str, Any]:
    """图 → agent_runtime.vision.analyze_images → 观察文本 → build_candidate → validate（同闸门）。

    返回 auto_connect_pipeline 同形结构。模型读出命令也过 R1-R9，绝不因来自图片就放行。
    """
    from agent_runtime import vision
    if not image_b64:
        return {"ok": False, "candidates": [], "search_error": "未提供图片"}
    _, context, audit = vision.analyze_images([image_b64])
    text = context if isinstance(context, str) else ""
    if not text or audit.get("mode") in ("none", "unavailable"):
        return {"ok": False, "candidates": [], "search_error": "视觉模型未返回可观察文本"}
    # 观察文本当作不可信正文，走同一抽取+闸门
    parsed = parse_install_command(text)
    if not parsed:
        return {"ok": False, "candidates": [], "search_error": "未能从图片观察中安全提取安装命令"}
    cand = build_candidate(parsed, provenance={"url": "", "domain": ""})
    ok, errs = validate_extracted_config(cand)
    if not ok:
        return {"ok": False, "candidates": [], "search_error": "；".join(errs)}
    trust = "source_untrusted"  # 图片来源不可核，保守标记
    return {"ok": True, "candidates": [_candidate_view(cand, trust, errs)], "search_error": ""}


# ---------------------------------------------------------------- 注册代管（L0/L1/L2 降级链）

# 进程内 live 会话注册表（仿 mcp_client._SESSIONS）：磁盘 context_dir 作崩溃恢复兜底。
_BROWSER_SESSIONS: dict[str, dict[str, Any]] = {}
_BROWSER_SESSIONS_LOCK = threading.Lock()

# 所有触 playwright 的操作串行到单一线程：playwright sync API 线程亲和（对象只能在
# 创建它的线程使用），而 FastAPI 的 run_in_threadpool 每次请求可能换线程——故 launch/
# goto/detect/fill/capture/close 全部 submit 到该单 worker 执行，保证创建与使用同线程，
# 跨请求复用 live page 才安全。单 worker 也顺带把并发浏览器请求串行化。
_BROWSER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="mcp-browser")


def _browser_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把浏览器操作提交到单线程 executor 并等待结果。

    被提交的函数内部自行 try/except 收敛异常（返回结果字典），故 future 一般不抛；
    若仍抛（例如 executor 已关闭），异常向上抛由调用方降级处理，绝不静默吞错。
    """
    return _BROWSER_EXECUTOR.submit(fn, *args, **kwargs).result()


def _edge_in_programfiles() -> bool:
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        pf = os.environ.get(env)
        if pf and os.path.isfile(os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe")):
            return True
    return False


def _edge_available() -> bool:
    return bool(shutil.which("msedge")) or _edge_in_programfiles()


def _playwright_available() -> bool:
    try:
        return True
    except Exception:
        return False


def _new_task_id() -> str:
    import uuid
    return f"ac_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"


def _new_context_dir() -> str:
    """建一个持久上下文目录，顺手回收被硬杀留下的空壳。

    `launch_persistent_context` 用的是调用方自持的 `user_data_dir`，playwright 故意不删，
    所以只有我们能删。实测这台机器上攒了 2,109 个 `docmind_ac_*` 与 22 个
    `playwright_chromiumdev_profile-*`（后者 247 MB）。回收判据是「改名探针」而不是年龄：
    活的 Chromium 一直开着 profile 里的 lockfile，Windows 上那会让目录改不了名。
    刻意不碰 `playwright-download-*`——那是浏览器下载缓存，本机 CDN 拉不到 chromium。
    """
    temp_state.sweep_stale_orphans(tempfile.gettempdir(),
                                   ("docmind_ac_", "playwright_chromiumdev_profile-",
                                    "playwright-artifacts-"))
    return tempfile.mkdtemp(prefix="docmind_ac_")


def _launch_edge(context_dir: str) -> tuple[Any, Any]:
    """持久上下文：复用系统 Edge（channel='msedge'），保 cookie/login 跨人工步与崩溃。

    playwright 惰性 import；必须用 launch_persistent_context(user_data_dir=...)，
    browser.new_context(user_data_dir=...) 不是合法 API。返回 (playwright, context)。
    """
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=context_dir,
            channel="msedge",
            headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
    except Exception:
        try:
            pw.stop()
        except Exception:
            pass
        raise
    return pw, context


def _stop_session_objects(sess: Optional[dict[str, Any]]) -> None:
    if not sess:
        return
    ctx = sess.get("context")
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass
    pw = sess.get("pw")
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def _close_browser_session_locked(task_id: str) -> None:
    """已在 executor 线程上时调用：pop 并关闭 live context（触 playwright 对象）。"""
    with _BROWSER_SESSIONS_LOCK:
        sess = _BROWSER_SESSIONS.pop(task_id, None)
    _stop_session_objects(sess)


def _close_browser_session(task_id: str) -> None:
    """从任意线程安全关闭：把触 playwright 的动作转交单线程 executor 执行。"""
    try:
        _browser_call(_close_browser_session_locked, task_id)
    except Exception as exc:
        _log.warning("close_browser_session failed task=%s err=%s", task_id, type(exc).__name__)


def _detect_challenge(page: Any) -> str:
    """本地确定性挑战检测；命中即 L1 停-继续。页面正文只在本地比对，绝不回传模型。"""
    url = ""
    try:
        url = str(getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""
    text = ""
    try:
        text = str(page.inner_text("body") or "")[:40000].lower()
    except Exception:
        text = ""
    hay = url + "\n" + text
    for marker in CHALLENGE_URL_MARKERS:
        if marker in url:
            return "login"
    for kw in CHALLENGE_KEYWORDS:
        if kw in hay:
            return kw
    return ""


def _fill_and_submit(page: Any, adapter: dict[str, Any]) -> tuple[bool, str]:
    """按适配器数据的确定性动作序列填表并（仅当 auto_submit 为 True 时）提交。

    返回 (ok, 失败原因)。auto_submit 默认 False：创建长期令牌/PAT 等 §5 denylist 动作
    绝不自动点 submit，填到提交前停下，由用户点「创建/生成」后走 resume 捕获。
    """
    for spec in adapter.get("fields", []) or []:
        selector = str(spec.get("selector") or "")
        if not selector:
            continue
        action = spec.get("action", "fill")
        try:
            loc = page.locator(selector).first
            if action == "fill":
                loc.fill(str(spec.get("value", "")))
            elif action == "check":
                loc.check()
            elif action == "click":
                loc.click()
            elif action == "select":
                loc.select_option(str(spec.get("value", "")))
            else:
                return False, f"未知字段动作：{action}"
        except Exception as exc:
            return False, f"字段操作失败：{selector}（{type(exc).__name__}）"
    submit = str(adapter.get("submit") or "")
    if submit and adapter.get("auto_submit") is True:
        try:
            page.locator(submit).first.click()
        except Exception as exc:
            return False, f"提交失败（{type(exc).__name__}）"
        try:
            page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
    return True, ""


def _submit_pending(adapter: dict[str, Any]) -> bool:
    """是否需要用户手动点提交：有 submit 选择器且未授权自动提交（§5 denylist 保护）。"""
    return bool(str(adapter.get("submit") or "")) and adapter.get("auto_submit") is not True


def _capture_credential(page: Any, patterns: list[str], token_pattern: str = "") -> str:
    """由白名单 DOM 选择器只读凭证。绝不 eval 页面脚本、绝不回传模型、绝不落日志。"""
    pat = re.compile(token_pattern) if token_pattern else None
    for selector in patterns or []:
        value = ""
        try:
            loc = page.locator(selector).first
            try:
                value = str(loc.input_value() or "")
            except Exception:
                value = ""
            if not value:
                value = str(loc.inner_text() or "")
        except Exception:
            value = ""
        value = value.strip()
        if not value:
            continue
        if pat:
            m = pat.search(value)
            if m:
                return m.group(0)
        elif _looks_like_secret(value):
            return value
    return ""


# ---------------------------------------------------------------- 会话持久化（写/读/增量更新）

def _sessions_path() -> str:
    """会话文件落 project_state（.docmind_autoconnect_sessions.json）。

    root 传 os.getcwd()：会话文件只按运行时单用户桌面落盘，API status 端点读同一路径。
    """
    return project_state.path(os.getcwd(), "autoconnect_sessions.json",
                              legacy=".docmind_autoconnect_sessions.json")


def _read_sessions_file(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_sessions_file(path: str, data: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _persist_session(task_id: str, tier: str, url: str, context_dir: str, status: str = "running",
                     *, provider: str = "", user_prompt: str = "", step: str = "",
                     root: str = "") -> dict[str, Any]:
    """落会话（含 provider/user_prompt/step）。凭证明文绝不进本文件。"""
    path = _sessions_path()
    data = _read_sessions_file(path)
    record = {
        "tier": tier, "url": url, "context_dir": context_dir,
        "resume_token": task_id, "created_at": time.time(), "status": status,
        "provider": provider, "user_prompt": user_prompt, "step": step, "root": root,
    }
    data[task_id] = record
    _write_sessions_file(path, data)
    return record


def _load_session(task_id: str) -> dict[str, Any]:
    return _read_sessions_file(_sessions_path()).get(task_id, {}) or {}


def _update_session(task_id: str, **fields: Any) -> dict[str, Any]:
    path = _sessions_path()
    data = _read_sessions_file(path)
    current = dict(data.get(task_id, {}) or {})
    current.update(fields)
    data[task_id] = current
    _write_sessions_file(path, data)
    return current


def _session_view(task_id: str, sess: dict[str, Any], *, ok: bool = True, **extra: Any) -> dict[str, Any]:
    view: dict[str, Any] = {
        "ok": ok, "task_id": task_id,
        "tier": sess.get("tier", "L2"), "status": sess.get("status", "waiting_user"),
        "url": sess.get("url", ""),
    }
    if sess.get("user_prompt"):
        view["user_prompt"] = sess["user_prompt"]
    if sess.get("step"):
        view["step"] = sess["step"]
    view.update(extra)
    return view


def _l2_result(note: str, url: str, *, provider: str = "",
               adapter: Optional[dict[str, Any]] = None, root: str = "") -> dict[str, Any]:
    """L2 降级：不起浏览器，登记会话并返回官方 URL，由前端弹 L2 引导。"""
    task_id = _new_task_id()
    context_dir = _new_context_dir()
    secret_provider = (adapter or {}).get("secret_provider", provider)
    _persist_session(task_id, "L2", url, context_dir, status="waiting_user",
                     provider=secret_provider, step="l2", root=root)
    _log.info("browser_register degraded tier=L2 task=%s provider=%s", task_id, secret_provider)
    return {"ok": True, "task_id": task_id, "tier": "L2", "url": url, "note": note}


def _capability_note() -> str:
    missing = []
    if not _edge_available():
        missing.append("Microsoft Edge")
    if not _playwright_available():
        missing.append("playwright")
    return "自动注册组件缺失（" + "、".join(missing) + "），已降级 L2 手动回填"


def _browser_run(task_id: str, context_dir: str, adapter: dict[str, Any],
                 register_url: str) -> dict[str, Any]:
    """在单线程 executor 上执行的浏览器操作段（launch→goto→挑战/填表/捕获）。

    返回纯数据结果字典（外层不持有任何 playwright 对象）：
      {'outcome': 'launch_error', 'reason'}
      {'outcome': 'l1', 'step': 'challenge'|'untrusted'|'fill'|'submit'|'capture', 'reason'}
      {'outcome': 'l0', 'step': 'captured', 'token'}   # token 仅内存传递，绝不落盘/日志
    本函数内部吞掉 launch 异常返回 launch_error，故 future 不抛，executor 不被卡死。
    """
    pw: Any = None
    context: Any = None
    try:
        pw, context = _launch_edge(context_dir)
        page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
    except Exception as exc:
        # N5：page 获取并入 launch 的 try；失败先关掉已拉起的 pw/context，避免对象泄漏
        _stop_session_objects({"pw": pw, "context": context})
        return {"outcome": "launch_error", "reason": type(exc).__name__}
    with _BROWSER_SESSIONS_LOCK:
        _BROWSER_SESSIONS[task_id] = {"pw": pw, "context": context, "page": page,
                                      "context_dir": context_dir, "adapter": adapter}
    try:
        page.goto(register_url, wait_until="domcontentloaded")
    except Exception:
        pass  # 导航异常不致命：仍由挑战检测/填表结果决定停在哪一档

    challenge = _detect_challenge(page)
    if challenge:
        return {"outcome": "l1", "step": "challenge", "reason": challenge}
    # N1：fill 前复核当前页仍在可信域（防跨域重定向后填凭证）
    if not _provider_url_trusted(str(getattr(page, "url", "") or ""), adapter):
        return {"outcome": "l1", "step": "untrusted", "reason": ""}
    filled, reason = _fill_and_submit(page, adapter)
    if not filled:
        return {"outcome": "l1", "step": "fill", "reason": reason}
    if _submit_pending(adapter):
        # B1：创建长期令牌类动作绝不自动提交 → 填到提交前停下走 L1，用户点后 resume 捕获
        return {"outcome": "l1", "step": "submit", "reason": ""}
    # N1：capture 前再复核一次可信域
    if not _provider_url_trusted(str(getattr(page, "url", "") or ""), adapter):
        return {"outcome": "l1", "step": "untrusted", "reason": ""}
    token = _capture_credential(page, adapter.get("token_selectors", []),
                                adapter.get("token_pattern", ""))
    if token:
        _close_browser_session_locked(task_id)  # 已在 executor 线程，直接关
        return {"outcome": "l0", "step": "captured", "token": token}
    return {"outcome": "l1", "step": "capture", "reason": ""}


# ---------------------------------------------------------------- 注册代管入口（drop-in）

def browser_register(root: str, key: str, cand: dict[str, Any], provider: str) -> dict[str, Any]:
    """L0/L1/L2 降级链入口（签名与返回键 {ok,task_id,tier,url,note} 不变）。

    L0：单页无验证 → 确定性填表 + 提交 + 白名单选择器捕获凭证 → secrets_store → done。
    L1：命中挑战（验证码 / 邮箱验证 / 2FA / 支付墙）或填表未完成 → 浏览器保活，status=waiting_user。
    L2：provider 未收录 / 首版不自动 / Edge 或 playwright 缺失 / 主机越域 → 不起浏览器，返 URL+note。

    安全不变量：抽取层不 subprocess；真实执行仅 probe_candidate 与 confirm→save_server；
    凭证只走 secrets_store；页面正文绝不喂模型；导航限 provider 可信域。
    """
    provenance = cand.get("provenance") or {}
    fallback_url = provenance.get("url") or cand.get("url") or ""
    adapter = select_provider_adapter(provider, cand)
    if adapter is None:
        # 未收录 provider：降级 L2 手动回填。注意 url 必须为空字符串——不得用
        # provenance.url/cand.url 冒充「去官网创建凭证」链接（provenance 是来源仓库，
        # 不代表凭证签发方；否则会像本次 bug 那样把 GitHub 仓库页当成凭证创建页）。
        return _l2_result(
            "未收录该 provider 的自动注册流程，已降级 L2 手动回填。"
            "请在对应服务官网的「Developer / API Keys」设置页创建凭证后回填。",
            "", provider=provider, root=root)
    if adapter.get("tier") == "L2":
        return _l2_result(adapter.get("note") or "该 provider 首版不做自动注册，已降级 L2",
                          adapter.get("register_url") or fallback_url,
                          provider=provider, adapter=adapter, root=root)

    register_url = adapter.get("deep_link") or adapter.get("register_url") or fallback_url
    if not _provider_url_trusted(register_url, adapter):
        return _l2_result("注册页主机不在 provider 可信域，已降级 L2 手动回填",
                          fallback_url, provider=provider, adapter=adapter, root=root)
    if not (_edge_available() and _playwright_available()):
        return _l2_result(_capability_note(), register_url,
                          provider=provider, adapter=adapter, root=root)

    task_id = _new_task_id()
    context_dir = _new_context_dir()
    secret_provider = adapter.get("secret_provider", provider)
    user_prompt = adapter.get("user_prompt", DEFAULT_USER_PROMPT)

    # 浏览器操作段提交单线程 executor（保证 playwright 对象创建与使用同线程）。
    try:
        outcome = _browser_call(_browser_run, task_id, context_dir, adapter, register_url)
    except Exception as exc:
        _log.warning("browser_run failed task=%s err=%s", task_id, type(exc).__name__)
        return _l2_result("浏览器操作异常（" + type(exc).__name__ + "），已降级 L2 手动回填",
                          register_url, provider=provider, adapter=adapter, root=root)

    if outcome.get("outcome") == "launch_error":
        _log.warning("launch_edge failed task=%s err=%s", task_id, outcome.get("reason"))
        return _l2_result("无法启动系统 Edge（" + str(outcome.get("reason")) + "），已降级 L2 手动回填",
                          register_url, provider=provider, adapter=adapter, root=root)

    if outcome.get("outcome") == "l0":
        secrets_store.save(root, secret_provider, outcome.get("token", ""))
        _persist_session(task_id, "L0", register_url, context_dir, status="done",
                         provider=secret_provider, step="captured", root=root)
        _log.info("browser_register tier=L0 task=%s provider=%s captured=1", task_id, secret_provider)
        return {"ok": True, "task_id": task_id, "tier": "L0", "url": register_url,
                "note": "已捕获凭证并写入 secrets_store"}

    step = outcome.get("step", "capture")
    reason = outcome.get("reason", "")
    _persist_session(task_id, "L1", register_url, context_dir, status="waiting_user",
                     provider=secret_provider, user_prompt=user_prompt, step=step, root=root)
    _log.info("browser_register tier=L1 task=%s step=%s", task_id, step)
    note = {
        "challenge": "命中人工验证（" + reason + "），请在浏览器中完成后点继续",
        "untrusted": "检测到注册页跳转到 provider 可信域之外，已停止自动填写与捕获，请在浏览器中核对后点继续",
        "fill": "自动填写未完成（" + reason + "），请手动处理后点继续",
        "submit": "已自动填表到提交前（创建令牌不可自动执行），请在浏览器中点「创建/生成」后回来点继续",
    }.get(step, "已提交但未在页面捕获到凭证，请在浏览器中复制后点继续")
    return {"ok": True, "task_id": task_id, "tier": "L1", "url": register_url, "note": note}


def _resume_page(task_id: str, sess: dict[str, Any], adapter: dict[str, Any], url: str) -> Optional[Any]:
    """仅由 executor 线程调用：优先复用 live context；否则按 context_dir 重开持久上下文。

    复用安全的前提：所有浏览器操作都提交到同一单线程 executor，live page 创建与使用同线程。
    """
    with _BROWSER_SESSIONS_LOCK:
        live = _BROWSER_SESSIONS.get(task_id)
    if live and live.get("page") is not None:
        return live["page"]
    context_dir = sess.get("context_dir") or ""
    if not (context_dir and os.path.isdir(context_dir)
            and _edge_available() and _playwright_available()):
        return None
    pw: Any = None
    context: Any = None
    try:
        pw, context = _launch_edge(context_dir)
        page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
    except Exception as exc:
        # N5：page 获取并入 try；失败先关已拉起的 pw/context，避免对象泄漏
        _stop_session_objects({"pw": pw, "context": context})
        _log.warning("resume relaunch failed task=%s err=%s", task_id, type(exc).__name__)
        return None
    with _BROWSER_SESSIONS_LOCK:
        _BROWSER_SESSIONS[task_id] = {"pw": pw, "context": context, "page": page,
                                      "context_dir": context_dir, "adapter": adapter}
    if url:
        try:
            page.goto(url, wait_until="domcontentloaded")
        except Exception:
            pass
    return page


def _browser_resume_run(task_id: str, sess: dict[str, Any], adapter: dict[str, Any],
                        url: str) -> dict[str, Any]:
    """executor 线程上的 resume 浏览器操作段。返回纯数据结果字典（不持有 playwright 对象）。

    resume 语义：用户已在浏览器中完成人工步（过验证码 / 点了「创建/生成」）后点「继续」。
    故 **先尝试捕获**；捕获不到才回落到更具体的停点，绝不因 re-fill 失败就放弃捕获。
    """
    page = _resume_page(task_id, sess, adapter, url)
    if page is None:
        return {"outcome": "no_session"}
    challenge = _detect_challenge(page)
    if challenge:
        return {"outcome": "l1", "step": "challenge", "reason": challenge}
    # N1：fill 前复核可信域（防跨域重定向）
    if not _provider_url_trusted(str(getattr(page, "url", "") or ""), adapter):
        return {"outcome": "l1", "step": "untrusted", "reason": ""}
    filled, reason = _fill_and_submit(page, adapter)
    # N1：capture 前再复核一次可信域
    if not _provider_url_trusted(str(getattr(page, "url", "") or ""), adapter):
        return {"outcome": "l1", "step": "untrusted", "reason": ""}
    token = _capture_credential(page, adapter.get("token_selectors", []),
                                adapter.get("token_pattern", ""))
    if not token:
        if not filled:
            return {"outcome": "l1", "step": "fill", "reason": reason}
        if _submit_pending(adapter):
            return {"outcome": "l1", "step": "submit", "reason": ""}
        return {"outcome": "l1", "step": "capture", "reason": ""}
    _close_browser_session_locked(task_id)  # 已在 executor 线程，直接关
    return {"outcome": "l0", "step": "captured", "token": token}


def register_resume(task_id: str) -> dict[str, Any]:
    """L1 用户点「继续」后续跑：复用 live / 按 context_dir 重开 → 回 L0 判定。

    返回 {ok, task_id, tier, status, user_prompt?, url?, step?, error?}。
    浏览器操作段提交单线程 executor；会话落盘与 secrets_store 留在调用线程。
    """
    sess = _load_session(task_id)
    if not sess:
        return {"ok": False, "task_id": task_id, "tier": "L2", "status": "unknown",
                "error": "会话不存在或已过期"}
    tier = sess.get("tier", "L2")
    adapter = select_provider_adapter(sess.get("provider", "")) or _adapter_by_url(sess.get("url", ""))
    if tier == "L2" or adapter is None or adapter.get("tier") == "L2":
        return _session_view(task_id, sess, ok=False, error="该会话为 L2 手动回填，无自动续跑")

    root = sess.get("root", "")
    url = sess.get("url") or adapter.get("register_url") or ""
    secret_provider = adapter.get("secret_provider", sess.get("provider", ""))
    user_prompt = adapter.get("user_prompt", DEFAULT_USER_PROMPT)
    try:
        outcome = _browser_call(_browser_resume_run, task_id, sess, adapter, url)
    except Exception as exc:
        _log.warning("browser_resume failed task=%s err=%s", task_id, type(exc).__name__)
        return _session_view(task_id, sess, ok=False,
                             error="浏览器续跑异常（" + type(exc).__name__ + "）")

    if outcome.get("outcome") == "no_session":
        return _session_view(task_id, sess, ok=False, error="无法恢复浏览器会话，请重新开始")
    if outcome.get("outcome") == "l0":
        if not root:
            # N3：root 为空则无法落库，绝不置 done 造成假成功
            _log.warning("register_resume l0 without root task=%s", task_id)
            return _session_view(task_id, sess, ok=False,
                                 error="会话缺少项目根目录，无法写入 secrets_store")
        secrets_store.save(root, secret_provider, outcome.get("token", ""))
        _update_session(task_id, tier="L0", status="done", step="captured", user_prompt="")
        _log.info("register_resume tier=L0 task=%s provider=%s captured=1", task_id, secret_provider)
        return _session_view(task_id, _load_session(task_id))

    step = outcome.get("step", "capture")
    _update_session(task_id, tier="L1", status="waiting_user", step=step, user_prompt=user_prompt)
    _log.info("register_resume tier=L1 task=%s step=%s", task_id, step)
    return _session_view(task_id, _load_session(task_id))


def register_commit(root: str, task_id: str, credentials: dict[str, Any]) -> dict[str, Any]:
    """用户回填 / L1 捕获的凭证 → secrets_store.save。返回 {ok, stored:[provider...]}。

    契约：
      - 有凭证：逐个 save，stored 含 provider 名（无明文）；会话置 done；重复 commit 幂等。
      - 空 credentials：回读会话是否已有本轮捕获的凭证（step=captured 或 done+provider）——
        有则返 stored=[provider] 并把会话置 done；无则 ok=False + 明确 error（不静默成功）。
    stored 只含 provider 名，绝不含明文；凭证绝不写会话 JSON、不写日志。
    """
    stored: list[str] = []
    for provider, value in (credentials or {}).items():
        if not str(value or "").strip():
            continue
        secrets_store.save(root, provider, value)
        stored.append(str(provider))

    sess = _load_session(task_id) if task_id else {}

    if stored:
        if task_id and sess:
            _update_session(task_id, status="done", step="commit", user_prompt="")
            _close_browser_session(task_id)
        _log.info("register_commit task=%s stored_count=%d", task_id, len(stored))
        return {"ok": True, "stored": stored}

    captured_provider = ""
    if sess and (sess.get("step") == "captured"
                 or (sess.get("status") == "done" and sess.get("provider"))):
        captured_provider = str(sess.get("provider") or "")
    if captured_provider:
        if sess.get("status") != "done":
            _update_session(task_id, status="done", step="captured", user_prompt="")
            _close_browser_session(task_id)
        _log.info("register_commit task=%s reused_captured provider=%s", task_id, captured_provider)
        return {"ok": True, "stored": [captured_provider]}

    _log.info("register_commit task=%s empty_credentials no_captured", task_id)
    return {"ok": False, "stored": [],
            "error": "未提供凭证，且该会话没有已捕获的凭证（请先在注册流程中创建或回填）"}
