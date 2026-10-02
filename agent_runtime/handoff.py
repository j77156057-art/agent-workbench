"""把「交付」这段补到能真的往前走一步，而又不越过人的决定。

`dev_propose` 已经能出「补丁 + 草稿」，补的是「改完了却只能口头描述」那段空洞。
但它到**草稿**就停了：产物躺在状态目录里，人还得自己决定分支名、自己写 commit
message、自己 `git add`、自己确认「这些改动里没有混进别人的在途 WIP」。于是闭环的
最后一段仍然是敞开的——交付空洞只是换了个位置。

本模块补的是**决策材料**，不是代替决策：

* **分支名建议**：从改动内容推出一个 Conventional Commits 风格的分支名。
* **commit message 草稿**：scope / type / 正文骨架都填好，人只需要读一遍再确认。
* **范围体检**：`git diff --stat` 的实际形状 + 本仓那条实测教训（`git commit -- <路径>`
  会绕过索引把别人在同一批文件里的在途改动一起扫进去，本仓 `b828d10` 就是这么连累
  HEAD 的）。所以这里明确算出「这个提交会包含哪些文件」，并把共享文件标出来。
* **未跟踪文件清单**：它们不在 diff 里，说成「已交付」是这类工具最容易骗人的地方。

**不做的事**（和仓库同一条纪律）：不 `git add`、不 `git commit`、不 `git push`、
不建分支。这些是人的决定，命令黑名单也拦着。本模块只跑只读 git 子命令
（`rev-parse` / `status` / `diff` / `log`），产物只写进项目状态目录——
多人同仓时把临时文件写进工作树等于给别人埋雷。

所有判定 fail-closed：不是 git 工作树、diff 读不出来、状态解析不出来，都明说
「未完成 + 原因」，绝不回一个空的「一切就绪」。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from agent_runtime import code_intel

#: 一次最多看多少个改动文件。超过这个数说明这一轮改得太宽，
#: 那本身就是要告诉人的一件事（提交应该拆）。
MAX_FILES = 200
#: 一次最多列多少个共享文件（同一个文件里混了多人的改动）。
MAX_SHARED = 40

#: Conventional Commits 的类型白名单。分支名/commit message 的 type 只从这里面取，
#: 推不出来就退回 `chore` 并说明是猜的——不硬编一个不在约定里的词。
TYPES = ("feat", "fix", "refactor", "perf", "test", "docs", "build", "ci",
         "style", "chore", "revert")

#: 目录 → scope 的映射表（取最深匹配的那个）。
SCOPES = (
    ("agent_runtime/", "harness"),
    ("api_routes/", "api"),
    ("frontend/src/ask/", "ask"),
    ("frontend/src/workbench/", "workbench"),
    ("frontend/", "frontend"),
    ("tests/", "tests"),
    ("golden/", "golden"),
    ("docs/", "docs"),
    ("packaging/", "packaging"),
    ("installer/", "installer"),
    ("showcase/", "showcase"),
    ("agent_skills/", "skills"),
)

_WORD_SPLIT = re.compile(r"[^0-9a-zA-Z]+")
#: 中文提交标题：取不到 ASCII 词时用它兜底（ Conventional Commits 的 scope 必须 ASCII）。
_CJK = re.compile(r"[一-鿿]")


def slugify(text: str, fallback: str = "change") -> str:
    """把标题压成一个分支名片段。

    ASCII 词优先（`Fix the thing` → `fix-the-thing`）；标题里没有 ASCII 词时
    （本仓的提交标题大量是中文）退回一个基于内容长度的短标记，而不是返回空串——
    空串会让分支名变成 `feat/` 这种没法看的东西（实测踩到）。
    """
    raw = str(text or "")
    out = _WORD_SPLIT.sub("-", raw.lower()).strip("-")
    out = re.sub(r"-{2,}", "-", out)
    if out:
        return out[:60]
    if _CJK.search(raw):
        # 保留 CJK 字符本身：git 分支名允许它们，`git switch -c` 也能正常用。
        kept = re.sub(r"[^\w\-]+", "", raw, flags=re.UNICODE).strip("-")
        return (kept[:24] or fallback)
    return fallback


def _recent_titles(root: str, limit: int = 12) -> list[str]:
    """看最近提交标题，用来对齐仓库自己的措辞习惯（而不是强推一套外来的）。"""
    rep = code_intel._run_git(["log", "-n", str(max(1, min(limit, 40))),
                               "--pretty=format:%s"], cwd=root)
    if rep.get("error") or rep.get("exit_code") != 0:
        return []
    return [line.strip() for line in (rep.get("output") or "").splitlines() if line.strip()]


def _convention(recent: list[str]) -> str:
    """这仓的提交标题用不用 Conventional Commits？据实说，不替它决定。"""
    typed = sum(1 for title in recent
                if re.match(r"^(%s)(\([^)]*\))?!?:" % "|".join(TYPES), title))
    if recent and typed >= max(1, len(recent) // 2):
        return "conventional"
    if recent:
        return "freeform"
    return "unknown"


def suggest_scope(files: list[str]) -> str:
    """从改动文件推 scope：取命中最多、最深的那个目录约定。"""
    best: tuple[int, str] = (0, "")
    for prefix, scope in SCOPES:
        hits = sum(1 for name in files if name.startswith(prefix))
        if hits > best[0] or (hits == best[0] and hits > 0 and len(prefix) > len(best[1])):
            if hits:
                best = (hits, scope)
    if best[1]:
        return best[1]
    stems = {os.path.splitext(name)[0].split("/")[-1] for name in files if "/" in name}
    if len(stems) == 1 and stems:
        return slugify(sorted(stems)[0], "core")[:20]
    if not files:
        return "core"
    top = files[0].split("/")[0]
    return slugify(top, "core")[:20] or "core"


def suggest_type(files: list[str], title: str = "") -> str:
    """推 commit 的 type。判据按可靠性排序，推不出就明说是 `chore`。"""
    haystack = " ".join(files).lower() + " " + str(title or "").lower()
    if re.search(r"(^|[/_-])(tests?|testing)([/_.-]|$)", haystack):
        return "test"
    if haystack.startswith("docs/") or " docs/" in haystack or ".md" in haystack:
        return "docs"
    words = str(title or "").lower()
    if re.search(r"\b(fix|bug|regression|broken|crash|error|wrong)\b", words) \
            or re.search(r"(修|坏|错|崩)", str(title or "")):
        return "fix"
    if re.search(r"\b(add|implement|support|new)\b", words) or re.search(r"(新增|支持)", str(title or "")):
        return "feat"
    if re.search(r"\b(refactor|rename|extract|simplify)\b", words) or re.search(r"(重构|重命名)", str(title or "")):
        return "refactor"
    if re.search(r"\b(perf|speed|faster|optimi)", words) or re.search(r"(性能|优化)", str(title or "")):
        return "perf"
    if re.search(r"(ci|workflow|github)", haystack):
        return "ci"
    if re.search(r"(build|pyinstaller|spec|packaging)", haystack):
        return "build"
    return "chore"


def analyze(root: str, *, files: list[str] | None = None, title: str = "",
            timeout: int = 30) -> dict[str, Any]:
    """算出交付需要的决策材料。只读 git，绝不 add/commit/push。"""
    result: dict[str, Any] = {
        "ok": False, "error": "", "head": "", "branch_now": "", "branch_base": "",
        "changed": [], "untracked": [], "stats": "", "stat_rows": [],
        "shared_files": [], "convention": "unknown", "recent_titles": [],
        "branch_suggestion": "", "message_suggestion": "", "type": "", "scope": "",
        "title_suggestion": "", "notes": [], "too_broad": False, "generated_at": "",
    }
    if not root or not os.path.isdir(root):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    # 归一成绝对路径：`_validate_paths` 用 `startswith(root + os.sep)` 判越界，
    # 传进来的若是 '.' 这种相对路径，`join('.', 'a/b.py')` 拼出来的是相对串，
    # 跟 '.' 比永远不成立——于是每一个正常路径都被误判成「越界」（实测踩到）。
    root = os.path.abspath(root)
    result["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # `_repo_top` 返回 (路径, 原因) —— 原因不能吞掉：不是 git 工作树与「git 跑不起来」
    # 是两回事，调用方需要原话才知道下一步该做什么。
    top, top_err = code_intel._repo_top(root)
    if not top:
        result["error"] = (top_err or "拿不到仓库根") + \
            "——没有 git 就没有「提交」这一步，本次交付材料未生成。"
        return result
    head = code_intel._run_git(["rev-parse", "--short", "HEAD"], cwd=top)
    result["head"] = ((head.get("output") or "").strip().splitlines() or [""])[0]
    branch = code_intel._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=top)
    result["branch_now"] = ((branch.get("output") or "").strip().splitlines() or [""])[0]

    status = code_intel._run_git(["status", "--porcelain"], cwd=top)
    if status.get("error") or status.get("exit_code") != 0:
        result["error"] = "读不到 git status：%s" % (status.get("error")
                                                    or status.get("output", "")[:120])
        return result
    rows: list[tuple[str, str]] = []
    for line in (status.get("output") or "").splitlines():
        if len(line) < 4:
            continue
        # **按位判断，不要 strip**：porcelain 的两列是「暂存区状态 / 工作区状态」。
        # `line[:2].strip()` 会把 `" M"` 变成 `"M"`、把 `" M"` 和 `"M "` 混成同一个值，
        # 更糟的是未跟踪行 `"??"` strip 后仍是 `"?"` 而已跟踪的 `" M"` strip 后是 `"M"`——
        # 一旦拿 strip 后的值去比 `"??"`，未跟踪文件就会被算进 `changed`，
        # 于是输出会说「这次提交会带上它」，而它根本不在 diff 里（实测踩到）。
        x, y = line[0], line[1]
        name = line[3:].strip().strip('"')
        rows.append((x, y, name))
    changed = sorted({name for x, y, name in rows if x != "?" and y != "?"})
    untracked = sorted({name for x, y, name in rows if x == "?" or y == "?"})
    # git 对**整个未跟踪目录**只报一行并带尾斜杠（`?? artifacts/`）。留着斜杠的话
    # 「它是不是在我关心的范围里」的前缀判断会失效，所以统一去掉，但保留「这是个目录」
    # 这个事实——按目录原样交给人看，比展开成几十个文件名有用。
    untracked = [name.rstrip("/") if not name.endswith("/") else name
                 for name in untracked]
    wanted = [str(item).strip() for item in (files or []) if str(item).strip()]
    if wanted:
        kept, err = code_intel._validate_paths(wanted, root, top)
        if kept is None:
            result["error"] = err
            return result
        scope_set = set(kept)
        changed = [name for name in changed if name in scope_set
                   or any(name.startswith(item.rstrip("/") + "/") for item in scope_set)]
        untracked = [name for name in untracked if name in scope_set
                     or any(name.startswith(item.rstrip("/") + "/") for item in scope_set)]
    if not changed:
        result["untracked"] = untracked[:MAX_FILES]
        if untracked:
            # 只有未跟踪文件时**不能**说「没有可提交的东西」——它们确实是要交付的
            # 内容，只是还没进 git。实测：新建一批模块后跑这条，输出是
            # 「这个范围里没有已跟踪的改动可提交」，读起来像「这轮白干了」，
            # 而实际上 45 个新文件正等着入库。所以单列一条状态，别混进 error。
            result["error"] = (
                "这个范围里**只有未跟踪的新文件**，没有已跟踪文件的改动。"
                "它们确实是要交付的内容，但不在 `git diff` 里——所以本工具算不出"
                "行数规模，也给不出「这次提交会包含哪些文件」。"
                "确认之后用 `git add <路径>` 逐个加入（**不要** `git add -A`），"
                "再跑一次本工具就能拿到完整材料。")
            return result
        result["error"] = ("这个范围里没有已跟踪的改动可提交。未跟踪的新文件不会出现在"
                           "`git diff` 里——要一起交付得先用 dev_patch 把它们写进补丁，"
                           "或者明确确认后再 `git add`。")
        return result

    inventory = code_intel.git_diff_preview(root, paths=files or None, staged=False,
                                            stat_only=True, timeout=timeout)
    result["changed"] = changed[:MAX_FILES]
    result["stats"] = (inventory.get("stat") or "").strip()
    result["untracked"] = untracked[:MAX_FILES]
    result["stat_rows"] = [row for row in (inventory.get("stat") or "").splitlines()
                           if row.strip()][:MAX_FILES]

    # 「共享文件」＝非测试、非文档这类主干文件。它们是本仓实测教训的高发区：
    # `git commit -- <路径>` 会绕过索引，把别人在同一批文件里的在途 WIP 一起扫进去。
    result["shared_files"] = [name for name in changed
                              if not name.startswith("tests/")
                              and not name.endswith(".md")][:MAX_SHARED]

    recent = _recent_titles(root)
    result["recent_titles"] = recent
    result["convention"] = _convention(recent)
    result["scope"] = suggest_scope(result["changed"])
    result["type"] = suggest_type(result["changed"], title)
    # 用户给的标题常常已经自带 `feat: ` / `fix(scope): ` 前缀（人写 commit message
    # 就是这个习惯）。直接再拼一次会得到 `chore(harness): feat: 修好…` 这种叠套，
    # 分支名也会变成 `fix/harness-fix-x`（type 重复）——实测两个都踩到，所以先剥。
    body = re.sub(r"^(%s)(\([^)]*\))?!?:\s*" % "|".join(TYPES), "",
                  str(title or "").strip(), flags=re.I).strip()
    stem = slugify(body) if body else ""
    if not stem:
        # 没给标题就从文件名推一个能读的骨架，而不是把文件名硬拼成一句话。
        stem = slugify("-".join(sorted({os.path.splitext(n)[0].split("/")[-1]
                                        for n in result["changed"][:3]})) or "change")
    result["title_suggestion"] = stem
    prefix = "%s(%s): " % (result["type"], result["scope"]) \
        if result["convention"] == "conventional" else ""
    result["message_suggestion"] = prefix + body
    result["branch_suggestion"] = "%s/%s-%s" % (result["type"], result["scope"], stem)

    result["too_broad"] = len(result["changed"]) > 40
    notes = []
    if result["too_broad"]:
        notes.append("这一轮改了 %d 个文件——通常意味着该拆成几个提交，"
                     "一个 40 文件的提交没人能 review。" % len(result["changed"]))
    if result["untracked"]:
        notes.append("有 %d 个未跟踪文件**不在 diff 里**：它们不会被这次提交带上，"
                     "需要单独确认后再入库。" % len(result["untracked"]))
    if result["shared_files"]:
        notes.append("其中 %d 个是主干共享文件（如 %s）。本仓实测教训："
                     "`git commit -- <路径>` 会绕过索引，把别人在同一批文件里的在途改动"
                     "一起扫进去（b828d10 就是这么连累 HEAD 的）。"
                     "提交前请核对 `git diff --cached --stat`，或先跑 dev_headcheck。"
                     % (len(result["shared_files"]), result["shared_files"][0]))
    if result["convention"] == "freeform":
        notes.append("本仓最近的提交标题不是 Conventional Commits 风格，"
                     "所以建议里没加 type(scope): 前缀——按仓库自己的习惯写更合适。")
    if result["convention"] == "unknown":
        notes.append("读不到本仓的提交历史（可能是刚 init），建议措辞只是骨架。")
    if result["branch_now"] in ("main", "master"):
        notes.append("当前在 %s 上。直接往主干提交还是先开分支，是人的决定——"
                     "本工具只给建议，不切分支。" % result["branch_now"])
    result["notes"] = notes
    result["ok"] = True
    return result


def _lane_notes(root: str) -> list[str]:
    try:
        from agent_runtime import lanes
        view = lanes.status(root)
    except Exception:  # noqa: BLE001 - 交接信息缺了不该挡住产物
        return []
    return ["%s（owner %s）认领 %s" % (row["lane"], row.get("owner") or "未署名",
                                     ", ".join(row.get("paths") or []))
            for row in view.get("live") or []]


def render(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return "dev_handoff 未完成：" + str(result.get("error") or "未知原因")
    lines = ["交付材料（**未 add / 未 commit / 未 push**——那些是人的决定）："]
    lines.append("  当前分支：%s   HEAD=%s" % (result.get("branch_now") or "?",
                                              result.get("head") or "?"))
    lines.append("  建议分支名：%s" % result.get("branch_suggestion"))
    lines.append("  建议 commit message：%s" % result.get("message_suggestion"))
    if result.get("convention") == "conventional":
        lines.append("  （本仓最近提交用 Conventional Commits，所以带上了 type(scope): 前缀）")
    lines.append("  会包含 %d 个已跟踪文件：%s" % (
        len(result.get("changed") or []),
        ", ".join((result.get("changed") or [])[:8])
        + (" …" if len(result.get("changed") or []) > 8 else "")))
    stats = str(result.get("stats") or "").splitlines()
    if stats:
        lines.append("  规模：%s" % stats[-1].strip())
    if result.get("untracked"):
        lines.append("  ⚠ 未跟踪 %d 个（**不会**被这次提交带上，需单独确认）：%s"
                     % (len(result["untracked"]), ", ".join(result["untracked"][:6])))
    for note in result.get("notes") or []:
        lines.append("  提示: " + note)
    lanes = _lane_notes_safe(result)
    if lanes:
        lines.append("  其他 lane 正在动的范围：")
        lines += ["    - " + row for row in lanes[:5]]
    lines.append("下一步（都由你执行）：")
    lines.append("  1. git switch -c %s" % result.get("branch_suggestion"))
    lines.append("  2. 挑改动进暂存区：git add <路径>（**不要**用 `git add -A`，"
                 "也别用 `git commit -- <路径>`）")
    lines.append("  3. git commit（用上面那条建议 message，提交前跑一次 dev_headcheck）")
    return "\n".join(lines)


def _lane_notes_safe(result: dict[str, Any]) -> list[str]:
    root = result.get("_root")
    return _lane_notes(root) if root else []


__all__ = ["MAX_FILES", "MAX_SHARED", "SCOPES", "TYPES", "analyze", "render",
           "slugify", "suggest_scope", "suggest_type"]
