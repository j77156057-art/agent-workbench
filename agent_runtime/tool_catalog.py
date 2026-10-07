"""把工具目录按「大类 → 小类」两级渲染，并提供下钻查询。

## 要解决的问题（实测数字）

`tools.TOOLS` 有 **111 个工具**，它们的 `description` 合计 **28773 字符
≈ 7193 tokens**（chars/4，与 opencode `codemode` 同一启发式），每次会话都要付。
而长度极不均匀：最短 25 字符，最长 `dev_vue` 1167 字符。

受限应用 agent 的系统提示当前是**把 111 条 description 全部动态 join 进去**
（`Agent.__init__`）。模型用不到其中大部分，却每次都付。

## 为什么是两级，而不是直接删掉 description

description 里有**必须知道的约束**，例如「进程只在 code_root 或其子目录内启动」、
「只允许回环地址」。删掉它等于让模型在不知道边界的情况下调工具。

所以做法是**按需展开**：先给 14 个大类的一句话定位（**97 tokens**），
模型觉得某类相关时再问那类里有哪些工具、各自做什么。

## 与「按名字猜」的分野

分类来自 `tool_domains.TOOL_DOMAINS` 的**声明**，不是 `_infer_group(name)` 那种
名字推断。名字推断就是拼词表：新增一个 `dev_xxx` 不会被任何规则捕获，静默落进
「其余」—— 而「其余」正是原来 74 个工具的 `developer` 组。
"""
from __future__ import annotations

from typing import Any, Mapping

from .tool_domains import DOMAINS, TOOL_DOMAINS, facets_of

#: 目录里每个小类最多列出几个工具名，超出只给数量。
#:
#: 小类里工具最多的也只有 9 个（`files/检索`），所以这个上限目前不会触发；
#: 它是防「某个小类以后长到几十个」时目录重新膨胀。
MAX_NAMES_PER_FACET = 12


def render_catalog(tools: Mapping[str, Any] | None = None) -> str:
    """两级目录：一行一个大类，带工具数与小类数。**不含任何工具 description。**"""
    counts: dict[str, int] = {}
    facets: dict[str, set[str]] = {}
    source = tools if tools is not None else _all_specs()
    for name, spec in source.items():
        dom = getattr(spec, "domain", "") or ""
        if not dom or dom not in DOMAINS:
            continue
        counts[dom] = counts.get(dom, 0) + 1
        facets.setdefault(dom, set()).add(getattr(spec, "facet", "") or "")
    lines = ["可用工具按用途分为下列大类。需要细节时用 `tool_catalog` 查某个大类的工具清单。"]
    for dom in DOMAINS:
        if dom not in counts:
            continue
        label, blurb = DOMAINS[dom]
        lines.append("- %s（%s，%d 个工具 / %d 个小类）：%s"
                     % (dom, label, counts[dom], len(facets[dom]), blurb))
    return "\n".join(lines)


def render_domain(domain: str, tools: Mapping[str, Any] | None = None) -> str:
    """展开一个大类：按小类分组，给工具名 + 各自 description。

    没有 description 的工具**不出现** —— 那一列必须是「这个工具做什么」的事实，
    而不是名字。没有就是没有，不拿别的东西凑。
    """
    if domain not in DOMAINS:
        return ("没有 %r 这个大类。现有大类：%s"
                % (domain, "、".join(DOMAINS)))
    label, blurb = DOMAINS[domain]
    source = tools if tools is not None else _all_specs()
    out = ["【%s】%s —— %s" % (domain, label, blurb)]
    for facet, names in facets_of(domain):
        rows = []
        for name in names:
            spec = source.get(name)
            desc = (getattr(spec, "description", "") or "").strip() if spec else ""
            if not desc:
                continue
            rows.append("  - %s: %s" % (name, desc))
        if not rows:
            continue
        out.append("%s：" % facet)
        out.extend(rows[:MAX_NAMES_PER_FACET])
        if len(rows) > MAX_NAMES_PER_FACET:
            out.append("  …另有 %d 个未列出" % (len(rows) - MAX_NAMES_PER_FACET))
    if len(out) == 1:
        out.append("（这一类下没有带说明的工具）")
    return "\n".join(out)


def _all_specs() -> dict[str, Any]:
    """已注册工具的规格表。

    注意在**顶层** `tools` 模块（不在 `agent_runtime.tools`，那里只有契约
    与注册 API，没有注册表）—— 我第一版写错了这个路径。
    """
    import tools as T

    return T.TOOLS


def catalog_tool(arg: str = "") -> str:
    """`tool_catalog` 的实现：按大类 code 查该类的工具清单与用法。

    不给参数时返回两级目录本身（等价于提示里那一段）。
    """
    domain = str(arg or "").strip()
    if not domain:
        return render_catalog()
    return render_domain(domain)


#: 与 `tools.py` 里其他内置工具同样的注册形状
CATALOG_TOOL: dict[str, Any] = {
    "description": (
        "按大类查工具清单与用法。输入大类 code（如 files/frontend/verify/mcp/web），"
        "留空则返回两级目录。工具总数超过百个，系统提示里只放目录，"
        "确定要哪一类再查这一类，省上下文。"
    ),
    "func": catalog_tool,
}