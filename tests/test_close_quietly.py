"""`tools._close_quietly`：关流失败不许把调用方带崩。

来历：改动行覆盖率门把 PTC 收尾里的 `except Exception: pass` 两行点亮红 —— 正常路径
里 `close()` 不会失败，内联的兜底分支永远不会被执行。抽成函数并直接击穿它，比给这两行
加 `pragma: no cover` 诚实：兜底逻辑本身也被测到了。
"""

import unittest
from unittest import mock

import tools


class CloseQuietlyTests(unittest.TestCase):
    def test_closes_the_stream(self):
        stream = mock.Mock()
        tools._close_quietly(stream)
        stream.close.assert_called_once_with()

    def test_none_is_a_no_op(self):
        tools._close_quietly(None)          # 不抛就够

    def test_a_failing_close_is_swallowed(self):
        stream = mock.Mock()
        stream.close.side_effect = OSError("close failed")
        tools._close_quietly(stream)        # 不抛就够：关失败不该把调用方带崩


if __name__ == "__main__":
    unittest.main()
