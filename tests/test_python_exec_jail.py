# -*- coding: utf-8 -*-
"""P0 回归锁：python_exec 文件牢笼（PEP-578 审计钩）堵住「绕过 dev_* 审批轨直写磁盘」。

钻测实据（AA）：读缓存命中桩被硬拦后，模型改用 python_exec 跑 `open(path,'w')`
直接把文件写进磁盘——这一路径完全绕开 dev_* 那一整套护栏（写意图门 / 路径牢 /
凭据地板 / edit-confirm 暂存）。无 Docker 的 Windows 默认把 `python -c` 降级到
全权限宿主解释器，于是子进程能任意越出 code_root、覆盖用户真实文件、读凭据。

修法落点（tools.py）：在交给子进程的代码前**预置一段审计钩 bootstrap**，装进子进程
内部（钩无法被被审计代码移除），对**写操作**强制复用与 dev 工具同源的地板/根约束：
  · 凭据 / NT 设备路径：读写都拦（对齐 `_hard_floor_reason`）；
  · 越出 code_root 的写：拦——但放行临时/缓存目录，否则合法脚本（pytest 临时文件、
    matplotlib 缓存等）连不动，过度拦 = agent 瘫；
  · 只读越界：不拦（读 site-packages 是刚需，危险只在凭据）；
  · 校验自身出岔：写侧 fail-closed 拒绝，读侧放行（别把 import 跑挂）。

刻意**不**动 `_is_write_tool`：`test_admin_and_exec_are_uncovered_by_design` 钉死
「EXEC 不受写意图护栏」是设计（写动词表会误伤「提交/回滚」），EXEC 隔离按设计落在沙箱边界。

另注：这是「抬高门槛、挡住 AA 那种直接 open(...,w) 的一阶绕过」，非密不透风——
混淆构造路径或 exec 原生 syscall 仍可绕，真·对抗性逃逸仍需容器 --read-only 兜底。
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tools as tools_mod  # noqa: E402
from config import set_runtime  # noqa: E402


class PythonExecJailTests(unittest.TestCase):
    def _run(self, root, code):
        with patch.dict(os.environ, {"DOCMIND_EXECUTION_MODE": "local"}):
            set_runtime("code_root", root)
            return tools_mod.python_exec(code)

    def test_write_inside_root_is_allowed(self):
        """根内写是 python_exec 的正常能力，牢笼不能把它一起拦死。"""
        with tempfile.TemporaryDirectory() as d:
            out = self._run(d, "open('inroot.txt','w').write('x'); print('WROTE')")
            self.assertIn("WROTE", out)
            self.assertTrue(os.path.exists(os.path.join(d, "inroot.txt")),
                            "根内写应成功落盘：%r" % out)

    def test_write_outside_root_is_blocked(self):
        """越出 code_root 的写必须被拒，且文件绝不落盘（这正是不受控的子进程写盘洞）。"""
        home = os.path.expanduser("~")
        esc = os.path.join(home, "__docmind_jail_escape_probe__.txt")
        try:
            with tempfile.TemporaryDirectory() as d:
                code = "open(%r,'w').write('x')" % esc
                out = self._run(d, code)
                # 判据用**权威信号**：子进程 traceback 会把源码行原样回显，故不能靠
                # 「标记串不在输出里」判放行——只能看「文件没落盘」+「明确拒绝文案」。
                self.assertTrue("安全拦截" in out and "越出代码根目录" in out,
                                "应给出明确的越界拒绝文案：%r" % out)
                self.assertIn("PermissionError", out, "越界写应以 PermissionError 中止：%r" % out)
                self.assertFalse(os.path.exists(esc),
                                 "被拒的越界写绝不能真的创建文件")
        finally:
            if os.path.exists(esc):
                os.remove(esc)

    def test_credential_floor_blocks_even_in_child(self):
        """凭据目录（.ssh）无论读写都拦——与 code_root 无关的硬地板，dev 工具同款。"""
        home = os.path.expanduser("~")
        probe = os.path.join(home, ".ssh", "docmind_jail_probe_key")
        try:
            with tempfile.TemporaryDirectory() as d:
                code = "open(%r,'w').write('x')" % probe
                out = self._run(d, code)
                self.assertIn("安全拦截", out, "凭据路径写应被拒：%r" % out)
                self.assertIn("PermissionError", out,
                              "凭据地板应以 PermissionError 中止：%r" % out)
                self.assertFalse(os.path.exists(probe))
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_temp_dir_write_is_carved_out(self):
        """临时目录写放行——否则 pytest/matplotlib 等合法脚本会被误拦，agent 直接瘫。"""
        tdir = tempfile.gettempdir()
        tp = os.path.join(tdir, "docmind_jail_temp_probe.txt")
        with tempfile.TemporaryDirectory() as d:
            code = "open(%r,'w').write('x'); print('TEMP_OK'); import os; os.remove(%r)" % (tp, tp)
            out = self._run(d, code)
            self.assertIn("TEMP_OK", out, "临时目录写应被允许：%r" % out)

    def test_legit_import_still_works(self):
        """读侧绝不能 fail-closed 把 import 跑挂：导入标准库会开一堆只读文件。"""
        with tempfile.TemporaryDirectory() as d:
            out = self._run(d, "import json, statistics; print(json.dumps(statistics.mean([1,2,3])))")
            self.assertIn("2", out, "正常 import/计算不该被牢笼误伤：%r" % out)


if __name__ == "__main__":
    unittest.main()
