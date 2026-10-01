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

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from agent_runtime import dev_server, page_action, vue_component

MAX_TEXTS = 8
DEFAULT_TAB = "素材"
EXPECTED_TABS = 4
BUILD_DIR = "web"
SOURCE_DIR = os.path.join("frontend", "src")
#: 整页要等 uvicorn 导入完（实测冷启动 4.7s），再等 vite 产物把视图挂载
READY_WAIT = 60.0
#: 只在回环上跑：这条工具不是网页浏览器，不能拿去访问别的机器
LOOPBACK = "127.0.0.1"
_API_PATHS = ("/api/projects",)


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


def server_env(state_root: Path) -> dict[str, str]:
    """把子进程的状态根指到隔离目录——但**先带上自己的 env**。

    `process_runner.clean_environment()` 拿到什么就用什么（不会自动并 os.environ），
    所以只传一个键会让 uvicorn 连 PATH/SYSTEMROOT 都拿不到。凭据类变量由它统一剔除。
    """
    return {**os.environ, "DOCMIND_STATE_ROOT": str(state_root)}


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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
            "tabs": int(probe("count", ".ws-tab", "") or 0),
            "rows": int(probe("count", ".ft-row", "") or 0),
            # 这一次独有的文件名要出现在文件树里：这是"后端数据到了 DOM"的凭据
            "fixture_in_tree": fixture in tree,
            "active_before": str(probe("text", ".ws-active", "") or "").strip(),
            "content_before": _digest(probe("text", "main", ""))}


def click_and_read(probe: Callable[..., Any], click: Callable[[str, str], Any], *,
                   tab: str, scope: str = ".ws-bar") -> dict[str, Any]:
    """点工作区标签，然后**再读一次**：绿必须来自"点了、活动标签真的换了"。"""
    outcome = click(tab, scope)
    return {"click_ok": bool(outcome.get("ok")),
            "click_effect": str(outcome.get("effect") or outcome.get("error") or ""),
            "active_after": str(probe("text", ".ws-active", "") or "").strip(),
            "content_after": _digest(probe("text", "main", ""))}


def _digest(value: Any) -> str:
    """正文指纹：`长度:摘要前 8 位`。整页正文太长，直接塞进报告会淹掉别的事实。"""
    text = str(value or "")
    return "%d:%s" % (len(text), hashlib.sha1(text.encode("utf-8")).hexdigest()[:8])


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
    failed = [name for name, ok in assertions.items() if not ok]
    return {"assertions": assertions, "failed": failed, "passed": not failed}


def run_app_test(root: str | Path, *, page: str = "/workbench", texts: str = "",
                 tab: str = DEFAULT_TAB, timeout: float = 120.0,
                 ready_wait: float = READY_WAIT) -> dict[str, Any]:
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
               timeout=timeout, ready_wait=ready_wait, sandbox=sandbox)
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
    judged = evaluate(state, {"tab": tab, "texts": needles, "tabs": EXPECTED_TABS},
                      fresh, cleanup)
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
             "click_effect", "log_tail")


def _facts(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state.get(key) for key in FACT_KEYS if key in state}


def _drive(base: Path, state: dict[str, Any], *, port: int, path: str, tab: str,
           needles: list[str], timeout: float, ready_wait: float,
           sandbox: dict[str, Any]) -> None:
    """真起服务、真开页面、真点导航——事实一律写进 `state`（判定与收回都在外面）。"""
    server = dev_server.start(str(base), "uvicorn api:app (整页守卫)",
                              argv=server_argv(port, python=sys.executable),
                              port=port, wait=ready_wait, env=server_env(sandbox["state"]))
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
    session = page_action.open_page(base, url=state["url"], timeout=timeout)
    state["page"] = session["page"]
    state["console_errors"] = list(session.get("console_errors") or [])
    state["failed_requests"] = list(session.get("failed_requests") or [])

    def probe(mode: str, target: str, scope: str = "") -> Any:
        if mode == "count":
            return page_action.locate(base, page=state["page"], selector=target).get("count")
        return page_action.read_page(base, page=state["page"], selector=target,
                                     mode="text").get("text")

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
    cleaned = vue_component.clean_target(Path(state["sandbox"]))
    return {"removed": bool(cleaned.get("removed")),
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
        reasons.append("console 有 %s 条错误、请求失败 %s 条" % (
            len(observed.get("console_errors") or []), len(observed.get("failed_requests") or [])))
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
                 "读文件树、切工作区；它不点真模型、不听真网关，也不替组件级断言背书。")
    return "\n".join(lines)


__all__ = ["AppTestError", "activate", "bundle_freshness", "click_and_read", "evaluate",
           "http_json", "make_sandbox", "page_path", "read_facts", "render", "run_app_test",
           "server_argv", "server_env", "state_isolated", "text_needles"]
