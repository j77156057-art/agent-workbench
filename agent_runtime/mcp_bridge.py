"""把已批准连接器的 MCP 工具投影成一等 TOOLS 条目。

原本 Agent 工作台 调外部 MCP 工具只有一条间接路：`dev_mcp_call(server, tool, json)`。模型看不
见那些工具的真实名字与 schema，只能靠 `dev_list_connector_tools` 先查再猜参数，连接器一多
选择质量就掉。这里在 Agent 实例构造时，把操作者在连接器配置里显式打了
`inline_tools: true` 的服务的工具挂进 `self.tools`，名字形如 `mcp__<server>__<tool>`。

边界（都是有意为之）：

* 工具权威仍然绑在实例上、来自操作者配置，而不是来自 prompt——prompt 说什么都加不出工具。
* 只有 `enabled` 且 `inline_tools` 为真的服务才会被投影；默认没有任何行为变化。
* 单个服务最多 PER_SERVER 条、总共最多 TOTAL 条，超了就退回 `dev_mcp_call`
  ——无限膨胀的 prompt 比没有工具更糟。
* 调用**不做跨连接器回退**：`call_tool_with_fallback` 可能把一次有副作用的调用改投到
  另一个连接器上重复执行。宁可失败报告，也不要重复副作用。
* 保守的能力标记：capability=NETWORK、side_effect=MUTATING、parallel_safe=False——
  连接器可能改外部世界，不能假设它幂等；HTTP 传输的还标成 `group="web"`，让「联网开关」
  真的能把它一起关掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Callable

PER_SERVER = 12
TOTAL = 24
DESC_LIMIT = 700
NAME_LIMIT = 64
CACHE_TTL = 300.0

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_TRUTHY = {"1", "true", "yes", "y", "on", "是"}
_FALSY = {"0", "false", "no", "off", "否"}
#: root -> (signature, expires_at, entries, skipped, notes)
_CACHE: dict[str, tuple[str, float, list, list, list]] = {}


def reset_cache() -> None:
    _CACHE.clear()


def _enabled_flag(cfg: dict[str, Any]) -> bool:
    """`enabled` 缺省为真（与 mcp_client._require_enabled 一致）。"""
    value = cfg.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "否")
    return bool(value)


def inline_requested(cfg: dict[str, Any]) -> bool:
    raw = cfg.get("inline_tools", False)
    if isinstance(raw, str):
        return raw.strip().lower() in _TRUTHY
    return bool(raw)


def _safe_name(server: str, tool: str) -> str:
    base = "mcp__%s__%s" % (_SAFE.sub("_", str(server).strip()),
                            _SAFE.sub("_", str(tool).strip()))
    if len(base) <= NAME_LIMIT:
        return base
    digest = hashlib.sha1(("%s|%s" % (server, tool)).encode("utf-8")).hexdigest()[:8]
    return base[:NAME_LIMIT - 10].rstrip("_-") + "_" + digest


def _single_string_field(schema: dict[str, Any]) -> str:
    """只有一个字符串字段时，裸文本就直接当那个字段用。

    必填与否都要认：harness 自己的 MCP server 把 `input` 声明成可选（空参数有语义），
    只认必填会把「dev_glob *.vue」这种自然调用判成参数错误。
    """
    props = (schema or {}).get("properties") or {}
    if not isinstance(props, dict) or len(props) != 1:
        return ""
    ((key, meta),) = props.items()
    if isinstance(meta, dict) and str(meta.get("type", "string")) == "string":
        return str(key)
    return ""


def _schema_hint(schema: dict[str, Any]) -> str:
    props = (schema or {}).get("properties") or {}
    lone = _single_string_field(schema)
    tail = ("只有一个字符串字段 %s 时，也可以直接给那个值本身。" % lone if lone
            else "入参必须是 JSON 对象。")
    if not isinstance(props, dict) or not props:
        return "入参为 JSON 对象字符串（该工具声明不需要参数时可留空）。" + tail
    required = set((schema or {}).get("required") or [])
    lines = []
    for key, meta in list(props.items())[:12]:
        kind = (meta or {}).get("type", "any") if isinstance(meta, dict) else "any"
        mark = "必填" if key in required else "可选"
        lines.append("%s(%s,%s)" % (key, kind, mark))
    return ("入参为 JSON 对象字符串，字段：" + "、".join(lines) + "。" + tail)


def _make_call(server: str, tool: str, schema: dict[str, Any],
               root: str = "") -> Callable[[str], str]:
    """生成 `func(input: str) -> str`，把文本入参翻成 MCP 调用。

    root 在投影时就绑定，不在调用时重新猜——换项目/换会话都不该改变一个已注册工具
    作用在哪个代码库上。
    """
    lone = _single_string_field(schema)

    def call(arg: str = "") -> str:
        import mcp_client

        text = str(arg or "").strip()
        arguments: dict[str, Any] = {}
        if text:
            try:
                parsed = json.loads(text)
            except ValueError:
                if lone:
                    arguments = {lone: text}
                else:
                    return ("参数不是 JSON 对象，而该工具无法从裸文本推断字段；"
                            "请按下面的说明给 JSON：%s" % _schema_hint(schema))
            else:
                if isinstance(parsed, dict):
                    arguments = parsed
                elif lone:
                    arguments = {lone: text}
                else:
                    return "参数必须是 JSON 对象。%s" % _schema_hint(schema)
        target_root = root or _root()
        try:
            rep = mcp_client.call_tool(target_root, server, tool, arguments,
                                       timeout=_call_timeout())
        except Exception as exc:  # noqa: BLE001 - MCPError 等多种
            return "MCP 调用失败（%s/%s）：%s" % (server, tool, str(exc)[:400])
        if isinstance(rep, dict):
            if rep.get("ok") is False:
                return "MCP 返回失败：%s" % str(rep.get("error") or rep)[:600]
            body = None
            for key in ("text", "content", "result"):
                if rep.get(key) not in (None, "", {}, []):
                    body = rep[key]
                    break
            if body is None:
                rest = {key: value for key, value in rep.items()
                        if key != "ok" and value not in (None, "", {}, [])}
                if not rest:
                    return "（MCP 返回空内容）"
                body = rest
            text_out = body if isinstance(body, str) else json.dumps(
                body, ensure_ascii=False, default=str)
        else:
            text_out = str(rep)
        return text_out[:12000] or "（MCP 返回空内容）"

    call.__name__ = "mcp_call"
    return call


def _root() -> str:
    import config
    import tools as tools_module

    return str(config.get_runtime("code_root") or tools_module.CODE_ROOT or "").strip()


def _call_timeout() -> int:
    raw = os.getenv("DOCMIND_MCP_INLINE_TIMEOUT", "")
    try:
        return max(5, min(int(raw), 300)) if raw else 30
    except (TypeError, ValueError):
        return 30


def discover(root: str, *, limit: int = TOTAL, refresh: bool = False) -> dict[str, Any]:
    """收集可内联的连接器工具（带 TTL 缓存）。

    `list_tools` 是一次真正的握手，每次构造 Agent 都去问一遍会把聊天回合格搞慢，所以
    结果按 (root, 配置指纹) 缓存；改了连接器配置会在缓存过期后生效，`refresh=True` 立刻生效。
    失败的服务只记录原因，绝不让 Agent 构造炸掉。
    """
    if not inline_switch_on():
        return {"ok": True, "entries": [], "skipped": [], "notes": ["内联投影已关闭（DOCMIND_MCP_INLINE=0）"]}
    signature = _signature(root)
    if signature is None:
        return {"ok": True, "entries": [], "skipped": [], "notes": []}
    cached = _CACHE.get(root)
    if cached and not refresh and cached[0] == signature and cached[1] > time.monotonic():
        return dict(cached[2])
    out = _discover_uncached(root, limit=limit)
    _CACHE[root] = (signature, time.monotonic() + _ttl(), dict(out))
    return out


def _ttl() -> float:
    raw = os.getenv("DOCMIND_MCP_INLINE_TTL", "")
    try:
        return max(0.0, min(float(raw), 3600.0)) if raw else 300.0
    except (TypeError, ValueError):
        return 300.0


def inline_switch_on() -> bool:
    return str(os.getenv("DOCMIND_MCP_INLINE", "1")).strip().lower() not in _FALSY


def _signature(root: str) -> str | None:
    """只指纹化「会影响内联结果」的配置：谁打了 inline_tools、是否启用。"""
    try:
        import mcp_client
        servers = mcp_client.server_configs(root) or {}
    except Exception:  # noqa: BLE001
        return None
    rows = []
    items = servers.items() if isinstance(servers, dict) else [
        (str(row.get("key") or ""), row) for row in (servers or []) if isinstance(row, dict)]
    for key, cfg in items:
        cfg = cfg if isinstance(cfg, dict) else {}
        if not inline_requested(cfg):
            continue
        rows.append("%s:%s:%s:%s" % (key, _enabled_flag(cfg), cfg.get("transport") or "stdio",
                                     bool(_truthy_flag(cfg.get("inline_read_only")))))
    if not rows:
        return None
    return hashlib.sha1("|".join(sorted(rows)).encode("utf-8")).hexdigest()


def _discover_uncached(root: str, *, limit: int = TOTAL) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "entries": [], "skipped": [], "notes": []}
    try:
        import mcp_client
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["skipped"].append({"server": "*", "reason": "mcp_client 不可用：%s" % exc})
        return out
    try:
        servers = mcp_client.server_configs(root) or {}
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["skipped"].append({"server": "*", "reason": "读连接器配置失败：%s" % exc})
        return out
    if isinstance(servers, dict):
        items = list(servers.items())
    else:                                  # 兼容 [{key,...}] 形状
        rows = [row for row in (servers or []) if isinstance(row, dict)]
        items = [(str(row.get("key") or ""), row) for row in rows if row.get("key")]
    for key, cfg in items:
        cfg = cfg if isinstance(cfg, dict) else {}
        if not _enabled_flag(cfg):
            out["skipped"].append({"server": key, "reason": "未启用"})
            continue
        if not inline_requested(cfg):
            continue                        # 没打内联标记：静默跳过，不是问题
        if len(out["entries"]) >= limit:
            out["skipped"].append({"server": key, "reason": "已达内联上限 %d" % limit})
            continue
        try:
            listed = mcp_client.list_tools(root, key)
        except Exception as exc:  # noqa: BLE001 - 服务没起来是常态
            out["skipped"].append({"server": key, "reason": "列工具失败：%s" % str(exc)[:160]})
            continue
        rows = (listed or {}).get("tools") or []
        taken = 0
        for row in rows:
            if taken >= PER_SERVER or len(out["entries"]) >= limit:
                if taken >= PER_SERVER:
                    out["notes"].append("%s 只内联前 %d 个工具，其余用 dev_list_connector_tools "
                                        "+ dev_mcp_call 调。" % (key, PER_SERVER))
                break
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            schema = row.get("input_schema") if isinstance(row.get("input_schema"), dict) else {}
            out["entries"].append({
                "name": _safe_name(key, name),
                "server": key,
                "tool": name,
                "description": "%s｜MCP 连接器 %s（transport=%s）。%s" % (
                    str(row.get("description") or "").strip()[:DESC_LIMIT], key,
                    str(cfg.get("transport") or "stdio"), _schema_hint(schema)),
                "read_only": bool(_truthy_flag(cfg.get("inline_read_only"))),
                "schema": schema,
                "root": root,
                "http": str(cfg.get("transport") or "").lower() not in ("stdio", "", "local"),
            })
            taken += 1
    return out


def _truthy_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


def build_specs(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """把 discover 的条目变成 ToolSpec 可用的注册项。"""
    from agent_runtime.tools import Capability, SideEffect, ToolSpec

    specs: dict[str, Any] = {}
    for entry in entries:
        group = "web" if entry.get("http") else "mcp"
        capability = Capability.NETWORK
        specs[entry["name"]] = ToolSpec(
            name=entry["name"],
            description=entry["description"],
            func=_make_call(entry["server"], entry["tool"],
                            entry.get("schema") or {}, entry.get("root") or ""),
            input_schema={"type": "object", "properties": {
                "input": {"type": "string", "description": "MCP 工具入参（JSON 对象字符串）"}},
                "required": ["input"]},
            capability=capability,
            side_effect=(SideEffect.PURE if entry.get("read_only") else SideEffect.MUTATING),
            parallel_safe=bool(entry.get("read_only")),
            applications=frozenset({"developer"}),
            group=group,
            verbatim=False,
        )
    return specs


def attach(tools: dict[str, Any], root: str, *, application_id: str = "developer",
           limit: int = TOTAL) -> dict[str, Any]:
    """把内联工具加进某个 Agent 实例的工具表。返回统计，供日志/测试断言。"""
    if application_id != "developer" or not root:
        return {"added": [], "skipped": [], "notes": []}
    found = discover(root, limit=limit)
    for entry in found["entries"]:
        name = entry["name"]
        if name in tools:
            continue
        specs = build_specs([entry])
        if name in specs:
            tools[name] = specs[name]
    return {"added": [entry["name"] for entry in found["entries"]],
            "skipped": found["skipped"], "notes": found["notes"]}


def status(root: str) -> dict[str, Any]:
    """给人/Agent 看的一眼：哪些连接器打了内联标记、实际能内联几条。"""
    found = discover(root)
    return {"ok": True, "inline": len(found["entries"]),
            "tools": [entry["name"] for entry in found["entries"]],
            "skipped": found["skipped"], "notes": found["notes"],
            "cap": {"per_server": PER_SERVER, "total": TOTAL}}
