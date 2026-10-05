# -*- coding: utf-8 -*-
"""`search_code` 必须按项目隔离 —— 这一族。

## 真实故障（真跑 `wf-0c6374c4d612`）

子代理在探测项目（只有 `calc.py` / `test_calc.py` / `README.md`）上干活，
`search_code` 却返回了：

    project.godot:L1-L39
    .docmind/web/index.js
    MiniMax_H3_提示词速查.md

——全是 **DocMind 自己仓库**的内容。它据此把调研结论写成了另一个项目，
三连任务失败（`research` / `research_retry` / `research_fix` 全部 failed）。

## 根因

`projects.code_collection(pid)` 给每个项目一个独立集合
（`docmind_code__<pid>`），`/api/ingest_code` 写进的是**它**。
而 `tools.search_code` 读的是

    get_runtime("code_collection") or CODE_COLLECTION_NAME

而**全仓从来没有**任何地方 `set_runtime("code_collection", ...)`
（实测扫遍所有 `set_runtime` 调用点，28 个键里没有它）。

于是：项目的代码被索引进它自己的集合，然后**永远不会被查询**；
每一次 `search_code` 都在查 legacy 全局集合。

实测（`.tmp/probe_collection.py`）：

    docmind_code                  -> project.godot / .docmind/web/index.js   （DocMind 仓库）
    docmind_code__prj-7bf576c...  -> 0 条                                    （探测项目）
    get_runtime('code_collection') = None -> 实际查 'docmind_code'

而 `prj-af6142eae52c`（rag-agent 自己）的集合里**确实有**它自己的文件
——说明按项目建的集合本身是好的，坏的只是没人读它。

## 为什么不能按「当前 code_root」过滤

工作流开项目舞台时，`code_root` 被覆盖成
`.docmind/project_stages/<wid>/workspace` 这个**临时副本**，而索引是从项目
**原根**建的。两者路径不同——按 `code_root` 过滤会把所有命中全删光，
`search_code` 直接废掉。

所以过滤要以**该集合自己的 ingest 根**为界，粒度天然正确。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import projects  # noqa: E402
import tools  # noqa: E402


class _Doc:
    """够用的假文档：只带 metadata.source 与 page_content。"""

    def __init__(self, source, content="x"):
        self.page_content = content
        self.metadata = {"source": source}


class SearchCodeProjectScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.root, "pkg"))
        self.inside = os.path.join(self.root, "pkg", "a.py")
        with open(self.inside, "w", encoding="utf-8") as fh:
            fh.write("print(1)\n")

    def _patch(self, docs, *, pid="prj-test", ingest_root=None, enabled=True,
               runtime_collection=None):
        """把 collection 选择与检索结果都替换掉，只测「选哪个集合 / 过不过滤」。"""
        calls = []

        def fake_retriever(**kwargs):
            calls.append(kwargs.get("collection"))
            return mock.Mock(invoke=lambda q: list(docs))

        patches = [
            mock.patch.object(projects, "enabled", lambda: enabled),
            mock.patch.object(projects, "code_collection",
                              mock.Mock(side_effect=lambda p=None: "docmind_code__%s" % (p or pid))),
            mock.patch.object(projects, "current_project_id", lambda: pid),
            mock.patch.object(projects, "get_project",
                              mock.Mock(return_value={"root": ingest_root or self.root})),
            mock.patch.object(tools, "get_retriever", fake_retriever),
            mock.patch.object(tools, "_get_code_root", lambda: self.root),
        ]
        if runtime_collection is not None:
            patches.append(mock.patch.object(tools, "get_runtime",
                                            mock.Mock(return_value=runtime_collection)))
        else:
            patches.append(mock.patch.object(
                tools, "get_runtime",
                mock.Mock(side_effect=lambda k, d=None:
                          runtime_collection if k == "code_collection" else d)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    # ---- ① 集合必须按项目选 ----

    def test_it_queries_the_projects_own_collection(self):
        calls = self._patch([_Doc("pkg/a.py")])
        tools.search_code("入口")

        self.assertEqual(calls, ["docmind_code__prj-test"],
                         "项目隔离开着时必须查该项目自己的集合，而不是 legacy 全局集合")

    def test_reverse_guard_the_legacy_collection_is_not_used_when_isolation_is_on(self):
        """**反向守卫**：项目隔离开着却仍查 legacy 集合，就是这次故障本身。

        这条断言写成「calls 里不能出现 legacy 名」而不是「必须等于某个值」，
        这样将来集合命名怎么改都不会误伤，但**回退到全局**一定会被抓住。
        """
        calls = self._patch([_Doc("pkg/a.py")])
        tools.search_code("入口")

        self.assertNotIn(tools.CODE_COLLECTION_NAME, calls)

    def test_an_explicit_runtime_collection_still_wins(self):
        """显式指定的集合优先于「按项目推断」——那是调试/测试用的后门。"""
        calls = self._patch([_Doc("pkg/a.py")], runtime_collection="explicit_coll")
        tools.search_code("入口")

        self.assertEqual(calls, ["explicit_coll"])

    def test_without_project_isolation_it_falls_back_to_the_legacy_collection(self):
        """**反向守卫**：隔离功能关闭时不能改成「按项目选」——那时没有项目概念。"""
        calls = self._patch([_Doc("pkg/a.py")], pid="legacy", enabled=False)
        tools.search_code("入口")

        self.assertEqual(calls, [tools.CODE_COLLECTION_NAME])

    # ---- ② 命中必须落在该集合自己的 ingest 根内 ----

    def test_reverse_guard_hits_outside_the_projects_ingest_root_are_dropped(self):
        """**反向守卫**：项目外的命中不许出现在结果里。

        集合切对了仍可能命中别的项目的内容（集合里留着上一次 ingest 的残留，
        或 legacy 集合里就是别人的仓库）——所以还要按 ingest 根兜一道。
        """
        self._patch([_Doc("pkg/a.py"), _Doc("../other_project/secrets.py"),
                     _Doc(os.path.join(self.tmp, "elsewhere", "b.py"))])
        out = tools.search_code("入口")

        self.assertIn("pkg/a.py", out)
        self.assertNotIn("secrets.py", out)
        self.assertNotIn("elsewhere", out)

    def test_reverse_guard_the_drop_must_not_be_silent(self):
        """**反向守卫**：全被过滤掉时不能报「代码库未找到相关内容」。

        那句话会把用户引向「换个关键词再搜」，而真因是「命中都在项目外」。
        这个误导和本次故障同一性质：证据与结论对不上。
        """
        self._patch([_Doc("../other_project/secrets.py")])
        out = tools.search_code("入口")

        self.assertNotIn("未找到相关内容", out)
        self.assertIn("过滤", out)

    def test_an_empty_result_is_still_reported_as_empty(self):
        """**反向守卫**：真的什么都没有时，「未找到」必须照旧说。"""
        self._patch([])
        out = tools.search_code("入口")

        self.assertIn("未找到", out)

    def test_it_does_not_filter_when_the_ingest_root_is_unknown(self):
        """拿不到 ingest 根时**不过滤**——宁可少一道防护，也不把工具打死。"""
        self._patch([_Doc("pkg/a.py")], ingest_root=None)
        calls = []
        with mock.patch.object(projects, "get_project", mock.Mock(return_value=None)):
            out = tools.search_code("入口")

        self.assertIn("pkg/a.py", out)

    def test_junk_metadata_does_not_raise(self):
        """脏 source（None / 空串 / 非字符串）不许把整个检索打崩。"""
        self._patch([_Doc(None), _Doc(""), _Doc(123), _Doc("pkg/a.py")])
        out = tools.search_code("入口")

        self.assertIn("pkg/a.py", out)

    def test_absolute_paths_inside_the_root_survive(self):
        """绝对路径但在根内的命中要**保留**——不能只认相对路径。"""
        self._patch([_Doc(os.path.join(self.root, "pkg", "a.py"))])
        out = tools.search_code("入口")

        self.assertIn("a.py", out)


if __name__ == "__main__":
    unittest.main()