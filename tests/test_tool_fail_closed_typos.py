# -*- coding: utf-8 -*-
"""三个关键指针的 fail-closed 锁定测试（2026-10-19，PK-AUDIT）。

覆盖 _parse_keyed 审计里定性为 HIGH/MEDIUM 的三处"拼错静默降级"缺陷：
  - dev_approve 的 target  → 拼错/缺省不再默认 *（防审批作用范围被放大）
  - dev_region_edit 的 old_text → 拼错不落到"整体重写"分支（防覆盖原文件）
  - dev_headcheck 的 gate → 拼错不把阻塞门禁静默降级为建议模式

每个都锁定"拼错家族 → 拒绝执行、零副作用"，并保留两个合法语义：
显式通配 target:* 仍放行、完全省略 old_text/gate 仍走原设计分支。
全部离线，不真正落审批台账、不真正改文件、不真正跑子进程。
"""
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch

import config
import tools
from tools import dev_approve, dev_region_edit, dev_headcheck


class _Root(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="docmind_fct_")
        self.root = os.path.join(self.tmp.name, "project")
        os.makedirs(self.root)
        self._old_root = config.get_runtime("code_root")
        config.set_runtime("code_root", self.root)

    def tearDown(self):
        if self._old_root:
            config.set_runtime("code_root", self._old_root)
        else:
            config._RUNTIME.pop("code_root", None)
        self.tmp.cleanup()


class DevApproveTargetTypos(_Root):
    """BUG-APPROVE-WILDCARD：target 是审批作用范围唯一指针，拼错/缺省不得默认 *。"""

    def test_typo_family_refused(self):
        for typo in ("targett", "targetting", "targets", "target_"):
            with self.subTest(typo=typo):
                arg = "action: commit_all\n%s: * " % typo
                with patch.object(tools, "_require_regions") as rr, \
                     patch("game_workbench.approval") as appr:
                    rr.return_value = ((self.root, {}), None)
                    out = dev_approve(arg)
                self.assertIn("target 键拼错", out)
                self.assertIn("未审批", out)
                appr.assert_not_called()  # 零副作用：没有写下任何审批

    def test_missing_target_no_longer_wildcards(self):
        # 旧行为：缺 target 兜底成 "*"（授权范围比模型本意更宽）。新行为：显式拒绝。
        with patch("game_workbench.approval") as appr:
            out = dev_approve("action: commit_all")
        self.assertIn("请显式提供 target", out)
        self.assertIn("不再默认", out)
        appr.assert_not_called()

    def test_explicit_star_still_proceeds(self):
        # 正向对照：显式写 target: * 时，审批照常放行（不误伤 commit_all/apply_regions）。
        with patch.object(tools, "_require_regions") as rr, \
             patch("game_workbench.approval") as appr:
            rr.return_value = ((self.root, {}), None)
            out = dev_approve("action: commit_all\ntarget: *")
        self.assertIn("已审批", out)
        kwargs = appr.call_args.kwargs
        self.assertEqual(kwargs.get("target"), "*")

    def test_precise_key_still_proceeds(self):
        # 正向对照：精确 key 拼写（未拼错、值非 *）时，审批照常按对象放行。
        with patch.object(tools, "_require_regions") as rr, \
             patch("game_workbench.approval") as appr:
            rr.return_value = ((self.root, {}), None)
            out = dev_approve("action: commit_region\ntarget: src")
        self.assertIn("已审批", out)
        self.assertEqual(appr.call_args.kwargs.get("target"), "src")


class DevRegionEditOldTextTypos(_Root):
    """BUG-OLDBLOCK-REWRITE：old_text 是"局部替换 vs 整体重写"的模式指针。"""

    def _run(self, arg, isfile=True):
        captured = {}

        def fake_apply(a):
            captured["apply"] = a
            return "APPLY_RAN"

        def fake_create(a):
            captured["create"] = a
            return "CREATE_RAN"

        with patch.object(tools, "_require_regions") as rr, \
             patch.object(tools, "_resolve_region_path") as rp, \
             patch.object(tools, "apply_edit", side_effect=fake_apply), \
             patch.object(tools, "create_file", side_effect=fake_create), \
             patch.object(tools.os.path, "isfile", return_value=isfile):
            rr.return_value = ((self.root, {"src": {}}), None)
            rp.return_value = (os.path.join(self.root, "a.py"), None)
            out = dev_region_edit(arg)
        return out, captured

    def test_old_text_family_refused(self):
        for typo in ("oldText", "oldtext", "old_key", "old"):
            with self.subTest(typo=typo):
                arg = "region: src\npath: a.py\n%s: X\nnew_text: Y" % typo
                out, cap = self._run(arg)
                self.assertIn("疑似 'old_text' 拼错", out)
                self.assertIn("拒绝执行", out)
                self.assertNotIn("apply", cap)   # 未整体重写
                self.assertNotIn("create", cap)  # 未新建

    def test_full_omission_still_rewrites(self):
        # 正向对照：完全省略 old_text 行 = docstring 明写的"整体重写"，应放行。
        out, cap = self._run("region: src\npath: a.py\nnew_text: WHOLE_FILE")
        self.assertNotIn("拒绝执行", out)
        self.assertIn("apply", cap)

    def test_precise_old_text_still_replaces(self):
        # 正向对照：精确 old_text 拼写时走局部安全替换。
        out, cap = self._run("region: src\npath: a.py\nold_text: X\nnew_text: Y")
        self.assertNotIn("拒绝执行", out)
        self.assertIn("apply", cap)


class DevHeadcheckGateTypos(_Root):
    """BUG-GATE-SILENT-OFF：gate 是发布门禁是否阻塞的开关。"""

    def _run(self, arg):
        captured = {}

        class FakeHeadCheck(types.ModuleType):
            SUITE_TIMEOUT_MAX = 300
            MAX_TEST_TARGETS = 12

            def check(self, root, **kw):
                captured["kw"] = kw
                return {"ok": True}

            def render(self, report):
                return "RENDER_RAN"

        fake = FakeHeadCheck("agent_runtime.head_check")
        with patch.object(tools, "_get_code_root", return_value=self.root), \
             patch("agent_runtime.head_check", fake, create=True):
            out = dev_headcheck(arg)
        return out, captured

    def test_gate_family_refused(self):
        for typo in ("gates", "gatee", "gate_flag", "gate_"):
            with self.subTest(typo=typo):
                arg = "ref: HEAD\ntimeout: 60\n%s: 1" % typo
                out, cap = self._run(arg)
                self.assertIn("gate 键拼错", out)
                self.assertIn("未执行 headcheck", out)
                self.assertNotIn("kw", cap)  # check 从未被调用

    def test_gate_on_still_runs_as_gate(self):
        # 正向对照：gate: 1 → 阻塞门禁模式照常执行。
        out, cap = self._run("ref: HEAD\ntimeout: 60\ngate: 1")
        self.assertNotIn("键拼错", out)
        self.assertTrue(cap["kw"].get("gate"))

    def test_omitted_gate_runs_import_only(self):
        # 正向对照：完全省略 gate 行 = 仅 import 自检（非门禁），仍应执行。
        out, cap = self._run("ref: HEAD\ntimeout: 60")
        self.assertNotIn("键拼错", out)
        self.assertFalse(cap["kw"].get("gate"))


if __name__ == "__main__":
    unittest.main()
