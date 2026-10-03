# -*- coding: utf-8 -*-
"""从正文里切文件引用时，扩展名不能被截断——截断了会**误判路径不存在**。

## 实测（`wf-2a548e060abf` 的 `design_retry`）

模型交出 3769 字符的设计说明、引用 18 处**完全正确**的路径，整份被
`review_output` 拒掉，理由是 `evidence_verified` 判红。真正的原因：

    "path": "behaviors/player.ts",  "exists": false

**`.tscn` 被咬成 `.ts`。** 原扩展名表有 `ts` 却没有 `tscn`，正则匹配到 `ts`
就收尾。审计于是拿着一条**模型从没写过的路径**说「路径打不开」。

同一张表里 `ts|tsx` 的顺序有同一个毛病：`.tsx` 也会被咬成 `.ts`。

这是同一族的第三次了——上一轮是 `behaviors/enemy.gd` 被切成 `enemy.gd`，
那次也是「模型写对了、审计切错了、白花一轮工具调用去修路径」。

**判据**：切出来的路径必须和模型写的一字不差。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.output_audit import _FILE_REF  # noqa: E402


def _refs(text):
    return [(m.group(1), m.group(2), m.group(3))
            for m in _FILE_REF.finditer(text)]


class ExtensionIsNotTruncatedTests(unittest.TestCase):
    """本项目的核心文件类型，一个都不能被截。"""

    def test_godot_scene_files_survive(self):
        """`.tscn` 是 Godot 场景文件，本项目最核心的一类引用。"""
        for name in ("behaviors/player.tscn", "scenes/Main.tscn",
                     "behaviors/enemy.tscn"):
            self.assertEqual(_refs(name)[0][0], name,
                             "「%s」被截断了——审计会拿这条不存在的路径"
                             "判 references_exist 失败" % name)

    def test_tsx_is_not_cut_to_ts(self):
        """`ts|tsx` 的顺序问题：`.tsx` 会被咬成 `.ts`。"""
        self.assertEqual(_refs("src/App.tsx")[0][0], "src/App.tsx")

    def test_the_other_godot_and_web_types(self):
        for name in ("project.godot", "export_presets.cfg", "assets/manifest.json",
                     "default_env.tres", "icon.svg", "main.gd", "index.html",
                     "App.vue", "style.css", "readme.md"):
            self.assertEqual(_refs(name)[0][0], name,
                             "「%s」被截断了" % name)

    def test_the_previously_fixed_case_still_works(self):
        """上一轮修过的那条不能被这次改动带坏。"""
        self.assertEqual(_refs("behaviors/enemy.gd")[0][0], "behaviors/enemy.gd")

    def test_line_suffixes_still_parse(self):
        got = _refs("behaviors/enemy.gd:7")
        self.assertEqual(got[0][0], "behaviors/enemy.gd")
        self.assertEqual(got[0][1], "7")
        got = _refs("behaviors/enemy.gd:L7-L40")
        self.assertEqual((got[0][1], got[0][2]), ("7", "40"))
        # 截断修复后，带行号的 .tscn 也必须完整
        got = _refs("scenes/Main.tscn:12-30")
        self.assertEqual((got[0][0], got[0][1], got[0][2]),
                         ("scenes/Main.tscn", "12", "30"))

    def test_a_suffix_inside_a_path_is_still_rejected(self):
        """防「路径后缀」误匹配：`behaviors/enemy.gd` 里那个 `enemy.gd` 不能再切一遍。

        前瞻断言靠的是紧邻前一个字符（这里是 `/`，落在禁止集里）。
        """
        got = [r[0] for r in _refs("behaviors/enemy.gd")]
        self.assertEqual(got, ["behaviors/enemy.gd"],
                         "同一条路径被切出了嵌套的短名：%s" % got)


class NoPhantomPathTests(unittest.TestCase):
    """端到端：切出来的路径必须**真的能打开**，否则就是拿幻觉判幻觉。"""

    def _audit(self, text):
        from agent_runtime.output_audit import audit_evidence
        return audit_evidence({"text": text, "trace": {"steps": []}},
                              question="分析 behaviors/player.tscn 的结构",
                              code_root=self.root)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "scenes"), exist_ok=True)
        with open(os.path.join(self.root, "scenes", "Main.tscn"), "w",
                  encoding="utf-8") as f:
            f.write("[gd_scene]\n" * 40)

    def test_a_correct_tscn_reference_passes(self):
        text = "场景入口在 `scenes/Main.tscn:1`，它实例化了 behaviors 下的脚本。"
        ev = self._audit(text)
        bad = [r for r in (ev.get("references") or []) if not r.get("exists")]
        self.assertEqual(bad, [],
                         "正确的 .tscn 引用被判成「路径打不开」：%s" % bad)
        self.assertTrue(ev.get("checks", {}).get("references_exist"),
                        "references_exist 判红——这就是拒掉整份答案的那一项")

    def test_a_genuinely_wrong_path_is_still_caught(self):
        """放宽扩展名表不等于放弃核验——真幻觉仍要被抓。"""
        text = "场景入口在 `scenes/NoSuchScene.tscn:1`。"
        ev = self._audit(text)
        self.assertFalse(ev.get("checks", {}).get("references_exist"),
                         "真不存在的路径没被抓到——核验被削弱了")


if __name__ == "__main__":
    unittest.main()