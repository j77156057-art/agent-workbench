# -*- coding: utf-8 -*-
"""隐私门（tools/privacy_scan.py）的**判据自测**。

## 为什么这道门需要自测

隐私门本身是「检查别人的代码」的判据 —— 一旦它自己失明，它会**永远绿**，
而没有任何东西会变红告诉��。实测踩过的两个真实失明：

1. `drive_path` 的分隔符只认单个 → 源码/JSON 里写成**转义形式**
   （`D:\\WorkBuddy\\…`）的整类写法在门禁面前隐形；
2. `session_id` 只认 `ask|web|dev` 三个前缀 → 工作流会话 id（`wf-`）与
   跑批时间戳 id（`golden-<时间戳>-GNN`）全漏，而仓库里当时有 137 处真实值。

这两类洞都不是「判据写错了」，而是「判据没覆盖到某种形态」—— 只有拿形态
去撞判据本身，才测得出来。

## 探针为什么用拼接字面量

本文件自己会被隐私门扫。如果把「像真的」样例直接写成字面量，门禁就会把
**测试文件本身**判成泄露（红），于是这道自测反而把门禁弄红。
所以探针一律拆成片段再拼：文件里存的是片段，门禁扫不到；
运行时拼出来的字符串照样去撞判据。
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_gate():
    path = os.path.join(ROOT, "tools", "privacy_scan.py")
    spec = importlib.util.spec_from_file_location("privacy_scan_gate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PS = _load_gate()


class GateJudgementTests(unittest.TestCase):
    """判据本身要能咬住东西 —— 否则又是一道永远绿的灯。"""

    def _hits(self, kind, text):
        return [m.group(0) for m in PS.PATTERNS[kind].finditer(text)]

    def test_an_escaped_machine_path_is_still_caught(self):
        """转义形式（源码/JSON 里最常见的写法）不能逃过。"""
        probe = "ROOT = r\"D:" + chr(92) * 2 + "WorkBuddy" + chr(92) * 2 + "rag-agent\""
        self.assertTrue(self._hits("drive_path", probe),
                        "转义形式的本机路径逃过了门禁——旧正则只认单分隔符")

    def test_a_single_separator_path_is_still_caught(self):
        probe = "D:" + chr(92) + "WorkBuddy" + chr(92) + "rag-agent"
        self.assertTrue(self._hits("drive_path", probe))

    def test_an_example_path_is_not_a_violation(self):
        """示例路径不判：它不指向真机身份。"""
        probe = "D:" + chr(92) + "MyGame" + chr(92) + "level.ts"
        self.assertFalse(self._hits("drive_path", probe))

    def test_a_real_workflow_session_id_is_caught(self):
        probe = "wf-" + "f3da9d3ffb32"
        self.assertTrue(self._hits("wf_session_id", probe))

    def test_the_synthetic_workflow_ids_are_allowed(self):
        """假化后的 id 必须放行 —— 否则门禁会逼着大家把真 id 留着。"""
        probe = "wf-" + "0" * 8 + "0001"
        self.assertTrue(self._hits("wf_session_id", probe))
        allow = PS.ALLOW["wf_session_id"]
        self.assertTrue(any(a.match(probe) for a in allow),
                        "合成占位没被白名单接住")

    def test_a_real_run_stamp_id_is_caught(self):
        probe = "golden-" + "20261027-201309-G01"
        self.assertTrue(self._hits("run_stamp_id", probe))

    def test_a_real_local_session_file_path_is_caught(self):
        probe = ".docmind_sessions" + chr(92) * 2 + "golden-" + "20260919-143443-G38.json"
        self.assertTrue(self._hits("local_session_path", probe))

    def test_the_state_dir_name_alone_is_not_a_violation(self):
        """`.docmind_sessions` 是产品公开的目录名，本身不判。"""
        probe = "会话写在 .docmind_sessions" + chr(92) + "x.json"
        self.assertFalse(self._hits("local_session_path", probe))


class GateOnTheRealRepoTests(unittest.TestCase):
    """门禁在**当前仓库**上的表现。"""

    def test_the_gate_is_green_on_this_tree(self):
        violations, allowed = PS.scan(ROOT)
        self.assertEqual(violations, [],
                         "隐私门在本仓判出泄露：\n" + "\n".join(violations[:20]))

    def test_the_committed_golden_baseline_carries_no_real_run_ids(self):
        """入库的基线必须**真的**假化过（README 对外承诺过这件事）。"""
        path = os.path.join(ROOT, "golden", "results_baseline.jsonl")
        if not os.path.isfile(path):
            self.skipTest("基线文件不在本工作树")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(PS.PATTERNS["run_stamp_id"].findall(text), [],
                         "入库基线里还有真实跑批会话 id")
        self.assertEqual(PS.PATTERNS["local_session_path"].findall(text), [],
                         "入库基线里还有真实本机会话文件路径")


if __name__ == "__main__":
    unittest.main()
