"""MCP 官方 Registry 客户端（自主发现主路径）。

数据源：`https://registry.modelcontextprotocol.io`（**无鉴权、只读** REST，preview）。
本模块以**纯函数**为主（URL 构造 / 响应解析 / 配置推导 / 排序）；
唯一的联网编排 `search_registry` 通过**注入的 `http_get`** 拉取，便于离线单测（不联网）。

不 import 任何项目内业务模块（仅标准库 + `textutil.as_text` 空依赖规整）→ 无循环导入。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from typing import Any, Callable, Optional

from textutil import as_text

__all__ = [
    "REGISTRY_BASE", "ALLOWED_LAUNCHERS", "LAUNCHER_BY_REGISTRY_TYPE",
    "registry_search_url", "parse_registry_response", "server_to_candidates",
    "rank_candidates", "search_registry", "RegistryCache",
]

REGISTRY_BASE = "https://registry.modelcontextprotocol.io"
REGISTRY_PATH = "/v0.1/servers"
DEFAULT_TTL = 86400          # 24h 缓存
DEFAULT_TIMEOUT = 8          # 联网超时上限（秒）
OFFICIAL_META_KEY = "io.modelcontextprotocol.registry/official"

# 启动器白名单：与 mcp_autoconnect.ALLOWED_LAUNCHERS（R3 信任闸门）**语义一致**。
# 此处独立声明而非 import（避免 registry ← autoconnect 反向依赖成环）；
# 单测 test_launcher_allowlist_parity 断言两者相等，防漂移。
ALLOWED_LAUNCHERS: frozenset[str] = frozenset({
    "uvx", "npx", "bunx", "bun", "node", "python", "python3",
    "pipx", "docker", "deno", "go", "poetry",
})

# registryType → 缺省启动器（无 runtimeHint 时回退）
LAUNCHER_BY_REGISTRY_TYPE: dict[str, str] = {
    "npm": "npx", "pypi": "uvx", "oci": "docker", "nuget": "dnx",
}

_UA = "Agent Workbench-MCP-Registry/1.0 (+https://registry.modelcontextprotocol.io)"
_URL_RE = re.compile(r"^https?://([^/]+)/?", re.I)


def _domain_of(url: str) -> str:
    m = _URL_RE.match(url or "")
    return m.group(1).lower() if m else ""


_EDA_QUERY_RE = re.compile(r"(?<![a-z0-9])(?:eda|pcb|kicad|altium|easyeda)(?![a-z0-9])|电路板|电路图|原理图|印制板", re.I)
_EDA_PRODUCT_RE = re.compile(r"kicad|altium|easyeda|(?<![a-z0-9])(?:eda|pcb|gerber|netlist|spice)(?![a-z0-9])", re.I)
_EDA_DESIGN_RE = re.compile(r"schematic", re.I)
_EDA_CONTEXT_RE = re.compile(r"circuit|electroni|electrical|hardware|pcb|board", re.I)


def is_eda_query(query: str) -> bool:
    """领域词需要语义过滤；Registry 的 search 也会返回 jeda 等子串误命中。"""
    return bool(_EDA_QUERY_RE.search(query or ""))


def is_eda_candidate(cfg: dict[str, Any]) -> bool:
    """只把有电路设计证据的 Registry 条目展示为 EDA 候选。"""
    prov = cfg.get("provenance") or {}
    info = " ".join(str(prov.get(k) or "") for k in ("server_name", "description", "url"))
    return bool(_EDA_PRODUCT_RE.search(info) or
                (_EDA_DESIGN_RE.search(info) and _EDA_CONTEXT_RE.search(info)))


# ---------------------------------------------------------------- 纯函数：URL / 解析

def registry_search_url(need: str, limit: int = 5) -> str:
    """构造检索 URL（纯函数，可测）。参数做 URL 编码。"""
    from urllib.parse import urlencode
    q = urlencode({"search": (need or "").strip(), "limit": int(limit)})
    return f"{REGISTRY_BASE}{REGISTRY_PATH}?{q}"


def parse_registry_response(payload: Any) -> list[dict[str, Any]]:
    """解析 Registry 响应 → 去重后的 server 列表（纯函数）。

    - 只留 `_meta[.../official].isLatest != False`；
    - 丢弃 `status` 存在且 != "active"（deprecated/deleted）；
    - 按 `server.name` 去重（同一 server 只保留一条）。
    """
    if isinstance(payload, dict):
        items = payload.get("servers") or payload.get("data") or payload.get("items") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        srv = it.get("server") if isinstance(it.get("server"), dict) else it
        meta = it.get("_meta") if isinstance(it.get("_meta"), dict) else {}
        official = meta.get(OFFICIAL_META_KEY) if isinstance(meta.get(OFFICIAL_META_KEY), dict) else {}
        if official.get("isLatest") is False:           # 多版本：只取 latest
            continue
        status = official.get("status")
        if status and status != "active":               # 跳过 deprecated / deleted
            continue
        name = srv.get("name") or ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(srv)
    return out


# ---------------------------------------------------------------- 纯函数：配置推导

def _arg_values(arg: Any) -> list[str]:
    """把一条 runtimeArguments/packageArguments 展开为 argv 片段。"""
    if isinstance(arg, str):
        return [arg] if arg else []
    if not isinstance(arg, dict):
        return []
    typ = str(arg.get("type") or "").lower()
    value = arg.get("value")
    name = arg.get("name")
    if typ in ("named", "option", "flag", "option-like") and name:
        if value is None or value == "":
            return [str(name)]
        return [str(name), str(value)]
    return [str(value)] if value not in (None, "") else []


def _provenance(server: dict[str, Any], repo_url: str) -> dict[str, Any]:
    name = server.get("name") or ""
    ns = name.split("/")[0] if "/" in name else name
    return {
        "url": repo_url,
        "domain": _domain_of(repo_url),
        "registry": True,
        "server_name": name,
        "namespace": ns,
        "description": str(server.get("description") or "")[:200],
    }


def server_to_candidates(server: dict[str, Any],
                         *, allowed_launchers: Optional[frozenset[str]] = ALLOWED_LAUNCHERS
                         ) -> list[dict[str, Any]]:
    """把一条 registry server 条目推导为候选 config 列表（纯函数）。

    `packages[]` → stdio；`remotes[]` → http。启动器不在 `allowed_launchers`
    则**降级跳过**该 package（不产出非法候选）。
    """
    if not isinstance(server, dict):
        return []
    repo_url = str((server.get("repository") or {}).get("url") or "") if isinstance(
        server.get("repository"), dict) else ""
    prov = _provenance(server, repo_url)
    out: list[dict[str, Any]] = []
    for pkg in server.get("packages") or []:
        cfg = _package_to_config(pkg, prov, allowed_launchers)
        if cfg:
            out.append(cfg)
    for rem in server.get("remotes") or []:
        cfg = _remote_to_config(rem, prov)
        if cfg:
            out.append(cfg)
    return out


def _package_to_config(pkg: Any, prov: dict[str, Any],
                       allowed_launchers: Optional[frozenset[str]]) -> Optional[dict[str, Any]]:
    if not isinstance(pkg, dict):
        return None
    identifier = str(pkg.get("identifier") or "").strip()
    if not identifier:
        return None
    rt = str(pkg.get("registryType") or "").lower()
    launcher = str(pkg.get("runtimeHint") or "").strip().lower() or LAUNCHER_BY_REGISTRY_TYPE.get(rt, "")
    if not launcher:
        return None
    if allowed_launchers is not None and launcher not in allowed_launchers:
        return None                                       # 白名单外 → 降级跳过
    args: list[str] = []
    for a in pkg.get("runtimeArguments") or []:
        args.extend(_arg_values(a))
    # stdio 没有交互终端；首次下载时 npx 的确认提示会挡住 MCP initialize。
    # 用户点「测试连接」即明确触发试连，命令仍在确认启用前临时运行。
    if launcher == "npx" and not any(a in ("-y", "--yes", "-n", "--no") for a in args):
        args.insert(0, "-y")
    args.append(identifier)
    for a in pkg.get("packageArguments") or []:
        args.extend(_arg_values(a))
    env: dict[str, str] = {}
    required: list[str] = []
    secret_specs: list[dict[str, Any]] = []
    for ev in pkg.get("environmentVariables") or []:
        if not isinstance(ev, dict) or not ev.get("name"):
            continue
        nm = str(ev["name"])
        env[nm] = f"@secret:{nm}" if ev.get("isSecret") else ""   # 密钥只留引用，绝无明文
        if ev.get("isSecret"):
            secret_specs.append({"name": nm, "required": bool(ev.get("isRequired"))})
        if ev.get("isRequired"):
            required.append(nm)
    p = dict(prov)
    if required:
        p["required_env"] = required
    if secret_specs:
        p["secret_specs"] = secret_specs
    return {
        "transport": "stdio", "command": launcher, "args": args, "url": "",
        "env": env, "headers": {}, "provenance": p, "command_unresolved": False,
    }


_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_.-]+)\}")


def _remote_header_value(header: dict[str, Any]) -> str:
    """remotes header 值推导（blueprint §2B 修正版），绝不落明文：

    1. 值含 `{placeholder}`（如 `Bearer {smithery_api_key}`）→ provider = **占位名**，
       并把占位符就地替换为 `@secret:<占位名>`，**保留模板前后缀**（`"Bearer @secret:smithery_api_key"`）；
    2. 无占位但 `isSecret` → 回退 `@secret:<header_name>`（与 _package_to_config 的 env 口径一致）；
    3. 否则原样返回 value。
    """
    value = str(header.get("value") or "")
    if _PLACEHOLDER_RE.search(value):
        return _PLACEHOLDER_RE.sub(lambda m: f"@secret:{m.group(1)}", value)
    if header.get("isSecret"):
        return f"@secret:{header['name']}"
    return value


def _remote_to_config(rem: Any, prov: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(rem, dict):
        return None
    url = str(rem.get("url") or "").strip()
    if not url:
        return None
    headers: dict[str, str] = {}
    secret_specs: list[dict[str, Any]] = []
    for h in rem.get("headers") or []:
        if not isinstance(h, dict) or not h.get("name"):
            continue
        name = str(h["name"])
        value = _remote_header_value(h)
        headers[name] = value
        providers = _PLACEHOLDER_RE.findall(str(h.get("value") or ""))
        if providers:
            secret_specs.extend({"name": p, "required": bool(h.get("isRequired"))}
                                for p in providers)
        elif h.get("isSecret"):
            secret_specs.append({"name": name, "required": bool(h.get("isRequired"))})
    p = dict(prov)
    if secret_specs:
        p["secret_specs"] = secret_specs
    return {
        "transport": "http", "command": "", "args": [], "url": url,
        "env": {}, "headers": headers, "provenance": p, "command_unresolved": False,
    }


# ---------------------------------------------------------------- 纯函数：排序 / 去重

def _is_official_namespace(ns: str) -> bool:
    return ns.startswith("io.modelcontextprotocol") or ns.startswith("io.github.")


def rank_candidates(cands: list[dict[str, Any]], need: str) -> list[dict[str, Any]]:
    """§3 排序 + 同 `repository.url` 去重（保留最高分一条）。纯函数。

    权重：有 packages（本地 stdio）优先 > 官方命名空间 > 描述相关度；`ai.smithery/*` 降权。
    """
    qtoks = set(re.findall(r"[a-z0-9]+", (need or "").lower()))
    best: dict[str, tuple[float, dict[str, Any]]] = {}
    for c in cands:
        prov = c.get("provenance") or {}
        ns = str(prov.get("namespace") or "")
        score = 0.0
        if prov.get("has_packages"):
            score += 1000.0
        if _is_official_namespace(ns):
            score += 500.0
        if ns.startswith("ai.smithery"):
            score -= 200.0
        text = (str(prov.get("server_name") or "") + " " + str(prov.get("description") or "")).lower()
        score += 10.0 * len(qtoks & set(re.findall(r"[a-z0-9]+", text)))
        key = str(prov.get("url") or "") or f"{prov.get('server_name')}|{c.get('command') or c.get('url')}"
        if key not in best or score > best[key][0]:
            best[key] = (score, c)
    return [c for _, c in sorted(best.values(), key=lambda x: x[0], reverse=True)]


# ---------------------------------------------------------------- 缓存（project_state 文件）

class RegistryCache:
    """`<STATE_ROOT>` 下的 JSON 文件缓存：`{need: {fetched_at, servers:[…]}}`。

    读写全程 try 兜底：缓存损坏/无权限一律当作未命中，绝不上抛。
    """

    def __init__(self, path: str, ttl: int = DEFAULT_TTL):
        self.path = path
        self.ttl = ttl
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        entry = self._load().get(key)
        return entry if isinstance(entry, dict) else None

    def set(self, key: str, servers: list[dict[str, Any]], *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            data[key] = {"fetched_at": now, "servers": servers}
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except Exception:
                pass


def _fresh(fetched_at: Any, now: float, ttl: int) -> bool:
    try:
        return (now - float(fetched_at)) < ttl
    except (TypeError, ValueError):
        return False


def _default_http_get(url: str) -> str:
    """标准库 urllib GET，超时 ≤8s，返回响应体文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:   # noqa: S310 (受信固定 host)
        return resp.read().decode("utf-8", "replace")


def _build_candidates(servers: list[dict[str, Any]], need: str, limit: int) -> list[dict[str, Any]]:
    """servers → 候选 config：每个 server 打 `has_packages` 标 → 排序 → top N。"""
    flat: list[dict[str, Any]] = []
    for s in servers:
        has_pkg = bool(isinstance(s, dict) and (s.get("packages") or []))
        for cfg in server_to_candidates(s):
            cfg["provenance"]["has_packages"] = has_pkg
            flat.append(cfg)
    return rank_candidates(flat, need)[: max(int(limit), 0)]


def search_registry(need: str, *, limit: int = 5,
                    http_get: Optional[Callable[[str], Any]] = None,
                    cache: Optional["RegistryCache"] = None,
                    ttl: int = DEFAULT_TTL,
                    now: Optional[float] = None,
                    attempts: int = 2, backoff: float = 0.5,
                    sleep_fn: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """编排：缓存 → Registry → 过期缓存兜底。**任何异常都不上抛**。

    返回 `{ok, candidates, source, error}`；source ∈ registry/cache/cache-stale/none。

    Registry preview 主机间歇性抖动（实测出现过 14s 收到 0 字节的读超时），故对
    网络 getter 做**有界重试**：仅对异常重试、成功即返回绝不重试、两次尝试间
    `sleep_fn(backoff)`；重试用尽仍失败才走原 except 分支（保持软降级语义）。
    `attempts/backoff/sleep_fn` 为带默认值的关键字参数，向后兼容既有调用。
    """
    need = (need or "").strip()
    if not need:
        return {"ok": False, "candidates": [], "source": "none", "error": "需求为空"}
    now = time.time() if now is None else now
    entry = cache.get(need) if cache is not None else None
    if entry and _fresh(entry.get("fetched_at"), now, ttl):
        cands = _build_candidates(entry.get("servers") or [], need, limit)
        return {"ok": bool(cands), "candidates": cands, "source": "cache", "error": ""}
    try:
        getter = http_get or _default_http_get
        url = registry_search_url(need, limit)
        n = max(int(attempts), 1)
        last_exc: Optional[BaseException] = None
        body: Any = None
        for attempt in range(n):                  # 仅对 getter 异常做有界重试
            try:
                body = getter(url)
                break
            except Exception as exc:  # noqa: BLE001 — 瞬时抖动，重试一次即可
                last_exc = exc
                if attempt + 1 < n:
                    sleep_fn(backoff)
        else:
            # 全部尝试失败：原样上抛，交由下方 except 统一软降级（含过期缓存兜底）
            raise last_exc if last_exc is not None else RuntimeError("registry getter failed")
        payload = body if isinstance(body, (dict, list)) else json.loads(as_text(body) or "{}")
        servers = parse_registry_response(payload)
        if cache is not None:
            cache.set(need, servers, now=now)
        cands = _build_candidates(servers, need, limit)
        return {"ok": bool(cands), "candidates": cands, "source": "registry", "error": ""}
    except Exception as exc:  # noqa: BLE001 — Registry preview 常抖动，一律软降级
        err = f"{type(exc).__name__}: {exc}"[:300]
        if entry:                                        # 过期缓存兜底
            cands = _build_candidates(entry.get("servers") or [], need, limit)
            if cands:
                return {"ok": True, "candidates": cands, "source": "cache-stale", "error": err}
        return {"ok": False, "candidates": [], "source": "none", "error": err}
