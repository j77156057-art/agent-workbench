# -*- coding: utf-8 -*-
"""会话级代码库隔离：换任务不该让检索器留在上一个任务的代码里。

## 实测事故（2026-10-10，一次工作流 5 个子任务 0/5 全败）

mean 题那次的探针目录被设成**全局** `code_root`（`.docmind_state.json` 顶层）。
四天后在同一项目里开新会话问「优化界面」，中间件某一环没设成上下文
-> `get_runtime("code_root")` 静默回落到那个全局值
-> research 在 mean 题的 `calc.py` 底下搜 `mean`
-> 3 轮 research 全败，implement/verify 连带 blocked。

**会话隔离是做到了的**（不同 `session_id`、不同文件）。没做到的是**代码库隔离**
—— 因为代码库从来没「跟着会话走」这个概念，它只挂在项目/全局上。

详见 `docs/known-defect-code-root-scope-too-coarse.md`。

## 契约

    get_runtime("code_root") 的优先级：**会话 > 项目 > 全局**

* 新会话首次落盘时，把**当时**的代码库记进会话文件
* 之后该会话一直用它 —— **切项目也不改**（会话是更窄的作用域）
* 老会话没这个字段 -> 读空串 -> 回落项目/全局，**行为与改动前完全一致**

## ⚠ 这套判据是在两次真跑失败后才对齐的

第一版让新会话绑定时读 `get_runtime("code_root")` —— 而那个函数的第一优先级
就是会话级，于是同一进程内连续构造 Agent 时：

    建 s1（项目 A）-> 会话级 = A
    建 s2（项目 B）-> 会话级 = A   *** 第二个会话继承了第一个的库 ***

比原缺陷更隐蔽。现在新会话绑定读的是 `get_runtime_excluding_session`
（只按 项目 > 全局），`test_no_session_leaks_into_the_next_agent` 钉住这条。
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ⚠ 会话是**落盘**的（.docmind_sessions/）。必须在 import config 之前把状态目录
# 指到沙箱，否则测试会污染用户真实会话数据。
_SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="docmind_sessiso_"))
os.environ["DOCMIND_STATE_DIR"] = str(_SANDBOX)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod          # noqa: E402
import config                      # noqa: E402
import sessions                    # noqa: E402


class _FakeLLM:
    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, enable_thinking=None, **kw):
        return iter(["Final Answer: ok"])


def tearDownModule():
    shutil.rmtree(_SANDBOX, ignore_errors=True)


class SessionScopedCodeRootTests(unittest.TestCase):
    """`get_runtime("code_root")` 必须是 会话 > 项目 > 全局。"""

    def setUp(self):
        self.proj_a = _SANDBOX / "projA"
        self.proj_b = _SANDBOX / "projB"
        for p in (self.proj_a, self.proj_b):
            p.mkdir(parents=True, exist_ok=True)
        # ⚠⚠ **两个 contextvar 都必须存-还**，否则本文件会把值漏给后面跑的文件
        # （实测 `test_project_routing` 因此红：它读到了本文件的临时目录）。
        # 我第一次只给**它**补了清理、自己这份没补 —— 结果它照样红。
        # 这类「加了一层上下文」的状态，凡经手它的测试都必须自己清干净。
        self._old_proj_ctx = config._CTX_CODE_ROOT.get()
        self._old_sess_ctx = config._CTX_SESSION_CODE_ROOT.get()
        config._CTX_CODE_ROOT.set(None)
        config._CTX_SESSION_CODE_ROOT.set(None)

    def tearDown(self):
        config._CTX_CODE_ROOT.set(self._old_proj_ctx)
        config._CTX_SESSION_CODE_ROOT.set(self._old_sess_ctx)

    def _make(self, session_id, project_root):
        """按指定项目 root 建 Agent（模拟一次请求绑定了某个项目）。"""
        config.set_runtime("code_root", str(project_root))
        config.set_context_code_root(str(project_root))
        return agent_mod.Agent(llm=_FakeLLM(), session_id=session_id,
                               project_id="p-test")

    # ---------- 隔离的核心 ----------

    def test_a_new_session_records_the_code_root_it_started_with(self):
        self._make("s-new", self.proj_a)
        self.assertEqual(sessions.current_code_root("s-new", "p-test"),
                         str(self.proj_a),
                         "新会话没记住建立时的代码库 —— 下次回落就又串了")

    def test_inside_a_turn_switching_projects_does_not_move_that_session(self):
        """**回合内**隔离：切项目不该让进行中的对话换库。

        ⚠ 会话级由 `run()` 按回合 set，所以必须在**回合内**断言 ——
        构造之后、run 之前那段时间按设计不生效（回合外的一切都不该被
        这个会话的代码库影响，那正是缺陷本身）。
        """
        a = self._make("s-bound", self.proj_a)
        seen = {}

        def _probe(messages, stream=True, enable_thinking=None, **kw):
            # 在模型调用点读一次 —— 这正处于回合内
            seen["root"] = config.get_runtime("code_root")
            return iter(["Final Answer: ok"])

        a.llm = type("L", (), {"count_tokens": lambda s, t: 1,
                               "chat": staticmethod(_probe)})()
        # 回合进行中把项目切到 B
        config.set_context_code_root(str(self.proj_b))
        list(a.run("随便问点什么", stream=True))
        self.assertEqual(seen.get("root"), str(self.proj_a),
                         "回合内切项目把该会话的代码库顶掉了 —— "
                         "这正是原缺陷的形态")

    def test_outside_a_turn_the_session_root_does_not_apply(self):
        """**回合外**不生效：否则值会留在进程里污染后来的一切。

        实测后果：`tests/test_mcp_self_assembly_tools.py` 构造 Agent 时
        把会话级挂上而它的 tearDown 只还原 `_RUNTIME` -> 值留在进程里 ->
        本轮全量 **93 条**测试读到别的测试的临时目录。
        """
        self._make("s-outside", self.proj_a)          # 会话绑到 projA
        config.set_context_code_root(str(self.proj_b))  # 项目切到 projB
        self.assertEqual(
            config.get_runtime("code_root"), str(self.proj_b),
            "回合外会话级仍在生效 —— 它只该活一个回合")

    def test_constructing_an_agent_leaves_no_session_context(self):
        """反向守卫：**构造** Agent 绝不许留下非空会话级。

        这是那次 93 条失败的直接锁。构造期只取值并落盘（`self._session_code_root`
        + `sessions.bind_code_root`），绑定由 `run()` 负责。
        """
        before = config._CTX_SESSION_CODE_ROOT.get()
        self._make("s-nocontext", self.proj_a)
        self.assertEqual(
            config._CTX_SESSION_CODE_ROOT.get(), before,
            "构造 Agent 时设置了会话级 contextvar —— 它没有任何 reset 点，"
            "会留在整个进程里")

    def test_a_turn_restores_the_context_even_on_exception(self):
        """回合结束（含异常/断连）必须还原 —— 否则同样泄漏。"""
        self._make("s-finally", self.proj_a)
        before = config._CTX_SESSION_CODE_ROOT.get()

        a = self._make("s-finally", self.proj_a)
        a.llm = type("L", (), {
            "count_tokens": lambda s, t: 1,
            "chat": staticmethod(lambda *a_, **k: (_ for _ in ()).throw(
                RuntimeError("boom"))),
        })()
        with self.assertRaises(Exception):
            list(a.run("会炸的回合", stream=True))
        self.assertEqual(config._CTX_SESSION_CODE_ROOT.get(), before,
                         "回合抛异常后会话级没还原")

    def test_the_binding_is_written_once_not_refreshed(self):
        """绑一次终身沿用；若每轮跟着项目变，等于没隔离。"""
        self._make("s-once", self.proj_a)
        self._make("s-once", self.proj_b)
        self._make("s-once", self.proj_a)
        self.assertEqual(sessions.current_code_root("s-once", "p-test"),
                         str(self.proj_a))

    # ---------- 泄漏（第一版真跑抓到的）----------

    def test_no_session_leaks_into_the_next_agent(self):
        """同进程内连续建 Agent 时，**后一个不能继承前一个的库**。

        ⚠ 第一版让新会话绑定时读 `get_runtime`，而它第一优先级就是会话级，
        于是 s2 拿到了 s1 的 A —— 比原缺陷更隐蔽。现在绑定读的是
        `get_runtime_excluding_session`。
        """
        self._make("s-leak-1", self.proj_a)
        self._make("s-leak-2", self.proj_b)
        self.assertEqual(sessions.current_code_root("s-leak-2", "p-test"),
                         str(self.proj_b),
                         "第二个会话继承了第一个会话的代码库 —— "
                         "新会话绑定时读了含会话级的 get_runtime")
        self.assertEqual(config.get_runtime("code_root"), str(self.proj_b))

    def test_bind_code_root_refuses_to_rebind_a_bound_session(self):
        """**直接**打 `bind_code_root`：已绑定的会话必须拒绝改绑。

        ⚠ 这条是变异实测逼出来的：把 `bind_code_root` 里「已绑定就不动」
        那两行删掉（改成每次刷新），**10 条测试全绿** —— 恒真。

        为什么原有测试抓不到：它们都走 `Agent(...)` 构造路径，
        而构造里只对**新会话**调 bind_code_root；已绑定会话走的是
        「已有绑定 -> 直接 set_ctx」分支，**根本没调**那个函数。
        所以它的「已绑定就不动」从未被执行过。
        """
        self.assertTrue(sessions.bind_code_root("s-rebind", str(self.proj_a),
                                                "p-test"),
                        "首次绑定应当成功")
        self.assertEqual(sessions.current_code_root("s-rebind", "p-test"),
                         str(self.proj_a))

        self.assertFalse(
            sessions.bind_code_root("s-rebind", str(self.proj_b), "p-test"),
            "bind_code_root 对已绑定会话返回了 True —— 它应当拒绝改绑")
        self.assertEqual(
            sessions.current_code_root("s-rebind", "p-test"),
            str(self.proj_a),
            "已绑定的会话被改绑了 —— 绑一次终身沿用这条被破坏，"
            "于是切项目就能顶掉进行中的对话所用的库")

    # ---------- 兼容性 ----------

    def test_a_legacy_session_without_the_field_behaves_as_before(self):
        """老会话读不到这个字段 -> 回落项目，**行为与改动前一致**。"""
        path = sessions._path("s-legacy", "p-test")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"session_id": "s-legacy",
                       "updated_at": "2026-01-01T00:00:00",
                       "summary": "",
                       "turns": [{"user": "旧问题", "assistant": "旧答案"}],
                       "snapshots": []}, fh, ensure_ascii=False)
        self.assertEqual(sessions.current_code_root("s-legacy", "p-test"), "",
                         "老会话应返回空串，让调用方回落")
        self._make("s-legacy", self.proj_b)
        self.assertEqual(config.get_runtime("code_root"), str(self.proj_b),
                         "老会话应按当前项目解析，与改动前一致")

    def test_binding_a_legacy_session_keeps_its_history_intact(self):
        """老会话**会被绑定**（正确），但**一个字的用户数据都不能动**。

        ⚠ 我第一版把这里的期望写反了：断言「老会话不该被写入 code_root」，
        理由是「避免行为突变」。**那是错的** —— 老会话不绑定就永远回落全局，
        那正是原缺陷本身。

        真正该防的是**静默改写用户历史**：绑定是**追加一个字段**，
        turns / summary / snapshots 必须是原样。
        """
        path = sessions._path("s-nowrite", "p-test")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        original = {"session_id": "s-nowrite", "updated_at": "2026-01-01T00:00:00",
                    "summary": "旧摘要",
                    "turns": [{"user": "q1", "assistant": "a1"},
                              {"user": "q2", "assistant": "a2"}],
                    "snapshots": [{"id": "s1"}]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(original, fh, ensure_ascii=False)

        self._make("s-nowrite", self.proj_a)

        after = json.load(open(path, encoding="utf-8"))
        self.assertEqual(after.get("code_root"), str(self.proj_a),
                         "老会话没被绑定 —— 它会永远回落全局，原缺陷仍在")
        for key in ("turns", "summary", "snapshots", "session_id"):
            self.assertEqual(after.get(key), original[key],
                             "绑定代码库时**改动了 %s** —— 那是用户数据，"
                             "只能追加字段，不能改写" % key)

    # ---------- 优先级 ----------

    def test_session_beats_project_and_global(self):
        config.set_runtime("code_root", str(_SANDBOX / "GLOBAL"))
        self._make("s-prio", self.proj_a)
        self.assertEqual(config.get_runtime("code_root"), str(self.proj_a),
                         "项目应盖住全局")

    def test_project_beats_global_when_no_session_binding(self):
        """没有会话绑定时，行为与改动前一致：项目 > 全局。"""
        config.set_runtime("code_root", str(_SANDBOX / "GLOBAL"))
        config.set_context_code_root(str(self.proj_b))
        token = config.set_ctx_session_code_root(None)   # 确保无会话级
        try:
            self.assertEqual(config.get_runtime("code_root"), str(self.proj_b))
        finally:
            config.reset_ctx_session_code_root(token)


class ReversalGuardTests(unittest.TestCase):
    """反向守卫：别把这次修复删回去。"""

    def test_get_runtime_really_has_three_levels(self):
        """判据不是「某个函数返回了某个值」，而是**优先级结构**本身。

        断言 `get_runtime` 的源码里真的按 会话 -> 项目 -> 全大 顺序取，
        且含 `get_runtime_excluding_session` 这个**不含会话级**的旁路 ——
        后者是防泄漏的关键，删了它会让第二个会话继承第一个的库。
        """
        import inspect
        src = inspect.getsource(config.get_runtime)
        self.assertIn("_CTX_SESSION_CODE_ROOT", src,
                      "get_runtime 不再看会话级 -> 隔离失效")
        self.assertIn("_CTX_CODE_ROOT", src,
                      "项目级那一层不见了")
        # 顺序：会话级必须**先于**项目级被查
        self.assertLess(src.index("_CTX_SESSION_CODE_ROOT"),
                        src.index("_CTX_CODE_ROOT.get()"),
                        "优先级反了：项目级先于会话级 -> 切项目会顶掉会话绑定")
        self.assertTrue(
            hasattr(config, "get_runtime_excluding_session"),
            "缺少 get_runtime_excluding_session —— 新会话绑定时会读到"
            "上一个会话泄漏下来的值")

    def test_agent_binds_with_the_session_excluding_reader(self):
        """Agent 构造时必须用「不含会话级」的取值来绑定。"""
        import inspect
        src = inspect.getsource(agent_mod.Agent.__init__)
        self.assertIn("current_code_root", src,
                      "Agent 不再读会话记住的代码库")
        self.assertIn("_get_runtime_excl_session", src,
                      "新会话绑定时用了含会话级的 get_runtime -> 同进程内"
                      "后建的会话会继承前一个的库")
        self.assertIn("bind_code_root", src, "新会话没有绑定代码库")


if __name__ == "__main__":
    unittest.main()
