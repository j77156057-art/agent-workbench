"""前端 lint 到底有没有被 CI 跑 —— 「装了裁判但没人吹哨」的专项守卫。

背景（2026-11-02 实测）：`frontend/eslint.config.mjs` 早就写好、`eslint` 也在
devDependencies 里、`package.json` 也有 `lint` 脚本 —— 但 harness.yml 里
`lint / eslint / coverage` 出现 **0 次**。接进 CI 的第一次运行就抓到一处真实缺陷：
`ChatDock.vue` 切项目时 `sid` 赋值之后再没被读（死赋值）。

这条缺口和 §16.39 那两条同一个形状：**能力在仓库里躺着，没有任何东西执行它**。
所以判定不能是「配置存在」，必须是「CI 真的会跑它，而且它的红会传出去」：

1. 步骤存在，且跑在 `npm ci` 之后（否则 eslint 二进制还没装，红得莫名其妙）；
2. **退出码不许被吞**（`|| true` / `continue-on-error` 都会把门变成装饰）；
3. 门本身要是**能红**的门：当场真跑一次 linter，红就红 —— 一个恒绿的门和没有门等价；
4. 裁判工具是真的（devDependencies + flat config + 脚本确实扫 `src`），不是字符串愿望；
5. job 必须有 `timeout-minutes`：默认 6 小时意味着一次死锁就把 runner 占满，
   红名单也出不来（CI 摘要产物只有在 lane 走到那一步时才会打印）。

npm 不可用时整体跳过，与 `tests/test_frontend_node_tests.py` 同一套降级方式。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"
NPM = shutil.which("npm") or shutil.which("npm.cmd")


def _job():
    with open(CI_FILE, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return doc["jobs"]["test"]


def _steps():
    return [step for step in _job().get("steps") or [] if isinstance(step, dict)]


def _step_with(run_fragment):
    """按 run 里的片段找步骤（找名字会被改名绕过，命令本体才是判据）。"""
    return [step for step in _steps()
            if run_fragment in str(step.get("run") or "")]


class LintStepWiringTests(unittest.TestCase):
    def test_ci_actually_runs_the_linter(self):
        self.assertTrue(_step_with("npm run lint"),
                        "CI 里没有跑 `npm run lint` 的步骤 —— 前端 lint 又回到「装了没人跑」")

    def test_lint_step_runs_inside_the_frontend_package(self):
        step = _step_with("npm run lint")[0]
        self.assertEqual(step.get("working-directory"), "frontend",
                         "不在 frontend 目录里跑，npm 会去仓库根找不到 lint 脚本")

    def test_lint_step_comes_after_the_dependencies_are_installed(self):
        # `npm ci` 在「Build workbench frontend」步骤里；lint 排在它之前必然因缺
        # eslint 二进制而红，那种红不指向任何真实缺陷。
        install = _step_with("npm ci")
        self.assertTrue(install, "找不到装依赖的步骤，harness.yml 结构变了？")
        names = [str(step.get("run") or "") for step in _steps()]
        self.assertLess(names.index(str(install[0].get("run"))),
                        names.index("npm run lint"))

    def test_exit_code_is_not_swallowed(self):
        step = _step_with("npm run lint")[0]
        run = str(step.get("run") or "")
        self.assertFalse(step.get("continue-on-error"),
                         "continue-on-error 会把 lint 变成装饰")
        for swallow in ("|| true", "|| :", "|| echo", "2>/dev/null"):
            self.assertNotIn(swallow, run, "退出码被 %r 吞掉了" % swallow)


class LinterIsRealTests(unittest.TestCase):
    def setUp(self):
        with open(FRONTEND / "package.json", encoding="utf-8") as handle:
            import json
            self.pkg = json.load(handle)

    def test_eslint_is_a_declared_dependency(self):
        deps = dict(self.pkg.get("devDependencies") or {})
        deps.update(self.pkg.get("dependencies") or {})
        self.assertIn("eslint", deps, "package.json 里没有 eslint —— 脚本会现场爆炸")

    def test_flat_config_exists(self):
        # eslint 9 只认 flat config；没有它时 `npm run lint` 会以「couldn't find config」红，
        # 那是配置缺失而不是代码缺陷。
        self.assertTrue((FRONTEND / "eslint.config.mjs").is_file(),
                        "缺 eslint.config.mjs，eslint 9 会拒绝运行")

    def test_lint_script_actually_targets_the_source_tree(self):
        script = str((self.pkg.get("scripts") or {}).get("lint") or "")
        self.assertTrue(script.startswith("eslint"), script)
        self.assertRegex(script, r"\bsrc\b",
                         "lint 脚本没扫 src 目录，等于扫了个空、恒绿")

    @unittest.skipIf(not NPM, "本机没有 npm，跳过真跑")
    def test_the_linter_currently_passes_on_this_tree(self):
        """门要能红，先得当场证明它现在绿 —— 否则接进 CI 只是把 CI 点着。"""
        proc = subprocess.run([NPM, "run", "lint"], cwd=str(FRONTEND),
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0,
                         "eslint 判红：\n%s\n%s" % (proc.stdout[-2000:], proc.stderr[-500:]))


class SiblingGatesSurviveTests(unittest.TestCase):
    """接一个新门不能把邻门的命令吃掉。

    这条是被**自己的变异**逼出来的：删掉 lint 步骤的头三行（`- name:` / `if:` /
    `working-directory:`）后，剩下的 `run: npm run lint` 会并进**上一条步骤**的映射，
    YAML 同键后者覆盖前者 —— 于是「跑前端行为测试」那一步悄悄变成跑 lint，10 条守卫
    全绿。删头三行的动作确实没把 lint 删掉（命令还在跑），但它把另一道门换了命令。
    所以守卫必须盯住邻门的命令本体，而不是只盯自己这条。
    """

    def test_lint_is_exactly_one_step(self):
        self.assertEqual(len(_step_with("npm run lint")), 1,
                         "lint 命令出现在多于一个步骤里 = 有步骤被并进/覆盖")

    def test_frontend_behaviour_tests_still_run(self):
        self.assertTrue(_step_with("npm run test:node"),
                        "前端行为测试那一步的命令不见了（被 lint 顶掉？）")

    def test_frontend_build_still_runs(self):
        self.assertTrue(_step_with("npm run build"),
                        "前端构建步骤不见了")


class JobHardeningTests(unittest.TestCase):
    def test_job_has_a_timeout(self):
        minutes = _job().get("timeout-minutes")
        self.assertIsInstance(minutes, int, "job 没有 timeout-minutes（默认挂到 6 小时）")
        # 上界也钉：写个 9999 等于没写。正常耗时实测 20–30 分钟。
        self.assertTrue(0 < minutes <= 120, minutes)

    def test_lint_gate_is_sqlite_only_like_other_node_steps(self):
        # node 环境只在 sqlite lane 装（`setup-node` 带 if），postgres lane 跑 lint
        # 会因为没 npm ci 而红 —— 门必须挂在有依赖的那条 lane 上。
        step = _step_with("npm run lint")[0]
        self.assertIn("sqlite", str(step.get("if") or ""))


if __name__ == "__main__":
    unittest.main()
