# -*- coding: utf-8 -*-
"""知识库检索的输出必须说清「这是知识库文档」，且不得把同一段文字重复 N 遍。

## 真实故障（真跑 `wf-37f43a4c042f`）

探测项目只有 `calc.py` / `test_calc.py` / `README.md`，子代理拿到的却是：

    [MiniMax_H3_提示词速查.md] # MiniMax H3 提示词速查（本地 ComfyUI 版）
    > 本机节点 `MiniMaxH3ImageToVideo` 只接受一个多行 prompt 字符串
      （`comfy_extras/nodes_minimax_h3.py`）

输出里**没有任何「这来自知识库」的标记**（实测 `含 知识库 = False`），读起来
就是项目里的一个文件；文档本身还写着「**本机**节点」「**本地** ComfyUI 版」，
更像项目事实。于是子代理转去 `read_file` / `grep` 追
`comfy_extras/nodes_minimax_h3.py`，五步全花在无关文档上，`calc.py` 一眼没看。

## 第二件事：同一段文字被返回 3 次

实测知识库里同一份文档存了 3 份，`source` 分别是

    39b47b5d58ce44c79099e9f01997bdff_MiniMax_H3_提示词速查.md
    33bab3b424d34dd58eb46490c04b839f_MiniMax_H3_提示词速查.md
    c2d1d387e91e48f29dae1b44265db4b7_MiniMax_H3_提示词速查.md

`chunk` 都是 `'0'`、内容一字不差。`_dedup_docs` 的去重键是
`(source, 文本)` —— source 不同，于是三条全都留下，同一段文字在一次检索里
出现 3 遍：既浪费上下文，又容易让模型误以为「多个来源互相印证」。

而 `_dedup_docs` 的 docstring 写的意图恰恰是防这个（「避免重复片段浪费
token、并防止 Agent 误以为『信息很多』而空转」）——键选错了，意图没兑现。

## 为什么不给知识库加「项目隔离」

知识库是**单个全局共享集合**（`COLLECTION_NAME = "docmind"`），
`projects.py` 里只有 `code_collection`，没有知识库集合的概念，
`knowledge_collection` 这个运行时键全仓从未被设置过。

给它建 per-project 集合会改掉上传语义（在项目 A 传的文档，项目 B 就看不见了），
那是产品决定。所以这里修的是**输出层能修的两件事**：来源要说清、重复要去掉。

## 层归属

纯层：只替换检索器返回值，不碰向量库、不碰上传路径。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402


class _Doc:
    def __init__(self, source, content):
        self.page_content = content
        self.metadata = {"source": source, "chunk": "0"}


BODY = "# MiniMax H3 提示词速查（本地 ComfyUI 版）\n> 本机节点只接受多行 prompt 字符串。"


def _patch(docs):
    retriever = mock.Mock(invoke=lambda q: list(docs))
    patches = [
        mock.patch.object(tools, "get_retriever", lambda **_kw: retriever),
        mock.patch.object(tools, "_get_emb", lambda: None),
        mock.patch.object(tools, "TOP_K", 5),
    ]
    for p in patches:
        p.start()
    return patches


class SearchKnowledgeProvenanceTests(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_patches", []):
            p.stop()
        self._patches = []

    def _run(self, docs):
        self._patches = _patch(docs)
        return tools.search_knowledge("MiniMax H3")

    # ---- ① 来源要说清 ----

    def test_reverse_guard_a_hit_must_be_marked_as_a_knowledge_document(self):
        """**反向守卫**：知识库命中不许读起来像项目里的一个文件。

        这条是本次故障的直接原因：模型据此认定「本项目有 ComfyUI 节点」，
        然后五步都在追那个不存在的文件。
        """
        out = self._run([_Doc("39b47b5d58ce44c79099e9f01997bdff_速查.md", BODY)])

        self.assertIn("知识库", out)
        self.assertNotIn("提示词速查.md] #", out.replace("知识库文档：", ""))

    def test_it_still_returns_the_content(self):
        """**反向守卫**：加标记不许把内容吃掉——那会让工具变得不可用。"""
        out = self._run([_Doc("abc_速查.md", BODY)])

        self.assertIn("本机节点", out)

    def test_an_empty_result_is_still_reported_as_empty(self):
        out = self._run([])

        self.assertIn("未找到", out)

    # ---- ② 同一段文字不许重复 N 遍 ----

    def test_reverse_guard_the_same_text_under_three_uuid_sources_appears_once(self):
        """**反向守卫**：同一份文档存了 N 份时，只出现一次。

        这是实测到的形状：三条 source 只差 uuid 前缀，`chunk` 与内容一字不差。
        """
        out = self._run([
            _Doc("39b47b5d58ce44c79099e9f01997bdff_速查.md", BODY),
            _Doc("33bab3b424d34dd58eb46490c04b839f_速查.md", BODY),
            _Doc("c2d1d387e91e48f29dae1b44265db4b7_速查.md", BODY),
        ])

        self.assertEqual(out.count("本机节点"), 1,
                         "同一段文字返回了 %d 次" % out.count("本机节点"))

    def test_reverse_guard_distinct_chunks_of_one_document_all_survive(self):
        """**反向守卫**：去重不许把**同一文档的不同片段**合并掉。

        只按文本去重会在这里咬人：片段内容不同、但 display source 相同——
        那是同一份文档的两段，必须都在。
        """
        out = self._run([
            _Doc("abc_速查.md", BODY),
            _Doc("def_速查.md", "# 第二段\n另一段内容，FPS 固定 24。"),
        ])

        self.assertIn("本机节点", out)
        self.assertIn("FPS 固定 24", out)

    def test_reverse_guard_distinct_documents_with_identical_text_are_not_collapsed_blindly(self):
        """**反向守卫**：两个**真不同**的文档即便正文一字不差，也各自保留一份。

        存在的理由：知识库里可能有 `a/说明.md` 与 `b/说明.md` 这种同名文档。
        display source 相同但**原始** source 不同——保留两份比合并更安全：
        少一条信息好过把两份不同来源的证据说成一份。
        """
        out = self._run([
            _Doc("u1_说明.md", "完全相同的一段正文"),
            _Doc("u2_说明.md", "完全相同的一段正文"),
        ])

        self.assertEqual(out.count("完全相同的一段正文"), 2)


if __name__ == "__main__":
    unittest.main()