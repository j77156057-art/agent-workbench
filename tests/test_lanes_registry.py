"""Lane 认领登记表回归：排他冲突、TTL 过期、worktree 开通/收回、以及护栏。

状态目录隔离到临时路径（config.STATE_ROOT），不碰仓库真实登记表；
需要真 git 的用例在 git 不可用时 skip。
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
import tools  # noqa: E402
from agent_runtime import lanes  # noqa: E402


def _git_available():
    try:
        proc = subprocess.run(["git", "--version"], capture_output=True, text=True,
                              timeout=15)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


GIT_OK = _git_available()

MINUTE = 60.0


class _StateRoot(unittest.TestCase):
    """code_root + STATE_ROOT 都指向临时目录。"""

    def setUp(self):
        self._prev_state = config.STATE_ROOT
        self._prev_root = config.get_runtime("code_root")
        self.state = tempfile.mkdtemp(prefix="docmind_lane_state_")
        self.repo = tempfile.mkdtemp(prefix="docmind_lane_repo_")
        config.STATE_ROOT = self.state
        config.set_runtime("code_root", self.repo)

    def tearDown(self):
        config.STATE_ROOT = self._prev_state
        config.set_runtime("code_root", self._prev_root)
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def registry(self):
        return lanes._registry_path(self.repo)


# --------------------------------------------------------------------------- #
# 模式与冲突判定（纯函数）
# --------------------------------------------------------------------------- #

class PatternCases(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(lanes.normalize_pattern("./frontend/src/"), "frontend/src/")
        self.assertEqual(lanes.normalize_pattern("'src\\a.py'"), "src/a.py")
        self.assertEqual(lanes.normalize_pattern("/abs/x"), "abs/x")
        self.assertEqual(lanes.normalize_pattern("  "), "")

    def test_literal_prefix(self):
        self.assertEqual(lanes.literal_prefix("frontend/src/workbench/**"),
                         "frontend/src/workbench")
        self.assertEqual(lanes.literal_prefix("tests/test_live_*.py"), "tests")
        self.assertEqual(lanes.literal_prefix("agent_runtime/realtime_omni.py"),
                         "agent_runtime/realtime_omni.py")
        self.assertEqual(lanes.literal_prefix("*.vue"), "")

    def test_sibling_prefix_is_not_nested(self):
        # src 与 src-extra 不是包含关系，保守判定不能把它当成冲突
        self.assertFalse(lanes._nested("frontend/src", "frontend/src-extra"))
        self.assertTrue(lanes._nested("frontend/src", "frontend/src/workbench"))
        self.assertTrue(lanes._nested("", "anything"))

    def test_pattern_matches_covers_directory(self):
        self.assertTrue(lanes.pattern_matches("agent_runtime/realtime_omni.py",
                                              "agent_runtime/realtime_omni.py"))
        self.assertTrue(lanes.pattern_matches("frontend/src", "frontend/src/a/b.ts"))
        self.assertFalse(lanes.pattern_matches("frontend/src", "frontend/srcx/a.ts"))

    def test_pattern_matches_globs(self):
        self.assertTrue(lanes.pattern_matches("frontend/src/**/*.ts",
                                              "frontend/src/workbench/x.ts"))
        self.assertTrue(lanes.pattern_matches("**/live*.ts", "a/b/liveAudioControl.ts"))
        self.assertTrue(lanes.pattern_matches("*.py", "tests/test_x.py"))
        self.assertFalse(lanes.pattern_matches("*.py", "tests/sub/test_x.ts"))
        # fnmatch 的 * 会跨越 `/`，所以认领只会【变宽】不会变窄——对排他是安全方向
        self.assertTrue(lanes.pattern_matches("src/*.ts", "src/a/b.ts"))


# --------------------------------------------------------------------------- #
# 登记表行为
# --------------------------------------------------------------------------- #

class RegistryCases(_StateRoot):
    def claim(self, lane, paths, **kw):
        return lanes.claim(self.repo, lane, paths, **kw)

    def test_claim_and_status_roundtrip(self):
        res = self.claim("AI-A", ["frontend/src/workbench/**"], owner="会话A",
                         note="接播放队列", now=1000.0)
        self.assertTrue(res["ok"], res.get("error"))
        self.assertEqual(res["paths"], ["frontend/src/workbench/**"])
        view = lanes.status(self.repo, now=1000.0 + MINUTE)
        self.assertEqual(view["count"], 1)
        row = view["live"][0]
        self.assertEqual(row["lane"], "AI-A")
        self.assertEqual(row["owner"], "会话A")
        self.assertEqual(row["note"], "接播放队列")
        self.assertTrue(row["live"])

    def test_persisted_to_state_dir(self):
        self.claim("AI-A", ["tools.py"], now=1000.0)
        self.assertTrue(self.registry().exists())
        # 状态必须落在 STATE_ROOT 下，不在 git 工作树里
        self.assertNotIn(self.repo, str(self.registry()))

    def test_overlap_is_rejected_and_names_the_owner(self):
        self.claim("AI-A", ["frontend/src/workbench/**"], owner="会话A", now=1000.0)
        res = self.claim("AI-B", ["frontend/src/workbench/liveAudioControl.ts"],
                         now=1000.0)
        self.assertFalse(res["ok"])
        self.assertEqual(res["conflicts"][0]["lane"], "AI-A")
        self.assertEqual(res["conflicts"][0]["owner"], "会话A")
        self.assertIn("frontend/src/workbench/**", res["conflicts"][0]["pattern"])
        text = lanes.render(res)
        self.assertIn("AI-A", text)
        self.assertIn("冲突", text)

    def test_disjoint_claims_coexist(self):
        self.assertTrue(self.claim("AI-A", ["frontend/src/**"], now=1000.0)["ok"])
        self.assertTrue(self.claim("AI-B", ["agent_runtime/realtime_omni.py"],
                                   now=1000.0)["ok"])
        self.assertEqual(lanes.status(self.repo, now=1000.0)["count"], 2)

    def test_repo_wide_basename_pattern_conflicts_with_everything(self):
        self.claim("AI-A", ["agent_runtime/lanes.py"], now=1000.0)
        res = self.claim("AI-B", ["*.py"], now=1000.0)
        self.assertFalse(res["ok"])
        self.assertTrue(res["conflicts"])

    def test_force_allows_shared_range(self):
        self.claim("AI-A", ["frontend/src/**"], now=1000.0)
        blocked = self.claim("AI-B", ["frontend/src/workbench/**"], now=1000.0)
        self.assertFalse(blocked["ok"])
        res = self.claim("AI-B", ["frontend/src/workbench/**"], now=1000.0, force=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["conflicts"])
        self.assertTrue(any("共管" in w for w in res["warnings"]))
        # 同 lane 自己不算冲突，也不该产生 force 提示
        self.claim("AI-A", ["frontend/src/workbench/deep/**"], now=1000.0)

    def test_heartbeat_extends_the_whole_window(self):
        self.claim("AI-A", ["api.py"], owner="会话A", ttl_minutes=10, now=1000.0)
        hb = lanes.heartbeat(self.repo, "AI-A", owner="会话A", now=1000.0 + 9 * MINUTE)
        self.assertTrue(hb["ok"])
        self.assertAlmostEqual(hb["seconds_left"], 10 * MINUTE, places=0)
        self.assertFalse(lanes.heartbeat(self.repo, "AI-A", owner="会话B",
                                        now=1000.0 + 9 * MINUTE)["ok"])
        self.assertFalse(lanes.heartbeat(self.repo, "AI-Z", now=1000.0)["ok"])

    def test_expired_lease_is_reclaimable_and_shows_as_stale(self):
        self.claim("AI-A", ["api.py"], owner="会话A", ttl_minutes=10, now=1000.0)
        later = 1000.0 + 11 * MINUTE
        view = lanes.status(self.repo, now=later)
        self.assertEqual(view["count"], 0)
        self.assertEqual(view["expired"][0]["lane"], "AI-A")
        self.assertTrue(lanes.check(self.repo, "api.py", now=later)["free"])
        self.assertTrue(self.claim("AI-B", ["api.py"], now=later)["ok"])

    def test_release_removes_and_wrong_owner_is_refused(self):
        self.claim("AI-A", ["api.py"], owner="会话A", now=1000.0)
        self.assertFalse(lanes.release(self.repo, "AI-A", owner="会话B")["ok"])
        self.assertTrue(lanes.release(self.repo, "AI-A", owner="会话A")["ok"])
        self.assertEqual(lanes.status(self.repo)["count"], 0)

    def test_check_finds_owners_and_can_exclude_self(self):
        self.claim("AI-A", ["frontend/src/**"], owner="会话A", now=1000.0)
        hit = lanes.check(self.repo, "frontend/src/workbench/x.ts", now=1000.0)
        self.assertEqual(hit["owners"][0]["lane"], "AI-A")
        self.assertFalse(hit["free"])
        self.assertTrue(lanes.check(self.repo, "frontend/src/workbench/x.ts",
                                   exclude_lane="AI-A", now=1000.0)["free"])

    def test_validation_rejects_bad_input(self):
        self.assertFalse(self.claim("", ["a.py"])["ok"])
        self.assertFalse(self.claim("../escape", ["a.py"])["ok"])
        self.assertFalse(self.claim("AI-A", [])["ok"])
        self.assertFalse(self.claim("AI-A", ["../outside"])["ok"])
        self.assertFalse(self.claim("AI-A", ["a.py\nstatus"])["ok"])
        self.assertFalse(self.claim("AI-A", ["-rf"])["ok"])
        self.assertFalse(self.claim("AI-A", ["a.py"], ttl_minutes="abc")["ok"])
        self.assertFalse(self.claim("AI-A", ["a.py"], ttl_minutes=-1)["ok"])
        self.assertFalse(self.claim("AI-A", ["a.py"], owner="--force")["ok"])

    def test_ttl_is_clamped(self):
        res = self.claim("AI-A", ["a.py"], ttl_minutes=99999, now=1000.0)
        self.assertEqual(res["ttl_minutes"], lanes.MAX_TTL_MINUTES)
        low = self.claim("AI-B", ["b.py"], ttl_minutes=1, now=1000.0)
        self.assertEqual(low["ttl_minutes"], lanes.MIN_TTL_MINUTES)

    def test_corrupt_registry_rebuilds_with_note(self):
        self.claim("AI-A", ["a.py"], now=1000.0)
        self.registry().write_text("{ not json", encoding="utf-8")
        view = lanes.status(self.repo)
        self.assertTrue(view["ok"])
        self.assertEqual(view["count"], 0)
        self.assertTrue(view["notes"])

    def test_lock_contention_reports_instead_of_hanging(self):
        lock = lanes._lock_dir(self.repo)
        lock.mkdir(parents=True, exist_ok=True)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                lanes.status(self.repo)
            self.assertLess(time.monotonic() - started, 10.0)
        finally:
            shutil.rmtree(lock, ignore_errors=True)

    def test_stale_lock_is_broken(self):
        lock = lanes._lock_dir(self.repo)
        lock.mkdir(parents=True, exist_ok=True)
        old = time.time() - lanes._LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        try:
            self.assertTrue(lanes.status(self.repo)["ok"])
        finally:
            shutil.rmtree(lock, ignore_errors=True)

    def test_render_shapes(self):
        self.claim("AI-A", ["frontend/src/**"], owner="会话A", note="R2", now=1000.0)
        text = lanes.render(lanes.status(self.repo, now=1000.0))
        self.assertIn("AI-A", text)
        self.assertIn("frontend/src/**", text)
        self.assertIn("在途 lane：1 条", text)
        released = lanes.render(lanes.release(self.repo, "AI-A", owner="会话A"))
        self.assertIn("已释放", released)
        self.assertIn("无人认领", lanes.render(lanes.check(self.repo, "frontend/src/a.ts")))


# --------------------------------------------------------------------------- #
# worktree 开通 / 收回（真仓库）
# --------------------------------------------------------------------------- #

@unittest.skipUnless(GIT_OK, "git 不可用")
class WorktreeCases(_StateRoot):
    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.git("config", "user.name", "T")
        self.git("config", "user.email", "t@example.com")
        with open(os.path.join(self.repo, "seed.py"), "w", encoding="utf-8") as fh:
            fh.write("print('seed')\n")
        self.git("add", "seed.py")
        self.git("commit", "-q", "-m", "seed")

    def git(self, *args, cwd=None):
        proc = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_git_helper_still_refuses_writes_to_history(self):
        rep = lanes._git(self.repo, ["commit", "-m", "x"])
        self.assertFalse(rep.get("ok", True))
        self.assertIn("白名单", rep["error"])

    def test_open_creates_isolated_checkout(self):
        lanes.claim(self.repo, "AI-A", ["api.py"], owner="会话A")
        res = lanes.open_worktree(self.repo, "AI-A")
        self.assertTrue(res["ok"], res.get("error"))
        self.assertTrue(os.path.isfile(os.path.join(res["worktree"], "seed.py")))
        # 隔离目录在 STATE_ROOT 下，不在主工作树里
        self.assertNotIn(os.path.normpath(self.repo), os.path.normpath(res["worktree"]))
        again = lanes.open_worktree(self.repo, "AI-A")
        self.assertTrue(again["ok"] and again.get("already"))

    def test_open_requires_a_claim(self):
        res = lanes.open_worktree(self.repo, "AI-Z")
        self.assertFalse(res["ok"])
        self.assertIn("先 claim", res["error"])

    def test_close_refuses_dirty_then_discards_on_request(self):
        lanes.claim(self.repo, "AI-A", ["api.py"])
        wt = lanes.open_worktree(self.repo, "AI-A")["worktree"]
        with open(os.path.join(wt, "half_done.py"), "w", encoding="utf-8") as fh:
            fh.write("wip\n")
        refused = lanes.close_worktree(self.repo, "AI-A")
        self.assertFalse(refused["ok"])
        self.assertIn("未提交", refused["error"])
        self.assertTrue(os.path.isdir(wt))
        done = lanes.close_worktree(self.repo, "AI-A", discard=True)
        self.assertTrue(done["ok"], done.get("error"))
        self.assertFalse(os.path.isdir(wt))

    def test_close_keeps_the_claim(self):
        lanes.claim(self.repo, "AI-A", ["api.py"])
        lanes.open_worktree(self.repo, "AI-A")
        self.assertTrue(lanes.close_worktree(self.repo, "AI-A")["ok"])
        self.assertEqual(lanes.status(self.repo)["count"], 1)

    def test_close_without_worktree_is_a_noop(self):
        lanes.claim(self.repo, "AI-A", ["api.py"])
        res = lanes.close_worktree(self.repo, "AI-A")
        self.assertTrue(res["ok"])
        self.assertTrue(res["notes"])

    def test_open_on_non_repo_reports(self):
        plain = tempfile.mkdtemp(prefix="docmind_lane_nogit_")
        try:
            lanes.claim(plain, "AI-A", ["a.py"])
            res = lanes.open_worktree(plain, "AI-A")
            self.assertFalse(res["ok"])
            self.assertIn("git", res["error"])
        finally:
            shutil.rmtree(plain, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 工具层
# --------------------------------------------------------------------------- #

class ToolLayerCases(_StateRoot):
    def test_missing_code_root(self):
        config.set_runtime("code_root", "")
        # 工具现在说**可行动的那一半**（缺什么 + 怎么补），不再只说「没配置」。
        self.assertIn("尚未选择项目，无法定位代码库", tools.dev_lanes("action: status"))

    def test_registration_and_prompt(self):
        names = {row["function"]["name"] for row in tools.tool_schemas()}
        self.assertIn("dev_lanes", names)
        prompt = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        self.assertIn("- dev_lanes(action", prompt)

    def test_unknown_action_lists_options(self):
        out = tools.dev_lanes("action: wat")
        self.assertIn("claim", out)
        self.assertIn("status", out)

    def test_missing_action_reports(self):
        self.assertIn("需要 action", tools.dev_lanes(""))

    def test_claim_status_release_via_text(self):
        out = tools.dev_lanes("action: claim\nlane: AI-A\nowner: 会话A\n"
                             "paths: frontend/src/**, tests/test_live_*.py\n"
                             "ttl_minutes: 60\nnote: 接 R2")
        self.assertIn("认领成功", out)
        self.assertIn("frontend/src/**", out)
        status = tools.dev_lanes("action: status")
        self.assertIn("AI-A", status)
        self.assertIn("接 R2", status)
        blocked = tools.dev_lanes("action: claim\nlane: AI-B\npaths: frontend/src/workbench/x.ts")
        self.assertIn("冲突", blocked)
        self.assertIn("会话A", blocked)
        check = tools.dev_lanes("action: check\npath: frontend/src/workbench/x.ts")
        self.assertIn("AI-A", check)
        self.assertIn("已释放", tools.dev_lanes("action: release\nlane: AI-A"))
        free = tools.dev_lanes("action: check\npath: frontend/src/workbench/x.ts")
        self.assertIn("无人认领", free)

    def test_bare_path_input_for_check(self):
        tools.dev_lanes("action: claim\nlane: AI-A\npaths: agent.py")
        self.assertIn("AI-A", tools.dev_lanes("action: check\nagent.py"))


if __name__ == "__main__":
    unittest.main()
