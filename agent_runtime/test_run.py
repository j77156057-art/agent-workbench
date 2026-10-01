"""Wave15：把"跑测试 → 读失败 → 只重跑失败"变成结构化的、可信的一等步骤。

以前这一段只能靠 `run_command("pytest -q")` 然后把整屏文本喂回去自己读，三个具体的坑：

1. **0 个测试也是"绿"的错觉**。pytest 在没收集到任何用例时退出码是 **5**（实测本仓
   `pytest .tmp/empty_t` → exit 5），而 5 既不是成功也不是"失败了几个"，很容易被当成通过；
   `--basetemp` 缺失或选择器写错时就是这个形状。
2. **`-q` 的失败摘要会丢**。整屏 text 里 `FAILED a.py::t - AssertionError...` 才是能下手的
   信息，模型读文本时最容易漏的就是这几行。
3. **`--lf` 在这台机器上不能用**：本工作树的 `.pytest_cache/` 是 `Permission denied`
   （实测每条 pytest 都带 `PytestCacheWarning: could not create cache path
   D:\\WorkBuddy\\rag-agent\\.pytest_cache\\v\\cache`），pytest 自己的 lastfailed 存不下来。
   所以"只重跑上次失败的"由本模块自己记账（状态目录里的 last-failed.json），
   并且一律带 `-p no:cacheprovider`——否则每次跑都在刷那条警告，也把"缓存写成功"
   误当成"记账成功"。

**覆盖率这一条不做假动作**：本仓 `.venv` 里既没有 `coverage` 也没有 `pytest-cov`（实测
`find_spec` 都是 False），所以 `coverage: true` 直接回"未启用，缺依赖"，不静默跳过、
更不把"没测"报成 0% 或 100%。

判定一律 fail-closed：跑不起来 / 没收集到 / 摘要解析不出来 / 退出码与计数不自洽，
统统算未通过。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from agent_runtime import process_runner

STATE_NAME = "testrun/last-failed.json"
#: 一次重跑最多带这么多 nodeid：再多说明上次是整片红，该跑全量而不是重跑。
MAX_NODEIDS = 200
#: `related` 一次最多带多少个测试文件：再多说明改得太宽，该跑全量。
MAX_RELATED_FILES = 24
#: pytest 的已知退出码：0 通过 1 有用例失败 2 中断 3 内部错误 4 用法错误 5 没收集到。
KNOWN_EXIT_CODES = frozenset({0, 1, 2, 3, 4, 5})
DEFAULT_TIMEOUT = 300.0
MAX_TIMEOUT = 1500.0
_NODEID_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?$")
_COUNT_RES = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "error": re.compile(r"(\d+) error"),
    "skipped": re.compile(r"(\d+) skipped"),
    "xfailed": re.compile(r"(\d+) xfailed"),
    "xpassed": re.compile(r"(\d+) xpassed"),
}
_SUBTEST_RE = re.compile(r"(\d+) subtests? passed")
_DURATION_RE = re.compile(r"in ([0-9.]+)m?s")
_NO_TESTS_RE = re.compile(r"(no tests ran|no tests were run)", re.I)
#: `--tb=line` 每个失败就一行：`<文件>:<行号>: <异常>: <消息>`。摘要里的 FAILED 只有 nodeid，
#: 光靠它没法下手，所以要把这一行的位置与消息并回去。
_LINE_TB_RE = re.compile(r"^(?P<file>\S+\.py):(?P<line>\d+): (?P<msg>.+)$")


class TestRunError(RuntimeError):
    """测试回路无法开始。"""


def parse_output(text: str, exit_code: int | None) -> dict[str, Any]:
    """把 pytest 的输出变成计数 + 可下手的失败清单。"""
    body = str(text or "")
    counts = {key: 0 for key in _COUNT_RES}
    matched = {}
    for key, pattern in _COUNT_RES.items():
        found = pattern.findall(body)
        if found:
            matched[key] = True
            counts[key] = sum(int(value) for value in found)
        else:
            matched[key] = False
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        hit = _NODEID_RE.match(line.strip())
        if hit:
            rows.append({"kind": hit.group(1).lower(), "nodeid": hit.group(2),
                         "reason": (hit.group(3) or "").strip()[:300]})
    trace = []
    for line in body.splitlines():
        tb = _LINE_TB_RE.match(line.strip())
        if tb:
            trace.append({"location": "%s:%s" % (tb.group("file").replace("\\", "/"),
                                                 tb.group("line")),
                          "message": tb.group("msg").strip()[:300]})
    # 顺序对应：`--tb=line` 每个失败打一行，摘要里每个失败也打一行。数量对不上时宁可留空，
    # 也不能把 A 的错误消息配到 B 的 nodeid 上。
    if trace and len(trace) == len(rows):
        for row, tb in zip(rows, trace):
            row["location"] = tb["location"]
            if not row["reason"]:
                row["reason"] = tb["message"]
    subtests = _SUBTEST_RE.findall(body)
    summary = matched.get("failed", False) or matched.get("passed", False) \
        or matched.get("error", False) or matched.get("skipped", False)
    return {"counts": counts, "failures": rows,
            "subtests_passed": int(sum(int(v) for v in subtests)) if subtests else 0,
            "duration": next((float(v) for v in _DURATION_RE.findall(body)), None),
            "exit_code": exit_code,
            "collected": (counts["passed"] + counts["failed"] + counts["error"]
                          + counts["skipped"] + counts["xfailed"] + counts["xpassed"]) > 0
                         or counts["failed"] > 0 or bool(rows),
            "no_tests_ran": bool(_NO_TESTS_RE.search(body)),
            "summary_parsed": bool(summary)}


def keep_project_location(row: dict[str, Any], root: str) -> dict[str, Any]:
    """`--tb=line` 对 unittest 风格断言打的是标准库那一帧（`…/unittest/case.py:900`），
    对下标没有用。位置落回【测试文件本身】，标准库那帧另存着，不假装它是现场。"""
    location = str(row.get("location") or "").replace("\\", "/")
    base = str(root).replace("\\", "/").rstrip("/")
    if location and base and not location.startswith(base + "/"):
        row["external_frame"] = location
        row["location"] = row["nodeid"].split("::", 1)[0]
    return row


def command(*, paths: list[str], keyword: str, nodeids: list[str],
            basetemp: str) -> list[str]:
    """固定的 pytest 命令行：没有 shell、没有任意参数注入空间。"""
    argv = ["-m", "pytest", "-q", "--no-header", "-rf", "--tb=line",
            # 本工作树的 .pytest_cache 写不进去（Permission denied），记账由 last-failed.json 做
            "-p", "no:cacheprovider",
            # 用户名带引号时默认 pytest-of-* 目录会 PermissionError，必须钉在项目内
            "--basetemp=%s" % basetemp]
    if keyword:
        argv += ["-k", keyword]
    argv += list(paths)
    argv += list(nodeids)
    return argv


def state_path(root: str | Path) -> Path:
    import project_state
    return Path(project_state.path(root, STATE_NAME))


def record_failures(root: str | Path, rows: list[dict[str, Any]], *,
                    paths: list[str], keyword: str) -> dict[str, Any]:
    """把这次失败的 nodeid 记下来，供下一次 `only_failed: true` 用。"""
    nodeids = [row["nodeid"] for row in rows if valid_nodeid(row["nodeid"])]
    target = state_path(root)
    payload = {"written_at": time.time(), "nodeids": nodeids[:MAX_NODEIDS],
               # total 记的是【真实失败条数】，不是截过的清单长度
               "total": len(nodeids), "truncated": len(nodeids) > MAX_NODEIDS,
               "scope": {"paths": paths, "keyword": keyword}}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
                       newline="\n")
        os.replace(tmp, target)
    except OSError as exc:
        return {"ok": False, "error": "失败清单记不下来：%s" % exc}
    return {"ok": True, "recorded": len(payload["nodeids"]), "truncated": payload["truncated"]}


def load_record(root: str | Path) -> dict[str, Any]:
    """读回记账。`total` 是【当时失败了几条】，`nodeids` 是被上限截过的清单——
    两者不等就说明上次整片红，逐条重跑没意义。"""
    try:
        data = json.loads(state_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"nodeids": [], "total": 0, "written_at": None}
    if not isinstance(data, dict):
        return {"nodeids": [], "total": 0, "written_at": None}
    rows = [str(item) for item in (data.get("nodeids") or []) if valid_nodeid(str(item))]
    return {"nodeids": rows, "total": int(data.get("total") or len(rows)),
            "written_at": data.get("written_at")}


def load_failures(root: str | Path) -> list[str]:
    return list(load_record(root)["nodeids"])


def valid_nodeid(text: str) -> bool:
    """nodeid 会变成 argv 的一项：不接受空、选项样式、换行、绝对路径或 `..`。"""
    clean = str(text or "")
    return bool(clean) and not clean.startswith("-") and ".." not in clean \
        and "::" in clean and not Path(clean).is_absolute() and not any(
            ch in clean for ch in ("\n", "\r", "\x00"))


def test_files(root: str | Path, *, limit: int = 4000) -> list[str]:
    """仓里的测试文件清单：先看 `tests/`，没有再退到全仓找 `test_*.py`。"""
    base = Path(root)
    for pool in ((base / "tests"), base):
        if not pool.is_dir():
            continue
        found = sorted(str(path.relative_to(base)).replace("\\", "/") for path in pool.rglob("test_*.py")
                       if path.is_file() and not any(
                           part.startswith(".") or part == "__pycache__" for part in path.relative_to(base).parts))
        if found:
            return found[:limit]
    return []


def related_files(root: str | Path, files: list[str], *,
                  limit: int = MAX_RELATED_FILES) -> dict[str, Any]:
    """把改过的源文件映射到【磁盘上真的存在】的测试文件。

    不用 `--collect-only` 拿全仓 nodeid 来做这件事：实测本仓的收集结果有 243,591 个字符，
    而有界缓冲只留 8,022（`truncated=True`），大部分 nodeid 在截断里丢了，相干选择会退化成
    "什么都找不到"。按文件名在磁盘上选，代价与仓库大小无关。
    """
    names = []
    for item in files:
        stem = Path(str(item).replace("\\", "/")).name
        names.append(stem[:-3] if stem.endswith(".py") else stem)
    names = [name for name in names if name]
    pool = test_files(root)
    by_name: dict[str, list[str]] = {name: [] for name in names}
    picked: list[str] = []
    for path in pool:
        for name in names:
            if name.lower() in path.lower():
                by_name[name].append(path)
                if path not in picked:
                    picked.append(path)
    return {"picked": picked[:limit], "names": names,
            "missed": [name for name in names if not by_name[name]],
            "by_name": by_name, "truncated": len(picked) > limit, "pool_size": len(pool)}


def sys_executable() -> str:
    import sys
    return sys.executable or "python"


def coverage_available() -> bool:
    import importlib.util
    return bool(importlib.util.find_spec("coverage")) or bool(importlib.util.find_spec("pytest_cov"))


def run(root: str | Path, *, paths: str = "", keyword: str = "", only_failed: bool = False,
        related: str = "", coverage: bool = False, timeout: float = DEFAULT_TIMEOUT,
        runner: Callable[..., dict[str, Any]] = process_runner.run_bounded) -> dict[str, Any]:
    """跑一批 pytest，回结构化判定 + 可直接下手的失败清单。"""
    base = Path(root).resolve()
    if not base.is_dir():
        return {"ok": False, "passed": False, "checks": {}, "error": "当前项目目录不存在。"}
    if coverage and not coverage_available():
        return {"ok": False, "passed": False, "checks": {},
                "error": "覆盖率没有启用：本仓环境里 `coverage` 与 `pytest_cov` 都不在。"
                         "这是【没测】，不是 0%——要先决定加不加这个依赖。"}
    selected = [item.strip() for item in re.split(r"[,\n]", str(paths or "")) if item.strip()]
    for item in selected:
        if item.startswith("-") or ".." in item or Path(item).is_absolute():
            return {"ok": False, "passed": False, "checks": {},
                    "error": "测试路径不接受这种写法：%s" % item[:120]}
    nodeids: list[str] = []
    if only_failed:
        record = load_record(base)
        if not record["nodeids"]:
            return {"ok": False, "passed": False, "checks": {},
                    "error": "没有上次失败的记账（或记账里的 nodeid 全不合法）。"
                             "先跑一次全量或指定范围，再 `only_failed: true`。"}
        # 记账时按上限截过，所以"要重跑多少条"必须看 total：201 条失败被存成 200 条清单后，
        # 再看清单长度就永远触发不了这条守卫（这是实测出来的）。
        if record["total"] > MAX_NODEIDS:
            return {"ok": False, "passed": False, "checks": {},
                    "error": "上次失败 %d 条，超过 %d 条上限——那是整片红，逐条重跑没有意义，"
                             "应该修到只剩几条再 `only_failed`。" % (record["total"], MAX_NODEIDS)}
        nodeids = record["nodeids"]
    related_info: dict[str, Any] = {}
    if related:
        wanted = [item.strip() for item in re.split(r"[,\n]", related) if item.strip()]
        related_info = related_files(base, wanted)
        if not related_info["picked"]:
            return {"ok": False, "passed": False, "checks": {"related_found": False},
                    "error": "这些源文件没有名字相干的测试文件：%s（在 %d 个测试文件里找过）。"
                             "别把【没跑到】说成【跑过了】。" % (
                                 ", ".join(related_info["names"]), related_info["pool_size"]),
                    "related": related_info}
        selected = related_info["picked"]
    if len(nodeids) > MAX_NODEIDS:
        return {"ok": False, "passed": False, "checks": {},
                "error": "要重跑的失败有 %d 条，超过 %d 条上限——这说明上次是整片红，"
                         "应该跑全量而不是逐条重跑。" % (len(nodeids), MAX_NODEIDS)}
    seconds = max(5.0, min(float(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    argv = [sys_executable()] + command(paths=selected, keyword=str(keyword or ""),
                                        nodeids=nodeids,
                                        basetemp=os.path.join(str(base), ".tmp", "pt-devtestrun"))
    started = time.monotonic()
    rep = runner(argv, cwd=str(base), timeout=int(seconds),
                 command_text=" ".join(argv[:6]) + (" …" if len(argv) > 6 else ""))
    text = str(rep.get("output") or "")
    exit_code = rep.get("exit_code")
    parsed = parse_output(text, exit_code)
    if rep.get("timed_out"):
        return {"ok": False, "passed": False, "checks": {"runner_ran": True, "finished": False},
                "error": "pytest 在 %s 秒内没跑完（输出截到这里：%s）" % (
                    int(seconds), text[-200:] or "无输出"),
                "command": argv, "elapsed": round(time.monotonic() - started, 2)}
    for row in parsed["failures"]:
        keep_project_location(row, str(base))
    counts = parsed["counts"]
    problems = counts["failed"] + counts["error"]
    checks = {
        "runner_ran": exit_code in KNOWN_EXIT_CODES,
        "finished": True,
        # 退出码 5 = 一个都没收集到；那不是通过
        "tests_collected": bool(parsed["collected"]) and exit_code != 5
                          and not parsed["no_tests_ran"],
        "summary_parsed": parsed["summary_parsed"],
        "no_failures": problems == 0,
        # 计数与退出码必须互相对得上：对不上就是输出被截断或被别的输出污染了
        "counts_agree_with_exit": (exit_code == 0) == (problems == 0) if exit_code is not None
                                  else False,
    }
    recorded = record_failures(base, parsed["failures"], paths=selected,
                              keyword=str(keyword or "")) \
        if parsed["failures"] else {"ok": True, "recorded": 0, "truncated": False}
    checks["rerun_record_kept"] = bool(recorded.get("ok"))
    return {"ok": all(checks.values()), "passed": checks["no_failures"],
            "checks": checks, "counts": counts, "failures": parsed["failures"][:40],
            "failure_total": len(parsed["failures"]),
            "subtests_passed": parsed["subtests_passed"],
            "duration_seconds": parsed["duration"], "elapsed": round(time.monotonic() - started, 2),
            "exit_code": exit_code, "command": argv, "scope": {
                "paths": selected, "keyword": str(keyword or ""),
                "nodeids": len(nodeids), "mode": ("only_failed" if only_failed else
                                                  "related" if related else
                                                  "paths" if selected else "all")},
            "related": related_info, "recorded": recorded,
            "coverage": "not_installed" if coverage else "off",
            "output_tail": text[-400:],
            "error": "" if all(checks.values()) else _why(checks, parsed, exit_code, seconds)}


def _why(checks: dict[str, bool], parsed: dict[str, Any], exit_code: int | None,
         seconds: float) -> str:
    if not checks["runner_ran"]:
        return "pytest 没跑出已知退出码（%r）：命令可能根本没起来。" % exit_code
    if not checks["tests_collected"]:
        return ("一个测试都没收集到（exit=%r）。范围写错 / 文件不存在 / 选择器太窄——"
                "这【不是通过】。" % exit_code)
    if not checks["summary_parsed"]:
        return "读不到 pytest 的计数摘要（输出被截断或被别的输出污染），不能当通过。"
    if not checks["counts_agree_with_exit"]:
        return "计数与退出码不自洽：解析到 %s 个失败/错误但退出码是 %r——报告不可信。" % (
            parsed["counts"]["failed"] + parsed["counts"]["error"], exit_code)
    if not checks["no_failures"]:
        return "%s 个用例未通过。" % (parsed["counts"]["failed"] + parsed["counts"]["error"])
    if not checks["rerun_record_kept"]:
        return "失败清单没记下来，下次 `only_failed` 会拿不到记账。"
    return "判定未通过（%s 秒内）。" % int(seconds)


def render(result: dict[str, Any]) -> str:
    if not result.get("checks"):
        return "测试回路未能开始：%s" % result.get("error", "未知原因")
    counts = result.get("counts") or {}
    head = "测试【%s】：pass %s / fail %s / error %s / skip %s（exit=%s，%s 秒）。" % (
        "通过" if result.get("ok") else "未通过", counts.get("passed", 0),
        counts.get("failed", 0), counts.get("error", 0), counts.get("skipped", 0),
        result.get("exit_code"), result.get("duration_seconds") or result.get("elapsed"))
    lines = [head, "范围：%s" % json.dumps(result.get("scope") or {}, ensure_ascii=False)]
    rows = result.get("failures") or []
    for row in rows[:12]:
        lines.append("%s %s%s" % (row["kind"].upper(), row["nodeid"],
                                  " — %s" % row["reason"] if row["reason"] else ""))
    if result.get("failure_total", 0) > len(rows):
        lines.append("（还有 %d 条失败没列出来）" % (result["failure_total"] - len(rows)))
    if rows:
        lines.append("只重跑这些：`only_failed: true`（已记账 %s 条 nodeid）。" %
                     (result.get("recorded") or {}).get("recorded", len(rows)))
    if result.get("related"):
        info = result["related"]
        lines.append("相干测试：命中 %d 条；没有相干测试的文件：%s" % (
            len(info.get("picked") or []), ", ".join(info.get("missed") or []) or "无"))
    if not result.get("ok"):
        lines.append("为什么不可信：%s" % result.get("error", ""))
    lines.append("边界：这条只报【跑到了什么、哪里红了】；覆盖率未启用（本仓没装 coverage/"
                 "pytest-cov），也不替代 CI 的全量 lane。")
    return "\n".join(lines)


__all__ = ["MAX_NODEIDS", "MAX_RELATED_FILES", "STATE_NAME", "TestRunError", "command",
           "coverage_available", "keep_project_location", "load_failures", "load_record",
           "parse_output", "record_failures", "related_files", "render", "run", "state_path",
           "sys_executable", "test_files", "valid_nodeid"]
