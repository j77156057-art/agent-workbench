# -*- coding: utf-8 -*-
"""②：未变化存根必须**可证伪**——把"直接引用"做成条件句，并给出重读逃逸。

背景（docmind 钻测 · "整文件截断读误记全覆盖→假存根封死尾段"修法清单第 2 条）：
存根代码只拿得到 `(stamp, ranges, served_bytes)`，它**无从知道**那次 observation
是否还留在当前上下文窗口里。① 已封死"截断读误记全覆盖"（送达之外的区间会真读），
但剩下的残余——**更早已送达的正文被上下文预算裁走**——①/③ 都够不到：存根若仍无
条件地喊"不要重复读取"，模型会被逼去 python_exec 兜底（实测单样本 42 步）。

本测试钉住存根的两条不变式，防止日后被"再压短一点"顺手删掉：
 1. 存根把成立条件交给模型自核（"你此刻是否看得到"），并明确给按行号重读的逃逸；
 2. 加了这段之后，存根仍显著短于其替代的真实内容（省 token 的初衷不破）。
同时守住既有锁："不要重复读取""未变化"仍在。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as C  # noqa: E402
import tools as T  # noqa: E402

LINES = 60  # "line%d" 整读约 600 字节，足以让存根 < 真实内容的一半


class StubIsFalsifiableTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"))
        self.target = os.path.join(self.tmp.name, "behaviors", "enemy.gd")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("\n".join("line%d" % i for i in range(1, LINES + 1)))
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", ""), T.clear_read_files()))

    def _read(self, arg):
        out = T.read_file(arg)
        return out if isinstance(out, str) else str(out)

    def _stub_body(self):
        # 先真读整份，再重读同一区间命中存根。
        self._read("path: behaviors/enemy.gd")
        return self._read("path: behaviors/enemy.gd")

    def test_stub_conditions_the_refer_claim_on_actually_seeing_it(self):
        stub = self._stub_body()
        self.assertIn("未变化", stub, "抬头应仍标未变化")
        # 成立条件交给模型自核：不是无条件"直接引用"。
        self.assertIn("你此刻仍能在上下文里看到", stub,
                      "存根没把'你此刻是否看得到'作为引用前提——不可证伪")

    def test_stub_gives_a_reread_escape_when_not_in_window(self):
        stub = self._stub_body()
        # 看不到就按行号重读，而不是被"不要重复读取"逼去绕 python_exec。
        self.assertIn("按行号重读", stub, "存根缺按行号重读的逃逸路径")
        self.assertIn("以你真正见到的内容为准", stub,
                      "存根应把最终裁量权交回模型真正见到的内容")

    def test_stub_still_keeps_the_anti_avoidance_discouragement(self):
        # 既有锁：看得到时仍要劝阻重读（治"验证躲避"的另一半）。
        stub = self._stub_body()
        self.assertIn("不要重复读取", stub)

    def test_falsifiable_stub_is_still_short_vs_real_content(self):
        real = self._read("path: behaviors/enemy.gd start: 1 end: %d" % LINES)
        stub = self._read("path: behaviors/enemy.gd start: 1 end: %d" % LINES)
        self.assertIn("未变化", stub, "第二次同区间读应命中存根")
        self.assertLess(len(stub), len(real) / 2,
                        "加可证伪条款后存根仍须显著短于真实内容（省 token 初衷）")


class TruncatedReadBeyondPrefixStillRealTests(unittest.TestCase):
    """① 的护栏顺带确认：截断整读之后，送达**之外**的区间不会命中这个可证伪存根，
    而是给真实正文——避免有人误以为②改了①的行为。用够大的文件触发 12000 字截断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "b"))
        self.target = os.path.join(self.tmp.name, "b", "big.gd")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("\n".join("l%05d_%s" % (i, "x" * 40) for i in range(1, 1401)))
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", ""), T.clear_read_files()))

    def _read(self, arg):
        out = T.read_file(arg)
        return out if isinstance(out, str) else str(out)

    def test_beyond_served_prefix_returns_real_content_not_stub(self):
        first = self._read("path: b/big.gd")
        self.assertIn("已截断", first, "构造的整读本应触发截断")
        tail = self._read("path: b/big.gd start: 900 end: 910")
        self.assertNotIn("未变化", tail,
                         "送达之外的区间被假存根挡掉——① 的护栏被破坏")
        self.assertIn("l00905", tail, "尾段应给出真实编号正文")


if __name__ == "__main__":
    unittest.main()
