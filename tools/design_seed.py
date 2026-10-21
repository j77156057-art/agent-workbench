# -*- coding: utf-8 -*-
"""种子驱动的设计令牌生成器（Agent 工作台 前端）。

为什么有这个东西
----------------
改界面时最耗人的不是"改哪个色"，而是**凭感觉来回试**：改了主色忘了改淡染、
改了浅底忘了深底、字号随手写了 11.5px —— 每一次都要人肉把所有派生值重算一遍。

这个脚本把"设计"变成**可复现的推导**：
    随机种子 → 一整套设计参数（色相/饱和/明度/圆角/阴影/描边/字号/间距）
             → 一份 CSS 皮肤（frontend/src/workbench/skins-seed.css）
同一个种子永远产出同一份皮肤；换种子就是换一整套观感，且**保证过 AA 对比度**
（色值不是随机出来的，是"随机选方向 + 按对比度反解明度"）。

用法
----
    python tools/design_seed.py --new-seed        # 随机一个新种子并写出皮肤
    python tools/design_seed.py                   # 用当前种子重新生成（幂等）
    python tools/design_seed.py --seed 20260720   # 指定种子
    python tools/design_seed.py --print           # 只打印派生参数，不写文件

约束
----
* 正文色对底色 ≥ 4.5:1（WCAG AA），描边/图形 ≥ 3:1 —— 由 ensure_contrast() 反解明度保证。
* 派生参数全部落在"能用"的区间内（圆角 0–12px、字号基线 13–15px、间距缩放 0.9–1.1），
  随机不等于失控。
* 生成物是**一个皮肤**（data-skin="seed"），与 paper / graphite / zinc 并列，
  不覆盖既有皮肤 —— 换种子 = 换观感，删文件 = 回到原样。
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_CSS = os.path.join(ROOT, "frontend", "src", "workbench", "skins-seed.css")
SEED_FILE = os.path.join(ROOT, "frontend", "src", "workbench", "design-seed.json")


# ---------------------------------------------------------------- 颜色工具
def hsl_to_rgb(h, s, l):
    """h: 0-360, s/l: 0-1 → (r, g, b) 0-255"""
    h = h % 360
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    if h < 60:   r, g, b = c, x, 0
    elif h < 120: r, g, b = x, c, 0
    elif h < 180: r, g, b = 0, c, x
    elif h < 240: r, g, b = 0, x, c
    elif h < 300: r, g, b = x, 0, c
    else:         r, g, b = c, 0, x
    return tuple(int(round((v + m) * 255)) for v in (r, g, b))


def rgb_to_hsl(r, g, b):
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        h, s = 0.0, 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:   h = ((g - b) / d) % 6
        elif mx == g: h = (b - r) / d + 2
        else:         h = (r - g) / d + 4
        h *= 60
    return h, s, l


def rel_lum(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def contrast(a, b):
    la, lb = rel_lum(a), rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(rgb, bg, target, darker=True):
    """把 rgb 的明度往目标方向推，直到对 bg 的对比度 ≥ target。

    颜色**方向**是随机的，明度是**反解**出来的 —— 这样"随机"不会产出不可用的配色。
    darker=True 往深走（浅底上的文字/强调色），False 往浅走（深底上的文字）。
    """
    h, s, l = rgb_to_hsl(*rgb)
    step = 0.01
    for _ in range(100):
        if contrast(rgb, bg) >= target:
            return rgb
        l += -step if darker else step
        l = max(0.0, min(1.0, l))
        rgb = hsl_to_rgb(h, s, l)
    return rgb


def tune_contrast(rgb, bg, target):
    """把颜色调到**刚好不低于** target 的对比度（从低对比那一侧逼近）。

    ensure_contrast 只保证"至少 target"—— 颜色可能比目标重得多：
    种子皮肤的 --border 之前是按 1.6 目标的，但实际落在 2.7:1，
    比 GitHub Primer 的 1.45 / Linear 的 1.15~1.26 重了整整一档。
    边框要的是"落在某一档"而不是"至少某一档"，所以这里扫明度、
    停在**刚好还达标**的那一档上。

    扫全明度区间，取**达标且对比度最小**的那一档：
    对比度随明度单调（浅底上越亮越低、深底上越亮越高），
    所以"最小但仍达标"在浅底上是能达标的最亮一档、在深底上是最暗一档。
    两种方向不用分别处理。
    """
    h, s, _ = rgb_to_hsl(*rgb)
    best = None
    best_v = None
    l = 0.0
    while l <= 1.0001:
        c = hsl_to_rgb(h, s, l)
        v = contrast(c, bg)
        if v >= target and (best_v is None or v < best_v):
            best, best_v = c, v
        l += 0.005
    if best is not None:
        return best
    # 这一档的饱和度到 l=1 也够不着（深底 + 低饱和度），退到最亮的一档
    return hsl_to_rgb(h, s, 1.0)


def hexs(rgb):
    return "#%02x%02x%02x" % rgb


# ---------------------------------------------------------------- 种子 → 参数
def derive(seed):
    """同一个种子 → 同一组参数（用 sha256 做确定性伪随机源）。"""
    rnd = _DetRandom(seed)

    hue = rnd.rand(0, 359)                       # 品牌主色相
    hue2 = (hue + rnd.choice([150, 180, 210, -30, -60, 40]) + 360) % 360   # 第二强调色（互补/邻近）
    neutral_hue = (hue + rnd.rand(-25, 25) + 360) % 360                  # 中性色的冷暖
    chroma = rnd.pick([0.22, 0.28, 0.34, 0.42, 0.55, 0.68])            # 强调色饱和度

    radius = rnd.pick([0, 0, 2, 4, 6, 8, 10, 12, 14])                 # 圆角基值
    radius_scale = rnd.pick([1.0, 1.0, 0.75, 1.5])                    # 圆角梯度陡度
    border_w = rnd.pick([1, 1, 1, 1])                                 # 描边基宽：统一 1px 发丝线
    shadow_level = rnd.pick([0, 0, 1, 2, 3])                          # 阴影强度档
    body_fs = rnd.pick([13, 13.5, 14, 14, 14.5, 15])                 # 正文字号基线
    density = rnd.pick([0.9, 0.95, 1.0, 1.0, 1.05, 1.1])            # 间距缩放
    tint = rnd.pick([0.02, 0.03, 0.04, 0.05, 0.06])                   # 底色带一点主色相
    bg_l = rnd.pick([0.975, 0.98, 0.985, 0.99])                       # 底色明度
    dark = rnd.pick([True, False, False])                             # 是否走深色底
    return dict(
        hue=hue, hue2=hue2, neutral_hue=neutral_hue, chroma=chroma,
        radius=radius, radius_scale=radius_scale, border_w=border_w,
        shadow_level=shadow_level, body_fs=body_fs, density=density,
        tint=tint, bg_l=bg_l, dark=dark,
    )


class _DetRandom:
    """由种子驱动的确定性随机源（不依赖 random 的全局状态，保证可复现）。"""

    def __init__(self, seed):
        self._h = hashlib.sha256(str(seed).encode("utf-8")).digest()
        self._i = 0

    def _next(self):
        if self._i >= len(self._h):
            self._h = hashlib.sha256(self._h).digest()
            self._i = 0
        b = self._h[self._i]
        self._i += 1
        return b

    def rand(self, a, b):
        return a + (self._next() / 255.0) * (b - a)

    def pick(self, seq):
        return seq[self._next() % len(seq)]

    def choice(self, seq):
        return seq[self._next() % len(seq)]


# ---------------------------------------------------------------- 生成 CSS
def build_tokens(p):
    """把参数变成一整套令牌（并保证对比度）。"""
    if p["dark"]:
        bg = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.7, 0.11)
        raised = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.7, 0.15)
        hover = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.7, 0.20)
        surface = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.7, 0.13)
        selected = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.7, 0.24)
        text = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.04, 0.90), raised, 4.5, darker=False)
        muted = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.66), raised, 4.5, darker=False)
        faint = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.55), raised, 4.5, darker=False)
        border = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.32), raised, 1.35)
        border_strong = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.44), raised, 1.95)
        border_control = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.56), raised, 3.10)
        accent = ensure_contrast(hsl_to_rgb(p["hue"], p["chroma"], 0.62), raised, 4.5, darker=False)
        accent_deep = hsl_to_rgb(p["hue"], min(0.75, p["chroma"] + 0.12), 0.74)
        accent_text = ensure_contrast(hsl_to_rgb(p["hue"], p["chroma"], 0.70), raised, 4.5, darker=False)
        accent_ink = ensure_contrast(hsl_to_rgb(p["hue"], p["chroma"], 0.10), accent, 4.5, darker=True)
        danger = ensure_contrast(hsl_to_rgb(6, 0.60, 0.66), raised, 4.5, darker=False)
        amber = ensure_contrast(hsl_to_rgb(40, 0.70, 0.66), raised, 4.5, darker=False)
        green = ensure_contrast(hsl_to_rgb(152, 0.55, 0.62), raised, 4.5, darker=False)
        tool = ensure_contrast(hsl_to_rgb(p["hue2"], 0.55, 0.66), raised, 4.5, darker=False)
        ink = text
        # 状态色**作底**时的字色：深底皮肤上状态色是浅色 → 字要深；反之要浅。
        state_ink = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.05, 0.10), danger, 4.5, darker=True)
    else:
        bg = hsl_to_rgb(p["neutral_hue"], p["tint"], p["bg_l"])
        raised = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.6, min(0.995, p["bg_l"] + 0.012))
        hover = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.8, max(0.0, p["bg_l"] - 0.05))
        surface = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.5, p["bg_l"] + 0.008)
        selected = hsl_to_rgb(p["neutral_hue"], p["tint"] * 0.9, max(0.0, p["bg_l"] - 0.03))
        # 浅底上文字是深色 → 对**越暗**的面板对比度越低。bg 比 raised 暗一档
        # （bg_l vs bg_l+0.012），所以解算的基准面必须取 bg 而不是 raised ——
        # 早先取 raised，解出来的 --text-faint 压在 --bg 上只有 4.41:1，
        # 棘轮当场报出"新引入的皮肤泄漏"。三套固定皮（paper/graphite/zinc）
        # 的手算值是对 --bg 验的，生成器应当跟它们一个基准。
        # 深底那一支反过来了：文字是浅色，对**越亮**的面板对比度越低，
        # 而 bg/surface/raised 里最亮的就是 raised —— 那一支本来就对。
        ref = bg
        text = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.12, 0.14), ref, 4.5, darker=True)
        muted = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.10, 0.40), ref, 4.5, darker=True)
        faint = ensure_contrast(hsl_to_rgb(p["neutral_hue"], 0.08, 0.50), ref, 4.5, darker=True)
        border = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.08, 0.62), ref, 1.35)
        # 边框三档，对齐 GitHub Primer / Linear 的实际取值：
        #   --border         容器外框 / 分隔线  1.35:1  装饰性，1.4.11 豁免
        #   --border-strong  强调分隔 / 弹层框  1.95:1
        #   --border-graphic 交互控件边界     3.10:1  1.4.11 真正管的这一档
        border_strong = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.08, 0.50), ref, 1.95)
        border_control = tune_contrast(hsl_to_rgb(p["neutral_hue"], 0.08, 0.40), ref, 3.10)
        accent = ensure_contrast(hsl_to_rgb(p["hue"], p["chroma"], 0.42), ref, 4.5, darker=True)
        accent_deep = ensure_contrast(hsl_to_rgb(p["hue"], min(0.75, p["chroma"] + 0.12), 0.33), ref, 4.5, darker=True)
        accent_text = ensure_contrast(hsl_to_rgb(p["hue"], p["chroma"], 0.36), ref, 4.5, darker=True)
        accent_ink = ensure_contrast(hsl_to_rgb(0, 0, 1), accent, 4.5, darker=False)
        danger = ensure_contrast(hsl_to_rgb(6, 0.62, 0.42), ref, 4.5, darker=True)
        amber = ensure_contrast(hsl_to_rgb(40, 0.72, 0.36), ref, 4.5, darker=True)
        green = ensure_contrast(hsl_to_rgb(152, 0.55, 0.34), ref, 4.5, darker=True)
        tool = ensure_contrast(hsl_to_rgb(p["hue2"], 0.55, 0.40), ref, 4.5, darker=True)
        ink = text
        # 状态色**作底**时的字色：浅底皮肤上状态色是深色 → 字要浅。
        state_ink = ensure_contrast(hsl_to_rgb(0, 0, 1), danger, 4.5, darker=False)

    r = p["radius"]
    rs = p["radius_scale"]

    def rr(base, k):
        return int(round(base * (k ** rs)))

    def fs(base):
        return round(base * (p["body_fs"] / 14.0), 1)

    # 投影色：浅底皮肤用 --ink（近黑）；**深底皮肤的 --ink 是浅色**，拿它当投影
    # 会在深底上"提亮"（违反"硬投影必须比自己落的底暗"的不变量），所以深底用近黑常量。
    shadow_color = "#05070a" if p["dark"] else "var(--ink)"
    shadow = {
        0: "none",
        1: "0 1px 2px %s" % shadow_color,
        2: "0 2px 6px %s" % shadow_color,
        3: "0 6px 18px %s" % shadow_color,
    }[p["shadow_level"]]

    s1 = int(round(4 * p["density"]))
    s2 = int(round(8 * p["density"]))
    s3 = int(round(12 * p["density"]))
    s4 = int(round(16 * p["density"]))
    s5 = int(round(24 * p["density"]))
    s6 = int(round(32 * p["density"]))

    return {
        "bg": hexs(bg), "surface": hexs(surface), "raised": hexs(raised),
        "hover": hexs(hover), "selected": hexs(selected),
        "text": hexs(text), "muted": hexs(muted), "faint": hexs(faint),
        "ink": hexs(ink), "border": hexs(border),
        "border_strong": hexs(border_strong), "border_control": hexs(border_control),
        "accent": hexs(accent), "accent_deep": hexs(accent_deep),
        "accent_text": hexs(accent_text), "accent_ink": hexs(accent_ink),
        "danger": hexs(danger), "amber": hexs(amber), "green": hexs(green), "tool": hexs(tool),
        "state_ink": hexs(state_ink),
        "radius": r, "r_xs": rr(r, 0.4), "r_sm": rr(r, 0.7), "r_md": r, "r_lg": rr(r, 1.4),
        "border_w": p["border_w"], "shadow": shadow,
        "fs_body": fs(14), "fs_sm": fs(12), "fs_cap": fs(11), "fs_md": fs(13),
        "fs_h3": fs(15), "fs_h2": fs(16), "fs_lead": fs(18), "fs_h1": fs(22),
        "s1": s1, "s2": s2, "s3": s3, "s4": s4, "s5": s5, "s6": s6,
        "measure": int(round(760 * p["density"])),
    }


def render_css(seed, p, t):
    name = "种子 %s" % seed
    L = []
    L.append("/* =============================================================")
    L.append("   Agent 工作台 · 种子皮肤（由 tools/design_seed.py 生成，**不要手改**）")
    L.append("")
    L.append("   种子：%s" % seed)
    L.append("   派生参数：%s" % json.dumps(p, ensure_ascii=False, sort_keys=True))
    L.append("")
    L.append("   换观感：python tools/design_seed.py --new-seed")
    L.append("   回到原样：删掉本文件（skins.ts 里的 seed 项同步去掉）")
    L.append("   ============================================================= */")
    L.append("")
    L.append(":root:root:root[data-skin='seed'] {")
    L.append("  --skin-dark: %d;" % (1 if p["dark"] else 0))
    L.append("")
    L.append("  /* 表面 */")
    for k in ("bg", "surface", "raised", "hover", "selected"):
        L.append("  --%s: %s;" % ({"bg": "bg", "surface": "surface", "raised": "bg-raised",
                                   "hover": "bg-hover", "selected": "bg-selected"}[k], t[k]))
    L.append("")
    L.append("  /* 墨与文字 */")
    for k in ("text", "muted", "faint", "ink", "border"):
        L.append("  --%s: %s;" % ({"text": "text", "muted": "text-muted", "faint": "text-faint",
                                   "ink": "ink", "border": "border"}[k], t[k]))
    L.append("  /* 描边三档：容器/分隔（装饰，1.4.11 豁免）· 强调 · 控件边界（1.4.11 管） */")
    L.append("  --border-strong: %s;" % t["border_strong"])
    L.append("  --border-graphic: %s;" % t["border_control"])
    L.append("")
    L.append("  /* 强调 */")
    L.append("  --accent: %s;" % t["accent"])
    L.append("  --accent-loud: %s;" % t["accent"])
    L.append("  --accent-deep: %s;" % t["accent_deep"])
    L.append("  --accent-text: %s;" % t["accent_text"])
    L.append("  --accent-ink: %s;" % t["accent_ink"])
    L.append("  --accent-2: %s;" % t["accent"])
    L.append("  --accent-2-ink: %s;" % t["accent_ink"])
    L.append("  --accent-soft: color-mix(in srgb, var(--accent) 12%, transparent);")
    L.append("  --tool: %s;" % t["tool"])
    L.append("")
    L.append("  /* 状态 */")
    L.append("  --danger: %s;" % t["danger"])
    L.append("  --danger-ink: %s;" % t["state_ink"])
    L.append("  --amber: %s;" % t["amber"])
    L.append("  --amber-ink: %s;" % t["state_ink"])
    L.append("  --green: %s;" % t["green"])
    L.append("  --green-ink: %s;" % t["state_ink"])
    L.append("")
    L.append("  /* 圆角 / 描边 / 阴影 */")
    L.append("  --r-xs: %dpx; --r-sm: %dpx; --r-md: %dpx; --r-lg: %dpx; --r-full: 999px;" %
             (t["r_xs"], t["r_sm"], t["r_md"], t["r_lg"]))
    L.append("  --radius-sm: %dpx; --radius-md: %dpx; --radius-lg: %dpx; --radius-xl: %dpx; --radius-pill: 999px;" %
             (t["r_sm"], t["r_md"], t["r_lg"], t["r_lg"]))
    L.append("  --modal-radius: %dpx;" % t["r_md"])
    L.append("  --edge-w: %dpx; --edge-w-strong: %dpx; --edge-w-heavy: %dpx;" %
             (t["border_w"], t["border_w"], t["border_w"]))
    L.append("  --edge-ink: var(--border);")
    # 装饰框档：chip / 次要按钮 / 图标按钮上那些「元素自带底色或文字」的框。
    # 种子皮肤是产品风（不是 brutalist 那两套），所以这档整个关掉 ——
    # 少画线、用留白和底色差分识别组件，结构线（顶栏底线/侧栏边/输入框边界）照留。
    L.append("  --edge-ink-soft: transparent;")
    L.append("  --edge-ink-soft-off: transparent;")
    L.append("  --chip-edge: transparent;")
    # 无框控件不投影：图标按钮这类既没有底色也没有框的元素，硬投影等于画出一个
    # 看不见的矩形，纸底上读起来还是一条明确的线。种子皮肤的装饰框关掉了，
    # 控件的投影也就跟着关掉（卡片/弹层的投影是另一档，不受影响）。
    L.append("  --shadow-control: none;")
    L.append("  --shadow-pop: %s;" % t["shadow"])
    L.append("  --shadow-soft: %s;" % t["shadow"])
    L.append("  --modal-shadow: %s;" % t["shadow"])
    L.append("  --sh-hard: %s;" % t["shadow"])
    L.append("  --sh-hard-sm: %s;" % t["shadow"])
    L.append("  --sh-hard-lg: %s;" % t["shadow"])
    L.append("")
    L.append("  /* 字阶（基线由种子决定） */")
    L.append("  --fs-body: %spx; --fs-md: %spx; --fs-sm: %spx; --fs-cap: %spx;" %
             (t["fs_body"], t["fs_md"], t["fs_sm"], t["fs_cap"]))
    L.append("  --fs-h2: %spx; --fs-h3: %spx; --fs-lead: %spx; --fs-h1: %spx; --fs-display: 30px;" %
             (t["fs_h2"], t["fs_h3"], t["fs_lead"], t["fs_h1"]))
    L.append("  --fs-micro: %spx;" % t["fs_cap"])
    L.append("")
    L.append("  /* 间距栅格 */")
    L.append("  --s1: %dpx; --s2: %dpx; --s3: %dpx; --s4: %dpx; --s5: %dpx; --s6: %dpx;" %
             (t["s1"], t["s2"], t["s3"], t["s4"], t["s5"], t["s6"]))
    L.append("  --gutter: %dpx;" % t["s4"])
    L.append("}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--new-seed", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--out", default=OUT_CSS)
    args = ap.parse_args()

    seed = None
    if os.path.exists(SEED_FILE):
        try:
            seed = json.load(open(SEED_FILE, encoding="utf-8")).get("seed")
        except Exception:
            seed = None
    if args.new_seed or seed is None:
        import secrets
        seed = secrets.randbelow(10 ** 9)
    if args.seed is not None:
        seed = args.seed

    p = derive(seed)
    t = build_tokens(p)
    css = render_css(seed, p, t)

    if args.show:
        print("seed =", seed)
        print(json.dumps(p, ensure_ascii=False, indent=2))
        print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(css)
    with open(SEED_FILE, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"seed": seed, "params": p}, f, ensure_ascii=False, indent=2)
    print("种子 %s → %s" % (seed, os.path.relpath(args.out, ROOT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
