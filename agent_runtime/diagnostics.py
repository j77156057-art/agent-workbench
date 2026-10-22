"""Structured lint diagnostics: 文件:行:列 + 规则号 + 说明，直接可行动。

`self_verify` 只回「过 / 不过」和一段尾部日志；Agent 拿到那坨文本要么瞎猜要么再跑一次
命令。这里把 ruff（Python）与 `npm run typecheck`（vue-tsc，前端）的输出解析成结构化
条目，并明确区分「工具没装」与「代码没问题」——后者才是可以停手的依据。

只读：本模块从不写文件，也从不传 `--fix` 之类的修改参数给外部工具。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any

from . import code_intel, process_runner

DEFAULT_TIMEOUT = 120
MAX_ISSUES = 200
MAX_TARGETS = 12
FE_DIRS = ("frontend", "web", "ui")
#: ruff 的 JSON 每条 finding 要几百字符，默认 4000+4000 的窗口会把 JSON 拦腰截断，
#: 截断的 JSON 解析不出条目 → 绝不能因此回报「没有问题」。
HEAD_CHARS = 140000
TAIL_CHARS = 40000

_TSC_LINE = re.compile(
    r"^(?P<file>[^\s(]+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<sev>error|warning)\s+"
    r"(?P<rule>[A-Za-z]+\d+)\s*:\s*(?P<message>.*)$")
_RUFF_CONCISE = re.compile(
    r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<rule>[A-Z]+\d+)?\s*(?P<message>.*)$")


#: 本仓库没有 ruff 配置，ruff 的默认规则会把上千条风格提醒刷给 Agent，
#: 真正的错误反而被埋掉。所以默认只查会咬人的：语法错误、未定义/重复定义、
#: 未使用的导入与局部变量、裸 except。要更多用 rules: 覆盖（`ALL` 或 `E,W,F`）。
DEFAULT_SELECT = "E9,F401,F811,F821,F822,F841,E722"
_RULES = re.compile(r"^(ALL|[A-Za-z]+[0-9]{0,4}(,[A-Za-z]+[0-9]{0,4})*)$")


def _select_rules(rules: str) -> tuple[str, str]:
    text = str(rules or "").strip().upper().replace(" ", "")
    if not text:
        return DEFAULT_SELECT, ""
    if not _RULES.match(text):
        return "", f"拒绝 rules: 取值 {rules!r}：只接受 ALL 或逗号分隔的规则号（如 E,W,F）。"
    return text, ""


def _rel(root: str, path: str) -> str:
    """Repo-relative, forward slashes; absolute paths outside root stay as-is.

    「在 root 之外就原样保留」这句话是**契约**，不是描述：早先的实现无条件 relativize，
    于是 root 之外的绝对路径被算成 `../../../../../../w/frontend/src/a.ts` ——
    一个比原路径更难读的相对串，而它的含义（这个文件不在项目里）恰恰丢了。
    现在先判「在不在 root 下」，不在就原样返回（去掉前导斜杠）。
    """
    text = str(path or "").replace("\\", "/")
    if not root or not os.path.isabs(path):
        return text.lstrip("./")
    try:
        base = os.path.normpath(os.path.abspath(root))
        target = os.path.normpath(os.path.abspath(path))
        if os.path.commonpath([base, target]) != base:
            return text.lstrip("./")
        return os.path.relpath(target, base).replace("\\", "/")
    except ValueError:
        # 跨盘符（Windows）/ 跨根（POSIX）：commonpath 抛错 = 不在同一个树下
        return text.lstrip("./")


def _severity(rule: str) -> str:
    """只有「会让程序跑挂」的才算 error：语法错误、重复定义、未定义名字。"""
    text = str(rule or "").strip().upper()
    if not text:
        return "info"
    if text.startswith(("E9", "F81", "F82")):
        return "error"
    if text[0] in "EWCFB":
        return "warning"
    return "info"


def _issue(root: str, *, engine: str, file: str, line: Any, column: Any,
           rule: str, message: str, severity: str = "") -> dict[str, Any]:
    try:
        line_no = int(line)
    except (TypeError, ValueError):
        line_no = 0
    try:
        col_no = int(column)
    except (TypeError, ValueError):
        col_no = 0
    return {"engine": engine, "file": _rel(root, file), "line": line_no, "column": col_no,
            "rule": str(rule or ""), "message": str(message or "").strip(),
            "severity": severity or _severity(str(rule or ""))}


# --------------------------------------------------------------------------- #
# python: ruff
# --------------------------------------------------------------------------- #

def parse_ruff(payload: str, root: str) -> tuple[list[dict[str, Any]], str]:
    """解析 `ruff check --output-format=json`；截断或空输出时退回 concise 行格式。"""
    text = (payload or "").strip()
    if not text:
        return [], ""
    start = text.find("[")
    if start >= 0:
        try:
            rows = json.loads(text[start:])
        except ValueError:
            rows = None
        if isinstance(rows, list):
            issues = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                loc = row.get("location") or {}
                issues.append(_issue(
                    root, engine="ruff", file=str(row.get("filename") or ""),
                    line=loc.get("row"), column=loc.get("column"),
                    rule=str(row.get("code") or ""), message=str(row.get("message") or "")))
            return issues, "json"
    issues = []
    for line in text.splitlines():
        match = _RUFF_CONCISE.match(line.strip())
        if not match:
            continue
        groups = match.groupdict()
        issues.append(_issue(root, engine="ruff", file=groups["file"], line=groups["line"],
                             column=groups["col"], rule=groups["rule"] or "",
                             message=groups["message"]))
    return issues, "concise"


def python_diagnostics(root: str, targets: list[str], *, timeout: int = DEFAULT_TIMEOUT,
                       config: str = "", rules: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"engine": "ruff", "ok": False, "issues": [], "notes": [],
                              "error": "", "format": "", "exit_code": None, "select": ""}
    select, err = _select_rules(rules)
    if err:
        result["error"] = err
        return result
    result["select"] = select
    if not str(rules or "").strip():
        result["notes"].append("默认只查会咬人的规则（" + DEFAULT_SELECT
                               + "）；要风格全检用 rules: ALL 或 rules: E,W,F")
    if targets:
        kept = []
        for raw in targets:
            full, err = code_intel._contained_path(os.path.normpath(root), raw)
            if err:
                result["error"] = err
                return result
            kept.append(full)
        if len(kept) > MAX_TARGETS:
            result["error"] = f"一次最多诊断 {MAX_TARGETS} 个目标（当前 {len(kept)}）；请分批或给目录。"
            return result
        args = kept
    else:
        args = ["."]
        result["notes"].append("未给 target，按整个代码根目录扫描（大仓库会比较慢）。")

    argv = [sys.executable, "-m", "ruff", "check", "--output-format=json", "--no-cache",
            "--quiet", f"--select={select}"]
    if config:
        cfg_full, err = code_intel._contained_path(os.path.normpath(root), config)
        if err:
            result["error"] = err
            return result
        argv.append(f"--config={cfg_full}")
    argv.extend(args)
    rep = process_runner.run_bounded(argv, cwd=root, timeout=timeout,
                                     head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS,
                                     command_text=" ".join(argv[:6]) + " ...")
    if rep.get("error"):
        result["error"] = f"ruff 无法运行：{rep['error']}"
        result["notes"].append("未安装时执行：python -m pip install ruff")
        return result
    if rep.get("timed_out"):
        result["error"] = f"ruff 超时（>{timeout}s）；请用 target: 缩小范围。"
        return result
    if "No module named ruff" in (rep.get("output") or ""):
        result["error"] = "ruff 未安装（python -m pip install ruff）。"
        return result
    raw = rep.get("output") or ""
    issues, fmt = parse_ruff(raw, root)
    exit_code = rep.get("exit_code")
    result["format"] = fmt
    result["exit_code"] = exit_code
    if not issues and exit_code not in (0, None):
        # ruff 只有两种「没问题」的样子：退出码 0 且空输出。其他一律如实报无法解析，
        # 绝不回「干净」——曾经 8786 字符的 JSON 被截断成非法 JSON，静默报了 0 条。
        result["error"] = ("ruff 退出码 %s 说明有发现，但输出没能解析（可能被截断）；"
                           "请用 target: 缩小范围重跑。原始尾部：\n%s"
                           % (exit_code, raw.strip()[-400:]))
        return result
    result["ok"] = True
    result["issues"] = issues
    if rep.get("truncated") or fmt == "concise":
        result["notes"].append("输出过长被截断，已退回逐行解析，条目可能不全。")
    return result


# --------------------------------------------------------------------------- #
# frontend: vue-tsc（经 npm run typecheck）
# --------------------------------------------------------------------------- #

def frontend_dir(root: str) -> str:
    for name in FE_DIRS:
        cand = os.path.join(root, name)
        if os.path.isfile(os.path.join(cand, "package.json")):
            return cand
    return ""


def parse_tsc(payload: str, prefix: str) -> list[dict[str, Any]]:
    issues = []
    for line in (payload or "").splitlines():
        match = _TSC_LINE.match(line.strip())
        if not match:
            continue
        groups = match.groupdict()
        issues.append(_issue("", engine="vue-tsc",
                             file=f"{prefix}/{groups['file']}".replace("//", "/"),
                             line=groups["line"], column=groups["col"],
                             rule=groups["rule"], message=groups["message"],
                             severity="error" if groups["sev"] == "error" else "warning"))
    return issues


def frontend_diagnostics(root: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    result: dict[str, Any] = {"engine": "vue-tsc", "ok": False, "issues": [], "notes": [],
                              "error": "", "exit_code": None}
    fe = frontend_dir(root)
    if not fe:
        result["error"] = "没找到带 package.json 的前端目录（试过 " + "/, ".join(FE_DIRS) + "）。"
        return result
    try:
        with open(os.path.join(fe, "package.json"), encoding="utf-8") as handle:
            scripts = (json.load(handle).get("scripts") or {})
    except (OSError, ValueError) as exc:
        result["error"] = f"package.json 读不了：{exc}"
        return result
    if "typecheck" not in scripts:
        result["error"] = "前端没有 typecheck 脚本，无法做类型诊断。"
        return result
    npm = os.getenv("DOCMIND_NPM_BIN") or shutil.which("npm")
    if not npm:
        result["error"] = "没找到 npm（可用 DOCMIND_NPM_BIN 指定路径）。"
        return result

    rel = _rel(root, fe)
    argv = [npm, "run", "typecheck"]
    rep = process_runner.run_bounded(argv, cwd=fe, timeout=timeout,
                                     head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS,
                                     command_text=" ".join(argv))
    if rep.get("error"):
        result["error"] = f"npm 无法运行：{rep['error']}"
        return result
    if rep.get("timed_out"):
        result["error"] = f"typecheck 超时（>{timeout}s）。"
        return result
    raw = rep.get("output") or ""
    result["exit_code"] = rep.get("exit_code")
    result["issues"] = parse_tsc(raw, rel)
    result["ok"] = True
    if not result["issues"] and (rep.get("exit_code") or 0) != 0:
        tail = raw.strip()[-500:]
        result["notes"].append("typecheck 失败但没解析出类型错误条目，原始尾部：\n" + tail)
    if rep.get("truncated"):
        result["notes"].append("typecheck 输出被截断，条目可能不全。")
    result["notes"].append(f"命令在 {rel}/ 内执行（vue-tsc --noEmit + tsc --noEmit）。")
    # Wave34：eslint 那一腿，`LINT_GATES` 决定它的发现是否进入判定。
    # 无论参不参与判定，它的发现与"没跑成"都必须在 notes 与报告里看得见——
    # 看不见就等于"假干净"（这份文件的既有规矩）。
    result["lint"] = frontend_lint(root, timeout=timeout)
    lint = result["lint"]
    if lint.get("issues") and LINT_GATES:
        result["issues"].extend(lint["issues"])
    if lint.get("issues"):
        result["notes"].append("eslint 报了 %s 条（%s）。"
                               % (len(lint["issues"]),
                                  "参与判定" if LINT_GATES else "只报不卡"))
    result["notes"].extend(lint.get("notes") or [])
    if lint.get("error"):
        # 跑挂了必须让整个工具变红——`tests/test_dev_diagnostics.py` 的模块 docstring
        # 写着「最要紧的不是能不能找出问题，而是跑不成时不能报成没问题」。
        # 只挂 note 是不够的：报告顶部那句「没有发现问题」会盖住它。
        result["error"] = (("%s；" % result["error"] if result.get("error") else "")
                           + "eslint 没跑成：%s" % lint["error"].split("\n")[0])
    return result


def frontend_lint_configured(fe: str) -> tuple[bool, str]:
    """这个前端目录**配了** eslint 吗？返回 (配了, 依据)。

    两条依据任一即可：有 `lint` 脚本，或有 eslint 的配置文件。**只看脚本**不够——
    脚本可能在而依赖没装；**只看配置**也不够——配置可能没接进 `npm run`。
    """
    try:
        with open(os.path.join(fe, "package.json"), encoding="utf-8") as handle:
            scripts = (json.load(handle).get("scripts") or {})
    except (OSError, ValueError):
        scripts = {}
    if "lint" in scripts:
        return True, "package.json 里有 lint 脚本"
    for name in ("eslint.config.js", "eslint.config.mjs", "eslint.config.ts",
                 ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
                 ".eslintrc.yaml", ".eslintrc.yml"):
        if os.path.isfile(os.path.join(fe, name)):
            return True, "有 %s" % name
    return False, "既没有 lint 脚本，也没有 eslint 配置文件"


#: eslint 那一腿**参不参与判定**。Wave33 先搭骨架时是 False（只报不卡），Wave34 实测出
#: 存量只有 15 条（见 docs §5.27）之后改成 True。
#:
#: 为什么当初要留这道开关：那份代码从没被 lint 过，`frontend/src` 有 129 个文件 / 39352 行，
#: 而 node_modules 里连 eslint 都没有。**万一**规则集一上来就报几千条，那道红会变成噪声，
#: 比没有 lint 更坏（它让人误以为有人在管）。现在数字量过了：vue/essential + 三条豁免
#: = 15 条 error、0 warning，其中 5 条是死 import。
LINT_GATES = True

#: eslint `stylish` 输出的一行，长这样：
#:   /abs/path/src/App.vue
#:     12:3  error  Unexpected console statement  no-console
_STYLISH_FILE = re.compile(r"^(?P<path>[^\s].*)$")
_STYLISH_ITEM = re.compile(
    r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+"
    r"(?P<msg>.+?)\s\s+(?P<rule>[\w@/-]+)\s*$")
_STYLISH_SUMMARY = re.compile(r"^\s*(?P<problems>\d+)\s+problems?\b", re.I)


def parse_eslint(payload: str, prefix: str) -> list[dict[str, Any]]:
    """解析 eslint `stylish` 输出（默认 formatter）。

    按**文件分组**读：文件行不带行列与 `error`/`warning`，条目行带。
    结尾那行 `N problems (X errors, Y warnings)` 是汇总，不是条目——第一版没排除它，
    于是会被当成一条"文件名是『12 problems (3 errors, 0 warnings)』"的假条目。
    """
    issues: list[dict[str, Any]] = []
    current: str | None = None
    for raw in str(payload or "").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _STYLISH_ITEM.match(line)
        if item:
            if current is None:
                continue                       # 条目出现在任何文件行之前：认不出归属，跳过
            issues.append(_issue(
                prefix, engine="eslint", file=current,
                line=int(item.group("line")), column=int(item.group("col")),
                rule=item.group("rule"),
                message=item.group("msg").strip(),
                severity=item.group("sev")))
            continue
        if _STYLISH_SUMMARY.match(line):
            current = None                     # 汇总行之后不再有归属
            continue
        head = _STYLISH_FILE.match(line)
        if head and not line.startswith((" ", "\t")):
            current = head.group("path").strip()
    return issues


def frontend_lint(root: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """前端 lint 这一腿。**只报不卡**（Wave33 搭的骨架）。

    为什么先"只报不卡"：`frontend/src` 有 129 个文件 / 39352 行，**从来没被 lint 过**
    （node_modules 里连 eslint 都没有）。贸然让它参与判定，第一天大概率是几千条，
    而几千条红会变成噪声——比没有 lint 更坏，因为它让人误以为有人在管。
    所以这里把发现放进 `issues` 与 `notes`、**不进** `run()` 的 `issues`、**不改** `ok`；
    等真装上 eslint、拿到存量数字，再决定要不要卡（改一个常量即可）。

    **没配就明说没跑**，绝不报成"干净"（这份文件的既有规矩）。
    """
    out: dict[str, Any] = {"configured": False, "ran": False, "ok": False,
                           "issues": [], "notes": [], "error": "", "why": ""}
    fe = frontend_dir(root)
    if not fe:
        out["error"] = "没找到带 package.json 的前端目录。"
        return out
    configured, why = frontend_lint_configured(fe)
    out["configured"] = configured
    out["why"] = why
    if not configured:
        out["notes"].append(
            "前端没有配置 eslint（%s）——**这一腿没跑**，它不构成「前端没有写法问题」的证据。"
            % why)
        return out
    npm = os.getenv("DOCMIND_NPM_BIN") or shutil.which("npm")
    if not npm:
        out["error"] = "没找到 npm（可用 DOCMIND_NPM_BIN 指定路径）。"
        return out
    rel = _rel(root, fe)
    argv = [npm, "run", "--silent", "lint"]
    rep = process_runner.run_bounded(argv, cwd=fe, timeout=timeout,
                                     head_chars=HEAD_CHARS, tail_chars=TAIL_CHARS,
                                     command_text=" ".join(argv))
    if rep.get("error"):
        out["error"] = "npm 无法运行 lint：%s" % rep["error"]
        return out
    if rep.get("timed_out"):
        out["error"] = "lint 超时（>%ss）。" % timeout
        return out
    raw = rep.get("output") or ""
    code = int(rep.get("exit_code") or 0)
    out["ran"] = True
    out["exit_code"] = code
    out["issues"] = parse_eslint(raw, rel)
    if not out["issues"] and code != 0:
        # **退出码非 0 却一条都没解析出来 = 它跑挂了，不等于"没问题"。**
        # 第一版只加了条 note，于是 eslint 根本没装（`npm run lint` 报
        # `'eslint' is not recognized`、退出码 1）时，报告照样写「没有发现问题」——
        # 骨架自己变成了假干净的样本。这里必须报 error。
        out["error"] = ("lint 退出码 %s 但没解析出任何条目——它多半没跑成，"
                        "不能当成「没有问题」。原始尾部：\n%s"
                        % (code, raw.strip()[-500:]))
        return out
    out["ok"] = True
    if rep.get("truncated"):
        out["notes"].append("lint 输出被截断，条目可能不全。")
    out["notes"].append("eslint 在 %s/ 内执行；这一腿**参与判定**（存量 15 条，见 docs §5.27）。"
                       % rel)
    return out


# --------------------------------------------------------------------------- #
# dispatch + render
# --------------------------------------------------------------------------- #

def run(root: str, target: Any = "", *, scope: str = "auto",
        timeout: int = DEFAULT_TIMEOUT, config: str = "", rules: str = "") -> dict[str, Any]:
    """按目标自动选引擎；`scope: py|frontend|all` 可强制。"""
    raw_targets = target if isinstance(target, (list, tuple)) else \
        re.split(r"[,;\n]", str(target or ""))
    targets = [str(item).strip() for item in raw_targets if str(item).strip()]
    want = (scope or "auto").strip().lower()
    if want not in ("auto", "py", "frontend", "all"):
        want = "auto"
    if want == "auto":
        if targets and all(os.path.splitext(t)[1].lower() in (".py", ".pyi") for t in targets):
            want = "py"
        elif targets and all(literal_is_frontend(root, t) for t in targets):
            want = "frontend"
        else:
            want = "all"
    out: dict[str, Any] = {"ok": True, "scope": want, "targets": targets,
                           "engines": {}, "issues": [], "notes": [], "error": ""}
    wanted = []
    if want in ("py", "all"):
        wanted.append(("python", lambda: python_diagnostics(
            root, [t for t in targets if not literal_is_frontend(root, t)],
            timeout=timeout, config=config, rules=rules)))
    if want in ("frontend", "all"):
        wanted.append(("frontend", lambda: frontend_diagnostics(root, timeout=timeout)))
    for name, call in wanted:
        part = call()
        out["engines"][name] = part
        out["issues"].extend(part.get("issues") or [])
        out["notes"].extend(part.get("notes") or [])
        if part.get("error"):
            out["ok"] = False
            out["error"] = part["error"]
    out["issues"].sort(key=lambda row: (row["file"], row["line"], row["rule"]))
    out["truncated"] = len(out["issues"]) > MAX_ISSUES
    out["issues"] = out["issues"][:MAX_ISSUES]
    out["count"] = len(out["issues"])
    return out


def literal_is_frontend(root: str, target: str) -> bool:
    rel = str(target or "").replace("\\", "/").lstrip("./")
    if any(rel.startswith(name + "/") for name in FE_DIRS):
        return True
    return os.path.splitext(rel)[1].lower() in (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")


def render(result: dict[str, Any]) -> str:
    lines: list[str] = []
    issues = result.get("issues") or []
    if result.get("error"):
        lines.append("诊断工具报错：" + result["error"])
    if not issues:
        engines = result.get("engines") or {}
        ran = [name for name, part in engines.items() if part.get("ok")]
        if ran and not result.get("error"):
            lines.append(f"没有发现问题（已跑：{', '.join(ran)}）。")
        elif not ran:
            lines.append("没有可用的诊断结果——注意这不等于代码没问题。")
        else:
            lines.append("已跑通的引擎没有报出条目，但有引擎失败，见上面。")
    else:
        lines.append(f"发现 {result['count']} 条问题（按 文件:行 排序）：")
        current = None
        for row in issues:
            if row["file"] != current:
                current = row["file"]
                lines.append(f"  {current}")
            where = f"{row['line']}:{row['column']}"
            lines.append(f"    {where}  {row['severity']} {row['rule']}  {row['message'][:160]}"
                         f"  [{row['engine']}]")
        if result.get("truncated"):
            lines.append(f"提示: 只显示前 {MAX_ISSUES} 条，用 target: 缩小范围再看剩下的。")
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    lint = ((result.get("engines") or {}).get("frontend") or {}).get("lint") or {}
    if lint and lint.get("error"):
        # 跑挂了比没跑更要紧：没跑是"不知道"，跑挂了是"它以为它跑过了"
        lines.append("注意: eslint 这一腿**没跑成**——%s" % lint["error"].split("\n")[0])
    elif lint and not lint.get("ran"):
        # 没跑就**说没跑**，而且要说清它不构成任何证据——只在 notes 里带一句会被淹掉
        lines.append("注意: eslint 这一腿**没跑**（%s）。"
                     "上面那些结论只覆盖类型检查，不覆盖写法。" % (lint.get("why") or "未配置"))
    return "\n".join(lines)
