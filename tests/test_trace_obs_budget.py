# -*- coding: utf-8 -*-
"""回传轨迹里的观察**必须完整到足以判案**——它同时是反思判据的证据。

## 实测事故（`wf-f3f3279f355a`）

同一个文件 `behaviors/player.gd`，三次尝试报出**三个行数**：

    read-player      「全文 47 行，已完整读取」
    read-player-fix  「文件共 50 行（read_file 完整返回，未被截断）」
    read-player-seg  「全文 51 行，已实际读取」

而 `trace.steps` 里两步的观察**都只有 251 字**（`ORCH_TRACE_OBS_CHARS=240`
在 `_clip` 里加上后缀就是 251）。真实观察是：

    grep       495 字  -> 251 字（51%）
    read_file  1388 字  -> 251 字（18%，砍完文件头那 7 行就没了）

## 判据的指控完全正确

    「结论声称文件共 50 行且完整返回，但工具观察中 read_file 明确显示已截断」
    「大量行号（30、31-32、33、8、25-26）在 read_file 观察里并未出现」

**两句都对。** 251 字里真的只有文件头那几行，行号 30/31/33 根本不在里面。
判据没有误判——它手上就那么多材料。**证据是在进判据之前被砍掉的。**

而模型之所以在数「47 / 50 / 51」，是因为它只看得见那 251 字，必须自己数。
给了全文它根本不需要猜。

## 为什么这条不能当成「落库体积」问题

`_traj_evidence_block` 的预算本轮刚从 1600 抬到 6400，但那份预算拿到的是
**已经被砍到 251 的 obs**——抬下游预算毫无意义。刀在上游。

同一个值喂三个消费者：落库 trace、`_traj_evidence_block`、replanner。
所以它不是「给数据库省多少字节」的取舍，是**判据据以判案的证据完整性**。

这是同一条线上第五次证明同一件事：`start_line:` 语法、`read_file` 键名、
证据块 420/1600、并行 tool_call 误判、这里。
"""
import io
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402

PLAYER_GD = (
    "extends CharacterBody3D\n"
    "## 玩家控制器（行为区）\n"
    "## 水平移动 + 重力 + 跳跃；受击只扣血并刷新 HUD。\n"
    "\n"
    "const GRAVITY: float = 24.0\n"
    "@export var speed: float = 6.0\n"
    "@export var jump_velocity: float = 8.0\n"
    "\n"
    "var _damage_source: Node = null\n"
    "\n"
    "func _physics_process(delta: float) -> void:\n"
    "\tif not is_on_floor():\n"
    "\t\tvelocity.y -= GRAVITY * delta\n"
    "\tif Input.is_action_just_pressed(\"jump\") and is_on_floor():\n"
    "\t\tvelocity.y = jump_velocity\n"
    "\tvar direction := Input.get_vector(\"move_left\", \"move_right\",\n"
    "\t\t\"move_forward\", \"move_back\")\n"
    "\tvelocity.x = direction.x * speed\n"
    "\tvelocity.z = direction.y * speed\n"
    "\tmove_and_slide()\n"
    "\n"
    "func _refresh_hud() -> void:\n"
    "\tif hud != null:\n"
    "\t\thud.show_hp(hp, max_hp)\n"
)


def _clip_to_trace(obs: str) -> str:
    """复刻 `_run_child` 落库时那一步裁剪。"""
    return agent._clip(obs, agent.ORCH_TRACE_OBS_CHARS)


class TheClipLeavesAWholeSmallFileTests(unittest.TestCase):
    def test_the_typical_project_file_survives_intact(self):
        clipped = _clip_to_trace(PLAYER_GD)
        self.assertNotIn("已截断", clipped,
                         "典型项目文件被落库裁剪砍掉了——判据只能看到文件头")

    def test_the_tail_of_the_file_is_still_there(self):
        """模型报出 47/50/51 三个行号，是因为它只看得见开头。"""
        clipped = _clip_to_trace(PLAYER_GD)
        self.assertIn("_refresh_hud", clipped,
                      "文件末尾不在落库里——模型只能自己数行数")

    def test_the_line_the_judge_kept_complaining_about_is_visible(self):
        """判据的原话：「行号（30、31-32、33）在 read_file 观察里并未出现」。"""
        clipped = _clip_to_trace(PLAYER_GD)
        for needle in ("velocity.x = direction.x * speed",
                       "velocity.z = direction.y * speed",
                       "move_and_slide()"):
            self.assertIn(needle, clipped,
                          "%r 不在落库里——判据的指控仍会成立" % needle)


class TheJudgeNowSeesTheWholeFileTests(unittest.TestCase):
    """端到端：落库裁剪 -> 证据块 -> 判据能核验的内容。"""

    def _block(self, obs):
        traj = [{"action": "read_file(path: behaviors/player.gd)",
                 "obs": _clip_to_trace(obs)}]
        return agent._traj_evidence_block(traj)

    def test_the_block_carries_the_whole_file(self):
        block = self._block(PLAYER_GD)
        self.assertIn("_refresh_hud", block)
        self.assertIn("const GRAVITY", block)

    def test_the_block_does_not_claim_truncation_that_did_not_happen(self):
        block = self._block(PLAYER_GD)
        self.assertNotIn("已截断", block,
                         "块里出现截断标记——模型据此会说「被截断」，"
                         "而它其实拿到了全文")

    def test_a_whole_file_read_is_still_declared_as_unnumbered(self):
        """行号那条判据独立于本条：裸正文就是裸正文，不因为给全文而生行号。"""
        block = self._block(PLAYER_GD)
        self.assertIn("不带行号", block,
                      "整文件读取被当成带行号——那是另一个方向的误判")


class TheBudgetIsNotSilentlyShrunkTests(unittest.TestCase):
    """守卫不能跟着改动一起被放宽成「反正测了」。"""

    def test_the_budget_is_at_least_a_typical_small_file(self):
        self.assertGreaterEqual(agent.ORCH_TRACE_OBS_CHARS, 1200,
                                "预算低到连一个典型小文件都装不下——"
                                "判据将永远只能看到文件头")

    def test_the_budget_stays_bounded(self):
        """抬预算不能变成不设防：单步仍有硬上限。"""
        self.assertLessEqual(agent.ORCH_TRACE_OBS_CHARS, 4000,
                             "单步预算无界——6 步 × 无限等于落库无界")

    def test_a_genuinely_huge_observation_is_still_cut(self):
        huge = "x" * 200000
        clipped = _clip_to_trace(huge)
        self.assertLess(len(clipped), agent.ORCH_TRACE_OBS_CHARS + 60)
        self.assertIn("已截断", clipped,
                      "超大观察没有裁剪标记——判据会以为文件真的那么长")


class TheTraceRecordsWhatActuallyRanTests(unittest.TestCase):
    """这是本条的元要求：**判据指控的对象必须真的存在。"""

    def test_each_step_keeps_its_own_observation(self):
        """并行批次下，两步的观察不能串位（同一族的 traj 配对守卫）。"""
        a_obs = "=== a.gd ===\n" + "\n".join("A%02d" % i for i in range(200))
        b_obs = "=== b.gd ===\n" + "\n".join("B%02d" % i for i in range(200))
        steps = [{"action": "read_file(path: a.gd)",
                  "obs": _clip_to_trace(a_obs)},
                 {"action": "read_file(path: b.gd)",
                  "obs": _clip_to_trace(b_obs)}]
        block = agent._traj_evidence_block(steps)
        self.assertIn("A199", block, "a.gd 的尾部丢了")
        self.assertIn("B199", block, "b.gd 的尾部丢了")
        self.assertIn("a.gd", block)
        self.assertIn("b.gd", block)


if __name__ == "__main__":
    unittest.main()