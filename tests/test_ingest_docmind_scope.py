# -*- coding: utf-8 -*-
"""④ 检索层对齐（ingest 建库）：ingest_code_directory 不再把 `.docmind` 状态目录里的
工作流副本 / 检查点 / 影子整仓 embed 进代码集合，但仍照常收录 `.docmind/web/` 等合法产物。

根因：`ingest_code_directory` 的遍历原本只做
`dns[:] = [d for d in dns if d not in _SKIP_DIRS]`，而 `.docmind` 不在 `ingest._SKIP_DIRS`
里 —— 于是 `.docmind/project_stages/<wid>/workspace` 这类**项目影子副本**被整份建库，
`search_code` 因此返回重复 / 陈旧命中。这与 grep 之前踩过的**同一个坑**：工具的作用域
口径与 glob/grep 不一致，只是这里发生在 ingest-time（更靠上游）。

修法：与 glob/grep 同源 —— **只在 `.docmind/` 直接下一层**按名修剪状态目录
（复用 `code_intel._DOCMIND_STATE_DIRS` / `_DOCMIND_STATE_PREFIXES`，黑名单而非白名单，
白名单会让新出现的产物目录重新变成静默假否定）。产物目录照旧入库。ingest 自己的
`_SKIP_DIRS` 基础集**保持不动**（它与 code_intel 版不同，整体替换会放进 Library/.chroma/
uploads，属回归）。返回签名仍为 int，剪枝情况走既有 print 日志如实交代。
"""
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingest  # noqa: E402


class _StubEmb:
    """非降级、可返回任意向量的桩——本用例把 _code_file_records 整个打桩，
    其实不会真正调用 embed；这里只为通过 ingest 开头的 _reject_degraded_embeddings。"""
    degraded = False

    def embed(self, xs):
        return [[0.0, 0.0, 0.0, 0.0] for _ in xs]


class IngestDocmindScopeTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dm_ingest_scope_")
        self.seen = []

        def _fake_records(path, root, emb):
            # 只记录「遍历最终决定收录了哪些文件」，不产切片、不碰向量库
            self.seen.append(os.path.relpath(path, root).replace("\\", "/"))
            return None

        self._p_rec = mock.patch.object(
            ingest, "_code_file_records", side_effect=_fake_records)
        self._p_rec.start()
        self._p_add = mock.patch.object(ingest, "add_documents", return_value=None)
        self._p_add.start()

    def tearDown(self):
        self._p_add.stop()
        self._p_rec.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, rel, text="func NEEDLE():\n    pass\n"):
        fp = os.path.join(self.root, *rel.split("/"))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(text)

    def _run(self, rels):
        for r in rels:
            self._mk(r)
        ingest.ingest_code_directory(self.root, emb=_StubEmb())
        return set(self.seen)

    # ---- 正常源码必须照常入库 --------------------------------------------
    def test_normal_source_is_ingested(self):
        seen = self._run(["src/game.gd"])
        self.assertIn("src/game.gd", seen)

    # ---- `.docmind/` 下的产物目录必须继续入库（黑名单非白名单）-----------
    def test_docmind_product_dir_stays_indexed(self):
        seen = self._run([".docmind/web/build.js", ".docmind/web/index.html"])
        self.assertIn(".docmind/web/build.js", seen,
                      "产物目录（Godot Web 导出）被误剪——会让 search_code 对产物假否定")
        self.assertIn(".docmind/web/index.html", seen)

    def test_file_directly_under_docmind_still_indexed(self):
        """只修剪状态**子目录**；`.docmind/` 直接一层的文件照常收录。"""
        seen = self._run([".docmind/DOCMIND_RULES.md"])
        self.assertIn(".docmind/DOCMIND_RULES.md", seen)

    # ---- 状态目录（工作流副本 / 检查点 / 影子整仓）必须**不**入库 ---------
    def test_docmind_state_dirs_are_pruned(self):
        rels = [
            ".docmind/project_stages/wf-1/workspace/dup.gd",
            ".docmind/projects/proj-a/headcheck/shadow.gd",
            ".docmind/retrieval/cache.gd",
            ".docmind/screenshots/shot.gd",
        ]
        seen = self._run(rels)
        for r in rels:
            self.assertNotIn(r, seen, "%s 属状态目录副本，不得入库（否则 search_code 命中重复）" % r)

    def test_state_prefix_dir_pruned(self):
        seen = self._run([".docmind/docmind-production-visual-abc/shot.gd"])
        self.assertEqual([s for s in seen if "docmind-production-visual-" in s], [],
                         "动态命名的状态目录（前缀匹配）应被修剪")

    # ---- 剪枝只作用于 `.docmind/` 直接下一层，不得波及项目真实同名目录 ----
    def test_state_dir_name_at_project_level_not_pruned(self):
        seen = self._run(["screenshots/real.gd", "projects/mygame.gd", "retrieval/keep.py"])
        self.assertIn("screenshots/real.gd", seen,
                      "项目里真实的 screenshots/ 目录不得被误剪——剪枝仅限 .docmind 下一层")
        self.assertIn("projects/mygame.gd", seen)
        self.assertIn("retrieval/keep.py", seen)

    # ---- ingest 基础 _SKIP_DIRS 不受影响（不该把 .chroma 等重新放进遍历）--
    def test_base_skip_dirs_still_effective(self):
        # node_modules 在 ingest._SKIP_DIRS 里，应继续被整目录跳过
        seen = self._run(["node_modules/pkg/lib.gd"])
        self.assertNotIn("node_modules/pkg/lib.gd", seen,
                         "改动不得削弱 ingest 原有的 _SKIP_DIRS 过滤")

    # ---- RETR-Pollution：通用点目录（含 Agent 私有记忆）必须**不**入库 -----
    def test_dot_dirs_pruned_including_agent_memory(self):
        """grep 的 _walk_source_files 一直剪所有点开头目录，ingest 建库此前不剪，
        导致 `.workbuddy/memory/*.md` 被整篇 embed 进语义向量库、被 search_code
        当"项目代码片段"命中引用。修复后必须与 grep 同口径剪掉。"""
        seen = self._run([
            ".workbuddy/memory/2026-09-23.md",
            ".hidden/config.json",
        ])
        self.assertNotIn(".workbuddy/memory/2026-09-23.md", seen,
                         "Agent 私有记忆正文不得进向量库（RETR-Pollution）")
        self.assertNotIn(".hidden/config.json", seen,
                         "通用点目录应与 grep 一致被剪")

    # ---- RETR-Pollution：DocMind 运行/遥测点文件不得入库 --------------------
    def test_dot_files_pruned(self):
        seen = self._run([
            ".docmind_mcp.json",
            ".docmind_runtime.jsonl",
            ".docmind_tasks.jsonl",
        ])
        for rel in (".docmind_mcp.json", ".docmind_runtime.jsonl", ".docmind_tasks.jsonl"):
            self.assertNotIn(rel, seen,
                             "%s 属 DocMind 运行时/遥测台账，不得进语义库" % rel)

    # ---- 剪点目录/点文件只针对点开头，真实同名非点目录必须照常入库 --------
    def test_non_dot_same_name_dirs_not_over_pruned(self):
        seen = self._run(["memory/notes.md", "docmind_rules/keep.py"])
        self.assertIn("memory/notes.md", seen,
                      "非点目录 memory/ 不得被误剪——剪枝仅限点开头目录")
        self.assertIn("docmind_rules/keep.py", seen)


if __name__ == "__main__":
    unittest.main()
