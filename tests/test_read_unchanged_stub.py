# -*- coding: utf-8 -*-
"""未变化的文件不该被重复读——借鉴 Claude Code 的 ``FILE_UNCHANGED_STUB``。

## 为什么值得做

实测（godot_sample 定位扣血逻辑）：模型**已经完整读过** enemy.gd / player.gd /
hud.gd，随后被纠偏要求"调用 read_file 读取相关行"，于是把三个文件按区间又读了一遍
——7 次额外调用、44 秒，token 白花。

Claude Code 的做法（``FileReadTool/prompt.ts``）：

    export const FILE_UNCHANGED_STUB =
      'File unchanged since last read. The content from the earlier Read tool_result
       in this conversation is still current — refer to that instead of re-reading.'

## 两个关键设计点

**一、记「已覆盖的行区间」而不是「读过这个文件」。** 上面那次浪费的形式是
"整文件读一遍 + 再读若干区间"，若只按整文件记账，这些区间重读一个都拦不住。
只读过 [1,20] 时再要 [50,60] 必须真读——所以区间要能判定**包含关系**。

**二、存根必须短。** 同一份源码的注释里记着他们的实测结论：报错路径只花约
100 字节，而返回内容要花上万 token（他们试过"截断代替报错"，量化后发现 token
反而涨了，于是回退）。

## 最危险的失败模式

**发得不该发的存根** —— 那会让模型拿着过期内容改代码。所以判定必须保守：
指纹用 ``(st_mtime_ns, st_size)``（写入必然改 mtime，不需要额外算哈希），
指纹不符一律真读。已单独写测试钉住。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import config as C  # noqa: E402
import tools as T  # noqa: E402

LINES = 60


class ReadStubTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "behaviors"))
        self.target = os.path.join(self.tmp.name, "behaviors", "enemy.gd")
        self._write(LINES)
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", ""), T.clear_read_files()))

    def _write(self, n, prefix="line"):
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write("\n".join("%s%d" % (prefix, i) for i in range(1, n + 1)))

    def _read(self, arg):
        out = T.read_file(arg)
        return out if isinstance(out, str) else str(out)

    def _is_stub(self, text):
        return "未变化" in text

    # ---- 该发存根 ----

    def test_rereading_the_whole_file_returns_a_stub(self):
        T.read_file("path: behaviors/enemy.gd")
        out = self._read("path: behaviors/enemy.gd")
        self.assertTrue(self._is_stub(out), "同一文件重复整读没有走存根")

    def test_a_range_inside_an_earlier_whole_file_read_returns_a_stub(self):
        """**本次实测浪费的正是这一种**：整文件读过之后再读它的若干区间。"""
        T.read_file("path: behaviors/enemy.gd")
        for rng in ("start: 1 end: 20", "start: 30 end: 45", "start: 55 end: 60"):
            out = self._read("path: behaviors/enemy.gd " + rng)
            self.assertTrue(self._is_stub(out),
                            "%s 已被整文件读取覆盖，却没有走存根" % rng)

    def test_the_stub_is_short_and_names_the_earlier_read(self):
        real = self._read("path: behaviors/enemy.gd start: 1 end: %d" % LINES)
        stub = self._read("path: behaviors/enemy.gd start: 1 end: %d" % LINES)
        self.assertLess(len(stub), len(real) / 2,
                        "存根必须显著短于真实内容（Claude Code 实测：约 100 字节 vs 上万 token）")
        self.assertIn("不要重复读取", stub,
                      "存根要兼作一句劝阻——这是治「验证躲避」的另一半")

    # ---- 不该发存根（发错比不发严重得多）----

    def test_an_uncovered_range_is_still_read_for_real(self):
        T.read_file("path: behaviors/enemy.gd start: 1 end: 20")
        out = self._read("path: behaviors/enemy.gd start: 50 end: 60")
        self.assertFalse(self._is_stub(out),
                         "只读过 [1,20] 却对 [50,60] 发存根——那段内容不在上下文里")
        self.assertIn("line50", out, "没有真读到第 50 行")

    def test_after_the_file_changed_it_is_read_again(self):
        """**最危险的失败模式**：发过期存根，模型会拿着旧内容改代码。"""
        T.read_file("path: behaviors/enemy.gd start: 1 end: 20")
        self._write(LINES, prefix="CHANGED")
        out = self._read("path: behaviors/enemy.gd start: 1 end: 20")
        self.assertFalse(self._is_stub(out), "文件已改却仍发存根——模型会拿旧内容改代码")
        self.assertIn("CHANGED1", out)

    def test_a_file_never_read_is_read_for_real(self):
        other = os.path.join(self.tmp.name, "behaviors", "player.gd")
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("\n".join("p%d" % i for i in range(1, 41)))
        out = self._read("path: behaviors/player.gd start: 1 end: 40")
        self.assertFalse(self._is_stub(out))
        self.assertIn("p1", out)

    def test_clearing_read_state_restores_full_reads(self):
        """切项目/重置后旧记录必须失效——否则会跨项目发存根。"""
        T.read_file("path: behaviors/enemy.gd")
        T.clear_read_files()
        out = self._read("path: behaviors/enemy.gd")
        self.assertFalse(self._is_stub(out), "clear_read_files 之后仍发存根")

    def test_coverage_records_the_served_range_not_the_requested_one(self):
        """直接断言记账用的是**实际送达**区间，而不是请求区间。

        请求 end=9999 而文件只有 60 行时实际送达1-60；按请求记成 [1,9999]
        会高估覆盖，让后续本该真读的行被存根挡掉。
        （端到端角度这个场景被test_an_uncovered_range_is_still_read_for_real 覆盖，
          这里钉的是机制本身。）
        """
        T.read_file("path: behaviors/enemy.gd start: 1 end: 9999")
        entry = T._READ_COVERAGE.get(os.path.normcase(self.target))
        self.assertIsNotNone(entry, "读完之后没有记录覆盖区间")
        ranges = entry[1]
        self.assertTrue(any(hi <= LINES for _lo, hi in ranges),
                        "覆盖记账里出现了超出文件行数的区间：%r" % (ranges,))

    def test_an_out_of_range_start_is_reported_clearly(self):
        """此前会输出「第 70-60 行」这种倒置区间且正文为空。

        现在的措辞是「起始行 N 越界，合法范围 1-M」——短，但把该给的信息给全了。
        """
        out = self._read("path: behaviors/enemy.gd start: %d end: %d" % (LINES + 10, LINES + 20))
        self.assertNotIn("-%d 行" % LINES, out.split("===")[0],
                         "输出里仍出现倒置区间行号")
        self.assertIn("1-%d" % LINES, out,
                      "越界必须给出真实行数范围，否则模型无从修正")
        self.assertIn("越界", out)

    # ---- 护栏不能被破坏 ----

    def test_the_same_range_is_read_for_real_after_repeated_stubs(self):
        """**存根不能无限发** —— 这是实测踩到的一个死循环。

        存根的前提是「模型此刻还看得见那次送达的正文」。而正文**会被上下文预算
        裁走**；那一刻起，同一个区间再要多少次，拿到的都只是同一句劝阻：

            === collision.py（未变化）===
            整份文件已在本次对话中读过、且此后未改动；……

        实测（2026-10-22）：模型连读同一个文件 4 次，每次都只拿到这句劝阻，
        于是它改用 python_exec 手搓无头驱动去「模拟」游戏逻辑 —— 既更贵、
        又绕开了受控执行环境，还把自己的调用签名写错当成产品问题。

        所以：**同一区间被反复要到第 N+1 次，就真读**。省 token 的初衷一点没丢
        （前几次照旧省），而「拿不到正文」这件事不再可能无限持续。
        """
        T.read_file("path: behaviors/enemy.gd")
        for _ in range(T._STUB_MAX_HITS):
            out = self._read("path: behaviors/enemy.gd")
            self.assertTrue(self._is_stub(out),
                            "前几次重读应当仍走存根（省 token 的初衷不能丢）")
        out = self._read("path: behaviors/enemy.gd")
        self.assertFalse(self._is_stub(out),
                         "同一区间被反复要了 %d 次还在发存根——模型会原地打转，"
                         "永远拿不到正文" % (T._STUB_MAX_HITS + 1))
        self.assertIn("line1", out,
                      "第 %d 次应当真的读到正文" % (T._STUB_MAX_HITS + 1))

    def test_serving_the_content_again_resets_the_stub_counter(self):
        """真读过之后，存根要重新开始发 —— 否则从此永远真读，省 token 的初衷丢一半。

        序列：第 1 次真读 → 第 2/3 次存根 → 第 4 次真读（**计数归零**）→
        第 5 次又该是存根。
        """
        T.read_file("path: behaviors/enemy.gd")
        for _ in range(T._STUB_MAX_HITS):
            out = self._read("path: behaviors/enemy.gd")
            self.assertTrue(self._is_stub(out),
                            "前 %d 次重读应当仍走存根" % T._STUB_MAX_HITS)
        real = self._read("path: behaviors/enemy.gd")
        self.assertFalse(self._is_stub(real),
                         "第 %d 次应当真读" % (T._STUB_MAX_HITS + 1))
        again = self._read("path: behaviors/enemy.gd")
        self.assertTrue(self._is_stub(again),
                        "真读过之后存根应当重新可用（计数已归零）——"
                        "否则从此永远真读，省 token 的初衷丢一半")

    def test_the_stub_still_registers_the_file_as_read(self):
        """否则 apply_edit 的「先读后写」护栏会误判成没读过。"""
        T.read_file("path: behaviors/enemy.gd")
        self._read("path: behaviors/enemy.gd")   # 这次返回存根
        self.assertIn(os.path.normcase(self.target), T._READ_FILES,
                      "存根路径没有登记 _READ_FILES——整体重写会被护栏拒绝")

    def test_it_can_be_turned_off_by_env(self):
        """线上出问题时要能一键关掉。"""
        original = T._READ_STUB_ENABLED
        T._READ_STUB_ENABLED = False
        try:
            T.read_file("path: behaviors/enemy.gd")
            out = self._read("path: behaviors/enemy.gd")
            self.assertFalse(self._is_stub(out),
                             "关掉开关后仍在发存根——线上无法回退")
        finally:
            T._READ_STUB_ENABLED = original


class RangeMergeTests(unittest.TestCase):
    """区间合并必须能正确判定包含关系，否则存根该发不发或不该发乱发。"""

    def test_merge(self):
        self.assertEqual(T._merge_ranges([(1, 20), (5, 30)]), [(1, 30)])
        self.assertEqual(T._merge_ranges([(1, 20), (25, 30)]), [(1, 20), (25, 30)])
        self.assertEqual(T._merge_ranges([(25, 30), (1, 20)]), [(1, 20), (25, 30)])

    def test_cover(self):
        self.assertTrue(T._range_covered([(1, 50)], 10, 20))
        self.assertTrue(T._range_covered([(1, 50)], 1, 50))
        self.assertFalse(T._range_covered([(1, 50)], 40, 60),
                         "超出上界却判定为已覆盖——会发出错误的存根")
        self.assertFalse(T._range_covered([(30, 40)], 10, 20))


if __name__ == "__main__":
    unittest.main()