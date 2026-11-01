# -*- coding: utf-8 -*-
"""把测试日志压成一份「红名单」，并写成 GitHub 注释（api.github.com 可读）。

为什么需要：CI 的日志下载地址是 objects.githubusercontent.com，本机连不上；
而 `gh api .../check-runs/<id>/annotations` 走 api.github.com，一直是通的。
所以让 workflow 把每条红打成一个 `::error file=...,title=...::`，红名单就有了
一个**不依赖下载域名**的读取通道。

输入是测试命令的完整输出（unittest 或 pytest 都认）：
    python tools/ci_red_summary.py /tmp/unittest.log

输出两类内容：
1. stdout 的 `RED <文件>::<用例>` 行（人类可读，同时留在 step 日志里）；
2. `::error ...` 注释行（GitHub 收集成 check-run annotations）。

本脚本**永远返回 0**：它只负责报告，测试本身的退出码由 workflow 负责。
"""
from __future__ import annotations

import re
import sys

#: GitHub 单个 step 的注释上限是 50 条；多了只截断并注明。
MAX_ANNOTATIONS = 50

_UNITTEST = re.compile(r"^(FAIL|ERROR):\s+(.+?)\s+\(([\w.]+)\)\s*$")
_PYTEST = re.compile(r"^FAILED\s+([\w./\\-]+\.py)::(\S+?)(?:\s+-\s+.*)?$")
_RAN = re.compile(r"^Ran (\d+) tests? in ")
_SUMMARY = re.compile(r"^FAILED \((.*)\)\s*$")


def _split_module(dotted: str) -> tuple[str, list[str]]:
    """`test_mod.Cls.test_fn` / `tests.test_mod.Cls.test_fn` → (`tests/test_mod.py`, [Cls, test_fn])。"""
    parts = [p for p in str(dotted or "").split(".") if p]
    if not parts:
        return "", []
    if parts[0] == "tests" and len(parts) > 1:
        module = "tests/" + parts[1] + ".py"
        rest = parts[2:]
    else:
        module = "tests/" + parts[0] + ".py"
        rest = parts[1:]
    return module, rest


def parse_unittest_line(line: str) -> dict | None:
    match = _UNITTEST.match(line.strip())
    if not match:
        return None
    kind, name, dotted = match.groups()
    module, rest = _split_module(dotted)
    if not module:
        return None
    # setUpClass / tearDownClass 这种钩子名不在 dotted 里，要补回去，否则两条同类
    # 报错会塌成同一个标题。
    title = ".".join(rest + ([name] if name and name not in rest else []))
    return {"kind": kind, "path": module, "title": title or name, "source": "unittest"}


def parse_pytest_line(line: str) -> dict | None:
    match = _PYTEST.match(line.strip())
    if not match:
        return None
    path, node = match.groups()
    return {"kind": "FAILED", "path": path.replace("\\", "/"),
            "title": node.replace("::", "."), "source": "pytest"}


def parse(text: str) -> dict:
    """返回 {reds, annotations_truncated, ran, failures, errors, summary_line}。"""
    reds: list[dict] = []
    seen: set[tuple[str, str]] = set()
    ran = 0
    failures = errors = 0
    summary_line = ""
    for line in str(text or "").splitlines():
        ran_match = _RAN.match(line.strip())
        if ran_match:
            ran = max(ran, int(ran_match.group(1)))
            continue
        summary_match = _SUMMARY.match(line.strip())
        if summary_match:
            summary_line = line.strip()
            for part in summary_match.group(1).split(","):
                key, _, value = part.partition("=")
                try:
                    number = int(value.strip())
                except ValueError:
                    continue
                if key.strip() == "failures":
                    failures = number
                elif key.strip() == "errors":
                    errors = number
            continue
        row = parse_unittest_line(line) or parse_pytest_line(line)
        if row is None:
            continue
        key = (row["path"], row["title"])
        if key in seen:
            continue
        seen.add(key)
        reds.append(row)
    return {"reds": reds, "ran": ran, "failures": failures, "errors": errors,
            "summary_line": summary_line,
            "annotations_truncated": max(0, len(reds) - MAX_ANNOTATIONS)}


def _escape(value: str) -> str:
    """GitHub workflow command 的转义：`%` `\\r` `\\n` 会让整行被截断或误读。"""
    return (str(value or "").replace("%", "%25")
            .replace("\r", "%0D").replace("\n", "%0A"))


def render(report: dict) -> list[str]:
    """人类可读清单 + 注释行。顺序固定：先清单，后注释。"""
    reds = report["reds"]
    header = "红名单：%d 条（%s）" % (
        len(reds), report["summary_line"] or "日志里没有汇总行")
    if report["ran"]:
        header += "，共运行 %d 条" % report["ran"]
    lines = [header]
    lines += ["RED %s::%s" % (row["path"], row["title"]) for row in reds]
    if report["annotations_truncated"]:
        lines.append("（注释只取前 %d 条，其余见上面的 RED 行）" % MAX_ANNOTATIONS)
    for row in reds[:MAX_ANNOTATIONS]:
        lines.append("::error file=%s,title=%s %s::%s" % (
            row["path"], _escape(row["kind"]), _escape(row["title"]),
            _escape("%s %s" % (row["kind"], row["title"]))))
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("用法：python tools/ci_red_summary.py <测试日志文件>", file=sys.stderr)
        return 0
    try:
        with open(args[0], encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        print("读不到日志：%s" % exc, file=sys.stderr)
        return 0
    report = parse(text)
    if not report["reds"]:
        print("红名单：0 条（没有解析到失败行）")
        return 0
    for line in render(report):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
