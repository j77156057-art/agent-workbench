"""Agent-facing code intelligence: read-only git diff preview and symbol reference lookup.

Both helpers are strictly read-only and bounded:

* :func:`git_diff_preview` shells out to ``git`` through the bounded runner
  (:mod:`agent_runtime.process_runner`) so a runaway repo can never hang the agent,
  and only ever calls ``rev-parse``/``status``/``diff``/``log``.
* :func:`git_log_preview` is the read-only history view (who changed what, and
  which commits touched a path).
* :func:`glob_files` finds files by name pattern, which ``grep`` cannot do.
* :func:`find_references` walks the source tree with a hard file/byte budget and
  uses :mod:`ast` for Python so comments and string literals are never reported
  as references (the whole reason this exists instead of ``grep``).
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
from typing import Any

from . import process_runner, ts_index

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

_GIT_TIMEOUT = 20
_GIT_MAX_TIMEOUT = 60

#: git subcommands this module is allowed to run. Read-only by construction.
_GIT_ALLOWED = frozenset({"rev-parse", "status", "diff", "log"})

_STAT_CHARS = 6000
_LIST_FILES = 60
_RENDERED_REFS = 80

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "dist", "dist-ssr",
    "build", "build.bak", "coverage", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "site-packages", "_archived_builds", ".tmp",
})

# `.docmind/` 是 DocMind 自己的状态根，但**产物也会写在这里** —— Godot 的 Web
# 导出默认就在 `.docmind/web/index.html`（见 export_presets.cfg 的 export_path）。
#
# ⚠ 这里曾经把整个 `.docmind` 塞进 `_SKIP_DIRS`。实测后果：Agent 在
# `D:\WorkBuddy\godot_sample` 里 `dev_glob('**/*.html')` 回报
# 「没有命中文件」，而 `.docmind/web/index.html` 就在脚下 ——
# 一个**没有任何保留的假否定**，比"扫到一半被截断"危险得多，因为它读起来
# 就是"这个项目没有 html"。
#
# 现在改成**按名修剪状态子目录**：DocMind 自己写的临时/状态目录不扫（实测
# project_stages 一个就 752MB/6978 文件，projects 下还有 headcheck 的整仓
# 影子副本 —— 见 docs/known-defect-docmind-shadow-repo-copies.md），
# 但产物目录（web/ 等）保持可见。
#
# 为什么用黑名单而不是"只允许产物目录"的白名单：白名单会让**新出现的产物目录**
# 重新变成静默假否定 —— 那正是本次要修的缺陷本身。黑名单最坏情况是多扫一点，
# 由 `_GLOB_MAX_SCANNED` 兜底，且会在 notes 里明说被修剪了什么。
_DOCMIND_STATE_DIRS = frozenset({
    "project_stages",        # 工作流临时副本（实测 752MB / 6978 文件）
    "project_checkpoints",   # 检查点（172MB / 3647 文件）
    "projects",              # 每项目状态，含 headcheck 整仓副本（影子）
    "projects-orphan-backup",# 旧备份（前缀匹配）
    "media-fixture",         # 测试素材，tools.py 注记"已 gitignore"
    "page-action",           # 页面动作截图
    "visual-evidence",       # 视觉验收截图
    "screenshots",           # 截图
    "retrieval",             # 检索中间产物
    "workflows",             # 工作流状态
    "cockpit-validation",    # 驾驶舱验收
    "dependency-state",      # 依赖状态
    "skills",                # 技能缓存
})
# 动态命名的状态目录前缀（一次性的 visual 证据目录等）。
_DOCMIND_STATE_PREFIXES = ("docmind-production-visual-",)

_SOURCE_EXTS = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".gd", ".shader", ".hlsl", ".glsl", ".rb", ".php", ".swift", ".scala", ".lua",
    ".sql", ".proto", ".md", ".json", ".yaml", ".yml", ".toml",
})

_MAX_FILES = 4000
_MAX_FILE_BYTES = 2 * 1024 * 1024


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _window(text: str, head: int = 4000, tail: int = 4000) -> tuple[str, bool, int]:
    """Return (windowed_text, truncated, total_chars) keeping head AND tail."""
    text = text or ""
    total = len(text)
    if total <= head + tail:
        return text, False, total
    omitted = total - head - tail
    return (text[:head] + f"\n...[省略 {omitted} 字符]...\n" + text[total - tail:],
            True, total)


def _is_probably_text(sample: bytes) -> bool:
    if b"\x00" in sample:
        return False
    return True


# --------------------------------------------------------------------------- #
# git diff preview
# --------------------------------------------------------------------------- #

def _run_git(args: list[str], *, cwd: str, timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    """Run one read-only git command through the bounded runner."""
    if not args or args[0] not in _GIT_ALLOWED:
        return {"ok": False, "command": " ".join(args),
                "error": f"拒绝执行：{args[0] if args else ''} 不在只读白名单内。"}
    # core.quotepath=false: 不要把中文/非 ASCII 路径转义成八进制，否则清单不可读
    argv = ["git", "--no-pager", "-c", "core.quotepath=false"] + args
    seconds = process_runner.clamp_timeout(timeout, _GIT_MAX_TIMEOUT, _GIT_TIMEOUT)
    rep = process_runner.run_bounded(argv, cwd=cwd, timeout=seconds,
                                     command_text=" ".join(argv))
    # git writes warnings to stderr, which the runner merges into stdout. Left
    # alone they get parsed as status entries ("w ning: could not open ...").
    if rep.get("output"):
        rep["raw_output"] = rep["output"]
        rep["output"] = _strip_git_noise(rep["output"])
    return rep


#: git 的诊断行（stderr 被合并进 stdout）。diff 正文行必带前导 +/-/空格/@，
#: 因此「行首直接是 warning:」的一定是噪声，可以安全剔除。
_GIT_NOISE_LINE = re.compile(r"^(warning|error|fatal|hint|notice)\s*:\s*", re.I)


def _strip_git_noise(text: str) -> str:
    """Drop git's top-level diagnostic lines while preserving everything else."""
    if not text:
        return text
    kept = [ln for ln in text.splitlines() if not _GIT_NOISE_LINE.match(ln)]
    return "\n".join(kept) + ("\n" if text.endswith("\n") else "")


def _repo_top(root: str) -> tuple[str | None, str]:
    """Resolve the worktree top-level for ``root``; (None, reason) when unavailable."""
    rep = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
    if rep.get("error"):
        return None, f"无法执行 git：{rep['error']}"
    if rep.get("timed_out"):
        return None, "git rev-parse 超时，仓库可能过大或 git 被卡住。"
    if rep.get("exit_code") != 0:
        detail = (rep.get("output") or "").strip().splitlines()
        return None, "当前代码根目录不是 git 工作树：" + (detail[0][:200] if detail else "无输出")
    out = (rep.get("output") or "").strip()
    if not out:
        return None, "git rev-parse 返回空路径，无法定位仓库顶层。"
    return os.path.normpath(out.splitlines()[0]), ""


def _scope_prefix(top: str, root: str) -> str:
    """Relative path of ``root`` inside ``top`` ('' when root *is* the top)."""
    try:
        rel = os.path.relpath(os.path.normpath(root), top)
    except ValueError:
        return ""
    rel = rel.replace("\\", "/")
    return "" if rel in (".", "") else rel


def _validate_paths(paths: list[str], root: str, top: str) -> tuple[list[str] | None, str]:
    """Reject flag injection and anything escaping the code root."""
    root_abs = os.path.normpath(root)
    top_abs = os.path.normpath(top)
    kept: list[str] = []
    for raw in paths:
        p = (raw or "").strip().strip("'\"")
        if not p:
            continue
        if p.startswith("-"):
            return None, f"拒绝路径 {p!r}：看起来像命令行选项，疑似参数注入。"
        candidate = p if os.path.isabs(p) else os.path.join(root_abs, p)
        candidate = os.path.normpath(candidate)
        if not (candidate == root_abs or candidate.startswith(root_abs + os.sep)):
            return None, f"拒绝路径 {p!r}：不在代码根目录内（禁止越界读取）。"
        rel = os.path.relpath(candidate, top_abs).replace("\\", "/")
        if rel in (".", ""):
            rel = "."
        kept.append(rel)
    return kept, ""


#: porcelain 状态列合法取值：X∈[MADRCUT? ]，Y∈[MADRCUT?! ]
_VALID_XY = re.compile(r"^([MADRCUT? ])([MADRCUT?! ])$")


def _parse_porcelain(text: str) -> list[tuple[str, str]]:
    """Parse `git status --porcelain`, dropping anything that is not a real entry.

    git diagnostics (already stripped) or unexpected output would otherwise be
    mistaken for a file: `warning: ...` parses as status ``w `` + ``ning: ...``.
    """
    entries: list[tuple[str, str]] = []
    for line in (text or "").splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if not _VALID_XY.match(xy) or xy == "  ":
            continue
        rest = line[3:]
        path = rest
        if " -> " in rest and "R" in xy:
            path = rest.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        entries.append((xy, path))
    return entries


def git_diff_preview(root: str, *, paths: list[str] | None = None, staged: bool = False,
                     stat_only: bool = False, context: int = 3,
                     timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    """Read-only preview of what changed. Never writes, never stages, never commits."""
    result: dict[str, Any] = {
        "ok": False, "root": root, "staged": bool(staged), "stat_only": bool(stat_only),
        "top": None, "paths": [], "files": [], "staged_files": [], "unstaged_files": [],
        "untracked_files": [], "stat": "", "stat_truncated": False, "diff": "",
        "diff_truncated": False, "diff_total_chars": 0, "error": "", "notes": [],
    }
    if not root or not os.path.isdir(root):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    top, reason = _repo_top(root)
    if top is None:
        result["error"] = reason
        return result
    result["top"] = top

    requested = [p for p in (paths or []) if str(p).strip()]
    if requested:
        kept, err = _validate_paths(requested, root, top)
        if kept is None:
            result["error"] = err
            return result
        scope = kept
    else:
        prefix = _scope_prefix(top, root)
        scope = [prefix] if prefix else []
    result["paths"] = scope

    path_args = ["--"] + scope if scope else []

    # 1) inventory: git diff cannot see untracked files, so surface them separately.
    rep = _run_git(["status", "--porcelain"] + path_args, cwd=top, timeout=timeout)
    if rep.get("error"):
        result["error"] = f"git status 失败：{rep['error']}"
        return result
    entries = _parse_porcelain(rep.get("output") or "")
    staged_files, unstaged_files, untracked_files = [], [], []
    for xy, path in entries:
        x, y = xy[0], xy[1]
        if xy == "??":
            untracked_files.append(path)
            continue
        if x not in (" ", "?"):
            staged_files.append(f"{x} {path}")
        if y not in (" ", "?"):
            unstaged_files.append(f"{y} {path}")
    result["staged_files"] = staged_files
    result["unstaged_files"] = unstaged_files
    result["untracked_files"] = untracked_files
    result["files"] = [p for _, p in entries]
    result["ok"] = True

    if not entries:
        result["notes"].append("工作树与暂存区均无改动（git status 为空）。")
        return result
    if untracked_files:
        result["notes"].append(
            f"{len(untracked_files)} 个未跟踪文件不会出现在 git diff 中"
            "（diff 只看已跟踪文件）；提交前需先 git add。"
        )

    # 2) stat
    rep = _run_git(["diff", "--stat"] + (["--cached"] if staged else []) + path_args,
                   cwd=top, timeout=timeout)
    if rep.get("error"):
        result["error"] = f"git diff --stat 失败：{rep['error']}"
        result["ok"] = False
        return result
    stat, stat_trunc, _ = _window(rep.get("output") or "", _STAT_CHARS, 0)
    result["stat"] = stat
    result["stat_truncated"] = stat_trunc

    if stat_only:
        return result

    # 3) body
    try:
        ctx = max(0, min(int(context), 50))
    except (TypeError, ValueError):
        ctx = 3
    rep = _run_git(["diff", f"-U{ctx}"] + (["--cached"] if staged else []) + path_args,
                   cwd=top, timeout=timeout)
    if rep.get("error"):
        result["error"] = f"git diff 失败：{rep['error']}"
        result["ok"] = False
        return result
    result["diff"] = rep.get("output") or ""
    result["diff_truncated"] = bool(rep.get("truncated"))
    result["diff_total_chars"] = int(rep.get("total_chars") or 0)
    if not result["diff"].strip():
        side = "暂存区" if staged else "工作树"
        result["notes"].append(f"{side}没有内容差异（改动可能只在另一侧，或仅存在未跟踪文件）。")
    return result


def render_git_diff(result: dict[str, Any]) -> str:
    if result.get("error") and not result.get("ok"):
        return f"dev_git_diff 失败：{result['error']}"
    lines: list[str] = []
    staged = result.get("staged")
    lines.append(f"=== git diff（{'暂存区 vs HEAD' if staged else '工作树 vs HEAD'}）===")
    if result.get("top"):
        lines.append(f"仓库顶层: {result['top']}")
    scope = result.get("paths") or []
    lines.append("范围: " + ("、".join(scope) if scope else "代码根目录全部"))

    s, u, t = (result.get("staged_files") or [], result.get("unstaged_files") or [],
               result.get("untracked_files") or [])
    lines.append(f"变更文件 {len(s) + len(u) + len(t)}（已暂存 {len(s)} / 未暂存 {len(u)} / 未跟踪 {len(t)}）")
    combined = s + u + [f"?? {p}" for p in t]
    for item in combined[:_LIST_FILES]:
        lines.append("  " + item)
    if len(combined) > _LIST_FILES:
        lines.append(f"  …另有 {len(combined) - _LIST_FILES} 个文件未列出")

    if result.get("stat"):
        lines.append("--- 统计 ---")
        lines.append(result["stat"].rstrip())
        if result.get("stat_truncated"):
            lines.append("（统计已截断）")

    if not result.get("stat_only"):
        lines.append("--- 差异正文 ---")
        body = (result.get("diff") or "").rstrip()
        lines.append(body if body else "（无内容差异）")
        if result.get("diff_truncated"):
            lines.append(f"（差异正文已窗口化：共 {result.get('diff_total_chars')} 字符，"
                         "保留开头与结尾；需要看中间部分请加 paths: 限定到单个文件）")
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    if result.get("ok") and result.get("error"):
        lines.append("注意: " + result["error"])
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# file lookup by name pattern
# --------------------------------------------------------------------------- #

_GLOB_MAX_LIMIT = 500
_GLOB_MAX_SCANNED = 50000


def _glob_match(name: str, pattern: str) -> bool:
    """fnmatch without the platform case-folding surprise.

    All-lowercase patterns also match capitalized names (the Windows habit); a
    pattern containing an uppercase letter is compared exactly, so ``Foo.vue``
    never satisfies a search for ``foo.vue``.
    """
    if any(ch.isupper() for ch in pattern):
        return fnmatch.fnmatchcase(name, pattern)
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def _glob_hits(rel: str, pattern: str) -> bool:
    base = rel.rsplit("/", 1)[-1]
    if pattern.startswith("**/"):
        return _glob_match(base, pattern[3:]) or _glob_match(rel, pattern[3:])
    if "/" in pattern:
        return _glob_match(rel, pattern.replace("**", "*"))
    if pattern.startswith("*"):
        return _glob_match(rel, pattern) or _glob_match(base, pattern)
    return _glob_match(base, pattern)


def _contained_path(root_abs: str, raw: str) -> tuple[str, str]:
    """Resolve ``raw`` inside ``root_abs``; reject flag-looking or escaping input."""
    p = str(raw or "").strip().strip("'\"")
    if not p:
        return "", ""
    if p.startswith("-"):
        return "", f"拒绝路径 {p!r}：看起来像命令行选项，疑似参数注入。"
    candidate = os.path.normpath(p if os.path.isabs(p) else os.path.join(root_abs, p))
    if not (candidate == root_abs or candidate.startswith(root_abs + os.sep)):
        return "", f"拒绝路径 {p!r}：不在代码根目录内（禁止越界读取）。"
    return candidate, ""


def _prune(dirnames: list[str], rel_dir: str) -> list[str]:
    """就地修剪要跳过的子目录名，返回被剪掉的名字（供调用方如实回报）。

    * 任何地方都跳过 `_SKIP_DIRS`；
    * **只在 `.docmind/` 直接下一层**额外跳过 `_DOCMIND_STATE_DIRS` /
      `_DOCMIND_STATE_PREFIXES` —— 状态目录不参与内容检索，但产物目录
      （Godot 的 `web/` 等）必须可见。
    """
    kept = [d for d in dirnames if d not in _SKIP_DIRS]
    pruned = [d for d in dirnames if d in _SKIP_DIRS]
    if rel_dir == ".docmind":
        for d in list(kept):
            if d in _DOCMIND_STATE_DIRS or d.startswith(_DOCMIND_STATE_PREFIXES):
                kept.remove(d)
                pruned.append(d)
    dirnames[:] = kept
    return sorted(pruned)


def glob_files(root: str, pattern: str, *, scope: str = "",
               limit: int = _GLOB_MAX_LIMIT) -> dict[str, Any]:
    """Find files by name pattern under the code root; ``grep`` only sees contents.

    ``_SOURCE_EXTS`` deliberately does not apply here: looking files up by name
    is exactly how an agent finds ``Dockerfile``, lockfiles and ``*.yml``.
    """
    result: dict[str, Any] = {
        "ok": False, "pattern": "", "root": root, "scope": "", "files": [],
        "count": 0, "truncated": False, "scanned": 0, "scan_capped": False,
        "pruned_dirs": [], "error": "", "notes": [],
    }
    pat = str(pattern or "").strip().strip("'\"")
    if not pat:
        result["error"] = "缺少 pattern：例如 tests/test_realtime_*.py、**/liveAudioControl.ts、*.vue"
        return result
    if pat.startswith("-") or any(ord(ch) < 32 for ch in pat):
        result["error"] = f"拒绝 pattern {pat!r}：含控制字符或疑似参数注入。"
        return result
    if len(pat) > 200:
        result["error"] = "拒绝 pattern：过长（上限 200 字符）。"
        return result
    result["pattern"] = pat
    root_abs = os.path.normpath(root or "")
    if not root or not os.path.isdir(root_abs):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    base, err = _contained_path(root_abs, scope)
    if err:
        result["error"] = err
        return result
    base = base or root_abs
    if not os.path.isdir(base):
        result["error"] = f"scope 不是目录：{scope}"
        return result
    try:
        cap = int(limit)
    except (TypeError, ValueError):
        cap = _GLOB_MAX_LIMIT
    cap = max(1, min(cap, _GLOB_MAX_LIMIT))
    rel_scope = os.path.relpath(base, root_abs).replace("\\", "/")
    result["scope"] = "" if rel_scope == "." else rel_scope
    # `.docmind/` 是 DocMind 自己的状态根：状态子目录不扫，但产物目录要可见
    # （Godot Web 导出默认就在 `.docmind/web/`）。这里把被剪掉的名字如实带回，
    # 否则"没有命中文件"会被读成"这个项目没有这种文件"。
    # ⚠ 只披露 `.docmind/` 下的状态目录，不要把 `_SKIP_DIRS` 里的通用项
    # （.git/.venv/node_modules…）也列出来 —— 那些本来就不该被当成
    # "DocMind 的状态"，混在一处会让人以为 `.git` 也能用 scope: 指进去。
    _dm = os.path.join(root_abs, ".docmind")
    if os.path.normpath(base) == os.path.normpath(root_abs) and os.path.isdir(_dm):
        _probe_dirs = sorted(
            d for d in os.listdir(_dm)
            if os.path.isdir(os.path.join(_dm, d))
            and (d in _DOCMIND_STATE_DIRS or d.startswith(_DOCMIND_STATE_PREFIXES)))
        result["pruned_dirs"] = [".docmind/" + d for d in _probe_dirs]

    matches: list[str] = []
    scanned = 0
    scan_capped = False
    pruned: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root_abs).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        pruned.update(_prune(dirnames, rel_dir))
    # 只披露 `.docmind/` 的状态目录：`_SKIP_DIRS` 的通用项（.git/.venv/…）
    # 是"任何项目都不扫"，不是"DocMind 的状态"，混在一处会造成误导。
        for name in sorted(filenames):
            scanned += 1
            if scanned > _GLOB_MAX_SCANNED:
                scan_capped = True
                break
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if _glob_hits(rel, pat):
                matches.append(rel)
        if scan_capped:
            break
    # 只披露 `.docmind/` 的状态目录。`_SKIP_DIRS` 的通用项（.git/.venv/…）
    # 是"任何项目都不扫"，不是"DocMind 的状态"，混在一处会造成误导 ——
    # 实测第一版就把 `.git` 列成了"没扫的 DocMind 状态目录"。
    pruned = {d for d in pruned
              if d in _DOCMIND_STATE_DIRS or d.startswith(_DOCMIND_STATE_PREFIXES)}
    result["ok"] = True
    result["scanned"] = scanned
    result["scan_capped"] = scan_capped
    # 如实交代"我没看哪里"：修剪掉的状态目录要列出来，否则"没有命中"会被
    # 读成"这里没有"。实测那次就是：整个 `.docmind` 被剪掉、notes 为空，
    # Agent 只能得出"这个项目没有 html"的结论。
    result["pruned_dirs"] = sorted(pruned)
    matches.sort(key=lambda rel: (rel.count("/"), rel))
    result["count"] = len(matches)
    result["truncated"] = len(matches) > cap
    result["files"] = matches[:cap]
    if scan_capped:
        result["notes"].append(f"目录条目超过 {scanned} 上限，结果可能不完整；请用 scope: 缩小范围。")
    return result


def render_glob(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return "dev_glob 失败：" + (result.get("error") or "未知错误")
    pat = result.get("pattern") or ""
    scope = result.get("scope") or ""
    files = result.get("files") or []
    title = f"匹配 {pat!r}" + (f"（限定 {scope}/）" if scope else "")
    if not files:
        # ⚠ 措辞必须是"在我看过的范围里没有"，不是"这里没有"。
        # 实测事故：整个 `.docmind` 被剪掉、notes 为空，Agent 只能得出
        # "这个项目没有 html"，而 `.docmind/web/index.html` 就在脚下。
        lines = [title + "：在我扫过的范围里没有命中。检查拼写，或用 scope: 指到具体子目录再看。"]
    else:
        more = f"，只显示前 {len(files)} 个" if result.get("truncated") else ""
        lines = [f"{title}：命中 {result.get('count')} 个{more}（相对代码根目录，浅层优先）"]
        lines += [f"  {rel}" for rel in files]
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    pruned = result.get("pruned_dirs") or []
    if pruned:
        shown = ", ".join(pruned[:12]) + ("…" if len(pruned) > 12 else "")
        lines.append("提示: 这几个目录没扫（DocMind 自己的状态/临时目录，不是项目内容）：%s。" % shown)
        lines.append("      要找的文件若可能在这些目录里，请用 scope: 直接指进去。")
    if result.get("truncated"):
        lines.append("提示: 用更具体的 pattern（如 **/workbench/live*.ts）或加 scope: 收窄。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# git history preview
# --------------------------------------------------------------------------- #

#: 输出里的分隔符。必须避开 `str.splitlines()` 认作换行的字符（\x1c-\x1e 在内），
#: 否则 _strip_git_noise 会把记录边界当换行吃掉——曾经只剩第一条提交。
_LOG_FIELD_SEP = "\x1f"
_LOG_RECORD_SEP = "\x01"
_LOG_DEFAULT_LIMIT = 20
_LOG_MAX_LIMIT = 200
#: 用 git 的 %xNN 转义（参数本身保持纯 ASCII），不要把控制字符放进 argv。
_LOG_PRETTY = "--pretty=format:%x01%h%x1f%ad%x1f%an%x1f%s"


def _clean_scalar(value: str, field: str) -> tuple[str, str]:
    """A git option value that can never open a new flag or carry control chars."""
    text = str(value or "").strip().strip("'\"")
    if not text:
        return "", ""
    if text.startswith("-") or text.startswith("=") or any(ord(ch) < 32 for ch in text):
        return "", f"拒绝 {field}：取值 {text!r} 疑似参数注入。"
    if len(text) > 120:
        return "", f"拒绝 {field}：取值过长（上限 120 字符）。"
    return text, ""


def _parse_log_records(text: str) -> list[dict[str, Any]]:
    """Parse the ``\\x1e``-delimited log format above; stray output lines are dropped."""
    records: list[dict[str, Any]] = []
    for chunk in (text or "").split(_LOG_RECORD_SEP):
        body = chunk.strip("\n")
        if not body.strip():
            continue
        header, _, rest = body.partition("\n")
        fields = [part.strip() for part in header.split(_LOG_FIELD_SEP)]
        if len(fields) < 4:
            continue
        records.append({"hash": fields[0], "date": fields[1], "author": fields[2],
                        "subject": fields[3],
                        "files": [ln.strip() for ln in rest.splitlines() if ln.strip()]})
    return records


def git_log_preview(root: str, *, paths: list[str] | None = None,
                    limit: int = _LOG_DEFAULT_LIMIT, since: str = "", until: str = "",
                    author: str = "", at: str = "", files: bool = False,
                    timeout: int = _GIT_TIMEOUT) -> dict[str, Any]:
    """Read-only history: recent commits, commits touching paths, or one commit."""
    result: dict[str, Any] = {
        "ok": False, "root": root, "top": None, "head": "", "entries": [],
        "count": 0, "truncated": False, "error": "", "notes": [],
    }
    root_abs = os.path.normpath(root or "")
    if not root or not os.path.isdir(root_abs):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    top, reason = _repo_top(root_abs)
    if top is None:
        result["error"] = reason
        return result
    result["top"] = top

    values: dict[str, str] = {}
    for field, raw in (("since", since), ("until", until), ("author", author), ("at", at)):
        clean, err = _clean_scalar(raw, field)
        if err:
            result["error"] = err
            return result
        values[field] = clean
    try:
        want = int(limit)
    except (TypeError, ValueError):
        want = _LOG_DEFAULT_LIMIT
    want = max(1, min(want, _LOG_MAX_LIMIT))

    # `at:` 指向单个 revision 时就是「只看这一条」；写成 a..b 区间时才按 limit 展开。
    single_ref = bool(values["at"]) and ".." not in values["at"]
    span = 1 if single_ref else want + 1
    args = ["log", "--no-color", "--date=short", _LOG_PRETTY, "--max-count", str(span)]
    for flag in ("--since", "--until", "--author"):
        value = values[flag[2:]]
        if value:
            args.append(f"{flag}={value}")
    if files:
        args.append("--name-only")
    if values["at"]:
        args.append(values["at"])
    requested = [p for p in (paths or []) if str(p).strip()]
    if requested:
        kept, err = _validate_paths(requested, root_abs, top)
        if kept is None:
            result["error"] = err
            return result
        args += ["--"] + kept

    rep = _run_git(args, cwd=top, timeout=timeout)
    if rep.get("error"):
        result["error"] = f"无法执行 git log：{rep['error']}"
        return result
    output = rep.get("output") or ""
    if rep.get("timed_out"):
        result["error"] = "git log 超时，仓库可能过大；请用 limit: 或 paths: 缩小范围。"
        return result
    if rep.get("exit_code") != 0:
        # 空仓库是合法状态而非错误。不能靠 git 的措辞判断（诊断行会被
        # _strip_git_noise 剥掉），所以直接问 HEAD 存不存在。
        probe = _run_git(["rev-parse", "--verify", "HEAD"], cwd=top)
        if not probe.get("error") and probe.get("exit_code") != 0:
            result["ok"] = True
            result["notes"].append("仓库还没有任何提交（新建仓库，或改动全都未提交）。")
            return result
        detail = (rep.get("raw_output") or output).strip().splitlines()
        result["error"] = "git log 失败：" + (detail[-1][:200] if detail else "无输出")
        return result

    records = _parse_log_records(output)
    result["truncated"] = len(records) > want
    records = records[:want]
    result["entries"] = records
    result["count"] = len(records)
    result["ok"] = True
    if not records:
        result["notes"].append("该范围内没有提交（空仓库、ref 不存在或该路径从未被提交）。")
    if files:
        result["notes"].append("files 来自 --name-only，重命名可能只显示目标路径。")
    head = _run_git(["rev-parse", "--short", "HEAD"], cwd=top)
    if not head.get("error") and head.get("exit_code") == 0:
        line = (head.get("output") or "").strip().splitlines()
        result["head"] = line[0] if line else ""
    return result


def render_git_log(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return "dev_git_log 失败：" + (result.get("error") or "未知错误")
    entries = result.get("entries") or []
    title = "提交流水"
    if result.get("head"):
        title += f"（当前 HEAD={result['head']}）"
    if not entries:
        lines = [title + "：没有记录"]
    else:
        more = "（还有更早的历史，用 limit: 调大或加 paths: 收窄）" if result.get("truncated") else ""
        lines = [f"{title}：最近 {len(entries)} 条{more}"]
        for item in entries:
            lines.append(f"{item['hash']} {item['date']} {item['author']}  {item['subject']}")
            lines += [f"      {name}" for name in item.get("files") or []]
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# symbol reference lookup
# --------------------------------------------------------------------------- #

def _walk_source_files(root: str, scope: list[str] | None = None):
    """Yield source file paths under root (bounded by _MAX_FILES)."""
    root_abs = os.path.normpath(root)
    bases: list[str] = []
    singles: list[str] = []  # scope 直接指到文件时，只看该文件
    if scope:
        for rel in scope:
            cand = rel if os.path.isabs(rel) else os.path.join(root_abs, rel)
            cand = os.path.normpath(cand)
            if cand == root_abs or cand.startswith(root_abs + os.sep):
                (bases if os.path.isdir(cand) else singles).append(cand)
    if not bases and not singles:
        bases = [root_abs]
    seen = 0
    for full in singles:
        if os.path.splitext(full)[1].lower() in _SOURCE_EXTS:
            yield full
    for base in bases:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if os.path.splitext(name)[1].lower() not in _SOURCE_EXTS:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.getsize(full) > _MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                seen += 1
                if seen > _MAX_FILES:
                    return
                yield full


def _read_lines(full: str) -> list[str] | None:
    try:
        with open(full, "rb") as fh:
            sample = fh.read(_MAX_FILE_BYTES)
    except OSError:
        return None
    if not _is_probably_text(sample):
        return None
    try:
        return sample.decode("utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return None


def _py_matches(lines: list[str], symbol: str, rel: str) -> tuple[list[dict], list[dict]]:
    """AST-precise Python lookup: comments and string literals are never matches."""
    defs: list[dict] = []
    refs: list[dict] = []
    src = "\n".join(lines)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return _regex_matches(lines, symbol, rel, lang="python-fallback")
    for node in ast.walk(tree):
        kind = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                kind = "def"
        elif isinstance(node, ast.Name) and node.id == symbol:
            kind = "ref"
        elif isinstance(node, ast.Attribute) and node.attr == symbol:
            kind = "attr"
        elif isinstance(node, ast.alias) and symbol in (node.name.split(".")[-1],
                                                        node.asname or ""):
            kind = "import"
        elif isinstance(node, ast.arg) and node.arg == symbol:
            kind = "param"
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and symbol in node.names:
            kind = "ref"
        if kind is not None:
            line = getattr(node, "lineno", 0) or 0
            code = lines[line - 1].strip() if 1 <= line <= len(lines) else ""
            entry = {"file": rel, "line": line, "code": code, "kind": kind}
            (defs if kind == "def" else refs).append(entry)

    # 字符串/字典键里的同名文本（如 TOOLS 注册表的 "dev_git_diff"）：AST 的 Name
    # 匹配抓不到，但重命名函数时它必须同步改，所以单列一类低置信命中。
    rx = re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")
    seen_lines = {e["line"] for e in defs} | {e["line"] for e in refs}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if not rx.search(node.value):
            continue
        line = getattr(node, "lineno", 0) or 0
        if line in seen_lines:
            continue
        seen_lines.add(line)
        refs.append({"file": rel, "line": line,
                     "code": lines[line - 1].strip() if 1 <= line <= len(lines) else "",
                     "kind": "str"})
    return defs, refs


def _regex_matches(lines: list[str], symbol: str, rel: str,
                   lang: str = "text") -> tuple[list[dict], list[dict]]:
    rx = re.compile(r"(?<![\w$])" + re.escape(symbol) + r"(?![\w$])")
    refs: list[dict] = []
    for i, line in enumerate(lines, 1):
        if not rx.search(line):
            continue
        stripped = line.strip()
        if stripped.startswith(("#", "//", "*", "/*")):
            continue  # comment-only noise
        refs.append({"file": rel, "line": i, "code": stripped, "kind": "ref"})
    return [], refs


def _validate_scope(root: str, scope: list[str] | None) -> tuple[list[str] | None, str]:
    """scope 必须真实存在且落在代码根内——写错时静默退化成全仓扫描会更糟。"""
    if not scope:
        return [], ""
    root_abs = os.path.normpath(root)
    kept: list[str] = []
    for rel in scope:
        cand = os.path.normpath(rel if os.path.isabs(rel) else os.path.join(root_abs, rel))
        if not (cand == root_abs or cand.startswith(root_abs + os.sep)):
            return None, f"拒绝 scope {rel!r}：不在代码根目录内。"
        if not os.path.exists(cand):
            return None, f"scope 不存在：{rel}"
        kept.append(cand)
    return kept, ""


def find_references(root: str, symbol: str, *, scope: list[str] | None = None,
                    kind: str = "all", limit: int = _RENDERED_REFS) -> dict[str, Any]:
    """Locate definitions and references of ``symbol`` with a hard scan budget."""
    result: dict[str, Any] = {
        "ok": False, "symbol": symbol or "", "root": root, "scanned": 0,
        "definitions": [], "references": [], "total": 0, "truncated": False,
        "by_file": {}, "error": "", "notes": [],
    }
    symbol = (symbol or "").strip()
    if not symbol:
        result["error"] = "参数缺失：请提供 symbol: <符号名>。"
        return result
    if not root or not os.path.isdir(root):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = _RENDERED_REFS
    want = (kind or "all").strip().lower()
    if want not in ("all", "def", "ref"):
        want = "all"
    scope, scope_err = _validate_scope(root, scope)
    if scope is None:
        result["error"] = scope_err
        return result

    root_abs = os.path.normpath(root)
    definitions: list[dict] = []
    references: list[dict] = []
    scanned = 0
    hit_limit = False
    ts_files = 0
    ts_notes: list[str] = []
    for full in _walk_source_files(root_abs, scope):
        scanned += 1
        lines = _read_lines(full)
        if lines is None:
            continue
        rel = os.path.relpath(full, root_abs).replace("\\", "/")
        exact = None
        if os.path.splitext(rel)[1].lower() in ts_index.TS_EXTS:
            exact = ts_index.ts_matches(rel, "\n".join(lines), symbol)
        if exact is not None:
            defs, refs, notes = exact
            ts_files += 1
            for note in notes:
                if note not in ts_notes:
                    ts_notes.append(note)
        elif rel.endswith((".py", ".pyi")):
            defs, refs = _py_matches(lines, symbol, rel)
        else:
            defs, refs = _regex_matches(lines, symbol, rel)
        definitions.extend(defs)
        references.extend(refs)
        if len(definitions) + len(references) > limit * 8:
            hit_limit = True
            break

    string_hits = [e for e in references if e.get("kind") == "str"]
    code_refs = [e for e in references if e.get("kind") != "str"]
    by_file: dict[str, int] = {}
    for entry in code_refs:
        by_file[entry["file"]] = by_file.get(entry["file"], 0) + 1
    result.update({
        "ok": True, "scanned": scanned, "definitions": definitions[:limit],
        "references": code_refs[:limit], "string_hits": string_hits[:limit],
        "by_file": by_file, "total": len(definitions) + len(code_refs) + len(string_hits),
        "truncated": hit_limit or len(code_refs) > limit or len(definitions) > limit,
    })
    if want == "def":
        result["references"] = []
        result["string_hits"] = []
    elif want == "ref":
        result["definitions"] = []
    if hit_limit:
        result["notes"].append(
            f"命中过多已提前停止（扫描 {scanned} 个文件）；请用 scope: 限定目录或换更精确的符号名。")
    if not definitions and not references:
        result["notes"].append("未找到任何匹配；确认符号名与拼写，或该符号位于被跳过的构建目录中。")
    if ts_files:
        result["ts_indexed_files"] = ts_files
        result["notes"].append(
            f"{ts_files} 个 TS/JS/Vue 文件按 tree-sitter 语法精确匹配——注释与字符串里的同名文本不算引用。")
        for note in ts_notes:
            if note not in result["notes"]:
                result["notes"].append(note)
    return result


def render_references(result: dict[str, Any]) -> str:
    if result.get("error") and not result.get("ok"):
        return f"dev_find_references 失败：{result['error']}"
    defs = result.get("definitions") or []
    refs = result.get("references") or []
    strs = result.get("string_hits") or []
    lines = [f"=== 符号 {result.get('symbol')} ===",
             f"定义 {len(defs)} 处 / 引用 {len(refs)} 处 / 字符串键 {len(strs)} 处"
             f"（扫描 {result.get('scanned')} 个文件）"]
    if defs:
        lines.append("--- 定义 ---")
        for d in defs:
            lines.append(f"  {d['file']}:L{d['line']}  {d['code']}")
    if refs:
        lines.append("--- 引用 ---")
        shown = 0
        for rel, count in sorted((result.get("by_file") or {}).items(),
                                 key=lambda kv: -kv[1]):
            lines.append(f"  {rel} ({count})")
            for r in refs:
                if r["file"] != rel:
                    continue
                lines.append(f"    L{r['line']}  {r['code']}")
                shown += 1
                if shown >= 60:
                    break
            if shown >= 60:
                lines.append("  …（引用过多，先显示前 60 条；请用 scope: 缩小范围）")
                break
    if strs:
        lines.append("--- 字符串/配置键命中（重命名时也要同步改，低置信）---")
        for e in strs[:20]:
            lines.append(f"  {e['file']}:L{e['line']}  {e['code']}")
        if len(strs) > 20:
            lines.append(f"  …另有 {len(strs) - 20} 处")
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    return "\n".join(lines)
