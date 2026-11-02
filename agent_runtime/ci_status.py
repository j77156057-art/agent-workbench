"""读 CI 状态：把 Actions 的结论变成 Agent 能行动的证据，而不是「我推了，祝好运」。

默认【只读】：只 GET runs / jobs / 失败日志，绝不触发或重跑 workflow（那是会花别人的
钱、改别人仓库的动作）。仓库与远端信息直接读 `.git/config`，不调 git 子命令也不猜。

可注入 transport：这个沙箱连不上 github.com:443（`git push` 实测被拒），所以判定
逻辑用假传输单测覆盖，真联调留到能出网的用户机器上——工具在这种情况下必须说
「连不上」而不是编一个状态，这条也是用例钉住的行为。
"""
from __future__ import annotations

import configparser
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

API_ROOT = "https://api.github.com"
TOKEN_ENV = ("DOCMIND_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
MAX_LOG_CHARS = 8000
_ERROR_LINE = re.compile(r"(?:^|\s)(?:Error:|error TS\d+|FAILED|fatal:)(.{0,200})")


def read_transport(url: str, headers: dict[str, str],
                   timeout: float = 15.0) -> tuple[int, bytes]:
    """默认传输：真实 HTTPS GET。返回 (状态码, 正文)。"""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status or 200), response.read()
    except urllib.error.HTTPError as exc:      # 4xx/5xx 也是有内容的答案
        return int(exc.code), exc.read() or b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ConnectionError("连不上 %s：%s" % (url.split("/")[2], str(exc)[:160])) from exc


def _git_paths(root: str) -> tuple[str, str, str]:
    """(gitdir, commondir, 错误)。`gitdir` 是本工作树自己的元数据目录（worktree 各不相同），
    `commondir` 是共享的那份（remote、hooks 等在这里）。`.git` 是文件时按 gitdir: 指针解析。
    """
    if not root or not os.path.isdir(root):
        return "", "", "代码根目录不存在或未配置。"
    pointer = os.path.join(root, ".git")
    gitdir = pointer
    if os.path.isfile(pointer):
        try:
            with open(pointer, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            return "", "", "读不到 .git 指针：%s" % exc
        line = next((row for row in text.splitlines() if row.startswith("gitdir:")), "")
        target = line.split(":", 1)[1].strip() if line else ""
        if not target:
            return "", "", ".git 指针里没有 gitdir。"
        gitdir = target if os.path.isabs(target) else os.path.normpath(
            os.path.join(root, target))
    if not os.path.isdir(gitdir):
        return "", "", "找不到 git 元数据目录：%s" % gitdir
    common = gitdir
    commondir_file = os.path.join(gitdir, "commondir")
    if os.path.isfile(commondir_file):
        try:
            with open(commondir_file, encoding="utf-8") as handle:
                rel = handle.read().strip()
        except OSError:
            rel = ""
        if rel:
            common = rel if os.path.isabs(rel) else os.path.normpath(
                os.path.join(gitdir, rel))
    return gitdir, common, ""


def repo_slug(root: str) -> tuple[str, str, str]:
    """从 git config 的 origin 里解析 owner/repo（不猜、不调 git 子命令）。"""
    gitdir, common, err = _git_paths(root)
    if err:
        return "", "", err
    # remote 定义在共享 config 里；worktree 自己的目录通常没有 config
    config_path = os.path.join(common, "config")
    if not os.path.isfile(config_path):
        config_path = os.path.join(gitdir, "config")
    if not os.path.isfile(config_path):
        return "", "", "找不到 git config：%s" % config_path
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path, encoding="utf-8")
    except (configparser.Error, UnicodeError) as exc:
        return "", "", "git config 解析失败：%s" % exc
    sections = parser.sections()
    origin = parser.get('remote "origin"', "url", fallback="")
    if not origin:
        urls = [parser.get(section, "url", fallback="") for section in sections
                if section.startswith('remote "')]
        urls = [row for row in urls if row]
        if len(set(urls)) != 1:
            return "", "", "没有 origin remote，且 remote 不唯一，无法确定仓库。"
        origin = urls[0]
    return parse_remote_url(origin)


def parse_remote_url(url: str) -> tuple[str, str, str]:
    """支持 git@host:owner/repo(.git) 与 https://host/owner/repo(.git)。"""
    text = str(url or "").strip()
    if not text:
        return "", "", "remote url 为空。"
    match = re.match(r"^[^@/]+@[^:]*:(?P<path>[^/].*?)\/(?P<repo>[^/]+?)(?:\.git)?$", text)
    if not match:
        match2 = re.match(r"^[a-z]+://(?:[^@/]+@)?[^/]+/(?P<path>.+?)/(?P<repo>[^/]+?)(?:\.git)?$",
                          text)
        match = match2
    if not match:
        return "", "", "认不出仓库归属：%s" % text[:120]
    owner = match.group("path").split("/")[-1]
    return owner, match.group("repo"), ""


def token(*, env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    for name in TOKEN_ENV:
        value = str(source.get(name) or "").strip()
        if value:
            return value
    return ""


def headers(*, tok: str = "") -> dict[str, str]:
    out = {"Accept": "application/vnd.github+json", "User-Agent": "docmind-harness"}
    if tok:
        out["Authorization"] = "Bearer " + tok
    return out


def _get_json(url: str, *, transport: Callable[..., tuple[int, bytes]], tok: str,
              timeout: float) -> tuple[Any, str]:
    try:
        status, body = transport(url, headers(tok=tok), timeout)
    except ConnectionError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001
        return None, "请求失败：%s" % str(exc)[:160]
    if status == 401 or status == 403:
        return None, ("GitHub 拒绝授权（HTTP %d）：检查 token 是否有效、是否有 actions 读取权限。"
                      % status)
    if status == 404:
        return None, "仓库或资源找不到（HTTP 404）：私有仓库需要带权限的 token。"
    if status >= 400:
        return None, "GitHub 返回 HTTP %d" % status
    try:
        return json.loads(body.decode("utf-8", "replace")), ""
    except ValueError:
        return None, "返回不是合法 JSON（HTTP %d）" % status


def current_head(root: str) -> str:
    """当前提交（用来判断 CI 跑的是不是本地这份代码）。直接读 .git，不调子命令。"""
    gitdir, common, err = _git_paths(root)
    if err:
        return ""
    try:
        with open(os.path.join(gitdir, "HEAD"), encoding="utf-8") as handle:
            text = handle.read().strip()
    except OSError:
        return ""
    if not text.startswith("ref:"):
        return text[:40]
    ref = text.split(" ", 1)[1].strip()
    for base in (gitdir, common):
        direct = os.path.join(base, *ref.split("/"))
        if os.path.isfile(direct):
            try:
                with open(direct, encoding="utf-8") as handle:
                    return handle.read().strip()[:40]
            except OSError:
                return ""
        packed = os.path.join(base, "packed-refs")
        if os.path.isfile(packed):
            try:
                with open(packed, encoding="utf-8") as handle:
                    for line in handle:
                        if line.strip().endswith(" " + ref):
                            return line.strip().split(" ")[0][:40]
            except OSError:
                return ""
    return ""


def fetch(root: str, *, ref: str = "", branch: str = "", per_page: int = 5,
          transport: Callable[..., tuple[int, bytes]] | None = None,
          tok: str | None = None, timeout: float = 15.0,
          fetch_jobs: bool = False) -> dict[str, Any]:
    """读最近的 workflow runs；`branch`/`ref` 可收窄，默认看当前分支。"""
    out: dict[str, Any] = {"ok": False, "owner": "", "repo": "", "head": "",
                           "runs": [], "error": "", "notes": []}
    owner, repo, err = repo_slug(root)
    if err:
        out["error"] = err
        return out
    out.update({"owner": owner, "repo": repo})
    out["head"] = current_head(root)
    if not out["head"]:
        out["notes"].append("读不到本地 HEAD（空仓库或未提交？）——无法判断 CI 跑的是不是这份代码。")
    carry = token() if tok is None else str(tok or "")
    if not carry:
        out["error"] = ("没有 GitHub token（%s 任一即可）。私有仓库或限流前的稳定读取都需要它；"
                       "不设 DOCMIND_OFFLINE_CI 时不要指望匿名访问。" % " / ".join(TOKEN_ENV))
        return out
    call = transport or read_transport
    page = max(1, min(int(per_page or 5), 20))
    query = ["per_page=%d" % page]
    if branch:
        query.append("branch=" + urllib.parse.quote(branch, safe=""))
    if ref:
        query.append("head_sha=" + urllib.parse.quote(ref, safe=""))
    url = "%s/repos/%s/%s/actions/runs?%s" % (API_ROOT, owner, repo, "&".join(query))
    payload, err = _get_json(url, transport=call, tok=carry, timeout=timeout)
    if err:
        out["error"] = err
        return out
    rows = (payload or {}).get("workflow_runs") or []
    for row in rows[:page]:
        item = {"id": row.get("id"), "name": row.get("name"),
                "status": row.get("status"), "conclusion": row.get("conclusion"),
                "head_sha": str(row.get("head_sha") or "")[:12],
                "branch": row.get("head_branch"), "event": row.get("event"),
                "html_url": row.get("html_url"), "updated": row.get("updated_at"),
                "jobs_url": row.get("jobs_url")}
        out["runs"].append(item)
    if out["head"] and not any(str(row.get("head_sha") or "") == out["head"][:12]
                               for row in out["runs"]):
        out["notes"].append("最近这些 run 里没有本地 HEAD=%s 的记录——很可能还没 push，"
                           "CI 跑的不是这份代码。" % (out["head"][:12] or "?"))
    if fetch_jobs and out["runs"]:
        target = out["runs"][0]
        if target.get("jobs_url"):
            jobs, jerr = _get_json(str(target["jobs_url"]), transport=call, tok=carry,
                                   timeout=timeout)
            if jerr:
                out["notes"].append("读 job 列表失败：" + jerr)
            else:
                target["jobs"] = [{"name": row.get("name"), "status": row.get("status"),
                                   "conclusion": row.get("conclusion")}
                                  for row in (jobs or {}).get("jobs") or []]
    out["ok"] = True
    return out


def failed_log(url: str, *, transport: Callable[..., tuple[int, bytes]] | None = None,
               tok: str = "", timeout: float = 20.0) -> dict[str, Any]:
    """抓一份日志并只回错误相关行（截断），供「按日志修」用。"""
    out: dict[str, Any] = {"ok": False, "errors": [], "error": "", "truncated": False}
    call = transport or read_transport
    try:
        status, body = call(url, headers(tok=tok), timeout)
    except ConnectionError as exc:
        out["error"] = str(exc)
        return out
    if status >= 400:
        out["error"] = "下载日志失败：HTTP %d" % status
        return out
    text = body.decode("utf-8", "replace")
    lines = [row.strip()[:200] for row in text.splitlines() if _ERROR_LINE.search(row)]
    out["errors"] = lines[:40]
    out["truncated"] = len(text) > MAX_LOG_CHARS or len(lines) > 40
    out["ok"] = True
    return out


def summarize(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return "dev_ci_status 未完成：" + (result.get("error") or "未知原因")
    runs = result.get("runs") or []
    head = str(result.get("head") or "")[:12]
    if not runs:
        lines = ["%s/%s：没查到匹配的 workflow run。" % (result["owner"], result["repo"])]
    else:
        lines = ["%s/%s，本地 HEAD=%s；最近 %d 次 run：" % (
            result["owner"], result["repo"], head or "?", len(runs))]
        for row in runs:
            mark = "仍在跑" if row.get("status") != "completed" else (
                "通过" if row.get("conclusion") == "success"
                else "结果=%s" % (row.get("conclusion") or "?"))
            lines.append("  %-11s %s  %s [%s] %s" % (str(row.get("head_sha") or "")[:9],
                                              mark, row.get("name") or "?",
                                              row.get("branch") or "?", row.get("html_url") or ""))
            for job in row.get("jobs") or []:
                if job.get("conclusion") not in (None, "success"):
                    lines.append("      失败 job：%s（%s）" % (job.get("name"), job.get("conclusion")))
    lines += ["提示: " + note for note in result.get("notes") or []]
    return "\n".join(lines)
