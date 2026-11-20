"""设计系统商业化验收门禁的回归测试。

为什么需要这些测试：一个从没红过的门禁等于没有门禁。
本文件用**变异测试**证明 tools/design_acceptance.py 每条规则都真的能拦住违规，
再用**基线冻结**防止有人靠 --update-baseline 悄悄把新增债务洗成"存量"。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOL = REPO / "tools" / "design_acceptance.py"
BASELINE = REPO / "tools" / "design_acceptance_baseline.json"

# 存量债务的**冻结计数**（口径 = 基线文件里的去重键数，不是命中行数）。
# 要放宽任何一条，必须同时改这里和基线文件，让"多欠了一笔债"在 PR diff 里
# 一眼可见，而不是被一条命令静默吸收。
FROZEN = {
    "R01": 17,  # 字面量字号（纯 px，非 var/clamp）；A1 收口时修掉 1 处（AskSidebar small 11px）
    "R03": 6,   # var(--x, 色值) 兜底
    "R04": 3,   # 间距脱离 2px 栅格；A2 收口时从 468 批量收敛到 3
    "R06": 24,  # outline:none 未补偿（全局有 :focus-visible 兜底，故为观测项）
}


def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(TOOL), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO),
    )


def make_tree(tmp: Path, css: str, extra: dict[str, str] | None = None) -> Path:
    (tmp / "workbench").mkdir(parents=True, exist_ok=True)
    (tmp / "workbench" / "probe.css").write_text(css, encoding="utf-8")
    for name, body in (extra or {}).items():
        p = tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp


# 一份"合规底样"：过 R02/R05/R07/R08/R10。变异测试只改其中一处，避免互相干扰。
CLEAN = """
:root { --bg: #fff; --surface: #fff; --bg-raised: #fff; --bg-hover: #eee; --bg-selected: #ddd;
        --ink: #111; --text: #111; --text-muted: #555; --border: #ccc; --border-strong: #999;
        --accent: #0f766e; --accent-text: #0b5c55; --accent-ink: #fff;
        --danger: #b3261e; --amber: #8a5a00; --green: #1b5e20;
        --fs-body: 12.5px; --motion-fast: 130ms; }
:root:root:root[data-skin='zinc'] { --bg: #fcfcfd; --surface: #fff; --bg-raised: #fff;
        --bg-hover: #eef; --bg-selected: #dde; --ink: #111; --text: #111; --text-muted: #555;
        --border: #ddd; --border-strong: #aaa; --accent: #0f766e; --accent-text: #0b5c55;
        --accent-ink: #fff; --danger: #b3261e; --amber: #8a5a00; --green: #1b5e20; }
.card { font-size: var(--fs-body); margin: 8px 16px; transition: color var(--motion-fast) ease; }
@media (prefers-reduced-motion: reduce) { .card { transition-duration: .01ms; } }
"""


class TestGateOnRealTree(unittest.TestCase):
    def test_current_tree_passes(self):
        """当前仓库必须过门禁——红了说明有人引入了回归，或基线没跟上代码。"""
        r = run(["--gate"])
        self.assertEqual(r.returncode, 0, f"门禁红：{r.stdout}{r.stderr}")

    def test_baseline_frozen_counts(self):
        """存量债务条数必须与冻结值一致，防止 --update-baseline 静默洗白。"""
        self.assertTrue(BASELINE.exists(), "基线文件缺失，先跑 --update-baseline 并提交")
        data = json.loads(BASELINE.read_text(encoding="utf-8"))
        for rule, expected in FROZEN.items():
            self.assertEqual(len(data.get(rule, [])), expected,
                             f"{rule} 存量条数 {len(data.get(rule, []))} ≠ 冻结值 {expected}；"
                             f"多出来的违规不该被基线吸收，应该修掉")


class TestMutationBlocksGate(unittest.TestCase):
    """每条规则单独注入一处违规，门禁必须变红。"""

    def _gate(self, css: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            make_tree(Path(td), css, extra)
            return run(["--src", td, "--gate"])

    def test_clean_tree_passes(self):
        """先证明底样是干净的，否则下面的红无法归因到注入项。"""
        r = self._gate(CLEAN)
        self.assertEqual(r.returncode, 0, f"底样不干净：{r.stdout}{r.stderr}")

    def test_literal_font_size_blocks(self):
        r = self._gate(CLEAN + "\n.x { font-size: 13px; }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R01", r.stdout)

    def test_dead_token_blocks(self):
        r = self._gate(CLEAN + "\n.x { color: var(--never-defined-zz); }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R02", r.stdout)

    def test_color_fallback_blocks(self):
        r = self._gate(CLEAN + "\n.x { color: var(--text, #123456); }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R03", r.stdout)

    def test_odd_spacing_blocks(self):
        r = self._gate(CLEAN + "\n.x { padding: 3px 7px; }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R04", r.stdout)

    def test_missing_skin_token_blocks(self):
        broken = CLEAN.replace("--accent-ink: #fff; --danger", "--danger", 1)
        r = self._gate(broken)
        self.assertEqual(r.returncode, 1)
        self.assertIn("R05", r.stdout)

    def test_slow_transition_blocks(self):
        r = self._gate(CLEAN + "\n.x { transition: all 300ms ease; }\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R07", r.stdout)

    def test_missing_reduced_motion_blocks(self):
        r = self._gate(CLEAN.replace("@media (prefers-reduced-motion: reduce)", "@media print"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("R08", r.stdout)

    def test_low_chroma_complement_blocks(self):
        """低彩度互补（深蓝紫底 + 暖黄白字，ΔH≈177°）必须被拦。

        这是 2026-11-19 真实踩到的坑：graphite 的 --ink 是 #e8e6e0(H91)，
        --bg 是 #16181d(H268)，两者彩度都 ≈0.01。低彩度互补不会产生"温暖对比"，
        只会互相拉脏。R11 就是为了钉死这条。
        """
        css = CLEAN + "\n:root:root:root[data-skin='bad'] { --bg: #16181d; --ink: #e8e6e0; }\n"
        r = self._gate(css)
        self.assertEqual(r.returncode, 1)
        self.assertIn("R11", r.stdout)

    def test_high_chroma_complement_allowed(self):
        """高彩度互补是有意的设计手法，不能被 R11 误伤。

        注意只断言 R11 未命中：这个测试树故意只给了一套精简皮肤块，
        R05（配色令牌齐全）本来就会红，与 R11 无关。
        """
        css = CLEAN + "\n:root:root:root[data-skin='loud'] { --bg: #ffe0b2; --ink: #1a237e; }\n"
        r = self._gate(css)
        self.assertNotIn("R11", r.stdout, f"高彩度互补被误报：{r.stdout}")


class TestNoFalsePositive(unittest.TestCase):
    def test_dynamic_injected_token_is_allowed(self):
        """Vue :style / JS setProperty 注入的令牌不是死引用，不能误报。"""
        with tempfile.TemporaryDirectory() as td:
            tmp = make_tree(Path(td), ":root { --bg: #fff; }\n.x { color: var(--dyn-x); }\n",
                            {"App.vue": "<template><div :style=\"{ '--dyn-x': '1px' }\"/></template>\n",
                             "motion.ts": "el.style.setProperty('--js-y', '2px')\n"})
            r = run(["--src", str(tmp), "--json"])
            rep = json.loads(r.stdout)["report"]
            tokens = [x["token"] for x in rep["R02"]]
            self.assertNotIn("--dyn-x", tokens)
            self.assertNotIn("--js-y", tokens)


if __name__ == "__main__":
    unittest.main()
