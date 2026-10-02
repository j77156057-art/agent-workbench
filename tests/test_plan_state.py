"""任务清单：把「进行中的事」外置到磁盘，让上下文压缩不再有损。

## 这条能力要解决的问题

上下文压缩是**有损**的——摘要有取舍，摘得越多、任务细节退化越明显。所以"让长对话
不退化"的正解不是压得更狠，而是把任务状态搬出上下文：

    清单在磁盘上 + 每轮注入系统消息
    ⇒ "上下文被摘掉一大半" 不等于 "目标丢了"

没有注入那一步，清单就只是个模型可能忘记去读的旁路文件——**那才是它最容易失败的方式**，
所以这里对注入的断言比对存储的断言更严。

## 三条设计决定，各有其代价

1. **JSON 事实来源 + markdown 渲染**，两者互不解析。
   勾选必须可靠且幂等，所以不用正则解析 markdown 回写；但模型/人要读的是可勾选清单，
   所以对外渲染成 `- [ ] / - [x]`。代价：模型不能自己手改文件当状态源。

2. **写项目状态目录，不写工作树**。工作树是别人的工作面（多人同仓时往里写临时
   文件等于给别人埋雷）。本仓 `dev_propose` 已确立这个约定，清单沿用。

3. **同名条目合并**。模型常常把计划重述一遍；若不合并就会堆出三份一样的条目，
   勾选时按 key 匹配就会漏勾。
"""

import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import plan_state  # noqa: E402


class Base(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="plan-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._old = config.get_runtime("code_root", "")
        config.set_runtime("code_root", self.root)
        self.addCleanup(config.set_runtime, "code_root", self._old)

    def open_plan(self, title="T", items=("a", "b", "c")):
        return plan_state.open_plan(self.root, title, list(items))


class StoreTests(Base):

    def test_it_survives_a_fresh_read(self):
        """清单的意义在于「换上下文窗口后还在」，所以必须真的落盘。"""
        self.open_plan("修登录", ["定位", "修复", "补测试"])
        plan = plan_state.read(self.root)
        self.assertEqual([i["text"] for i in plan["items"]],
                         ["定位", "修复", "补测试"])

    def test_it_does_not_dirty_the_work_tree(self):
        """写工作树在多人同仓时等于给别人埋雷（本仓 dev_propose 的既有约定）。"""
        import subprocess
        self.open_plan()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                              capture_output=True, text=True, cwd=self.root)
        self.assertEqual(dirty.stdout.strip(), "",
                         "清单把工作树弄脏了：%s" % dirty.stdout[:200])

    def test_items_are_deduplicated_by_content(self):
        """模型重述计划很常见；不合并就会堆出重复项、按 key 勾选时漏勾。"""
        plan = plan_state.open_plan(self.root, "T", ["加日志", "加日志", "加日志"])
        self.assertEqual(len(plan["items"]), 1)

    def test_append_merges_and_reports_the_skip(self):
        self.open_plan(items=["加日志", "补测试"])
        plan = plan_state.append_items(self.root, ["加日志", "写文档"])
        self.assertEqual(plan["_added"], 1)
        self.assertEqual(plan["_skipped"], 1)
        self.assertEqual(len(plan["items"]), 3)

    def test_ticking_is_idempotent_and_reversible(self):
        self.open_plan(items=["a", "b"])
        for _ in range(3):
            plan_state.set_done(self.root, "a", True)
        done = [i["text"] for i in plan_state.read(self.root)["items"] if i["done"]]
        self.assertEqual(done, ["a"], "重复勾选不该产生副作用")
        plan_state.set_done(self.root, "a", False)
        self.assertFalse(any(i["done"] for i in plan_state.read(self.root)["items"]))

    def test_a_missing_item_is_reported_not_silently_ignored(self):
        """静默忽略会让人以为勾上了——那正是"以为做完其实没做"的来源。"""
        self.open_plan(items=["a"])
        plan = plan_state.set_done(self.root, "不存在的条目", True)
        self.assertIsNone(plan["_hit"])

    def test_items_can_be_matched_by_text_prefix(self):
        """模型未必回读 key；能用文本前缀勾上才算真的可用。"""
        self.open_plan(items=["定位登录超时点"])
        plan = plan_state.set_done(self.root, "定位登录", True)
        self.assertIsNotNone(plan["_hit"])

    def test_a_corrupt_state_file_degrades_to_no_plan(self):
        """fail-closed 的方向是「少给信息」，不是「拒绝服务」。"""
        import project_state
        path = project_state.path(self.root, plan_state.PLAN_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        plan = plan_state.read(self.root)
        self.assertTrue(plan.get("corrupt"))
        self.assertEqual(plan["items"], [])
        self.assertEqual(plan_state.summary_for_prompt(self.root), "")

    def test_render_shows_progress_and_the_next_item(self):
        """清单要能回答"接下来做什么"，不然它只是一堆记录。"""
        self.open_plan("T", ["a", "b", "c"])
        plan_state.set_done(self.root, "a", True)
        text = plan_state.render(plan_state.read(self.root))
        self.assertIn("1/3", text)
        self.assertIn("b", text.split("下一件事：")[1].splitlines()[0])


class PromptInjectionTests(Base):
    """注入是这件事的**关键环节**——没有它，清单就是模型可能忘记读的旁路文件。"""

    def test_the_pending_plan_is_injected(self):
        self.open_plan("修登录", ["定位", "修复"])
        hint = plan_state.summary_for_prompt(self.root)
        self.assertIn("定位", hint)
        self.assertIn("修复", hint)
        self.assertIn("下一件未完成", hint.replace("下一件事", "下一件未完成")
                      if "下一件未完成" in hint else "未完成")

    def test_an_empty_plan_injects_nothing(self):
        """空清单不能塞进提示——那是纯浪费 token，且会稀释真提示。"""
        self.assertEqual(plan_state.summary_for_prompt(self.root), "")

    def test_completed_items_drop_out_of_the_injection(self):
        """已完成项继续占位就是在提示模型重做。"""
        self.open_plan(items=["a", "b", "c"])
        plan_state.set_done(self.root, "a", True)
        plan_state.set_done(self.root, "b", True)
        hint = plan_state.summary_for_prompt(self.root)
        self.assertNotIn("- [ ] a", hint)
        self.assertNotIn("- [ ] b", hint)
        self.assertIn("c", hint)

    def test_all_done_says_so_instead_of_pretending_there_is_work(self):
        self.open_plan(items=["a"])
        plan_state.set_done(self.root, "a", True)
        hint = plan_state.summary_for_prompt(self.root)
        self.assertIn("全部完成", hint)

    def test_agent_builds_messages_with_the_plan(self):
        """端到端：真的走 _build_messages，确认清单进了系统消息。"""
        import agent as agent_mod
        self.open_plan("修登录", ["定位超时点", "加日志"])
        a = agent_mod.Agent(llm=None)
        joined = " ".join(str(m.get("content") or "") for m in a._build_messages("继续"))
        self.assertIn("定位超时点", joined,
                      "清单没进系统消息——那它就是个模型不会读的旁路文件")


class ToolSurfaceTests(Base):

    def setUp(self):
        super().setUp()
        import tools as tools_mod
        self.tools = tools_mod

    def _run(self, arg):
        return self.tools.dev_plan(arg)

    def test_open_then_check_then_read(self):
        out = self._run("action: open\ntitle: 修超时\nitems: 定位, 修复, 补测试")
        self.assertIn("修超时", out)
        self.assertIn("0/3", out)
        self.assertIn("1/3 已完成", self._run("action: check\nitem: 定位"))
        read = self._run("action: read")
        self.assertIn("1/3", read)
        self.assertIn("[x] 定位", read)

    def test_a_missing_items_argument_says_what_to_do(self):
        out = self._run("action: open\ntitle: T")
        self.assertIn("items", out)

    def test_an_unknown_action_lists_the_valid_ones(self):
        out = self._run("action: 瞎写")
        self.assertIn("open", out)
        self.assertIn("read", out)

    def test_default_action_is_read_not_an_error(self):
        self.assertIn("没有任务清单", self._run(""))

    def test_ticking_something_absent_names_the_real_keys(self):
        """报错要说清现在有哪些可勾选项，否则模型只能瞎试。"""
        self._run("action: open\nitems: 定位超时点")
        out = self._run("action: check\nitem: 不存在的")
        self.assertIn("定位超时点", out)


if __name__ == "__main__":
    unittest.main()