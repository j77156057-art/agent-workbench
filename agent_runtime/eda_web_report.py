"""在线 EDA 报告页 → M3 解析 → M6 合并门 的取页胶水链路（**只读，不执行回滚**）。

信条（对齐 docs/multimodal-verification-engine-eda-kickoff.md §0/§2/§4 + M3/M6）：
- **取页源可注入，换后端其余零改动。** 与 `api_routes.eda.make_frame_source` 的平台可切换
  取像源同套路：M3/M6 只吃文本，"正文从哪来"由一个 `PageSource` 契约决定——默认走仓库已有的
  `tools.web_fetch`（urllib 抓 HTML→正文），也可由调用方（例如 agent 用通用浏览器 `builtin_browser`
  抓回的正文）注入一个常量源。链路本身对"从哪取"无感知。
- **fail-closed，不造假。** 取不到正文（None / 全空白 / 抓取失败串）一律返回 `no_report_text`
  并判 uncertain，**绝不喂空壳给解析器凑一个"看起来通过"的结果**。取到正文但非本类报告，
  照旧由 M3 的 `parse_report` fail-closed 成 `ok:false`，M6 归一为 uncertain → 升级给人。
- **只判不动手。** 复用 `verification_gate.gate_and_maybe_rollback(..., auto_rollback=False)`：
  即便结构化/视觉判 UNMET，也只产出 `decision.action="rollback"` 这个**建议信号**，真实 revert
  须走带 `require_approval` 人工门的动作接口。HTTP 层开一个"自动回滚"入口 = 静默放行风险。
- **不触网、不依赖重模块也能 import。** `tools`（大模块）与网络栈只在真正取页时惰性导入；
  `import agent_runtime.eda_web_report` 本身无副作用，无网络/无依赖的机器上照样能加载、能离线自测。

依赖：只用标准库 + 已提交的 `agent_runtime.eda_tcl`（M3）/ `verification_gate`（M6）。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# 取页源契约：给定 url，返回清理后的正文文本；任何取不到的情况一律返回 None（fail-closed）。
PageSource = Callable[[str], Optional[str]]

# tools.web_fetch / _fetch_page 抓取失败时回传的串前缀（见 tools.py），用于识别"没抓到"。
_FETCH_FAIL_PREFIX = "网页读取失败"


def default_page_source() -> PageSource:
    """默认取页后端：惰性导入 `tools`，用其 builtin（urllib）抓取器取正文。

    `_fetch_page(url, image_budget=0)` 恒返回纯字符串（来源/标题/正文），不含图片分支的
    ToolResult；抓取失败会返回以 `网页读取失败` 开头的串——这里统一规整成 None，让上层
    走 no_report_text 的 fail-closed，而不是把错误提示当正文喂给解析器。
    """
    def _source(url: str) -> Optional[str]:
        try:
            import tools  # 大模块：只在真要联网取页时才导入
            text = tools._fetch_page(url, image_budget=0)
        except Exception:
            return None
        if not text or not str(text).strip():
            return None
        if str(text).lstrip().startswith(_FETCH_FAIL_PREFIX):
            return None
        return str(text)

    return _source


def make_page_source(backend: Optional[str] = "auto", *,
                     content: Optional[str] = None,
                     page_source: Optional[PageSource] = None) -> PageSource:
    """按后端归一成一个 PageSource。

    - `page_source` 显式给定 → 直接用（测试注入 / 浏览器抓回的正文走这条）。
    - `"inline"` → 常量源，恒返回 `content`：调用方（如 agent 用通用浏览器）已备好正文，
      产品代码无需触网——这正是"接到浏览器"的落点（浏览器在编排层，不在运行时）。
    - `"web_fetch"` / `"auto"` → `default_page_source()`（urllib 直抓）。
    """
    if page_source is not None:
        return page_source
    cap = str(backend or "auto").strip().lower()
    if cap == "inline":
        text = content
        return lambda _url: (str(text) if text and str(text).strip() else None)
    # web_fetch / auto：默认 urllib 抓取。
    return default_page_source()


def _source_label(backend: Optional[str], page_source: Optional[PageSource]) -> str:
    if page_source is not None:
        return "injected"
    cap = str(backend or "auto").strip().lower()
    return cap if cap in ("inline", "web_fetch", "auto") else "web_fetch"


def gate_online_report(*, tool: str, kind: str,
                       url: Optional[str] = None,
                       content: Optional[str] = None,
                       fetch: str = "auto",
                       page_source: Optional[PageSource] = None,
                       visual: Optional[dict] = None,
                       vlm_verified: Optional[bool] = None,
                       critical: Optional[Any] = None) -> dict[str, Any]:
    """取一页在线正文 → M3 解析成结构化事实 → M6 双信号合并门（只判不动手）。

    取页源优先级：注入的 `page_source` > `content`（inline）> `fetch` 后端默认抓取。
    全程 fail-closed：取不到正文直接判 uncertain；取到但非本类报告由 M3 判 ok:false。
    """
    from agent_runtime import eda_tcl, verification_gate as vg

    source = make_page_source(fetch, content=content, page_source=page_source)
    label = _source_label(fetch, page_source)

    # ---- 1) 取正文（fail-closed：取不到就绝不往下造事实）----
    try:
        text = source(url or "")
    except Exception:
        text = None

    if not text or not str(text).strip():
        vi_state, _vi_reasons = vg.visual_state(visual, vlm_verified=vlm_verified)
        merged = vg.merge_signals(
            (vg.UNCERTAIN, ["在线正文取不到（fetch_failed/no_report_text）：无可信证据，不能判通过"]),
            (vi_state, _vi_reasons),
        )
        return {
            "ok": False,
            "error": "no_report_text",
            "source": label,
            "url": url,
            "tool": tool,
            "kind": kind,
            "fetch_failed": True,
            "fact": None,  # 明确：没解析出任何事实，绝不给"看起来绿"的空壳
            "gate": {
                "structured_state": vg.UNCERTAIN,
                "visual_state": vi_state,
                "merged": merged,
                "decision": vg.acceptance_decision(merged),
                "rollback_attempted": False,
                "rollback_result": None,
            },
            "executes_rollback": False,
            "note": "取不到在线正文 → 不喂解析器、不造事实，直接判不确定并升级人工/下一段核实。",
        }

    # ---- 2) M3 解析成结构化事实（非本类文本自然 ok:false / metrics 不全，fail-closed）----
    try:
        fact = eda_tcl.parse_report(tool, kind, str(text))
    except Exception as exc:  # 解析器不该抛；抛了也归一成 ok:false，不 500、不假绿
        fact = eda_tcl.EdaFact(ok=False, tool=tool, kind=kind).fail(
            "parse_exception:%s" % str(exc)[:200])

    # ---- 3) M6 合并门（只判不执行回滚：auto_rollback 默认 False）----
    gate = vg.gate_and_maybe_rollback(
        structured=fact.to_dict(), visual=visual,
        vlm_verified=vlm_verified, critical=critical, auto_rollback=False)

    note = ("在线正文已解析；判定由 M3 事实 + M6 门给出。"
            if fact.ok else
            "在线正文非可解析的本类报告 → M3 判 ok:false，M6 归一为不确定，交人工/下一段，"
            "绝不据此谎报通过。")
    return {
        "ok": True,
        "source": label,
        "url": url,
        "tool": tool,
        "kind": kind,
        "fetch_failed": False,
        "fact": fact.to_dict(),
        "gate": gate,
        "executes_rollback": False,
        "note": note,
    }
