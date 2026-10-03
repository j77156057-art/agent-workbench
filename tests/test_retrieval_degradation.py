"""向量检索的**降级路径**必须真的能用——一类"降级存在、但降级后是坏的"的兜底。

## 三个缺口，同一个根因

`EmbeddingClient` 在 Ollama 探测失败时会降级成本地哈希词袋（256 维），而
bge-m3 是 1024 维。于是出现两条坏路径：

| | 触发 | 后果 |
|---|---|---|
| 查询侧 | 索引用 1024 维建好，Ollama 掉线 -> 查询向量 256 维 | `vectorstore.query` 抛 ValueError |
| **入库侧** | Ollama 掉线时上传文档 -> **256 维**向量先入库，把集合维度钉死 | 集合被灌入**无意义**向量；Ollama 恢复后维度又不匹配了 |

入库侧更隐蔽：它不报错。集合维度被 Chroma 在首次 insert 时固定，于是
"看起来入库成功了"，但那些向量是哈希词袋——**知识库检索会静默返回垃圾**。

第三条：`HybridRetriever` 的 dense 腿（`retrieval.py` 里 `ChromaRetriever(...).invoke`）
**没有任何 try/except**。所以上面那个 ValueError 会掀掉整次检索——哪怕它后面
还有一条完好的 BM25 词法腿根本用不上。名为 hybrid，实际是「一腿断、全断」。

根因与本项目最近修的一串 bug 相同：**只有降级代码，没有验证降级后是否真能用**。
"""

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ingest  # noqa: E402
from config import LOCAL_EMBED_DIM  # noqa: E402
from embeddings import EmbeddingClient  # noqa: E402


class DegradedClientIsVisibleTests(unittest.TestCase):
    """降级这件事本身必须能被调用方看见，否则它就是个静默副作用。"""

    def _degraded(self):
        """构造一个「本该用 ollama、但探测失败」的客户端。"""
        with patch.object(EmbeddingClient, "_probe_dim",
                          side_effect=RuntimeError("连接被拒绝")):
            return EmbeddingClient()

    def test_it_reports_that_it_degraded(self):
        client = self._degraded()
        self.assertTrue(getattr(client, "degraded", False),
                        "Ollama 探测失败后没有标记降级——调用方无法察觉，"
                        "于是拿着 256 维向量去查 1024 维的集合")
        self.assertTrue(getattr(client, "degraded_reason", ""),
                        "降级必须带原因，否则无法告诉用户到底该做什么")

    def test_the_degraded_vector_is_the_local_fallback_dimension(self):
        """降级后维度确实变成哈希词袋的维度——这正是维度不匹配的来源。"""
        client = self._degraded()
        self.assertEqual(client.dim, LOCAL_EMBED_DIM)
        self.assertEqual(len(client.embed(["一段文字"])[0]), LOCAL_EMBED_DIM)

    def test_an_explicitly_chosen_local_provider_is_not_a_failure(self):
        """用户主动选local 是**决定**，不是故障——不该被标成 degraded。

        这个区分很重要：入库侧要拦的是「意外降级后写入无意义向量」，
        而不是拦住用户明确选择的离线模式。
        """
        with patch("embeddings.get_runtime", return_value="local"), \
             patch("embeddings.EMBEDDING_PROVIDER", "local"):
            client = EmbeddingClient()
        self.assertFalse(getattr(client, "degraded", False),
                         "用户主动选 local 被当成故障了——于是入库守卫会拦下"
                         "一个完全合法的离线配置")


class IngestRefusesToPoisonTheCollectionTests(unittest.TestCase):
    """入库侧：意外的降级不得悄悄把集合维度钉死在无意义向量上。"""

    def test_it_refuses_when_the_client_silently_degraded(self):
        class Degraded:
            degraded = True
            degraded_reason = "ollama 探测失败"

            def embed(self, texts):
                return [[0.0] * LOCAL_EMBED_DIM for _ in texts]

        with patch.object(ingest, "load_text", return_value="一些正文"), \
             patch.object(ingest, "add_documents") as add:
            with self.assertRaises(Exception) as caught:
                ingest.ingest_file("x.md", emb=Degraded())

        self.assertFalse(add.called,
                         "向量已经在写——集合维度会被首次 insert 钉死在 256 维，"
                         "而那些是哈希词袋，不是语义向量。检索会静默返回垃圾。")
        text = str(caught.exception)
        self.assertTrue("Ollama" in text or "检索" in text,
                        "报错必须说清是向量/检索的问题：%r" % text)

    def test_it_still_ingests_with_an_explicitly_local_client(self):
        """用户主动选 local：维度一致，照常入库。"""
        class ExplicitLocal:
            degraded = False
            degraded_reason = ""

            def embed(self, texts):
                return [[0.0] * LOCAL_EMBED_DIM for _ in texts]

        with patch.object(ingest, "load_text", return_value="一些正文"), \
             patch.object(ingest, "add_documents") as add:
            n = ingest.ingest_file("x.md", emb=ExplicitLocal())

        self.assertEqual(n, 1)
        add.assert_called_once()


def _lexical_over(docs):
    """构造一个真实的 BM25Index。

    mock 返回 None 会让被测代码在别处崩掉，那样测的就不是
    「dense 挂了会怎样」而是「我 mock 得对不对」——一盏测不出目标行为的灯。
    """
    from agent_runtime import retrieval as R

    index = R.BM25Index([d.page_content for d in docs])
    return lambda name, corpus=None: (index, {"ready": True,
                                              "documents": len(docs),
                                              "backend": "memory"})


class HybridRetrieverSurvivesADeadDenseLegTests(unittest.TestCase):
    """名为 hybrid，就该真的是 hybrid：一腿断，另一腿还能用。"""

    def test_dense_failure_still_returns_lexical_results(self):
        from langchain_core.documents import Document

        from agent_runtime import retrieval as R

        docs = [
            Document(page_content="扣血逻辑在 enemy.gd 里", metadata={"source": "enemy.gd"}),
            Document(page_content="无关内容", metadata={"source": "other.gd"}),
        ]
        retriever = R.HybridRetriever(collection="kb", top_k=3, candidate_k=5)

        boom = RuntimeError(
            "Collection expecting embedding with dimension of 1024, got 256")

        with patch.object(R, "get_lexical_index", side_effect=_lexical_over(docs)), \
             patch.object(R, "_collection_snapshot", return_value=docs), \
             patch.object(R, "_lexical_backend", return_value="memory"), \
             patch.object(R.ChromaRetriever, "invoke", side_effect=boom):
            out = retriever._get_relevant_documents("扣血")

        self.assertTrue(out, "dense 腿挂了就把整次检索丢了——"
                             "可 BM25 腿明明还能给出结果")
        self.assertTrue(any("扣血" in d.page_content for d in out),
                        "关键词完全匹配的文档必须能被词法腿捞回来")

    def test_a_healthy_dense_leg_still_contributes(self):
        """反向对照：dense 正常时，结果里应出现只有向量能命中的文档。

        没有这条，"dense 腿其实从来没生效过"也会让上一条测试变绿。
        """
        from langchain_core.documents import Document

        from agent_runtime import retrieval as R

        corpus = [Document(page_content="扣血逻辑", metadata={"source": "enemy.gd"})]
        dense_only = Document(page_content="语义相近但字面毫无交集的段落",
                              metadata={"source": "semantic.md"})
        retriever = R.HybridRetriever(collection="kb", top_k=3, candidate_k=5)

        with patch.object(R, "get_lexical_index", side_effect=_lexical_over(corpus)), \
             patch.object(R, "_collection_snapshot", return_value=corpus), \
             patch.object(R, "_lexical_backend", return_value="memory"), \
             patch.object(R.ChromaRetriever, "invoke", return_value=[dense_only]):
            out = retriever._get_relevant_documents("扣血")

        sources = {d.metadata.get("source") for d in out}
        self.assertIn("semantic.md", sources,
                      "dense 腿的命中没进结果——那上一条测试只是因为它坏了才绿")
        self.assertIn("enemy.gd", sources, "词法腿的命中丢了")


class CodeIndexDegradesInsteadOfBlockingTests(unittest.TestCase):
    """代码的语义索引是**辅助**能力——它挂了不该让「选项目」整个失败。

    这里的教训来自本轮自己差点犯的错：守卫加在 ``ingest_code_directory`` 上之后，
    /api/ingest_code 会返回 400，而 ``set_runtime("code_root")`` 在它**之后**——
    于是「为了一个可选索引，项目根本选不上了」。

    这与刚修掉的「检索不可用封掉对话」是同一个错误，只是换了一层：代码检索真正
    靠的是 grep / read_file / search_code，全程不碰向量。
    """

    def _ingest_code(self, ingest_raises):
        import asyncio
        import json

        import api

        with patch.object(api, "ingest_code_directory", side_effect=ingest_raises), \
             patch.object(api, "reset_collection"), \
             patch.object(api, "count", return_value=0), \
             patch.object(api, "load_project_rules", return_value=""), \
             patch.object(api, "save_state"), \
             patch.object(api, "_agent_for"), \
             patch.object(api, "_stop_other_engines", return_value=([], [])), \
             patch.object(api, "set_runtime") as set_runtime, \
             patch.object(api.projects, "current_project_id", return_value=""), \
             patch.object(api.projects, "code_collection", return_value="code"), \
             patch.object(api.projects, "enabled", return_value=False):
            # 用真实存在的目录（仓库自身）：端点开头有 os.path.isdir 校验，
            # mock 掉它不如给个真目录——后者才不依赖对该校验的假设。
            resp = asyncio.run(api.ingest_code(root=ROOT))
            # 成功路径返回普通 dict，失败路径返回 JSONResponse——两种都要吃得下。
            # 只处理其中一种的测试，会在另一条路径改动时给出误导性的 AttributeError。
            if isinstance(resp, dict):
                payload = resp
            else:
                payload = json.loads(resp.body) if resp.body else {}
            return payload, set_runtime

    def test_project_still_gets_selected_when_embeddings_are_degraded(self):
        payload, set_runtime = self._ingest_code(
            ValueError("向量模型当前不可用，已停止入库"))
        self.assertTrue(payload.get("ok"),
                        "项目没被选中：%s" % payload.get("error"))
        keys = {c.args[0] for c in set_runtime.call_args_list if c.args}
        self.assertIn("code_root", keys,
                      "code_root 没被设置——用户会看到「项目没选上」，"
                      "而原因只是一个可选的语义索引建不起来")

    def test_it_says_what_is_lost_and_what_is_not(self):
        payload, _ = self._ingest_code(
            ValueError("向量模型当前不可用，已停止入库"))
        notice = payload.get("notice", "")
        self.assertIn("grep", notice,
                      "必须说清代码检索不受影响，否则用户以为整个项目都不能用了")

    def test_a_healthy_backend_indexes_normally(self):
        payload, set_runtime = self._ingest_code(None)
        self.assertTrue(payload.get("ok"))
        keys = {c.args[0] for c in set_runtime.call_args_list if c.args}
        self.assertIn("code_root", keys)


class SearchKnowledgeNeverThrowsTests(unittest.TestCase):
    """`search_knowledge` 之前没有任何 try/except：检索一坏就变成裸异常。"""

    def test_it_reports_instead_of_raising(self):
        import tools

        with patch.object(tools, "get_retriever") as gr:
            gr.return_value.invoke.side_effect = ValueError(
                "查询向量维度与集合不匹配（当前输入 256 维）")
            result = tools.search_knowledge("扣血逻辑")

        self.assertIsInstance(result, str)
        self.assertIn("检索", result)
        self.assertNotIn("Traceback", result)


if __name__ == "__main__":
    unittest.main()