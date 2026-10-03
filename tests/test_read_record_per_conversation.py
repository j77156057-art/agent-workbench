# -*- coding: utf-8 -*-
"""读记录必须按**对话**隔离，否则重试在结构上不可能成功。

## 实测（`wf-3073fc5f2403`，连错三轮的真正原因）

`research` 子代理读完 `behaviors/enemy.gd` 全文后被反思判失败。重规划产生的
`research_behaviors_r2` 是一个**全新上下文**的子代理，它再读同一文件拿到的是：

    === behaviors\\enemy.gd（未变化）===
    整份文件已在本次对话中读过、且此后未改动；直接引用那次结果。

**那份内容在上一���子代理的上下文里，它压根没见过。** 而文件确实没被改过，
所以存根必然触发 —— 于是每一次重试都必然拿不到正文，那个重试在结构上不可能
成功，界面上只看得到「子代理失败」。

反思反复报的判据原文：「`read_file` 观察仅返回『已读过、不要重复读取』的缓存提示，
没有展示正文」。

## 这与「任务要写文件却派给只读角色」是同一族

一个**注定完成不了**的任务被呈现成「模型没做好」。两者的共同教训是：
界面上看不出来时，必须有一处结构性地保证它能完成，或者至少把「完成不了」
这件事报出来。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config as C  # noqa: E402
import tools as T  # noqa: E402

#: 存根只在它比被替代的内容更短时才发（`_STUB_MIN_SAVING`），
#: 所以测「有没有发存根」必须用一个大文件。
_BIG_LINES = 4000
_BODY = "x" * 400 + "\n"


class _Project:
    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "big.gd"), "w", encoding="utf-8") as f:
            f.write(_BODY * _BIG_LINES)
        C.set_runtime("code_root", self.root)
        T.clear_read_files()
        return self

    def __exit__(self, *exc):
        T.clear_read_files()
        C.set_runtime("code_root", "")
        return False


class _ReadBase(unittest.TestCase):
    def _first_read(self):
        return T.read_file("path: big.gd")

    def _abs(self, name):
        return os.path.normcase(os.path.join(C.get_runtime("code_root"), name))


class ReadRecordIsPerConversationTests(_ReadBase):

    def test_the_stub_still_fires_within_one_conversation(self):
        """隔离不能把「别重复读」这个功能整个关掉——那会烧掉上万 token。"""
        with _Project():
            first = self._first_read()
            self.assertNotIn("未变化", first)
            second = self._first_read()
            self.assertIn("未变化", second,
                          "同一个对话里重复读没有被拦住——存根失效了")

    def test_a_new_conversation_gets_the_real_content(self):
        """这是本条的核心：另一个子代理必须能真正读到内容。"""
        with _Project():
            self._first_read()          # 上一轮子代理读了全文
            token = T.begin_read_conversation()
            try:
                fresh = self._first_read()
            finally:
                T.end_read_conversation(token)
            self.assertNotIn("未变化", fresh,
                             "新对话拿到了「已在本次对话中读过」的存根——"
                             "那份内容它压根没见过，于是这次重试注定失败")
            self.assertIn(_BODY[:40], fresh, "应当真的返回正文")

    def test_the_outer_conversation_is_restored_afterwards(self):
        with _Project():
            self._first_read()
            token = T.begin_read_conversation()
            try:
                self._first_read()
            finally:
                T.end_read_conversation(token)
            self.assertIn("未变化", self._first_read(),
                          "退出子代理作用域后，外层对话自己的存根能力丢了")

    def test_nested_conversations_each_keep_their_own(self):
        with _Project():
            token = T.begin_read_conversation()
            try:
                self._first_read()
                inner = T.begin_read_conversation()
                try:
                    self.assertNotIn("未变化", self._first_read())
                finally:
                    T.end_read_conversation(inner)
                self.assertIn("未变化", self._first_read(),
                              "内层退回到外层后，外层的记录丢了")
            finally:
                T.end_read_conversation(token)

    def test_the_edit_guard_is_scoped_too(self):
        """「先读后写」护栏也必须按对话算。

        否则上一个子代理读过某文件，就等于替这一个子代理拿到了「已确认内容」
        ——那正是这个护栏要防的事：没亲眼看过就整体重写。
        """
        with _Project():
            self._first_read()
            token = T.begin_read_conversation()
            try:
                files, _cov = T._read_store()
                self.assertNotIn(self._abs("big.gd"), files,
                                 "新对话继承了别的对话的「已读」——"
                                 "先读后写护栏被跨对话放行了")
            finally:
                T.end_read_conversation(token)


class DefaultConversationUnchangedTests(_ReadBase):
    """不进入子代理作用域时，行为必须与从前**完全一致**。"""

    def test_the_module_level_stores_are_the_default(self):
        with _Project():
            files, coverage = T._read_store()
            self.assertIs(files, T._READ_FILES)
            self.assertIs(coverage, T._READ_COVERAGE)

    def test_clear_read_files_clears_the_current_conversation_too(self):
        with _Project():
            self._first_read()
            token = T.begin_read_conversation()
            try:
                self._first_read()
                files, coverage = T._read_store()
                self.assertTrue(files or coverage)
                T.clear_read_files()
                files2, coverage2 = T._read_store()
                self.assertEqual(len(files2), 0)
                self.assertEqual(len(coverage2), 0)
            finally:
                T.end_read_conversation(token)


class SubagentGetsItsOwnRecordTests(unittest.TestCase):
    """`_run_child` 必须真的开新对话——否则上面那些全是空谈。"""

    def _src(self):
        import inspect

        import agent
        return inspect.getsource(agent.Agent._run_child)

    def test_run_child_opens_and_closes_the_record(self):
        src = self._src()
        self.assertIn("begin_read_conversation()", src,
                      "_run_child 没有开新对话——子代理会继承上一个的读记录")
        self.assertIn("end_read_conversation(_read_conv_token)", src,
                      "_run_child 没有还原读记录")

    def test_the_record_is_restored_even_when_the_child_raises(self):
        """子代理抛异常时也必须还原。

        `_run_child` 里异常路径是 `return failed`，不在 finally 里还原的话，
        外层对话的存根能力会被**永久**削掉——而且越跑越差，从不报错。
        """
        src = self._src()
        tail = src[src.rindex("finally:"):]
        self.assertIn("end_read_conversation", tail[:400],
                      "end_read_conversation 不在 finally 里——"
                      "异常路径会把读记录永久留在子代理那一份上")


if __name__ == "__main__":
    unittest.main()