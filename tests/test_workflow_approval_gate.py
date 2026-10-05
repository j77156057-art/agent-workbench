# -*- coding: utf-8 -*-
"""审批门：界面说「等待计划审核」时，点批准必须真的能启动。

## 实测到的两个形状（`.tmp/probe_approve_matrix.py`）

    真实=planned           界面报=planned（等待计划审核）-> 拒绝：当前工作流没有待审核执行
    真实=awaiting_approval  界面报=awaiting_approval      -> 放行

第一条是用户截图里那行 `⚠ 当前工作流没有待审核执行`：**屏幕上写着「等待你批准执行
计划」、按钮就在那里、点下去说没有可批准的东西**。

根因两处：

- `AutonomousCockpit.vue:701` 的 `STATUS_LABEL` 把 `planned` 与 `awaiting_approval`
  **都**标成「等待计划审核」
- 而 2587 行的批准按钮走 `workflowApprove(id, true, true)` -> `approve()`，
  **从不先调 `execute()`**；`planned -> awaiting_approval` 的转换在 `execute()` 里

## 反向守卫

防三种「把门悄悄关回去」：
- 只让报错文案变好看、但**仍拒绝**（用户还是点不动）
- `planned` 能放行、但**顺带跳过了验收条件审批**（把安全门整体拆了）
- 拒绝后**不落状态**（下次启动又变成可批准，用户被反复问）
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime import game_workflow as gw  # noqa: E402


TASKS = [{"id": "research", "title": "调研现状与约束", "depends_on": []},
         {"id": "implement", "title": "实现最小可运行版本", "depends_on": ["research"]}]


def _planned_workflow(mgr):
    """造一个停在 `planned` 的工作流（直接摆状态，不跑规划 —— 这里量的是门禁）。"""
    wid = mgr.start("审批门探针")["workflow_id"]
    state = mgr._load(wid)
    state.status, state.phase = "planned", "execute"
    state.tasks = [dict(t) for t in TASKS]
    state.selected_option = {"id": "workflow", "title": "方案"}
    state.approved = False
    return wid, mgr._save(state)


class ApprovalGateTests(unittest.TestCase):
    def setUp(self):
        # 不用 TemporaryDirectory：langgraph 的 sqlite 句柄要到进程退出才释放，
        # 删目录时 WinError 32。第一版就栽在这儿，输出全被 traceback 盖住。
        import tempfile as _tf
        self.root = _tf.mkdtemp(prefix="wf_approval_gate_")
        self.mgr = gw.GameWorkflowManager(self.root)

    def test_the_ui_labels_planned_as_awaiting_approval(self):
        """守卫的前提：界面确实把 `planned` 标成等待审核。

        这一条**故意只读前端源码**：界面标签一改，下面那条「planned 要能放行」
        就该跟着失效 —— 两边不一致时守卫必须先报出来，而不是默默放过。
        """
        import re
        vue = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "frontend", "src", "workbench", "components",
                           "AutonomousCockpit.vue")
        with open(vue, encoding="utf-8") as handle:
            body = handle.read()
        m = re.search(r"awaiting_approval:\s*'([^']+)'", body)
        self.assertIsNotNone(m, "AutonomousCockpit.vue 里找不到 awaiting_approval 的界面标签")
        self.assertEqual(m.group(1), "等待计划审核")
        self.assertIn("planned:", body,
                      "找不到 planned 的界面标签 —— 若界面改了措辞，请同步更新本守卫")

    def test_approving_from_planned_is_not_refused(self):
        """**反向守卫**：不能只改文案。`planned` 下点批准必须**真的放行**。"""
        wid, _ = _planned_workflow(self.mgr)
        before = self.mgr._load(wid)
        self.assertEqual(before.status, "planned")
        try:
            out = self.mgr.approve(wid, True, auto_execute=False)
        except gw.WorkflowError as exc:
            self.fail("planned 下点批准被拒：%s（界面却标着「等待计划审核」）" % exc)
        self.assertTrue(out)

    def test_promoting_to_the_gate_records_why_it_stopped(self):
        """推上审批门时必须写 `interrupt_reason` —— 那是用户看到的「为什么停在这儿」。"""
        wid, _ = _planned_workflow(self.mgr)
        self.mgr._execution_callbacks[wid] = {}
        state = self.mgr._load(wid)
        state.status = "planned"
        self.mgr._save(state)
        promoted = self.mgr._promote_to_approval_gate(wid, state)
        self.assertEqual(promoted.status, "awaiting_approval")
        self.assertTrue(promoted.interrupt_reason,
                        "推上审批门却没写原因，界面上就只剩一个没有解释的「等待审核」")

    def test_the_refusal_message_says_where_it_is_and_what_to_do(self):
        """报错要说清**现在在哪、下一步做什么**。

        原先那句「当前工作流没有待审核执行」只会让人盯着屏幕猜 —— 而界面上还写着
        「等待你批准执行计划」，两句话互相矛盾。
        """
        wid = self.mgr.start("审批门探针2")["workflow_id"]
        state = self.mgr._load(wid)
        state.status, state.phase = "executing", "execute"
        state.tasks = [dict(t) for t in TASKS]
        self.mgr._save(state)
        with self.assertRaises(gw.WorkflowError) as caught:
            self.mgr.approve(wid, True, auto_execute=False)
        message = str(caught.exception)
        self.assertIn("executing", message,
                      "报错里必须说清当前处于什么状态：%r" % message)
        self.assertNotIn("没有待审核执行", message,
                         "这句会被读成「界面骗我」，且不含任何下一步")
        self.assertTrue("启动" in message or "继续" in message,
                        "报错必须给出可执行的下一步：%r" % message)

    def test_approval_still_approves_the_acceptance_contract(self):
        """**反向守卫**：让 `planned` 能放行，**不等于**拆掉验收条件审批。

        这是安全门：模型生成的验收条件不能自己批准自己（`approve_acceptance`）。
        修门禁时最容易顺手把它一起放过去。
        """
        wid, _ = _planned_workflow(self.mgr)
        self.mgr.set_acceptance_contract(wid, [
            # 字段是 `statement` 不是 `text` —— 第一版写成 `text`，被
            # AcceptanceError「表述不可为空」打回。那是我的输入错了，不是实现错了。
            {"id": "a1", "statement": "任务 research 产出真实运行记录", "required": True},
        ])
        state = self.mgr._load(wid)
        self.assertTrue(state.acceptance_contract.get("items"))
        before = [dict(i) for i in state.acceptance_contract["items"]]
        try:
            self.mgr.approve(wid, True, auto_execute=False)
        except gw.WorkflowError:
            pass  # 门禁本身怎么走都不重要，下面验验收条件有没有被绕过
        after = self.mgr._load(wid).acceptance_contract.get("items") or []
        self.assertTrue(after, "验收条件不见了 —— 批准不该清空它")
        self.assertEqual(after[0].get("id"), before[0].get("id"))
        # 批准之后它必须被标成**已批准**（而不是仍然 pending/user 未批）
        self.assertTrue(any(i.get("user_approved") or i.get("approved") for i in after),
                        "验收条件没被记为已批准：%r" % (after,))

    def test_a_high_autonomy_policy_is_not_forced_through_a_gate(self):
        """高自主模式本来没有审批门，`_promote_to_approval_gate` 不得把它按停在门上。"""
        wid, _ = _planned_workflow(self.mgr)
        state = self.mgr._load(wid)
        state.policy = dict(state.policy or {})
        state.policy["approval_mode"] = "high"
        self.mgr._save(state)
        promoted = self.mgr._promote_to_approval_gate(wid, self.mgr._load(wid))
        self.assertNotEqual(promoted.status, "awaiting_approval",
                            "高自主模式被按停在审批门上了")


if __name__ == "__main__":
    unittest.main()