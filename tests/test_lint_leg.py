"""`dev_diagnostics` 的 eslint 那一腿（Wave33 搭的骨架，**只报不卡**）。

## 为什么先不卡

`frontend/src` 有 129 个文件 / 39352 行，**从来没被 lint 过**（node_modules 里连 eslint
都没有）。贸然让它参与判定，第一天大概率是几千条；而几千条红会变成噪声——**比没有 lint
更坏，因为它让人误以为有人在管**。所以这一腿的发现进 `issues`/`notes` 但**不进** `run()`
的 `issues`、**不改** `ok`。等真装上 eslint、拿到存量数字，改一个常量就能切成卡。

## 这一份钉的三件事

1. **没配就明说没跑**，而且说清它不构成任何证据——仓库的既有规矩是
   「跑不成时不能报成没问题」（见 `tests/test_dev_diagnostics.py` 的模块 docstring）。
2. **解析器认得 eslint `stylish` 的真实形状**，包括那条**不是条目**的汇总行。
3. **只报不卡是真的不卡**：eslint 报了东西，`ok` 仍然是 `True`、`count` 仍然只数类型问题。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import diagnostics as d  # noqa: E402

#: eslint `stylish` 的真实输出形状（含 Windows 路径、warning、汇总行）。
STYLISH = r"""
C:\work\frontend\src\App.vue
  12:3  error    Unexpected console statement  no-console
  40:1  warning  'x' is assigned a value but never used  @typescript-eslint/no-unused-vs

C:\work\frontend\src\api\_base.ts
  3:9  error  Unexpected any. Specify a different type  @typescript-eslint/no-explicit-any

✖ 3 problems (2 errors, 1 warning)
"""


class ParseStylishTests(unittest.TestCase):
    def test_it_reads_file_error_and_warning_rows(self):
        issues = d.parse_eslint(STYLISH, "frontend")
        self.assertEqual(len(issues), 3, issues)
        rules = [row["rule"] for row in issues]
        self.assertEqual(rules, ["no-console", "@typescript-eslint/no-unused-vs",
                                 "@typescript-eslint/no-explicit-any"])
        self.assertEqual([row["severity"] for row in issues],
                         ["error", "warning", "error"])

    def test_the_summary_line_is_not_mistaken_for_a_file(self):
        """`✖ 3 problems (2 errors, 1 warning)` 不是条目——第一版没排除它，
        它会被读成一个"文件名就叫这个"的假条目。"""
        issues = d.parse_eslint(STYLISH, "frontend")
        for row in issues:
            self.assertNotIn("problems", row["file"], row)
            self.assertNotIn("✖", row["file"], row)

    def test_rows_before_any_file_line_are_not_invented(self):
        """条目行出现在任何文件行之前时不能硬归属——那说明输出被截断了。"""
        issues = d.parse_eslint("  1:1  error  boom  some-rule\n", "frontend")
        self.assertEqual(issues, [])

    def test_empty_output_is_empty(self):
        self.assertEqual(d.parse_eslint("", "frontend"), [])
        self.assertEqual(d.parse_eslint("\n\n", "frontend"), [])

    def test_it_handles_forward_slash_paths_too(self):
        issues = d.parse_eslint(
            "/w/frontend/src/a.ts\n  1:1  error  boom  r\n", "frontend")
        self.assertEqual(len(issues), 1)
        # `_rel` 把绝对路径归一成相对 `prefix` 的形式，前导斜杠会去掉
        self.assertEqual(issues[0]["file"], "w/frontend/src/a.ts", issues[0])


class ConfigDetectionTests(unittest.TestCase):
    def _frontend(self, **files):
        root = Path(tempfile.mkdtemp(prefix="dm_fe_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        fe = root / "frontend"
        fe.mkdir()
        (fe / "package.json").write_text(
            json.dumps({"name": "x", "scripts": files.pop("scripts", {})}),
            encoding="utf-8")
        for name, body in files.items():
            (fe / name).write_text(body, encoding="utf-8")
        return str(fe)

    def test_a_lint_script_counts_as_configured(self):
        fe = self._frontend(scripts={"lint": "eslint src"})
        configured, why = d.frontend_lint_configured(fe)
        self.assertTrue(configured, why)
        self.assertIn("lint", why)

    def test_a_flat_config_counts_as_configured(self):
        fe = self._frontend(**{"eslint.config.mjs": "export default [];"})
        self.assertTrue(d.frontend_lint_configured(fe)[0])

    def test_neither_means_not_configured(self):
        fe = self._frontend()
        configured, why = d.frontend_lint_configured(fe)
        self.assertFalse(configured)
        self.assertIn("既没有", why)


class NotConfiguredMeansNotRunTests(unittest.TestCase):
    """没配 eslint 时：**不许**报成"干净"，也不许把工具变红。"""

    def _run(self):
        root = Path(tempfile.mkdtemp(prefix="dm_fe2_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        fe = root / "frontend"
        (fe / "src").mkdir(parents=True)
        (fe / "src" / "a.ts").write_text("export const a = 1\n", encoding="utf-8")
        # **typecheck 脚本必须在**：少了它 typecheck 那一腿自己就会红，
        # 于是 `ok=False` 是它造成的、不是 eslint 造成的——这条断言就测不出"eslint 不染红"。
        (fe / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"typecheck": "true"}}), encoding="utf-8")
        (root / "api.py").write_text("x = 1\n", encoding="utf-8")
        return d.run(str(root), scope="frontend")

    def test_the_lint_leg_reports_that_it_did_not_run(self):
        lint = d.frontend_lint(self._root_with_fe())
        self.assertFalse(lint["configured"])
        self.assertFalse(lint["ran"], "没配却 ran=True")
        self.assertEqual(lint["issues"], [])
        joined = " ".join(lint["notes"])
        self.assertIn("没跑", joined)
        self.assertIn("不构成", joined, "必须说清它不构成任何证据")

    def test_an_unconfigured_lint_does_not_turn_the_tool_red(self):
        out = self._run()
        lint = ((out.get("engines") or {}).get("frontend") or {}).get("lint") or {}
        self.assertFalse(lint.get("ran"))
        # eslint 没配**不是**工具的错——typecheck 过了，ok 就还是 True
        self.assertTrue(out["ok"], out.get("error"))

    @staticmethod
    def _root_with_fe():
        root = Path(tempfile.mkdtemp(prefix="dm_fe3_"))
        fe = root / "frontend"
        (fe / "src").mkdir(parents=True)
        (fe / "package.json").write_text(json.dumps({"name": "x", "scripts": {}}),
                                         encoding="utf-8")
        return str(root)


class RenderHonestyTests(unittest.TestCase):
    def test_render_says_the_lint_leg_did_not_run(self):
        """报告里必须有一句明写"没跑"——只在 notes 里带一句会被淹掉。"""
        text = d.render({"issues": [], "ok": True, "notes": ["类型检查跑过了"],
                         "engines": {"frontend": {"ok": True, "lint": {
                             "configured": False, "ran": False, "issues": [],
                             "why": "既没有 lint 脚本，也没有 eslint 配置文件"}}}})
        self.assertIn("eslint", text)
        self.assertIn("没跑", text)
        self.assertIn("不覆盖写法", text)

    def test_render_does_not_invent_a_lint_problem_when_it_never_ran(self):
        text = d.render({"issues": [], "ok": True, "notes": [],
                         "engines": {"frontend": {"ok": True, "lint": {
                             "configured": False, "ran": False, "issues": [],
                             "why": "未配置"}}}})
        self.assertNotIn("eslint 报了", text)


if __name__ == "__main__":
    unittest.main()
