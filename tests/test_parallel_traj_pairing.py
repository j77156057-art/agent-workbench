# -*- coding: utf-8 -*-
r"""并行工具批次下，`_run_child` 的 action/observation 配对错位。

## 实测依据（不是设想）

真实工作流 `wf-2097b5fbf7c1`（跑在 godot_sample 上）的落库 trace 里，动作和
观察对不上：

| 动作 | 实际观察到的内容 |
|---|---|
| `read_file(path: regions.json)` | `.docmind\web\index.audio.position.worklet.js:31: class ...` —— 这是上一步 `grep` 的结果 |
| `read_file(path: values/level.gd)` | `=== behaviors\\player.gd === extends CharacterBody3D` —— 这是前面某一步的结果 |
| `list_dir(path: ui)` | `目录 (代码库根) 共 20 项：` —— 这是 `list_dir(.)` 的结果 |

## 原因

`agent.py:3211-3219`（并行批次路径）**先 yield 全部 action，再 yield 全部
observation**：

    for ...: yield {"type": "action", ...}
    for ...: yield {"type": "observation", ...}

而 `_run_child`（`agent.py:4580`）的 `pending` 只有**一个槽**。批次里第二个
action 把第一个顶掉，第一个 observation 填进第二个 pending，第二个被
`not pending["obs"]` 丢掉。

## 为什么这不是「记录不好看」

`traj` 会喂给 `_traj_evidence_block` → `_reflection_result`。也就是说
**反思是拿一份动作与观察错配的轨迹，在判断该不该重试这个子代理**。

实测后果：子代理报告「所有读取都返回空、无法核实」并放弃，而它自己那轮的
真实观察其实是有的。父代理据此判定失败、replan、再派一次 —— 每次都基于
同一份错配的证据。真实那一轮就是这样连错三次的。

## 这条测试量的就是真代码路径

第一版我写的是「复刻一份配对逻辑再回放」——量的是副本，不是 `agent.py` 里
那条真实路径，正是本会话已经犯过一次的错（端到端测试调 `_run_child` 却忘了
接 `step_sink`，量了一条没人走的路）。现在改成 patch `Agent.run` 回放事件，
让 `_run_child` 自己的配对代码去处理。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import agent  # noqa: E402


class _ScriptedLLM:
    """只为了能构造 Agent；事件由 patch 后的 `Agent.run` 提供。"""

    provider = "fake"
    model = "x"
    last_usage = {}
    last_tool_calls = []

    def clone(self):
        return self

    def chat(self, messages, stream=True, **kw):
        return iter([{"role": "assistant", "content": "Final Answer: 好了。"}])

    def count_tokens(self, text):
        return 1


def _steps_from(events, role="researcher"):
    """回放 `events`，返回真 `_run_child` 落下的 traj 步骤。

    patch 的是 `Agent.run` —— `_run_child` 内部自己会建子 Agent 并调它的
    `run`，所以这里替掉整个 run 的产出，剩下的配对逻辑全是真代码。
    """
    orig_run = agent.Agent.run

    def fake_run(self, question, stream=False):
        for ev in events:
            yield dict(ev)

    agent.Agent.run = fake_run
    try:
        out = agent.Agent(llm=_ScriptedLLM())._run_child(
            role, "查一下", reflect=False)
    finally:
        agent.Agent.run = orig_run
    return out["trace"]["steps"]


def _batch(names, obs_texts):
    """并行批次的真实形状：N 个 action 连着来，然后 N 个 observation。"""
    evs = [{"type": "action", "text": "%s()" % n, "step_cost": 1} for n in names]
    evs += [{"type": "observation", "text": t} for t in obs_texts]
    return evs


class ParallelBatchPairingTests(unittest.TestCase):
    def test_a_batch_of_two_pairs_each_observation_with_its_own_action(self):
        steps = _steps_from(_batch(["grep", "read_file"], ["GREP_OUT", "READ_OUT"]))
        self.assertEqual([s["action"] for s in steps], ["grep()", "read_file()"])
        self.assertEqual(
            [s["obs"] for s in steps], ["GREP_OUT", "READ_OUT"],
            "观察错配到别的动作上了——真实 trace 里表现为"
            "「read_file 拿到 grep 的结果」")

    def test_a_batch_of_three(self):
        steps = _steps_from(_batch(["a", "b", "c"], ["OA", "OB", "OC"]))
        self.assertEqual([s["obs"] for s in steps], ["OA", "OB", "OC"])

    def test_a_batch_of_four_is_the_configured_parallel_max(self):
        names = ["n%d" % i for i in range(agent.PARALLEL_MAX)]
        texts = ["ON%d" % i for i in range(agent.PARALLEL_MAX)]
        steps = _steps_from(_batch(names, texts))
        self.assertEqual([s["obs"] for s in steps], texts,
                         "并发上限是 %d，这条覆盖满批" % agent.PARALLEL_MAX)

    def test_the_sequential_shape_still_pairs(self):
        """顺序路径是 action/observation 交替，不能被改坏。"""
        events = []
        for nm, ob in (("grep", "O1"), ("read_file", "O2"), ("list_dir", "O3")):
            events.append({"type": "action", "text": nm, "step_cost": 1})
            events.append({"type": "observation", "text": ob})
        steps = _steps_from(events)
        self.assertEqual([s["obs"] for s in steps], ["O1", "O2", "O3"])

    def test_mixed_sequential_then_batch(self):
        events = [
            {"type": "action", "text": "s1", "step_cost": 1},
            {"type": "observation", "text": "OS1"},
            {"type": "action", "text": "x1", "step_cost": 1},
            {"type": "action", "text": "x2", "step_cost": 1},
            {"type": "observation", "text": "OX1"},
            {"type": "observation", "text": "OX2"},
        ]
        steps = _steps_from(events)
        self.assertEqual([s["action"] for s in steps], ["s1", "x1", "x2"])
        self.assertEqual([s["obs"] for s in steps], ["OS1", "OX1", "OX2"])

    def test_two_batches_in_a_row(self):
        events = (_batch(["a1", "a2"], ["OA1", "OA2"])
                  + _batch(["b1", "b2"], ["OB1", "OB2"]))
        steps = _steps_from(events)
        self.assertEqual([s["obs"] for s in steps], ["OA1", "OA2", "OB1", "OB2"],
                         "第二个批次的观察不能被第一个批次残留的 pending 吃掉")

    def test_a_batch_larger_than_the_trace_cap_still_pairs_the_kept_ones(self):
        """超限的步骤不记，但**不能抢**前面已记步骤的观察。

        队列在这里必须是 FIFO：如果按「最近一个 pending」填，超限步骤的观察
        会落到最后一个保留步骤上。
        """
        n = agent.ORCH_TRACE_STEPS
        names = ["t%d" % i for i in range(n + 2)]
        texts = ["OT%d" % i for i in range(n + 2)]
        steps = _steps_from(_batch(names, texts))
        self.assertEqual(len(steps), n, "超出上限的步骤不该被累积")
        self.assertEqual([s["obs"] for s in steps], texts[:n],
                         "超限步骤的观察被塞进了保留步骤里")


class RealRunShapeTests(unittest.TestCase):
    """把 `wf-2097b5fbf7c1` 里观察到的那个形状钉下来当回归。"""

    def test_the_real_shape_pairs_correctly(self):
        steps = _steps_from([
            {"type": "action", "text": "grep(pattern: class_name|extends )",
             "step_cost": 1},
            {"type": "action", "text": "read_file(path: project.godot)",
             "step_cost": 1},
            {"type": "observation", "text": "GREP_MATCHES"},
            {"type": "observation", "text": ""},
        ])
        by_action = {s["action"]: s["obs"] for s in steps}
        self.assertEqual(by_action["grep(pattern: class_name|extends )"],
                         "GREP_MATCHES")
        self.assertEqual(by_action["read_file(path: project.godot)"], "",
                         "grep 的观察被安到了 read_file 头上——"
                         "父代理据此以为读取真的失败了")


class ReflectionSeesTheRightEvidenceTests(unittest.TestCase):
    """这条最要紧：**反思的判据**必须拿到配对正确的轨迹。"""

    def test_the_evidence_block_shows_each_action_with_its_own_observation(self):
        steps = _steps_from(_batch(
            ["grep", "read_file"], ["GREP_FOUND_TWO_MATCHES", "FILE_HAS_80_LINES"]))
        block = agent._traj_evidence_block(steps)
        self.assertIn("GREP_FOUND_TWO_MATCHES", block)
        self.assertIn("FILE_HAS_80_LINES", block)
        # 证据块是「- 动作」一行 + 「  观察：…」一行的**成对**结构，
        # 所以要按相邻行配对检查，不能只看同一行。
        lines = block.splitlines()
        pairs = {}
        for i, line in enumerate(lines):
            if line.startswith("- ") and i + 1 < len(lines):
                pairs[line[2:].split()[0]] = lines[i + 1]
        self.assertIn("GREP_FOUND_TWO_MATCHES", pairs.get("grep()", ""),
                      "grep 那步挂着别的工具的观察")
        self.assertIn("FILE_HAS_80_LINES", pairs.get("read_file()", ""),
                      "read_file 那步挂着别的工具的观察")


if __name__ == "__main__":
    unittest.main()