"""页面交互原语：让 Agent 真的点得动、填得动本机网页，而不是靠猜。

vibecoding 的循环到这里只有一半：`preview_project` 能【看】（截图 + console + 失败请求），
但没有任何输入原语，所以「点一下才复现」的 bug 只能靠模型想象。本模块补另一半：
真实浏览器里的 click / type / press / wait / read / locate，走 CDP `Input.*`，页面拿到的
是原生事件（Vue/React 的 `input`/`click` 监听会正常触发）。

## 为什么要"会话"

一次 `open` 之后浏览器必须活着：否则每个动作都重新起一个 Edge（2~4s），而且页面状态会丢
（表单填了下一步就没了）。会话持有 `visual_acceptance.browser_session()` —— **不自己 Popen、
不引 playwright**：浏览器必须出生在 kill-on-close 作业对象里（见 HANDOFF 的 99°C 漏浏览器
事故），父进程被硬杀时由操作系统收走整棵树。空闲会话按 TTL 在下次调用时回收，进程退出时
`atexit` 再兜一道，绝不把 headless Edge 漏在机器上。

## 每个动作都要留下可复核的证据

「点了但没生效」和「报对了其实没跑」是同一类事故，所以：

* 定位只认 CSS 选择器或可见文本；命中的元素会被记在页面里的 `window.__docmindTarget`，
  后续动作直接作用在**那一个**元素上，不再用"重新猜一个选择器"的方式二次定位。
* **多个命中默认拒绝**（要点第几个就显式给 `nth`）；零面积/隐藏/禁用/中心不在视口内的元素
  直接拒——不替用户点一个看不见的东西。
* 动作前记一组信号（DOM 变更计数、console 错误数、失败请求数、运行时异常数、地址与标题），
  动作后比对，得出 `changed` / `navigated` / `error_seen` / `no_change`。没变化就如实说
  没变化，绝不写「成功」。
* 等待条件超时 = 未通过，并附上当时看到的状态。

## 边界

* 只允许【本机回环】地址；这不是上网浏览器。
* 不暴露任意 JS 执行入口（那等于把沙箱交出去）：所有页面脚本都是本文件里的固定常量，参数
  以 JSON 字面量传入，绝不拼接。
* 页面上的按钮可能真的会改你的项目或发请求——那是你的开发服务，不是沙箱里的玩具。
"""
from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_runtime import visual_acceptance as visual
from agent_runtime import url_policy

MAX_SESSIONS = 3
IDLE_TTL_SECONDS = 600.0
MAX_WAIT = 30.0
MAX_VALUE_CHARS = 4_000
MAX_READ_CHARS = 4_000
#: `mode: contains` 的三个上限：要搜的词、一次最多扫多少字符、回几段上下文。
#: 上限存在不是为了省 CPU，是为了【让"没找到"这句话可被核实】——扫不完就必须说没扫完。
MAX_NEEDLE_CHARS = 200
MAX_SCAN_CHARS = 200_000
MAX_SAMPLES = 3
MAX_CANDIDATES = 8
MAX_SETTLE = 5.0

#: 按键名 → (key, code, windowsVirtualKeyCode, 文本)。只认这张表，不接受任意键序列或组合键注入。
KEYS: dict[str, tuple[str, str, int, str]] = {
    "enter": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, "\t"),
    "escape": ("Escape", "Escape", 27, ""), "esc": ("Escape", "Escape", 27, ""),
    "backspace": ("Backspace", "Backspace", 8, ""), "delete": ("Delete", "Delete", 46, ""),
    "arrowup": ("ArrowUp", "ArrowUp", 38, ""), "arrowdown": ("ArrowDown", "ArrowDown", 40, ""),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""), "arrowright": ("ArrowRight", "ArrowRight", 39, ""),
    "home": ("Home", "Home", 36, ""), "end": ("End", "End", 35, ""),
    "pageup": ("PageUp", "PageUp", 33, ""), "pagedown": ("PageDown", "PageDown", 34, ""),
    "space": (" ", "Space", 32, " "),
}

WAIT_STATES = ("present", "absent", "visible", "hidden", "text")
READ_MODES = ("text", "value", "attr", "html", "count", "contains")


class PageActionError(RuntimeError):
    """页面动作无法安全执行，或无法证实它执行了。"""


# ---- 页面脚本（固定常量；参数用 JSON 字面量传入，绝不拼接用户输入） --------------

PROBE_JS = r"""() => {
  const state = window.__docmindPage;
  if (!state || state.doc !== document) {
    const fresh = {doc: document, mutations: 0, observer: false};
    window.__docmindPage = fresh;
    try {
      new MutationObserver((records) => { fresh.mutations += records.length; })
        .observe(document.documentElement,
                 {subtree: true, childList: true, attributes: true, characterData: true});
      fresh.observer = true;
    } catch (err) { /* 装不上观察器时 changed 判定退回地址/标题，但必须让调用方知道降级了 */ }
  }
  return {mutations: window.__docmindPage.mutations,
          observer: !!window.__docmindPage.observer};
}"""

# 解析定位 → 把选中的那一个记进 window.__docmindTarget，供后续动作直接使用。
RESOLVE_JS = r"""(spec) => {
  const list = [];
  const seen = new Set();
  const push = (el, how) => {
    if (!el || seen.has(el) || list.length >= 64) return;
    seen.add(el);
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    list.push({
      el: el, how: how, tag: el.tagName.toLowerCase(),
      text: ((el.innerText !== undefined ? el.innerText : (el.textContent || '')) + ' '
             + (el.value || '') + ' ' + (el.getAttribute('aria-label') || '')).trim().slice(0, 120),
      id: el.id || '', testid: el.getAttribute('data-testid') || '',
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height),
      visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
               && style.display !== 'none' && Number(style.opacity) !== 0,
    });
  };
  window.__docmindTarget = null;
  if (spec.selector) {
    document.querySelectorAll(spec.selector).forEach((el) => push(el, 'selector'));
  } else {
    const needle = spec.text.toLowerCase();
    const scopeNodes = spec.scope ? Array.from(document.querySelectorAll(spec.scope)) : [document];
    const texts = [];
    for (const host of scopeNodes) {
      (host || document).querySelectorAll(
        'a,button,input,textarea,select,label,[role=button],[onclick],[tabindex]:not([tabindex="-1"])')
        .forEach((el) => {
          const own = ((el.innerText || el.textContent || '') + ' ' + (el.value || '') + ' '
                       + (el.getAttribute('aria-label') || '') + ' '
                       + (el.getAttribute('placeholder') || '')).trim().toLowerCase();
          if (own.includes(needle)) texts.push([el, own]);
        });
      if (texts.length) break;
    }
    // 两级匹配：先要"整串相等"，只有它歧义/落空时才退到子串。
    // 单级子串会把「加一」匹配到「吃掉加一按钮」，然后点错东西还报生效。
    const exact = texts.filter(([, own]) => own === needle);
    (exact.length ? exact : texts).forEach(([el]) => push(el, exact.length ? 'text-exact' : 'text-part'));
  }
  const chosen = spec.nth < list.length ? list[spec.nth] : null;
  if (chosen && (list.length === 1 || spec.nth !== null)) {
    window.__docmindTarget = chosen.el;
    chosen.el.scrollIntoView({block: 'center', inline: 'center'});
    const after = chosen.el.getBoundingClientRect();
    chosen.x = Math.round(after.x); chosen.y = Math.round(after.y);
  }
  return {count: list.length, nth: spec.nth,
          matched: chosen ? (({el, ...rest}) => rest)(chosen) : null,
          candidates: list.slice(0, 8).map(({el, ...rest}) => rest),
          view: {width: window.innerWidth, height: window.innerHeight},
          href: location.href, title: document.title || '', ready: document.readyState};
}"""

TARGET_JS = r"""(spec) => {
  const el = window.__docmindTarget;
  if (!el || !document.contains(el)) return {ok: false, reason: 'element-gone'};
  el.focus && el.focus();
  if (spec.select) {
    if ('setSelectionRange' in el) { try { el.select(); } catch (err) {} }
  }
  if (spec.replace && typeof el.value === 'string') {
    el.value = '';
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  const rect = el.getBoundingClientRect();
  return {ok: true, tag: el.tagName.toLowerCase(), focused: document.activeElement === el,
          type: (el.getAttribute('type') || '').toLowerCase(),
          value: typeof el.value === 'string' ? el.value.slice(0, 200) : '',
          x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2)};
}"""

INSPECT_JS = r"""(spec) => {
  const pick = () => {
    if (spec.target === 'held' && window.__docmindTarget && document.contains(window.__docmindTarget))
      return window.__docmindTarget;
    const list = spec.selector ? Array.from(document.querySelectorAll(spec.selector)) : [];
    return list.length > (spec.nth || 0) ? list[spec.nth || 0] : null;
  };
  const el = pick();
  const list = spec.selector ? Array.from(document.querySelectorAll(spec.selector)) : [];
  const out = {count: list.length, href: location.href, title: document.title || ''};
  if (!el) { out.found = false; return out; }
  out.found = true;
  out.tag = el.tagName.toLowerCase();
  const rect = el.getBoundingClientRect();
  out.visible = rect.width > 0 && rect.height > 0
                && getComputedStyle(el).visibility !== 'hidden';
  out.disabled = !!el.disabled;
  if (spec.mode === 'attr') out.text = String(el.getAttribute(spec.attr || '') ?? '');
  else if (spec.mode === 'value') out.text = typeof el.value === 'string' ? el.value : '';
  else if (spec.mode === 'html') out.text = el.innerHTML || '';
  else if (spec.mode === 'count') out.text = String(list.length);
  else out.text = (el.innerText !== undefined ? el.innerText : (el.textContent || '')) || '';
  // full_length 必须在切之前算：以前这里先 slice(0,4000)，Python 再拿 len(text) > 4000 判
  // "有没有截断"——那个条件结构上永远为假，`truncated` 是个死字段，读了一半的页面
  // 看起来像读完了。
  out.full_length = String(out.text).length;
  out.text = String(out.text).slice(0, 4000);
  return out;
}"""

# mode: contains —— 在【指定容器】的可见文字里搜一个词，只回命中数与周围一小段。
# 存在的理由：`read` 的其它模式都把整段文本交给调用方，而 4000 字上限会让"这句话
# 在不在页面上"退化成"它在前 4000 字里在不在"。这里把判断下推到页面里，返回量与
# 页面大小无关，缺席断言才谈得上可靠。
# 可见性必须【显式】处理：`innerText` 对不在渲染树里的元素（`display:none` 及其子树）
# 会退回 `textContent`，实测 `#hid` 照样搜得到——不分开计数的话「页面上没有这句话」
# 会悄悄变成「DOM 里没有这句话」。所以命中分 `hits`（可见）与 `hidden_hits`（不可见）报。
CONTAINS_JS = r"""(spec) => {
  const needle = String(spec.needle || '');
  const limit = spec.limit || 200000;
  const maxSamples = spec.samples;          // 上限只由 Python 下发，JS 不再自带一套默认值
  const nodes = Array.from(document.querySelectorAll(spec.selector || 'body'));
  const out = {needle: needle, selector: spec.selector || 'body', elements: nodes.length,
               elements_with_hit: 0, hits: 0, hidden_hits: 0, hidden_elements: 0,
               searched_chars: 0, scanned_all: true, samples: [],
               href: location.href, title: document.title || ''};
  if (!needle) { out.scanned_all = false; out.error = 'empty needle'; return out; }
  const countIn = (hay) => {
    let at = hay.indexOf(needle), n = 0;
    const grabbed = [];
    while (at >= 0) {
      n += 1;
      if (grabbed.length < maxSamples) grabbed.push(
        hay.slice(Math.max(0, at - 40), at + needle.length + 40));
      at = hay.indexOf(needle, at + needle.length);
      if (n >= 500) break;                                    // 一个元素里刷屏也要说清
    }
    return {n: n, grabbed: grabbed, ran: n >= 500};
  };
  for (const el of nodes) {
    if (out.searched_chars >= limit) { out.scanned_all = false; break; }
    const hay = String(el.innerText !== undefined ? el.innerText : (el.textContent || ''));
    out.searched_chars += Math.min(hay.length, limit - out.searched_chars);
    const hidden = el.getClientRects().length === 0;         // 不在渲染树里＝用户看不见
    const seen = countIn(hay);
    if (hidden) { out.hidden_hits += seen.n; out.hidden_elements += 1; }
    else {
      out.hits += seen.n;
      if (seen.n) out.elements_with_hit += 1;
      for (const row of seen.grabbed) {
        if (out.samples.length < maxSamples) out.samples.push(row);
      }
    }
    if (seen.ran) out.scanned_all = false;
  }
  return out;
}"""


#: 公网专用的「就绪」判据：**readyState 不是 'loading' 就算就绪**。
#:
#: 为什么不用回环那条（`readyState === 'complete'`）：本机预览要等 bundle +
#: `/api/config` 才算真能用，所以必须等 complete。但**公网站点没有这个语义** ——
#: 长轮询、流式响应、埋点请求都会让 readyState 永远停在 'interactive'。
#: 实测 python.org 就是这样：等 complete 等到 socket 超时（25s 的 ready 预算和
#: 25s 的接收超时赛跑，socket 先断），整次 open 直接失败 —— 可页面其实早就能读能点了。
#:
#: 这个脚本**不抛异常**：超时就返回当前 readyState，由调用方如实报告"还在加载"，
#: 而不是把一次成功的打开说成失败。
PUBLIC_READY_JS = (
    "(async()=>{const d=%(budget_ms)d,s=50;"
    "for(let t=0;t<d;t+=s){if(document.readyState!=='loading')return document.readyState;"
    "await new Promise(r=>setTimeout(r,s))}return document.readyState})()"
)


def _evaluate(devtools: Any, source: str, spec: dict[str, Any]) -> Any:
    return devtools.evaluate("(%s)(%s)" % (source, json.dumps(spec or {}, ensure_ascii=True)))


def _target_spec(selector: Any = "", text: Any = "", nth: Any = None,
                 scope: Any = "") -> dict[str, Any]:
    selector = str(selector or "").strip()
    text = str(text or "").strip()
    scope = str(scope or "").strip()
    if selector and text:
        raise PageActionError("selector 与 text 二选一，不能同时给（两个定位互相矛盾时要点哪个不明确）。")
    if not selector and not text:
        raise PageActionError("定位需要 selector: <CSS> 或 text: <可见文字> 之一。")
    if selector.startswith(("<", "javascript:", "@")):
        raise PageActionError("selector 看起来不是 CSS 选择器：%r" % selector[:60])
    if len(selector) > 400 or len(scope) > 400:
        raise PageActionError("选择器太长（上限 400 字符）。")
    if len(text) > 200:
        raise PageActionError("text 太长（上限 200 字符）。")
    # `nth: 0` 是【显式要第一个】，与"没写 nth"必须区分开：以前两者都变成 0，
    # 于是 nth: 0 也被"命中多个，未指定 nth"拒掉，调用方无论怎么写都点不到第一项。
    given = nth not in ("", None)
    if not given:
        nth = 0
    try:
        index = int(nth)
    except (TypeError, ValueError):
        raise PageActionError("nth 必须是整数序号。") from None
    if not 0 <= index <= 99:
        raise PageActionError("nth 超出范围（0~99）。")
    return {"selector": selector, "text": text, "nth": index, "scope": scope,
            "nth_given": given}


def _describe(found: dict[str, Any], spec: dict[str, Any]) -> str:
    rows = found.get("candidates") or []
    lines = ["定位 %r 未执行：%s（命中 %s 个）" % (spec.get("selector") or spec.get("text"),
                                                 found.get("reason") or "原因未知",
                                                 found.get("count"))]
    for index, row in enumerate(rows[:MAX_CANDIDATES]):
        lines.append("  [%d] <%s> %r %sx%s @%s,%s 可见=%s 禁用=%s" % (
            index, row.get("tag"), str(row.get("text"))[:40], row.get("w"), row.get("h"),
            row.get("x"), row.get("y"), row.get("visible"), row.get("disabled")))
    if (found.get("count") or 0) > 1:
        lines.append("多个命中时用 nth: <序号> 指定要哪一个。")
    return "\n".join(lines)


def _resolve(devtools: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """定位并把元素交给页面记账；任何歧义或不可见都在这里拒掉，错误信息带候选清单。"""
    found = _evaluate(devtools, RESOLVE_JS, spec) or {}
    count = int(found.get("count") or 0)
    if not count:
        found["reason"] = "没有找到匹配元素"
        raise PageActionError(_describe(found, spec))
    if count > 1 and not spec["nth_given"]:
        found["reason"] = "命中多个，未指定 nth"
        raise PageActionError(_describe(found, spec))
    matched = found.get("matched")
    if not matched:
        found["reason"] = "nth 超出命中数量（0~%d）" % (count - 1)
        raise PageActionError(_describe(found, spec))
    if not matched.get("visible"):
        found["reason"] = "元素不可见（零面积或 hidden）"
        raise PageActionError(_describe(found, spec))
    if matched.get("disabled"):
        found["reason"] = "元素处于禁用状态"
        raise PageActionError(_describe(found, spec))
    view = found.get("view") or {}
    center_x = int(matched["x"]) + int(matched["w"]) // 2
    center_y = int(matched["y"]) + int(matched["h"]) // 2
    if not (0 <= center_x <= int(view.get("width") or 0)) or \
            not (0 <= center_y <= int(view.get("height") or 0)):
        found["reason"] = "元素中心不在视口内（%d,%d / 视口 %sx%s）" % (
            center_x, center_y, view.get("width"), view.get("height"))
        raise PageActionError(_describe(found, spec))
    found["center"] = [center_x, center_y]
    return found


# ---- 会话持有 -------------------------------------------------------------------

class _Session:
    def __init__(self, page_id: str, stack: contextlib.ExitStack, devtools: Any, root: Path,
                 url: str, cleanup: dict, server: Any, thread: Any,
                 domain: str = "loopback") -> None:
        self.id = page_id
        self.stack = stack
        self.devtools = devtools
        self.root = root
        self.url = url
        self.cleanup = cleanup
        self.server = server
        self.thread = thread
        # 会话的**访问域**。'loopback' = 只许本机回环（dev_page_action 用），
        # 'public' = 只许公网（内置浏览器用）。两者互不相通：回环会话不能被公网工具
        # 拿去点，公网会话也不能被回环工具当成本机页面——否则"只允许本机"这条
        # 护栏就成了一个可以绕过的建议。
        # 名字叫 domain 而不是 scope：click/read 的 scope 是"限定定位的 CSS 范围"，
        # 两个含义挤在一个参数名上迟早会被用错。
        self.domain = domain
        self.last_used = time.monotonic()
        self.opened_at = time.monotonic()
        self.counter = 0
        # 上一张截图的摘要。`_verdict` 只看 DOM 变更数 / console / 失败请求 /
        # 地址 / 标题，对 **canvas 类页面**（Godot/WebGL 游戏、地图、可视化）
        # 永远是 no_change —— 实测那次按了方向键，三张截图大小各不相同
        # （14914 / 14728 / 14445 字节），工具仍回报「没有观察到变化」，
        # Agent 只能拿它当负面证据，最后任务结论悬空。
        # 现在同一会话内记住上一张摘要，动作后比一次：字节不同即画面变了。
        self.last_shot_digest = ""

    def close(self) -> dict[str, Any]:
        # cleanup 是 browser_session 在 finally 里填的：必须等 stack.close() 之后再取快照，
        # 先拷就永远是个空字典，「作业对象已解绑」这条凭据会退化成一句空话。
        with contextlib.suppress(Exception):
            self.stack.close()        # browser_session 的 finally：作业对象解绑 + profile 回收
        report = dict(self.cleanup)
        if self.server is not None:
            visual.stop_static(self.server, self.thread)
            self.server = self.thread = None
        report.setdefault("spawned", True)
        return report


_sessions: dict[str, _Session] = {}
_reservations = 0          # 正在启动、还没登记的会话数：没有它就会同时放行多个浏览器
_lock = threading.RLock()
atexit.register(lambda: close_all())


def _sweep() -> list[_Session]:
    """挑出空闲会话并除名，但【不在锁里】关浏览器。

    `stack.close()` 会等作业对象解绑与 profile 回收（可达数秒），抱着全局锁做这件事会把
    别的调用一起拖住。返回的会话由调用方在锁外关闭。
    """
    now = time.monotonic()
    with _lock:
        dead = [pid for pid, row in list(_sessions.items())
                if now - row.last_used > IDLE_TTL_SECONDS]
        return [_sessions.pop(pid) for pid in dead if pid in _sessions]


def close_all() -> int:
    with _lock:
        rows = list(_sessions.values())
        _sessions.clear()
    for row in rows:
        row.close()
    return len(rows)


def live_pages() -> list[dict[str, Any]]:
    with _lock:
        return [{"page": pid, "url": row.url, "actions": row.counter,
                 "domain": row.domain,
                 "idle_seconds": round(time.monotonic() - row.last_used, 1),
                 "age_seconds": round(time.monotonic() - row.opened_at, 1)}
                for pid, row in _sessions.items()]


def _off_domain(domain: str, href: str) -> bool:
    """当前地址是否**越出了这个会话该待的范围**。

    公网会话不能停在回环/内网上，回环会话不能跑到公网去。判据按 domain 分开，
    因为"离开范围"的含义在两侧正好相反。
    """
    if str(domain) == "public":
        ok, _why = url_policy.is_public_url(href)
        return not ok
    return not _loopback(href)


def _get(page_id: str, domain: str = "") -> _Session:
    for stale in _sweep():
        stale.close()
    pid = str(page_id or "").strip()
    if not pid:
        raise PageActionError("缺少 page: <会话 id>（先 action: open）。")
    with _lock:
        row = _sessions.get(pid)
        if row is not None:
            row.last_used = time.monotonic()
    if row is None:
        raise PageActionError("找不到会话 %r：可能已超时回收或进程重启，请重新 open。" % pid)
    # 域不匹配就拒：公网工具不能操作回环会话（那会让"只允许本机"变成可以绕过的
    # 建议），回环工具也不能操作公网会话（它会把外站当成本机服务去点）。
    if domain and row.domain != domain:
        raise PageActionError(
            "会话 %r 是 %s 会话，当前工具只操作 %s 会话——两者互不相通。"
            % (pid, row.domain, domain))
    return row


# ---- 信号与生效判定 -------------------------------------------------------------

def _probe(devtools: Any) -> dict[str, Any]:
    raw = _evaluate(devtools, PROBE_JS, {})
    if isinstance(raw, dict):
        return {"mutations": int(raw.get("mutations") or 0), "observer": bool(raw.get("observer"))}
    return {"mutations": int(raw or 0), "observer": False}


def _signals(devtools: Any, page: dict[str, Any]) -> dict[str, Any]:
    """取一组可比对的计数。用去重【前】的事件计数：列表会去重，同一个报错每次动作都复现时
    列表长度差恒为 0，于是「动作后一直报错」会被看成没报错——那正是要防的假干净。"""
    return {"mutations": _probe(devtools)["mutations"],
            "console": int(getattr(devtools, "error_events", 0) or 0),
            "failed": int(getattr(devtools, "failure_events", 0) or 0),
            "runtime": int(getattr(devtools, "runtime_events", 0) or 0),
            "href": page.get("href"), "title": page.get("title")}


def _current(devtools: Any) -> dict[str, Any]:
    """读页面此刻的地址与标题。动作前的快照必须真读——硬编码空标题会让"什么都没变"
    被比对成"标题变了"，从而把 no_change 误报成生效。"""
    return _evaluate(devtools, INSPECT_JS, {"selector": "html", "mode": "count"}) or {}


def _loopback(href: Any) -> bool:
    from agent_runtime import dev_server
    _url, error = dev_server.loopback_url(str(href or ""))
    return not error


def _verdict(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    delta = {key: int(after[key]) - int(before[key])
             for key in ("mutations", "console", "failed", "runtime")}
    navigated = str(after.get("href") or "") != str(before.get("href") or "")
    retitled = str(after.get("title") or "") != str(before.get("title") or "")
    # 换文档时 PROBE_JS 会重装观察器，计数从 0 重新开始 → 差值为负。这不是"少了几个变更"，
    # 而是页面重载了：如实单独判，不把负数喂给模型当证据。
    reloaded = int(after["mutations"]) < int(before["mutations"])
    if delta["console"] > 0 or delta["failed"] > 0 or delta["runtime"] > 0:
        verdict = "error_seen"
    elif navigated:
        verdict = "navigated"
    elif reloaded:
        verdict = "reloaded"
    elif delta["mutations"] > 0 or retitled:
        verdict = "changed"
    else:
        verdict = "no_change"
    if reloaded:
        delta["mutations"] = 0
    return {"verdict": verdict, "delta": delta, "navigated": navigated}


def _capture(row: _Session, action: str) -> tuple[str, str]:
    """截图并返回 `(相对路径, 摘要)`。

    ⚠ 摘要是 sha256 前 16 位，只用于**同一会话内前后两帧比对**，不是安全用途。
    PNG 是无损且 Chromium 的 `Page.captureScreenshot` 不写时间戳，所以同一画面
    两次截图字节一致、不同画面字节不同 —— 这是一个可靠的"画面变了"信号，
    而且不需要 PIL（实测环境里就没有 PIL，那条路走不通）。
    """
    directory = row.root / ".docmind" / "page-action" / time.strftime("%Y%m%d-%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    encoded = str((row.devtools.call("Page.captureScreenshot", {"format": "png"}) or {}).get("data") or "")
    if not encoded:
        return "", ""
    raw = base64.b64decode(encoded)
    digest = hashlib.sha256(raw).hexdigest()[:16]
    path = directory / ("%s-%d.png" % (action, row.counter))
    path.write_bytes(raw)
    return path.relative_to(row.root).as_posix(), digest


def _finish(row: _Session, action: str, before: dict[str, Any], *, settle_ms: float,
            screenshot: bool, matched: dict[str, Any], **extra) -> dict[str, Any]:
    settle = max(0.1, min(MAX_SETTLE, float(settle_ms or 600) / 1000.0))
    row.devtools.drain(settle)
    page = _current(row.devtools)
    after = _signals(row.devtools, page)
    effect = _verdict(before, after)
    verdict = effect["verdict"]
    delta = effect["delta"]
    # 只有真的观察到变化才算成功。`no_change` 交给结构化消费方就是"成功"，那正是本仓库
    # 反复付学费的假干净：点了没生效必须让 ok 也是 False。
    ok = verdict in ("changed", "navigated", "reloaded")
    # ⚠⚠ canvas 类页面的补救：DOM 一点没变，但**画面变了**。
    #
    # `_verdict` 只看 DOM/地址/标题，对 Godot/WebGL 这类 canvas 页面结构性失效
    # （点击与按键都不产生 mutation）。于是"按了方向键、角色真的动了"与
    # "按了个没绑定的键、什么都没发生"回报一模一样 —— 前者是假阴性，
    # 会把 Agent 引向"点错了地方"的错误结论。
    #
    # 现在同一会话内比前后两帧的 sha256：不同即 `pixels`。只有真的截了图
    # （`screenshot=True`）才有这个信号；没截图就照旧只有 DOM 判据，不会虚增。
    shot_path, shot_digest = "", ""
    if screenshot:
        shot_path, shot_digest = _capture(row, action)
    prev_digest = row.last_shot_digest
    if shot_digest:
        row.last_shot_digest = shot_digest
    pixels_changed = bool(shot_digest and prev_digest
                          and shot_digest != prev_digest)
    if verdict == "no_change" and pixels_changed:
        verdict = "pixels"
        ok = True
    # type_text 的直接证据，与 pixels 同类：`Input.insertText` 只改 `.value`，
    # MutationObserver 结构性看不到它（实测真实浏览器里往 input 打字 mutations 恒 0），
    # 于是「文本已落入」在纯 DOM 判据下永远是 no_change。type_text 已回读证实
    # value_landed——这是比 DOM 计数更直接的证据，不能因此判假阴性
    # （否则 Agent 收到 ok:false 会去重试或回报「填不进去」，而字其实已经在框里）。
    value_landed = bool(extra.get("value_landed"))
    if verdict == "no_change" and value_landed:
        verdict = "typed"
        ok = True
    # 越界判定按会话域来：公网会话一旦被页面重定向到内网/回环，就是越界；
    # 回环会话跑到公网也是越界（"只允许本机"不能只是句提醒）。
    left_scope = _off_domain(row.domain, page.get("href"))
    left_loopback = (not _loopback(page.get("href"))) if row.domain != "public" else False
    if left_scope or verdict == "error_seen":
        ok = False
    console_new = [item for item in row.devtools.console if item.get("level") == "error"]
    if delta["console"] <= 0:
        console_new = []
    failed_new = list(row.devtools.failed_requests) if delta["failed"] > 0 else []
    result = {"action": action, "page": row.id, "effect": verdict, "delta": delta, "ok": ok,
              "element": matched, "left_loopback": left_loopback, "left_scope": left_scope,
              "url": page.get("href"), "title": page.get("title"),
              "screenshot": shot_path,
              # 给结构化消费方一个可判的依据：比了哪两帧、是否真有两帧。
              # 没有上一帧（本会话第一次截图）时 `prev_shot` 为空 ——
              # 这时**不能**说"画面没变"，只能说不构成证据。
              "shot_digest": shot_digest, "prev_shot": prev_digest,
              "pixels_changed": pixels_changed,
              "console_errors": console_new[-3:], "failed_requests": failed_new[-3:]}
    result.update(extra)
    return result


def _settle_ms(raw: Any) -> float:
    try:
        value = float(raw if raw not in ("", None) else 600)
    except (TypeError, ValueError):
        raise PageActionError("settle 必须是毫秒数。") from None
    if not 0 <= value <= MAX_SETTLE * 1000:
        raise PageActionError("settle 超出范围（0~%d 毫秒）。" % int(MAX_SETTLE * 1000))
    return value


# ---- 对外动作 -------------------------------------------------------------------

def open_page(root: str | Path, *, url: str = "", entry: str = "", timeout: float = 25.0,
              flags: Any = ()) -> dict[str, Any]:
    """打开一个页面并持有会话。`url` 只允许本机回环；不给 url 就用项目里的 HTML 入口起临时静态站。"""
    global _reservations
    base = Path(root).resolve()
    if not base.is_dir():
        raise PageActionError("当前项目目录不存在。")
    for stale in _sweep():
        stale.close()
    with _lock:
        # 名额必须在【启动之前】占住：只看已登记会话的话，两个并发 open 各自看到 2/3
        # 就会一起起浏览器，上限形同虚设。
        if len(_sessions) + _reservations >= MAX_SESSIONS:
            raise PageActionError(
                "同时最多持有 %d 个页面会话（每个都是一整个浏览器进程）。先 action: close 回收，"
                "当前在会话：%s。" % (MAX_SESSIONS, ", ".join(row["page"] for row in live_pages())))
        _reservations += 1
    cleanup: dict[str, Any] = {}
    stack = contextlib.ExitStack()
    page_id = uuid.uuid4().hex[:8]
    server = thread = None
    try:
        target, entry_path = visual.preview_target(base, entry, url)
        if not target:
            server, thread = visual.serve_static(base)
            target = "http://127.0.0.1:%d/%s" % (server.server_port,
                                                 entry_path.relative_to(base).as_posix())
        devtools = stack.enter_context(contextlib.contextmanager(_session_gen)(
            timeout, visual.sanitize_flags(flags), cleanup))
        page_url = "%s%sdocmind=%d" % (target, "&" if "?" in target else "?", time.time_ns())
        devtools.call("Page.navigate", {"url": page_url})
        # 预算跟着 `timeout` 走（有上限），不再写死 120 次 × 50ms = 6 秒 ——
        # 那 6 秒装不下「加载 bundle + 等 GET /api/config（含 Ollama 探活冷路径）」。
        # 预算只有一份，在 visual_acceptance.page_ready_js。
        devtools.evaluate(visual.page_ready_js(timeout))
        devtools.drain(0.5)
        probe = _probe(devtools)
        page = _evaluate(devtools, INSPECT_JS, {"selector": "html", "mode": "count"}) or {}
    except Exception:
        with contextlib.suppress(Exception):
            stack.close()
        if server is not None:
            visual.stop_static(server, thread)
        with _lock:
            _reservations -= 1
        raise
    row = _Session(page_id, stack, devtools, base, page_url, cleanup, server, thread,
                   domain="loopback")
    with _lock:
        # 登记与释放在同一个锁里做完：中间留空隙的话，并发 open 会短暂看到"既没会话也没名额"
        # 然后又多起一个浏览器。
        _sessions[page_id] = row
        _reservations -= 1
    console_errors = [item for item in devtools.console if item["level"] == "error"]
    return {"action": "open", "page": page_id, "url": page_url, "href": page.get("href"),
            "title": page.get("title"), "ready": page.get("ready"),
            "elements": page.get("count"), "mutation_observer": bool(probe["observer"]),
            "console_errors": console_errors, "runtime_errors": list(devtools.runtime_errors),
            "failed_requests": list(devtools.failed_requests)}


def _session_gen(timeout: float, flags: list[str], cleanup: dict):
    """把唯一的浏览器启动通道变成可持有的生成器——启动路径仍然只有一条。"""
    with visual.browser_session(timeout, flags, cleanup=cleanup) as devtools:
        yield devtools


def open_external_page(root: str | Path, *, url: str, domain: str = "public",
                       timeout: float = 25.0, flags: Any = ()) -> dict[str, Any]:
    """打开一个**公网**地址并持有会话（内置浏览器用）。

    与 `open_page` 的区别有三处，每一处都是有意为之：

    1. **地址校验走 `url_policy.public_url`**（只允许公网），而不是回环校验。
       内网地址、内网域名后缀、解析到内网的域名、云元数据地址一律拒绝。
    2. **不起临时静态站**：公网地址不需要本机文件服务，`serve_static` 在这里
       只会白起一个线程。
    3. **URL 不追加 `docmind=<ns>` 缓存穿透参数**：那个参数是给本机预览用的，
       加到别人的网址上会改变对方看到的 URL（有些站点会据此走不同分支）。

    其余（浏览器启动通道、会话名额、TTL 回收、证据判定）与本机预览完全共用，
    所以"浏览器怎么被起、怎么被收"只有一条实现。
    """
    global _reservations
    base = Path(root).resolve()
    if not base.is_dir():
        raise PageActionError("当前项目目录不存在。")
    target, error = url_policy.public_url(url)
    if error:
        raise PageActionError(error)
    for stale in _sweep():
        stale.close()
    with _lock:
        # 名额必须在启动之前占住：只看已登记会话的话，两个并发 open 各自看到 2/3
        # 就会一起起浏览器，上限形同虚设。
        if len(_sessions) + _reservations >= MAX_SESSIONS:
            raise PageActionError(
                "同时最多持有 %d 个页面会话（每个都是一整个浏览器进程）。先 action: close 回收，"
                "当前在会话：%s。" % (MAX_SESSIONS, ", ".join(row["page"] for row in live_pages())))
        _reservations += 1
    cleanup: dict[str, Any] = {}
    stack = contextlib.ExitStack()
    page_id = uuid.uuid4().hex[:8]
    try:
        devtools = stack.enter_context(contextlib.contextmanager(_session_gen)(
            timeout, visual.sanitize_flags(flags), cleanup))
        base_timeout = max(10.0, float(timeout))
        # 导航回包的等待要比就绪预算更宽：站点慢时 Chromium 会迟迟不给
        # Page.navigate 的回包（实测 python.org 就是），两者赛跑的话先断的一定是
        # socket，而页面其实已经在加载了。
        devtools.set_timeout(base_timeout + 20.0)
        navigate_note = ""
        try:
            devtools.call("Page.navigate", {"url": target})
        except Exception as exc:
            # 导航回包超时 ≠ 页面没打开。轮询一次当前地址：救得回来就继续用，
            # 否则整次打开失败 —— 而模型只看到一句"连接超时"，既不知道是哪个站、
            # 也不知道是该重试还是该放弃。
            navigate_note = "导航回包超时（%s），已改用当前页面状态继续。" % type(exc).__name__
            devtools.set_timeout(5.0)
            landed_probe = ""
            try:
                landed_probe = str(devtools.evaluate(
                    "() => (document.location && document.location.href) || ''") or "")
            except Exception:
                landed_probe = ""
            if not landed_probe or landed_probe.lower() == "about:blank":
                raise PageActionError(
                    "打开 %s 超时：站点在 %.0f 秒内没有响应（网络慢、不可达或被拦）。"
                    "换个站点或稍后重试。" % (target, base_timeout + 20.0))
        # 公网页面的就绪判据比本机宽松（见 PUBLIC_READY_JS 的说明），并且
        # **探针超时不致命**：页面可能还在加载，但它已经能读能点了 —— 那就继续用。
        devtools.set_timeout(base_timeout + 15.0)
        ready_state = ""
        try:
            ready_state = str(devtools.evaluate(PUBLIC_READY_JS % {
                "budget_ms": int(visual.page_ready_budget(timeout) * 1000)}) or "")
        except Exception:
            # 就绪探针失败不是致命伤：地址策略已经在前面验过了，页面也已经导航过去了。
            # 记下来，后面如实报告"就绪状态未知"，而不是让模型以为页面打不开。
            ready_state = ""
        finally:
            devtools.set_timeout(base_timeout)
        devtools.drain(0.5)
        probe = _probe(devtools)
        page = _evaluate(devtools, INSPECT_JS, {"selector": "html", "mode": "count"}) or {}
    except Exception:
        with contextlib.suppress(Exception):
            stack.close()
        with _lock:
            _reservations -= 1
        raise
    row = _Session(page_id, stack, devtools, base, target, cleanup, None, None, domain=domain)
    with _lock:
        _sessions[page_id] = row
        _reservations -= 1
    console_errors = [item for item in devtools.console if item["level"] == "error"]
    # 打开之后**再核一次**当前地址：目标站可能把我们重定向到内网（比如某个
    # 单点登录的内部跳转）。这时必须如实报出来，而不是当成功。
    landed = str(page.get("href") or "")
    off_scope = _off_domain(domain, landed)
    return {"action": "open", "page": page_id, "url": target, "href": landed,
            "title": page.get("title"), "ready": ready_state,
            "elements": page.get("count"), "mutation_observer": bool(probe["observer"]),
            "console_errors": console_errors, "runtime_errors": list(devtools.runtime_errors),
            "failed_requests": list(devtools.failed_requests),
            "domain": domain, "off_scope": off_scope,
            "navigate_note": navigate_note,
            # 重定向后的地址与请求地址不一致，是模型最容易忽略的一件事：它会以为
            # 自己还在原来那个站点上，于是把跳转后的内容当成原站点的话来引用。
            "redirected": bool(landed) and landed.rstrip("/") != target.rstrip("/")}


def click(root: str | Path, *, page: str = "", selector: str = "", text: str = "",
          nth: Any = None,
          scope: str = "", settle: float = 600, screenshot: bool = True,
          domain: str = "loopback") -> dict[str, Any]:
    """真实点击：定位 → 滚进视口 → CDP 鼠标按下/抬起 → 比对是否真的发生了变化。"""
    row = _get(page, domain)
    spec = _target_spec(selector, text, nth, scope)
    row.counter += 1
    found = _resolve(row.devtools, spec)
    matched = found["matched"]
    center_x, center_y = found["center"]
    before = _signals(row.devtools, found)
    for params in ({"type": "mouseMoved", "x": center_x, "y": center_y, "buttons": 0},
                   {"type": "mousePressed", "x": center_x, "y": center_y, "button": "left",
                    "clickCount": 1, "buttons": 1},
                   {"type": "mouseReleased", "x": center_x, "y": center_y, "button": "left",
                    "clickCount": 1, "buttons": 0}):
        row.devtools.call("Input.dispatchMouseEvent", params)
    return _finish(row, "click", before, settle_ms=settle, screenshot=screenshot,
                   matched=matched, at=[center_x, center_y])


def type_text(root: str | Path, *, page: str = "", value: str = "", selector: str = "",
              text: str = "", nth: Any = None, scope: str = "", replace: bool = True,
              settle: float = 600, screenshot: bool = False, allow_password: bool = False,
              domain: str = "loopback") -> dict[str, Any]:
    """往【刚才定位到的那个】元素里插文本：走 `Input.insertText`，Vue/React 的 input 监听会收到。"""
    row = _get(page, domain)
    payload = str(value if value is not None else "")
    if len(payload) > MAX_VALUE_CHARS:
        raise PageActionError("输入内容超过 %s 字符上限。" % MAX_VALUE_CHARS)
    spec = _target_spec(selector, text, nth, scope)
    row.counter += 1
    found = _resolve(row.devtools, spec)
    matched = found["matched"]
    before = _signals(row.devtools, found)
    held = _evaluate(row.devtools, TARGET_JS, {"replace": bool(replace), "select": bool(replace)}) or {}
    if not held.get("ok"):
        raise PageActionError("元素在定位之后消失了（%s），没有输入任何内容。" % held.get("reason"))
    # 密码框护栏：往 `type=password` 的输入框里打字，等于把凭据交给这个网站。
    # 本机开发服务里不存在这种框，所以这条只对**外部站点**有意义 —— 但也正因为
    # 外部站点是不可控的一方（页面内容可能是敌意输入，会诱导模型"把密码填上去"），
    # 这里要求调用方**显式**声明，而不是让它默认发生。要登录请让用户自己操作。
    if str(held.get("type") or "").lower() == "password" and not allow_password:
        raise PageActionError(
            "目标是密码框（type=password），已拒绝输入。往外部站点的密码框里写凭据"
            "必须是用户明确的动作——如果确实要登录，请让用户自己在浏览器里输入；"
            "确实要由本工具写入时，显式声明 allow_password: true。")
    if payload:
        row.devtools.call("Input.insertText", {"text": payload})
    # 回读要按元素实际承载内容的方式取：`<input>` 的 innerText 恒为空，只看 text 就等于
    # "插了字却没法证实插进去了"。value 优先，落空才退回可见文本。
    read = _evaluate(row.devtools, INSPECT_JS, {"target": "held", "mode": "value"}) or {}
    seen = str(read.get("text") or "")
    if not seen.strip():
        read = _evaluate(row.devtools, INSPECT_JS, {"target": "held", "mode": "text"}) or {}
        seen = str(read.get("text") or "")
    return _finish(row, "type", before, settle_ms=settle, screenshot=screenshot,
                   matched=matched, focused=bool(held.get("focused")),
                   value_seen=seen[:200],
                   # 空 payload 时 `"" in seen` 恒真——那等于「什么都没做也算成功」。
                   value_landed=bool(payload) and payload in seen)


def press_key(root: str | Path, *, page: str = "", key: str = "", selector: str = "",
              settle: float = 600, screenshot: bool = False,
              domain: str = "loopback") -> dict[str, Any]:
    """按一个键（只认固定键表）。给了 selector 就先把焦点放到那个元素上。"""
    row = _get(page, domain)
    name = str(key or "").strip().lower()
    if name not in KEYS:
        raise PageActionError("按键 %r 不在支持列表里。支持：%s；组合键请改用 click 或 type。"
                              % (key, "/".join(sorted(set(KEYS)))))
    logical, code, vk, typed = KEYS[name]
    matched: dict[str, Any] = {"tag": "document", "text": logical}
    if str(selector or "").strip():
        found = _resolve(row.devtools, _target_spec(selector, "", 0, ""))
        matched = found["matched"]
        held = _evaluate(row.devtools, TARGET_JS, {"replace": False, "select": False}) or {}
        if not held.get("ok"):
            raise PageActionError("元素在定位之后消失了（%s），没有按键。" % held.get("reason"))
    row.counter += 1
    before = _signals(row.devtools, _current(row.devtools))
    base = {"key": logical, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    row.devtools.call("Input.dispatchKeyEvent", {**base, "type": "keyDown", "text": typed})
    row.devtools.call("Input.dispatchKeyEvent", {**base, "type": "keyUp"})
    return _finish(row, "press", before, settle_ms=settle, screenshot=screenshot,
                   matched=matched, key=logical)


def wait_for(root: str | Path, *, page: str = "", selector: str = "", state: str = "present",
             value: str = "", timeout: float = 4.0,
             domain: str = "loopback") -> dict[str, Any]:
    """等一个条件成立。超时就是未通过，并附上当时看到的状态与新出现的报错。"""
    row = _get(page, domain)
    kind = str(state or "present").strip().lower()
    if kind not in WAIT_STATES:
        raise PageActionError("state 只支持 %s，收到 %r。" % ("|".join(WAIT_STATES), state))
    css = str(selector or "").strip()
    if not css and kind != "text":
        raise PageActionError("%s 需要 selector: <CSS>。" % kind)
    if len(css) > 400:
        raise PageActionError("选择器太长（上限 400 字符）。")
    try:
        budget = max(0.1, min(MAX_WAIT, float(timeout if timeout not in ("", None) else 4.0)))
    except (TypeError, ValueError):
        raise PageActionError("timeout 必须是秒数。") from None
    started = time.monotonic()
    deadline = started + budget
    seen: dict[str, Any] = {}
    spec = {"selector": css, "mode": "text" if kind == "text" else "count", "attr": ""}
    while True:
        seen = _evaluate(row.devtools, INSPECT_JS, spec) or {}
        count = int(seen.get("count") or 0)
        hit = ((kind == "present" and count >= 1)
               or (kind == "absent" and count == 0)
               or (kind == "visible" and count >= 1 and bool(seen.get("visible")))
               or (kind == "hidden" and (count == 0 or not seen.get("visible")))
               or (kind == "text" and str(value or "") in str(seen.get("text") or "")))
        if hit:
            return {"action": "wait", "page": row.id, "ok": True, "state": kind,
                    "waited_ms": round((time.monotonic() - started) * 1000),
                    "seen": {key: seen.get(key) for key in ("count", "found", "visible", "text")},
                    "url": seen.get("href")}
        if time.monotonic() >= deadline:
            row.devtools.drain(0.3)
            return {"action": "wait", "page": row.id, "ok": False, "state": kind,
                    "timed_out": True, "waited_ms": round((time.monotonic() - started) * 1000),
                    "wanted": str(value or css)[:200],
                    "seen": {key: seen.get(key) for key in ("count", "found", "visible", "text")},
                    "console_errors": [item for item in row.devtools.console
                                       if item["level"] == "error"],
                    "failed_requests": list(row.devtools.failed_requests)}
        time.sleep(0.1)


def read_page(root: str | Path, *, page: str = "", selector: str = "", mode: str = "text",
              attr: str = "", nth: Any = None, held: bool = False,
              needle: str = "", domain: str = "loopback") -> dict[str, Any]:
    row = _get(page, domain)
    kind = str(mode or "text").strip().lower()
    if kind not in READ_MODES:
        raise PageActionError("mode 只支持 %s，收到 %r。" % ("|".join(READ_MODES), mode))
    if kind == "attr" and not str(attr or "").strip():
        raise PageActionError("mode: attr 需要 attr: <属性名>。")
    if len(str(attr or "")) > 60:
        raise PageActionError("属性名太长。")
    css = str(selector or "").strip() or ("body" if kind == "contains" else "html")
    if kind == "contains":
        return _read_contains(row, css, str(needle or ""))
    index = 0 if held else _target_spec(css, "", nth, "")["nth"]
    out = _evaluate(row.devtools, INSPECT_JS,
                    {"selector": "" if held else css,
                     "target": "held" if held else "", "mode": kind, "attr": str(attr or "").strip(),
                     "nth": index}) or {}
    text = str(out.get("text") or "")
    full = out.get("full_length")
    # 双端核对：页面报了全文长度就用它判，没报（老页面/别的实现）才退回字符串长度。
    truncated = (int(full) > len(text)) if isinstance(full, (int, float)) \
        else len(text) > MAX_READ_CHARS
    return {"action": "read", "page": row.id, "ok": True, "mode": kind, "count": out.get("count"),
            "found": out.get("found"), "visible": out.get("visible"), "tag": out.get("tag"),
            "title": out.get("title"), "url": out.get("href"), "text": text[:MAX_READ_CHARS],
            "full_length": int(full) if isinstance(full, (int, float)) else len(text),
            "truncated": truncated}


def _read_contains(row: _Session, css: str, needle: str) -> dict[str, Any]:
    """在 `selector` 容器的可见文字里搜 `needle`：只回命中数与周围几段，不回整页文本。"""
    if not needle.strip():
        raise PageActionError("mode: contains 需要 needle: <要找的词>。")
    if len(needle) > MAX_NEEDLE_CHARS:
        raise PageActionError("needle 太长（上限 %d 字符）。" % MAX_NEEDLE_CHARS)
    _target_spec(css, "", None, "")            # 容器仍走同一道选择器围栏
    out = _evaluate(row.devtools, CONTAINS_JS,
                    {"selector": css, "needle": needle, "limit": MAX_SCAN_CHARS,
                     "samples": MAX_SAMPLES}) or {}
    elements = int(out.get("elements") or 0)
    hits = int(out.get("hits") or 0)
    scanned_all = bool(out.get("scanned_all"))
    return {"action": "read", "page": row.id, "ok": True, "mode": "contains",
            "needle": needle, "selector": css,
            "found": hits > 0, "hits": hits, "elements": elements,
            "elements_with_hit": int(out.get("elements_with_hit") or 0),
            # 不可见容器里的命中单独报：混进 `hits` 会让「页面上没有」变成「DOM 里没有」，
            # 完全丢掉则会让"用户看得见的那句"被误判成不存在
            "hidden_hits": int(out.get("hidden_hits") or 0),
            "hidden_elements": int(out.get("hidden_elements") or 0),
            "searched_chars": int(out.get("searched_chars") or 0), "scanned_all": scanned_all,
            # 样本限量已经由 `spec.samples` 在页面里生效，这里不再二次裁剪：两处各一套上限时，
            # 调一处另一处仍按老数字静默截断（变异电池实测：删掉这里的 [:MAX_SAMPLES] 谁都发现不了）
            "samples": list(out.get("samples") or []),
            "title": out.get("title"), "url": out.get("href"), "count": elements,
            "text": "", "truncated": not scanned_all, "error": str(out.get("error") or "")}


def locate(root: str | Path, *, page: str = "", selector: str = "", text: str = "",
           nth: Any = None, scope: str = "", domain: str = "loopback") -> dict[str, Any]:
    """只看不动：这个定位会命中什么、可不可见、中心在哪个坐标。歧义也照样列出来。"""
    row = _get(page, domain)
    spec = _target_spec(selector, text, nth, scope)
    found = dict(_evaluate(row.devtools, RESOLVE_JS, spec) or {})
    found["ambiguous"] = int(found.get("count") or 0) > 1 and not spec["nth_given"]
    return {"action": "locate", "page": row.id, "ok": True, **found}


def close_page(page: str = "", domain: str = "") -> dict[str, Any]:
    pid = str(page or "").strip()
    with _lock:
        row = _sessions.get(pid)
        if row is not None and domain and row.domain != domain:
            raise PageActionError("会话 %r 是 %s 会话，当前工具只回收 %s 会话。"
                                  % (pid, row.domain, domain))
    with _lock:
        row = _sessions.pop(pid, None)
    if row is None:
        return {"action": "close", "ok": False, "page": pid, "error": "找不到会话（可能已回收）。",
                "live": live_pages()}
    report = row.close()
    reaped = report.get("job_closed") is True and report.get("profile_released") is True
    return {"action": "close", "page": pid, "ok": reaped, "cleanup": report, "live": live_pages()}


def render(result: dict[str, Any]) -> str:
    action = str(result.get("action") or "")
    if action == "open":
        lines = ["页面会话已打开：page=%s" % result.get("page"),
                 "地址：%s" % result.get("url"), "标题：%s" % result.get("title")]
        if result.get("console_errors"):
            lines.append("打开时就有 %d 条 console 错误（先按报错修，别继续点）：%s" % (
                len(result["console_errors"]),
                " / ".join(str(item.get("text"))[:120] for item in result["console_errors"][:3])))
        if result.get("failed_requests"):
            lines.append("打开时就有 %d 个失败请求：%s" % (
                len(result["failed_requests"]),
                " / ".join(str(item.get("url"))[:80] for item in result["failed_requests"][:3])))
        lines.append("后续动作都带 page: %s；用完 action: close 回收。" % result.get("page"))
        return "\n".join(lines)
    if action == "wait":
        return "等待 %s %s（等了 %sms）；当时看到 %s。" % (
            result.get("state"), "成立" if result.get("ok") else "【未成立：超时】",
            result.get("waited_ms"), json.dumps(result.get("seen"), ensure_ascii=False)[:240])
    if result.get("mode") == "contains":
        lines = ["页面搜索 %r：在 %s 的【可见】文字里命中 %s 处（扫了 %s 字符，覆盖 %s/%s 个元素）" % (
            result.get("needle"), result.get("selector") or "body", result.get("hits"),
            result.get("searched_chars"), result.get("elements_with_hit"),
            result.get("elements"))]
        if not result.get("elements"):
            # 最坏的一种"没找到"：容器本身根本不存在，这句「页面上没有」等于没说
            lines.append("注意：容器一个都没匹配到——这次搜索不构成任何证据。")
        elif int(result.get("hidden_hits") or 0):
            lines.append("另有 %s 处在【当前不可见】的元素里（%s 个元素）：按「用户看得见」判定时"
                         "不计入命中。" % (result.get("hidden_hits"),
                                        result.get("hidden_elements")))
        elif not result.get("scanned_all"):
            lines.append("注意：没扫完（超出上限就停了），所以「没找到」不能当成「没有」。")
        for row in (result.get("samples") or []):
            lines.append("  命中处：%s" % str(row).replace("\n", " ⏎ ")[:180])
        return "\n".join(lines)
    if action == "read":
        return "读取 %s：命中 %s 个，%s%s" % (
            result.get("mode"), result.get("count"),
            json.dumps(result.get("text"), ensure_ascii=False),
            "（已截断）" if result.get("truncated") else "")
    if action == "locate":
        lines = ["定位命中 %s 个%s（视口 %s）：" % (
            result.get("count"), "【歧义：未指定 nth】" if result.get("ambiguous") else "",
            json.dumps(result.get("view"), ensure_ascii=False))]
        for index, row in enumerate(result.get("candidates") or []):
            lines.append("  [%d] <%s> %r %sx%s @%s,%s 可见=%s 禁用=%s" % (
                index, row.get("tag"), str(row.get("text"))[:40], row.get("w"), row.get("h"),
                row.get("x"), row.get("y"), row.get("visible"), row.get("disabled")))
        return "\n".join(lines)
    if action == "close":
        cleanup = result.get("cleanup") or {}
        if not result.get("ok"):
            if result.get("error"):
                return "会话 %s 【没有回收到东西】：%s；仍在会话：%s。" % (
                    result.get("page"), result["error"],
                    ", ".join(row["page"] for row in result.get("live") or []) or "无")
            return "会话 %s 收尾【不完整】：作业对象解绑=%s，profile 回收=%s——浏览器可能还活着，" \
                   "请检查 msedge 进程。" % (result.get("page"), cleanup.get("job_closed"),
                                          cleanup.get("profile_released"))
        return "会话 %s 已回收（作业对象解绑=True，profile 回收=True）；仍在会话：%s。" % (
            result.get("page"), ", ".join(row["page"] for row in result.get("live") or []) or "无")
    if action == "list":
        rows = result.get("live") or []
        if not rows:
            return "在途页面会话 0 个。"
        lines = ["在途页面会话 %d 个：" % len(rows)]
        for row in rows:
            lines.append("  %s  idle %ss  动作 %s 次  %s" % (
                row["page"], row["idle_seconds"], row["actions"], str(row["url"])[:90]))
        return "\n".join(lines)
    element = result.get("element") or {}
    delta = result.get("delta") or {}
    outcome = {"changed": "生效（DOM 变了）", "navigated": "生效（页面跳转了）",
               "reloaded": "生效（文档被重新加载）",
               "error_seen": "【未通过】动作后出现新报错", "no_change": "【没有观察到变化】",
               # canvas 类页面（Godot/WebGL/地图）点击与按键都不产生 DOM 变更，
               # 旧判据下"真的动了"和"什么都没发生"回报一模一样。
               "pixels": "生效（画面变了，DOM 没变 —— canvas 类页面看这个）",
               # 输入框的 value 不走 MutationObserver，DOM 判据结构性看不到；
               # 这一条以回读到的 value 为准。
               "typed": "生效（文本已填入，回读对得上）"} \
        .get(result.get("effect"), str(result.get("effect")))
    lines = ["%s：%s —— DOM %+d，console %+d，失败请求 %+d；当前地址 %s。" % (
        action, outcome, delta.get("mutations", 0), delta.get("console", 0),
        delta.get("failed", 0), str(result.get("url"))[:120])]
    lines.append("作用元素：<%s> %r %sx%s%s" % (
        element.get("tag"), str(element.get("text"))[:40], element.get("w"), element.get("h"),
        "  中心=%s" % result.get("at") if result.get("at") else ""))
    if result.get("screenshot"):
        lines.append("截图：%s" % result["screenshot"])
    if result.get("value_seen") is not None:
        lines.append("元素里现在是 %r（%s）" % (str(result.get("value_seen"))[:60],
                                          "内容对得上" if result.get("value_landed")
                                          else "【没有看到预期的文本】"))
    for item in (result.get("console_errors") or [])[:3]:
        lines.append("新 console 错误：%s" % str(item.get("text"))[:200])
    for item in (result.get("failed_requests") or [])[:3]:
        lines.append("新失败请求：%s（%s）" % (str(item.get("url"))[:120], item.get("kind")))
    if result.get("left_loopback"):
        # 回环会话跑到了公网：沿用既有措辞「已离开本机」——
        # 它是这条护栏一直以来的说法，别在没有理由的时候改掉（既有测试守着它，
        # 而且模型/文档已经按这句话理解）。
        lines.append("【已离开本机】当前地址不是回环地址：%s——这个会话不再受"
                     "「只访问本机」约束，请 close 后重新 open 本机地址。" % str(result.get("url"))[:120])
    elif result.get("left_scope"):
        # 公网会话越界（被页面重定向到内网/回环）：这不是"结果不好看"，是**这个会话
        # 已经不在它该待的地方**，继续操作它就等于让模型在内网里点东西 ——
        # 所以明说，并要求关掉重来。
        lines.append("【已离开允许的访问域】当前地址 %s 越出了这个会话的范围"
                     "（公网会话只许公网）——请 action: close 回收后重新 open 合法地址。"
                     % str(result.get("url"))[:120])
    if result.get("effect") == "no_change":
        lines.append("没有观察到任何变化：可能点到的不是你要的控件，或这个控件本来就不改 DOM（"
                     "这时用 read/locate 取具体状态再判断，不要当成成功）。")
        # ⚠ 只有"截了图且拿不到上一帧"时才提醒。没有上一帧时，
        # 「画面没变」这句**不构成证据** —— 必须说清，否则又是一次假否定。
        if result.get("screenshot") and not result.get("prev_shot"):
            lines.append("注意：这是本会话第一张截图，没有上一帧可比 —— "
                         "所以「画面没变」这次不构成证据。再做一个带 screenshot: true "
                         "的动作，才能比出画面是否真的变了。")
    if result.get("effect") == "pixels":
        lines.append("画面变了但 DOM 没变：这类页面（Godot/WebGL 游戏、地图、可视化）"
                     "的点击与按键本就不产生 DOM 变更，所以要以这一条为准。")
    if result.get("effect") == "typed":
        lines.append("文本进去了但 DOM 没变：输入框的 value 不走 MutationObserver，"
                     "所以「没有观察到变化」这次不构成否定证据——以回读到的内容为准。")
    if result.get("shot_digest"):
        lines.append("截图摘要：%s（上一帧 %s）"
                     % (result["shot_digest"], result.get("prev_shot") or "无"))
    return "\n".join(lines)


__all__ = ["IDLE_TTL_SECONDS", "KEYS", "MAX_SESSIONS", "PageActionError", "click", "close_all",
           "close_page", "live_pages", "locate", "open_external_page", "open_page", "press_key",
           "read_page", "render", "type_text", "wait_for"]
