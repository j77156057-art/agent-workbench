"""Python 侧静态缺陷门（ruff）到底有没有被 CI 跑、跑的是哪几条规则。

和 `tests/test_ci_lint_gate.py` 同一个形状：**工具在依赖里躺着，但没人执行**。
`ruff` 早就在 `requirements.txt`（第 43 行），`dev_diagnostics` 也会按需跑它，
可 `harness.yml` 里 `ruff` 出现 0 次 —— 合并前没有任何静态缺陷门。

规则集是这道门的**全部内容**，所以它必须被钉住，而且只能从 CI 配置本身读：

- 只放真缺陷类 `F821`（未定义名）/ `F811`（重复定义且旧值没被用）/ `F402`（循环变量遮蔽导入）
  / `F541`（f-string 没有占位符）/ `F841`（赋值后没人读）/ `F401`（未用 import）/
  `W605`（非法转义序列）/ `SIM115`（文件打开后没用上下文管理，句柄靠 gc 关）。
- **不放整组 `F`**：实测 `--select F` 在本仓库 195 条（F401 132 + F841 50 + F541 4 +
  F402 3 + F811 3 + F821 3）。裸接进 CI 的第一天就是 195 行噪声，比没有门更坏 ——
  它让人误以为有人在管。存量清一批再放一条规则。
- 守卫**从工作流里解析出规则集再真跑一次**，所以「把 CI 的规则改宽成 `F`」和
  「把规则缩成只剩 `F821`」都会红：前者被真跑判红（现场就有 195 条），
  后者被子集断言判红。

接线当天它抓到 3 条活的缺陷：`agent_runtime/app_test.py` 把 `Mapping` 只写进注解
却没 import。因为文件第 24 行有 `from __future__ import annotations`，导入期不炸，
但 `typing.get_type_hints(server_env)` 实测抛 `NameError: name 'Mapping' is not defined`
—— 运行时一做类型内省就死，而且是本仓 `receipts` 那类「名字没导入」的同族。
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"
SELECT = re.compile(r"--select\s+([A-Za-z0-9,]+)")
#: 这道门的最小承诺（只增不减）。缩到比它更少就不是同一道门了。
#: 2026-11-02 四次放宽：先 F541/F841 清零放入，再 F401 清掉 119 条后放入 ——
#: F401 剩的 10 条是带证据的豁免（再导出 / 走模块对象取名字 / `ctx.engine_start`
#: 这种静态扫描看不见的动态取用），各自拆成独立语句逐行标 noqa 加 F401；
#: 然后 W605（非法转义，Python 3.12 已出 SyntaxWarning、以后是 SyntaxError）2 条清零放入；
#: 最后 SIM115（未关闭文件句柄）90 处清零放入，剩 3 处带关闭点证据的豁免。
#: 所以这道门的含义是「**新的**这类缺陷会被抓」，不是「仓库里没有豁免」。
#: 注意：这一带别写 noqa 指令的字面形式（井号加空格加 noqa 加冒号加代码）—— 注释里的
#: 那种字面量会被 ruff 当成**本行**的豁免指令解析，写错格式只会得到一句
#: `warning: Invalid # noqa directive`，而豁免其实是哑的。2026-11-02 就是这样踩到一次。
REQUIRED_RULES = {"F821", "F811", "F402", "F541", "F841", "F401", "W605", "SIM115"}


def _steps():
    with open(CI_FILE, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return [s for s in doc["jobs"]["test"].get("steps") or [] if isinstance(s, dict)]


def _ruff_step():
    hits = [s for s in _steps() if "ruff check" in str(s.get("run") or "")]
    return hits


class RuffGateWiringTests(unittest.TestCase):
    def test_ci_actually_runs_the_static_defect_gate(self):
        self.assertTrue(_ruff_step(),
                        "CI 里没有 `ruff check` 步骤 —— Python 侧静态缺陷门又回到 0 处")

    def test_rule_set_is_the_defect_class_not_bare_F(self):
        run = str(_ruff_step()[0].get("run") or "")
        codes = set(SELECT.search(run).group(1).split(","))
        self.assertTrue(REQUIRED_RULES <= codes,
                        "规则集缩到 %s，比 %s 少 = 这道门被换掉了" % (sorted(codes), sorted(REQUIRED_RULES)))
        self.assertNotIn("F", codes, "放开整组 F：本仓库存量 195 条，第一天就是噪声")

    def test_gate_scans_the_whole_repo(self):
        # 把目标从 `.` 缩成某个子目录，门就只守着一小块地方而仍然显示绿。
        run = str(_ruff_step()[0].get("run") or "").strip()
        self.assertTrue(run.endswith("."), "扫描目标不是仓库根：%r" % run[-40:])

    def test_exit_code_is_not_swallowed(self):
        step = _ruff_step()[0]
        run = str(step.get("run") or "")
        self.assertFalse(step.get("continue-on-error"), "continue-on-error 会把门变成装饰")
        for swallow in ("|| true", "|| :", "|| echo", "2>/dev/null"):
            self.assertNotIn(swallow, run, "退出码被 %r 吞掉了" % swallow)

    def test_gate_is_not_parked_on_one_matrix_lane(self):
        # 两条 lane 都有 python 环境；挂 `if` 意味着可以悄悄只在一条上跑。
        step = _ruff_step()[0]
        self.assertNotIn("if", step, "这道门被挂上了条件（两条 lane 都该跑）")

    def test_gate_runs_after_the_python_dependencies_are_installed(self):
        # ruff 来自 requirements.txt；排在校装依赖之前会因为找不到模块而直接炸。
        runs = [str(s.get("run") or "") for s in _steps()]
        install = next(i for i, r in enumerate(runs) if "pip install -r requirements.txt" in r)
        gate = next(i for i, r in enumerate(runs) if "ruff check" in r)
        self.assertLess(install, gate, "ruff 门排在装 Python 依赖之前")

    def test_ruff_is_a_declared_dependency(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?im)^\s*ruff\b", "requirements.txt 里没有 ruff，CI 现场爆炸")

    def test_sibling_gates_still_run_their_own_commands(self):
        """接新门不许把邻门的命令吃掉。

        今早刚为这条付过一次：只删步骤头三行时，剩下的 `run:` 会并进上一条步骤，
        YAML 同键后者覆盖前者，「跑前端行为测试」那一步悄悄变成跑 lint，而所有断言
        只盯自己那条门就全绿。
        """
        runs = [str(s.get("run") or "") for s in _steps()]
        joined = "\n".join(runs)
        for fragment, label in (("npm run lint", "前端 lint 门"),
                                ("npm run test:node", "前端行为测试"),
                                ("python -m compileall", "编译检查")):
            self.assertIn(fragment, joined, "%s 的命令不见了" % label)
        self.assertEqual(len(_ruff_step()), 1, "ruff 命令出现在多个步骤 = 有步骤被并进/覆盖")


class RuffGateActuallyPassesTests(unittest.TestCase):
    """门必须**当场能红**：拿 CI 自己写的规则集真跑一次。

    断言的不是「我本机 ruff 装了」，而是「CI 那条命令此刻退出码为 0」——
    否则接进 CI 只是把 CI 点着；规则被改宽时这条会立刻抓住。
    """

    def test_the_repo_passes_the_gates_own_rule_set(self):
        run = str(_ruff_step()[0].get("run") or "")
        codes = SELECT.search(run).group(1)
        try:
            proc = subprocess.run([sys.executable, "-m", "ruff", "check",
                                   "--select", codes, "."],
                                  cwd=str(ROOT), capture_output=True, text=True,
                                  encoding="utf-8", timeout=600)
        except FileNotFoundError:  # pragma: no cover - ruff 是 stdlib 之外的可执行
            self.fail("ruff 不可执行：CI 里这道门也会直接炸")
        except subprocess.TimeoutExpired:
            self.fail("ruff 超过 600 秒没跑完，这道门在 CI 里不可用")
        self.assertEqual(proc.returncode, 0,
                         "CI 用 `--select %s` 会在现场判红：\n%s" % (codes, (proc.stdout or "")[-3000:]))


if __name__ == "__main__":
    unittest.main()
