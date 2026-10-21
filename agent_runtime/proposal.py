"""把「改完了」变成可评审的产物：补丁文件 + 提交/PR 草稿。

命令黑名单挡掉了 `git commit/push`（这是有意的：提交与推送是人的决定），但结果
是 Agent 只能口头描述自己改了什么，评审者看不到一份能直接用的东西。本模块补这段
【交付后半环】，并且不放宽任何闸门：

* 只跑只读 git 子命令（rev-parse/status/diff），绝不 add/commit/push；
* 产物只写进**项目状态目录**，不写进仓库工作树——否则 `git status` 会被自己人搞脏，
  在多人同仓里这就是事故（见 lanes.py 同源的教训）；
* 补丁正文用真 `git diff` 输出，可直接喂给 `dev_patch` 重放；未跟踪文件【不伪装成补丁】，
  只列清单——把没入库的东西说成已入库，是这类工具最容易骗人的地方。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from agent_runtime import code_intel

MAX_PATCH_BYTES = 4_000_000
MAX_FILES_LISTED = 200
SLUG = re.compile(r"[^a-z0-9._-]+")


def _repo_top(root: str) -> tuple[str, str]:
    rep = code_intel._run_git(["rev-parse", "--show-toplevel"], cwd=root)
    if rep.get("error") or rep.get("exit_code") != 0:
        return "", "当前代码根目录不是 git 工作树，没法出补丁。"
    line = (rep.get("output") or "").strip().splitlines()
    return (os.path.normpath(line[0]) if line else ""), ""


def _head(top: str) -> str:
    rep = code_intel._run_git(["rev-parse", "--short", "HEAD"], cwd=top)
    return ((rep.get("output") or "").strip().splitlines() or [""])[0]


def _raw_diff(top: str, paths: list[str], staged: bool, timeout: int) -> tuple[str, str]:
    args = ["diff", "--no-color", "--unified=3"]
    if staged:
        args.append("--cached")
    args += ["--"] + paths if paths else []
    rep = code_intel._run_git(args, cwd=top, timeout=timeout)
    if rep.get("error"):
        return "", str(rep["error"])
    if rep.get("exit_code") != 0:
        return "", ((rep.get("raw_output") or rep.get("output") or "").strip()[-200:]
                    or "git diff 退出码 %s" % rep.get("exit_code"))
    body = rep.get("output") or ""
    if len(body.encode("utf-8")) > MAX_PATCH_BYTES:
        return "", "改动超过 %d 字节，请拆分后再生成补丁。" % MAX_PATCH_BYTES
    return body, ""


def slugify(title: str, fallback: str = "proposal") -> str:
    text = SLUG.sub("-", str(title or "").strip().lower()).strip("-")
    return (text[:60] or fallback)


def _untracked(top: str, paths: list[str]) -> list[str]:
    rep = code_intel._run_git(["status", "--porcelain"], cwd=top)
    rows: list[str] = []
    for line in (rep.get("output") or "").splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        if paths and not any(rel.startswith(str(p).rstrip("/") + "/")
                             or rel == str(p).rstrip("/") for p in paths):
            continue
        rows.append(rel)
    return rows


def _lane_notes(root: str) -> list[str]:
    try:
        from agent_runtime import lanes
        view = lanes.status(root)
    except Exception:  # noqa: BLE001 - 交接信息缺了不该挡住产物
        return []
    out = []
    for row in view.get("live") or []:
        out.append("%s（owner %s）认领 %s" % (row["lane"], row.get("owner") or "未署名",
                                          ", ".join(row.get("paths") or [])))
    return out


def build(root: str, *, title: str = "", body: str = "", files: list[str] | None = None,
          staged: bool = False, test_plan: str = "", lane: str = "",
          timeout: int = 30) -> dict[str, Any]:
    """生成补丁 + 草稿并写入项目状态目录。返回两者路径与统计。"""
    result: dict[str, Any] = {"ok": False, "patch": "", "draft": "", "changed": [],
                              "untracked": [], "stats": "", "error": "", "notes": []}
    if not root or not os.path.isdir(root):
        result["error"] = "代码根目录不存在或未配置。"
        return result
    top, err = _repo_top(root)
    if err:
        result["error"] = err
        return result
    requested = [str(p).strip() for p in (files or []) if str(p).strip()]
    if requested:
        kept, perr = code_intel._validate_paths(requested, root, top)
        if kept is None:
            result["error"] = perr
            return result
        requested = kept
    diff, derr = _raw_diff(top, requested, staged, timeout)
    if derr:
        result["error"] = "取差异失败：" + derr
        return result
    if not diff.strip():
        result["error"] = ("没有可打包的%s改动。未跟踪的新文件不会出现在 diff 里，"
                           "要一起交付请在 files: 里显式给出目录，或用 dev_patch 先把新文件写进补丁。"
                           % ("暂存区" if staged else "未暂存"))
        return result
    inventory = code_intel.git_diff_preview(root, paths=files or None, staged=staged,
                                           stat_only=True, timeout=timeout)
    changed = [str(row) for row in (inventory.get("staged_files") if staged
                                    else inventory.get("unstaged_files")) or []]
    result["changed"] = changed[:MAX_FILES_LISTED]
    result["stats"] = (inventory.get("stat") or "").strip()
    result["untracked"] = _untracked(top, requested)[:MAX_FILES_LISTED]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = slugify(title or (changed[0] if changed else "proposal"))
    patch_name = "proposals/%s-%s.patch" % (name, stamp)
    draft_name = "proposals/%s-%s.md" % (name, stamp)
    patch_path = Path(os.path.abspath(str(code_intel_path_root(root, patch_name))))
    draft_path = patch_path.with_suffix(".md")
    header = ("# 由 Agent 工作台 dev_propose 生成（未提交、未推送）\n"
              "# base HEAD=%s；产物只写进项目状态目录，仓库工作树未被改动。\n\n" % (_head(top) or "未知"))
    lines = ["# %s" % (title or "本轮改动提案"), ""]
    if lane:
        lines += ["署名 lane：%s（产物未提交、未推送）" % lane, ""]
    if body.strip():
        lines += [body.strip(), ""]
    lines += ["## 改动文件（%s%d 个）" % ("已暂存 " if staged else "未暂存 ", len(changed)),
              ""]
    lines += ["- %s" % row for row in result["changed"]]
    if result["untracked"]:
        lines += ["", "## 未跟踪文件（**不在补丁里**，需确认后再入库）", ""]
        lines += ["- %s" % row for row in result["untracked"]]
    if test_plan.strip():
        lines += ["", "## 测试计划", "", test_plan.strip()]
    notes = _lane_notes(root)
    if notes:
        lines += ["", "## 其他 lane 正在动的范围", ""] + ["- " + row for row in notes]
    lines += ["", "## 复核方式", "",
              "1. `git apply --check %s`（或在本仓库用 dev_patch 试打）" % patch_path.name,
              "2. 逐文件对照 `git diff`；补丁是 %s正文，未做模糊匹配"
              % ("暂存区 " if staged else "工作树 "),
              "3. 提交与推送由人执行：本工具不跑 add/commit/push。"]
    try:
        patch_path.write_text(header + diff if diff.endswith("\n") else header + diff + "\n",
                              encoding="utf-8", newline="\n")
        draft_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        result["error"] = "写产物失败：%s" % exc
        return result
    result.update({"ok": True, "patch": str(patch_path), "draft": str(draft_path),
                   "head": _head(top)})
    if lane:
        result["notes"].append("署名 lane：%s（补丁未提交，交接时请带上产物路径）" % lane)
    result["notes"].append("产物在仓库外的项目状态目录，`git status` 不会因此变脏。")
    return result


def code_intel_path_root(root: str, name: str) -> str:
    """状态目录里的产物路径（复用 project_state 的越界/符号链接校验）。"""
    import project_state
    return project_state.path(root, name)


def render(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return "dev_propose 未完成：" + (result.get("error") or "未知原因")
    lines = ["提案已生成（未提交、未推送）：",
             "  补丁：%s" % result.get("patch"),
             "  草稿：%s" % result.get("draft"),
             "  基线 HEAD=%s；%d 个文件有改动" % (result.get("head") or "?",
                                          len(result.get("changed") or []))]
    if result.get("untracked"):
        lines.append("  未跟踪 %d 个（不在补丁里）：%s"
                     % (len(result["untracked"]), ", ".join(result["untracked"][:6])))
    if result.get("stats"):
        lines.append("  统计：" + str(result["stats"]).splitlines()[-1].strip())
    lines += ["提示: " + note for note in result.get("notes") or []]
    return "\n".join(lines)
