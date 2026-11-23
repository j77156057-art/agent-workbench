"""LLM 客户端的**惰性 SDK 客户端**回归测试。

来历（2026-11-24 的启动耗时体检）：`import api` 要 5.2 秒，
`-X importtime` 显示 `openai` SDK 占 1.6 秒 —— 它比 chromadb 还多。
而服务启动时一次都用不到它：api.py 在模块级就建默认 Agent
（`_agent_for("default")`），Agent.__init__ 又会 `LLMClient()`。
顶层 import SDK 等于让**每次启动**都白付这 1.6 秒。

改成：`openai` SDK 与 client 都惰性 —— 第一次真正访问 `self.client` 时才
import + 构造。

这里锁住四件事：
1. 导入 api **不会**顺带把 openai 拉进来；
2. 真要用时 client 照常能建（惰性不是禁用）；
3. mock provider 的 `client` 仍是 None（与改动前一致）；
4. client **可被外部注入**（测试用 `client.client = SimpleNamespace(...)` 注入桩）。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LazyOpenAIImportTests(unittest.TestCase):
    """openai SDK 的惰性导入（用独立子进程验，避免本进程已加载造成假绿）。"""

    def _run(self, code):
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, cwd=ROOT, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r.stdout.strip().splitlines()[-1].strip()

    def test_import_api_does_not_load_openai(self):
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); import api; "
            "print('openai' in sys.modules)" % ROOT)
        self.assertEqual(out, "False", "导入 api 仍然加载了 openai")

    def test_mock_provider_client_is_none(self):
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); from llm import LLMClient; "
            "c = LLMClient(provider='mock'); "
            "print(c.client is None, 'openai' in sys.modules)" % ROOT)
        self.assertEqual(out, "True False", out)

    def test_client_can_be_injected(self):
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); "
            "from types import SimpleNamespace; from llm import LLMClient; "
            "c = LLMClient(provider='mock'); "
            "c.client = SimpleNamespace(marker=1); "
            "print(c.client.marker)" % ROOT)
        self.assertEqual(out, "1", out)
    def test_module_level_agent_is_lazy(self):
        """api 的模块级默认 Agent 惰性 —— 导入期不建 LLMClient。"""
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); import api; "
            "print('openai' in sys.modules)" % ROOT)
        self.assertEqual(out, "False", "导入 api 仍然加载了 openai")

    def test_module_level_agent_proxy_works(self):
        """惰性代理：首次访问 api.agent 能拿到 Agent，且是同一个实例。"""
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); import api; "
            "a = api.agent; print(type(a).__name__, a is api.agent)" % ROOT)
        parts = out.split()
        self.assertEqual(parts[0], "Agent", out)
        self.assertEqual(parts[1], "True", out)


class LlmClientPropertyTests(unittest.TestCase):
    """同进程内的行为。"""

    def setUp(self):
        sys.path.insert(0, ROOT)
        from llm import LLMClient
        self.LLMClient = LLMClient

    def test_provider_custom_without_base_url_raises(self):
        """custom provider 缺 base_url 仍要报错（构造期校验没被惰性化绕过）。"""
        with self.assertRaises(ValueError):
            self.LLMClient(provider="custom", base_url="")


if __name__ == "__main__":
    unittest.main()