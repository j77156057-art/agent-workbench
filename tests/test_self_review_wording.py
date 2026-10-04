# -*- coding: utf-8 -*-
"""复盘摘要不能把「无法验证」说成「有未通过项」。

## 实测（六次真跑全中）

`workflow_reflection.build_self_review` 旧实现无条件加那半句：

    已完成 3/3 个任务；仍需检查未通过项    非ok任务：无
    已完成 4/4 个任务；仍需检查未通过项    非ok任务：无
    已完成 3/3 个任务；仍需检查未通过项    非ok任务：无
    已完成 4/4 个任务；仍需检查未通过项    非ok任务：无
    已完成 4/4 个任务；仍需检查未通过项    非ok任务：无

`review.project.status` 是 `unverified`，四个原因**全都是「无法验证」**：

    试做区没有容器级命令隔离 / 变更文件超过单轮审核上限
    执行前快照有遗漏 / 项目有变更但未配置独立测试命令

那里没有任何未通过项。而且它和 `uncertainties` 里已经如实记着的
「当前没有足够的独立验证证据」**自相矛盾**。

## 与 24c845c / 3a5f8dc 同一族

同一个被压扁的布尔（`review.ok` = `agent_ok and project.ok`），这是**第三个**
下游消费者。

## 反向守卫

`independent_review.py` 里还有一个 **`failed`** status（「独立测试失败或沙箱不可用」）
——**那才是真的有东西没过**，必须照旧说「仍需检查未通过项」。所以判据是
`status` 而不是 `project.ok`：`ok=False` 有两种完全不同的含义。

另外：`confidence` 那个 −0.2 惩罚**刻意保留**（没独立验证就少一点自信是判断，
不是造谣），由 `test_the_confidence_penalty_is_keptOnPurpose` 钉住。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_runtime.workflow_reflection import build_self_review  # noqa: E402


def _state(*, ok_tasks, review, status="failed", bad_tasks=0):
    results = {}
    for i in range(ok_tasks):
        results["t%d" % i] = {"status": "ok", "conclusion": "结论 %d" % i}
    for i in range(bad_tasks):
        results["bad%d" % i] = {"status": "failed", "error": "boom"}
    total = ok_tasks + bad_tasks
    return {"status": status, "results": results, "review": review,
            "tasks": [{"id": k} for k in results]}


UNVERIFIED_REVIEW = {
    "ok": False, "agent_ok": True, "failures": [],
    "checks": {"has_result": True, "evidence_verified": True},
    "project": {"ok": False, "status": "unverified", "independent": True,
                "change_count": 0, "changes": [], "diff": "",
                "message": "试做区没有容器级命令隔离，不能作为独立测试通过的证据"},
}


class UnverifiedIsNotAFailureTests(unittest.TestCase):
    def test_the_real_run_summary_must_not_invent_failed_items(self):
        sr = build_self_review(_state(ok_tasks=4, review=UNVERIFIED_REVIEW))
        self.assertNotIn("未通过项", sr["summary"],
                         "四任务全 ok 却宣称「仍需检查未通过项」：%r" % sr["summary"])
        self.assertIn("未做独立验证", sr["summary"])
        self.assertIn("4/4", sr["summary"])

    def test_it_still_discloses_the_missing_verification(self):
        """改措辞不等于把安全声明也抹掉——`uncertainties` 里必须仍然在。"""
        sr = build_self_review(_state(ok_tasks=4, review=UNVERIFIED_REVIEW))
        joined = " ".join(sr["uncertainties"])
        self.assertIn("独立验证", joined,
                      "不能说成「未通过」，但也不能当没这回事——独立验证缺失"
                      "必须仍然如实记着")

    def test_the_task_count_is_still_reported(self):
        sr = build_self_review(_state(ok_tasks=3, review=UNVERIFIED_REVIEW))
        self.assertIn("3/3", sr["summary"])


class RealFailuresKeepSayingSoTests(unittest.TestCase):
    """**反向守卫**：真的没过，就必须说过。"""

    def test_project_status_failed_still_reports_failed_items(self):
        review = {"ok": False, "agent_ok": True,
                  "project": {"ok": False, "status": "failed", "independent": True,
                              "message": "独立测试失败或沙箱不可用"}}
        sr = build_self_review(_state(ok_tasks=4, review=review))
        self.assertIn("仍需检查未通过项", sr["summary"],
                      "独立测试真失败了，措辞必须回到「未通过项」")

    def test_agent_ok_false_still_reports_failed_items(self):
        review = {"ok": False, "agent_ok": False,
                  "project": {"ok": False, "status": "unverified"}}
        sr = build_self_review(_state(ok_tasks=1, review=review))
        self.assertIn("仍需检查未通过项", sr["summary"],
                      "内容复核没过时不能被说成「未做独立验证」——那是另一回事")

    def test_project_missing_falls_back_to_failed_wording(self):
        sr = build_self_review(_state(ok_tasks=1, review={"ok": False, "agent_ok": True}))
        self.assertIn("仍需检查未通过项", sr["summary"],
                      "没有 project 这一项时属于未知，不能默认往好的方向说")


class PassingStillSaysPassingTests(unittest.TestCase):
    def test_review_ok_true(self):
        review = {"ok": True, "agent_ok": True,
                  "project": {"ok": True, "status": "passed"}}
        sr = build_self_review(_state(ok_tasks=2, review=review, status="completed"))
        self.assertIn("工作流复核通过", sr["summary"])

    def test_local_mode_project_is_ok(self):
        review = {"ok": True, "agent_ok": True,
                  "project": {"ok": True, "status": "local_mode", "independent": False}}
        sr = build_self_review(_state(ok_tasks=2, review=review, status="completed"))
        self.assertIn("工作流复核通过", sr["summary"])


class TheConfidencePenaltyIsKeptOnPurposeTests(unittest.TestCase):
    """没独立验证就少一点自信 —— 这是**判断**，刻意保留，不跟着一起改。

    第一版这条测试用「4/4 全 ok」，结果两边都是 0.95：`confidence` 先算
    `ok_count/task_count = 1.0`，再 `min(1.0, 1.0 + 0.2)` —— **+0.2 被封顶吃掉**，
    于是那 −0.2 根本观察不到。是测试前提错了，不是实现错。
    改成基准分不满（3 ok / 4 个任务）才看得出差别。
    """

    def test_unverified_loses_the_bonus(self):
        unverified = build_self_review(
            _state(ok_tasks=3, bad_tasks=1, review=UNVERIFIED_REVIEW))
        passed = build_self_review(_state(
            ok_tasks=3, bad_tasks=1, status="completed",
            review={"ok": True, "agent_ok": True,
                    "project": {"ok": True, "status": "passed"}}))
        self.assertLess(unverified["confidence"], passed["confidence"],
                        "unverified 少 0.2 是故意的：没独立验证就该少一点自信")

    def test_the_penalty_is_exactly_the_bonus(self):
        """封顶逻辑别把这 −0.2 变成别的数。"""
        unverified = build_self_review(
            _state(ok_tasks=3, bad_tasks=1, review=UNVERIFIED_REVIEW))
        passed = build_self_review(_state(
            ok_tasks=3, bad_tasks=1, status="completed",
            review={"ok": True, "agent_ok": True,
                    "project": {"ok": True, "status": "passed"}}))
        self.assertAlmostEqual(
            passed["confidence"] - unverified["confidence"], 0.2, places=2)


if __name__ == "__main__":
    unittest.main()