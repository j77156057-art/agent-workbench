import unittest
from unittest.mock import patch

from agent_runtime.retrieval import (BM25Index, ChromaRetriever, Document,
                                     HybridRetriever, get_lexical_index,
                                     invalidate_lexical_index, lexical_status,
                                     get_retriever, retrieval_events,
                                     retrieve_context, status)


class _Embedding:
    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


class RetrieverAdapterTests(unittest.TestCase):
    def test_chroma_retriever_returns_langchain_documents(self):
        with patch("agent_runtime.retrieval.vector_query", return_value={
            "documents": [["alpha", "beta"]],
            "metadatas": [[{"source": "a.py", "start_line": 3}, {"source": "b.py"}]],
            "distances": [[0.1, 0.4]],
        }) as query:
            docs = ChromaRetriever(collection="code", top_k=2,
                                   embedding_client=_Embedding()).invoke("find alpha")
        query.assert_called_once_with([0.1, 0.2], k=2, collection="code")
        self.assertEqual([doc.page_content for doc in docs], ["alpha", "beta"])
        self.assertEqual(docs[0].metadata["source"], "a.py")
        self.assertEqual(docs[0].metadata["distance"], 0.1)

    def test_source_allowlist_is_forwarded_to_dense_query(self):
        with patch("agent_runtime.retrieval.vector_query", return_value={
            "documents": [["allowed"]], "metadatas": [[{"source": "safe.gd"}]],
            "distances": [[0.1]], "ids": [["safe"]],
        }) as query:
            docs = ChromaRetriever(collection="code", top_k=1,
                                   source_allowlist=["safe.gd"],
                                   embedding_client=_Embedding()).invoke("find")
        query.assert_called_once_with([0.1, 0.2], k=1, collection="code",
                                      where={"source": {"$in": ["safe.gd"]}})
        self.assertEqual(docs[0].metadata["source"], "safe.gd")

    def test_factory_and_status_keep_component_boundary(self):
        retriever = get_retriever(collection="docs", top_k=5,
                                   embedding_client=_Embedding(), mode="dense")
        self.assertEqual(retriever.collection_name, "docs")
        self.assertEqual(retriever.top_k, 5)
        report = status(collection="docs", top_k=5)
        self.assertEqual(report["backend"], "hybrid")
        self.assertTrue(report["langchain_core"])

    def test_retrieve_context_is_bounded_and_keeps_source_locations(self):
        with patch("agent_runtime.retrieval.vector_query", side_effect=[
            {"documents": [["function body"]],
             "metadatas": [[{"source": "game.py", "start_line": 12}]]},
            {"documents": [["knowledge body"]],
             "metadatas": [[{"source": "design.md"}]]},
        ]):
            evidence = retrieve_context("movement", collections={
                "code": "code", "knowledge": "knowledge"},
                top_k=1, max_chars=500, embedding_client=_Embedding(), mode="dense")
        self.assertIn("code | game.py:L12", evidence)
        self.assertIn("knowledge | design.md", evidence)
        self.assertLessEqual(len(evidence), 500)

    def test_bm25_prefers_exact_terms(self):
        index = BM25Index(["movement controller jump", "inventory crafting", "movement movement"])
        scores = index.scores("movement")
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[2], scores[1])

    def test_hybrid_fuses_dense_and_lexical_candidates(self):
        class Collection:
            def get(self, **_kwargs):
                return {
                    "ids": ["lex", "dense"],
                    "documents": ["exact movement controller", "unrelated text"],
                    "metadatas": [{"source": "a.py"}, {"source": "b.py"}],
                }

        with patch("agent_runtime.retrieval.get_collection", return_value=Collection()), \
                patch("agent_runtime.retrieval.vector_query", return_value={
                    "ids": [["dense"]],
                    "documents": [["unrelated text"]],
                    "metadatas": [[{"source": "b.py"}]],
                    "distances": [[0.1]],
                }):
            retriever = HybridRetriever(collection="docs", top_k=2, candidate_k=2,
                                        embedding_client=_Embedding(), reranker=None)
            docs = retriever.invoke("exact movement controller")
        self.assertEqual({doc.metadata.get("_id") for doc in docs}, {"lex", "dense"})
        self.assertEqual(docs[0].metadata["_id"], "lex")
        self.assertIn("bm25_score", docs[0].metadata)

    def test_retrieval_ledger_records_rank_scores_and_latency(self):
        class Collection:
            def get(self, **_kwargs):
                return {
                    "ids": ["lex", "dense"],
                    "documents": ["exact movement controller", "unrelated text"],
                    "metadatas": [{"source": "player.gd", "start_line": 4},
                                  {"source": "other.gd"}],
                }

        with patch("agent_runtime.retrieval._RETRIEVAL_EVENTS", __import__("collections").deque(maxlen=200)), \
                patch("agent_runtime.retrieval.get_collection", return_value=Collection()), \
                patch("agent_runtime.retrieval.vector_query", return_value={
                    "ids": [["dense"]],
                    "documents": [["unrelated text"]],
                    "metadatas": [[{"source": "other.gd"}]],
                    "distances": [[0.1]],
                }):
            HybridRetriever(collection="trace-docs", top_k=2, candidate_k=2,
                                   embedding_client=_Embedding(), reranker=None).invoke("exact movement")
            events = retrieval_events(10)
        self.assertTrue(events)
        event = next(item for item in events if item.get("mode") == "hybrid")
        self.assertEqual(event["collection"], "trace-docs")
        self.assertGreaterEqual(event["duration_ms"], 0)
        self.assertEqual(event["documents"][0]["source"], "player.gd")
        self.assertIn("hybrid_score", event["documents"][0])

    def test_hybrid_rerank_mode_forces_cross_encoder(self):
        with patch("agent_runtime.retrieval.SentenceTransformerReranker") as reranker:
            instance = object()
            reranker.return_value = instance
            result = get_retriever(collection="docs", top_k=2,
                                   embedding_client=_Embedding(), mode="hybrid_rerank")
        reranker.assert_called_once()
        self.assertIs(result._reranker, instance)

    def test_lexical_index_persists_and_reuses_snapshot(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"DOCMIND_LEXICAL_BACKEND": "snapshot",
                                "DOCMIND_LEXICAL_INDEX_DIR": tmp,
                                "DOCMIND_LEXICAL_PERSIST": "1"}), \
                patch("agent_runtime.retrieval._LEXICAL_MEMORY", {}), \
                patch("agent_runtime.retrieval._LEXICAL_STATS", {}):
            docs = [Document(page_content="movement controller", metadata={"_id": "a", "source": "player.gd"}),
                    Document(page_content="inventory", metadata={"_id": "b", "source": "inventory.gd"})]
            first, first_meta = get_lexical_index("code", docs)
            self.assertEqual(first.scores("movement")[0] > 0, True)
            self.assertEqual(first_meta["rebuilds"], 1)
            invalidate_lexical_index("code")
            second, second_meta = get_lexical_index("code", docs)
            self.assertEqual(second.scores("movement")[0] > 0, True)
            self.assertEqual(second_meta["disk_loads"], 1)
            self.assertEqual(lexical_status("code")["memory_cached"], True)

    def test_lexical_index_reports_incremental_delta_after_change(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"DOCMIND_LEXICAL_INDEX_DIR": tmp, "DOCMIND_LEXICAL_PERSIST": "1"}), \
                patch("agent_runtime.retrieval._LEXICAL_MEMORY", {}), \
                patch("agent_runtime.retrieval._LEXICAL_STATS", {}):
            original = [Document(page_content="alpha", metadata={"_id": "a"}),
                        Document(page_content="beta", metadata={"_id": "b"})]
            changed = [Document(page_content="alpha updated", metadata={"_id": "a"}),
                       Document(page_content="gamma", metadata={"_id": "c"})]
            get_lexical_index("docs", original)
            invalidate_lexical_index("docs")
            _, meta = get_lexical_index("docs", changed)
            self.assertEqual(meta["last_delta"], {"added": 1, "removed": 1, "updated": 1})

    def test_sqlite_backend_reuses_postings_without_chroma_snapshot(self):
        import tempfile
        class Collection:
            def __init__(self):
                self.get_calls = 0

            def get(self, **_kwargs):
                self.get_calls += 1
                return {
                    "ids": ["lex", "dense"],
                    "documents": ["exact movement controller", "unrelated text"],
                    "metadatas": [{"source": "a.py"}, {"source": "b.py"}],
                }

        collection = Collection()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"DOCMIND_LEXICAL_BACKEND": "sqlite",
                                "DOCMIND_LEXICAL_INDEX_DIR": tmp}, clear=False), \
                patch("agent_runtime.retrieval._LEXICAL_MEMORY", {}), \
                patch("agent_runtime.retrieval._LEXICAL_SQLITE", {}), \
                patch("agent_runtime.retrieval._LEXICAL_STATS", {}), \
                patch("agent_runtime.retrieval.get_collection", return_value=collection), \
                patch("agent_runtime.retrieval.vector_query", return_value={
                    "ids": [["dense"]],
                    "documents": [["unrelated text"]],
                    "metadatas": [[{"source": "b.py"}]],
                    "distances": [[0.1]],
                }):
            retriever = HybridRetriever(collection="sqlite-docs", top_k=2,
                                        candidate_k=2, embedding_client=_Embedding(), reranker=None)
            first = retriever.invoke("exact movement controller")
            second = retriever.invoke("exact movement controller")
        self.assertEqual(collection.get_calls, 1)
        self.assertEqual(first[0].metadata["_id"], "lex")
        self.assertEqual(second[0].metadata["_id"], "lex")
        self.assertEqual(second[0].metadata["lexical_index"], "persistent")

    def test_sqlite_backend_applies_incremental_mutations(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"DOCMIND_LEXICAL_BACKEND": "sqlite",
                                "DOCMIND_LEXICAL_INDEX_DIR": tmp}, clear=False), \
                patch("agent_runtime.retrieval._LEXICAL_SQLITE", {}), \
                patch("agent_runtime.retrieval._LEXICAL_STATS", {}):
            original = [Document(page_content="alpha", metadata={"_id": "a"}),
                        Document(page_content="beta", metadata={"_id": "b"})]
            changed = [Document(page_content="alpha updated", metadata={"_id": "a"}),
                       Document(page_content="gamma", metadata={"_id": "c"})]
            index, first = get_lexical_index("sqlite-delta", original)
            self.assertEqual([hit.key for hit in index.query_hits("beta", 5)], ["b"])
            invalidate_lexical_index("sqlite-delta")
            index, second = get_lexical_index("sqlite-delta", changed)
            self.assertEqual(second["last_delta"], {"added": 1, "removed": 1, "updated": 1})
            self.assertEqual(index.query_hits("beta", 5), [])
            self.assertEqual([hit.key for hit in index.query_hits("gamma", 5)], ["c"])


if __name__ == "__main__":
    unittest.main()
