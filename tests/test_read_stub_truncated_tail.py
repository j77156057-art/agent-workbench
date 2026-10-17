# -*- coding: utf-8 -*-
"""锁死根因①：read_file 整文件读**被截断**时，覆盖记账只能是"真正送达"的那一段。

## 事故形态（drill 现场 · 2026-10-17）

read_file 对 >12000 字的文件只送达前 12000 字（正文老实标了「已截断」），
却调用 _note_read_range(target, None, None, ...)——None,None 折叠成
ranges=[(1, _FULL_FILE_END)]，等于宣称"整份文件都读过了"。于是同一会话里，
模型自觉"被截断了我按行号补读尾段"时，_unchanged_read_stub 会对从未送达的行
返回假存根「已在本次对话中读过、不要重复读取」，把正文永久挡在外面。

真实跑一轮：模型被假存根拦了两次后**凭空写出第 757-768 行的函数体**，还声明
"行号与函数体来自那次真实读取，不是推断"——一致性度量因"它抄的行恰好真实存在"
判了 PASS。这是"真行号但来源是编的"那一类最危险的过度声称。

## 这组测试钉的是机制本身

- 截断整读之后，尾段（从未送达的行）区间读必须拿到真实正文，不是存根；
- 截断整读之后，头部（已送达的行）区间读仍应走存根——不能把 token 节省也丢了；
- 截断整读记录的覆盖里绝不能出现 _FULL_FILE_END 这种"全文件"上界；
- 顺带守 TruncationKeepsTheRealCountTests 的字符串契约：抬头仍按完整文件报总行数、仍带「已截断」。
"""

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config as C  # noqa: E402
import tools as T  # noqa: E402

# 每行定宽 12 字符（含换行）-> 1500 行约 18000 字，稳过 12000 截断线。
N_LINES = 1500
NEEDLE_LINE = 1400          # 远在截断窗口（约前 1000 行）之后
NEEDLE = "NEEDLE_TAIL_XYZ"


def _body():
    out = []
    for i in range(1, N_LINES + 1):
        if i == NEEDLE_LINE:
            out.append("%s %d\n" % (NEEDLE, i))
        else:
            out.append("ln_%05d \n" % i)
    return "".join(out)


class TruncatedReadCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.join(self.tmp.name, "big.gd")
        with open(self.target, "w", encoding="utf-8") as fh:
            fh.write(_body())
        self._prev = C._RUNTIME.get("code_root")
        C.set_runtime("code_root", self.tmp.name)
        C.set_runtime("project_rules", "")
        T.clear_read_files()
        self.addCleanup(lambda: (C.set_runtime("code_root", self._prev or ""),
                                 T.clear_read_files()))

    def _read(self, arg):
        out = T.read_file(arg)
        return out if isinstance(out, str) else str(out)

    def _is_stub(self, text):
        return "未变化" in text or "不要重复读取" in text

    def test_whole_file_read_is_actually_truncated(self):
        """前置条件必须先成立：这是一次会走截断分支的整读。"""
        out = self._read("path: big.gd")
        self.assertIn("已截断", out, "fixture 太小，没触发截断分支，测不到点上")
        self.assertNotIn(NEEDLE, out, "整读竟然送达了尾段记号——截断线没生效")

    def test_tail_range_after_truncation_returns_real_content(self):
        """核心：截断整读之后，从未送达的尾段按行号补读必须拿到真实正文。"""
        self._read("path: big.gd")  # 先污染（旧行为会误记全覆盖）
        out = self._read("path: big.gd, start: %d, end: %d" % (NEEDLE_LINE, NEEDLE_LINE + 5))
        self.assertFalse(self._is_stub(out),
                         "尾段从没送达却返回『未变化』假存根——正是本次要修的缺陷")
        self.assertIn(NEEDLE, out, "没有真读到第 %d 行的记号" % NEEDLE_LINE)

    def test_head_range_after_truncation_still_stubbed(self):
        """反向守：已送达的头部区间重读仍走存根，别把 token 节省也丢了。"""
        self._read("path: big.gd")
        out = self._read("path: big.gd, start: 5, end: 20")
        self.assertTrue(self._is_stub(out),
                        "头部明明已送达，重读却没走存根——回退过头，白白重读")

    def test_coverage_never_claims_full_file_after_truncation(self):
        """机制层直断：截断整读记录的覆盖里不能出现 _FULL_FILE_END 上界。"""
        self._read("path: big.gd")
        entry = T._read_store()[1].get(os.path.normcase(self.target))
        self.assertIsNotNone(entry, "截断整读之后应记录送达区间")
        ranges = entry[1]
        self.assertTrue(all(hi < T._FULL_FILE_END for _lo, hi in ranges),
                        "覆盖记账出现了『全文件』上界：%r——尾段会被假存根封死" % (ranges,))
        max_hi = max(hi for _lo, hi in ranges)
        self.assertLess(max_hi, N_LINES, "送达上界不该逼近整文件行数：%d" % max_hi)

    def test_rereading_whole_truncated_file_is_not_stubbed_as_complete(self):
        """整文件重读也不能谎称整份读过——尾段仍未见，应重新给（截断的）正文。"""
        first = self._read("path: big.gd")
        again = self._read("path: big.gd")
        self.assertFalse(self._is_stub(again),
                         "整读被截断，却对『再整读』发全覆盖存根——尾段依旧拿不到")
        self.assertIn("已截断", again)

    def test_string_contract_keeps_full_total_and_truncation_note(self):
        """守 TruncationKeepsTheRealCountTests 的契约：抬头按完整文件报总行数。"""
        out = self._read("path: big.gd")
        head = out.splitlines()[0]
        self.assertIn("共 %d 行" % N_LINES, head,
                      "截断抬头必须按完整文件报总行数，否则模型以为文件就这么长")


if __name__ == "__main__":
    unittest.main()
