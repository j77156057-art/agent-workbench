"""CI 的 runner 标签与 action 大版本：把天天挂在红名单旁边的弃用告警变成门。

证据（2026-11-02 实测，不是推断）：

- 每个 job 的注解里都有一条 warning：`Node.js 20 is deprecated. The following actions
  target Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4,
  actions/setup-python@v5`；另有一条 notice：`The ubuntu-latest label will migrate to
  Ubuntu 26 beginning October 19, 2026`。
- 「被强制跑到 node24」现在还没炸，是因为 GitHub 替我们兜着 —— 这个兜底是**有截止日期的**：
  node20 那批动作一旦被 runner 拒绝，两条 lane 会一起变成「Set up job 就红」，
  而那时的红名单只有一句 `Container action is not supported...` 之类的噪声。
- 每个大版本跑哪个 node，是从动作自己的 `action.yml` 逐个 tag 读出来的，不是印象：
  checkout v4=node20 / v5=node24；setup-python v5=node20 / v6=node24；setup-node v4=node20
  / v5=node24。node24 这批要求的 runner 版本是 v2.327.1，托管 runner 此刻是 2.338.2 —— 够。
- `ubuntu-latest` 今天解析到的镜像实测是 `Image: ubuntu-24.04`（run 41398743596 的
  `Set up job` 日志）。所以「钉成 ubuntu-24.04」不是换环境，是把已经量到的环境**说出口**；
  标签继续飘着，迁移发生那天浏览器/沙箱/AppArmor 行为跟着变，而红名单不会告诉我们原因。

升级这三个动作有真实的连带风险：`fetch-depth: 0`（改动行覆盖率门靠它取到 base）、
`node-version: "24"`（npm 11.6+ 才认 `min-release-age`，本仓唯一的 npm 侧供应链防护）
和 `cache:` 都会被改步骤时顺手吃掉，所以这里同时钉住输入，不只钉住版本号。
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"

#: 每个动作「第一个跑在 node24 上的大版本」。低于它就等于挂着一条弃用告警。
MIN_NODE24_MAJOR = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/setup-node": 5,
}
#: 实测到的当前镜像（run 41398743596，`Set up job` 里 `Image: ubuntu-24.04`）。
PINNED_RUNNER = "ubuntu-24.04"
USES_RE = re.compile(r"^(?P<repo>[^@/]+/[^@/]+)@(?P<ref>[^/]+)(?:/.*)?$")
CONCRETE_UBUNTU_RE = re.compile(r"^ubuntu-\d+\.\d+$")


def _doc():
    with open(CI_FILE, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _steps():
    return [s for s in _doc()["jobs"]["test"].get("steps") or [] if isinstance(s, dict)]


def _step_by_repo(repo):
    hits = []
    for step in _steps():
        match = USES_RE.match(str(step.get("uses") or "").strip())
        if match and match.group("repo") == repo:
            hits.append((match.group("ref"), step))
    return hits


def _major(ref):
    """从 `v7` 或 `v7.0.1` 取出大版本；取不出来就不是版本标签。

    允许补丁号是实测量出来的，不是宽容：把 `checkout@v7.0.1` 这种精确标签判红
    是本守卫的假阳性 —— 精确标签比会移动的 `v7` **钉得更死**，而 `v7` 本身也只是标签
    （真要不可变得钉 commit SHA，本仓没配 dependabot，SHA 钉死会一路烂到某天突然不兼容）。
    """
    match = re.match(r"^v(\d+)(?:\.\d+\.\d+)?$", ref)
    if not match:
        return None
    return int(match.group(1))


class RunnerLabelTests(unittest.TestCase):
    def test_runner_label_is_pinned_not_latest(self):
        label = _doc()["jobs"]["test"]["runs-on"]
        self.assertRegex(str(label), CONCRETE_UBUNTU_RE,
                         "runs-on 是 %r：`-latest` 会在迁移那天悄悄换镜像" % label)

    def test_pinned_label_is_the_measured_image(self):
        # 钉一个没量过的标签等于把「环境漂移」换成「环境换错」。
        self.assertEqual(str(_doc()["jobs"]["test"]["runs-on"]), PINNED_RUNNER,
                         "标签和实测镜像不一致：日志里是 %s，写的是 %s" % (PINNED_RUNNER, _doc()["jobs"]["test"]["runs-on"]))

    def test_job_timeout_survives(self):
        minutes = _doc()["jobs"]["test"].get("timeout-minutes")
        self.assertIsInstance(minutes, int, "timeout-minutes 没了：挂死的 job 会占满 runner 6 小时")


class ActionNodeVersionTests(unittest.TestCase):
    def test_every_action_runs_on_a_supported_node(self):
        for repo, floor in MIN_NODE24_MAJOR.items():
            hits = _step_by_repo(repo)
            self.assertTrue(hits, "CI 里找不到 %s 步骤" % repo)
            for ref, _step in hits:
                major = _major(ref)
                self.assertIsNotNone(major, "%s 的引用 %r 不是版本标签（vN 或 vN.M.P）" % (repo, ref))
                self.assertGreaterEqual(major, floor,
                                        "%s@%s 仍声明 node20（node24 从 v%d 起）—— 每次跑都吃一条弃用告警"
                                        % (repo, ref, floor))

    def test_actions_are_pinned_by_tag_not_moving_branch(self):
        for step in _steps():
            uses = str(step.get("uses") or "").strip()
            if not uses:
                continue
            match = USES_RE.match(uses)
            self.assertIsNotNone(match, "解析不了 uses: %r" % uses)
            self.assertRegex(match.group("ref"), r"^v\d+(\.\d+.\d+)?$",
                             "%s 用的是可变引用 %r" % (uses, match.group("ref")))

    def test_no_local_action_reference_is_missing(self):
        # 只读断言：`uses: ./` 这类本地动作不在本文件的承诺范围内，但也不该被顺手删掉。
        for step in _steps():
            uses = str(step.get("uses") or "")
            if uses.startswith("./"):
                self.assertTrue((ROOT / uses[2:].split("#")[0].strip() or "action.yml").exists(),
                                "本地动作 %s 指向不存在的目录" % uses)


class ActionInputsSurviveUpgradeTests(unittest.TestCase):
    """升大版本时不许把输入吃掉 —— 这三条输入各自守着另一道门。"""

    def test_checkout_still_fetches_full_history(self):
        hits = _step_by_repo("actions/checkout")
        self.assertEqual(len(hits), 1, "checkout 步骤不止一个：改动行覆盖率门的 base 会取错")
        with_ = hits[0][1].get("with") or {}
        self.assertEqual(str(with_.get("fetch-depth")), "0",
                         "fetch-depth 不是 0：浅克隆取不到 base，覆盖率门会 fail-closed 恒红")

    def test_setup_node_still_pins_the_npm_that_understands_min_release_age(self):
        hits = _step_by_repo("actions/setup-node")
        self.assertTrue(hits, "setup-node 没了：前端 lane 的 node/npm 由谁给？")
        with_ = hits[0][1].get("with") or {}
        self.assertEqual(str(with_.get("node-version")), "24",
                         "node-version 不再是 24：node22 自带 npm 10.x，不认 min-release-age")
        self.assertEqual(str(with_.get("cache")), "npm", "npm 缓存输入被改了")
        self.assertEqual(str(with_.get("cache-dependency-path")),
                         "frontend/package-lock.json", "缓存依赖路径被改了：npm ci 会每次重装")

    def test_setup_python_still_pins_the_interpreter_and_pip_cache(self):
        hits = _step_by_repo("actions/setup-python")
        with_ = hits[0][1].get("with") or {}
        self.assertEqual(str(with_.get("python-version")), "3.12")
        self.assertEqual(str(with_.get("cache")), "pip")

    def test_removed_inputs_are_not_still_passed(self):
        """setup-python v7 删掉了 `pip-install` 输入。留着旧输入不是「无害」。"""
        for repo, forbidden in (("actions/setup-python", "pip-install"),
                                ("actions/setup-node", "always-auth")):
            for _ref, step in _step_by_repo(repo):
                self.assertNotIn(forbidden, step.get("with") or {},
                                 "%s 还在传 v7 已删除的 %s" % (repo, forbidden))


class SiblingGatesSurviveTests(unittest.TestCase):
    def test_the_other_gates_still_own_their_commands(self):
        joined = "\n".join(str(s.get("run") or "") for s in _steps())
        for fragment, label in (("ruff check", "静态缺陷门"),
                                ("npm run lint", "前端 lint 门"),
                                ("npm run test:node", "前端行为测试"),
                                ("coverage_eval", "改动行覆盖率门"),
                                ("python -m compileall", "编译检查")):
            self.assertIn(fragment, joined, "%s 的命令不见了" % label)

    def test_the_steps_referenced_by_guards_are_still_there(self):
        uses = [str(s.get("uses") or "").strip() for s in _steps()]
        for repo in MIN_NODE24_MAJOR:
            self.assertTrue(any(u.startswith(repo + "@") for u in uses),
                            "%s 步骤被删：其它门的运行环境一起没了" % repo)


if __name__ == "__main__":
    unittest.main()
