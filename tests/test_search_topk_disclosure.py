"""search_code 的 top-K「可能还有更多」计数披露。

背景：语义检索走 get_retriever(top_k=TOP_K)，检索器内部已把候选切到 TOP_K。
所以当返回条数恰好取满 TOP_K 时，意味着排序靠后的相关片段被配额截掉了。
此前这一点没有如实交代，模型会把"返回的这几条"当成全部命中、据此报"总数/
都找到了"——与本次钻测的 grep 触顶故障同一性质。这里锁住披露：取满必标注，
未取满不标注，空结果走原早退分支不受影响。

全程离线打桩，不依赖真实 embedding / chromadb。
"""

import unittest
from unittest import mock

import tools


class _Doc:
    """最小 Document 替身：search_code 只读 page_content 与 metadata。"""

    def __init__(self, source, text):
        self.page_content = text
        self.metadata = {"source": source, "start_line": 1, "end_line": 2,
                         "lang": "python", "kind": "code"}


class _StubRetriever:
    def __init__(self, docs):
        self._docs = docs

    def invoke(self, query):  # noqa: ARG002 - 接口需收 query
        return list(self._docs)


def _run_search(retrieved_docs, top_k):
    """在打桩环境下调用 tools.search_code，返回其字符串输出。"""
    with mock.patch.object(tools, "_get_code_root", return_value="/fake/root"), \
            mock.patch.object(tools, "_project_code_scope", return_value=(None, None)), \
            mock.patch.object(tools, "_get_emb", return_value=None), \
            mock.patch.object(tools, "TOP_K", top_k), \
            mock.patch.object(tools, "get_retriever",
                              return_value=_StubRetriever(retrieved_docs)):
        return tools.search_code("hero_damage")


class SearchCodeTopKDisclosureTests(unittest.TestCase):
    def test_quota_filled_adds_disclosure(self):
        # 取满 TOP_K=4：必须标注「已取满 / 不要把这几条当全部」
        docs = [_Doc("a.py", "alpha code"), _Doc("b.py", "beta code"),
                _Doc("c.py", "gamma code"), _Doc("d.py", "delta code")]
        out = _run_search(docs, top_k=4)
        self.assertIn("已取满", out)
        self.assertIn("不要把这几条当全部", out)
        # 命中内容照常返回，披露是**追加**而非替换
        self.assertIn("a.py", out)

    def test_not_quota_filled_has_no_disclosure(self):
        # 只返回 2 条 < TOP_K=4：配额没占满，不该吓唬人
        docs = [_Doc("a.py", "alpha code"), _Doc("b.py", "beta code")]
        out = _run_search(docs, top_k=4)
        self.assertNotIn("已取满", out)
        self.assertIn("a.py", out)

    def test_empty_result_uses_early_return(self):
        # 空命中：走既有早退分支，不应崩、也不追加 top-K 披露
        out = _run_search([], top_k=4)
        self.assertNotIn("已取满", out)
        self.assertTrue(str(out).strip())

    def test_disclosure_grows_with_larger_top_k(self):
        # 披露里的数字应等于当前 TOP_K，不是硬编码 4
        docs = [_Doc("a.py", "one"), _Doc("b.py", "two")]
        out = _run_search(docs, top_k=2)  # 2 条恰好取满 TOP_K=2
        self.assertIn("前 2 条", out)


if __name__ == "__main__":
    unittest.main()
