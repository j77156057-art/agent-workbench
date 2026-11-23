"""项目源码遍历的剪枝口径回归测试。

来历（2026-11-23 的后端体检）：`game_workbench` 里有四处就地写的目录排除
字面量，彼此不一致 ——

1. `asset_dependencies` / `impact_analysis` 的清单**漏了 `node_modules`**；
2. 四份清单**都漏了 `.docmind` 状态目录**（`project_stages` / `projects` /
   `project_checkpoints` 等工作流副本与整仓影子副本，本机 8 万+ 文件）；
3. 结果：`/api/asset_dependencies` 要 **196 秒**（实测），
   `/api/validate_data` 要 11.7 秒，而且它把 `.docmind` 影子副本里的
   `regions.json` 报成「项目配置损坏」—— **142 条假阳性**。

现在这四处统一走 `agent_runtime.source_walk.walk_source_files`，口径与
`ingest` 的建库层、`code_intel` 的 grep 层、`tools` 的 glob 层一致。

这里锁住三件事：
1. 依赖/构建产物/点目录一律不扫（`node_modules` 曾经是漏网之鱼）；
2. `.docmind/` 直接下一层的状态目录按名剪掉，但**产物目录照常可见**
   （Godot Web 导出的 `web/` 必须留着 —— 见 `code_intel` 的同名注释）；
3. 真实源码一个都不能少。
"""

import os
import unittest
from pathlib import Path

from agent_runtime.source_walk import walk_source_files
import game_workbench as gw


def _write(root: Path, rel: str, text: str = "x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class SourceWalkPruningTests(unittest.TestCase):
    """walk_source_files 的剪枝口径。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="dm-srcwalk-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rel(self):
        out = set()
        for path in walk_source_files(str(self.root)):
            out.add(os.path.relpath(path, str(self.root)).replace("\\", "/"))
        return out

    def test_skips_dependencies_build_output_and_dot_dirs(self):
        """依赖、构建产物、点目录一律不扫。"""
        _write(self.root, "src/app.py")
        _write(self.root, "node_modules/pkg/index.js")
        _write(self.root, "frontend/node_modules/dep/index.js")
        _write(self.root, "dist/bundle.js")
        _write(self.root, "build/out.js")
        _write(self.root, ".venv/lib/x.py")
        _write(self.root, ".hidden/secret.py")
        _write(self.root, ".tmp/scratch.py")
        _write(self.root, ".pytest_cache/x.py")
        rel = self._rel()
        self.assertIn("src/app.py", rel, "真实源码被剪掉了：%r" % sorted(rel))
        for gone in ("node_modules/pkg/index.js", "frontend/node_modules/dep/index.js",
                     "dist/bundle.js", "build/out.js", ".venv/lib/x.py",
                     ".hidden/secret.py", ".tmp/scratch.py", ".pytest_cache/x.py"):
            self.assertNotIn(gone, rel, "%s 本该被剪掉：%r" % (gone, sorted(rel)))

    def test_prunes_docmind_state_dirs_but_keeps_artifact_dirs(self):
        """.docmind 的状态目录按名剪掉，产物目录（web/）照常可见。"""
        _write(self.root, ".docmind/project_stages/wf-x/workspace/app.py")
        _write(self.root, ".docmind/projects/prj-x/headcheck/app.py")
        _write(self.root, ".docmind/project_checkpoints/cp-x/files/regions.json")
        _write(self.root, ".docmind/web/index.html")
        rel = self._rel()
        self.assertIn(".docmind/web/index.html", rel,
                      "产物目录被误剪了：%r" % sorted(rel))
        for gone in (".docmind/project_stages/wf-x/workspace/app.py",
                     ".docmind/projects/prj-x/headcheck/app.py",
                     ".docmind/project_checkpoints/cp-x/files/regions.json"):
            self.assertNotIn(gone, rel, "%s 本该被剪掉：%r" % (gone, sorted(rel)))

    def test_dynamic_state_dir_prefix_is_pruned(self):
        """动态命名的状态目录（docmind-production-visual-*）也剪。"""
        _write(self.root, ".docmind/docmind-production-visual-abc/shot.png")
        rel = self._rel()
        self.assertNotIn(".docmind/docmind-production-visual-abc/shot.png", rel,
                         "动态状态目录没被剪掉：%r" % sorted(rel))

    def test_empty_and_missing_root_yield_nothing(self):
        """空目录 / 不存在的目录不炸，也不产出东西。"""
        self.assertEqual(list(walk_source_files(str(self.root))), [])
        self.assertEqual(list(walk_source_files(str(self.root / "nope"))), [])
        self.assertEqual(list(walk_source_files("")), [])

    def test_extra_skip_is_additive(self):
        """extra_skip 叠加在默认口径之上（引擎专属排除项）。"""
        _write(self.root, "game/Library/cache.js")
        _write(self.root, "game/src/main.js")
        rel = set()
        for path in walk_source_files(str(self.root), extra_skip=frozenset({"Library"})):
            rel.add(os.path.relpath(path, str(self.root)).replace("\\", "/"))
        self.assertNotIn("game/Library/cache.js", rel, "extra_skip 没生效：%r" % sorted(rel))
        self.assertIn("game/src/main.js", rel, "extra_skip 误伤了正常文件：%r" % sorted(rel))

    def test_oversized_file_is_skipped_by_default(self):
        """超大文件跳过 —— 状态导出（39MB 的 JSON）不该拖垮一次扫描。"""
        from agent_runtime.source_walk import MAX_FILE_BYTES
        _write(self.root, "src/app.py", "x = 1\n")
        big = self.root / "big.json"
        big.write_text(" " * (MAX_FILE_BYTES + 1), encoding="utf-8")
        rel = self._rel()
        self.assertIn("src/app.py", rel, "正常文件被误伤了：%r" % sorted(rel))
        self.assertNotIn("big.json", rel, "超大文件没被跳过：%r" % sorted(rel))

    def test_size_limit_can_be_overridden(self):
        """max_file_bytes=0 表示不限（调用方自己把关时用）。"""
        from agent_runtime.source_walk import MAX_FILE_BYTES
        big = self.root / "big.json"
        big.write_text(" " * (MAX_FILE_BYTES + 1), encoding="utf-8")
        rel = set()
        for path in walk_source_files(str(self.root), max_file_bytes=0):
            rel.add(os.path.relpath(path, str(self.root)).replace("\\", "/"))
        self.assertIn("big.json", rel, "max_file_bytes=0 没生效：%r" % sorted(rel))


class GameWorkbenchUsesSharedPruningTests(unittest.TestCase):
    """`game_workbench` 那四处函数必须走统一口径，不再自带排除字面量。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="dm-gw-prune-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_validate_data_ignores_state_dir_garbage(self):
        """影子副本里的坏 JSON 不许被报成「项目配置损坏」。

        这就是那 142 条假阳性的来源：`.docmind/project_checkpoints/.../regions.json`
        是工作流检查点的工作副本，不是项目配置。
        """
        _write(self.root, ".docmind/project_checkpoints/cp-x/files/regions.json", "{ broken")
        _write(self.root, "config/settings.json", '{"ok": true}')
        out = gw.validate_data(str(self.root))
        self.assertTrue(out["ok"], "影子副本的坏 JSON 被报成了项目错误：%r" % out["errors"])
        self.assertEqual(out["errors"], [], out["errors"])
        self.assertEqual(out["checked"], 1, "真实配置文件没被读到：%r" % out)

    def test_validate_data_still_reports_real_broken_json(self):
        """真实项目里的坏 JSON 仍然要报 —— 别把口径剪过头了。"""
        _write(self.root, "config/broken.json", "{ not json")
        out = gw.validate_data(str(self.root))
        self.assertFalse(out["ok"], "真实坏 JSON 没被报出来：%r" % out)
        self.assertEqual(len(out["errors"]), 1, out["errors"])
        self.assertIn("broken.json", out["errors"][0]["path"], out["errors"])

    def test_asset_dependencies_skips_node_modules_and_state_dirs(self):
        """资产引用只在真实源码里找：依赖包与状态目录里的不算。"""
        _write(self.root, "scripts/hero.py", 'hero = "texture:art/hero.png"\n')
        _write(self.root, "node_modules/pkg/index.js", 'x = "texture:art/vendor.png"\n')
        _write(self.root, ".docmind/projects/prj-x/headcheck/hero.py",
               'hero = "texture:art/shadow.png"\n')
        assets = [r.get("asset") for r in gw.asset_dependencies(str(self.root))]
        self.assertIn("art/hero.png", assets, "真实引用没被识别：%r" % assets)
        self.assertNotIn("art/vendor.png", assets, "依赖包里的引用被算进来了：%r" % assets)
        self.assertNotIn("art/shadow.png", assets, "影子副本里的引用被算进来了：%r" % assets)

    def test_impact_analysis_grep_fallback_skips_state_dirs(self):
        """兜底 grep 也不能命中影子副本里的同名函数。"""
        _write(self.root, "scripts/hero.py", "def spawn_hero():\n    pass\n")
        _write(self.root, ".docmind/project_stages/wf-x/workspace/hero.py",
               "def spawn_hero():\n    pass\n")
        import sys as _sys
        from unittest import mock
        with mock.patch.dict(_sys.modules, {"vectorstore": None}):
            hits = gw.impact_analysis(str(self.root), "spawn_hero")
        normalized = [h.replace("\\", "/") for h in hits]
        self.assertIn("scripts/hero.py", normalized, "真实命中没找到：%r" % hits)
        self.assertFalse(any(".docmind" in h for h in normalized),
                         "影子副本里的命中被算进来了：%r" % hits)


class EngineScanPruningTests(unittest.TestCase):
    """`engine_scan` / `engine_inspect` 的剪枝口径。

    此前这两处是「按路径片段 continue」—— 既不剪枝、也漏掉 `.docmind` 状态
    目录，于是工作流检查点里**同一份项目的几十份副本**全被当成独立项目报出
    （实测 152 个），扫描还要 4~8 秒。对 AI 来说是纯噪声。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="dm-engine-prune-")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_real_project_but_not_checkpoint_copies(self):
        """真实项目照常识别；检查点副本不再被当成独立项目。"""
        _write(self.root, "game/project.godot", "[application]\n")
        _write(self.root, ".docmind/project_checkpoints/cp-1/files/project.godot",
               "[application]\n")
        _write(self.root, ".docmind/project_stages/wf-x/workspace/project.godot",
               "[application]\n")
        out = gw.engine_scan(str(self.root))
        paths = [p["path"] for p in out["projects"]]
        self.assertIn("game/project.godot", paths, "真实项目没被识别：%r" % paths)
        self.assertEqual([p for p in paths if ".docmind" in p], [],
                         "检查点副本被当成独立项目了：%r" % paths)

    def test_engine_inspect_still_reads_unity_assets(self):
        """Unity 资产索引照常产出（别把口径剪过头）。"""
        _write(self.root, "ProjectSettings/ProjectVersion.txt",
               "m_EditorVersion: 2022.3.1f1\n")
        _write(self.root, "Assets/Scenes/Main.unity", "unity scene\n")
        _write(self.root, "Assets/Props/Box.prefab", "prefab\n")
        _write(self.root, "Assets/Props/Box.prefab.meta", "guid: abc123\n")
        result = gw.engine_inspect(str(self.root), engine="unity")
        scenes = [s["path"] for s in result["scenes"]]
        prefabs = [s["path"] for s in result["prefabs"]]
        self.assertIn("Assets/Scenes/Main.unity", scenes, scenes)
        self.assertIn("Assets/Props/Box.prefab", prefabs, prefabs)
        self.assertTrue(result["manifests"], "ProjectVersion 没被读出来：%r" % result["manifests"])


if __name__ == "__main__":
    unittest.main()
