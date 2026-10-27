# -*- coding: utf-8 -*-
"""复核**必须认**子代理这一轮真的读过的路径。

## 实测事故（`wf-54651f11c4a0`，探针落盘 `.tmp/stall_probe/audit-root-100137.json`）

同一份结论里，两类引用得到相反判定：

    behaviors/player.gd     exists=TRUE    ← 完整路径，8 个文件全过
    behaviors/player.gd:8   exists=TRUE    line_ok=TRUE
    project.godot           exists=TRUE
    enemy.gd                exists=FALSE   ← **裸文件名**，而子代理刚用
                                         read_file(path: behaviors/enemy.gd)
                                         读到了全文

`enemy.gd` 那一条让 `references_exist: false`，于是 `gate_ok=false`，
**把整份结论否掉**——而同一轮 `reflection issues=0`，判据说「做对了」。

## 根因不是「宽松判定」，是判据问了错的问题

打不开的理由是「根下没有 `enemy.gd`」。可 harness **自己 traj 里躺着那条 obs**：
`read_file(path: behaviors/enemy.gd)` 返回的全文就在手上。

`output_audit.py` 曾有一条注释论证「裸文件名照常判定——它在根下确实不存在」：
**那条推理方向错了**。它论证的是「检查跑得了所以必须判」，却漏了自己有**更好的
证据来源**：不是文件系统，而是**本轮真实执行记录**。

## 边界必须钉死

| 情形 | 判定 | 理由 |
|---|---|---|
| 根下查得到 | 通过 | 文件系统判定 |
| 根下查不到，但**这轮读过** | 通过 | 执行记录本身就是证据 |
| 根下查不到，这轮没读过 | **拦下** | 真的是幻觉路径 |
| 完整路径打不开 | **拦下** | 不给它兜底 —— 兜底只针对裸文件名 |

最后一行是重点：兜底**只**作用于裸文件名。完整路径打不开就是打不开，
不能因为「这轮读过某个同名文件」而放行一个根本不存在的路径。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.output_audit import (  # noqa: E402
    _basename_index,
    _read_paths_from_actions,
    audit_evidence,
)

# 子代理真实动作文本（工作流 traj 里就是带参数的整句）
REAL_ACTIONS = [
    "read_file(path: behaviors/player.gd, start: 1, end: 25)",
    "read_file(path: behaviors/player.gd, start: 26, end: 49)",
    "read_file(path: behaviors/enemy.gd)",
    "grep(pattern: GRAVITY|detect\npath: behaviors/enemy)",
]

ANSWER = (
    "玩家移动在 `behaviors/player.gd:8`，"
    "重力常量见 `behaviors/player.gd:6`，"
    "敌人追击逻辑在 `enemy.gd:12`，"
    "输入映射见 `project.godot`。"
)


def _audit(text=ANSWER, actions=REAL_ACTIONS, code_root=""):
    return audit_evidence(
        {"text": text, "trace": {"steps": [{"action": a} for a in actions]}},
        question="敌人追击逻辑在哪个文件哪一行？", code_root=code_root,
        require_read=True)


class ExtractReadPathsTests(unittest.TestCase):
    def test_it_reads_the_path_out_of_a_real_action_line(self):
        paths = _read_paths_from_actions([
            "read_file(path: behaviors/enemy.gd)",
            "read_file(path: behaviors/player.gd, start: 26, end: 49)"])
        self.assertIn("behaviors/enemy.gd", paths)
        self.assertIn("behaviors/player.gd", paths,
                      "区间参数把路径切坏了：%r" % paths)

    def test_the_scope_form_is_recognised_too(self):
        paths = _read_paths_from_actions([
            "grep(pattern: GRAVITY\npath: behaviors/enemy)"])
        self.assertIn("behaviors/enemy", paths)

    def test_unrelated_actions_are_ignored(self):
        self.assertEqual(_read_paths_from_actions(["list_dir(path: .)"]), [])

    def test_a_prefixed_tool_name_is_not_fooled(self):
        """`read_file_backup(...)` 不是 `read_file(...)`。"""
        self.assertEqual(_read_paths_from_actions(["read_file_backup(path: a.gd)"]), [])


class BasenameIndexTests(unittest.TestCase):
    def test_full_paths_are_indexed_by_their_last_segment(self):
        """索引键是**裸文件名**——查询方拿着 `enemy.gd`，来源是 `behaviors/enemy.gd`。

        曾经写成「跳过带目录的路径」，索引建空、兜底从未生效。
        """
        idx = _basename_index(["behaviors/enemy.gd", "enemy.gd", "res://x/y.gd"])
        self.assertIn("enemy.gd", idx)
        self.assertIn("y.gd", idx)
        self.assertEqual(idx["enemy.gd"], ["behaviors/enemy.gd", "enemy.gd"],
                         "索引必须保留完整来源路径，将来要能说清凭哪条放行")

    def test_case_is_normalised(self):
        idx = _basename_index(["Enemy.gd"])
        self.assertIn("enemy.gd", idx)

    def test_dot_directories_are_not_indexed(self):
        self.assertEqual(_basename_index([".", ".."]), {})


class BareNameThatWasReadThisTurnTests(unittest.TestCase):
    """这一轮真的读过 -> 认。"""

    def test_the_bare_name_that_was_read_now_passes(self):
        row = _audit()
        bare = [r for r in row["references"] if r["path"] == "enemy.gd"]
        self.assertTrue(bare, "结论里应该有 enemy.gd 这一条")
        self.assertTrue(bare[0]["exists"],
                        "这轮读过 behaviors/enemy.gd，却因为写成裸文件名被判打不开")
        self.assertTrue(row["gate_checks"]["references_exist"],
                        "references_exist 仍为 false——整份结论被否掉")

    def test_it_is_marked_as_read_verified_not_as_a_filesystem_hit(self):
        """两种「通过」必须分得清：读过的 vs 系统里有的。"""
        row = _audit()
        bare = [r for r in row["references"] if r["path"] == "enemy.gd"][0]
        full = [r for r in row["references"] if r["path"] == "behaviors/player.gd"][0]
        self.assertTrue(bare.get("read_verified"),
                        "没标出「靠执行记录放行」——以后分不清它为什么过")
        self.assertFalse(full.get("read_verified"),
                         "完整路径是靠文件系统判定的，不该被标成 read_verified")

    def test_a_full_path_that_does_not_exist_is_still_blocked(self):
        """兜底**只**给裸文件名——这是本条最重要的边界。"""
        row = _audit(text="配置在 `ui/does_not_exist.gd:3`。")
        bad = [r for r in row["references"] if r["path"] == "ui/does_not_exist.gd"]
        self.assertTrue(bad)
        self.assertFalse(bad[0]["exists"],
                         "不存在的完整路径被放行了——这轮读过 player.gd 不该替它兜底")


class BareNameThatWasNotReadTests(unittest.TestCase):
    """这轮没读过 -> 照旧拦下。"""

    def test_a_bare_name_absent_from_this_turn_is_blocked(self):
        row = _audit(actions=["read_file(path: behaviors/player.gd)"])
        bare = [r for r in row["references"] if r["path"] == "enemy.gd"]
        self.assertTrue(bare)
        self.assertFalse(bare[0]["exists"],
                         "这轮没读 enemy.gd，裸文件名就该判打不开")

    def test_a_bare_name_written_out_of_thin_air_is_blocked(self):
        row = _audit(text="配置在 `nonexistent.gd:5`。")
        bad = [r for r in row["references"] if r["path"] == "nonexistent.gd"]
        self.assertTrue(bad)
        self.assertFalse(bad[0]["exists"], "凭空写的路径被放行了")


class ExistingTestsStillHoldTests(unittest.TestCase):
    """既有的判据一条都不能被这次放宽破坏。"""

    def test_line_numbers_are_still_verified_for_real_files(self):
        row = _audit()
        full = [r for r in row["references"]
                if r["path"] == "behaviors/player.gd" and r.get("start_line")]
        self.assertTrue(full)
        for ref in full:
            self.assertNotEqual(ref.get("line_ok"), False,
                               "行号校验被跳过了——而这正是本次事故之外的守卫")

    def test_read_file_present_is_still_computed_from_the_trace(self):
        row = _audit()
        self.assertIn("read_file_present", row["checks"])


class BareNameThatWasWrittenThisTurnTests(unittest.TestCase):
    """这一轮**亲手写出来**的文件 -> 裸名引用该认（实测 booklog probe_report.py:?）。

    写入比读取是更强的存在证据：读只是碰巧见到，写是模型自己造的。此前兜底
    只认读过的路径，于是「写完直接用裸名引用、没读回」被判成幻觉、白烧一轮纠偏。
    """

    def _audit(self, steps, text):
        return audit_evidence({"text": text, "trace": {"steps": steps}},
                              question="统计逻辑在哪个文件？", require_read=True)

    def test_a_bare_name_the_model_just_wrote_now_passes(self):
        steps = [{"action": "create_file(path: booklog/probe_report.py, "
                            "new_text: def summary(): return 1)",
                  "ok": True, "obs": "已创建 booklog/probe_report.py"}]
        row = self._audit(steps, "统计探针逻辑见 probe_report.py。")
        bare = [r for r in row["references"] if r["path"] == "probe_report.py"]
        self.assertTrue(bare, "结论里应有 probe_report.py")
        self.assertTrue(bare[0]["exists"],
                        "模型刚 create_file 写出的文件，裸名引用却判打不开")
        self.assertEqual(bare[0].get("verified_via"), "write",
                         "没标出放行凭据是「本轮写过」")
        self.assertTrue(row["gate_checks"]["references_exist"],
                        "references_exist 仍 false——整份结论被否掉")

    def test_paths_inside_new_text_body_do_not_pollute_the_index(self):
        """边界：只认被写的 path，绝不把 new_text 正文里的 `xxx.py` 当经手文件。"""
        steps = [{"action": "create_file(path: booklog/probe_report.py, "
                            "new_text: import decoy_helper)",
                  "ok": True, "obs": "已创建 booklog/probe_report.py"}]
        row = self._audit(steps, "依赖见 decoy_helper.py。")
        decoy = [r for r in row["references"] if r["path"] == "decoy_helper.py"]
        self.assertTrue(decoy)
        self.assertFalse(decoy[0]["exists"],
                         "new_text 正文里的代码路径被误当成本轮经手文件放行了")


class BareNameSeenInSearchObservationTests(unittest.TestCase):
    r"""grep/search_code 动作里没 path，但**成功观察**逐行列出了文件 -> 该认。

    实测 booklog 现场：模型 `grep(pattern: count_by_status|def stats)`，观察里
    打印 `booklog\stats.py:49`，结论缩写成裸名 `stats.py` 却被判幻觉、逼它多读两轮。
    """

    def _audit(self, steps, text):
        return audit_evidence({"text": text, "trace": {"steps": steps}},
                              question="统计逻辑在哪个文件？", require_read=True)

    def test_a_bare_name_listed_in_a_successful_grep_observation_passes(self):
        obs = "booklog\\stats.py:49: def count_by_status(books):\n" \
              "booklog\\service.py:126: def count_by_status(self):"
        steps = [{"action": "grep(pattern: count_by_status|def stats)",
                  "ok": True, "obs": obs}]
        row = self._audit(steps, "统计逻辑见 stats.py 和 service.py。")
        for name in ("stats.py", "service.py"):
            bare = [r for r in row["references"] if r["path"] == name]
            self.assertTrue(bare, "结论里应有 %s" % name)
            self.assertTrue(bare[0]["exists"],
                            "grep 观察里明明列出了 %s，裸名引用却判打不开" % name)
            self.assertEqual(bare[0].get("verified_via"), "observed")

    def test_a_failed_search_observation_does_not_rescue_any_bare_name(self):
        steps = [{"action": "grep(pattern: whatever)", "ok": False,
                  "obs": "booklog\\ghost.py:1: 命中"}]
        row = self._audit(steps, "逻辑见 ghost.py。")
        bare = [r for r in row["references"] if r["path"] == "ghost.py"]
        self.assertTrue(bare)
        self.assertFalse(bare[0]["exists"],
                         "失败的检索观察不该替任何裸名背书")

    def test_a_bare_name_never_written_read_or_seen_is_still_blocked(self):
        steps = [{"action": "grep(pattern: count_by_status)", "ok": True,
                  "obs": "booklog\\stats.py:49: def count_by_status(books):"}]
        row = self._audit(steps, "还有个幽灵文件 ghost_module.py。")
        bare = [r for r in row["references"] if r["path"] == "ghost_module.py"]
        self.assertTrue(bare)
        self.assertFalse(bare[0]["exists"],
                         "本轮没经手的裸名被放行了——兜底不能变成放水")


if __name__ == "__main__":
    unittest.main()