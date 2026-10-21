"""Wave12 回归：dev_propose（交付产物）与 dev_ci_status（只读 CI）。

两件事必须钉住：① propose 不许把仓库弄脏、不许把未跟踪文件伪装成已入库的补丁；
② ci_status 在没 token / 被拒 / 连不上的时候必须说「未完成 + 原因」，绝不能编一个
状态出来——本沙箱就是连不上 github，所以判定逻辑用假传输全量覆盖，真联调留给用户机器。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import ci_status, patch_apply, proposal  # noqa: E402


def _git_ok():
    try:
        return subprocess.run(["git", "--version"], capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


GIT_OK = _git_ok()


class _RepoCase(unittest.TestCase):
    """一个临时 git 仓库 + 隔离的项目状态目录。"""

    def setUp(self):
        self._prev_root = config.get_runtime("code_root")
        self._prev_state = config.STATE_ROOT
        self._prev_tokens = {k: os.environ.get(k) for k in ci_status.TOKEN_ENV}
        self.tmp = tempfile.mkdtemp(prefix="docmind_propose_")
        self.state = tempfile.mkdtemp(prefix="docmind_propose_state_")
        config.set_runtime("code_root", self.tmp)
        config.STATE_ROOT = self.state
        for key in ci_status.TOKEN_ENV:
            os.environ.pop(key, None)

    def tearDown(self):
        config.set_runtime("code_root", self._prev_root)
        config.STATE_ROOT = self._prev_state
        for key, value in self._prev_tokens.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    def git(self, *args):
        proc = subprocess.run(["git", *args], cwd=self.tmp, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def write(self, rel, content):
        full = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(full) or self.tmp, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)

    def init_repo(self, origin="https://github.com/acme/agent-workbench.git"):
        self.git("init", "-q")
        self.git("config", "user.email", "e@example.com")
        self.git("config", "user.name", "E")
        if origin:
            self.git("remote", "add", "origin", origin)
        self.write("a.py", "x = 1\n")
        self.write("keep.py", "y = 2\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def porcelain(self):
        return self.git("status", "--porcelain").strip()


class ProposalCases(_RepoCase):
    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_build_writes_artifacts_outside_the_repo(self):
        self.init_repo()
        self.write("a.py", "x = 2\n")
        before = self.porcelain()
        res = proposal.build(self.tmp, title="fix: a 改成 2", body="为了复现简单",
                             test_plan="pytest -k a", lane="AI-A")
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(self.porcelain(), before, "打包不许把仓库弄脏")
        for path in (res["patch"], res["draft"]):
            self.assertFalse(os.path.normpath(path).startswith(os.path.normpath(self.tmp)),
                             "产物必须在仓库外：%s" % path)
            self.assertTrue(os.path.isfile(path))
        patch = open(res["patch"], encoding="utf-8").read()
        draft = open(res["draft"], encoding="utf-8").read()
        self.assertIn("@@", patch)
        self.assertIn("+x = 2", patch)
        self.assertIn("未提交、未推送", patch)
        self.assertIn("## 测试计划", draft)
        self.assertIn("AI-A", draft)

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_untracked_files_are_listed_not_faked_into_the_patch(self):
        self.init_repo()
        self.write("a.py", "x = 2\n")
        self.write("brand_new.md", "# fresh\n")
        res = proposal.build(self.tmp, title="带新文件")
        self.assertTrue(res["ok"], res["error"])
        patch = open(res["patch"], encoding="utf-8").read()
        draft = open(res["draft"], encoding="utf-8").read()
        self.assertNotIn("brand_new.md", patch, "未跟踪文件不该混进补丁正文")
        self.assertIn("brand_new.md", draft)
        self.assertIn("不在补丁里", draft)

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_files_filter_limits_the_patch(self):
        self.init_repo()
        self.write("a.py", "x = 2\n")
        self.write("keep.py", "y = 9\n")
        res = proposal.build(self.tmp, title="只打 a.py", files=["a.py"])
        patch = open(res["patch"], encoding="utf-8").read()
        self.assertIn("a.py", patch)
        self.assertNotIn("keep.py", patch)

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_nothing_changed_is_reported_not_faked(self):
        self.init_repo()
        res = proposal.build(self.tmp, title="没改动")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.state, "proposals"))
                         and os.listdir(os.path.join(self.state, "proposals")))

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_staged_mode_and_path_escape(self):
        self.init_repo()
        self.write("a.py", "x = 3\n")
        self.git("add", "a.py")
        staged = proposal.build(self.tmp, title="暂存区", staged=True)
        self.assertTrue(staged["ok"], staged["error"])
        self.assertIn("+x = 3", open(staged["patch"], encoding="utf-8").read())
        escaped = proposal.build(self.tmp, title="越界", files=["../outside.py"])
        self.assertFalse(escaped["ok"])
        self.assertIn("越界", escaped["error"])

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_generated_patch_replays_byte_identical(self):
        """产物必须能用：同一份补丁打到同样的基线上，结果要一字不差。"""
        self.init_repo()
        self.write("a.py", "x = 2\n")
        self.write("keep.py", "y = 2\nz = 3\n")
        res = proposal.build(self.tmp, title="round trip")
        patch = open(res["patch"], encoding="utf-8").read()
        mirror = tempfile.mkdtemp(prefix="docmind_replay_")
        try:
            for rel in ("a.py", "keep.py"):
                with open(os.path.join(mirror, rel), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("x = 1\n" if rel == "a.py" else "y = 2\n")
            applied = patch_apply.apply_patch(mirror, patch)
            self.assertTrue(applied["ok"], applied["errors"])
            for rel in ("a.py", "keep.py"):
                got = open(os.path.join(mirror, rel), encoding="utf-8").read()
                want = open(os.path.join(self.tmp, rel), encoding="utf-8").read()
                self.assertEqual(got, want, rel)
        finally:
            shutil.rmtree(mirror, ignore_errors=True)

    def test_render_reports_missing_root(self):
        text = proposal.render({"ok": False, "error": "代码根目录不存在或未配置。"})
        self.assertIn("未完成", text)

    def test_slugify(self):
        self.assertEqual(proposal.slugify("Fix: 打断!!"), "fix")
        self.assertTrue(proposal.slugify("a/b c").startswith("a-b-c"))
        self.assertEqual(proposal.slugify(""), "proposal")


class RemoteParsingCases(unittest.TestCase):
    def test_common_url_shapes(self):
        cases = {
            "https://github.com/acme/agent-workbench.git": ("acme", "rag-agent"),
            "git@github.com:acme/rag-agent.git": ("acme", "rag-agent"),
            "https://user@token@github.com/acme/rag-agent": ("acme", "rag-agent"),
            "ssh://git@gitlab.example.com/group/sub/proj.git": ("sub", "proj"),
        }
        for url, want in cases.items():
            self.assertEqual(ci_status.parse_remote_url(url)[:2], want, url)

    def test_unusable_urls(self):
        for bad in ("", "/local/path", "github.com:acme/x"):
            owner, repo, err = ci_status.parse_remote_url(bad)
            self.assertFalse(owner or repo, bad)
            self.assertTrue(err, bad)

    def test_token_lookup_order(self):
        os.environ["DOCMIND_GITHUB_TOKEN"] = "a"
        os.environ["GITHUB_TOKEN"] = "b"
        self.assertEqual(ci_status.token(), "a")
        del os.environ["DOCMIND_GITHUB_TOKEN"]
        self.assertEqual(ci_status.token(), "b")
        self.assertEqual(ci_status.token(env={}), "")


@unittest.skipUnless(GIT_OK, "git 不可用")
class RepoSlugCases(_RepoCase):
    def test_reads_origin_from_config(self):
        self.init_repo(origin="git@github.com:acme/rag-agent.git")
        self.assertEqual(ci_status.repo_slug(self.tmp), ("acme", "rag-agent", ""))

    def test_refuses_to_guess_between_two_remotes(self):
        self.init_repo(origin="")
        self.git("remote", "add", "upstream", "https://github.com/other/repo.git")
        self.git("remote", "add", "fork", "https://github.com/me/repo.git")
        owner, repo, err = ci_status.repo_slug(self.tmp)
        self.assertFalse(owner)
        self.assertIn("不唯一", err)

    def test_single_remote_without_origin_is_usable(self):
        self.init_repo(origin="")
        self.git("remote", "add", "upstream", "https://github.com/only/one.git")
        owner, repo, err = ci_status.repo_slug(self.tmp)
        self.assertEqual((owner, repo), ("only", "one"), err)

    def test_worktree_pointer_is_followed(self):
        self.init_repo()
        wt = os.path.join(self.state, "wt")
        self.git("worktree", "add", "-q", "-b", "probe", wt)
        marker = os.path.join(wt, "marker.py")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("m = 1\n")
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")), "worktree 的 .git 是文件")
        owner, repo, err = ci_status.repo_slug(wt)
        self.assertEqual((owner, repo), ("acme", "rag-agent"), err)
        self.assertTrue(ci_status.current_head(wt))


def _transport(payload_by_keyword):
    """假传输：按 URL 里的关键字返回预置响应，同时记录请求头。"""
    seen = []

    def call(url, headers, timeout):
        seen.append((url, headers))
        for key, value in payload_by_keyword.items():
            if key in url:
                return value
        return 404, b'{"message":"Not Found"}'
    call.seen = seen
    return call


class FetchCases(_RepoCase):
    @unittest.skipUnless(GIT_OK, "git 不可用")
    def setUp(self):
        super().setUp()
        self.init_repo()
        self.write("a.py", "x = 2\n")

    RUNS = {"workflow_runs": [{"id": 7, "name": "Harness Tests", "status": "completed",
                               "conclusion": "failure",
                               "head_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                               "head_branch": "main", "html_url": "https://gh/runs/7",
                               "jobs_url": "https://gh/jobs"}]}

    def test_requires_a_token_instead_of_faking(self):
        res = ci_status.fetch(self.tmp, transport=_transport({}), tok="")
        self.assertFalse(res["ok"])
        self.assertIn("token", res["error"])

    def test_happy_path_reports_head_mismatch(self):
        call = _transport({"/actions/runs": (200, json.dumps(self.RUNS).encode()),
                           "/jobs": (200, json.dumps({"jobs": [
                               {"name": "sqlite tests", "status": "completed",
                                "conclusion": "failure"}]}).encode())})
        res = ci_status.fetch(self.tmp, transport=call, tok="t", branch="main",
                              fetch_jobs=True)
        self.assertTrue(res["ok"], res["error"])
        self.assertEqual(res["owner"], "acme")
        self.assertEqual(res["runs"][0]["conclusion"], "failure")
        self.assertEqual(res["runs"][0]["jobs"][0]["name"], "sqlite tests")
        self.assertTrue(any("没有本地 HEAD" in note for note in res["notes"]),
                        "没 push 时必须直说，不能让人以为绿了")
        url = call.seen[0][0]
        self.assertIn("branch=main", url)
        self.assertNotIn("per_page=5&per_page", url)
        self.assertEqual(call.seen[0][1]["Authorization"], "Bearer t")

    def test_summary_text(self):
        call = _transport({"/actions/runs": (200, json.dumps(self.RUNS).encode()),
                           "/jobs": (200, json.dumps({"jobs": [
                               {"name": "sqlite tests", "status": "completed",
                                "conclusion": "failure"}]}).encode())})
        res = ci_status.fetch(self.tmp, transport=call, tok="t", fetch_jobs=True)
        text = ci_status.summarize(res)
        self.assertIn("acme/rag-agent", text)
        self.assertIn("结果=failure", text)
        self.assertIn("失败 job：sqlite tests", text)

    def test_auth_and_missing_are_distinct(self):
        for status, needle in ((401, "拒绝授权"), (403, "拒绝授权"), (404, "找不到"),
                               (500, "HTTP 500")):
            res = ci_status.fetch(self.tmp, tok="t",
                                  transport=_transport({"/actions/runs": (status, b"{}")}))
            self.assertFalse(res["ok"], status)
            self.assertIn(needle, res["error"], status)

    def test_connection_failure_is_honest(self):
        def broken(url, headers, timeout):
            raise ConnectionError("连不上 github.com：timeout")
        res = ci_status.fetch(self.tmp, tok="t", transport=broken)
        self.assertFalse(res["ok"])
        self.assertIn("连不上", res["error"])

    def test_failed_log_keeps_only_error_lines(self):
        body = (b"2026-01-01T0:0:0Z Starting\n"
                b"2026-01-01T0:0:1Z Error: tests/test_x.py FAILED\n"
                b"2026-01-01T0:0:2Z noise line\n"
                b"2026-01-01T0:0:3Z fatal: not a git repo\n")
        res = ci_status.failed_log("https://gh/log", transport=_transport(
            {"/log": (200, body)}), tok="t")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["errors"]), 2)
        self.assertIn("FAILED", res["errors"][0])
        self.assertIn("not a git repo", res["errors"][1])

    def test_failed_log_http_error(self):
        res = ci_status.failed_log("https://gh/log",
                                   transport=_transport({"/log": (403, b"")}), tok="t")
        self.assertFalse(res["ok"])
        self.assertIn("403", res["error"])


class ToolLayerCases(_RepoCase):
    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        self.assertIn("dev_propose", names)
        self.assertIn("dev_ci_status", names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        self.assertIn("- dev_propose(title?", prompt)
        self.assertIn("- dev_ci_status(branch?", prompt)

    def test_missing_code_root(self):
        config.set_runtime("code_root", "")
        self.assertIn("尚未配置代码库根目录", tools.dev_propose("title: x"))
        self.assertIn("尚未配置代码库根目录", tools.dev_ci_status(""))

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_dev_ci_status_without_token_reports_honestly(self):
        self.init_repo()
        text = tools.dev_ci_status("per_page: 3")
        self.assertIn("未完成", text)
        self.assertIn("token", text)

    def test_ci_status_without_a_repo_says_so(self):
        text = tools.dev_ci_status("per_page: 3")
        self.assertIn("未完成", text)
        self.assertNotIn("通过", text)

    @unittest.skipUnless(GIT_OK, "git 不可用")
    def test_dev_propose_end_to_end_text(self):
        self.init_repo()
        self.write("a.py", "x = 7\n")
        text = tools.dev_propose("title: fix: a 改 7\ntest_plan: pytest -k a\nlane: AI-A")
        self.assertIn("提案已生成", text)
        self.assertIn("未提交、未推送", text)
        self.assertIn("不会因此变脏", text)

    def test_log_url_branch(self):
        saved = ci_status.read_transport
        ci_status.read_transport = _transport({"/log": (200, b"Error: boom\n")})
        try:
            text = tools.dev_ci_status("log_url: https://gh/log")
        finally:
            ci_status.read_transport = saved
        self.assertIn("boom", text)


if __name__ == "__main__":
    unittest.main()
