r"""Windows 批处理的引号静态门：一条会静默毁掉参数的 bug 类。

## 这个坑长什么样（2026-10-02 真实踩到）

`run_desktop.bat` 里写着 `... dependency_bootstrap.py --root "%~dp0"`。`%~dp0` 展开成
`D:\WorkBuddy\rag-agent\`——**它天生以反斜杠结尾**，于是命令行上的字节是：

    --root "D:\WorkBuddy\rag-agent\"

cmd 自己不在乎，但被调用的**外部程序**（python / powershell / node / npm…）按 MSVCRT 的
argv 规则解析这串字节：`\"` 是「转义过的引号」，**闭合引号就此消失**，参数值里多出一个
字面 `"`。现场症状是这样一个既不像引号问题也不像路径问题的错：

    FileNotFoundError: missing D:\WorkBuddy\rag-agent"\requirements.txt

源码里看着一切正常，只有双击运行才炸——所以这道门必须在静态层就咬住。

## 两条规则（互为镜像，只写一条就成了错误的建议）

* `quoted_dir_expansion`：外部命令的实参**加了引号，但展开后以反斜杠结尾**。
  正确写法是 `"%~dp0."`（补一个点）或 `"%ROOT:~0,-1%"`（切掉尾字符）。
* `unquoted_dir_expansion`：外部命令的实参是**没引号的**目录展开（`%~dp0`）。
  仓库路径里有空格时（`Program Files`、用户名带空格）会被切成两个参数。
  这条挡住「为了躲开上一条干脆不加引号」这种反向误修。

## 边界（都是 cmd 自己解析、不吃 MSVCRT 规则的地方，有意不查）

* 内置命令（`cd`/`set`/`if`/`del`/`echo`/…）：`cd /d "%~dp0"` 是**正确写法**，不是 bug；
* `if` 的条件部分与 `for` 的 `in (...)` 部分；只有条件之后真正执行的那条命令才被查；
* `call x.bat`：参数交给被调批处理里的 cmd 解析；`call python x.py` 是外部程序，查；
* `set "ROOT=%~dp0"` 之后 `--root "%ROOT%"` 这类**一跳**能追到的变量会追（`_directory_vars`），
  两跳以上（变量套变量）不追。别把这道门当成「引号问题全部查过了」。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

BATCH_SUFFIXES = (".bat", ".cmd")
#: 单个文件最多读这么多字节：超了要如实报，不能显示成「读完了且没问题」。
MAX_SOURCE_BYTES = 400_000

#: 由 cmd 自己解析的词：它们的实参不进 MSVCRT，尾反斜杠无害。
#: `if`/`for`/`call` **不在这张表里**——它们各有一半需要往下查（条件/`in (...)`/被调对象），
#: 由 `arguments_of` 的分支处理；把它们整个豁免掉就等于放过 `if exist x py --root "%~dp0"`。
CMD_BUILTINS = frozenset({
    "cd", "chcp", "cls", "copy", "date", "del", "dir", "echo", "endlocal", "erase",
    "exit", "goto", "md", "mkdir", "pause", "popd", "prompt", "pushd",
    "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "time",
    "title", "type", "ver", "vol",
})
#: `call` 后面跟批处理时等于「再开一个 cmd」，参数由 cmd 解析。
BATCH_CALL_SUFFIXES = (".bat", ".cmd")
_IF_KEYWORDS = ("exist", "errorlevel", "defined")
_IF_COMPARATORS = frozenset({"==", "equ", "neq", "lss", "leq", "gtr", "geq"})

#: `%~` + 修饰字母 + 目标数字，**停在末尾**：`%~dp0`、`%~dpn1`、`%~dp$PATH:1`。
#: 必须停在末尾才算「展开后的值以反斜杠结尾」；`%~dp0setup.py` 是安全写法。
_DIR_EXPANSION_END_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)$")
_DIR_EXPANSION_ANY_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)")
#: `%NAME%` 停在末尾——配合 `set` 一跳表判断这个变量是不是目录。
_VAR_AT_END_RE = re.compile(r"%([A-Za-z_]\w{0,63})%$")
#: cmd 的重定向：`>nul`、`2>&1`、`>>log.txt`——它们不是实参。
_REDIRECT_RE = re.compile(r"^\d*[<>]")
#: 分隔符：`&`/`&&`/`|`；`2>&1` 里跟在 `>` 后面的 `&` 不算分隔符。
_SEGMENT_SPLIT_RE = re.compile(r"(?<![<>])(?:&&|[&|])")
#: `set "NAME=value"` 与 `set NAME=value`（只用于一跳追踪）。
_SET_RE = re.compile(r"""^set\s+(?:/a\s+)?["']?([A-Za-z_]\w{0,63})=(.*)$""", re.IGNORECASE)

RULE_QUOTED = "quoted_dir_expansion"
RULE_UNQUOTED = "unquoted_dir_expansion"
RULE_TOO_LARGE = "file_too_large"

_FIXES = {
    RULE_QUOTED: '写成 "%~dp0."（补一个点），或用 "%ROOT:~0,-1%" 切掉尾字符',
    RULE_UNQUOTED: '给它加引号——但别写成 "%~dp0"，要写 "%~dp0."',
    RULE_TOO_LARGE: "批处理不该长到这个量级：拆开，或把参数逻辑集中到一处",
}


class BatGuardError(RuntimeError):
    """无法读取待扫描的内容。"""


def _bare(token: Any) -> str:
    """去掉首尾引号后的形状（判断命令词/后缀用）。"""
    text = str(token).strip()
    return text[1:-1] if len(text) >= 2 and text[0] == '"' and text[-1] == '"' else text


def _quoted_spans(token: Any) -> list[str]:
    """token 里被引号夹住的片段。cmd 不做反斜杠转义，所以按配对切就够。"""
    pieces = str(token).split('"')
    return [pieces[i] for i in range(1, len(pieces), 2)]


def _tokens(line: str) -> list[str]:
    """按 cmd 的空格分词，引号内的空格不切。"""
    out: list[str] = []
    buf: list[str] = []
    quoted = False
    for ch in str(line):
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif not quoted and ch in " \t":
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def logical_lines(text: str) -> list[tuple[int, str]]:
    """物理行 → (行号, 逻辑行)。`^` 续行要拼起来，否则参数正好断在续行处就查不到。"""
    rows: list[tuple[int, str]] = []
    pending, start = "", 0
    for index, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not pending:
            start = index
        if line.endswith("^"):
            pending += line[:-1].rstrip() + " "
            continue
        rows.append((start, (pending + line).strip()))
        pending = ""
    if pending.strip():
        rows.append((start, pending.strip()))
    return [(index, row) for index, row in rows if row]


def _directory_vars(text: str) -> set[str]:
    """一跳追踪：`set "ROOT=%~dp0"` 之后的 `"%ROOT%"` 与 `"%~dp0"` 是同一个坑。

    为什么值得追这一跳：把 `--root "%~dp0"` 「修」成 `set ROOT=%~dp0` + `--root "%ROOT%"`
    是最自然的重构，而它**一个字节都没有改变**问题。不追这一跳的门等于鼓励误修。
    """
    known: set[str] = set()
    for _, line in logical_lines(text):
        match = _SET_RE.match(line.strip())
        if not match:
            continue
        name, value = match.group(1).lower(), match.group(2).strip().strip('"')
        if _DIR_EXPANSION_END_RE.search(value):
            known.add(name)
            continue
        tail = _VAR_AT_END_RE.search(value)
        if tail and tail.group(1).lower() in known:
            known.add(name)
    return known


def _segments(line: str) -> list[str]:
    """一行里 `&`/`&&`/`|` 隔开的每条命令。"""
    return [row.strip() for row in _SEGMENT_SPLIT_RE.split(line) if row.strip()]


def _after_if(tokens: list[str]) -> list[str]:
    """砍掉 `if` 的条件部分，返回真正要执行的那段。

    cmd 里 `"%X%"=="0" goto y` 的比较式是**一个 token**（`==` 两侧没有空格），
    所以既要比对独立的 `==`，也要认嵌在 token 里的 `==`。
    """
    rest = list(tokens)
    if rest and _bare(rest[0]).lower() == "not":
        rest = rest[1:]
    if not rest:
        return []
    head = _bare(rest[0]).lower()
    if head in _IF_KEYWORDS:
        return rest[2:]
    for index in range(len(rest)):
        word = _bare(rest[index]).lower()
        if index and word in _IF_COMPARATORS:
            return rest[index + 2:]
        if "==" in word:
            return rest[index + 1:]
    return []


def _after_for(tokens: list[str]) -> list[str]:
    """`for ... do <cmd>`：`in (...)` 部分由 cmd 解析，`do` 之后才是命令。"""
    for index, token in enumerate(tokens):
        if _bare(token).lower() == "do":
            return tokens[index + 1:]
    return []


def arguments_of(segment: str) -> list[str]:
    """这条命令交给【外部程序】的实参；cmd 自己处理的场合回空表。

    命令词是 `%DOCMIND_HOST_PYTHON%` 这类展开时按外部程序算——它展开成 `py -3`/`python`，
    后面每个参数都要过 MSVCRT。
    """
    body = str(segment).strip()
    if body.startswith("(") and body.endswith(")"):
        # `if x (py a b)`：写成一行的括号块，`(` 粘在命令词上、`)` 粘在最后的实参上。
        body = body[1:-1].strip()
    tokens = _tokens(body)
    if not tokens:
        return []
    word = _bare(tokens[0]).lower().lstrip("@")
    if word == "call":
        rest = tokens[1:]
        if not rest:
            return []
        if _bare(rest[0]).lower().endswith(BATCH_CALL_SUFFIXES):
            return []              # 调的是批处理：参数交给 cmd，不是 MSVCRT
        return arguments_of(" ".join(rest))
    if word == "if":
        return arguments_of(" ".join(_after_if(tokens[1:])))
    if word == "for":
        return arguments_of(" ".join(_after_for(tokens[1:])))
    if word in CMD_BUILTINS or word in ("(", ")"):
        return []
    args = tokens[1:]
    while args and _bare(args[0]) in ("(", ")"):
        args = args[1:]
    return [token for token in args if not _REDIRECT_RE.match(token)]


def _expands_to_directory(content: str, dir_vars: Iterable[str]) -> str:
    """引号里的这段，展开后会不会以反斜杠结尾？会的话说清是哪一种。"""
    if (len(content) - len(content.rstrip("\\"))) % 2 == 1:
        return "字面尾反斜杠把闭合引号转义掉了"
    if _DIR_EXPANSION_END_RE.search(content):
        return "%~dp 这类目录展开展开后自带尾反斜杠"
    tail = _VAR_AT_END_RE.search(content)
    if tail and tail.group(1).lower() in set(dir_vars):
        return "%s 被 set 成了目录展开（一跳追到的）" % tail.group(0)
    return ""


def find_issues(text: str, source: str = "") -> list[dict[str, Any]]:
    """扫一份批处理源码，回每条问题（纯函数：不碰文件系统、不看平台）。"""
    dir_vars = _directory_vars(text)
    findings: list[dict[str, Any]] = []
    for lineno, line in logical_lines(text):
        stripped = line.lstrip().lower()
        if stripped.startswith(("rem ", "rem\t", "::", ":", "@rem")) or stripped == "rem":
            continue
        for segment in _segments(line):
            for token in arguments_of(segment):
                rule, why = "", ""
                for content in _quoted_spans(token):
                    why = _expands_to_directory(content, dir_vars)
                    if why:
                        rule = RULE_QUOTED
                        break
                if not rule and '"' not in token and (
                        _DIR_EXPANSION_ANY_RE.search(token)
                        or (_VAR_AT_END_RE.search(token)
                            and _VAR_AT_END_RE.search(token).group(1).lower() in dir_vars)):
                    rule, why = RULE_UNQUOTED, "目录展开没引号，路径里有空格就裂成两个参数"
                if rule:
                    findings.append({"path": source, "line": lineno, "rule": rule,
                                     "token": token, "command": segment.strip()[:160],
                                     "why": why, "fix": _FIXES[rule]})
    return findings


def read_source(path: Path | str) -> tuple[str, bool]:
    """按 cmd 真正会用的方式读（cp936）。回 (文本, 是否被截断)。"""
    raw = Path(path).read_bytes()
    clipped = len(raw) > MAX_SOURCE_BYTES
    body = raw[:MAX_SOURCE_BYTES]
    for codec in ("gbk", "utf-8-sig", "latin-1"):
        try:
            return body.decode(codec), clipped
        except UnicodeDecodeError:
            continue
    return body.decode("latin-1", errors="replace"), clipped


#: 这些目录不进去：`.venv`/`node_modules` 里成千上万个文件不是本仓启动器的语料，
#: 全走进去只会让这道门慢到没人跑，而一条都不会命中。
PRUNED_DIRS = frozenset({".git", ".venv", ".tmp", ".pytest_cache", "__pycache__",
                         "node_modules", "dist", "build", "web"})


def discover(root: Path | str) -> list[Path]:
    """仓里所有批处理（相对启动器那一层与子目录）。"""
    base = Path(root)
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = sorted(name for name in dirnames if name not in PRUNED_DIRS)
        for name in filenames:
            if name.lower().endswith(BATCH_SUFFIXES):
                out.append(Path(dirpath) / name)
    return sorted(out)


def scan_paths(paths: Iterable[Path | str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for path in paths:
        target = Path(path)
        try:
            text, clipped = read_source(target)
        except OSError as exc:
            raise BatGuardError("读不到批处理 %s：%s" % (target, exc)) from exc
        relative = str(target)
        if clipped:
            findings.append({"path": relative, "line": 0, "rule": RULE_TOO_LARGE,
                             "token": "", "command": "",
                             "why": "文件超过 %d 字节，只扫了前一段" % MAX_SOURCE_BYTES,
                             "fix": _FIXES[RULE_TOO_LARGE]})
        findings.extend(find_issues(text, relative))
    return findings


def audit(root: Path | str) -> dict[str, Any]:
    """扫一棵树，回结构化结论。`files_scanned == 0` 绝不是「干净」。"""
    paths = discover(root)
    try:
        findings = scan_paths(paths)
    except BatGuardError as exc:
        return {"ok": False, "error": str(exc), "files_scanned": 0, "findings": []}
    return {"ok": bool(paths) and not findings, "error": "",
            "files_scanned": len(paths), "findings": findings,
            "files": [str(row) for row in paths]}


def render(report: dict[str, Any]) -> str:
    findings = report.get("findings") or []
    if not report.get("files_scanned"):
        return "批处理引号门：一个 .bat/.cmd 都没读到——这不是「干净」，是没扫。"
    if not findings:
        return ("批处理引号门通过：%d 个批处理里，交给外部程序的实参既没有"
                "「展开后以反斜杠结尾的引号参数」，也没有裸的目录展开。"
                % report["files_scanned"])
    lines = ["批处理引号门：%d 个文件里发现 %d 处。" % (report["files_scanned"], len(findings))]
    for row in findings:
        lines.append("%s:%s [%s] %s" % (row["path"], row["line"] or "?", row["rule"],
                                         row["token"]))
        lines.append("    为什么：%s" % row["why"])
        lines.append("    怎么改：%s" % row["fix"])
        lines.append("    所在命令：%s" % row["command"])
    lines.append("被 cmd 自己解析的地方（cd/set/if 条件/echo）有意不查——"
                 "见模块文档的「边界」一节。")
    return "\n".join(lines)


__all__ = ["BatGuardError", "CMD_BUILTINS", "MAX_SOURCE_BYTES", "RULE_QUOTED",
           "RULE_UNQUOTED", "arguments_of", "audit", "discover", "find_issues",
           "logical_lines", "read_source", "render", "scan_paths"]
