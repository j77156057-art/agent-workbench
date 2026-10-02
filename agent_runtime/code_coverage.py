"""让 `coverage: true` 真的给出数字，而不是又一条「未启用」。

`dev_testrun` 一直带着一句诚实的免责声明：「本仓没装 coverage/pytest-cov，
`coverage: true` 直接回未启用——这是【没测】，不是 0%」。那是当时能做的最好选择，
因为说谎比缺功能更贵。但它留了一个洞：**没有任何测试能回答「我这次改动覆盖到了
吗」**。而这恰恰是 vibecoding 里最要紧的一个问题——你改完一段逻辑，跑一遍全量绿了，
然后完全不知道新写的那些分支有没有被任何用例走过。

本模块把这个洞补上，并且只做一件别处没有的事：

**按改动文件回答，而不是给一个全仓百分比。**

全仓覆盖率是个会骗人的数字：一条 87% 的横幅可能完全没碰到你刚写的三行分支。
真正能下手的凭据是「我改的这个文件里，哪些行没被执行过」——所以这里的主输出
是 `uncovered`（文件 + 行号 + 该行源码），`total` 只是附带。

三条 fail-closed，与仓库其它地方一致：

1. **coverage 没装就明说没装**，绝不把「没测」折算成 0% 或 100%。
2. **JSON 报告读不出来就判未通过**。实测踩过：`.coverage` 数据存在，但 `coverage json`
   因为仓里有一个语法坏掉的文件而整体失败——那种情况下「覆盖率未知」和「覆盖率很高」
   长得一模一样，不能当成后者。
3. **扫描被上限截断 / 有文件解析不了，都不能读成「覆盖到了」**。这两件事单列，
   并让 `complete=False`；`ok` 不看它们（那不是覆盖率的问题），但 `complete` 会让
   调用方知道这份覆盖率能信到什么程度。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from agent_runtime import process_runner

#: 一次最多列多少个「有未覆盖行」的文件。
MAX_FILES = 60
#: 每个文件最多列多少行未覆盖。总数与「文件级百分比」另外给，不靠这个上限撑门面。
MAX_LINES_PER_FILE = 40
#: 只看改动文件时，一次最多接受多少个。
MAX_TARGET_FILES = 24

#: 覆盖率数字的口径。低于它就单独提示一句，但**不改变 ok**——
#: 「覆盖率只有 40%」是事实，「所以这次运行失败」是另一个判断，别混成一条。
LOW = 70.0


def coverage_available() -> bool:
    """两个包任一在就算可用：纯 `coverage run` 或 `pytest-cov` 插件都认。"""
    import importlib.util
    return bool(importlib.util.find_spec("coverage"))


def _rel(root: str, path: str) -> str:
    text = str(path or "").replace("\\", "/")
    base = str(root).replace("\\", "/").rstrip("/")
    if text.startswith(base + "/"):
        return text[len(base) + 1:]
    if os.path.isabs(text):
        try:
            return os.path.relpath(os.path.normpath(text), os.path.normpath(root)).replace("\\", "/")
        except ValueError:
            return text
    return text.lstrip("./")


def parse_report(text: str, root: str, *, targets: list[str] | None = None
                 ) -> dict[str, Any]:
    """解析 `coverage json -o -` 的输出。

    `coverage json` 失败时会把它对**每个**无法解析的文件都打一行警告，正文里混着
    人话（实测："Couldn't parse ... as Python source: unterminated string literal"）。
    所以这里不靠「第一个 `{`」去切 JSON，而是**从末尾找最后一个完整的 JSON 对象**——
    宁可多花几行，也不要把一段警告当成报告。
    """
    body = str(text or "")
    start = body.find("{")
    payload = None
    parsed_from = None
    if start >= 0:
        try:
            payload = json.loads(body[start:])
            parsed_from = "json"
        except ValueError:
            payload = None
    if payload is None:
        # 最后一次机会：从后往前扫，找一个能独立解析成功的对象起点。
        for index in range(len(body) - 1, max(-1, len(body) - 4_000_000), -1):
            if body[index] != "{":
                continue
            try:
                candidate = json.loads(body[index:])
            except ValueError:
                continue
            if isinstance(candidate, dict) and "files" in candidate:
                payload = candidate
                parsed_from = "json-tail"
                break
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        return {"ok": False, "parsed": False, "error":
                "coverage 的 JSON 报告读不出来（这是【覆盖率未知】，不是「覆盖率高」）。"
                "原始输出尾部：\n%s" % body.strip()[-400:], "notes": []}
    return {"ok": True, "parsed": True, "error": "", "parsed_from": parsed_from,
            "report": payload, "notes": []}


def summarize(payload: dict[str, Any], root: str, *, targets: list[str] | None = None,
              max_lines: int = MAX_LINES_PER_FILE) -> dict[str, Any]:
    """从报告里算出「哪些文件、哪些行没被执行过」。"""
    files = payload.get("files") or {}
    wanted = {str(item).replace("\\", "/").strip() for item in (targets or []) if str(item).strip()}
    rows: list[dict[str, Any]] = []
    incomplete: list[str] = []
    for name, body in files.items():
        rel = _rel(root, str(name))
        summary = (body or {}).get("summary") or {}
        missing = [int(line) for line in ((body or {}).get("missing_lines") or [])]
        # coverage 自己会标「这行有注释/不可执行」，别把它算成没覆盖。
        excluded = {int(line) for line in ((body or {}).get("excluded_lines") or [])}
        missing = sorted(line for line in missing if line not in excluded)
        if wanted and rel not in wanted and (rel + ".py") not in wanted:
            continue
        if not missing:
            continue
        rows.append({
            "file": rel,
            "percent": float(summary.get("percent_covered") or 0.0),
            "statements": int(summary.get("num_statements") or 0),
            "missing_lines": missing[:max_lines],
            "missing_total": len(missing),
            "partial": len(missing) > max_lines,
        })
    rows.sort(key=lambda row: (row["percent"], row["file"]))
    truncated = len(rows) > MAX_FILES
    totals = payload.get("totals") or {}
    total_percent = float(totals.get("percent_covered") or 0.0)
    out: dict[str, Any] = {
        "ok": True,
        "total_percent": round(total_percent, 1),
        "covered_lines": int(totals.get("covered_lines") or 0),
        "num_statements": int(totals.get("num_statements") or 0),
        "covered_percent_display": payload.get("coverage_percent_display") or "coverage",
        "files_with_gaps": len(rows),
        "files": rows[:MAX_FILES],
        "truncated": truncated,
        "scope": ("targets" if wanted else "all"),
        "targets_matched": sorted(wanted & {row["file"] for row in rows}) if wanted else [],
        "targets_without_gaps": sorted(
            name for name in wanted
            if name not in {row["file"] for row in rows}
            and any(_rel(root, key) in (name, name + ".py") for key in files)),
        "targets_not_in_report": sorted(
            name for name in wanted
            if not any(_rel(root, key) in (name, name + ".py") for key in files)),
        "incomplete": incomplete,
    }
    if out["num_statements"] == 0:
        # 「0 条语句」不是「100% 覆盖」，也不是「0%」。它是没有可测的东西。
        out["ok"] = False
        out["error"] = ("覆盖率报告里一条语句都没有——这通常是 `--source` 圈错了范围，"
                        "不是覆盖率为 0，也不是 100%。")
    if out["targets_not_in_report"]:
        out["ok"] = False
        out["error"] = ("这些文件根本没出现在覆盖率报告里（所以它们的覆盖率【未知】，"
                        "不能读成「已覆盖」）：%s" % ", ".join(out["targets_not_in_report"]))
    return out


def run(root: str, *, paths: list[str] | None = None, timeout: int = 600,
        targets: list[str] | None = None,
        runner: Callable[..., dict[str, Any]] = process_runner.run_bounded
        ) -> dict[str, Any]:
    """在一次真实的 pytest 运行上量覆盖率。

    两趟：先 `coverage run -m pytest`（真的把测试跑一遍），再 `coverage json`
    （只读那份数据文件）。**不用 `pytest-cov` 插件**——插件会把覆盖率报告塞进
    pytest 的 stdout 里，那段文本会和测试摘要混在一起，解析它就得再写一套「怎么从
    一堆 stdout 里认出哪几行是报告」的启发式，而那正是这个仓库反复吃亏的地方。
    分成两趟之后，每一趟的输出都只有一种含义。
    """
    base = Path(os.path.abspath(root or ""))
    started = time.monotonic()
    if not base.is_dir():
        return {"ok": False, "complete": False, "error": "代码根目录不存在或未配置。",
                "checks": {}}
    if not coverage_available():
        return {"ok": False, "complete": False,
                "error": "覆盖率没有启用：本环境里 `coverage` 不在。这是【没测】，"
                         "不是 0%%——要先决定加不加这个依赖（pip install coverage）。",
                "checks": {"coverage_installed": False}}
    wanted = [str(item).replace("\\", "/").strip() for item in (targets or [])]
    if len(wanted) > MAX_TARGET_FILES:
        return {"ok": False, "complete": False,
                "error": "一次最多看 %d 个改动文件（当前 %d）；请分批，或直接看全仓。"
                         % (MAX_TARGET_FILES, len(wanted)),
                "checks": {"targets_bounded": False}}
    data_file = base / ".tmp" / "cov-devtestrun" / ".coverage"
    try:
        data_file.parent.mkdir(parents=True, exist_ok=True)
        if data_file.exists():
            data_file.unlink()
    except OSError as exc:
        return {"ok": False, "complete": False,
                "error": "准备覆盖率数据文件失败：%s" % exc, "checks": {}}

    argv = [sys.executable, "-m", "coverage", "run",
            "--data-file=%s" % data_file,
            "--source=%s" % base,
            "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
            "--basetemp=%s" % (base / ".tmp" / "pt-covrun")]
    argv += list(paths or [])
    seconds = max(30, min(int(timeout or 600), 1800))
    run_report = runner(argv, cwd=str(base), timeout=seconds,
                        command_text="coverage run -m pytest …")
    checks = {"coverage_installed": True, "pytest_ran": run_report.get("exit_code") is not None}
    # 超时**先判**：`run_bounded` 超时时同时给 `error` 与 `timed_out`、`exit_code=None`，
    # 先看 error 会把「跑太久被掐」误报成「根本没起来」——两者的下一步完全不同
    # （一个该缩范围或加预算，一个该查命令名/路径）。
    if run_report.get("timed_out"):
        return {"ok": False, "complete": False,
                "checks": dict(checks, pytest_finished=False),
                "error": "带覆盖率的 pytest 超过 %d 秒被终止（超时不是「覆盖率未知」，"
                         "是这次根本没测完）。请缩小 paths 或加大 timeout。" % seconds}
    if run_report.get("error") and run_report.get("exit_code") is None:
        return {"ok": False, "complete": False, "checks": checks,
                "error": "带覆盖率的 pytest 没能跑起来：%s" % run_report["error"]}

    # **报告写文件，不走 stdout**——这是被本仓反复咬过的地方：`coverage json -o -`
    # 在本仓会产出 200KB+ 的 JSON，而 `process_runner` 的输出缓冲默认只留头尾各
    # 4000 字符，于是 JSON 被拦腰截断、`json.loads` 抛错、报告读不出来。
    # 实测：截断后的尾部正好停在 JSON 中段，肉眼完全看不出它被切过——
    # 这正是「假干净」最贵的那种形态。写文件就没有这个问题。
    report_path = data_file.with_suffix(".json")
    json_argv = [sys.executable, "-m", "coverage", "json", "--ignore-errors",
                 "--data-file=%s" % data_file, "-o", str(report_path)]
    json_report = runner(json_argv, cwd=str(base), timeout=180,
                         command_text="coverage json -o <tmp>")
    notes: list[str] = []
    if json_report.get("error") and json_report.get("exit_code") is None:
        return {"ok": False, "complete": False, "checks": checks,
                "error": "coverage json 没能跑起来：%s" % json_report["error"]}
    try:
        raw_report = report_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # 数据文件在，但报告没落盘——「覆盖率未知」必须原样报出去。
        notes.append("测试本身跑完了（exit=%s），但覆盖率报告没能生成（%s）。"
                     % (run_report.get("exit_code"), exc))
        return {"ok": False, "complete": False,
                "checks": dict(checks, report_parsed=False),
                "error": "覆盖率报告文件读不出来：%s（这是【覆盖率未知】，不是「覆盖率高」）。"
                         % exc,
                "pytest_exit_code": run_report.get("exit_code"),
                "notes": notes, "elapsed": round(time.monotonic() - started, 2)}
    parsed = parse_report(raw_report, str(base))
    if not parsed.get("parsed"):
        # 实测（本仓真踩到）：仓里有一个语法坏掉的 .py 时，不带 `--ignore-errors`
        # 的 `coverage json` 会**整体失败、一条 JSON 都不吐**（exit=1，stdout 只有
        # 一行 "Couldn't parse …"）。那种情况下「未知」和「很高」在界面上长得一模一样，
        # 所以宁可报未知也不折算。
        notes.append("测试本身跑完了（exit=%s），但覆盖率报告没能解析。"
                     % run_report.get("exit_code"))
        return {"ok": False, "complete": False,
                "checks": dict(checks, report_parsed=False),
                "error": parsed["error"], "pytest_exit_code": run_report.get("exit_code"),
                "notes": notes, "elapsed": round(time.monotonic() - started, 2)}
    checks["report_parsed"] = True
    # `--ignore-errors` 让 coverage 跳过读不了的文件、继续把其余文件的报告写完。
    # 但被跳过的文件**既不算已覆盖也不算未覆盖**——它们的覆盖率未知，必须单列，
    # 否则一份 99% 的报告会让人以为整个仓都量到了。带 `-o <file>` 时 warning 进
    # JSON 的 `warnings` 字段，所以主要看那里。
    unparsed = _unparsed_from_report(parsed["report"]) \
        or _unparsed_sources(json_report.get("output") or "")
    unparsed = sorted(set(unparsed))
    if unparsed:
        notes.append("有 %d 个文件 coverage 读不了源码（多半是语法本身就坏），"
                     "它们的覆盖率【未知】，既不算已覆盖也不算未覆盖：%s"
                     % (len(unparsed), ", ".join(os.path.basename(item) for item in unparsed[:5])))

    summary = summarize(parsed["report"], str(base), targets=wanted or None)
    summary["checks"] = checks
    summary["notes"] = list(summary.get("notes") or []) + notes
    summary["unparsed_sources"] = unparsed[:20]
    summary["complete"] = not unparsed
    summary["pytest_exit_code"] = run_report.get("exit_code")
    summary["pytest_finished"] = not bool(run_report.get("timed_out"))
    summary["elapsed"] = round(time.monotonic() - started, 2)
    # 覆盖率低**不是**运行失败；但覆盖率未知是。两件事分开。
    if summary.get("error"):
        summary["ok"] = False
    return summary


def _unparsed_sources(text: str) -> list[str]:
    """从 `coverage json` 的警告里挑出「解析不了的文件」。"""
    found: list[str] = []
    for line in str(text or "").splitlines():
        if "Couldn't parse" not in line:
            continue
        head = line.split("Couldn't parse", 1)[1].strip()
        if head.startswith("'"):
            head = head[1:]
        name = head.split("'")[0].split('"')[0].strip()
        if name and name not in found:
            found.append(name)
    return found


def _unparsed_from_report(payload: dict[str, Any]) -> list[str]:
    """从报告自带的 `warnings` 里挑出「解析不了的文件」。

    带 `--ignore-errors` 时 warning 会进 JSON 而不是打到 stdout，所以两处都要看——
    只看一处就会漏掉一半，而漏掉的那一半会被静默读成「已覆盖」。
    """
    found: list[str] = []
    for row in payload.get("warnings") or ():
        if "Couldn't parse" in str(row):
            found.extend(_unparsed_sources(str(row)))
    return found


def render(result: dict[str, Any]) -> str:
    error = str(result.get("error") or "")
    # 「没跑成」和「跑成了但这份数字不能用」是两件事，下一步也不同：前者要查命令/预算，
    # 后者要查报告来源。合成一句「未完成」会把后者说成「什么都没做」。
    ran = bool(result.get("checks", {}).get("report_parsed")) \
        or result.get("total_percent") is not None
    if error and not ran:
        return "覆盖率未完成：" + error
    checks = result.get("checks") or {}
    lines: list[str] = []
    if result.get("ok"):
        lines.append("覆盖率：全仓 %.1f%%（%d/%d 条语句），pytest exit=%s。"
                     % (result.get("total_percent", 0.0), result.get("covered_lines", 0),
                        result.get("num_statements", 0), result.get("pytest_exit_code")))
    else:
        lines.append("覆盖率【不可信】：%s" % (error or "未知原因"))
    scope = result.get("scope") or "all"
    if scope == "targets":
        lines.append("按你给的改动文件看（`targets:` 圈定范围）：")
        for name in result.get("targets_not_in_report") or []:
            lines.append("  %s —— **不在报告里**，覆盖率未知" % name)
        for name in result.get("targets_without_gaps") or []:
            lines.append("  %s —— 在报告里且没有未覆盖行" % name)
    rows = result.get("files") or []
    if rows:
        lines.append("有未覆盖行的文件 %d 个（按覆盖率升序）：" % len(rows))
        for row in rows[:20]:
            lines.append("  %-52s %5.1f%%  缺 %d 行" % (
                row["file"], row["percent"], row["missing_total"]))
            shown = row["missing_lines"][:12]
            lines.append("      行号：%s%s" % (
                ", ".join(str(line) for line in shown),
                " …（共 %d 行）" % row["missing_total"] if row["partial"] else ""))
        if result.get("truncated"):
            lines.append("  …还有文件没列出来（上限 %d）。" % MAX_FILES)
    elif result.get("ok"):
        lines.append("没有列出任何「有未覆盖行」的文件。")
    if not result.get("complete"):
        lines.append("**这份覆盖率不完整**——有文件根本没被量到，别读成「都覆盖到了」。")
    for name, ok in sorted(checks.items()):
        if not ok:
            lines.append("未通过的检查：%s" % name)
    if result.get("ok") and result.get("total_percent", 100.0) < LOW:
        lines.append("提示: 全仓覆盖率 %.1f%% 偏低。这只是事实，不影响本次运行的判定——"
                     "真正要紧的是上面按文件列出的那些行。" % result["total_percent"])
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    lines.append("边界：这条只量「哪些行被执行过」。它不判断测试对不对——"
                 "一行被执行 100 次也可能断言是错的；覆盖率满了也不能替代 dev_testrun "
                 "的通过判定。")
    return "\n".join(lines)


__all__ = ["LOW", "MAX_FILES", "MAX_LINES_PER_FILE", "MAX_TARGET_FILES",
           "coverage_available", "parse_report", "render", "run", "summarize"]
