"""在线 EDA 报告页 → M3 解析 →（可选 M4/M5 视觉回归）→ M6 合并门 的取页+取像胶水链路（**只读，不执行回滚**）。

信条（对齐 docs/multimodal-verification-engine-eda-kickoff.md §0/§2/§4 + M3/M5/M6）：
- **取页源可注入，换后端其余零改动。** 与 `api_routes.eda.make_frame_source` 的平台可切换
  取像源同套路：M3/M6 只吃文本，"正文从哪来"由一个 `PageSource` 契约决定——默认走仓库已有的
  `tools.web_fetch`（urllib 抓 HTML→正文），也可由调用方（例如 agent 用通用浏览器 `builtin_browser`
  抓回的正文）注入一个常量源。链路本身对"从哪取"无感知。
- **双通道一起进 gate。** 视觉侧同样可插拔：调用方要么直接透传一个已算好的 `visual` verdict dict
  （`visual_source="passthrough"`），要么给 `golden_key`+`golden_root`+ROI(view/bbox)+帧来源
  （M2/M5 的零参 `FrameSource`，或 M4 `frame_grabber(hwnd)`）→ 本层复用 M5 `check_eda_regression`
  **现算**一次 ROI golden diff（`visual_source="regression"`），把 verdict 喂进 M6。比对/取像逻辑
  全部复用已提交的 M1/M2/M4/M5 零件，本层**不新写任何像素比较**。都不给 → 视觉 uncertain，
  退回纯文本单通道（对齐"没证据就别判绿"）。
- **fail-closed，不造假。** 取不到正文（None / 全空白 / 抓取失败串）一律返回 `no_report_text`
  并判 uncertain，**绝不喂空壳给解析器凑一个"看起来通过"的结果**。取到正文但非本类报告，
  照旧由 M3 的 `parse_report` fail-closed 成 `ok:false`，M6 归一为 uncertain → 升级给人。
  视觉侧取不到帧/没 golden 由 M5 判 `no_frame`/`needs_golden` → uncertain；比对里任何异常也收敛成
  uncertain（附 `visual_error`），不 500、不假绿。
- **只判不动手。** 复用 `verification_gate.gate_and_maybe_rollback(..., auto_rollback=False)`：
  即便结构化/视觉判 UNMET，也只产出 `decision.action="rollback"` 这个**建议信号**，真实 revert
  须走带 `require_approval` 人工门的动作接口。HTTP 层开一个"自动回滚"入口 = 静默放行风险。
- **不在在线链里存 golden。** 存基线是有副作用的写盘动作，留给 `/api/eda/visual-regression mode=save`
  与本地 `eda_verify_pipeline`；在线只读门只**比对已有 golden**，守住"只读不动手"。
- **不触网、不依赖重模块也能 import。** `tools`（大模块）、网络栈、以及
  `eda_visual_regression`/`visual_regression`/`frame_grabber_linux`（numpy/PIL）都只在真正取页/取像时
  惰性导入；`import agent_runtime.eda_web_report` 本身无副作用，无网络/无 PIL/无 X11 的机器照样能加载、能离线自测。

依赖：只用标准库 + 已提交的 `agent_runtime.eda_tcl`（M3）/ `verification_gate`（M6）；
视觉回归路径再惰性复用 `eda_visual_regression`（M5）/ `visual_regression`（M1）/ `frame_grabber_linux`（M4）。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

# 取页源契约：给定 url，返回清理后的正文文本；任何取不到的情况一律返回 None（fail-closed）。
PageSource = Callable[[str], Optional[str]]

# 取帧源契约（对齐 M2/M5 的 FrameSource）：零参返回 (bgra, w, h, hwnd)，取不到一律 None（fail-closed）。
FrameSource = Callable[[], Optional[tuple]]

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


def _produce_visual(*,
                    visual: Optional[dict],
                    frame_source: Optional[FrameSource],
                    grabber: Optional[Callable[[Any], Optional[tuple]]],
                    window_id: Any,
                    golden_root: Optional[str],
                    golden_key: Optional[str],
                    view: Optional[str],
                    bbox_norm: Optional[Iterable[float]],
                    out_size: int,
                    ssim_threshold: float,
                    delta_threshold: float,
                    grid: int,
                    ) -> tuple[Optional[dict], str, Optional[str]]:
    """产出进 M6 的视觉 verdict dict，返回 `(visual, visual_source, visual_error)`。

    优先级（对齐"取页源可注入、换后端零改动"的同构套路）：
    1) 调用方已直接给 `visual` dict → 原样透传（`visual_source="passthrough"`，兼容旧行为
       与"通用浏览器抓回 verdict 直接喂进来"这条）。
    2) 给了 `golden_key` + `golden_root` + ROI（view 或 bbox_norm）+ 帧来源
       （`frame_source` 或 M4 `grabber`）→ **真跑 M5** `check_eda_regression` 拿 verdict
       （`visual_source="regression"`）。比对逻辑完全复用已提交的 M1/M2/M5 零件，本层不新写。
    3) 以上都不满足 → `(None, "none", None)`：在线链退回纯文本单通道，M6 判视觉 uncertain
       （**绝不猜、绝不假绿**）。

    全程 fail-closed：惰性导入/取像/比对里任何异常都收敛成 `visual=None` + 一个 `visual_error`
    串，让上层照旧把视觉归成 uncertain——不 500、不把"看起来绿"的空壳喂给门。
    """
    # ---- 1) 已给 verdict dict：直接透传，不碰重模块 ----
    if isinstance(visual, dict) and "verdict" in visual:
        return visual, "passthrough", None

    # ---- 3) 没配视觉回归入参：退回单通道 ----
    has_frames = frame_source is not None or grabber is not None
    has_roi = bbox_norm is not None or view is not None
    if not (golden_key and golden_root and has_frames and has_roi):
        return None, "none", None

    # ---- 2) 真跑 M5：ROI + 帧来源 vs golden 的确定性 diff ----
    try:
        from agent_runtime import eda_visual_regression as ev
        from agent_runtime import visual_regression as vr

        src = frame_source
        if src is None:
            # 把 M4 grabber(hwnd)->(bgra,w,h) 适配成引擎要的零参 FrameSource。
            src = ev.frame_source_from_grabber(grabber, window_id)

        store = vr.GoldenStore(root=golden_root)
        result = ev.check_eda_regression(
            store, golden_key, view=view, bbox_norm=bbox_norm, source=src,
            out_size=out_size, ssim_threshold=ssim_threshold,
            delta_threshold=delta_threshold, grid=grid)
        if isinstance(result, dict) and "verdict" in result:
            return result, "regression", None
        return None, "regression", "visual_no_verdict"
    except Exception as exc:  # 惰性导入缺依赖 / 取像炸 / 比对异常 → fail-closed 成 uncertain
        return None, "regression", ("visual_error:%s" % str(exc)[:180])


def gate_online_report(*, tool: str, kind: str,
                       url: Optional[str] = None,
                       content: Optional[str] = None,
                       fetch: str = "auto",
                       page_source: Optional[PageSource] = None,
                       visual: Optional[dict] = None,
                       vlm_verified: Optional[bool] = None,
                       critical: Optional[Any] = None,
                       frame_source: Optional[FrameSource] = None,
                       grabber: Optional[Callable[[Any], Optional[tuple]]] = None,
                       window_id: Any = None,
                       golden_root: Optional[str] = None,
                       golden_key: Optional[str] = None,
                       view: Optional[str] = None,
                       bbox_norm: Optional[Iterable[float]] = None,
                       out_size: int = 256,
                       ssim_threshold: float = 0.98,
                       delta_threshold: float = 8.0,
                       grid: int = 8,
                       changeset_id: Optional[str] = None,
                       project_root: Optional[str] = None) -> dict[str, Any]:
    """取一页在线正文 → M3 解析 →（可选）M5 ROI golden diff → M6 双信号合并门（只判不动手）。

    取页源优先级：注入的 `page_source` > `content`（inline）> `fetch` 后端默认抓取。
    视觉通道优先级：透传的 `visual` dict >（给了 golden_key+golden_root+ROI+帧来源时）真跑
    `check_eda_regression` 产 verdict > 都没有则视觉 uncertain（退回纯文本单通道）。
    全程 fail-closed：取不到正文直接判 uncertain；取到但非本类报告由 M3 判 ok:false；
    取不到帧/没 golden 由 M5 判 no_frame/needs_golden → M6 uncertain。绝不谎报通过。
    """
    from agent_runtime import eda_tcl, verification_gate as vg

    source = make_page_source(fetch, content=content, page_source=page_source)
    label = _source_label(fetch, page_source)

    # ---- 先算视觉 verdict（透传或真跑 M5）；异常/缺参数已在 _produce_visual 里收敛成 uncertain ----
    visual, visual_source, visual_error = _produce_visual(
        visual=visual, frame_source=frame_source, grabber=grabber, window_id=window_id,
        golden_root=golden_root, golden_key=golden_key, view=view, bbox_norm=bbox_norm,
        out_size=out_size, ssim_threshold=ssim_threshold,
        delta_threshold=delta_threshold, grid=grid)

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
            "visual_source": visual_source,
            "visual_error": visual_error,
            "gate": {
                "structured_state": vg.UNCERTAIN,
                "visual_state": vi_state,
                "merged": merged,
                "decision": vg.acceptance_decision(merged),
                "rollback_attempted": False,
                "rollback_result": None,
            },
            "executes_rollback": False,
            "approval_requested": False,
            "changeset_id": changeset_id or "",
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

    # ---- 3b) 门判 rollback → 登记审批请求（桥线：gate→人工审批队列，绝不自动 revert）----
    approval_requested = False
    if (gate.get("decision", {}).get("action") == "rollback"
            and changeset_id and project_root):
        try:
            from agent_runtime.cockpit_policy import record_gate_request
            record_gate_request(project_root, "rollback_changeset", changeset_id)
            approval_requested = True
        except Exception:  # 队列写不进也绝不崩门，只是不登记（降级为纯建议）
            pass

    note = ("在线正文已解析；判定由 M3 事实 + M6 门给出"
            + ("（视觉走 M5 ROI golden diff 现算）" if visual_source == "regression"
               else "（视觉为调用方透传 verdict）" if visual_source == "passthrough"
               else "（未配视觉通道，视觉判不确定）")
            + "。"
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
        "visual_source": visual_source,
        "visual_error": visual_error,
        "gate": gate,
        "executes_rollback": False,
        "approval_requested": approval_requested,
        "changeset_id": changeset_id or "",
        "note": note,
    }
