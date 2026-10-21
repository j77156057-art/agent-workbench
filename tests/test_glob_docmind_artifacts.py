# -*- coding: utf-8 -*-
"""glob 必须看得见 `.docmind/` 下的产物，且如实交代哪里没扫。

## 实测事故（2026-10-10）

交付代理在 `D:\\WorkBuddy\\godot_sample` 里跑 `dev_glob('**/*.html')`，
回报「没有命中文件」，而 `.docmind/web/index.html` 就在脚下 ——
Godot 的 Web 导出默认就写在 `.docmind/web/`（见 `export_presets.cfg`
的 `export_path`）。

根因：`agent_runtime/code_intel.py` 的 `_SKIP_DIRS` 里有 `.docmind`，
整个目录被剪掉，`notes` 为空 -> 一个**没有任何保留的假否定**。
它比"扫到一半被截断"更危险：读起来就是"这个项目没有 html"。

## 契约

* `.docmind/` 下的**产物**（`web/` 等）必须可被 glob 命中；
* `.docmind/` 下的**状态**目录（project_stages / projects / …）仍不扫
  （实测 project_stages 一个就 752MB/6978 文件，projects 下还有 headcheck
  整仓影子副本）；
* 没扫的目录必须**如实列出**，"没有命中"不许读成"这里没有"；
* 冒出的列表只含 docmind 状态目录，不把 `.git` 之类混进来。
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import code_intel          # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="docmind_glob_"))


def tearDownModule():
    shutil.rmtree(SB, ignore_errors=True)


def _mk(root: pathlib.Path, rel: str, content: str = "x") -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class GlobSeesDocmindArtifactsTests(unittest.TestCase):
    """`.docmind/web/` 里的产物必须找得到。"""

    def setUp(self):
        self.root = SB / ("proj-%s" % self.id().rsplit(".", 1)[-1])
        self.root.mkdir(parents=True, exist_ok=True)

    def _glob(self, pat, scope=""):
        return code_intel.glob_files(str(self.root), pat, scope=scope)

    def test_godot_web_export_is_findable(self):
        """**核心判据**：Godot 导出到 `.docmind/web/` 的页面必须能被找到。"""
        _mk(self.root, ".docmind/web/index.html", "<html>StarVoyager</html>")
        _mk(self.root, ".docmind/web/index.wasm", "wasm")
        r = self._glob("**/*.html")
        self.assertIn(".docmind/web/index.html", r["files"],
                      "`.docmind/` 下的导出产物找不到 —— 这正是那次 0/5 全败的形态")
        self.assertEqual(r["count"], 1)

    def test_wasm_and_pck_also_visible(self):
        _mk(self.root, ".docmind/web/index.wasm")
        _mk(self.root, ".docmind/web/index.pck")
        for pat in ("**/*.wasm", "**/*.pck"):
            r = self._glob(pat)
            self.assertEqual(r["count"], 1, "%s 没找到" % pat)

    def test_other_docmind_subdirs_are_not_blanket_hidden(self):
        """不能又变成"只有白名单目录可见"—— 新产物目录也要能出来。"""
        _mk(self.root, ".docmind/some_new_output/app.js")
        r = self._glob("**/*.js")
        self.assertIn(".docmind/some_new_output/app.js", r["files"],
                      "`.docmind/` 下新的产物目录又被藏起来了")

    # ---------- 状态目录仍然不扫 ----------

    def test_state_dirs_are_still_pruned(self):
        _mk(self.root, ".docmind/web/index.html")
        for d in ("project_stages", "projects", "media-fixture", "page-action"):
            _mk(self.root, ".docmind/%s/hidden.html" % d)
        r = self._glob("**/*.html")
        self.assertIn(".docmind/web/index.html", r["files"])
        self.assertNotIn(".docmind/project_stages/hidden.html", r["files"],
                         "状态目录又被扫进来了（752MB/6978 文件，会拖垮 glob）")

    def test_headcheck_shadow_copies_stay_hidden(self):
        """影子副本必须继续藏住 —— 见 docs/known-defect-docmind-shadow-repo-copies.md。"""
        _mk(self.root, ".docmind/projects/prj-x/headcheck-abc/tools.py")
        _mk(self.root, "tools.py")
        r = self._glob("**/tools.py")
        self.assertEqual(r["files"], ["tools.py"],
                         "影子副本被 glob 出来了")

    # ---------- 如实交代 ----------

    def test_state_pruning_only_applies_under_docmind(self):
        """⛔ 反向守卫：状态目录修剪**只在 `.docmind/` 下一层**生效。

        为什么必须有这条：第一版 `_prune` 写成 `if True`（不判断当前位置），
        于是用户项目自己建的 `screenshots/`、`workflows/`、`projects/` 也被剪掉 ——
        把"Agent 工作台 的状态目录"误当成"任何项目都该跳过的名字"。
        实测那次 7 个变异里只有这条没被抓住，说明先前的判据全靠 `.docmind/`
        下的形态，**从没测过同名的项目自有目录**。
        """
        # 项目自有的同名目录 —— 必须能被找到
        for d in ("project_stages", "projects", "media-fixture", "page-action",
                  "screenshots", "workflows", "retrieval"):
            _mk(self.root, "%s/keep.txt" % d)
        r = self._glob("**/keep.txt")
        got = sorted(r["files"])
        expect = sorted("%s/keep.txt" % d for d in
                        ("project_stages", "projects", "media-fixture", "page-action",
                         "screenshots", "workflows", "retrieval"))
        self.assertEqual(got, expect,
                         "项目自有的同名目录被误剪了 —— 状态修剪越过了 .docmind 边界")
        # 且 pruned_dirs 不该出现这些（它们不是 .docmind 下的）
        for d in r["pruned_dirs"]:
            self.assertNotIn(d, ("project_stages", "projects", "screenshots"),
                             "%s 被当成了 Agent 工作台 状态目录" % d)

    def test_docmind_state_pruning_is_one_level_deep(self):
        """`.docmind/a/b/` 这种更深层不该被状态名单剪。

        名单只针对 `.docmind/` 的**直接子目录**；更深处的同名目录是项目内容
        （例如 `.docmind/web/screenshots/` 是导出物的子目录）。
        """
        _mk(self.root, ".docmind/web/screenshots/deep.png")
        r = self._glob("**/deep.png")
        self.assertEqual(r["files"], [".docmind/web/screenshots/deep.png"],
                         "`.docmind/` 下第二层的 screenshots 被误剪了")
        _mk(self.root, ".docmind/web/index.html")
        _mk(self.root, ".docmind/project_stages/w/workspace/x.py")
        r = self._glob("**/*.html")
        self.assertIn("project_stages", r["pruned_dirs"],
                      "没交代哪里没扫 ->'没有命中'会被读成'这里没有'")

    def test_pruned_list_excludes_generic_skip_dirs(self):
        """`.git`/`.venv` 是"任何项目都不扫"，不是"Agent 工作台 的状态"。

        实测第一版把 `.git` 也列进了 pruned_dirs，读起来像在建议
        "用 scope: 指进 .git" —— 误导。
        """
        _mk(self.root, ".docmind/web/index.html")
        _mk(self.root, ".git/config")
        r = self._glob("**/*.html")
        self.assertNotIn(".git", r["pruned_dirs"])
        for d in r["pruned_dirs"]:
            self.assertNotIn(d, code_intel._SKIP_DIRS,
                             "pruned_dirs 混进了通用跳过项: %s" % d)

    # ---------- 措辞不许是假否定 ----------

    def test_no_hit_message_is_not_a_false_negative(self):
        """"没有命中"必须带范围保留，不能读成"这里没有"。"""
        r = self._glob("**/*.nonexistent_ext")
        text = code_intel.render_glob(r)
        self.assertNotIn("没有命中文件", text,
                         "这句是无保留的假否定措辞")
        self.assertIn("在我扫过的范围里", text)

    def test_no_hit_reports_pruning_when_any(self):
        _mk(self.root, ".docmind/project_stages/x/y.py")
        r = self._glob("**/*.html")
        text = code_intel.render_glob(r)
        self.assertIn("没扫", text, "修剪了目录却不告诉调用方")

    # ---------- 不回归 ----------

    def test_normal_source_files_still_found(self):
        _mk(self.root, "scenes/main.gd")
        _mk(self.root, "ui/hud.gd")
        r = self._glob("**/*.gd")
        self.assertEqual(sorted(r["files"]), ["scenes/main.gd", "ui/hud.gd"])

    def test_scope_still_confines_the_search(self):
        """scope 必须真的把搜索限制住，且路径仍相对代码根（保持既有契约）。"""
        _mk(self.root, ".docmind/web/index.html")
        _mk(self.root, "ui/hud.gd")
        _mk(self.root, "ui/index.html")
        r = self._glob("*.html", scope="ui")
        self.assertEqual(r["files"], ["ui/index.html"],
                         "scope 没限制住")
        self.assertEqual(r["scope"], "ui")
        r2 = self._glob("*.html", scope=".docmind/web")
        self.assertEqual(r2["files"], [".docmind/web/index.html"])
        self.assertEqual(r2["scope"], ".docmind/web",
                         "scope 可以对进 .docmind 子目录 —— 这也是缺陷 A 的补救通路")


if __name__ == "__main__":
    unittest.main(verbosity=2)
