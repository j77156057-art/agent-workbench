"""BUG-10 回归锁：python_exec 子进程的中文 stdout 不得被解码成乱码替换符。

事故（缺陷清单 BUG-10 / 钻测轮次 11）：深度思考里真跑了 python_exec 验算，输出本应是
「caseB 掉到 0 时刻 = 3.2s」，画面上却是「caseB hits = 5 ◆◆0◆◆ = 3.2」——中文被替换字符
（U+FFFD / ◆）吃掉。

根因：子进程 stdout 是**管道**（非控制台）时，CPython 按本机区域编码写中文（中文 Windows=
cp936/GBK）；而 `run_agent_command` 的 local/fallback 路径不给 encoding（默认按 locale 解），
`windows_sandbox.run_isolated` 又按 utf-8+errors=replace 解——两侧编码不一致时 GBK 字节被当
非法 UTF-8 静默替换成 ◆。

修法：所有执行路径统一——子进程强制 PYTHONIOENCODING=utf-8（吐 UTF-8 字节）+ 父端统一
utf-8 解码。本测试直接走 run_agent_command（当前本机 local 路径）跑一段打印中文的脚本，
断言 stdout 含原文中文且**不含 U+FFFD**。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime.enterprise_sandbox import (  # noqa: E402
    run_agent_command,
    utf8_child_env,
)

CN = "到0时刻=中文OK"
REPL = "\ufffd"  # U+FFFD OBJECT REPLACEMENT CHARACTER —— 乱码的标志


class Utf8ChildEnvTests(unittest.TestCase):
    def test_forces_pythonioencoding_utf8(self):
        env = utf8_child_env({"PYTHONIOENCODING": "gbk", "PATH": os.environ.get("PATH", "")})
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8",
                         "必须覆盖成 utf-8，不能让继承来的 gbk 赢得解码方向")

    def test_defaults_to_os_environ_when_none(self):
        env = utf8_child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        # 应当是拷贝，不改到进程真实环境
        self.assertIsNot(env, os.environ)


class ChineseStdoutTests(unittest.TestCase):
    def _run_cn_print(self):
        code = "print('%s')" % CN
        cmd = [sys.executable, "-c", code]
        return run_agent_command(cmd, project_root=ROOT, timeout=15)

    def test_chinese_survives_roundtrip_without_replacement_char(self):
        res = self._run_cn_print()
        out = (res.stdout or "") + (res.stderr or "")
        self.assertNotIn(REPL, out,
                         "stdout 出现 U+FFFD 说明编码又错配了（BUG-10 复发）：%r" % out)
        self.assertIn(CN, out,
                      "中文原文应完整可见，实际：%r" % out)

    def test_nonascii_variety(self):
        """再覆盖一类非 ASCII：全角、箭头、单位，确保不是只有某一段侥幸过。"""
        probe = "距离≤3.5m · 命中率19% · ✓ 完成"
        code = "print('%s')" % probe
        cmd = [sys.executable, "-c", code]
        res = run_agent_command(cmd, project_root=ROOT, timeout=15)
        out = (res.stdout or "") + (res.stderr or "")
        self.assertNotIn(REPL, out, "非 ASCII 混合串被替换符污染：%r" % out)
        self.assertIn(probe, out, "应原样回显：%r" % out)


if __name__ == "__main__":
    unittest.main()
