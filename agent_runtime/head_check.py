"""HEAD 自洽检查：把【提交本身】挂成干净 worktree，在那棵树上真 import、真跑离线套件。

存在的理由是一起真实事故（2026-10-01，本仓 `b828d10`）：pathspec 形式的 `git commit -- <路径>`
绕过索引，把别人未提交的 WIP 一起提交掉，于是 `agent.py` 里引用
`agent_runtime/run_budget.py` 的那行 import 进了 HEAD，而**那个文件本身还没入库**。
工作树里一切正常（模块还在磁盘上），单独检出 HEAD 却 `ModuleNotFoundError`——
CI、别人的 `git worktree add`、任何 clone 都会红，而在这个共享工作树里跑测试永远看不出来。

所以这条回路必须回到【只有已提交内容】的那棵树上跑。三条设计约束都是被这次事故决定的：

* **先把 ref 解析成 sha 再用**：HEAD 在共享工作树里会被别人推着走，`worktree add` 与
  报告必须指向同一个提交，否则"我验过的"和"我说的"不是同一个东西。
* **只允许 `--detach`**：绝不占用分支，绝不碰主工作树的 checkout 或索引。
* **未跟踪文件要能被归因**：缺模块时同时回答"它在磁盘上吗、它在提交里吗"——
  只在磁盘上 = 别人的新文件被我的提交引用了，这正是上面那类错的指纹。

2026-10-02 又付了一次同型的账，这次是我自己犯的：hunk 暂存时收进了**别人那段的尾巴**
（`dev_eval` 里 6 条 `_check(...)`），而它们读的局部量还在别人未提交的上半段里。import 全绿，
`dev_headcheck` 当时也全绿——因为它只验 import。是"在干净检出里把门禁题集跑一遍"才红的，
而那次是我手工做的。这一档现在进了工具：`gate: 1` 在干净检出里真跑离线套件，
**红了自动再拿工作树跑同一条当对照组**——「检出红 / 工作树绿」这个差值就是
"提交缺内容"的指纹，比任何措辞都硬；两边都红则是另一回事（负载、缺工具链、真坏了）。

边界：这条验的是【只有已提交内容时能不能跑成】，不验行为对不对（那是 CI 全套件的活）。
它也不覆盖需要**未跟踪工具链**的套件：`frontend/node_modules`、`web/` 都不在提交里，
新检出上那些用例必然红（实测：`test_voice_loop::BundleTests` 4 条报"前端没有 esbuild"）——
那种红是环境，不是自洽问题，报出来时要能说清这两件事。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from agent_runtime import process_runner

#: 入口模块：从这些进就能把仓里主要的 import 图走一遍（config→tools→agent 是真实依赖顺序）。
ENTRY_MODULES = ("config", "tools", "agent")
#: 本模块只允许的 git 子命令：全部是读或 worktree 生命周期，没有 history/暂存/推送。
GIT_ALLOWED = frozenset({"rev-parse", "worktree", "ls-files", "ls-tree", "status"})
MAX_REF_LENGTH = 80
#: ref 允许的形状：不能以 `-` 开头（否则被 git 当选项），不含空格/glob/`..`/分号。
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.^~/-]*$")
_MISSING_RE = re.compile(r"No module named '([^']+)'")
_FRAME_RE = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[^\n]+)\n'
                       r'(?P<body>(?: {4}\S[^\n]*\n?)*)')
_IMPORT_STMT_RE = re.compile(r"(?:from|import)\s+([A-Za-z_][\w.]*)")
_MARKER = ".docmind-headcheck-owner"

# --------------------------------------------------------------------------- #
# 第二档：在干净检出里真跑离线套件（import 绿不等于行为绿，2026-10-02 为这句话付过账）
# --------------------------------------------------------------------------- #

#: 门禁整集的命令。基线走**仓内相对路径**：在干净检出里跑，读的就是那份提交里的基线。
GATE_BASELINE = ".github/dev-eval-baseline.json"
GATE_ARGV = ("agent_runtime.dev_eval", "--self-check", "--baseline", GATE_BASELINE, "--json")
#: 门禁整集默认给的秒数。上限就是 `process_runner.MAX_TIMEOUT`——`run_bounded` 会把
#: 超过它的值**静默削到 300**，所以这里不能假装能配到 3600 秒。
SUITE_TIMEOUT_DEFAULT = 300
SUITE_TIMEOUT_MAX = 300
SUITE_TIMEOUT_MIN = 30
MAX_TEST_TARGETS = 12
MAX_PATH_CHARS = 200
#: 结论窗口：`dev_eval --json` 一行就是 ~26 KB，默认的 4000 字符窗会把 JSON 拦腰截断，
#: 于是"读不到结论"看起来像"门禁没跑成"。这两件事必须分得开。
SUITE_HEAD_CHARS = 120_000
SUITE_TAIL_CHARS = 8_000
#: pytest 的 -q 收尾行：`39 passed, 48 subtests passed in 0.5s` / `no tests ran in 0.12s`。
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) (?:failed|errors?)")
_SKIPPED_RE = re.compile(r"(\d+) (?:skipped|deselected)")
_NO_TESTS_RE = re.compile(r"no tests ran|^\s*collected 0 items", re.MULTILINE)
#: `-rf` 打出来的失败清单：`FAILED tests/x.py::test_y - AssertionError`
_FAILED_ID_RE = re.compile(r"^FAILED (\S+?)(?: - .*)?$", re.MULTILINE)


def safe_relative(path: Any) -> str:
    """只接受仓内相对路径：绝对路径、`..`、以 `-` 开头（会被当 pytest 选项）一律拒。"""
    text = str(path or "").strip().replace("\\", "/")
    if not text or len(text) > MAX_PATH_CHARS:
        return ""
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("-"):
        return ""
    parts = [row for row in text.split("/") if row not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def gate_argv(baseline: str = "") -> list[str]:
    """跑 dev_eval 整集的命令（`python -B -m ... `）。基线路径可以换，但必须是仓内相对路径。"""
    rows = list(GATE_ARGV)
    if baseline:
        cleaned = safe_relative(baseline)
        if not cleaned:
            raise HeadCheckError("baseline 必须是仓内相对路径：%s" % str(baseline)[:80])
        rows[rows.index("--baseline") + 1] = cleaned
    return [sys.executable, "-B", "-m", *rows]


def pytest_argv(targets: tuple[str, ...] | list[str], basetemp: Path | str) -> list[str]:
    """在检出里跑指定 pytest 目标的命令。缓存与临时目录都不能落在检出里。"""
    cleaned: list[str] = []
    for row in targets:
        relative = safe_relative(row)
        if not relative:
            raise HeadCheckError("测试目标必须是仓内相对路径：%s" % str(row)[:80])
        if relative not in cleaned:
            cleaned.append(relative)
    if not cleaned:
        raise HeadCheckError("没给测试目标")
    if len(cleaned) > MAX_TEST_TARGETS:
        raise HeadCheckError("一次最多跑 %d 个测试目标，收到 %d 个" % (MAX_TEST_TARGETS, len(cleaned)))
    return [sys.executable, "-B", "-m", "pytest", *cleaned, "-q", "-rf",
            "-p", "no:cacheprovider", "--basetemp=%s" % str(basetemp).replace("\\", "/")]


def parse_gate(output: str) -> dict[str, Any]:
    """从 dev_eval 的 --json 里取结论；取不到就是【没结论】，绝不默认成功。"""
    for line in reversed(str(output or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("report"), dict):
            report, gate = row["report"], row.get("gate")
            return {"parsed": True, "passed": int(report.get("passed") or 0),
                    "scored": int(report.get("scored") or 0),
                    "failed": [str(x) for x in (report.get("failed") or [])][:8],
                    "regressions": [str(x) for x in ((gate or {}).get("regressions") or [])][:8],
                    "regressed": bool((gate or {}).get("regressed")) if gate else False}
    return {"parsed": False, "passed": 0, "scored": 0, "failed": [], "regressions": [],
            "regressed": False}


def parse_pytest(output: str, exit_code: Any) -> dict[str, Any]:
    """数的是【真跑掉的条数】：`no tests ran` 与 `collected 0 items` 不是通过。"""
    text = str(output or "")
    passed = int((_PASSED_RE.search(text).group(1)) if _PASSED_RE.search(text) else 0)
    failed = int((_FAILED_RE.search(text).group(1)) if _FAILED_RE.search(text) else 0)
    skipped = int((_SKIPPED_RE.search(text).group(1)) if _SKIPPED_RE.search(text) else 0)
    empty = bool(_NO_TESTS_RE.search(text)) or (passed + failed == 0)
    names = _FAILED_ID_RE.findall(text)[:12]
    return {"parsed": True, "passed": passed, "failed": failed, "skipped": skipped,
            "ran": passed + failed, "empty": empty, "failed_names": names,
            "ok": exit_code == 0 and not empty}


ATTR_ONLY_IN_COMMIT = ("只有检出里红的题：%s——差别在【没提交的那部分】或【未跟踪的工具链】"
                       "（前者=漏提交/误收，后者=node_modules、web/ 这类本来就不在提交里的东西）")
ATTR_BOTH_RED = "两棵树里都红，且没有【只在检出里红】的题：这不是自洽问题（负载、缺工具链或真的坏了）"
ATTR_NO_CONTROL = "对照组没跑成，不能据此归因"


def attribute(row: dict[str, Any], control: dict[str, Any] | None) -> str:
    """把一次红归到哪一类——靠**两边失败题名单的差集**，不是靠整集的布尔值。

    为什么不比布尔值：拿本仓 `f7ec14e`（就是我该抓的那次）真跑过一次，两棵树都因为
    `diagnostics` 那两题在负载下抖而红，"整集比整集"就得出"两棵树都红"，而真正要指出的
    那一题（只在检出里红的 `page-action-...`）被埋在后面。集合差把公共噪声减掉了。
    """
    if row.get("ok"):
        return ""
    if control is None:
        return ""
    if not control.get("ran"):
        return ATTR_NO_CONTROL
    here = set(row.get("failed_names") or [])
    there = set(control.get("failed_names") or [])
    only = row.get("only_in_checkout") or sorted(here - there)
    if only:
        return ATTR_ONLY_IN_COMMIT % ", ".join(only[:6])
    return ATTR_BOTH_RED


def run_suite(target: Path, argv: list[str], *, kind: str, state_root: Path | str,
              timeout: float = SUITE_TIMEOUT_DEFAULT,
              runner: Callable[..., Any] = None) -> dict[str, Any]:
    """在那棵检出的目录里跑一条命令，回一份**永不默认成功**的结论。

    cwd 决定 `python -m` 解析到哪份代码：cwd=检出 → 跑的就是【已提交内容】。
    状态根单独给一个检出外的临时目录：套件会写 sqlite/缓存，写进检出就把回收搞脏了。
    """
    runner = runner or process_runner.run_bounded
    cleaned = safe_relative(str(argv[0])) or str(argv[0])
    row: dict[str, Any] = {"kind": kind, "argv": [Path(str(argv[0])).name, *argv[1:]][:8],
                           "ok": False, "ran": False, "exit_code": None, "detail": "",
                           "passed": 0, "failed_count": 0, "skipped": 0}
    try:
        rep = runner(list(argv), cwd=str(target), timeout=int(timeout),
                     env={**os.environ, "DOCMIND_STATE_ROOT": str(state_root)},
                     head_chars=SUITE_HEAD_CHARS, tail_chars=SUITE_TAIL_CHARS,
                     command_text="%s in clean checkout" % kind)
    except Exception as exc:  # noqa: BLE001 - 起不来必须是红，不能是"没跑所以绿"
        row["detail"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        return row
    output = str((rep or {}).get("output") or "")
    row.update({"exit_code": (rep or {}).get("exit_code"),
                "detail": str((rep or {}).get("error") or "")[-300:] or output[-400:],
                "timed_out": bool((rep or {}).get("timed_out"))})
    if row["timed_out"]:
        row["detail"] = "超时（%s 秒）；%s" % (int(timeout), row["detail"][-200:])
        return row
    if (rep or {}).get("error"):
        return row
    parsed = parse_gate(output) if kind.split("-", 1)[0] == "gate" else \
        parse_pytest(output, row["exit_code"])
    row.update({"ran": True, "parsed": bool(parsed.get("parsed")),
                "passed": int(parsed.get("passed") or 0),
                "skipped": int(parsed.get("skipped") or 0)})
    row["failed_names"] = sorted(set(parsed.get("failed_names") or []))
    if kind.split("-", 1)[0] == "gate":
        row["failed_names"] = sorted(set(parsed.get("failed") or [])
                                     | set(parsed.get("regressions") or []))
        row["failed_count"] = len(row["failed_names"])
        row["ok"] = bool(parsed.get("parsed")) and parsed["scored"] > 0 \
            and not parsed["failed"] and not parsed["regressed"]
        row["gate"] = parsed
        if not parsed.get("parsed"):
            row["detail"] = "读不出结论（输出被截断、或不是那份 JSON）：%s" % row["detail"][-200:]
        elif not row["ok"]:
            row["detail"] = "门禁 %d/%d 通过；失败或回退的题：%s" % (
                parsed["passed"], parsed["scored"], ", ".join(row["failed_names"][:8]) or "无")
    else:
        row["failed_count"] = int(parsed.get("failed") or 0)
        row["ok"] = bool(parsed.get("ok"))
        if parsed.get("empty"):
            row["detail"] = "一条都没跑到（收集为 0 或 no tests ran）——这不是通过：%s" % \
                            row["detail"][-200:]
    return row


def run_suites(target: Path, repo: str, *, want_gate: bool, tests: tuple[str, ...],
               state_root: Path, timeout: float, runner: Callable[..., Any] = None
               ) -> list[dict[str, Any]]:
    """跑请求的套件；**红了才在工作树里跑同一条**当对照组（绿的时候不翻倍花时间）。"""
    rows: list[dict[str, Any]] = []
    jobs: list[tuple[str, list[str]]] = []
    if want_gate:
        jobs.append(("gate", gate_argv()))
    if tests:
        try:
            jobs.append(("pytest", pytest_argv(tests, Path(state_root) / "pt")))
        except HeadCheckError as exc:
            rows.append({"kind": "pytest", "ok": False, "ran": False, "parsed": False,
                         "argv": list(tests)[:8], "detail": str(exc), "passed": 0,
                         "failed_count": 0, "skipped": 0, "targets_missing": []})
    for kind, argv in jobs:
        if kind == "pytest":
            # 只在"磁盘上有、提交里没有"时拦下来：那种红跑它没有意义，直接说清是谁漏了。
            missing = [row for row in tests if not (target / safe_relative(row)).is_file()
                       and (Path(repo) / safe_relative(row)).is_file()]
        else:
            missing = []
        if missing:
            rows.append({"kind": kind, "ok": False, "ran": False, "parsed": False,
                         "argv": argv[:8], "passed": 0, "failed_count": 0, "skipped": 0,
                         "targets_missing": missing, "likely": "",
                         "detail": "要跑的测试在提交里不存在：%s" % ", ".join(missing[:4])})
            continue
        row = run_suite(target, argv, kind=kind, state_root=state_root, timeout=timeout,
                        runner=runner)
        row["targets_missing"] = []
        row["attempts"] = 1
        control = None
        if not row["ok"] and row.get("ran"):
            # 红了先在**同一棵树**里重跑一次：门禁题里有计时敏感的（ruff 起子进程），
            # 负载造成的红不该被说成"提交坏了"。重跑只在红时发生，绿的时候不多花一倍时间。
            retry = run_suite(target, argv, kind="%s-retry" % kind, state_root=state_root,
                              timeout=timeout, runner=runner)
            row["attempts"] = 2
            if retry.get("ok"):
                row["flipped"] = True
                row["first_detail"] = str(row.get("detail") or "")[-300:]
                row["ok"], row["detail"] = True, ""
                row["passed"] = retry.get("passed") or row.get("passed")
                row["gate"] = retry.get("gate") or row.get("gate")
                rows.append(row)
                continue
            row["ok"] = bool(retry.get("ok"))
            row["detail"] = str(retry.get("detail") or "")[-400:]
            row["gate"] = retry.get("gate") or row.get("gate")
            row["failed_names"] = retry.get("failed_names") or row.get("failed_names")
            row["failed_count"] = len(row["failed_names"] or [])
            control = run_suite(Path(repo), argv, kind="%s-control" % kind,
                                state_root=state_root, timeout=timeout, runner=runner)
            control["argv"] = argv[:8]
            row["control"] = {k: control.get(k) for k in ("ok", "ran", "exit_code",
                                                         "passed", "detail")}
            here = set(row.get("failed_names") or [])
            there = set(control.get("failed_names") or [])
            row["only_in_checkout"] = sorted(here - there)
            row["only_in_worktree"] = sorted(there - here)
        row["likely"] = attribute(row, control)
        rows.append(row)
    return rows


class HeadCheckError(RuntimeError):
    """HEAD 自洽检查无法开始。"""


def resolve_ref(root: str, ref: str = "HEAD") -> dict[str, Any]:
    """把 ref 钉成 sha，顺手证明这里确实是个 git 工作树。空 ref 就按默认 HEAD 处理。"""
    text = str(ref if ref is not None else "").strip() or "HEAD"
    if len(text) > MAX_REF_LENGTH or not _REF_RE.match(text) or ".." in text:
        return {"ok": False, "error": "ref 形状不被接受：%s" % text[:80]}
    rep = _git(root, ["rev-parse", "--verify", f"{text}^{{commit}}"])
    if rep.get("error") or rep.get("exit_code") != 0:
        return {"ok": False, "error": "找不到提交：%s" % text,
                "detail": (rep.get("output") or "")[-200:]}
    lines = (rep.get("output") or "").strip().splitlines()
    return {"ok": True, "ref": text, "sha": lines[0] if lines else ""}


def _git(root: str, args: list[str], *, timeout: int = 60) -> dict[str, Any]:
    if not args or args[0] not in GIT_ALLOWED:
        return {"ok": False, "exit_code": None, "output": "",
                "error": "拒绝执行：%s 不在 HEAD 自检白名单内。" % (args[0] if args else "")}
    argv = ["git", "--no-pager", "-c", "core.quotepath=false", *args]
    return process_runner.run_bounded(argv, cwd=root, timeout=timeout,
                                      command_text=" ".join(argv))


def worktree_base(root: str) -> Path:
    """检出永远落在项目状态目录下（仓外），不与任何人的工作树抢路径。"""
    import project_state
    return Path(project_state.directory(root)) / "headcheck"


def make_target(root: str, sha: str) -> Path:
    base = worktree_base(root)
    base.mkdir(parents=True, exist_ok=True)
    return base / ("headcheck-%s-%s" % (sha[:8], _token()))


def _token() -> str:
    """两次调用同名不是意外，所以每次带上 3 字节随机后缀。"""
    return secrets.token_hex(3)


def add_worktree(root: str, target: Path, sha: str, *, timeout: int = 120) -> dict[str, Any]:
    if target.exists():
        return {"ok": False, "error": "检出目录已存在，不敢覆盖：%s" % target}
    rep = _git(root, ["worktree", "add", "--detach", str(target), sha], timeout=timeout)
    if rep.get("error") or rep.get("exit_code") != 0:
        return {"ok": False, "error": "git worktree add 失败："
                 + ((rep.get("output") or rep.get("error") or "").strip()[-300:] or "无输出")}
    # 归属标记：remove 之前必须证明这个目录是本次建的，不拆别人的检出。
    with contextlib.suppress(OSError):
        (target / _MARKER).write_text(sha + "\n", encoding="utf-8", newline="\n")
    return {"ok": True, "path": os.path.normpath(str(target))}


def remove_worktree(root: str, target: Path, *, timeout: int = 60) -> dict[str, Any]:
    """收回本次创建的检出；不是我建的一律不动。"""
    if not target.exists():
        return {"ok": True, "removed": False, "notes": ["目录已不在"]}
    marker = target / _MARKER
    if not marker.is_file():
        return {"ok": False, "removed": False,
                "error": "检出目录没有本工具的归属标记，拒绝删除：%s" % target}
    rep = _git(root, ["worktree", "remove", "--force", str(target)], timeout=timeout)
    output = (rep.get("output") or rep.get("error") or "").strip()
    if rep.get("exit_code") == 0:
        _git(root, ["worktree", "prune"], timeout=timeout)
    gone = not target.exists()
    if not gone and target.is_dir():
        # Windows 上 `worktree remove` 常留下空壳目录（自己的 cwd 或杀软句柄占住）。
        with contextlib.suppress(OSError):
            for leftover in sorted(target.rglob("*"), key=lambda p: len(str(p)), reverse=True):
                with contextlib.suppress(OSError):
                    if leftover.is_dir():
                        leftover.rmdir()
                    else:
                        leftover.unlink()
            target.rmdir()
        gone = not target.exists()
    return {"ok": gone, "removed": gone,
            "error": "" if gone else "检出没收回干净：%s" % output[-200:]}


def import_module(target: Path, name: str, *, timeout: int = 60,
                  runner: Callable[..., dict[str, Any]] = process_runner.run_bounded) -> dict[str, Any]:
    """在检出的目录里真 import 一个模块（cwd 决定 sys.path[0]，所以 import 的是 HEAD 的代码）。"""
    code = ("import importlib, json, sys\n"
            "try:\n"
            "    importlib.import_module(sys.argv[1])\n"
            "    print(json.dumps({'ok': True, 'error': ''}))\n"
            "except BaseException as exc:\n"
            "    import traceback\n"
            "    print(json.dumps({'ok': False, 'kind': type(exc).__name__,\n"
            "                      'error': '%s: %s' % (type(exc).__name__, exc),\n"
            "                      'trace': traceback.format_exc()}))\n")
    rep = runner([sys.executable, "-B", "-c", code, name], cwd=str(target), timeout=timeout,
                 command_text="import %s" % name)
    return {"module": name, "report": rep}


def _relative_importer(path: str, target: Path, repo: str) -> str:
    """traceback 里给的是【临时检出】的绝对路径，报回仓内相对路径才有用。"""
    cleaned = str(path).replace("\\", "/")
    for base in (str(target).replace("\\", "/"), str(repo).replace("\\", "/")):
        if cleaned.startswith(base + "/"):
            return cleaned[len(base) + 1:]
    return cleaned


def parse_outcome(rep: dict[str, Any]) -> dict[str, Any]:
    """子进程回的是 JSON 一行；解析失败就当命令本身出了问题，绝不默认成功。"""
    text = str((rep or {}).get("output") or "").strip()
    try:
        row = json.loads(text)
    except ValueError:
        return {"ok": False, "error": text[-400:] or "子进程没有回话",
                "trace": text, "kind": "RunnerError"}
    if not isinstance(row, dict):
        return {"ok": False, "error": text[-400:], "trace": text, "kind": "RunnerError"}
    # 只认真正的 True：`{"ok": "..."}` 这类truthy 字符串不能算成功。
    return {"ok": row.get("ok") is True, "error": str(row.get("error") or ""),
            "trace": str(row.get("trace") or ""), "kind": str(row.get("kind") or "")}


def find_importer(trace: str, name: str) -> tuple[str, int, str]:
    """从 traceback 里找出【真正写出那行 import】的帧。

    直接正则搜"File ... 后面跟一句 import"会命中 importlib 自己的帧（`<string>` /
    `importlib/__init__.py`），报出来的引用点是假的。逐帧取源码行，再从最深的帧往前
    找与被缺模块匹配的那一条。
    """
    hits: list[tuple[str, int, str]] = []
    for frame in _FRAME_RE.finditer(trace or ""):
        statement = _IMPORT_STMT_RE.search(frame.group("body"))
        if statement:
            hits.append((frame.group("path").replace("\\", "/"),
                         int(frame.group("line")), statement.group(1)))
    for path, line, imported in reversed(hits):
        if imported == name or name.startswith(imported + "."):
            return path, line, imported
    return hits[-1] if hits else ("", 0, "")


def classify(trace: str, root: str, sha: str, name: str) -> dict[str, Any]:
    """把一次 import 失败翻成人话：谁 import 了它、它在不在提交里、它在不在磁盘上。"""
    importer, importer_line, imports = find_importer(trace, name)
    relative = (name.replace(".", "/") + ".py").lstrip("/")
    package = (name.replace(".", "/") + "/__init__.py").lstrip("/")
    in_commit = any(_git(root, ["ls-tree", "-r", "--name-only", sha, "--", candidate]).get("output")
                    for candidate in (relative, package))
    on_disk = (Path(root) / relative).is_file() or (Path(root) / package).is_file()
    return {"missing": name, "importer": importer, "importer_line": importer_line,
            "import_statement": imports,
            "in_commit": bool(in_commit), "on_disk": bool(on_disk),
            "likely": ("别人未提交的新文件被提交引用（工作树能跑、单独检出必红）"
                       if on_disk and not in_commit else
                       "提交里根本没有这个模块（漏提交、改名，或它本该是第三方依赖）"
                       if not in_commit and not on_disk else "模块在提交里，失败另有原因")}


def check(root: str | Path, *, ref: str = "HEAD", modules: tuple[str, ...] | None = None,
          timeout: float = 120.0, runner: Callable[..., Any] = None,
          gate: bool = False, tests: tuple[str, ...] = (),
          suite_timeout: float = SUITE_TIMEOUT_DEFAULT) -> dict[str, Any]:
    """挂干净检出 → 逐个 import 入口模块 → （可选）在那棵树上跑套件 → 归因 → 收回检出。"""
    runner = runner or process_runner.run_bounded
    base = Path(root).resolve()
    if not base.is_dir():
        return {"ok": False, "passed": False, "checks": {}, "error": "当前项目目录不存在。"}
    top = _git(str(base), ["rev-parse", "--show-toplevel"])
    if top.get("error") or top.get("exit_code") != 0:
        return {"ok": False, "passed": False, "checks": {},
                "error": "这里不是 git 工作树，HEAD 自检无从开始。"}
    lines = (top.get("output") or "").strip().splitlines()
    repo = os.path.normpath(lines[0]) if lines else str(base)
    resolved = resolve_ref(repo, ref)
    if not resolved["ok"]:
        return {"ok": False, "passed": False, "checks": {}, "error": resolved["error"]}
    sha = resolved["sha"]
    # 空列表不能悄悄退回默认入口：那样"我验了 0 个模块"会被显示成"我验了 3 个"。
    wanted = ENTRY_MODULES if modules is None else tuple(modules)
    target = make_target(repo, sha)
    added = add_worktree(repo, target, sha, timeout=int(timeout))
    if not added["ok"]:
        return {"ok": False, "passed": False, "checks": {"worktree_added": False},
                "error": added["error"], "ref": resolved["ref"], "sha": sha}
    results: list[dict[str, Any]] = []
    suites: list[dict[str, Any]] = []
    cleanup: dict[str, Any] = {"ok": False, "removed": False, "error": "没走到收回"}
    run_root = worktree_base(repo) / ("run-%s-%s" % (sha[:8], _token()))
    try:
        for name in wanted:
            outcome = parse_outcome(import_module(target, name, timeout=int(timeout),
                                                  runner=runner)["report"])
            row = {"module": name, "ok": bool(outcome["ok"]),
                   "error": "" if outcome["ok"] else (outcome["error"] or outcome["trace"])[-400:]}
            if not row["ok"]:
                missing = _MISSING_RE.search(row["error"])
                if missing:
                    row.update(classify(outcome["trace"] or row["error"], repo, sha, missing.group(1)))
                    if row.get("importer"):
                        row["importer"] = _relative_importer(row["importer"], target, repo)
            results.append(row)
        if gate or tests:
            # 套件写出来的东西（sqlite/缓存/临时目录）一律落在检出**外面**，
            # 否则 `worktree remove` 会碰到自己刚建的目录，回收判据就不可信了。
            run_root.mkdir(parents=True, exist_ok=True)
            suites = run_suites(target, repo, want_gate=gate, tests=tuple(tests),
                                state_root=run_root, timeout=suite_timeout, runner=runner)
    finally:
        cleanup = remove_worktree(repo, target, timeout=int(timeout))
        shutil.rmtree(str(run_root), ignore_errors=True)
    ok_imports = [row for row in results if row.get("ok")]
    checks = {
        "worktree_added": True,
        # 一个都没跑成不算通过：`all([])` 是 True，那是废话判定。
        "every_entry_imported": bool(results) and len(ok_imports) == len(results),
        "no_leftover_checkout": bool(cleanup.get("removed")) or not target.exists(),
    }
    if suites:
        checks["every_suite_ran"] = all(row.get("ran") for row in suites)
        checks["every_suite_green"] = all(row.get("ok") for row in suites)
    passed = checks["every_entry_imported"] and checks.get("every_suite_green", True)
    why = []
    if not checks["every_entry_imported"]:
        why.append("import 失败：%s" % ", ".join(row["module"] for row in results
                                                if not row.get("ok")))
    for row in suites:
        if not row.get("ok"):
            why.append("%s 未通过：%s" % (row["kind"], str(row.get("detail") or "")[-200:]))
    return {"ok": passed, "passed": passed, "checks": checks, "ref": resolved["ref"],
            "sha": sha, "modules": list(wanted), "results": results,
            "worktree": added["path"], "cleanup": cleanup,
            "suites": suites, "suite_state_removed": not run_root.exists(),
            "imported": [row["module"] for row in ok_imports],
            "failures": [row for row in results if not row.get("ok")],
            "error": "" if passed else "；".join(why)}


def render(result: dict[str, Any]) -> str:
    if not result.get("checks"):
        return "HEAD 自检未能开始：%s" % result.get("error", "未知原因")
    head = "HEAD 自检【%s】：%s 个入口模块在干净检出里 import 成功（%d 个尝试）。" % (
        "通过" if result.get("ok") else "未通过", len(result.get("imported") or []),
        len(result.get("modules") or []))
    lines = [head, "提交：%s = %s" % (result.get("ref"), str(result.get("sha"))[:12])]
    for row in result.get("failures") or []:
        lines.append("失败：%s → %s" % (row.get("module"), str(row.get("error") or "")[-160:]))
        if row.get("missing"):
            lines.append("  缺的模块：%s；在提交里=%s，在磁盘上=%s → %s" % (
                row["missing"], row["in_commit"], row["on_disk"], row["likely"]))
            if row.get("importer"):
                lines.append("  引用它的地方：%s:%s（import %s）" % (
                    row["importer"], row.get("importer_line") or "?",
                    row.get("import_statement") or "?"))
    for row in result.get("suites") or []:
        name = "门禁整集" if row.get("kind") == "gate" else "指定测试"
        if row.get("ok"):
            lines.append("%s：干净检出里跑绿（通过 %s 条，跑了 %d 次）。" % (
                name, row.get("passed"), int(row.get("attempts") or 1)))
            if row.get("flipped"):
                lines.append("  要说清楚：第一次是红的，重跑才绿——按计时/负载处理，"
                             "不算这次的红，但也不能当成没发生过。第一次看到的：%s"
                             % str(row.get("first_detail") or "")[-160:])
            continue
        lines.append("%s：未通过。%s" % (name, str(row.get("detail") or "")[-300:]))
        gate = row.get("gate") or {}
        if row.get("attempts", 1) > 1:
            lines.append("  同一棵检出里重跑一次仍然红（所以不是抖一下）。")
        if gate.get("failed") or gate.get("regressions"):
            lines.append("  失败题：%s｜相对基线回退：%s" % (
                ", ".join(gate.get("failed") or []) or "无",
                ", ".join(gate.get("regressions") or []) or "无"))
        if row.get("targets_missing"):
            lines.append("  提交里没有这些测试（磁盘上有 = 漏提交）：%s" %
                         ", ".join(row["targets_missing"][:4]))
        control = row.get("control") or {}
        if control:
            lines.append("  对照组（工作树跑同一条）：%s（跑成 %s，退出码 %s，通过 %s 条）" % (
                "绿" if control.get("ok") else "红", bool(control.get("ran")),
                control.get("exit_code"), control.get("passed")))
            if row.get("only_in_worktree"):
                lines.append("  只在工作树里红（别人在途的东西坏了，不归本工具修）：%s"
                             % ", ".join(row["only_in_worktree"][:6]))
            if row.get("likely"):
                lines.append("  归因：%s" % row["likely"])
    if not (result.get("cleanup") or {}).get("removed"):
        lines.append("注意：临时检出没有收回干净（%s），路径 %s" % (
            (result.get("cleanup") or {}).get("error", ""), result.get("worktree")))
    if result.get("suites") and not result.get("suite_state_removed"):
        lines.append("注意：这次跑套件用的临时状态目录没删掉（多半是句柄还没被系统收走）。")
    if result.get("ok"):
        ran = "上面这些入口能被真的 import 起来"
        if result.get("suites"):
            ran += "，且请求的离线套件在那棵树上真的跑绿"
        lines.append("能证明的只有这些：%s。" % ran)
    lines.append("边界：这是【只有已提交内容时能不能跑成】的检查，不验行为对不对；"
                 "需要未跟踪工具链（frontend/node_modules、web/）的套件在新检出里必然红，"
                 "那种红是环境不是自洽问题——看对照组。")
    return "\n".join(lines)


__all__ = ["ENTRY_MODULES", "GATE_BASELINE", "GIT_ALLOWED", "HeadCheckError", "add_worktree",
           "attribute", "check", "classify", "find_importer", "gate_argv", "import_module",
           "make_target", "parse_gate", "parse_pytest", "pytest_argv", "remove_worktree",
           "render", "resolve_ref", "run_suite", "run_suites", "safe_relative",
           "worktree_base"]
