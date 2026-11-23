"""启动期不该加载 langchain / langgraph 的回归测试。

来历（2026-11-24 的启动耗时体检）：`import api` 要 2.6 秒，`-X importtime`
显示两个大头都是**可选依赖**，且启动期一个都用不到：

- `langchain_core.retrievers`（~1.2 秒，顺带拖起 langsmith / langgraph）
  —— 只在**真正检索**（构造 retriever）时才需要；
- `langgraph.graph`（~1.6 秒）—— 只在**工作流真正执行**（建图）时才需要。

改后导入期四个重依赖都不加载，`import api` 1.5 秒。

这里锁住：
1. 导入 api / retrieval / game_workflow **不会**拖起 langchain 或 langgraph；
2. 真要用时它们照常可用 —— **类仍是真正的 LangChain 类**（不是降级版）；
3. 外部 `from ... import Document / ChromaRetriever` 仍能拿到真类；
4. checkpointer 的诊断状态在**被问到时**如实反映（惰性建 saver）。
"""

import subprocess
import sys
import unittest

ROOT = r"D:\WorkBuddy\rag-agent"


def _run(code: str) -> str:
    r = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, cwd=ROOT, timeout=300)
    if r.returncode != 0:
        raise AssertionError(r.stderr[-1500:])
    return r.stdout.strip().splitlines()[-1].strip()


class StartupImportTests(unittest.TestCase):
    """启动期不加载可选依赖。"""

    def test_import_api_does_not_load_langchain_or_langgraph(self):
        out = _run(
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "import api\n"
            "print(int('langchain_core' in sys.modules), int('langgraph' in sys.modules))" % ROOT)
        self.assertEqual(out, "0 0", "导入期不该加载 langchain/langgraph：%r" % out)

    def test_import_retrieval_alone_is_cheap(self):
        out = _run(
            "import sys, time; sys.path.insert(0, r'%s'); "
            "t0=time.perf_counter(); import agent_runtime.retrieval; "
            "print(int((time.perf_counter()-t0)*1000), 'langchain_core' in sys.modules)" % ROOT)
        ms, loaded = out.split()
        self.assertEqual(loaded, "False", "导入 retrieval 就加载了 langchain")
        self.assertLess(int(ms), 600, "导入 retrieval 仍太慢：%s ms" % ms)


class LazyBoundaryTests(unittest.TestCase):
    """真要用时照常可用，且类仍是真 LangChain 类。"""

    def test_document_is_real_langchain_class(self):
        out = _run(
            "import sys; sys.path.insert(0, r'%s'); "
            "from agent_runtime.retrieval import Document; "
            "d = Document(page_content='x', metadata={'a': 1}); "
            "print(d.page_content, d.metadata['a'], type(d).__module__)" % ROOT)
        page, meta, module = out.split()
        self.assertEqual(page, "x", out)
        self.assertEqual(meta, "1", out)
        self.assertEqual(module, "langchain_core.documents.base",
                         "拿到的不是真 LangChain Document：%r" % out)

    def test_retriever_classes_are_real_langchain_subclasses(self):
        out = _run(
            "import sys; sys.path.insert(0, r'%s'); "
            "from agent_runtime.retrieval import ChromaRetriever; "
            "print(ChromaRetriever.__mro__[1].__name__)" % ROOT)
        self.assertEqual(out, "BaseRetriever", out)

    def test_get_retriever_still_builds_a_working_retriever(self):
        code = (
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "from agent_runtime.retrieval import get_retriever\n"
            "class E:\n"
            "    def embed(self, t):\n"
            "        return [[0.1, 0.2] for _ in t]\n"
            "r = get_retriever(collection='c', top_k=3, embedding_client=E(), mode='dense')\n"
            "print(type(r).__name__, r.collection_name, r.top_k)\n") % ROOT
        out = _run(code)
        name, coll, topk = out.split()
        self.assertEqual((name, coll, topk), ("ChromaRetriever", "c", "3"), out)


class CheckpointerDiagnosticTests(unittest.TestCase):
    """checkpointer 诊断在被问到时如实反映（惰性建 saver）。"""

    def test_checkpoint_backend_is_reported_after_lazy_build(self):
        out = _run(
            "import sys; sys.path.insert(0, r'%s'); "
            "import agent_runtime.game_workflow as G; "
            "print(G.WORKFLOWS.checkpoint_backend)" % ROOT)
        self.assertIn(out, ("langgraph-sqlite", "langgraph-memory", "native"), out)


if __name__ == "__main__":
    unittest.main()