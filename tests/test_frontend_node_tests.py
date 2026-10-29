"""Wave8 第一步：把「写了但从来没人跑」的前端行为测试接进守卫。

`frontend/tests/*.test.mjs` 早就躺在仓库里（5 个文件、12 项断言，跑的是
`src/` 下各页面的真逻辑），但 `package.json` 没有 test 脚本、CI 里也没有对应步骤——
所以这 12 项一次都没执行过，等于零守卫。

这里最阴的失效不是测试变红，而是**测试静默消失**：`node --test` 匹配到 0 个文件时
退出码仍是 0、计数仍是 0，任何「看退出码」的接法都会把「用例被人删了/改名了」显示成
「全绿」。所以本文件的判定核心是**计数比对**：先从源码里数出声明了多少个
`test(...)`，再要求 node 真的跑了同样多个。下面第 4 条用例把这个陷阱本身钉住
（它断言空 glob 确实会假绿，所以任何人想简化成「只看 returncode」都会撞红）。

node 或版本不足时整文件跳过，与仓库里其他前端行为测试同一套降级方式。
"""

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FRONTEND = ROOT / "frontend"
TEST_DIR = FRONTEND / "tests"
NODE = shutil.which("node")
MIN_NODE_MAJOR = 22      # 直接 import .ts 需要 node 的类型剥离（22.6+）
EXPECTED_FILES = 5       # 少于这个数就是有人删了或改了目录
DECLARED = re.compile(r"^\s*(?:test|it)\s*\(", re.M)
SUMMARY = re.compile(r"^# (tests|pass|fail|skipped)\s+(\d+)$", re.M)


def _node_major() -> int:
    if not NODE:
        return 0
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, timeout=20).stdout
    match = re.search(r"v(\d+)", str(out))
    return int(match.group(1)) if match else 0


RUNNABLE = bool(NODE) and _node_major() >= MIN_NODE_MAJOR


def declared_cases() -> tuple[dict[str, int], int]:
    """从源码数出「声明了多少个用例」。用声明数而不是硬编码常量，加用例就不用改这里。"""
    per_file = {}
    for path in sorted(TEST_DIR.glob("*.test.mjs")):
        per_file[path.name] = len(DECLARED.findall(path.read_text(encoding="utf-8")))
    return per_file, sum(per_file.values())


def _summary(text: str) -> dict[str, int]:
    return {key: int(value) for key, value in SUMMARY.findall(str(text or ""))}


def _run(files: list[str]) -> subprocess.CompletedProcess:
    # 显式文件列表，不依赖 shell 的 glob：Windows 的 cmd 不展开通配符。路径一律相对
    # frontend/（node 只认这种写法，裸文件名会当模块去找然后报找不到）。
    # 类型剥离：用例直接 import `.ts`，22.6~22.17 要显式带开关，22.18+ 与 23+ 默认开启
    # （开关仍被接受）。CI 的 `node-version: "22"` 落到哪个补丁号不由我们定，所以 22
    # 一律带上，避免同一份守卫在 CI 与本机表现不一致。
    flags = ["--experimental-strip-types"] if _node_major() == 22 else []
    paths = [str(Path("tests") / name) for name in files]
    return subprocess.run([NODE, *flags, "--test", "--test-reporter=tap", *paths],
                          cwd=str(FRONTEND), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def _glob_run(pattern: str) -> subprocess.CompletedProcess:
    """把一个 glob 原样交给 node（不经过 shell）——只为复现「空匹配也退出 0」。"""
    flags = ["--experimental-strip-types"] if _node_major() == 22 else []
    return subprocess.run([NODE, *flags, "--test", "--test-reporter=tap", pattern],
                          cwd=str(FRONTEND), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


@unittest.skipUnless(RUNNABLE, "node 不可用或低于 v22（需要类型剥离），跳过前端行为测试守卫")
class FrontendBehaviourTests(unittest.TestCase):
    def test_the_expected_test_files_are_still_there(self):
        per_file, total = declared_cases()
        self.assertGreaterEqual(len(per_file), EXPECTED_FILES,
                                "前端用例文件比预期少：有人删了或改名了 → %s" % sorted(per_file))
        self.assertGreaterEqual(total, 12, "声明的用例总数掉了：%s" % per_file)
        for name, count in per_file.items():
            self.assertGreater(count, 0, "%s 里没有任何 test(...) 声明" % name)

    def test_every_declared_case_actually_runs_and_passes(self):
        per_file, expected = declared_cases()
        proc = _run(sorted(per_file))
        summary = _summary(proc.stdout)
        detail = proc.stdout[-2000:] + proc.stderr[-800:]
        self.assertEqual(summary.get("fail", -1), 0, detail)
        # 只看 fail 与退出码都不够：`test.skip` 与「文件被改名后 glob 空匹配」都是退出 0。
        # 声明数必须等于实跑数，且不能有 skip。
        self.assertEqual(summary.get("skipped", -1), 0, detail)
        self.assertEqual(summary.get("tests", -1), expected, detail)
        self.assertEqual(proc.returncode, 0, detail)

    def test_the_modules_under_test_are_the_ones_the_app_imports(self):
        """用例必须真的在测生产模块，而不是测一份副本。

        正则原来只认 `src/workbench/<名字>.ts`，**路径里不能有 `/`** —— 于是
        `src/workbench/composables/workflow-format.ts`（`frontend/src/workbench`
        下唯一被子目录引用的生产模块）整条漏掉，
        `workflowReviewHeadline.test.mjs` 被判成「没有 import 生产模块」。

        那是**守卫的漏洞，不是那份测试的问题**：它第 3 行明明写着
        `from '../src/workbench/composables/workflow-format.ts'`，而且那个模块正是
        `874a6d8` 改过的 `timelineLabel` 所在处。漏掉它等于让那条改动的守卫失效。
        """
        per_file, _ = declared_cases()
        # 允许所有页面目录（workbench / ask / shared / landing）：守卫只问
        # 「测的是不是 src 下的生产模块」，不假设它属于哪个页面。此前只认
        # `src/workbench/`，7ccb40c 给问答页加 messagePresentation.test.mjs 后
        # 这条守卫就红了 —— 是守卫的范围假设过期，不是那份测试有问题。
        sources = FRONTEND / "src"
        # 字符类里必须带 `-`：`workflow-format.ts` 含连字符 —— 我第一版写正则时
        # 漏了它，于是这条守卫自己红着，正好说明了「正则收窄会让守卫失效」。
        pattern = re.compile(
            r"from '\.\./src/([A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)\.ts'")
        for name in per_file:
            body = (TEST_DIR / name).read_text(encoding="utf-8")
            targets = pattern.findall(body)
            self.assertTrue(targets, "%s 没有 import 生产模块" % name)
            for module in targets:
                # 逐段查，且**不许**用 glob / 存在性近似 —— `api/foo` 与 `apifoo`
                # 在 `is_file()` 面前无法区分，但它们在文件系统上是两个不同的东西。
                self.assertTrue((sources / (module + ".ts")).is_file(),
                                "%s import 的 %s.ts 不存在" % (name, module))
                # 反向：引用的必须是**真实存在的生产文件**，不能是测试目录里的副本
                self.assertNotIn("/tests/", module,
                                 "%s 引用的像是测试目录里的东西：%s" % (name, module))

    def test_the_guard_pattern_accepts_a_module_under_a_subdirectory(self):
        """**反向守卫**：别再把正则收窄到「路径里不能有 `/`」。

        上一版那个正则就是这么漏掉了 `composables/workflow-format.ts` —— 而
        `src/workbench` 下明明有 `api/`（18 个 .ts）、`composables/`（23 个）、
        `components/`（1 个）三个子目录共 42 个生产模块，那条改动（`874a6d8` 的
        `timelineLabel`）就在里面。漏掉它的后果是「守卫变绿，但守的东西没被测」。

        同时钉住页面目录不被写死：问答页（`src/ask/`）也有行为测试。
        """
        pattern = re.compile(
            r"from '\.\./src/([A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)\.ts'")
        for rel in ("workbench/liveVisionAlerts", "workbench/composables/workflow-format",
                    "workbench/api/agent", "workbench/components/TopBar",
                    "ask/messagePresentation", "shared/markdown"):
            self.assertTrue(pattern.findall("import x from '../src/%s.ts'" % rel),
                            "正则认不出子目录形态 %r —— 那会让真被测的模块漏出守卫" % rel)
        # 正则按设计不再按目录白名单收窄：`other/` 这种不存在的模块由主用例的
        # `is_file()` 逐个拒掉，不靠正则。这里只钉住「必须还是 src 下的 .ts」。
        self.assertFalse(pattern.findall("import x from '../tests/helper.ts'"))
        self.assertFalse(pattern.findall("import x from '../src/workbench/foo.vue'"))

    def test_an_empty_glob_is_green_for_node_so_counts_are_the_only_guard(self):
        """把陷阱本身钉住：空匹配时 node 退出码 0、tests 0。

        这条不是在测产品，而是在测「我们的判定方式不能只看退出码」——如果哪天有人把
        守卫简化成 assertEqual(proc.returncode, 0)，这条会告诉他为什么不行。
        """
        proc = _glob_run("tests/*.does-not-exist.mjs")
        self.assertEqual(proc.returncode, 0, "node 的行为变了，下面的计数判定需要重写：%s"
                         % proc.stderr[-300:])
        self.assertEqual(_summary(proc.stdout).get("tests"), 0, proc.stdout[-400:])

    def test_npm_script_exists_so_humans_can_run_them(self):
        """没有 `npm run test:node` 的话，这 12 项还是会继续没人跑。"""
        import json
        scripts = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))["scripts"]
        self.assertIn("test:node", scripts, "前端行为测试没有可运行的入口")
        self.assertIn("--test", scripts["test:node"])


if __name__ == "__main__":
    unittest.main()
