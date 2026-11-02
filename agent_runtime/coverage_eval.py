"""改动行覆盖率门：这次改的行，有没有被任何用例走过。

**为什么不是全仓百分比。** 2026-11-02 实测：在一组真实测试上跑 coverage，
全仓分母是 **124712 条语句 / 覆盖 19237 条 = 15.4%**。这个数无论涨跌都回答不了
「我这次改动被走过吗」——它只回答「测试跑了多少代码」。同一份报告里
「改动行覆盖率」是 **94.6%（37 行改动，2 行未执行）**，那 2 行正是当时改的
`dev_server.py:140` 与 `head_check.py:194`：一条改动的分支没有任何用例走到。
所以门只盯 diff 行，不盯百分比横幅。

口径（三条，都写成常量并在 self-check 里钉住）：

1. **合计门槛 90%**：本次改动行里被执行的总比例低于它就红。
   为什么不是 100%：改一行 `except ... as e:` 的写法、改一行日志文案，
   要求它必须被执行会把人逼去写「只为覆盖而存在」的用例；这类用例会污染
   变异电池（它靠「改坏实现有测试变红」判有没有牙）。
2. **整块未执行门槛**：某个文件本次改动 ≥5 行而**一行都没被执行** → 红。
   这是真正危险的那种：改了一整块逻辑，没有任何测试碰过它。
   合计规则会把它藏起来（今天这 37 行里 35 行覆盖了，100% 未执行的文件仍可以是 0%）。
3. **改动的生产文件没出现在覆盖率报告里 → 红**（「未知」不许读成「已覆盖」）。
   与 `code_coverage` 的三条 fail-closed 同源。

只盯**生产代码**：`tests/` 与前端不算；人工现场跑的脚本（`verify_*.py`、
`tools/mutation/probe_*.py` 等）单独归为 **warning** —— CI 里没有用例执行它们，
要求覆盖只会把门变成恒红，但改到它们必须看得见。这份清单由
`tests/test_coverage_eval.py` 钉住，加一项必须写理由。

跑出来的成本（决定门放在 CI 哪一步）：把 pytest lane 的命令换成
`coverage run -m pytest …` 不重复跑测试，实测同组 plain 29s → 带 coverage 34s（1.17 倍）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

#: 合计改动行覆盖率的下限（低于此判红）。
AGGREGATE_FLOOR = 90.0
#: 单文件改动达到这么多行、却一行都没被执行 → 判红。
BLANKET_MIN_LINES = 5
#: 一次最多判多少个文件（防止把整仓重构塞进一次判定）。
MAX_FILES = 60

#: 这些前缀/模式是生产代码。tests 与前端不参与本门。
PRODUCTION_PREFIXES = ("agent_runtime/", "api_routes/", "tools/")
ROOT_LEVEL_PRODUCTION = re.compile(r"^[a-z0-9_]+\.py$")
#: 人工现场跑的脚本：改它们只出 warning，不判红（CI 里没有用例执行它们）。
#: `run_golden.py` 在这里是**结构性**原因，不是嫌麻烦：它在导入期就写进程环境
#: （`DOCMIND_TRACE=0`、`DOCMIND_GOLDEN_NO_LOCK=1`，必须早于 `import agent`），
#: 一旦被进程内导入，`unittest discover` 那条 lane 的红会从 30 放大到 146
#: （实测，见 `tests/test_eval_memory_isolation.py:98` 的 docstring）。
#: 所以它只能子进程跑，进程内永远覆盖不到它的行。
#: 这份清单由 `tests/test_coverage_eval.py` 钉住：加一项必须同时给出理由。
#: 2026-11-02 补两个：`tools/mutation/mutate_*`（变异电池驱动，按定义手工跑、测试永不 import）
#: 与 `tools/design_seed.py`（一次性种子脚本）。依据同 run_golden：判定是**结构性**的 ——
#: 它们的入口是命令行，测试里没有也不该有调用点；把它们算成 production 会让门拿
#: 「测试本来就不该执行的行」判红，那是量具错，不是代码错。
MANUAL_SCRIPTS = ("verify_", "tools/mutation/probe_", "tools/mutation/diagnose_",
                  "tools/mutation/mutate_", "tools/design_seed.py",
                  "make_lnk_pure.py", "demo_", "tools/backfill_", "run_golden.py")


def classify(path: str) -> str:
    """把 diff 里的一个文件分成 production / manual / not_tracked。"""
    rel = str(path or "").replace("\\", "/").lstrip("./")
    if not rel.endswith(".py"):
        return "not_tracked"
    if rel.startswith("tests/") or rel.startswith("frontend/") or rel.startswith("web/"):
        return "not_tracked"
    if any(rel.startswith(p) or p in rel for p in MANUAL_SCRIPTS):
        return "manual"
    if rel.startswith(PRODUCTION_PREFIXES) or ROOT_LEVEL_PRODUCTION.match(rel):
        return "production"
    return "not_tracked"


def parse_diff_added_lines(diff_text: str) -> dict[str, list[int]]:
    """从 `git diff --unified=0` 里取「新文件侧」的改动行号（只算新增/修改行）。

    纯删除不产生新行号，所以不会把「删掉一行」算成改动行 —— 这是有意的：
    删除本身由测试是否仍绿来判，覆盖率回答不了它。
    """
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in str(diff_text or "").splitlines():
        head = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
        if head:
            current = head.group(2)
            continue
        if current is None:
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if not hunk:
            continue
        start = int(hunk.group(1))
        count = int(hunk.group(2)) if hunk.group(2) is not None else 1
        if count <= 0:                       # 纯删除
            continue
        out.setdefault(current, set()).update(range(start, start + count))
    return {name: sorted(lines) for name, lines in out.items() if lines}


def normalize_key(name: str, base: str) -> str:
    """覆盖率报告里的键 → 仓库相对路径。

    单独抽出来是因为 `present`（这文件在不在报告里）与 `missing` 必须用**同一套**
    归一化，否则同一文件会被算成「不在报告里」→ 误判红。
    Windows 跨盘符没有相对路径，那种键原样留着：它只会「不算已覆盖」，不会冒充覆盖。
    """
    rel = str(name or "").replace("\\", "/")
    root = str(base or "").replace("\\", "/").rstrip("/")
    if not root:
        return rel.lstrip("./")
    abspath = os.path.abspath(root)
    if rel.startswith(abspath.replace("\\", "/") + "/"):
        return rel[len(abspath) + 1:]
    if os.path.isabs(rel):
        try:
            return os.path.relpath(os.path.normpath(rel), abspath).replace("\\", "/")
        except ValueError:
            return rel
    return rel.lstrip("./")


def normalize_keys(payload: dict[str, Any], root: str = ".") -> set[str]:
    """报告里出现过的文件集合（归一化后），用来判「这个改动文件根本没被测」。"""
    base = os.path.abspath(root or ".")
    return {normalize_key(name, base) for name in (payload.get("files") or {})}


def missing_lines_from_report(payload: dict[str, Any], root: str = ".") -> dict[str, set[int]]:
    """覆盖率报告 → {相对路径: 未执行行号集合}（减去 coverage 自己标记的 excluded）。"""
    base = os.path.abspath(root or ".")
    out: dict[str, set[int]] = {}
    for name, body in (payload.get("files") or {}).items():
        rel = normalize_key(name, base)
        missing = {int(x) for x in ((body or {}).get("missing_lines") or [])}
        excluded = {int(x) for x in ((body or {}).get("excluded_lines") or [])}
        out[rel] = missing - excluded
    return out


def evaluate(changed: dict[str, list[int]], missing: dict[str, set[int]], *,
             floor: float = AGGREGATE_FLOOR, blanket_min: int = BLANKET_MIN_LINES,
             present: set[str] | None = None) -> dict[str, Any]:
    """把「改动行」与「未执行行」求交，给出判定。纯函数，不读盘不读 git。

    `present`：覆盖率报告里出现过的文件集合（None = 不检查未知文件）。
    """
    rows: list[dict[str, Any]] = []
    total = covered = 0
    reds: list[str] = []
    warnings: list[str] = []
    for rel, lines in sorted(changed.items()):
        kind = classify(rel)
        if kind == "not_tracked":
            continue
        if present is not None and rel not in present:
            if kind == "production":
                reds.append("%s 没出现在覆盖率报告里 —— 覆盖率【未知】，不能读成已覆盖" % rel)
            else:
                warnings.append("%s 是人工脚本且不在覆盖率报告里（未知）" % rel)
            continue
        miss = missing.get(rel, set())
        hit = [ln for ln in lines if ln in miss]
        covered_here = len(lines) - len(hit)
        if kind == "production":
            total += len(lines)
            covered += covered_here
        pct = 100.0 * covered_here / len(lines)
        rows.append({"file": rel, "kind": kind, "changed": len(lines),
                     "covered": covered_here, "missed_lines": hit[:20],
                     "missed": len(hit), "percent": round(pct, 1)})
        if kind == "manual":
            if hit:
                warnings.append("%s 是人工跑的脚本，本次改动 %d 行里 %d 行没被 CI 用例执行（不计入门）"
                                % (rel, len(lines), len(hit)))
            continue
        if hit:
            warnings.append("%s 改动 %d 行，其中 %d 行没有任何用例走过：行号 %s"
                            % (rel, len(lines), len(hit), hit[:8]))
        if len(lines) >= blanket_min and covered_here == 0:
            reds.append("%s 改了 %d 行，一行都没被执行过（改了一整块逻辑却没有测试碰它）"
                        % (rel, len(lines)))
    percent = round(100.0 * covered / total, 1) if total else 100.0
    if total and percent < floor:
        reds.append("改动行覆盖率 %.1f%% 低于门槛 %.1f%%" % (percent, floor))
    return {"ok": not reds, "changed_total": total, "covered_total": covered,
            "percent": percent, "floor": floor, "files": rows[:MAX_FILES],
            "files_truncated": len(rows) > MAX_FILES,
            "failures": reds, "warnings": warnings,
            "note": ("本次没有生产代码的改动行（纯删除/文档/测试），合计按 100%% 记"
                     if not total else "")}


def changed_python_files(base: str, head: str = "HEAD", root: str = ".") -> dict[str, list[int]]:
    """对真实 git 仓库取改动行（薄壳，便于 self-check 之外复用）。"""
    proc = subprocess.run(["git", "-C", str(root), "diff", "--unified=0",
                           "%s..%s" % (base, head), "--", "*.py"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("git diff 失败（base=%s head=%s）：%s"
                           % (base, head, (proc.stderr or "").strip()[:200]))
    return parse_diff_added_lines(proc.stdout)


def _self_check_cases() -> list[tuple[str, bool]]:
    """self-check：门本身必须能被证伪，否则接进 CI 只是个装饰。"""
    out: list[tuple[str, bool]] = []

    def check(name: str, changed: dict[str, list[int]], missing: dict[str, set[int]],
             want_ok: bool, **kw) -> None:
        got = evaluate(changed, missing, **kw)
        out.append((name, got["ok"] == want_ok))

    full = {"agent_runtime/x.py": [1, 2, 3, 4, 5]}
    check("全覆盖判绿", full, {}, True, present=set(full))
    check("改一整块没人碰判红", full, {"agent_runtime/x.py": {1, 2, 3, 4, 5}}, False, present=set(full))
    check("只改一行且没人执行判红", {"agent_runtime/y.py": [7]}, {"agent_runtime/y.py": {7}},
          False, present={"agent_runtime/y.py"})
    check("改动多行只漏两行判绿但出警告",
          {"agent_runtime/y.py": [i for i in range(1, 36)], "agent_runtime/z.py": [40]},
          {"agent_runtime/y.py": {12, 30}}, True,
          present={"agent_runtime/y.py", "agent_runtime/z.py"})
    check("未知文件判红", {"agent_runtime/z.py": [1, 2]}, {}, False, present=set())
    check("人工脚本不判红", {"verify_realtime_acceptance.py": [1, 2, 3]},
          {"verify_realtime_acceptance.py": {1, 2, 3}}, True,
          present={"verify_realtime_acceptance.py"})
    check("合计低于门槛判红", {"agent_runtime/a.py": [1, 2], "agent_runtime/b.py": [3]},
          {"agent_runtime/a.py": {1, 2}}, False, present={"agent_runtime/a.py", "agent_runtime/b.py"})
    check("tests 目录不参与", {"tests/test_a.py": [1, 2, 3]}, {}, True, present=set())
    check("纯删除不算改动行", parse_diff_added_lines(
        "diff --git a/agent_runtime/x.py b/agent_runtime/x.py\n"
        "--- a/agent_runtime/x.py\n+++ b/agent_runtime/x.py\n@@ -3,2 +2,0 @@\n-g1\n-g2\n"),
        {}, True)
    return out


def self_check() -> int:
    cases = _self_check_cases()
    bad = [name for name, ok in cases if not ok]
    print(json.dumps({"self_check": {"cases": len(cases), "passed": len(cases) - len(bad),
                                    "failed": bad}}, ensure_ascii=False, indent=2))
    return 1 if bad else 0


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="改动行覆盖率门")
    ap.add_argument("--report", help="coverage json 的输出文件")
    ap.add_argument("--base", default="", help="diff 基线 SHA（push 事件用 github.event.before）")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--root", default=".")
    ap.add_argument("--floor", type=float, default=AGGREGATE_FLOOR)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.self_check:
        return self_check()
    if not args.report or not args.base:
        print(json.dumps({"ok": False, "failures":
                          ["缺 --report 或 --base —— 覆盖率未知/基线取不到时不判绿（fail-closed）"]},
                         ensure_ascii=False))
        return 1
    try:
        payload = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "failures": ["覆盖率报告读不出来：%s" % exc]},
                         ensure_ascii=False))
        return 1
    try:
        changed = changed_python_files(args.base, args.head, args.root)
    except (RuntimeError, OSError) as exc:
        print(json.dumps({"ok": False, "failures": ["拿不到 git diff：%s（浅克隆要 fetch-depth: 0）" % exc]},
                         ensure_ascii=False))
        return 1
    present = {key for key in normalize_keys(payload, args.root) if classify(key) != "not_tracked"}
    result = evaluate(changed, missing_lines_from_report(payload, args.root),
                      floor=args.floor, present=present)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("改动行覆盖率 %.1f%%（门槛 %.1f%%）｜生产改动 %d 行、被执行 %d 行"
              % (result["percent"], args.floor, result["changed_total"], result["covered_total"]))
        for row in result["files"]:
            # 每条「改而未执行」都指到具体源码行上：GitHub 会把它渲染成那一行的行内注释，
            # 作者在自己的 diff 上就能看见是哪一行没人走过，而不是只看到一串行号。
            for ln in row["missed_lines"]:
                tag = "::warning" if row["kind"] == "production" else "::notice"
                print("%s file=%s,line=%d::这一行改完没有任何用例执行过" % (tag, row["file"], ln))
        for item in result["warnings"]:
            print("::warning:: %s" % item)
        for item in result["failures"]:
            print("::error:: %s" % item)
        if result["ok"]:
            print("改动行覆盖率门：通过")
    return 0 if result["ok"] else 1


if __name__ == "__main__":  # pragma: no cover —— 作为脚本执行的那一行，进程内测不到
    sys.exit(main())
