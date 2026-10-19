"""让「跑起来」这一步对**后端与命令行**也有真凭据，而不只是浏览器。

`preview_project` / `dev_page_action` / `dev_vue` / `dev_apptest` 这一整段观察能力
全部长在浏览器上：截图、点击、组件、整页。对一个纯后端项目、一条 CLI、或者一个
worker 来说，它们一样都够不着——于是那段闭环就退回成 `run_command` 加一屏文本，
模型自己读。实测那种文本有两个具体的坏处：

1. **「没看到报错」被读成「没报错」**。输出被截断、或者错误写在 stderr 而 stderr
   没并进来，都会得到一个看起来干净的结论。
2. **「跑完了」被读成「跑对了」**。CLI 退出码 0 只说明它没崩；一个查了 0 行数据的
   任务、和一个真的查到 10 行的任务，在退出码上一模一样。

本模块给出两种可自动判定的凭据，两种都 fail-closed：

* ``http`` —— 真的发一个请求到回环地址，回状态码、响应体形状、耗时，并把响应里
  「看起来像报错」的东西（5xx、错误 JSON 字段）单独拎出来。**只允许回环**，和
  `dev_serve` / `dev_page_action` 同一道边界。
* ``cli`` —— 跑一条命令，除了退出码还回：traceback（真的还是被包装过的）、
  ERROR/CRITICAL 级别的行、被引用但没定义的名字、输出为空这件事本身。

两者都遵守仓库里那条最贵的教训：**没真跑成的检查不许报「干净」**。连接失败、
命令起不来、输出被截断、扫描没扫完——每一种都单独成为一条红的 `checks`，而不是
悄悄变成一个好看的结论。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import dev_server, loopback_http, process_runner

DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 120
#: 响应体最多回多少字符。超出必须标 truncated——「只看了前 4KB 里有这句话」
#: 不能被读成「它说了这句话」。
BODY_LIMIT = 8000
#: 一次最多回多少条 ERROR 级行。
MAX_ERROR_LINES = 40
#: 一次最多回多少个「引用但没定义」的名字。
MAX_NAMES = 40

#: 日志里表示出错的行。**只认行首等级标记**，不拿正文里出现的 "error" 字样充数
#: ——那会把 "修复 error handling" 这种正常输出判成一次报错。
_LOG_ERROR = re.compile(
    r"^\s*(?:\[?(?P<level>ERROR|CRITICAL|FATAL|SEVERE|PANIC|UNCAUGHT)\]?"
    r"[\s:\-–|]*)?(?P<rest>.*)$", re.I)
_LEVEL_ONLY = re.compile(
    r"^\s*(?:\[?(?:ERROR|CRITICAL|FATAL|SEVERE|PANIC|UNCAUGHT)\]?[\s:\-–|]+)", re.I)

#: 真正的 traceback：必须带 `Traceback (most recent call last)`，或者最后一行是
#: `XxxError: ...` 这种带类型的抛出。Python 的 `KeyError: 'a'` 算，
#: 日志里一句 "error: 缺少字段" 不算。
_TB_HEADER = re.compile(r"Traceback \(most recent call last\)")
_TB_TAIL = re.compile(r"^\s*(?:[A-Za-z_][\w.]*)?(?:Error|Exception|Interrupt|Exit|"
                      r"Warning|Failure)\b\s*:?\s*(.*)$")

#: uvicorn / gunicorn / werkzeug 这类默认会把 access log 打到 stdout 或 stderr，
#: 一条一行、每行都带随机端口——不折的话会把真正的错误挤到窗口外。
_ACCESS_LINE = re.compile(
    r"^\S+\s+-\s+[\d.]+\s+\"?[A-Z]+\s+\S+\s+HTTP/[\d.]+\"\s+\d{3}")

#: 疑似错误响应体：JSON 里的错误字段。**只对 JSON 生效**，HTML 错误页不算。
_JSON_ERROR_KEYS = ("error", "errors", "message", "detail", "exception", "traceback")


# --------------------------------------------------------------------------- #
# 公共：判定「输出里有没有出错」
# --------------------------------------------------------------------------- #

def scan_output(text: str, *, truncated: bool = False) -> dict[str, Any]:
    """从命令输出里找错误行 + traceback。只做**有把握**的识别。

    这里刻意做得比「看起来像错误」保守：宁可少报，不可把正常输出判成报错——一个
    天天误报的检查，用两次就会被关掉。
    """
    body = str(text or "")
    error_lines: list[str] = []
    level_count = 0
    for line in body.splitlines():
        if not line.strip():
            continue
        if _ACCESS_LINE.match(line):
            continue  # access log 的噪声，不是错误
        if _LEVEL_ONLY.match(line):
            level_count += 1
            if len(error_lines) < MAX_ERROR_LINES:
                error_lines.append(line.strip()[:300])
    has_tb = bool(_TB_HEADER.search(body))
    tb_tail = ""
    lines = body.splitlines()
    if has_tb:
        # 有完整表头时，**末行才是有用的那一行**（异常类型 + 消息）。只回「有 traceback」
        # 而不回末行，模型还得自己从一屏文本里捞最后一行——那正是这段要替它做的事。
        for line in reversed(lines):
            if not line.strip():
                continue
            if _TB_TAIL.match(line):
                tb_tail = line.strip()[:300]
            break  # 只看最后一个非空行：不是异常行就不硬凑，报空。
    else:
        # 只有一个光秃秃的 `KeyError: 'x'`（比如子进程只回了一行）也算 traceback。
        for line in reversed(lines):
            hit = _TB_TAIL.match(line)
            if hit and not line[:1].isspace():
                tb_tail = line.strip()[:300]
                has_tb = True
                break
            if line.strip() and not hit:
                break
    return {"error_lines": error_lines,
            "error_line_count": level_count,
            "truncated": bool(truncated),
            "has_traceback": has_tb,
            "traceback_tail": tb_tail}


# --------------------------------------------------------------------------- #
# http：真的打一个请求
# --------------------------------------------------------------------------- #

def _loopback_opener() -> urllib.request.OpenerDirector:
    """直连回环的 opener：**显式禁用环境代理**。

    `urllib.request.urlopen()` 默认读 `HTTP_PROXY` / `http_proxy` 等环境变量。
    本函数只被 `http_probe` 用，而目标已由 `dev_server.loopback_url()` 保证是回环
    地址 —— 对回环地址走代理是纯粹的错：代理会替目标应答（实测拿到 502），
    于是「连不上」被读成「服务端应答了」，fail-closed 语义被静默反转。
    """
    return loopback_http.loopback_opener()


def http_probe(url: str, *, method: str = "GET", body: str = "",
               headers: dict[str, str] | None = None, timeout: int = 15) -> dict[str, Any]:
    """对一个**回环**地址发一次请求，回结构化凭据。"""
    result: dict[str, Any] = {
        "ok": False, "url": "", "status": None, "elapsed": None,
        "body_excerpt": "", "body_length": None, "truncated": False,
        "content_type": "", "json": None, "json_error_fields": [],
        "checks": {}, "error": "", "notes": [],
    }
    target, err = dev_server.loopback_url(url)
    if err:
        result["error"] = err
        result["checks"] = {"url_allowed": False}
        return result
    result["url"] = target
    verb = (method or "GET").strip().upper()
    if verb not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        result["error"] = "不认这个方法：%s" % verb
        result["checks"] = {"method_known": False}
        return result
    payload = body.encode("utf-8") if body else None
    request = urllib.request.Request(target, data=payload, method=verb)
    for key, value in (headers or {}).items():
        request.add_header(str(key), str(value))
    if payload is not None and not any(
            str(k).lower() == "content-type" for k in (headers or {})):
        request.add_header("Content-Type", "application/json")
    started = time.monotonic()
    try:
        with _loopback_opener().open(request, timeout=max(1, int(timeout))) as response:
            raw = response.read(BODY_LIMIT + 1)
            result["status"] = int(getattr(response, "status", 0) or 0)
            result["content_type"] = str(response.headers.get("Content-Type") or "")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是一次**成功的观察**——它证明了服务端在应答。区别只在于
        # 这本身算不算「通过」，那是调用方用 expect_status 表达的。
        raw = b""
        try:
            raw = exc.read(BODY_LIMIT + 1)
        except Exception:  # noqa: BLE001 - 读不到正文不影响状态码这个凭据
            pass
        result["status"] = int(exc.code)
        result["content_type"] = str((exc.headers or {}).get("Content-Type") or "")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        result["error"] = "连不上 %s：%s" % (target, exc)
        result["elapsed"] = round(time.monotonic() - started, 3)
        result["checks"] = {"url_allowed": True, "method_known": True,
                            "server_answered": False}
        return result
    result["elapsed"] = round(time.monotonic() - started, 3)
    text = ""
    if raw:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            result["notes"].append("响应不是 UTF-8，按替换字符解码。")
    result["body_length"] = len(raw)
    if len(raw) > BODY_LIMIT:
        text = text[:BODY_LIMIT]
        result["truncated"] = True
    result["body_excerpt"] = text
    if "json" in (result["content_type"] or "").lower() and text.strip():
        try:
            parsed = json.loads(text)
        except ValueError:
            result["notes"].append("Content-Type 说自己是 JSON，但正文解析不了。")
        else:
            result["json"] = parsed if isinstance(parsed, (dict, list)) else None
            if isinstance(parsed, dict):
                for key in _JSON_ERROR_KEYS:
                    value = parsed.get(key)
                    if value not in (None, "", [], {}):
                        result["json_error_fields"].append(key)
    status = result["status"] or 0
    result["checks"] = {
        "url_allowed": True,
        "method_known": True,
        "server_answered": status > 0,
        # 5xx 单独一条：它既是「服务在应答」的凭据，也是「这次不该通过」的证据。
        "not_server_error": not (500 <= status <= 599),
    }
    result["ok"] = all(result["checks"].values())
    return result


# --------------------------------------------------------------------------- #
# cli：跑一条命令，除了退出码还看别的
# --------------------------------------------------------------------------- #

def _split_argv(text: str) -> list[str]:
    """没有注入 `argv_of` 时的兜底拆分。

    用 `shlex`（posix 模式）而不是 `str.split`：后者会把 `python -c "print(1)"`
    拆成 `-c` 和 `"print(1)"` 两段——引号原样留在参数里，命令跑出来的行为和
    你写的不一样，而这种「看起来跑了其实跑错了」的偏差最难查。
    posix=True 是刻意的：本工具跑的是命令行，不是 cmd 批处理。
    """
    import shlex
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        # 引号不配对这类错误交给上层的「命令解析后是空的」去报，
        # 这里不吞掉原因——`cli_probe` 会明确说解析失败。
        return []


def _undefined_names(text: str) -> list[str]:
    """Python 里「引用了但没定义」的名字：`NameError: name 'x' is not defined`。"""
    names: list[str] = []
    for match in re.finditer(r"NameError: name '([^']+)' is not defined", str(text or "")):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names[:MAX_NAMES]


def cli_probe(root: str, command: str, *, timeout: int = DEFAULT_TIMEOUT,
              expect_text: str = "", forbid_text: str = "", allow_silent: bool = False,
              runner=process_runner.run_bounded,
              argv_of=None, gate=None) -> dict[str, Any]:
    """跑一条项目内命令，回结构化凭据。

    比退出码多给的东西，都是「退出码 0 但事情没成」的常见形态：输出为空、
    里有 ERROR 级行、有 traceback、有 NameError、输出被截断。

    **命令解析与安全门一律复用 tools 层**（`argv_of` / `gate` 由调用方注入）：
    自己 `text.split()` 会把 `-c "print(1)"` 拆成三段（实测这是真的），更重要的是
    会绕开 `run_command` 那道黑名单——那等于给这条工具开了一个后门。这里默认走
    `shlex`，拿不到注入时也只是退回 POSIX 拆分，**绝不**自己发明一套 shell。
    """
    result: dict[str, Any] = {
        "ok": False, "exit_code": None, "elapsed": None, "command": command,
        "output": "", "checks": {}, "error": "", "notes": [],
        "error_lines": [], "traceback_tail": "", "undefined_names": [],
    }
    text = str(command or "").strip()
    if not text:
        result["error"] = "没给要跑的命令。"
        result["checks"] = {"command_given": False}
        return result
    if not os.path.isdir(root or ""):
        result["error"] = "代码根目录不存在或未配置。"
        result["checks"] = {"command_given": True, "root_exists": False}
        return result
    if gate is not None:
        blocked, why = gate(text)
        if blocked:
            result["error"] = "命令被安全策略拦截（命中「%s」）——这条观察没有发生。" % why
            result["checks"] = {"command_given": True, "root_exists": True,
                                "command_allowed": False}
            return result
    argv = list(argv_of(text)) if argv_of is not None else _split_argv(text)
    if not argv:
        result["error"] = "命令解析后是空的。"
        result["checks"] = {"command_given": True, "root_exists": True,
                            "command_allowed": True, "process_started": False}
        return result
    seconds = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    rep = runner(argv, cwd=root, timeout=seconds, command_text=text)
    result["elapsed"] = rep.get("elapsed")
    result["exit_code"] = rep.get("exit_code")
    output = str(rep.get("output") or "")
    result["output"] = output
    if rep.get("error") and rep.get("exit_code") is None:
        result["error"] = "命令没能跑起来：%s" % rep["error"]
        result["checks"] = {"command_given": True, "root_exists": True,
                            "command_allowed": True, "process_started": False}
        return result
    if rep.get("timed_out"):
        result["error"] = "命令超过 %d 秒被终止（超时不是通过）。" % seconds
    scanned = scan_output(output, truncated=bool(rep.get("truncated")))
    result["error_lines"] = scanned["error_lines"]
    result["traceback_tail"] = scanned["traceback_tail"]
    result["undefined_names"] = _undefined_names(output)
    if scanned["truncated"]:
        result["notes"].append(
            "输出被截断（原始 %s 字符）——后面的内容没看到，不能当「后面没有错」。"
            % rep.get("total_chars"))
    if rep.get("timed_out"):
        result["notes"].append("超时的进程已被终止，但它写下的副作用留在磁盘上了。")
    expect = str(expect_text or "").strip()
    forbid = str(forbid_text or "").strip()
    has_expect = None
    if expect:
        has_expect = expect in output
        if not has_expect:
            result["notes"].append("输出里找不到 %r——这次运行没有产生你期待的证据。" % expect[:60])
    has_forbid = bool(forbid) and forbid in output
    if has_forbid:
        result["notes"].append("输出里出现了禁止出现的 %r。" % forbid[:60])
    result["checks"] = {
        "command_given": True,
        "root_exists": True,
        "command_allowed": True,
        "process_started": True,
        "finished": not bool(rep.get("timed_out")),
        "exit_zero": rep.get("exit_code") == 0,
        # 退出码 0 只说明没崩；一个什么都没打印的 CLI 和一个干了活的 CLI
        # 在退出码上无法区分，所以「有输出」单独成一条（显式 allow_silent 时除外）。
        "produced_output": bool(output.strip()) or bool(allow_silent),
        "no_error_lines": scanned["error_line_count"] == 0,
        "no_traceback": not scanned["has_traceback"],
        "no_undefined_names": not result["undefined_names"],
        "no_truncated_output": not scanned["truncated"],
    }
    if expect:
        result["checks"]["expected_text_present"] = bool(has_expect)
    if forbid:
        result["checks"]["forbidden_text_absent"] = not has_forbid
    result["ok"] = all(result["checks"].values())
    return result


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def render_http(result: dict[str, Any], *, expect_status: int = 0,
                expect_text: str = "") -> str:
    # 被 URL 边界或方法名拒掉时，checks 里只有一条 false、没有 status——这时
    # 「未通过」不说明**为什么**，必须把原话带出来（实测踩过：只看到
    # "url_allowed: False"，看不出是越界还是方法不认）。
    if result.get("error") and result.get("status") is None:
        return "dev_probe(http) 未完成（没有发出任何请求）：" + str(result["error"])
    checks = result.get("checks") or {}
    status = result.get("status")
    health_ok = bool(result.get("ok"))
    want = int(expect_status or 0)
    needle = str(expect_text or "").strip()
    body = str(result.get("body_excerpt") or "")
    # 两层分开：health_ok 只说「服务有没有正常应答、是不是 5xx」，跟调用方想验收
    # 什么无关；验收层只在显式设了 expect 时才判达没达标。以前抬头词只认 health_ok，
    # 于是 401 配 expect_status=200 时底行已老实写「凭据不符」，抬头却还报【通过】，
    # 把这句细糠盖掉了——这里让抬头词如实反映「观察完成但验收未达」，不动 result["ok"]。
    expect_set = bool(want) or bool(needle)
    expect_met = (not want or status == want) and (not needle or needle in body)
    if not health_ok:
        verdict = "未通过"
    elif expect_set and not expect_met:
        verdict = "观察完成·验收未达"
    else:
        verdict = "通过"
    lines = ["HTTP 观察【%s】：%s → %s（%s 秒）" % (
        verdict, result.get("url") or "?",
        "HTTP %s" % status if status else "没有应答",
        result.get("elapsed"))]
    if want and status != want:
        lines.append("期望 HTTP %d，实际 %s——这是凭据不符，不是「服务没起」。" % (want, status))
    elif want:
        lines.append("状态码符合预期（%d）。" % want)
    if result.get("content_type"):
        lines.append("Content-Type：%s" % result["content_type"])
    if result.get("json_error_fields"):
        lines.append("响应 JSON 里带错误字段：%s" % ", ".join(result["json_error_fields"]))
    if needle:
        lines.append("要找的 %r：%s" % (needle[:60], "在响应里" if needle in body else "**不在**响应里"))
    if body:
        head = body[:1200]
        lines.append("响应正文（前 %d 字%s）：\n%s" % (
            len(body), "，已截断" if result.get("truncated") else "", head))
    else:
        lines.append("响应正文为空。")
    for name, ok in sorted(checks.items()):
        if not ok:
            lines.append("未通过的检查：%s" % name)
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    return "\n".join(lines)


def render_cli(result: dict[str, Any]) -> str:
    if result.get("error") and not result.get("checks", {}).get("process_started"):
        return "dev_probe(cli) 未完成：" + str(result["error"])
    checks = result.get("checks") or {}
    verdict = "通过" if result.get("ok") else "未通过"
    lines = ["命令观察【%s】：`%s` → exit=%s（%s 秒）" % (
        verdict, result.get("command") or "?",
        result.get("exit_code"), result.get("elapsed"))]
    for name, ok in sorted(checks.items()):
        if not ok:
            lines.append("  未通过：%s" % name)
    if result.get("error_lines"):
        lines.append("错误级日志行（%d 条，前 5 条）：" % len(result["error_lines"]))
        lines += ["  " + row for row in result["error_lines"][:5]]
    if result.get("traceback_tail"):
        lines.append("traceback 末行：%s" % result["traceback_tail"])
    if result.get("undefined_names"):
        lines.append("引用了但没定义的名字：%s" % ", ".join(result["undefined_names"]))
    output = str(result.get("output") or "")
    if output.strip():
        lines.append("输出尾部：\n%s" % output[-800:])
    else:
        lines.append("这条命令**没有任何输出**——退出码 0 在这里不构成「它干了活」的证据。")
    if result.get("error"):
        lines.append("为什么不可信：" + str(result["error"]))
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    return "\n".join(lines)


__all__ = ["BODY_LIMIT", "DEFAULT_TIMEOUT", "MAX_ERROR_LINES", "MAX_NAMES", "MAX_TIMEOUT",
           "cli_probe", "http_probe", "render_cli", "render_http", "scan_output"]
