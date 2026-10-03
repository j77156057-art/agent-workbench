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


class GodotUriSchemeTests(unittest.TestCase):
    """`res://` 是 Godot 原生路径写法，模型每写一个就被误报一次。

    实测（`wf-87c0df3224aa` 的 `design_probe`）：模型交出 4028 字符、每条都带
    `文件:行号` 的证据清单，被整份拒掉，理由是 6 条引用「路径打不开」：

        //addons/docmind_bridge/docmind_bridge.gd
        //behaviors/enemy.tscn
        //scenes/Main.tscn
        …

    正则的路径字符集含 `/`，于是从 `res:` 后面那两个斜杠开始咬，把 **scheme
    吃进了路径**。这个项目是 Godot 优先的——`res://` 是它的母语，不是边角情况。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        for rel in ("scenes/Main.tscn", "behaviors/player.tscn",
                    "behaviors/player.gd"):
            full = os.path.join(self.root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write("[gd_scene]\n" * 40)

    def _audit(self, text, question="分析场景结构"):
        from agent_runtime.output_audit import audit_evidence
        return audit_evidence({"text": text, "trace": {"steps": []}},
                              question=question, code_root=self.root)

    def test_the_scheme_is_stripped_before_the_path_is_resolved(self):
        ev = self._audit("场景在 res://scenes/Main.tscn:1，脚本在 res://behaviors/player.gd:2。")
        self.assertTrue(ev["checks"]["references_exist"],
                        "res:// 原生路径被判成打不开：%s"
                        % ev.get("references"))

    def test_the_extracted_paths_are_the_ones_the_model_wrote(self):
        ev = self._audit("见 res://scenes/Main.tscn:1 与 res://behaviors/player.gd:2")
        got = sorted(r["path"] for r in ev["references"])
        self.assertEqual(got, ["behaviors/player.gd", "scenes/Main.tscn"])

    def test_other_schemes_are_handled_too(self):
        ev = self._audit("见 user://behaviors/player.gd:2")
        self.assertTrue(ev["checks"]["references_exist"],
                        "user:// 前缀没处理")

    def test_backslashes_and_dot_slash_are_normalised(self):
        ev = self._audit(r"见 .\scenes\Main.tscn:1")
        self.assertTrue(ev["checks"]["references_exist"],
                        "Windows 反斜杠 / 前导 ./ 没归一：%s" % ev.get("references"))


class BareNameIsStillJudgedTests(unittest.TestCase):
    """裸文件名**照常判定**——它在代码根下确实不存在。

    我一度把它们从判定里摘出去，理由是「我们没去找它在哪一份」。**那是把一条
    关于**行号**的规矩挪用到**路径存在性**上了**：本文件原有的「打不开 →
    行号标记为不适用」讲的是行号越界，而裸文件名的存在性是**跑得了的检查**。

    既有测试已把它钉死（`test_a_bare_basename_is_caught_as_an_unopenable_reference`
    / `test_an_unopenable_reference_fails_the_gate`），原则出自 Claude Code：
    「If you can run the check, you must decide PASS or FAIL.」所以这里守原判。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        full = os.path.join(self.root, "behaviors", "player.tscn")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write("[gd_scene]\n" * 40)

    def _audit(self, text):
        from agent_runtime.output_audit import audit_evidence
        return audit_evidence({"text": text, "trace": {"steps": []}},
                              question="分析结构", code_root=self.root)

    def test_a_bare_name_is_still_an_unopenable_reference(self):
        ev = self._audit("场景树见 player.tscn。")
        self.assertFalse(ev["checks"]["references_exist"],
                         "裸文件名被放行了——它在代码根下确实不存在")

    def test_but_its_line_numbers_are_not_also_reported_as_bad(self):
        """「打不开 → 行号不适用」这条规矩只管**行号**，不能被一起推翻。"""
        ev = self._audit("数值在 player.gd:7。")
        self.assertTrue(ev["checks"]["reference_lines_valid"],
                        "打不开的文件被同时报成「行号越界」——我们从未数过行数")


class WorkflowTraceShapeTests(unittest.TestCase):
    """工作流存的 action 是**带参数的整句**，不是裸工具名。"""

    def test_tool_name_is_extracted_from_the_action_text(self):
        from agent_runtime.output_audit import _tool_name
        self.assertEqual(_tool_name("read_file(path: project.godot)"), "read_file")
        self.assertEqual(_tool_name("read_file"), "read_file")
        self.assertEqual(_tool_name(" python_exec(code: pass) "), "python_exec")
        self.assertEqual(_tool_name(""), "")
        self.assertEqual(_tool_name("list_dir(path: .)"), "list_dir")

    def test_read_file_present_is_true_for_the_workflow_shape(self):
        """此前写的是 `action == "read_file"` 精确匹配，工作流路径下**恒为空**。

        而 `evidence_verified` 用的正是含这一项的聚合 —— 于是**每个工作流
        子代理的结论都被 Harness 复核拒掉**，无论内容多好。
        """
        from agent_runtime.output_audit import audit_evidence
        ev = audit_evidence(
            {"text": "见 res://behaviors/player.gd:1",
             "trace": {"steps": [{"action": "read_file(path: behaviors/player.gd)",
                                  "obs": "…"}]}},
            question="这个字段在哪个文件哪一行？", code_root=None)
        self.assertTrue(ev["checks"]["read_file_present"],
                        "工作流的动作文本形状认不出来——这一项恒为 False")

    def test_the_gate_still_requires_a_read(self):
        """**刻意保留严的一边**，不为省事放宽。

        `audit_evidence` 里另有 `gate_ok`（只看事实项）并注释说「调用方应当用它
        驱动闸门」，`test_verification_gate_reshape.py` 也按那条原则写；而
        `test_output_audit.py` 要求「定位类问题必须真读过文件」。两个契约互相
        矛盾。

        本轮站在**严的一边**：`read_file_present` 恒为 False 是**bug**
        （认不出 `read_file(path: x)` 这个真实形状），不是这一项本身不可靠。
        bug 修好后这一项就判得准了，没必要再放宽口径——否则等于放过
        「模型没读过也能报出 文件:行号」这个真实漏洞。
        """
        import inspect

        from agent_runtime import output_audit
        src = inspect.getsource(output_audit.audit_final_output)
        self.assertIn('"evidence_verified": evidence["ok"]', src,
                      "口径被放宽成只看事实项——定位类问题不真读文件也能过了")

    def test_a_location_answer_without_a_read_is_still_rejected(self):
        """上面那条判定的实际后果，单独钉住。"""
        import tempfile
        from pathlib import Path

        from agent_runtime.output_audit import audit_final_output
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "enemy.gd"
            path.write_text("func _ready():\n\tadd_to_group(\"enemy\")\n",
                            encoding="utf-8")
            out = audit_final_output(
                {"status": "ok", "text": "敌人入口在 enemy.gd:2。",
                 "trace": {"steps": [{"action": "search_code(query: enemy)",
                                      "ok": True}]}},
                question="敌人的巡逻逻辑在哪个文件？", code_root=root)
            self.assertFalse(out["ok"],
                             "没读文件却放行了——那是靠猜报出 文件:行号")


if __name__ == "__main__":
    unittest.main()