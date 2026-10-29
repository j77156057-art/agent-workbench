"""Wave17b：整页集成守卫——起【真后端】、开真浏览器，断言页面真的从后端拿到了数据。

`dev_vue`（Wave16/17a）守的是单个组件的接线，它明确不证明"整页集成"：组件的 props 是我
喂进去的。这一条补上剩下那段——真实 `api.py` 起服务、真实构建产物 `web/` 被伺服、
浏览器真的发出 `/api/...` 并把数据渲染出来。

**三件被实测决定了的设计**：

1. 状态根必须隔离。`config.py` 里 `STATE_ROOT = os.getenv("DOCMIND_STATE_ROOT") or BASE_DIR`，
   所以起服务时显式指到一个临时目录，整页测试就不会往仓库里的真项目登记表/索引上写。
   判据 `state_isolated` 盯两件事：临时状态根里真的落下了文件，**且** `GET /api/projects`
   只列得出这一次造的项目——漏传 env 时后面那条必红（真登记表里项目不止一个）。
2. "后端数据到了页面"必须由**这次独有的数据**证明。夹具是一个随机命名的文件
   （`probe_<6位>.py`）写进临时项目根，页面上的文件树要把它列出来才算数——
   随机名让"缓存/旧构建里恰好有这句话"没法冒充通过。
3. 构建产物的新旧必须如实报。`web/` 是 gitignore 的构建产物；如果 `frontend/src/**` 里
   有文件比 `web/**` 新，那整页跑的是**旧构建**，绿了也不代表当前代码——所以
   `bundle_built_after_latest_source` 是一条会判红的断言，不是脚注。

进程与浏览器的生命周期不自己实现：服务走 `dev_server`（`process_runner` 的 job，
Windows 上 born-in-job，父进程被杀也带得走），浏览器走 `page_action`（同一套回收），
产物目录的删除与"有没有删干净"复用 `vue_component.clean_target`。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from agent_runtime import dev_server, loopback_http, page_action, vue_component

#: 诊断文本的长度上限。此前 `error` 排在 `failed` **之后**、整个 dict 又被 `[:1200]`
#: 一刀切，于是失败判据一多（实测深链那条 20 条）就把唯一有信息量的 `error` 挤了出去 ——
#: 报出来是一串判据全红，看不出原因。这正是「红得毫无信息量」的直接来源。
DETAIL_LIMIT = 1200


def detail_text(got: dict[str, Any], **extra: Any) -> str:
    """整页守卫的失败诊断。

    `error` **排第一**且**不被截断** —— 它是唯一说清「为什么」的字段；`failed`
    逐条可能十几二十条，超预算时先砍它。
    """
    observed = got.get("observed") or {}
    parts: dict[str, Any] = {"error": got.get("error") or ""}
    parts.update(extra)
    for key in ("failed", "deep", "console", "deep_console_errors"):
        if key in parts:
            continue
        if key == "failed":
            parts["failed"] = got.get("failed")
        elif key == "deep":
            parts["deep"] = observed.get("deep_links")
        elif key == "console":
            parts["console"] = observed.get("console_errors")
        else:
            parts["deep_console_errors"] = observed.get("deep_console_errors")
    head = json.dumps(parts, ensure_ascii=False)
    if len(head) <= DETAIL_LIMIT:
        return head
    # 超预算：保住 error（它已在最前），压缩 failed 的条数。
    # 注意收尾**不能**再一刀切 —— error 自己就可能超长（实测堆栈信息两三千字），
    # 那样砍掉的又正好是唯一说清「为什么」的那一段。第一版就是这么写的，
    # 守卫 `test_detail_keeps_the_whole_error_even_when_failed_is_huge` 把它咬了出来。
    shrunk = dict(parts)
    failed = parts.get("failed") or []
    keep = max(3, DETAIL_LIMIT // 60)
    shrunk["failed"] = list(failed[:keep]) + (["…（另有 %d 条）" % (len(failed) - keep)]
                                      if len(failed) > keep else [])
    body = json.dumps(shrunk, ensure_ascii=False)
    if len(body) <= DETAIL_LIMIT:
        return body
    # 仍然超长：砍别的字段，但 **error 一个字都不许丢**。
    # 砍的顺序有讲究：先砍最不值钱的（deep/console 那几段原始观察），再砍 failed。
    # 第一版是把除 error 外的字段拼起来对半砍，结果 **failed 的「另有 N 条」被一起
    # 砍掉了** —— 而那正是「已压缩」的证据，守卫
    # `test_when_the_budget_is_tight_failed_gets_shrunk_not_error` 把它咬了出来。
    tail_parts = {k: v for k, v in shrunk.items() if k != "error"}
    # 保底：failed 至少留「前 3 条 + 另有 N 条」，否则压缩发生过也无从得知
    floor = {"failed": tail_parts["failed"][:3]
             + (["…（另有 %d 条）" % (len(tail_parts["failed"]) - 3)]
                if len(tail_parts["failed"]) > 3 else [])}
    error_len = len(json.dumps(parts["error"], ensure_ascii=False))

    def _fit(fields):
        room = DETAIL_LIMIT - error_len - len('{"error": , ') - 2
        return len(json.dumps(fields, ensure_ascii=False)) <= max(40, room)

    for droppable in ("deep", "console", "deep_console_errors", "observed"):
        if _fit(tail_parts):
            break
        tail_parts.pop(droppable, None)
    if not _fit(tail_parts):
        for keep in (10, 5, 3):
            shrunk["failed"] = list(shrunk["failed"][:keep]) + [
                "…（另有 %d 条）" % (len(failed) - keep)] if len(failed) > keep else []
            tail_parts = {k: v for k, v in shrunk.items() if k != "error"}
            if _fit(tail_parts):
                break
    if not _fit(tail_parts):
        # 最后手段：只保留 failed 的压缩形态（floor），其余全丢
        tail_parts = floor
    prefix = '{"error": '
    room = DETAIL_LIMIT - len(prefix) - 2
    keep_error = parts["error"]
    if error_len > room:
        keep_error = keep_error[:max(0, room // 2)] + "…（error 过长已截）"
    return prefix + json.dumps(keep_error, ensure_ascii=False) + ", " \
        + json.dumps(tail_parts, ensure_ascii=False)[1:]


MAX_TEXTS = 8
DEFAULT_TAB = "素材"
#: 工作区主视图收进顶栏下拉：概览 / 代码 两个 `[data-wb-view]`；素材入口在「更多」。
#: 原值 4 是「开发舱」还在时留下的 —— 那个 workspace 在 v2 重构 E 阶段已被删除
#: （`App.vue`：cockpit workspace 已删；`ChatDock.vue`：那个 scope 已不存在）。
#: 判据数的是 `[data-wb-view]` 元素个数，所以这里必须跟着 UI 的真实视图数走。
EXPECTED_TABS = 2
BUILD_DIR = "web"
SOURCE_DIR = os.path.join("frontend", "src")
#: 整页要等 uvicorn 导入完（实测冷启动 4.7s），再等 vite 产物把视图挂载
READY_WAIT = 60.0
#: 只在回环上跑：这条工具不是网页浏览器，不能拿去访问别的机器
LOOPBACK = "127.0.0.1"
_API_PATHS = ("/api/projects",)
#: 流式档要点的几步：更多 → 实时对话 → 开始。
#:
#: ⚠ 这条链路在 v2 重构 E 阶段换过一次实现：原来的「开发舱」标签页与
#: `AutonomousCockpit.vue` 已被删除（`App.vue`：cockpit workspace 已删；
#: `ChatDock.vue`：那个 scope 已不存在），现在实时对话是顶栏「更多」里的
#: **实时对话** 弹层（`LiveStreamPanel.vue`）。照旧标签去点只会「命中 0 个」。
#:
#: ⚠⚠ **每一步都必须限定作用域** —— 页面里有**同名**的按钮：
#: 素材区工具条上就有一个「更多 ▼」筛选 chip（`AssetCenterView.vue`）。
#: 不限定时文字定位命中的是它（实测：命中在 x=442,y=100 的那个 63×31 的 chip，
#: 而真正的顶栏触发器在 x=702,y=12）—— 点了没反应，还报「ok=True / changed」，
#: 因为点开的是素材区的筛选下拉，DOM 确实变了。
#: 这正是老 harness 给「开发舱」加 `.ws-bar` 限定的同一个坑，换了个标签名。
#:
#: 为什么只有三步（实测）：
#: · 「实时对话」弹层要先打开（菜单是 teleport 到 body 的，默认收起）；
#: · 画面来源默认就是「摄像头」，点它什么都不会变（所以不列为一步）；
#: · 「开始」= **画面 + 麦克风一起开**（`LiveStreamPanel`：麦克风是必需输入，
#:   只发视频时服务端会稳定断流）—— 麦克风不需要单独一次手势。
STREAM_STEPS = (("更多", ".wb-topbar"), ("实时对话", ".wb-menu"), ("开始", ".lsp"))
#: 回合文本由 `realtime_scripted` 生成：`"N 片 · M 字节 · 峰值 P"`。
CAPTION_RE = re.compile(r"(\d+)\s*片\s*·\s*(\d+)\s*字节\s*·\s*峰值\s*([0-9]*\.?[0-9]+)")
#: `.*` 加 DOTALL 会贪过整段字幕，把"第一条转写 + 最后一条轮次"配成一对——
#: 于是两轮只认出一个轮次。限在同一行内、非贪婪，才对得上每一轮自己那一句。
REPLY_RE = re.compile(r"收到\s*(\d+)\s*字节[^\n]{0,60}?第\s*(\d+)\s*轮")
STREAM_SCRIPT = "sil:0.3 talk:0.8 sil:0.3 talk:0.8 sil:1.6"
#: 字节/片数要自洽（16k mono 100ms = 3200 字节），峰值落在这个带内才算"真有人说话"：
#: 静音喂进来也能凑出数字的话，这条守卫就成了装饰。
PEAK_LO, PEAK_HI = 0.05, 1.0
#: 流式要等浏览器真的推够一轮尾静音（实测开麦后 ~3s 出第一轮）
STREAM_WAIT = 45.0
#: 单步点击的重试窗口（按钮要等 ws 握手完才存在）
STREAM_STEP_WAIT = 25.0

# --------------------------------------------------------------------------- #
# 深链整页：每条页面路由必须渲染**它自己**那一页（Wave24）
# --------------------------------------------------------------------------- #
#
#: 三条页面路由各自独立、**没有 catch-all**（`api.py` 的 `/`、`/workbench`、`/trace`
#: 各自 `FileResponse` 自己的 html）。所以"路由 A 返回了页面 B"今天不会让任何东西变红：
#: 谁把 `/trace` 接到 `index.html` 上，整页守卫照样 14 条全绿。
#:
#: 三组标记**互不相交**，于是这件事可判定：`own` 是这一页自己的挂载点，另外两页的挂载点
#: 必须一个都看不到。标记只用 CSS 选择器——`page_action` 的 `text:` 定位只在**可交互元素**
#: 里找，`AI 操作记录` 那个 `<h1>` 实测命中 0 个（Wave17 已付过这笔学费），所以改用
#: `#tablewrap` 这个结构标记。
#:
#: **这里只写 `own`，不写"另外两页是谁"** —— 那份由 `_foreign_for()` 从同一张表推出来。
#: 手写两份的话，加第 4 个页面时手写那份会静默变陈旧（§9：上限写两处就等于写了一处旧的）。
DEEP_LINK_ROUTES = (
    {"path": "/", "own": ".ask-shell"},
    {"path": "/workbench", "own": ".wb-shell"},
    {"path": "/trace", "own": "#tablewrap"},
)

#: `api.py` 给了 `/workbench.html`、`/trace.html` 两个 307 别名（为了纯静态托管也能打开）。
#: 断言它们**落在同一页**上（别名 404 或者指向别的页都是回归），并且**真的停在规范路径**——
#: 实测浏览器跟完 307 之后 href 就是 `/workbench`、连 `page_action` 加的 `?docmind=`
#: 缓存戳都掉了。只查"标记在"不够：那证不了重定向落在哪。
DEEP_LINK_ALIASES = (
    {"path": "/workbench.html", "own": ".wb-shell", "canonical": "/workbench"},
    {"path": "/trace.html", "own": "#tablewrap", "canonical": "/trace"},
)

#: 反向对照：站内不存在的路径**不许**渲染出任何一页。没有它的话，一个 catch-all
#: 把所有路径都回成 `index.html`，上面那些断言会**全部照样绿**。
DEEP_LINK_UNKNOWN = "/no-such-page-for-deeplink"

#: 单页挂载点的等待窗口（实测冷加载 1~3s；给足余量但要有截止期，不能固定 sleep）
DEEP_LINK_WAIT = 20.0

#: 工作台**数据**到位的等待窗口。
#:
#: 为什么要等数据而不是等文档：`open_page` 的就绪判据是
#: `document.readyState === 'complete'`，而工作台把 UI 门控在 `GET /api/config` 上，
#: 那条冷路径要跑一次 Ollama 探活（`api.py:4022`；`check_ollama` 自己的注释就写着
#: "Ollama 未运行时探测本身就可能耗时数秒"）。本机实测**首次 6108 毫秒、之后 15 毫秒**，
#: 于是"文档加载完"与"文件树有行"之间隔着好几秒（实测 1903ms → 5160ms）。
#: 读 facts 之前不等这一段，就是在跟那 6 秒赛跑——实测表现为"有时绿有时红"，
#: 而且**红得毫无信息量**（`rows: 0`、`console_errors: []`、`failed_requests: []`）。
WORKBENCH_DATA_WAIT = 25.0

#: 问答页那一档：提交一个问题之后，等回答里出现**那一次运行独有的凭据**。
#: 凭据是 `list_dir` 列出的代码库根里的随机夹具文件名（`probe_<6位>.py`，见 `make_sandbox`）
#: ——页面字幕里出现它，就只能解释为"整条链路真跑过"，照 §5.8「夹具是随机命名的文件」那条。
ASK_WAIT = 40.0

#: 建议问题按钮：问答页刚打开时有若干个（实测 3 个）。2026-10 改版后点它只把 prompt
#: 填进草稿（`useCapability`），不再直接发送——所以后面必须再点一次发送，才算真跑一轮。
ASK_SUGGESTION = "button.suggestion"
ASK_COMPOSER = ".composer textarea"
ASK_SEND = ".composer button.send-btn"


def _deep_slug(path: str) -> str:
    """把路径变成判据名的一截：`/` → `root`，`/workbench` → `workbench`。"""
    text = str(path or "").strip().strip("/").replace("/", "_").replace(".", "_")
    return "root" if not text else text[:32]


#: 浏览器把"请求被**客户端**取消"报成这个。它和"服务端失败"是两件事：
#: 工作台的存活探针 `probeBackend()` 自己带 `AbortController`（4s）并在 `catch` 里
#: 故意吞掉（`frontend/src/workbench/composables/demo.ts:13-31`），所以页面加载慢的时候
#: 那条 `/api/health` 会被前端自己掐掉。本机实测：health 本身 15~24 ms，连 6 条并发慢
#: `/api/config` 在飞时也只要 24 ms——所以被取消的**不是**服务端慢，是客户端不想要了。
CLIENT_CANCELLED = "ERR_ABORTED"


def split_requests(rows: Any) -> dict[str, list]:
    """把浏览器报的失败请求分成**真失败**与**客户端自己取消**两堆。

    分类而不是丢弃：两堆都进报告（`failed_requests` / `cancelled_requests`），
    判据只对真失败变红。静默扔掉就叫"假干净"，而把客户端取消也算成失败则是"假红"
    ——本仓刚为"确定性红教人忽略门禁"付过账（§9）。
    """
    failed, cancelled = [], []
    for row in rows or []:
        detail = str((row or {}).get("detail") or "")
        (cancelled if CLIENT_CANCELLED in detail else failed).append(row)
    return {"failed_requests": failed, "cancelled_requests": cancelled}


def ask_checks(observed: dict[str, Any]) -> dict[str, bool]:
    """问答页那一档的判定：整页真的走完一轮问答（页面 → `/api/chat` → ReAct → 工具 → DOM）。

    **凭据**是那一次运行独有的随机夹具文件名——它只能从 `list_dir` 的真实返回里来，
    所以"回答里出现了它"证的是整条链路，而不是"页面上写了句像样的话"。
    """
    text = str(observed.get("ask_text") or "")
    fixture = str(observed.get("ask_credential") or "")
    return {
        "ask_shell_mounted": int(observed.get("ask_shell") or 0) >= 1,
        "ask_suggestion_clicked": bool(observed.get("ask_click_ok")),
        # 建议卡只填草稿；没有真的点发送（或按钮还 disabled）时，下面等回复是白等
        "ask_submitted": bool(observed.get("ask_submit_ok")),
        # 只点一下不算：得真的出现一条回答，而且它回的是我们提交的那个问题
        "ask_question_echoed": bool(observed.get("ask_question_echoed")),
        "ask_reply_arrived": bool(fixture) and fixture in text,
        # 失败文案出现就不能算通过（HTTP 200 + 有回复 ≠ 成功，§5.18 ② 那条同款）
        "ask_reply_is_not_an_error": "本轮请求失败" not in text,
        # "这一句话没出现"要读全了才算证据：read_page 有 4000 字上限
        "ask_read_wholly": not bool(observed.get("ask_truncated")),
        "ask_console_clean": not (observed.get("ask_console_errors") or []),
        "ask_no_failed_requests": not (observed.get("ask_failed_requests") or []),
    }


def _console_matters(spec: dict) -> bool:
    """这条深链的 console/失败请求要不要算进"干净"里。

    未知路径**理应**加载失败（实测就是 1 条 console 错误 + 1 条失败请求），把它算进去的话
    `deeplink_console_clean` 永远红。抽成纯函数是为了让这条判断在 CI 上可验——
    留在 `_drive_deep_link` 里就只能靠真浏览器，而本机浏览器那一档有时序敏感。
    """
    return not bool(spec.get("unknown"))


def _href_path(href: Any) -> str:
    """从 href 里取**路径**部分——去掉 origin、`?docmind=…` 缓存戳与 `#`。

    别名那条判据要的就是它：浏览器跟完 307 之后 href 应该是规范路径，
    而 `page_action.open_page` 给每条 URL 都加了 `?docmind=<ns>`，不剥掉就永远对不上。
    只剥查询串是不够的：那样留下来的是整个 URL，比 `canonical` 多一截 origin。
    """
    text = str(href or "").strip()
    for cut in ("#", "?"):
        text = text.split(cut, 1)[0]
    if "://" in text:
        text = text.split("://", 1)[1]
        slash = text.find("/")
        text = text[slash:] if slash >= 0 else "/"
    return text.rstrip("/") or "/"


def _foreign_for(own: str) -> tuple[str, ...]:
    """"另外几页的挂载点"——按 own 反查，而不是每条手写一遍。

    手写会漏：别名那一档起初就没带 foreign，于是现场那侧数出空列表、
    `[][0]` 直接 IndexError。**从同一张表推**，别名与反向对照才拿得到同一套标记。
    """
    return tuple(spec["own"] for spec in DEEP_LINK_ROUTES if spec["own"] != own)


def deep_link_facts(read: Callable[[dict], dict]) -> list[dict[str, Any]]:
    """把每条深链页面上要看的东西读齐——`read` 由调用方注入，纯层因此能拿假页面喂。

    页面交互（开会话、等挂载、数选择器）全在 `read` 里面：现场那侧每条路由都要**自己**
    的会话，塞进这个纯函数会让它没法在 CI 上跑。

    `own_seen` / `foreign_seen` 都要**真的数过**：只看"own 在"不够——
    "A 路由吐了 B 页面"时 B 的挂载点同样在场，只查 own 会照样绿。
    """
    rows: list[dict[str, Any]] = []
    for spec in list(DEEP_LINK_ROUTES) + [dict(row, alias=True)
                                          for row in DEEP_LINK_ALIASES]:
        spec = dict(spec, foreign=_foreign_for(str(spec["own"])))
        got = read(spec)
        foreign = [int(count or 0) for count in (got.get("foreign") or [])]
        rows.append({"path": spec["path"], "own": spec["own"],
                     "alias": bool(spec.get("alias")),
                     "canonical": str(spec.get("canonical") or ""),
                     "landed": _href_path(got.get("href")),
                     "own_seen": int(got.get("own") or 0) >= 1,
                     "foreign_seen": any(count >= 1 for count in foreign),
                     "foreign_counts": foreign})
    # 反向对照：那条路径必须把**三页的标记**都数一遍。漏传 foreign 的话
    # `any([])` 恒为 False，这条判据就成了永远为真的装饰——而它恰恰是"没有 catch-all
    # 把所有路径都回成 index.html"的唯一凭据。
    every = tuple(spec["own"] for spec in DEEP_LINK_ROUTES)
    got = read({"path": DEEP_LINK_UNKNOWN, "unknown": True, "foreign": every})
    seen = [int(count or 0) for count in (got.get("foreign") or [])]
    rows.append({"path": DEEP_LINK_UNKNOWN, "unknown": True, "own_seen": False,
                 "unknown_served_a_page": any(count >= 1 for count in seen),
                 "unknown_counts": seen})
    return rows


def deep_link_checks(observed: dict[str, Any]) -> dict[str, bool]:
    """深链那一档的判定表。每一条都对应一类"看起来绿其实没测"的假象。"""
    rows = {str(row.get("path")): row for row in (observed.get("deep_links") or [])}
    out: dict[str, bool] = {}
    for spec in DEEP_LINK_ROUTES:
        row = rows.get(spec["path"]) or {}
        slug = _deep_slug(spec["path"])
        out["deeplink_%s_renders_its_own_page" % slug] = bool(row.get("own_seen"))
        out["deeplink_%s_serves_no_other_page" % slug] = bool(row.get("own_seen")) \
            and not bool(row.get("foreign_seen"))
    for spec in DEEP_LINK_ALIASES:
        row = rows.get(spec["path"]) or {}
        slug = "alias_%s" % _deep_slug(spec["path"])
        out["deeplink_%s_reaches_its_page" % slug] = bool(row.get("own_seen"))
        out["deeplink_%s_serves_no_other_page" % slug] = bool(row.get("own_seen")) \
            and not bool(row.get("foreign_seen"))
        # 重定向要真的落在规范路径上：只查"标记在"的话，停在任何带 .wb-shell 的页都算过
        out["deeplink_%s_lands_on_the_canonical_path" % slug] = bool(row) \
            and str(row.get("landed") or "") == str(spec.get("canonical") or "")
    unknown = rows.get(DEEP_LINK_UNKNOWN) or {}
    out["deeplink_unknown_path_serves_no_page"] = bool(unknown) \
        and not bool(unknown.get("unknown_served_a_page"))
    # 一行都没读到时，上面那些都会因为 `rows.get(path) or {}` 变成"没数据所以没关系"，
    # 而 console/failed 两条又会因为空列表报"没有错误"——整张表看起来还挺绿。
    # 所以显式钉一条"该读的路径都读了"。
    out["deeplink_every_route_was_visited"] = \
        len(observed.get("deep_links") or []) == len(DEEP_LINK_ROUTES) \
        + len(DEEP_LINK_ALIASES) + 1
    out["deeplink_console_clean"] = not (observed.get("deep_console_errors") or [])
    out["deeplink_no_failed_requests"] = not (observed.get("deep_failed_requests") or [])
    return out


class AppTestError(RuntimeError):
    """整页守卫无法开始。"""


def page_path(raw: str) -> str:
    """页面路径只接受站内绝对路径，且拒掉能拼出协议/穿越的写法。"""
    text = str(raw or "").strip() or "/workbench"
    if not text.startswith("/") or text.startswith("//"):
        raise AppTestError("page 要是以 / 开头的站内路径，收到：%s" % text[:120])
    if ".." in text or any(ch in text for ch in "?#\"'<>\\ \t\n"):
        raise AppTestError("page 只允许静态路径（不带查询/片段/空白），收到：%s" % text[:120])
    return text[:120]


def text_needles(raw: str) -> list[str]:
    """额外要在页面上看到的文字。整页的文本断言不是 CSS 选择器（`0 个分区` 这种要能写）。"""
    out: list[str] = []
    for item in str(raw or "").splitlines():
        clean = item.strip()
        if not clean:
            continue
        if len(clean) > 120:
            raise AppTestError("页面文本断言太长（>120 字符）：%s" % clean[:60])
        if clean not in out:
            out.append(clean)
    if len(out) > MAX_TEXTS:
        raise AppTestError("页面文本断言最多 %d 条，收到 %d 条。" % (MAX_TEXTS, len(out)))
    return out


def server_argv(port: int, *, python: str, module: str = "api:app") -> list[str]:
    """uvicorn 的 argv：端口由调用方给的临时端口，日志级别压到 warning 免得淹掉真错误。"""
    if not 1024 <= int(port) <= 65535:
        raise AppTestError("端口不在可绑定范围：%s" % port)
    return [python, "-m", "uvicorn", module, "--host", LOOPBACK, "--port", str(int(port)),
            "--log-level", "warning"]


def server_env(state_root: Path, *, realtime: bool = False,
               extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """把子进程的状态根指到隔离目录——但**先带上自己的 env**。

    `process_runner.clean_environment()` 拿到什么就用什么（不会自动并 os.environ），
    所以只传一个键会让 uvicorn 连 PATH/SYSTEMROOT 都拿不到。凭据类变量由它统一剔除。
    `realtime` 那次额外把实时适配器指到 `scripted`：零网络、零 key，但走的仍是
    生产选适配器那条路（`DOCMIND_REALTIME_PROVIDER` + 注册表）。
    `extra` 是调用方自己带的环境（问答那一档用它把 `LLM_PROVIDER=custom` +
    `LLM_BASE_URL` 指向 `llm_stub` 起的回环假服务——同样零网络、走的仍是生产 provider
    那条路）。**子进程**才是导入时读 `config` 的那个进程，所以这里用 env 是对的
    （进程内注入必须走 `config.set_runtime`，见 `llm_stub` 的文档）。
    """
    env = {**os.environ, "DOCMIND_STATE_ROOT": str(state_root)}
    if realtime:
        env["DOCMIND_REALTIME_PROVIDER"] = "scripted"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def make_sandbox(base: str | Path | None = None) -> dict[str, Any]:
    """一次性沙箱：一个临时目录里放状态根 + 项目根 + 一个随机命名的文件。

    项目在仓库外是有意的：整页测试要把"页面能读到后端能读到的真实文件"当凭据，
    拿仓库自己当项目根会让断言与仓库内容耦合（而且别人正在改的文件会随机改变结果）。
    """
    root = Path(base or Path(os.path.realpath(os.getenv("TEMP") or "/tmp")))
    sandbox = root / ("docmind-apptest-%d-%s" % (int(time.time()), uuid.uuid4().hex[:6]))
    (sandbox / "state").mkdir(parents=True)
    project = sandbox / "project"
    project.mkdir()
    fixture = "probe_%s.py" % uuid.uuid4().hex[:6]
    (project / fixture).write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    return {"sandbox": sandbox, "state": sandbox / "state", "project": project,
            "fixture": fixture}


def bundle_freshness(root: str | Path) -> dict[str, Any]:
    """构建产物是否比最新源码旧——旧的话整页测的就不是现在这份代码。"""
    base = Path(root)
    build, source = base / BUILD_DIR, base / SOURCE_DIR
    if not (build / "index.html").is_file():
        return {"built": False, "fresh": False, "newest_build": 0.0, "newest_source": 0.0,
                "stale_by": 0, "detail": "%s 里没有 index.html（前端没构建）" % build}
    newest_build = _newest_mtime(build)
    newest_source = _newest_mtime(source)
    return {"built": True, "fresh": newest_source <= newest_build,
            "newest_build": newest_build, "newest_source": newest_source,
            "stale_by": int(max(0.0, newest_source - newest_build)),
            "detail": "最新源码 %s / 最新产物 %s" % (
                _stamp(newest_source), _stamp(newest_build))}


def _newest_mtime(directory: Path) -> float:
    newest = 0.0
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".tmp", "__pycache__")]
        for name in filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    return newest


def _stamp(value: float) -> str:
    return time.strftime("%m-%d %H:%M:%S", time.localtime(value)) if value else "无"


def http_json(url: str, payload: dict[str, Any] | None = None, *,
              timeout: float = 20.0) -> tuple[int, Any]:
    """只对回环地址发一个请求；解析不出 JSON 就如实报错，不猜。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise AppTestError("只允许访问本机回环地址，收到 %r。" % parsed.hostname)
    if parsed.path not in _API_PATHS:
        raise AppTestError("只允许 %s，收到 %r。" % ("、".join(_API_PATHS), parsed.path))
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method="POST" if body else "GET",
                                     headers={"Content-Type": "application/json"})
    try:
        with loopback_http.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), int(getattr(response, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), int(exc.code)
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise AppTestError("%s 回的不是 JSON（HTTP %s）：%s"
                           % (parsed.path, status, raw[:160])) from None


def activate(status: int, listing: Any, posted: Any) -> dict[str, Any]:
    """`POST /api/projects` 之后必须用 `GET /api/projects` **读回来核对**：
    只信"注册接口返回了 200"会出现登记表根本没写进去、页面却在空项目下正常渲染的假绿。"""
    project_id = str(posted.get("project_id") or "") if isinstance(posted, dict) else ""
    rows = (listing or {}).get("projects") if isinstance(listing, dict) else None
    ids = [str(row.get("project_id") or "") for row in (rows or []) if isinstance(row, dict)]
    current = str((listing or {}).get("current") or "") if isinstance(listing, dict) else ""
    return {"ok": bool(project_id) and status == 200 and isinstance(posted, dict)
               and current == project_id and ids == [project_id],
            "status": status, "current": current, "listed": ids, "project_id": project_id}


def state_isolated(state_root: Path, verdict: dict[str, Any]) -> bool:
    """隔离凭据：状态根里真的落下了文件，并且项目登记表**只有这一个**项目。"""
    wrote_something = state_root.is_dir() and any(state_root.iterdir())
    return bool(wrote_something and verdict.get("ok")
                and len(verdict.get("listed") or []) == 1)


def read_facts(probe: Callable[[str, str, str], Any], *, fixture: str) -> dict[str, Any]:
    """把页面上要看的东西一次读齐。

    `probe(模式, 目标, 范围)` 由调用方注入（纯层因此能拿假页面喂，真浏览器那一半在现场类里）。
    模式：`count` 数 CSS、`text` 读可见文字、`html` 读结构。
    夹具文件靠【文件树容器的文本】认，不能靠 `text:` 定位——`page_action` 的文本定位只在
    **可交互元素**里找（a/button/input/label/...），文件树里那行是个 `<span>`，
    实测 `text: probe_xxx.py` 命中 0 个。整页"内容换没换"靠 `main` 正文指纹，
    **不能靠 `#app *` 计数**：`page_action` 的候选清单在 64 个就截断
    （`list.length >= 64` 直接 return），实测切工作区前后都报 64，看不出任何变化。
    """
    tree = str(probe("text", ".ft-body", "") or "")
    return {"shell": int(probe("count", ".wb-shell", "") or 0),
            "tabs": int(probe("count", "[data-wb-view]", "") or 0),
            "rows": int(probe("count", ".ft-row", "") or 0),
            # 这一次独有的文件名要出现在文件树里：这是"后端数据到了 DOM"的凭据
            "fixture_in_tree": fixture in tree,
            "active_before": str(probe("text", ".wb-view-label", "") or "").strip(),
            "content_before": _digest(probe("text", "main", ""))}


def click_and_read(probe: Callable[..., Any], click: Callable[[str, str], Any], *,
                   tab: str, scope: str = ".wb-view-menu") -> dict[str, Any]:
    """打开顶栏视图下拉，选中目标视图，然后**再读一次**：绿必须来自"点了、视图真的换了"。"""
    if tab == "素材":
        opened = click("更多", ".wb-topbar-right")
        scope = ".wb-menu"
    else:
        current = str(probe("text", ".wb-view-label", "") or "").strip() or "概览"
        opened = click(current, ".wb-view-wrap")
    outcome = click(tab, scope)
    effects = [str(opened.get("effect") or opened.get("error") or ""),
               str(outcome.get("effect") or outcome.get("error") or "")]
    return {"click_ok": bool(opened.get("ok")) and bool(outcome.get("ok")),
            "click_effect": " → ".join(x for x in effects if x),
            "active_after": str(probe("text", ".wb-view-label", "") or "").strip(),
            "content_after": _digest(probe("text", "main", ""))}


def _digest(value: Any) -> str:
    """正文指纹：`长度:摘要前 8 位`。整页正文太长，直接塞进报告会淹掉别的事实。"""
    text = str(value or "")
    return "%d:%s" % (len(text), hashlib.sha1(text.encode("utf-8")).hexdigest()[:8])


def stream_flags(sandbox: dict[str, Any], *, script: str = STREAM_SCRIPT) -> list[str]:
    """合成"有人说话 + 尾静音"的麦克风内容，交给 Edge 的假设备开关。

    文件必须真的存在：`--use-file-for-fake-audio-capture` 指向不存在的文件时 Edge 会
    **静默回落**成内置假设备，现象是"跑通了但内容是假的"（Wave7 踩过，`fake_media_flags`
    因此直接抛错）。
    """
    from agent_runtime import media_fixture
    holder = Path(sandbox["sandbox"])
    wav = media_fixture.write_wav(holder / "mic.wav",
                                  media_fixture.build_pcm(
                                      media_fixture.parse_script(script)))
    frame = holder / "cam.y4m"
    frame.write_bytes(media_fixture.build_y4m(frames=25))
    return media_fixture.fake_media_flags(audio=wav, video=frame)


def read_turns(captions: str, mode_text: str) -> dict[str, Any]:
    """把页面上的回合文本拆成可判定的量。

    **数字一律不硬编码**：浏览器推了多少片取决于时钟与夹具循环的位置，所以判据只能是
    "自洽"——字节数等于片数 × 3200（16k mono 100ms），且助手回包引用的字节数与转写来自
    同一次实测。这两条一起就把"页面自己写了串像样的字"排除掉了。
    """
    heard = [{"chunks": int(chunks), "bytes": int(row_bytes), "peak": float(peak)}
             for chunks, row_bytes, peak in CAPTION_RE.findall(str(captions or ""))]
    replied = [{"bytes": int(row_bytes), "round": int(number)}
               for row_bytes, number in REPLY_RE.findall(str(captions or ""))]
    return {"heard": heard, "replied": replied,
            "rounds": sorted({row["round"] for row in replied}),
            "native": "原生实时" in str(mode_text or "")}


def stream_checks(observed: dict[str, Any], *, rounds: int = 2) -> dict[str, bool]:
    """流式那一段的判定。每条都对应一类"看着像通了其实没通"。"""
    consistent = [row for row in (observed.get("heard") or [])
                  if row["bytes"] == row["chunks"] * 3200 and PEAK_LO < row["peak"] < PEAK_HI]
    heard_bytes = {row["bytes"] for row in consistent}
    return {
        # 页面报的是"原生实时"而不是降级——没协商成功时后面几条绿也不可信
        "stream_negotiated_native": bool(observed.get("native")),
        # 麦克风内容真的到了服务端并被实测：字节/片数自洽 + 峰值在"有人说话"的带内
        "stream_audio_measured": bool(consistent),
        # 回包引用的字节数必须与那次实测同一个数：两件事件互相印证
        "stream_reply_matches_measured_audio": bool(
            heard_bytes & {row["bytes"] for row in (observed.get("replied") or [])}),
        # 至少两轮：只出一轮可能是撞上一次幸运投递
        "stream_turns_repeat": len(observed.get("rounds") or []) >= rounds,
        # 麦克风真的开着（页面自己承认）：少了这条，"没转写"到底是没开麦还是没上行分不清
        "stream_mic_started": bool(observed.get("mic_on")),
    }


def evaluate(observed: dict[str, Any], wanted: dict[str, Any],
             fresh: dict[str, Any], cleanup: dict[str, Any]) -> dict[str, Any]:
    """判定表。每一条都对应一类"看起来绿其实没测"的假象，缺任何一条证据都判未通过。"""
    texts = observed.get("texts") or {}
    needles = list(wanted.get("texts") or [])
    assertions = {
        "backend_started": bool(observed.get("ready")),
        "state_isolated": bool(observed.get("isolated")),
        "project_activated": bool(observed.get("activated")),
        # 旧构建上的绿灯不代表当前代码没问题——这条就是为了让它不能被当成没问题
        "bundle_built_after_latest_source": bool(fresh.get("built")) and bool(fresh.get("fresh")),
        "shell_mounted": int(observed.get("shell") or 0) >= 1,
        "nav_renders_every_tab": int(observed.get("tabs") or 0) == int(wanted.get("tabs") or 0),
        # 后端数据真的到了页面：文件树有行、且这一次独有的文件名被列了出来
        "backend_data_reaches_page": int(observed.get("rows") or 0) >= 1
                                     and observed.get("fixture_in_tree") is True,
        "console_clean": not (observed.get("console_errors") or []),
        # 只对**真失败**变红：客户端自己 AbortController 取消的那条不算（见
        # `split_requests`），但它照样进报告，不是被扔掉。
        "no_failed_requests": not (observed.get("failed_requests") or []),
        "nav_click_applied": bool(observed.get("click_ok")),
        "view_changed_on_click": (str(observed.get("active_after") or "")
                                  == str(wanted.get("tab") or "")
                                  and str(observed.get("active_before") or "")
                                  != str(observed.get("active_after") or "")),
        "page_content_changed_on_click": (
            str(observed.get("content_after") or "") != str(observed.get("content_before") or "")
            # 换完是【空正文】不算"内容变了"：那正是整块挂载失败的形状
            and str(observed.get("content_after") or "") != _digest("")),
        # 正文要读全了才配说"这句话没出现"：read_page 有 4000 字符上限，
        # 截断之后的"没找到"不是证据，得单独红出来
        "page_text_read_wholly": (not needles) or not observed.get("body_truncated"),
        "extra_texts_rendered": all(texts.get(needle) is True for needle in needles),
        "artifacts_cleaned": bool(cleanup.get("removed")) and bool(cleanup.get("job_stopped"))
                             and int(cleanup.get("live_pages") or 0) == 0,
    }
    if wanted.get("stream"):
        # 流式那一段是"整页 + 假麦克风 + 真 ws"，判定另成一表并并进同一张红单
        assertions.update(stream_checks(observed, rounds=int(wanted.get("rounds") or 2)))
    if wanted.get("deep_link"):
        # 深链那一档是"冷开每条页面路由"，判定同样另成一表并并进同一张红单
        assertions.update(deep_link_checks(observed))
    if wanted.get("ask"):
        # 问答那一档是"整页真的走完一轮问答"，判定同样另成一表并并进同一张红单
        assertions.update(ask_checks(observed))
    failed = [name for name, ok in assertions.items() if not ok]
    return {"assertions": assertions, "failed": failed, "passed": not failed}


def run_app_test(root: str | Path, *, page: str = "/workbench", texts: str = "",
                 tab: str = DEFAULT_TAB, stream: bool = False, timeout: float = 120.0,
                 ready_wait: float = READY_WAIT, deep_link: bool = False,
                 ask: bool = False, server_env_extra: Mapping[str, str] | None = None
                 ) -> dict[str, Any]:
    """前置检查 → 起后端 → 注册项目 → 开整页 → 点击导航 → 逐条判定 → 拆干净。"""
    base = Path(root).resolve()
    started = time.monotonic()
    out: dict[str, Any] = {"ok": False, "passed": False, "checks": {}, "url": "", "error": ""}
    try:
        path = page_path(page)
        needles = text_needles(texts)
        tab = str(tab or DEFAULT_TAB).strip()[:40] or DEFAULT_TAB
    except AppTestError as exc:
        out["error"] = str(exc)
        return out
    if not (base / "api.py").is_file():
        out["error"] = "%s 里没有 api.py，起不了后端。" % base
        return out
    fresh = bundle_freshness(base)
    if not fresh["built"]:
        out["error"] = fresh["detail"]
        return out
    port = dev_server.free_port()
    sandbox = make_sandbox()
    state: dict[str, Any] = {"job_id": "", "page": "", "ready": False, "texts": {},
                             "console_errors": [], "failed_requests": [],
                             "sandbox": sandbox["sandbox"]}
    try:
        _drive(base, state, port=port, path=path, tab=tab, needles=needles,
               timeout=timeout, ready_wait=ready_wait, sandbox=sandbox, stream=bool(stream),
               deep_link=bool(deep_link), ask=bool(ask), ask_env=server_env_extra)
    except (AppTestError, page_action.PageActionError,
            urllib.error.URLError, OSError) as exc:
        out["error"] = "整页守卫没能走完：%s" % str(exc)[:300]
    except Exception as exc:  # noqa: BLE001 - 起不来/读不到一律报原因，绝不报通过
        out["error"] = "整页守卫异常：%s: %s" % (type(exc).__name__, str(exc)[:220])
    finally:
        cleanup = _teardown(state)
    if not state["ready"]:
        out["error"] = state.get("error") or "后端没起来（看服务输出尾部）"
        out["checks"] = {"backend_started": False}
        out["cleanup"] = cleanup
        out["log_tail"] = state.get("log_tail", "")
        out["elapsed"] = round(time.monotonic() - started, 2)
        return out
    judged = evaluate(state, {"tab": tab, "texts": needles, "tabs": EXPECTED_TABS,
                              "stream": bool(stream), "rounds": 2,
                              "deep_link": bool(deep_link), "ask": bool(ask)}, fresh, cleanup)
    reasons = [out.get("error") or ""]
    if not judged["passed"]:
        reasons.append(_why(judged["failed"], fresh, state))
    out.update({"ok": judged["passed"], "passed": judged["passed"], "url": state.get("url", ""),
                "checks": judged["assertions"], "failed": judged["failed"],
                "observed": _facts(state), "bundle": fresh, "cleanup": cleanup,
                "elapsed": round(time.monotonic() - started, 2),
                "error": "；".join(row for row in reasons if row)})
    return out


FACT_KEYS = ("shell", "tabs", "rows", "fixture_in_tree", "active_before", "active_after",
             "content_before", "content_after", "body_truncated", "texts", "console_errors",
             "failed_requests", "activated", "isolated", "project_id", "click_ok",
             "click_effect", "log_tail", "heard", "replied", "rounds", "native", "mic_on",
             "stream_steps", "stream_captions", "stream_flags",
             # 深链那一档的事实**必须**进报告：判据只给"绿/红"，不给"数到了什么"，
             # 于是"标记选错了"和"这一页真的没渲染"在报告里长得一模一样（§9：
             # 多上报一个原始量，而不是猜）。
             "deep_links", "deep_console_errors", "deep_failed_requests",
             "deep_retried_routes",
             # 客户端自己取消的请求单独上报：判据不为它变红，但它必须看得见——
             # 静默扔掉就是"假干净"。
             "cancelled_requests", "deep_cancelled_requests",
             # 问答那一档：凭据（随机夹具名）、点了哪个问题、等了多少毫秒。
             "ask_credential", "ask_shell", "ask_click_ok", "ask_click_effect",
             "ask_draft", "ask_submit_ok", "ask_submit_effect",
             "ask_question", "ask_question_echoed", "ask_waited_ms", "ask_text",
             "ask_truncated", "ask_console_errors", "ask_failed_requests", "ask_error",
             # 等了两级多久、数据到底来没来：红了的时候这两个数直接说明"是没等到"
             # 还是"等到了但内容不对"。
             "shell_waited_ms", "data_waited_ms", "data_ready")


def _facts(state: dict[str, Any]) -> dict[str, Any]:
    # `probe` 是个函数，不能进报告（也没意义）
    return {key: state.get(key) for key in FACT_KEYS if key in state}


def _drive(base: Path, state: dict[str, Any], *, port: int, path: str, tab: str,
           needles: list[str], timeout: float, ready_wait: float,
           sandbox: dict[str, Any], stream: bool = False,
           deep_link: bool = False, ask: bool = False,
           ask_env: Mapping[str, str] | None = None) -> None:
    """真起服务、真开页面、真点导航——事实一律写进 `state`（判定与收回都在外面）。"""
    server = dev_server.start(str(base), "uvicorn api:app (整页守卫)",
                              argv=server_argv(port, python=sys.executable),
                              port=port, wait=ready_wait,
                              env=server_env(sandbox["state"], realtime=stream,
                                             extra=ask_env if ask else None))
    state["job_id"] = str(server.get("job_id") or "")
    state["ready"] = bool(server.get("ready"))
    state["log_tail"] = str(server.get("log_tail") or "")[-500:]
    if not state["ready"]:
        state["error"] = "后端没起来：%s" % (server.get("error") or "就绪探测失败")
        return
    url = "http://%s:%d" % (LOOPBACK, port)
    state["url"] = url + path
    status, posted = http_json(url + "/api/projects",
                               {"root": str(sandbox["project"]), "name": "apptest"})
    read_status, listing = http_json(url + "/api/projects")
    verdict = activate(read_status, listing, posted)
    state["activated"] = verdict["ok"]
    state["isolated"] = state_isolated(sandbox["state"], verdict)
    state["project_id"] = verdict["project_id"]
    # 假设备必须在**开页之前**定好：`--use-file-for-fake-*-capture` 是浏览器启动参数，
    # 页面已经起来了再传是无效的。少了它们，「打开摄像头」会静默失败、
    # 麦克风按钮压根不出现——现象和"上行不通"一模一样，所以只能在这里就带对。
    flags = stream_flags(sandbox) if stream else []
    state["stream_flags"] = [row.split("=")[0] for row in flags]
    session = page_action.open_page(base, url=state["url"], timeout=timeout, flags=flags)
    state["page"] = session["page"]
    state["console_errors"] = list(session.get("console_errors") or [])
    state["failed_requests"] = list(session.get("failed_requests") or [])

    # 两级等待，都在读 facts 之前——见 `WORKBENCH_DATA_WAIT` 上面那段：
    # "文档加载完"不等于"应用把数据拉回来了"。
    # 第一级只问"这是不是工作台那一页"；**它不判红**——`page:` 指向不存在的路径时
    # 壳子本来就不该出现，那条路要靠后面的断言去红
    # （`test_a_page_that_does_not_exist_turns_the_guard_red`）。
    shell = page_action.wait_for(base, page=state["page"], selector=".wb-shell",
                                 state="present", timeout=min(5.0, float(timeout)))
    state["console_errors"] += list(shell.get("console_errors") or [])
    state["failed_requests"] += list(shell.get("failed_requests") or [])
    state["shell_waited_ms"] = shell.get("waited_ms")
    if shell.get("ok"):
        # 第二级等**数据**：文件树有行正是 `backend_data_reaches_page` 读的那个凭据。
        data = page_action.wait_for(base, page=state["page"], selector=".ft-row",
                                    state="present", timeout=WORKBENCH_DATA_WAIT)
        state["console_errors"] += list(data.get("console_errors") or [])
        state["failed_requests"] += list(data.get("failed_requests") or [])
        state["data_waited_ms"] = data.get("waited_ms")
        state["data_ready"] = bool(data.get("ok"))

    def probe(mode: str, target: str, scope: str = "") -> Any:
        if mode == "count":
            return page_action.locate(base, page=state["page"], selector=target).get("count")
        if mode == "count-text":
            # 按**可见文字**数元素：`关麦`/`开麦` 这种状态标签没有稳定 class。
            # 限定 `.lsp`（实时对话弹层）—— 页面别处也可能出现同名字样。
            return page_action.locate(base, page=state["page"], text=target,
                                      scope=scope).get("count")
        return page_action.read_page(base, page=state["page"], selector=target,
                                     mode="text").get("text")

    state["probe"] = probe                      # 流式那一档要复用同一个读法
    state.update(read_facts(probe, fixture=sandbox["fixture"]))
    body = page_action.read_page(base, page=state["page"], selector="body", mode="text")
    read_text = str(body.get("text") or "")
    state["texts"] = {needle: needle in read_text for needle in needles}
    state["body_truncated"] = bool(body.get("truncated"))
    state.update(click_and_read(
        probe, lambda value, where: page_action.click(base, page=state["page"], text=value,
                                                      scope=where, screenshot=False),
        tab=tab))
    settled = page_action.wait_for(base, page=state["page"], selector=".wb-shell",
                                   state="present", timeout=5.0)
    state["console_errors"] += list(settled.get("console_errors") or [])
    state["failed_requests"] += list(settled.get("failed_requests") or [])
    if stream:
        _drive_stream(base, state)
    if deep_link:
        # 深链要**冷开**每一条页面路由，所以复用上面那个会话是没有意义的（那样只是站内
        # 导航）；它自己按路由逐个开新会话，跑在同一个后端上。
        _drive_deep_link(base, state, "http://%s:%d" % (LOOPBACK, port), timeout=timeout)
    if ask:
        # 问答那一档开在 `/` 上（上面那个会话开的是工作台），所以它自己开新会话。
        # **必须在工作台那轮之后跑**：它复用同一个后端，而问答会写会话历史。
        _drive_ask(base, state, "http://%s:%d" % (LOOPBACK, port),
                   fixture=str(sandbox["fixture"]), timeout=timeout)
    # 全部收齐之后再做一次分类：客户端自己取消的（`probeBackend` 那条 `/api/health`）
    # 不算失败，但**照样上报**（`cancelled_requests`）。
    for key, out_key in (("failed_requests", "cancelled_requests"),
                        ("deep_failed_requests", "deep_cancelled_requests")):
        if key in state:
            split = split_requests(state.get(key))
            state[key] = split["failed_requests"]
            state[out_key] = split["cancelled_requests"]


def _open_with_retry(base: Path, target: str, *, timeout: float,
                     retries: list[str] | None = None,
                     attempts: int = 3) -> dict[str, Any]:
    """开一个页面；顶破 `open_page` 那 6 秒就绪预算时重试，**次数记进 `retries`**。

    就绪判据是 `document.readyState === 'complete'`，而它的预算**硬编码**为
    `120 × 50ms`（`page_action.py` 的脚本里，不是参数；那个文件现阶段属别人），
    每条路由又都要重下整套资源（`open_page` 会给 URL 加 `?docmind=` 缓存戳）。
    深链与问答两档共用这一份——同一个逻辑写两处就是"其中一处迟早变陈旧"（§9）。

    **重试之间必须先把上一个浏览器收干净、并给它退避时间**（第一版只 `sleep(1.0)`）：
    `close_page` 只是从 `_sessions` 里摘掉记录，Edge 进程退出是**异步**的。实测表现为
    **交替**失���——连着跑时第 1/2/5/7 次三次重试全超时、第 3/6 次却正常，而 `live_pages`
    每次都是空的（说明会话没泄漏，是**进程**还没退出）。紧挨着重试等于把一次失败放大成
    三次连续抢资源。所以这里每次失败都 `close_all()` 并按次数退避（2s / 4s）。
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return page_action.open_page(base, url=target, timeout=timeout)
        except Exception as exc:                 # noqa: BLE001
            if "页面加载超时" not in str(exc):
                raise
            last = exc
            if retries is not None:
                retries.append("%s#%d" % (target, attempt))
            with contextlib.suppress(Exception):
                page_action.close_all()
            time.sleep(2.0 * attempt)             # 退避：给上一个浏览器真正退出的时间
    raise AppTestError("开了 %d 次都顶破 open_page 的就绪预算（每次都已回收上一个会话"
                       "并退避 %s 秒）：%s"
                       % (attempts, "/".join(str(2 * n) for n in range(1, attempts)),
                          str(last)[:160]))


def _drive_ask(base: Path, state: dict[str, Any], url: str, *, fixture: str,
               timeout: float) -> None:
    """冷开问答页 `/`，用建议卡填一个问题并提交，等回答里出现**那一次独有的凭据**。

    为什么要先点建议卡：问题文案由产品提供，测试不猜；改版（2026-10）后点它只把 prompt
    填进草稿（`useCapability`），**不再直接发送**——所以必须再点一次发送键，才算真跑一轮。
    提交前从草稿读回真正的 prompt 作为「问的是哪个问题」的凭据：按钮文字是标题+说明，
    和真正发给模型的内容不是一回事，拿它做回显断言会假绿/假红。
    代价是问题文案不由我们定；所以凭据不能依赖问题内容，落在 `list_dir` 的返回上（见
    `ASK_WAIT`）。
    """
    state["ask_credential"] = fixture
    errors: list[Any] = []
    failed: list[Any] = []
    session: dict[str, Any] = {}
    try:
        # 开页放在 try **里面**：第一版写在外面，它一抛（问答页资源重，实测会顶破
        # `open_page` 那 6 秒就绪预算）`finally` 就不执行，`ask_credential` 从没被设上、
        # 真实原因被吞掉——报告里只剩"凭据: None"。
        session = _open_with_retry(base, url + "/", timeout=timeout)
        page = str(session.get("page") or "")
        state["ask_page"] = page
        errors += list(session.get("console_errors") or [])
        failed += list(session.get("failed_requests") or [])
        shell = page_action.wait_for(base, page=page, selector=".ask-shell",
                                     state="present", timeout=WORKBENCH_DATA_WAIT)
        errors += list(shell.get("console_errors") or [])
        failed += list(shell.get("failed_requests") or [])
        state["ask_shell"] = 1 if shell.get("ok") else 0
        ready = page_action.wait_for(base, page=page, selector=ASK_SUGGESTION,
                                     state="present", timeout=WORKBENCH_DATA_WAIT)
        if not ready.get("ok"):
            state["ask_error"] = "问答页没有出现建议问题按钮"
            return
        clicked = page_action.click(base, page=page, selector=ASK_SUGGESTION, nth=0,
                                    screenshot=False)
        state["ask_click_ok"] = bool(clicked.get("ok"))
        state["ask_click_effect"] = str(clicked.get("effect") or clicked.get("error") or "")[:120]
        # 建议卡只负责填草稿；等它真的落进 textarea，读回 prompt 作为"问的是哪个问题"
        draft, deadline = "", time.monotonic() + 3.0
        while time.monotonic() < deadline:
            draft = str(page_action.read_page(base, page=page, selector=ASK_COMPOSER,
                                              mode="value").get("text") or "").strip()
            if draft:
                break
            time.sleep(0.1)
        question = draft.splitlines()[0].strip() if draft else ""
        state["ask_draft"] = draft[:200]
        if not question:
            state["ask_error"] = "点建议卡没有把问题填进草稿"
            return
        # 草稿到位后发送键才可点；直接点 disabled 的按钮会"成功"却没有请求，白等 40 秒。
        armed = page_action.wait_for(base, page=page, selector=ASK_SEND + ":not([disabled])",
                                     state="present", timeout=3.0)
        errors += list(armed.get("console_errors") or [])
        failed += list(armed.get("failed_requests") or [])
        submitted = page_action.click(base, page=page, selector=ASK_SEND, screenshot=False)
        state["ask_submit_ok"] = bool(submitted.get("ok"))
        state["ask_submit_effect"] = str(submitted.get("effect") or submitted.get("error") or "")[:120]
        # 等凭据出现在会话区里——用 `state="text"`，不带截止期地 sleep 就是跟渲染赛跑
        landed = page_action.wait_for(base, page=page, selector=".chat-scroll",
                                      state="text", value=fixture, timeout=ASK_WAIT)
        body = page_action.read_page(base, page=page, selector=".chat-scroll", mode="text")
        text = str(body.get("text") or "")
        state["ask_text"] = text[:4000]
        state["ask_truncated"] = bool(body.get("truncated"))
        state["ask_waited_ms"] = landed.get("waited_ms")
        state["ask_question"] = question[:80]
        state["ask_question_echoed"] = bool(question) and question[:20] in text
        errors += list(landed.get("console_errors") or [])
        failed += list(landed.get("failed_requests") or [])
    except Exception as exc:                     # noqa: BLE001
        # 绝不静默：把原因写进 state，报告里点名（第一版这里什么都不写，
        # 于是"凭据: None"就是全部信息，连是超时还是没挂上都看不出来）。
        state["ask_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        if session:
            page_action.close_page(str(session.get("page") or ""))
        state["ask_console_errors"] = errors
        state["ask_failed_requests"] = failed
        state["ask_credential"] = fixture


def _drive_deep_link(base: Path, state: dict[str, Any], url: str, *,
                     timeout: float = 60.0) -> None:
    """逐条**冷开**每一条深链：每条路由一个新会话、走完整加载，绝不复用上一个页面。

    为什么必须每条都新会话：复用同一个页面去 navigate，第二条起就是**站内导航**，
    而深链要的恰恰是"冷启动直接进这一页"。站内导航顺带会把上一条的状态带过来，
    于是"这一条自己能不能立起来"就没人验了。

    等挂载点必须**带截止期**（`wait_for`），不能固定 sleep：三个页面的冷加载耗时不同，
    固定 sleep 会把慢的那个判成"没挂上"。
    """
    console_errors: list[Any] = []
    failed_requests: list[Any] = []
    retries: list[str] = []
    # 先把同一个 list 对象挂进 state：失败了也要看得见"重试过哪几条"，
    # 只在成功路径末尾赋值的话，一旦抛异常这些信息就全丢了（第一版就是这样，
    # 红了只有 `deep: null`，连是哪条路由超时都看不出来）。
    state["deep_retried_routes"] = retries

    def open_route(spec: dict) -> dict:
        """开一条路由；`页面加载超时` 时重试（次数记进 `deep_retried_routes`）。

        重试**不是掩盖**：每条的重试次数在报告里看得见；三次都超时就照常红——
        而且错误里点名是哪条路由。最重的是 `/workbench` 那几条（缓存戳让每次都要重下
        整套资源，含 vendor 与 model-viewer）。
        """
        path = str(spec["path"])
        try:
            return _open_with_retry(base, url + path, timeout=timeout, retries=retries)
        except AppTestError as exc:
            raise AppTestError("深链路由 %s：%s" % (path, exc)) from None

    def read(spec: dict) -> dict:
        session = open_route(spec)
        page = str(session.get("page") or "")
        try:
            if spec.get("own"):
                # 挂载点没出现也要往下走：下面数出来的是 0，判据会如实报红，
                # 而不是在这里抛异常把整轮带崩。
                page_action.wait_for(base, page=page, selector=str(spec["own"]),
                                     state="present", timeout=DEEP_LINK_WAIT)
            if _console_matters(spec):
                console_errors.extend(session.get("console_errors") or [])
                failed_requests.extend(session.get("failed_requests") or [])

            def count(css: str) -> int:
                if not css:
                    return 0
                return int(page_action.locate(base, page=page, selector=css).get("count") or 0)

            return {"own": count(str(spec.get("own") or "")),
                    "foreign": [count(css) for css in (spec.get("foreign") or ())],
                    "href": session.get("href")}
        finally:
            page_action.close_page(page)

    state["deep_links"] = deep_link_facts(read)
    state["deep_console_errors"] = console_errors
    state["deep_failed_requests"] = failed_requests


def _drive_stream(base: Path, state: dict[str, Any]) -> None:
    """整页的流式那一档：假麦克风喂进真 Edge → 真 ws → scripted 适配器 → 回落到 DOM。

    每一环都不可省（实测）：ws 只在**画面通道**建立后才存在。
    「开始」是**画面 + 麦克风一起开**（`LiveStreamPanel`：麦克风是这条链路的必需输入，
    只发视频时服务端会稳定断流），所以麦克风不需要单独一步手势。

    每一步都要重试：「开始」按钮在弹层刚打开时还没渲染出来，而握手要一两秒 ——
    实测一次 1 秒的固定 sleep 就会报"命中 0 个"。
    """
    read = state["probe"]
    for index, (label, scope) in enumerate(STREAM_STEPS):
        deadline = time.monotonic() + (STREAM_STEP_WAIT if index else 6.0)
        recorded = None
        while recorded is None:
            try:
                back = page_action.click(base, page=state["page"], text=label, scope=scope,
                                         screenshot=False)
                recorded = {"label": label, "ok": bool(back.get("ok")),
                            "effect": str(back.get("effect") or "")}
            except page_action.PageActionError as exc:
                if time.monotonic() >= deadline:
                    state.setdefault("stream_steps", []).append(
                        {"label": label, "ok": False, "effect": str(exc)[:160]})
                    return
                time.sleep(0.7)
        state.setdefault("stream_steps", []).append(recorded)
        time.sleep(0.5)
    # 麦克风状态要在**会话刚起来**时读：脚本回合跑完后会话自己结束，
    # 按钮随之从「关麦」翻回「开麦」—— 放到读字幕之后读就晚了一步，
    # 判据会误报成「麦克风没开」。
    deadline = time.monotonic() + 12.0
    mic_on = False
    while time.monotonic() < deadline:
        if int(read("count-text", "关麦", ".lsp") or 0) >= 1:
            mic_on = True
            break
        time.sleep(0.4)
    state["mic_on"] = mic_on
    deadline = time.monotonic() + STREAM_WAIT
    captions = mode = ""
    while True:
        captions = str(read("text", ".lsp-captions", "") or "")
        mode = str(read("text", ".rmb-label", "") or "")
        facts = read_turns(captions, mode)
        if len(facts["rounds"]) >= 2 or time.monotonic() >= deadline:
            state.update(facts)
            state["stream_captions"] = captions[:400]
            return
        time.sleep(1.0)


def _teardown(state: dict[str, Any]) -> dict[str, Any]:
    """关页面、停服务、删沙箱，并且【如实回报有没有删干净】。"""
    if state.get("page"):
        try:
            closed = page_action.close_page(str(state["page"]))
            if not closed.get("ok"):
                state["close_error"] = str(closed.get("error") or "页面没关掉")[:120]
        except Exception as exc:  # noqa: BLE001 - 关不掉也要继续拆服务，但要把原因留下
            state["close_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    job_error = ""
    if state.get("job_id"):
        try:
            stopped = dev_server.stop(str(state["job_id"]))
            job_error = "" if stopped.get("ok") else str(stopped.get("error") or "回收失败")
        except Exception as exc:  # noqa: BLE001 - 停不下来也要继续往下拆
            job_error = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    # 沙箱里有子进程自己的 sqlite/状态文件：`job_cancel` 返回时句柄可能还没被 OS 收走，
    # 实测默认 4 次 × 0.25s（约 1s）删不掉，留了两份孤儿目录在系统临时目录里。
    cleaned = vue_component.clean_target(Path(state["sandbox"]), attempts=10, pause=0.4)
    return {"removed": bool(cleaned.get("removed")), "attempts": cleaned.get("attempts"),
            "job_stopped": not job_error, "job_error": job_error,
            "close_error": state.get("close_error", ""),
            "live_pages": len(page_action.live_pages()),
            "error": cleaned.get("error", ""), "sandbox": str(state["sandbox"])}


def _why(failed: list[str], fresh: dict[str, Any], observed: dict[str, Any]) -> str:
    reasons = []
    if "bundle_built_after_latest_source" in failed:
        reasons.append("整页跑的是【旧构建】（%s，旧了 %ss）：先 `cd frontend && npm run build`"
                       "再说整页绿。" % (fresh["detail"], fresh["stale_by"]))
    if "state_isolated" in failed:
        reasons.append("状态根没隔离或未只列出本次项目——可能往仓库真登记表上写了")
    if "backend_data_reaches_page" in failed:
        reasons.append("页面没把后端读到的那个文件列出来（文件树 %s 行）"
                       % observed.get("rows"))
    if "console_clean" in failed or "no_failed_requests" in failed:
        reasons.append("console 有 %s 条错误、请求失败 %s 条（另有 %s 条是客户端自己取消的，"
                       "不计入失败）" % (
                           len(observed.get("console_errors") or []),
                           len(observed.get("failed_requests") or []),
                           len(observed.get("cancelled_requests") or [])))
    deep_bad = [row for row in (observed.get("deep_links") or [])
                if not row.get("own_seen") and row.get("own")]
    if deep_bad:
        reasons.append("这些深链没渲染出自己的挂载点：%s（标记是 %s）" % (
            "、".join(str(row.get("path")) for row in deep_bad),
            "、".join(str(row.get("own")) for row in deep_bad)))
    cross = [row for row in (observed.get("deep_links") or []) if row.get("foreign_seen")]
    if cross:
        reasons.append("这些深链吐出了**别的**页面（不该出现的外来挂载点）：%s" % (
            "、".join("%s→%s" % (row.get("path"), row.get("foreign_counts"))
                      for row in cross)))
    unknown_bad = [row for row in (observed.get("deep_links") or [])
                   if row.get("unknown") and row.get("unknown_served_a_page")]
    if unknown_bad:
        reasons.append("不存在的路径却渲染出了一页（说明有 catch-all 把所有路径都回成某页）：%s" % (
            unknown_bad[0].get("unknown_counts"),))
    alias_bad = [row for row in (observed.get("deep_links") or [])
                 if row.get("alias") and row.get("landed")
                 and row.get("canonical") and row["landed"] != row["canonical"]]
    if alias_bad:
        reasons.append("这些别名没停在规范路径上：%s" % (
            "、".join("%s→%s（应为 %s）" % (row.get("path"), row.get("landed"),
                                          row.get("canonical")) for row in alias_bad),))
    return "；".join(reasons) or "有断言未成立"


def render(result: dict[str, Any]) -> str:
    if not result.get("checks"):
        return "整页守卫未能开始：%s" % (result.get("error") or "未知原因")
    head = "整页守卫【%s】：%s（%s 秒）" % (
        "通过" if result.get("ok") else "未通过", result.get("url"), result.get("elapsed"))
    lines = [head]
    for name in (result.get("failed") or []):
        lines.append("未通过项：%s" % name)
    bundle = result.get("bundle") or {}
    if bundle:
        lines.append("构建新旧：%s（%s）" % ("产物不比源码旧" if bundle.get("fresh")
                                          else "源码比产物新 %ss" % bundle.get("stale_by"),
                                          bundle.get("detail")))
    observed = result.get("observed") or {}
    if observed:
        lines.append("页面事实：外壳=%s 标签=%s 文件树行=%s 本次文件在树里=%s "
                     "活动标签 %s→%s 正文 %s→%s 正文读全=%s" % (
                         observed.get("shell"), observed.get("tabs"), observed.get("rows"),
                         observed.get("fixture_in_tree"), observed.get("active_before"),
                         observed.get("active_after"), observed.get("content_before"),
                         observed.get("content_after"), not observed.get("body_truncated")))
        for needle, got in (observed.get("texts") or {}).items():
            lines.append("页面文本 %r：%s" % (needle, "有" if got else "没有"))
        for row in (observed.get("console_errors") or [])[:3]:
            lines.append("console：%s" % str(row.get("text") if isinstance(row, dict)
                                             else row)[:180])
        for row in (observed.get("failed_requests") or [])[:3]:
            lines.append("失败请求：%s" % str(row.get("url") if isinstance(row, dict)
                                              else row)[:160])
    tail = str(observed.get("log_tail") or result.get("log_tail") or "")
    if tail:
        lines.append("服务输出尾部：%s" % tail[-300:])
    heard = observed.get("heard") or []
    if heard or observed.get("stream_steps"):
        lines.append("流式回合：实测 %s；回包 %s；轮次 %s；模式 %s" % (
            json.dumps(heard[-2:], ensure_ascii=False),
            json.dumps(observed.get("replied") or [], ensure_ascii=False),
            observed.get("rounds"), "原生实时" if observed.get("native") else "非原生"))
        for row in (observed.get("stream_steps") or []):
            lines.append("流式点击：%s → %s%s" % (row.get("label"), "ok" if row.get("ok")
                                                else "失败", ("：" + str(row.get("effect"))[:80])
                                                if not row.get("ok") else ""))
        if observed.get("stream_captions"):
            lines.append("字幕原文：%s" % str(observed["stream_captions"])[:220])
    cleanup = result.get("cleanup") or {}
    if not cleanup.get("removed") or not cleanup.get("job_stopped") or cleanup.get("live_pages") \
            or cleanup.get("close_error"):
        lines.append("注意：现场没完全收回（沙箱=%s 服务=%s%s 页面=%s%s）" % (
            cleanup.get("removed"), cleanup.get("job_stopped"),
            "：" + str(cleanup.get("job_error"))[:80] if cleanup.get("job_error") else "",
            cleanup.get("live_pages"),
            "：" + str(cleanup.get("close_error"))[:80] if cleanup.get("close_error") else ""))
    if not result.get("ok"):
        lines.append("为什么不可信：%s" % (result.get("error") or "有断言未成立"))
    lines.append("边界：证明的是【真后端 + 真构建产物 + 真浏览器】这一段集成——注册项目、"
                 "读文件树、切工作区；流式那一档走的是 scripted 离线适配器，"
                 "证明的是自家协议与页面接线，**不证明厂商事件与打断语义**（那些仍归 R12 人跑）。")
    return "\n".join(lines)


__all__ = ["AppTestError", "activate", "bundle_freshness", "click_and_read", "evaluate",
           "http_json", "make_sandbox", "page_path", "read_facts", "render", "run_app_test",
           "server_argv", "server_env", "state_isolated", "text_needles"]
