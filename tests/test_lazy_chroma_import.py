"""向量库依赖的**惰性导入**回归测试。

来历（2026-11-23 的启动耗时体检）：`import api` 要 6.8 秒，`-X importtime`
显示 chromadb 占 1.5 秒 —— 而 chromadb 只在真正连向量库时才需要。
绝大多数请求根本不碰知识库（列会话、开工作台、看文件树），
却让**每次启动**都先付这 2 秒。

改成惰性后：
- `vectorstore` 顶层不再 import chromadb，只在 `_get_client()` 里 import；
- `ingest` 顶层不再 import vectorstore.add_documents（改走 `_add_documents`）；
- `api` 顶层不再调 `list_sources()` 回填 `_INGESTED`（改走 `_ingested_sources()`）。

这里锁住三件事：
1. 导入 `api` **不会**顺带把 chromadb 拉进来；
2. 真要用向量库时它照样能用（不是被删掉了）；
3. `_ingested_sources()` 的回填结果与旧的导入期回填**逐条一致**。
"""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LazyChromaImportTests(unittest.TestCase):
    """惰性导入的行为（用独立子进程验，避免本进程已加载 chromadb 造成假绿）。"""

    def _run(self, code):
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, cwd=ROOT, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return r.stdout.strip()

    def test_import_api_does_not_load_chromadb(self):
        """导入 api 不应拖起 chromadb —— 这是这轮改动的全部意义。"""
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); "
            "import api; "
            "print('chromadb' in sys.modules)" % ROOT)
        self.assertEqual(out.splitlines()[-1].strip(), "False",
                         "导入 api 仍然加载了 chromadb")

    def test_vectorstore_still_works_when_used(self):
        """真要用向量库时照常能用（惰性不是禁用）。"""
        out = self._run(
            "import sys; sys.path.insert(0, r'%s'); "
            "from vectorstore import count; "
            "print(isinstance(count('code'), int))" % ROOT)
        self.assertEqual(out.splitlines()[-1].strip(), "True", out)

    def test_ingested_sources_backfill_matches_direct_call(self):
        """惰性回填的结果与直接调 list_sources() 一致。"""
        out = self._run(
            "import os, sys; sys.path.insert(0, r'%s'); "
            "import api; from vectorstore import list_sources; "
            "old = sorted(os.path.basename(s) for s in list_sources()); "
            "new = sorted(api._ingested_sources()); "
            "print(old == new, len(old), len(new))" % ROOT)
        last = out.splitlines()[-1].strip()
        self.assertTrue(last.startswith("True"),
                        "惰性回填与直接调用不一致：%r" % last)


if __name__ == "__main__":
    unittest.main()
