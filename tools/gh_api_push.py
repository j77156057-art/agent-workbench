"""绕过被墙的 github.com，直接把本地领先的 commit 推到 GitHub。

## 为什么需要这个

`git push` 走的是 `github.com:443`。在墙内那台机器上这个口被 SNI 阻断：
`curl https://github.com` 恒 `000`、`git push` 恒 `Could not connect`，连用
`--resolve` 指定真实 IP 也无效（阻断在 SNI，不在 DNS）。

但 **`api.github.com` 是通的（200）**，而且本机 `gh` 通常已登录、token 带
`repo` 权限。GitHub 的 **Git Data API**（建 blob / tree / commit、更新 ref）
全部由 api.github.com 提供 —— 于是可以把「要推的 commit」逐个重放成服务端对象，
最后一次 `PATCH` 把 `refs/heads/<branch>` 指上去，全程不碰 github.com。

SSH（github.com:22）虽然常常能连，但要账号里有一台机器认识的 key；`gh ssh-key add`
又需要 `admin:public_key` scope（一般没有，补 scope 要开 github.com 登录页 —— 同样被墙）。
所以本脚本走的是「api.github.com + 已有 gh token」这条不需要额外权限的路。

## 用法

    # 预览：只看会推哪些 commit，不写远端
    python tools/gh_api_push.py --dry-run

    # 真推（当前分支 -> 同名远端分支）
    python tools/gh_api_push.py

    # 指定分支 / 远端名
    python tools/gh_api_push.py --branch main --remote origin

依赖：`gh` 已 `gh auth login`（token 需含 `repo`）、仓库里有 `requests`。

## ⚠ 三个必须知道的边界

1. **远端 commit 的 SHA 会和本地不一样。** Git Data API 不允许设 committer，
   服务端会把 committer 写成当前 gh 用户 + 当前时间，于是 hash 必然变。
   但 **tree（=文件内容）逐字节相同**，脚本末尾会拿「本地 HEAD 的 tree」和
   「服务端返回的 tree」对比来证明内容一致。事后 github.com 直连恢复时若想让
   本地 hash 对齐远端，跑 `git fetch && git reset --hard origin/<branch>`
   （内容相同，不丢东西）。在那之前别再直接 `git push`，会因 hash 分叉被拒。
   **但本脚本本身还能继续用**：若服务端 tip 这个对象本地没有（正是上次重放的后果），
   它会自动按「服务端当前 tree」在本地反查内容一致的那个 commit 当 diff base，
   新 commit 仍挂到真实的服务端 tip 上（内容一致 → 依旧 fast-forward）。也就是说
   「连着用本脚本推」不需要先 fetch；只是原生 `git push` 依旧推不动。

2. **只支持 fast-forward。** 脚本用服务端的**真实 tip**当 base（不是本地那个可能
   过期的 `origin/<branch>` —— 拿错的 base 会报 422 `not a fast forward`）。
   若远端已走到本地没有的、且不是本地 HEAD 祖先的位置（真分叉），直接中止，
   让你先 fetch/rebase，而不是强推覆盖别人。

3. **前提：base 对象本地可得。** `git rev-list <server_tip>..HEAD` 需要服务端 tip
   这个 commit 对象在本地存在（正常「本地领先」时都成立）。若本地没有（远端有
   全新提交），会提示你先 `git fetch` —— 那步需要 github.com 通。
"""

from __future__ import annotations

import argparse
import base64
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

import requests  # noqa: E402

API = "https://api.github.com"
NULL_SHA = "0" * 40
REPO = str(ensure_on_path())  # 仓库根；所有 git 调用都 -C 到它，与调用者 cwd 无关


def run(*args: str) -> str:
    """跑 git，失败即退出并打印 stderr。"""
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=False)
    if r.returncode != 0:
        raise SystemExit("git %s 失败:\n%s" % (" ".join(args),
                          r.stderr.decode("utf-8", "replace")))
    return r.stdout.decode("utf-8", "replace")


def owner_repo(remote: str) -> tuple[str, str]:
    """从 remote URL 解析 OWNER/NAME，支持 https 与 ssh 两种写法。"""
    url = run("remote", "get-url", remote).strip()
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not m:
        raise SystemExit("无法从 remote '%s' 解析 github OWNER/NAME: %s" % (remote, url))
    return m.group(1), m.group(2)


def gh_token() -> str:
    r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise SystemExit("拿不到 gh token —— 先 `gh auth login`（token 需含 repo scope）。")
    return r.stdout.strip()


class Github:
    def __init__(self, token: str, owner: str, name: str):
        self.owner, self.name = owner, name
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": "token " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })

    def _url(self, *parts: str) -> str:
        return "/".join([API, "repos", self.owner, self.name, *parts])

    def ref(self, branch: str) -> str:
        r = self.s.get(self._url("git", "ref", "heads", branch))
        if r.status_code != 200:
            raise SystemExit("查远端分支 %s 失败 %s: %s" % (branch, r.status_code, r.text[:300]))
        return r.json()["object"]["sha"]

    def blob(self, sha: str, raw: bytes) -> None:
        if sha == NULL_SHA:
            return
        r = self.s.post(self._url("git", "blobs"),
                        json={"content": base64.b64encode(raw).decode(), "encoding": "base64"})
        # 已存在返回 201/422；422 且提示 already exists 视为成功
        if r.status_code not in (201,) and "already exists" not in r.text:
            raise SystemExit("上传 blob %s 失败 %s: %s" % (sha[:8], r.status_code, r.text[:300]))

    def tree(self, base_tree: str, entries: list[dict]) -> str:
        r = self.s.post(self._url("git", "trees"), json={"base_tree": base_tree, "tree": entries})
        if r.status_code not in (200, 201):
            raise SystemExit("建 tree 失败 %s: %s" % (r.status_code, r.text[:400]))
        return r.json()["sha"]

    def commit(self, message: str, tree: str, parents: list[str],
               author: dict) -> str:
        body = {"message": message, "tree": tree, "parents": parents, "author": author}
        r = self.s.post(self._url("git", "commits"), json=body)
        if r.status_code not in (200, 201):
            raise SystemExit("建 commit 失败 %s: %s" % (r.status_code, r.text[:400]))
        return r.json()["sha"]

    def update_ref(self, branch: str, sha: str) -> None:
        r = self.s.patch(self._url("git", "refs", "heads", branch),
                         json={"sha": sha, "force": False})
        if r.status_code != 200:
            raise SystemExit("更新 refs/heads/%s 失败 %s: %s\n"
                             "（多半是远端已走到非本地 HEAD 祖先的位置——真分叉，"
                             "先 fetch/rebase，别强推）" % (branch, r.status_code, r.text[:400]))

    def commit_tree(self, branch: str) -> str:
        """取该分支当前 commit 的 tree sha（服务端 tip 对象本地不可得时用来锚定）。"""
        r = self.s.get(self._url("commits", branch))
        if r.status_code != 200:
            raise SystemExit("查远端 %s 的 tree 失败 %s: %s" % (branch, r.status_code, r.text[:300]))
        return r.json()["commit"]["tree"]["sha"]


def commit_meta(sha: str) -> dict:
    line = run("show", "-s", "--format=%H%x00%P%x00%T%x00%aI%x00%an%x00%ae", sha).strip()
    h, parents, tree, date, an, ae = line.split("\x00")
    return {"sha": h, "parents": parents.split(), "tree": tree,
            "date": date, "author": {"name": an, "email": ae, "date": date},
            "message": run("show", "-s", "--format=%B", sha)}


def local_has(rev: str) -> bool:
    """本地是否能解析这个对象（cat-file -e，不抛异常）。"""
    r = subprocess.run(["git", "-C", REPO, "cat-file", "-e", rev],
                       capture_output=True)
    return r.returncode == 0


def find_local_commit_with_tree(head: str, tree: str) -> str | None:
    """在 head 的祖先里找**最新**一个 tree 内容等于 tree 的 commit。

    用于「服务端 tip 对象本地不可得、但内容和本地某 commit 一致」的锚定：
    API 重放只改 committer → hash 变、tree 不变，所以按 tree 反查本地对应 commit。
    """
    for sha in run("rev-list", head).split():
        if run("rev-parse", "%s^{tree}" % sha).strip() == tree:
            return sha
    return None


def replay_one(gh: Github, meta: dict, local_parent: str, server_parent: str,
               base_tree: str, uploaded: set[str]) -> tuple[str, str]:
    """把一个本地 commit 重放到服务端。返回 (server_commit_sha, server_tree_sha)。

    diff 用**本地**父子（local_parent），而建 commit 的 parents 用**服务端** sha
    （server_parent）—— 多 commit 重放时两者会分叉（服务端 hash 和本地不同）。
    """
    raw = run("diff-tree", "-r", "--no-renames", "--raw", local_parent, meta["sha"]).strip()
    entries: list[dict] = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        meta_field, path = ln.split("\t", 1)
        oldmode, newmode, _oldsha, newsha, status = meta_field.split()[:5]
        if status == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        sha = run("rev-parse", "%s:%s" % (meta["sha"], path)).strip()
        if sha not in uploaded:
            blob_raw = subprocess.run(
                ["git", "-C", REPO, "cat-file", "blob", "%s:%s" % (meta["sha"], path)],
                capture_output=True).stdout
            gh.blob(sha, blob_raw)
            uploaded.add(sha)
        mode = newmode if newmode != "000000" else "100644"
        type_ = "commit" if mode == "160000" else "blob"
        entries.append({"path": path, "mode": mode, "type": type_, "sha": sha})
    tree = gh.tree(base_tree, entries)
    csha = gh.commit(meta["message"], tree, [server_parent], meta["author"])
    return csha, tree


def main() -> None:
    ap = argparse.ArgumentParser(description="绕过被墙的 github.com，用 api.github.com 推 commit")
    ap.add_argument("--branch", default=None, help="要推的分支（默认当前分支）")
    ap.add_argument("--remote", default="origin", help="远端名（默认 origin）")
    ap.add_argument("--dry-run", action="store_true", help="只预览要推的 commit，不写远端")
    ap.add_argument("--base", default=None,
                    help="覆盖「服务端 tip」的本地 ref/sha（离线 --dry-run 预览或多分支高级用法）；"
                         "默认用 api 查到的服务端真实 tip")
    args = ap.parse_args()

    branch = args.branch or run("rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch in ("HEAD", ""):
        raise SystemExit("当前处于游离 HEAD，请用 --branch 指定分支名。")
    local_head = run("rev-parse", "HEAD").strip()

    owner, name = owner_repo(args.remote)
    print("仓库: %s/%s   分支: %s" % (owner, name, branch))

    offline_preview = bool(args.base) and args.dry_run
    gh = None if offline_preview else Github(gh_token(), owner, name)

    if args.base:
        server_tip = run("rev-parse", "%s^{commit}" % args.base).strip()
        print("指定 base: %s（离线预览，未查服务端）" % server_tip[:8])
    else:
        server_tip = gh.ref(branch)
        print("服务端真实 tip: %s（本地 origin/%s 可能是过期的，以服务端为准）"
              % (server_tip[:8], branch))

    if server_tip == local_head:
        print("已经同步，无内容可推。")
        return

    # 确定 diff_base（本地可得，用于列待推 + 算改动）。server_tip 本地可得时直接用它；
    # 否则（典型：本脚本上次推过，远端 tip 的 hash 因 committer 变了、对象本地没有）
    # 按「服务端当前 tree」在本地反查内容一致的 commit 当 diff_base，而 commit 的
    # parents 仍挂到真实 server_tip —— 因为 tree 相同，新 commit 依然是 fast-forward。
    if local_has("%s^{commit}" % server_tip):
        diff_base = server_tip
    elif args.dry_run:
        raise SystemExit("服务端 tip %s 本地不可得；离线 --dry-run 请用 --base 指定本地 base，"
                         "或去掉 --dry-run 让脚本走 tree 锚定。" % server_tip[:8])
    else:
        server_tree = gh.commit_tree(branch)
        anchor = find_local_commit_with_tree(local_head, server_tree)
        if not anchor:
            raise SystemExit("服务端 tip %s 本地不可得，且本地没有 tree 相同的 commit 可锚定"
                             "（远端可能有本地完全没有的新提交）。请先 `git fetch %s`。"
                             % (server_tip[:8], args.remote))
        diff_base = anchor
        print("服务端 tip 本地不可得，按内容锚定：本地 %s（tree==服务端）作 diff_base；"
              "新 commit 仍挂到服务端真实父 %s" % (diff_base[:8], server_tip[:8]))

    # 仅支持 fast-forward：diff_base 必须是 local_head 的祖先
    ff = subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", diff_base, local_head])
    if ff.returncode != 0:
        raise SystemExit("真分叉：%s 不是本地 HEAD 的祖先，本脚本只做 fast-forward。\n"
                         "请先 rebase/merge 本地到远端之上再推。" % diff_base[:8])

    to_push = run("rev-list", "--reverse", "%s..%s" % (diff_base, local_head)).split()
    print("待推 %d 个 commit:" % len(to_push))
    for s in to_push:
        print("  %s  %s" % (s[:8], run("show", "-s", "--format=%s", s).strip()))

    if args.dry_run:
        print("\n[dry-run] 未做任何写入。")
        return

    uploaded: set[str] = set()
    local_parent = diff_base    # 首个新 commit 的本地父
    server_parent = server_tip  # 首个新 commit 挂到服务端真实 tip（保证 fast-forward）
    base_tree = run("rev-parse", "%s^{tree}" % diff_base).strip()  # 服务端也持有这棵 tree
    final = server_tip
    for s in to_push:
        meta = commit_meta(s)
        final, base_tree = replay_one(gh, meta, local_parent, server_parent, base_tree, uploaded)
        local_parent = s        # 下一个 commit 的 diff 基于本地父子
        server_parent = final   # 下一个 commit 挂在服务端新建链上
        print("  重放 %s -> %s" % (s[:8], final[:8]))

    gh.update_ref(branch, final)
    local_tree = run("rev-parse", "HEAD^{tree}").strip()
    print("\nrefs/heads/%s -> %s" % (branch, final))
    if base_tree == local_tree:
        print("内容一致：服务端 tree == 本地 HEAD tree（%s）。" % local_tree[:8])
    else:
        print("⚠ tree 不一致！本地=%s 服务端=%s —— 请核对。" % (local_tree, base_tree))
    print("提醒：远端 commit hash(%s) 与本地(%s) 不同属正常（API 无法保留 committer），"
          "内容已逐字节一致。" % (final[:8], local_head[:8]))


if __name__ == "__main__":
    main()
