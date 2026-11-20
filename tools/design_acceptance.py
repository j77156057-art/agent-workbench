"""DocMind 设计系统 · 商业化验收检测器（v1.0）

定位：把 docs/DESIGN-ACCEPTANCE.md 里的条款变成可执行判定，供 CI / 本地门禁调用。
纯标准库（无第三方依赖），只读源码，不改任何文件（--update-baseline 除外）。

用法：
    python tools/design_acceptance.py                # 人读报告 + 门禁判定（红则退出码 1）
    python tools/design_acceptance.py --json         # 机器读
    python tools/design_acceptance.py --gate         # 只输出门禁结论
    python tools/design_acceptance.py --update-baseline   # 承认现状，把存量记入基线

设计原则（与项目既有方法论一致，勿改）：
  ① **先量后判**：所有阈值来自实测基线，不凭空设定。
  ② **存量不背锅，增量不许破**：存量违规进 baseline 白名单，新增一条即门禁红。
     这样标准可以立即上线，而不必先还清全部历史债。
  ③ **区分"有意共享"与"漏定义"**：结构类令牌（--scrim / --edge-w）在
     `:root:root:root` 给默认值、各皮肤共用，是设计决定；配色类令牌必须逐套覆写。
  ④ **区分"动态注入"与"死引用"**：Vue `:style` 与 JS `setProperty` 注入的令牌
     不在 CSS 里定义是合法的，不能误判。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "frontend" / "src"
BASELINE = Path(__file__).resolve().with_name("design_acceptance_baseline.json")
_IS_DEFAULT_SRC = True   # --src 指到别处时置 False，此时存量基线不适用

# ---------- 规则常量（改这里 = 改标准，须同步 docs/DESIGN-ACCEPTANCE.md） ----------

# 必须逐套皮肤覆写的**配色类**令牌。结构类（--scrim / --edge-w / --r-*）不在此列：
# 它们在 :root:root:root 有共享默认值，各皮肤共用是有意的设计决定。
SKIN_COLOR_TOKENS = [
    "--bg", "--surface", "--bg-raised", "--bg-hover", "--bg-selected",
    "--ink", "--text", "--text-muted",
    "--border", "--border-strong",
    "--accent", "--accent-text", "--accent-ink",
    "--danger", "--amber", "--green",
]

MOTION_MAX_MS = 260          # transition 时长上限（--motion-modal 260ms）
SPACING_GRID = 4             # 主档栅格
SPACING_TOLERANCE = 2        # 微调档：2 的倍数但非 4 倍数，容忍
SPACING_ALLOW_ODD = {1}      # 1px 允许：细线补偿，不算间距

SKIP_DIRS = {"node_modules", "dist", "build", "__pycache__", ".git"}
INTERACTIVE_SEL = re.compile(r":hover\b")

# ---------- 读取 ----------


def _strip_comments(s: str) -> str:
    return re.sub(r"/\*.*?\*/", "", s, flags=re.S)


def load_units() -> list[tuple[str, str]]:
    """返回 [(unit_name, css_text)]。CSS 整体成一块，Vue 按 <style> 块拆。"""
    units: list[tuple[str, str]] = []
    for p in sorted(SRC.rglob("*.css")):
        if any(d in p.parts for d in SKIP_DIRS):
            continue
        units.append((p.relative_to(SRC).as_posix(), _strip_comments(p.read_text(encoding="utf-8", errors="replace"))))
    for p in sorted(SRC.rglob("*.vue")):
        if any(d in p.parts for d in SKIP_DIRS):
            continue
        raw = p.read_text(encoding="utf-8", errors="replace")
        for i, m in enumerate(re.finditer(r"<style[^>]*>(.*?)</style>", raw, re.S)):
            units.append((f"{p.relative_to(SRC).as_posix()}#style{i}", _strip_comments(m.group(1))))
    return units


def dynamic_tokens() -> set[str]:
    """CSS 之外注入的令牌：Vue :style 对象键 + JS setProperty。合法，不计死引用。"""
    found: set[str] = set()
    pats = [
        re.compile(r"""['"](--[a-zA-Z0-9_-]+)['"]\s*:"""),          # {'--x': ...}
        re.compile(r"""\.setProperty\(\s*['"](--[a-zA-Z0-9_-]+)['"]"""),
    ]
    for ext in ("*.vue", "*.ts"):
        for p in SRC.rglob(ext):
            if any(d in p.parts for d in SKIP_DIRS):
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            for pat in pats:
                found.update(pat.findall(txt))
    return found


def iter_rules(units):
    """粗粒度块切分：(unit, selector, body)。够用——只做静态特征匹配。"""
    for name, text in units:
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
            yield name, m.group(1).strip(), m.group(2)


# ---------- 各项规则 ----------


def r01_font_size_literal(units):
    """R01 字面量字号：必须走 --fs-* 令牌（clamp / em / rem 不算违规）。"""
    hits = []
    for name, text in units:
        for m in re.finditer(r"font-size\s*:\s*([^;}]+)", text):
            v = m.group(1).strip()
            if "var(--" in v or "clamp(" in v or "calc(" in v:
                continue
            if re.search(r"\d+(\.\d+)?px", v):
                hits.append({"unit": name, "value": v})
    return hits


def r02_undefined_tokens(units) -> list[dict]:
    """R02 死引用：var(--x) 引用但既无 CSS 定义、也无动态注入。"""
    all_text = "\n".join(t for _, t in units)
    defs = set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", all_text))
    refs = set(re.findall(r"var\(\s*(--[a-zA-Z0-9_-]+)", all_text))
    dyn = dynamic_tokens()
    missing = sorted(refs - defs - dyn)
    out = []
    for tok in missing:
        where = [n for n, t in units if re.search(r"var\(\s*" + re.escape(tok) + r"[\s,)]", t)]
        out.append({"token": tok, "units": where[:4]})
    return out


def r03_color_fallback(units) -> list[dict]:
    """R03 色值兜底：var(--x, #hex) 会静默掩盖令牌名写错，禁止。"""
    hits = []
    for name, text in units:
        for m in re.finditer(r"var\(\s*(--[a-zA-Z0-9_-]+)\s*,\s*([^)]*)\)", text):
            v = m.group(2).strip()
            if re.match(r"^#|^rgba?\(|^hsla?\(|^oklch\(|^(white|black|transparent)$", v):
                hits.append({"unit": name, "token": m.group(1), "fallback": v[:40]})
    return hits


def r04_spacing_grid(units):
    """R04 间距栅格：主档 4 的倍数；微调档允许 2 的倍数；奇数(≠1) 为违规。"""
    props = r"(?:margin|padding|gap|row-gap|column-gap)(?:-(?:top|right|bottom|left))?"
    violations, conform, tolerance, total = [], 0, 0, 0
    for name, text in units:
        for m in re.finditer(props + r"\s*:\s*([^;}]+)", text):
            val = m.group(1)
            if any(k in val for k in ("var(--", "auto", "%", "calc(", "vw", "vh")):
                continue
            nums = [float(x) for x in re.findall(r"(-?[\d.]+)px", val)]
            if not nums:
                continue
            total += 1
            bad = [n for n in nums if n != 0 and n not in SPACING_ALLOW_ODD and n % SPACING_TOLERANCE != 0]
            if bad:
                violations.append({"unit": name, "value": val.strip()[:44], "off": bad})
            elif all(n % SPACING_GRID == 0 or n in SPACING_ALLOW_ODD for n in nums):
                conform += 1
            else:
                tolerance += 1
    return {"violations": violations, "conform": conform, "tolerance": tolerance, "total": total}


def r05_skin_coverage(units) -> dict[str, list[str]]:
    """R05 皮肤一致性：配色令牌必须逐套覆写，否则切皮肤时该处不跟随。"""
    blocks: dict[str, set[str]] = defaultdict(set)
    for _, sel, body in iter_rules(units):
        if "data-skin=" not in sel:
            continue
        m = re.search(r"data-skin=['\"]?([a-zA-Z0-9_-]+)['\"]?", sel)
        if m:
            blocks[m.group(1)].update(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", body))
    return {k: [t for t in SKIN_COLOR_TOKENS if t not in v] for k, v in sorted(blocks.items())}


def r06_outline_compensation(units):
    """R06 焦点可见：outline:none 所在选择器组必须自带 :focus，或全局有 :focus-visible 兜底。"""
    global_focus = any(":focus-visible" in t for _, t in units)
    uncompensated = []
    for name, sel, body in iter_rules(units):
        if not re.search(r"outline\s*:\s*(none|0)\b", body):
            continue
        if ":focus" in sel:
            continue
        # 同一选择器组里别处给了 focus 环也算补偿
        if ":focus" in body:
            continue
        uncompensated.append({"unit": name, "selector": sel[-80:]})
    return {"uncompensated": uncompensated, "has_global_fallback": global_focus}


def r07_motion_duration(units):
    """R07 动效克制：transition 不得超过 260ms；animation 不设上限但统计。"""
    over, transition_total, token_transition, anim_max = [], 0, 0, 0
    for name, text in units:
        for m in re.finditer(r"transition\s*:\s*([^;}]+)", text):
            v = m.group(1)
            transition_total += 1
            if "var(--motion-" in v:
                token_transition += 1
            for ms in re.findall(r"(\d+)ms", v):
                if int(ms) > MOTION_MAX_MS:
                    over.append({"unit": name, "value": v.strip()[:60], "ms": int(ms)})
        for m in re.finditer(r"animation\s*:\s*([^;}]+)", text):
            for ms in re.findall(r"(\d+)ms", m.group(1)):
                anim_max = max(anim_max, int(ms))
    return {"over": over, "transition_total": transition_total,
            "token_transition": token_transition, "anim_max_ms": anim_max}


def r08_reduced_motion(units) -> int:
    all_text = "\n".join(t for _, t in units)
    return len(re.findall(r"prefers-reduced-motion\s*:\s*reduce", all_text))


def r09_focus_pairing(units) -> dict:
    """R09 交互可达：有 :hover 的文件必须至少有一条 :focus 规则（文件级，抗噪）。"""
    per_file: dict[str, dict[str, int]] = defaultdict(lambda: {"hover": 0, "focus": 0})
    for name, text in units:
        per_file[name]["hover"] += len(INTERACTIVE_SEL.findall(text))
        per_file[name]["focus"] += len(re.findall(r":focus(?:-visible|-within)?\b", text))
    offending = {k: v for k, v in per_file.items() if v["hover"] > 0 and v["focus"] == 0}
    return {"offending": offending,
            "hover_total": sum(v["hover"] for v in per_file.values()),
            "focus_total": sum(v["focus"] for v in per_file.values())}


def r10_state_coverage() -> dict:
    """R10 状态完整度：存在的入口目录，各自具备 空 / 载 / 错 三类语义。

    目录不存在 → 记为 skipped（该入口不在本棵树里，不该判缺）；
    存在则三态必须齐全。真实树两个入口都在，所以这条仍然有效。
    """
    targets = {"workbench": SRC / "workbench", "ask": SRC / "ask"}
    out = {}
    for name, d in targets.items():
        files = [p for p in d.rglob("*") if p.is_file() and p.suffix in {".vue", ".ts"}] if d.exists() else []
        # 目录不存在、或目录里没有任何页面代码 → 该入口不在本棵树，跳过。
        if not files:
            out[name] = {"skipped": True}
            continue
        blob = "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
        out[name] = {
            "empty": bool(re.search(r"empty|no-?data|:empty", blob, re.I)),
            "loading": bool(re.search(r"loading|skeleton|aria-busy", blob, re.I)),
            "error": bool(re.search(r"error|failed|无法|失败", blob, re.I)),
        }
    return out


# ---------- R11 用：色值 → OKLCH ----------


def _hex_to_rgb(h: str):
    h = h.strip().lstrip("#")
    if len(h) in (3, 4):
        h = "".join(c * 2 for c in h[:3])
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None


def _rgb_to_oklch(rgb):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (x ** (1 / 3) for x in (l, m, s))
    L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s
    a = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s
    bb = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s
    return L, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


HUE_HARMONY_MAX = 40    # 中性色 bg / ink 的色相差上限（度）
HUE_NEUTRAL_C = 0.04    # 低于此彩度即视为"中性色"——中性色互补会互相拉脏


def r11_skin_hue_harmony(units) -> list[dict]:
    """R11 中性色色相同族：--bg 与 --ink 的色相差不得超过 40°（仅当两者都是低彩度）。

    2026-11-19 实测教训：graphite 的 --bg 是 H268（蓝紫）、--ink 是 H91（暖黄白），
    相差 176.7°，接近正互补，而两者彩度只有 0.008~0.010。低彩度互补并置不会产生
    "温暖对比"，只会互相拉脏 —— 底显脏、字显黄，整屏发闷。这是四套皮肤里唯一
    bg/ink 色相差超过 32° 的一套，用户直接反馈"这套配色看着最怪"。
    修法是把 ink 改到 250°（ΔH≈18°），亮度不变、对比度反而从 12.15 提到 13.4:1。

    豁免条件：任一方彩度 ≥ 0.04 时不做判定 —— 高彩度互补是有意的设计手法
    （如互补色强调），不属于"脏"。这条规则只管"低彩度中性色互相抵消"。
    """
    blocks: dict[str, dict[str, str]] = {}
    for _, sel, body in iter_rules(units):
        if "data-skin=" not in sel:
            continue
        m = re.search(r"data-skin=['\"]?([a-zA-Z0-9_-]+)['\"]?", sel)
        if not m:
            continue
        d = blocks.setdefault(m.group(1), {})
        for tok, key in (("--bg", "bg"), ("--ink", "ink")):
            mm = re.search(re.escape(tok) + r"\s*:\s*(#[0-9a-fA-F]{3,8})\s*;", body)
            if mm:
                d[key] = mm.group(1)

    out = []
    for skin, d in sorted(blocks.items()):
        if not (d.get("bg") and d.get("ink")):
            continue
        rb, ri = _hex_to_rgb(d["bg"]), _hex_to_rgb(d["ink"])
        if not (rb and ri):
            continue
        _, c_bg, h_bg = _rgb_to_oklch(rb)
        _, c_ink, h_ink = _rgb_to_oklch(ri)
        # 有一方接近纯灰（C≈0）时，色相是无意义的数值噪声 —— #111111 这类纯中性色
        # 算出来的 H 可以是任意值。参与判定会产生随机误报（实测自证用例就栽在这）。
        if min(c_bg, c_ink) < 0.005:
            continue
        delta = abs(h_bg - h_ink)
        delta = min(delta, 360 - delta)
        if delta > HUE_HARMONY_MAX and max(c_bg, c_ink) < HUE_NEUTRAL_C:
            out.append({
                "skin": skin, "bg": d["bg"], "ink": d["ink"],
                "delta": round(delta, 1),
                "bgC": round(c_bg, 3), "inkC": round(c_ink, 3),
            })
    return out


# ---------- 汇总 ----------


def collect() -> dict:
    units = load_units()
    return {
        "units_scanned": len(units),
        "R01": r01_font_size_literal(units),
        "R02": r02_undefined_tokens(units),
        "R03": r03_color_fallback(units),
        "R04": r04_spacing_grid(units),
        "R05": r05_skin_coverage(units),
        "R06": r06_outline_compensation(units),
        "R07": r07_motion_duration(units),
        "R08": r08_reduced_motion(units),
        "R09": r09_focus_pairing(units),
        "R10": r10_state_coverage(),
        "R11": r11_skin_hue_harmony(units),
    }


def _keyset(items, *fields) -> set[str]:
    return {json.dumps([i.get(f) for f in fields], ensure_ascii=False, sort_keys=True) for i in items}


def load_baseline() -> dict:
    # --src 指向别处（变异测试 / 单测）时，存量白名单没有意义：
    # 那棵树里的每一条都算"新增"，门禁才会真的对违规敏感。
    if not _IS_DEFAULT_SRC:
        return {}
    if BASELINE.exists():
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    return {}


def baseline_payload(rep: dict) -> dict:
    """把"存量债务"记成可比对的键集合。新增即门禁红。"""
    return {
        "_note": "存量债务白名单。新增违规=门禁红；修掉后可用 --update-baseline 收紧。",
        "R01": sorted(_keyset(rep["R01"], "unit", "value")),
        "R03": sorted(_keyset(rep["R03"], "unit", "token", "fallback")),
        "R04": sorted(_keyset(rep["R04"]["violations"], "unit", "value")),
        "R06": sorted(_keyset(rep["R06"]["uncompensated"], "unit", "selector")),
    }


def evaluate(rep: dict, base: dict) -> list[dict]:
    """返回 [{id, level, title, detail}]。level: red / yellow / ok。"""
    rows = []
    base = base or {}
    newb = baseline_payload(rep)

    def delta(rule):
        old = set(base.get(rule, []))
        return sorted(set(newb[rule]) - old)

    # R01 字号令牌化 —— 只拦新增
    d = delta("R01")
    rows.append({"id": "R01", "level": "red" if d else "ok",
                 "title": "字号必须走 --fs-* 令牌",
                 "detail": f"新增字面量 {len(d)} 处" + (f"：{d[:2]}" if d else f"（存量 {len(rep['R01'])} 处豁免）")})

    # R02 死引用 —— 零容忍
    n = len(rep["R02"])
    rows.append({"id": "R02", "level": "red" if n else "ok",
                 "title": "不得引用未定义的令牌",
                 "detail": f"{n} 处" + (f"：{', '.join(x['token'] for x in rep['R02'][:4])}" if n else "")})

    # R03 色值兜底 —— 只拦新增
    d = delta("R03")
    rows.append({"id": "R03", "level": "red" if d else "ok",
                 "title": "禁 var(--x, 色值) 兜底",
                 "detail": f"新增 {len(d)} 处" + (f"（存量 {len(rep['R03'])} 处豁免）" if not d else "")})

    # R04 间距栅格 —— 只拦新增 + 比例不恶化
    d = delta("R04")
    r04 = rep["R04"]
    rows.append({"id": "R04", "level": "red" if d else "ok",
                 "title": "间距落在 2/4px 栅格",
                 "detail": f"违规 {len(r04['violations'])}/{r04['total']} 声明"
                           f"（合规 {r04['conform']} / 微调 {r04['tolerance']}），新增 {len(d)} 处"})

    # R05 皮肤覆盖 —— 零容忍
    missing = {k: v for k, v in rep["R05"].items() if v}
    rows.append({"id": "R05", "level": "red" if missing else "ok",
                 "title": "配色令牌逐套皮肤覆写",
                 "detail": f"{sum(len(v) for v in missing.values())} 项缺失" + (f"：{missing}" if missing else "")})

    # R06 焦点补偿 —— 观测项。
    # 为什么不进硬门禁：静态分析无法判断「outline:none 之后焦点是否仍然可见」，
    # 绝大多数命中是 input/textarea 自带 border 变化 + 全局 focus-visible 兜底。
    # 只在「完全没有全局兜底」且「有新增未补偿」时才判红 —— 那才是真的键盘不可达。
    d = delta("R06")
    has_fb = rep["R06"]["has_global_fallback"]
    if d and not has_fb:
        lvl = "red"
    elif rep["R06"]["uncompensated"]:
        lvl = "yellow"
    else:
        lvl = "ok"
    rows.append({"id": "R06", "level": lvl,
                 "title": "outline:none 必须有焦点补偿（观测）",
                 "detail": f"未补偿 {len(rep['R06']['uncompensated'])} 处" + (f"，新增 {len(d)}" if d else "")
                           + ("；全局有 :focus-visible 兜底" if has_fb else "；⚠ 无全局兜底")})

    # R07 transition 时长 —— 零容忍
    over = rep["R07"]["over"]
    rows.append({"id": "R07", "level": "red" if over else "ok",
                 "title": "transition ≤ 260ms",
                 "detail": f"{len(over)} 处超标，最长 animation {rep['R07']['anim_max_ms']}ms"})

    # R08 reduced-motion —— 必须存在
    n = rep["R08"]
    rows.append({"id": "R08", "level": "ok" if n else "red",
                 "title": "尊重 prefers-reduced-motion",
                 "detail": f"{n} 个 reduce 覆写块"})

    # R09 focus 配对 —— 观测项。
    # 文件级统计是弱判据：focus 环可能由父级/全局统一提供。真正判据是浏览器里
    # 逐元素 Tab 遍历（见 docs/DESIGN-ACCEPTANCE.md §7 人工评审），静态层只报趋势。
    off = rep["R09"]["offending"]
    if off and not rep["R06"]["has_global_fallback"]:
        lvl = "red"
    elif off:
        lvl = "yellow"
    else:
        lvl = "ok"
    rows.append({"id": "R09", "level": lvl,
                 "title": "有 hover 的文件必须有 focus（观测）",
                 "detail": f"缺口 {len(off)} 个文件；hover {rep['R09']['hover_total']} / focus {rep['R09']['focus_total']}"})

    # R10 状态完整度（skipped 的入口不参与判定）
    active = {k: v for k, v in rep["R10"].items() if not v.get("skipped")}
    bad = [k for k, v in active.items() if not all(v.values())]
    rows.append({"id": "R10", "level": "red" if bad else "ok",
                 "title": "空/载/错三态齐全",
                 "detail": "、".join(f"{k} 缺 {[s for s, ok in v.items() if not ok]}" for k, v in active.items() if not all(v.values()))
                           or f"{len(active)} 个入口均齐全"})

    # R11 中性色色相同族 —— 零容忍（低彩度互补会让整屏发脏）
    h = rep["R11"]
    rows.append({"id": "R11", "level": "red" if h else "ok",
                 "title": "中性色 bg/ink 色相同族（ΔH ≤ 40°）",
                 "detail": f"{len(h)} 套不同族" + (
                     "：" + "、".join(f"{x['skin']} ΔH={x['delta']}°" for x in h) if h else "")})

    return rows


LEVEL_MARK = {"red": "✗ 红", "yellow": "! 黄", "ok": "✓ 绿"}


def print_report(rep: dict, rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"report": rep, "gates": rows}, ensure_ascii=False, indent=2))
        return
    print("=" * 74)
    print("DocMind 设计系统 · 商业化验收   （扫描 %d 个样式单元）" % rep["units_scanned"])
    print("=" * 74)
    for r in rows:
        print(f"[{LEVEL_MARK[r['level']]:<4}] {r['id']}  {r['title']}")
        print(f"        {r['detail']}")
    red = [r for r in rows if r["level"] == "red"]
    yellow = [r for r in rows if r["level"] == "yellow"]
    print("-" * 74)
    verdict = "PASS" if not red else "FAIL"
    print(f"结论：{verdict}   红 {len(red)} / 黄 {len(yellow)} / 绿 {len(rows) - len(red) - len(yellow)}")
    if red:
        print("须修：" + "、".join(r["id"] for r in red))


def main(argv=None) -> int:
    global SRC, _IS_DEFAULT_SRC
    ap = argparse.ArgumentParser(description="DocMind 设计系统商业化验收")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--gate", action="store_true", help="只输出 PASS/FAIL")
    ap.add_argument("--update-baseline", action="store_true", help="承认现状并写入基线")
    ap.add_argument("--src", help="覆盖扫描目录（变异测试用；此模式下基线不适用）")
    args = ap.parse_args(argv)

    if args.src:
        SRC = Path(args.src).resolve()
        _IS_DEFAULT_SRC = False

    if not SRC.exists():
        print(f"找不到前端源码目录：{SRC}", file=sys.stderr)
        return 2

    rep = collect()
    if args.update_baseline:
        payload = baseline_payload(rep)
        BASELINE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"基线已写入 {BASELINE.relative_to(REPO)}")
        return 0

    rows = evaluate(rep, load_baseline())

    if args.gate:
        red = [r["id"] for r in rows if r["level"] == "red"]
        print("FAIL " + ",".join(red) if red else "PASS")
        return 1 if red else 0

    print_report(rep, rows, args.json)
    return 1 if any(r["level"] == "red" for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
