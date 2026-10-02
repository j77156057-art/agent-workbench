"""dev_deps 回归：把「本地能跑、干净装不上」那一类问题变成一步，并盯住它的假干净。

要防的假干净有五种，每一种都在下面有对应用例：

1. **正则扫 import** → 注释里/字符串里的 `import re` 被当成依赖，于是往清单里塞
   标准库。守它的用例：`a_comment_is_not_an_import`。
2. **只看仓库根** → 嵌套项目的本地包（`game_demo/starvoyager`）被当成第三方发行包。
   守它的用例：`a_nested_local_package_is_not_a_dependency`。
3. **映射靠猜** → 猜错的名字被写进 requirements.txt，装了个不相干的包，真缺的那个
   还缺着。守它的用例：`an_unmappable_import_is_never_written`。
4. **解析失败当没有** → 语法坏掉的 .py 扫不出 import，报告说「没发现缺什么」。
   守它的用例：`a_broken_file_is_not_a_clean_bill`。
5. **追加时动了别人的行** → 改版本、删行、重排。守它的用例：`apply_only_appends`。
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime import deps_sync as ds  # noqa: E402


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


class Case(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="depssync-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def path(self, rel):
        return os.path.join(self.dir, rel.replace("/", os.sep))


class ReconcileTests(Case):

    def test_a_declared_import_is_not_missing(self):
        """清单里写了的就不报；没写的要报——即使它**恰好**是另一个包的传递依赖
        （`starlette` 由 `fastapi` 带进来，正因如此本仓才一直没人发现 api.py 直接
        import 它却没声明）。"""
        write(self.path("requirements.txt"), "fastapi>=0.110\n")
        write(self.path("a.py"), "import fastapi\nfrom starlette.applications import Starlette\n")
        result = ds.reconcile(self.dir)
        self.assertEqual([r["module"] for r in result["missing"]], ["starlette"])
        self.assertFalse(result["ok"])
        write(self.path("requirements.txt"), "fastapi>=0.110\nstarlette\n")
        self.assertTrue(ds.reconcile(self.dir)["ok"])

    def test_a_comment_is_not_an_import(self):
        """注释与字符串里的 import 语句不是依赖。正则扫全文会把它们算进去。"""
        write(self.path("requirements.txt"), "requests\n")
        write(self.path("a.py"),
              "# import numpy\n"
              "TEXT = 'import pandas'\n"
              'DOC = """import scipy"""\n'
              "import requests\n")
        result = ds.reconcile(self.dir)
        self.assertTrue(result["ok"], result["missing"])
        self.assertNotIn("numpy", [r["module"] for r in result["missing"]])
        self.assertNotIn("pandas", [r["module"] for r in result["missing"]])
        self.assertNotIn("scipy", [r["module"] for r in result["missing"]])

    def test_stdlib_is_never_a_dependency(self):
        write(self.path("requirements.txt"), "")
        write(self.path("a.py"), "import os, sys, json, re, asyncio\nimport unittest\n"
                                 "from collections import OrderedDict\n")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["missing"], [], result["missing"])

    def test_a_relative_import_is_never_a_dependency(self):
        write(self.path("requirements.txt"), "")
        write(self.path("pkg/__init__.py"), "")
        write(self.path("pkg/a.py"), "from . import b\nfrom .helper import thing\n")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["missing"], [], result["missing"])

    def test_a_top_level_local_module_is_not_a_dependency(self):
        """`import tools` 指向本仓的 tools.py。它在 PyPI 上真实存在，
        建议装它比漏报更糟。"""
        write(self.path("requirements.txt"), "")
        write(self.path("tools.py"), "X = 1\n")
        write(self.path("a.py"), "import tools\nfrom agent_runtime import thing\n")
        write(self.path("agent_runtime/__init__.py"), "")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["missing"], [], result["missing"])

    def test_a_nested_local_package_is_not_a_dependency(self):
        """实测过的那条误报：`game_demo/main.py` 里 `from starvoyager.config import …`
        解析到 `game_demo/starvoyager/`，是项目内代码。"""
        write(self.path("requirements.txt"), "")
        write(self.path("game_demo/starvoyager/__init__.py"), "")
        write(self.path("game_demo/starvoyager/config.py"), "A = 1\n")
        write(self.path("game_demo/main.py"), "from starvoyager.config import A\n")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["missing"], [], result["missing"])

    def test_the_same_module_can_be_local_and_external(self):
        """同名模块对 A 文件是本地的、对 B 文件是真第三方——按导入方逐个判。"""
        write(self.path("requirements.txt"), "")
        write(self.path("game_demo/starvoyager/__init__.py"), "")
        write(self.path("game_demo/main.py"), "from starvoyager.config import A\n")
        write(self.path("other.py"), "import starvoyager\n")
        result = ds.reconcile(self.dir)
        self.assertEqual([r["module"] for r in result["missing"]], ["starvoyager"])
        self.assertEqual(result["missing"][0]["importers"], ["other.py"])

    def test_a_broken_file_is_not_a_clean_bill(self):
        """语法坏掉的 .py 扫不出 import。那不是「没有依赖」，是「没查」。"""
        write(self.path("requirements.txt"), "")
        write(self.path("good.py"), "print(1)\n")
        write(self.path("bad.py"), "def broken(:\n    return 1\n")
        result = ds.reconcile(self.dir)
        self.assertFalse(result["complete"], "有解析失败的文件时不能报完整")
        self.assertEqual([row["path"] for row in result["unparsed"]], ["bad.py"])
        self.assertTrue(any("解析失败" in note for note in result["notes"]), result["notes"])

    def test_no_requirement_file_says_so_instead_of_looking_clean(self):
        write(self.path("a.py"), "print(1)\n")
        result = ds.reconcile(self.dir)
        self.assertFalse(result["complete"])
        self.assertTrue(any("没有依赖清单" in note for note in result["notes"]), result["notes"])

    def test_the_scan_cap_is_reported_not_hidden(self):
        """被上限截断的报告不能被读成「全查过了」。"""
        original = ds.MAX_FILES
        ds.MAX_FILES = 2
        try:
            for index in range(6):
                write(self.path("mod%d.py" % index), "import json\n")
            result = ds.reconcile(self.dir)
        finally:
            ds.MAX_FILES = original
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertTrue(any("上限" in note for note in result["notes"]), result["notes"])


class MappingTests(Case):

    def test_installed_metadata_beats_the_alias_table(self):
        """实测：`websocket` 的发行名是 `websocket-client`、`webview` 是 `pywebview`。
        本机装着的元数据比任何手写表都硬。"""
        table = {"websocket": ["websocket-client"], "yaml": ["PyYAML"]}
        found = ds.distribution_for("websocket", installed=table)
        self.assertEqual(found["distribution"], "websocket-client")
        self.assertEqual(found["confidence"], "installed")
        self.assertTrue(found["installed"])

    def test_a_declared_extra_counts_as_declared(self):
        """`psycopg[binary,pool]` 的 pool extra 确实带出 psycopg-pool。"""
        found = ds.distribution_for("psycopg_pool", declared={"psycopg": "[binary,pool]"},
                                    installed={})
        self.assertIn(found["confidence"], ("extra", "installed"))

    def test_a_same_name_match_is_flagged_as_weak_evidence(self):
        """本机没装时只能同名匹配——必须标成弱证据，不能装作有依据。"""
        found = ds.distribution_for("sentence_transformers", declared={}, installed={})
        self.assertEqual(found["distribution"], "sentence-transformers")
        self.assertEqual(found["confidence"], "exact")
        self.assertFalse(found["installed"])

    def test_an_unmappable_import_is_never_written(self):
        """映射不到发行名时归 unknown，`distribution` 必须是空的——否则 apply 会
        把它写进清单，装个不相干的包。"""
        for name in ("__future__", "_socket", "2fa"):
            found = ds.distribution_for(name, declared={}, installed={})
            self.assertEqual(found["distribution"], "", name)
            self.assertEqual(found["confidence"], "unknown", name)

    def test_an_unmappable_import_never_reaches_the_written_lines(self):
        write(self.path("requirements.txt"), "")
        result = {"missing": [{"module": "__weird__", "distribution": "",
                                "importers": ["a.py"], "file_count": 1}],
                  "unknown": [{"module": "__weird__", "importers": ["a.py"], "file_count": 1}]}
        self.assertEqual(ds.missing_lines(result), [])
        applied = ds.apply(self.dir, result)
        self.assertFalse(applied["ok"])
        self.assertIn("没有可追加", applied["error"])

    def test_an_alias_hit_is_recorded_as_such(self):
        found = ds.distribution_for("win32con", declared={}, installed={})
        self.assertEqual(found["distribution"], "pywin32")
        self.assertEqual(found["confidence"], "alias")


class RequirementsParsingTests(Case):

    def test_specifiers_extras_markers_and_comments(self):
        parsed = ds.parse_requirements(
            "# a comment\n"
            "fastapi>=0.110,<1\n"
            "psycopg[binary,pool]>=3.2 ; python_version >= '3.10'\n"
            "pillow  # inline\n"
            "-r other.txt\n"
            "--index-url https://example.invalid\n"
            "-e .\n"
            "weird line without name\n")
        names = parsed["names"]
        self.assertIn("fastapi", names)
        self.assertIn("psycopg", names)
        self.assertIn("pillow", names)
        self.assertEqual(names["fastapi"], ">=0.110,<1")
        self.assertIn("-r", parsed["skipped"])
        self.assertIn("--index-url", parsed["skipped"])
        self.assertTrue(any("weird" in item for item in parsed["skipped"]))

    def test_names_are_compared_pep503_insensitively(self):
        """清单里写 `PyYAML`、代码里 `import yaml` ——归一后必须算同一个。"""
        parsed = ds.parse_requirements("PyYAML\nFoo.Bar_Baz>=1\n")
        self.assertIn("pyyaml", parsed["names"])
        self.assertIn("foo-bar-baz", parsed["names"])

    def test_pyproject_is_read_when_there_is_no_requirements(self):
        write(self.path("pyproject.toml"),
              "[project]\nname = \"x\"\ndependencies = [\"httpx>=0.27\", \"rich\"]\n")
        write(self.path("a.py"), "import rich\n")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["declared_source"], "pyproject.toml")
        self.assertEqual(result["missing"], [], result["missing"])

    def test_a_broken_pyproject_is_not_read_as_no_dependencies(self):
        write(self.path("pyproject.toml"), "[project\nname = broken\n")
        write(self.path("a.py"), "import json\n")
        result = ds.reconcile(self.dir)
        self.assertFalse(result["complete"])
        self.assertTrue(any("解析失败" in note for note in result["notes"]), result["notes"])

    def test_an_optional_dependency_group_counts_as_declared(self):
        write(self.path("pyproject.toml"),
              "[project]\nname = \"x\"\ndependencies = []\n\n"
              "[project.optional-dependencies]\ndev = [\"pytest>=8\"]\n")
        write(self.path("a.py"), "import pytest\n")
        result = ds.reconcile(self.dir)
        self.assertEqual(result["missing"], [], result["missing"])


class ApplyTests(Case):

    def _fixture(self):
        write(self.path("requirements.txt"), "fastapi\n")
        write(self.path("a.py"), "import starlette\nimport rich\n")

    def test_apply_only_appends(self):
        """只追加：不改版本、不删行、不重排。"""
        self._fixture()
        before = Path(self.path("requirements.txt")).read_text(encoding="utf-8")
        result = ds.reconcile(self.dir)
        applied = ds.apply(self.dir, result)
        self.assertTrue(applied["ok"], applied)
        after = Path(self.path("requirements.txt")).read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before), "原有内容必须原样保留在开头")
        self.assertEqual(before.strip(), "fastapi")
        self.assertIn("starlette", after)
        self.assertIn("rich", after)

    def test_apply_refuses_when_there_is_nothing_missing(self):
        write(self.path("requirements.txt"), "fastapi\nstarlette\nrich\n")
        write(self.path("a.py"), "import starlette\nimport rich\n")
        result = ds.reconcile(self.dir)
        self.assertTrue(result["ok"], result["missing"])
        applied = ds.apply(self.dir, result)
        self.assertFalse(applied["ok"])
        self.assertEqual(Path(self.path("requirements.txt")).read_text(encoding="utf-8"),
                         "fastapi\nstarlette\nrich\n")

    def test_apply_refuses_to_create_a_manifest(self):
        """没有依赖清单时不替人新建——那是「这个项目怎么管依赖」的决策。"""
        write(self.path("a.py"), "import starlette\n")
        result = ds.reconcile(self.dir)
        applied = ds.apply(self.dir, result)
        self.assertFalse(applied["ok"])
        self.assertIn("不会替你新建", applied["error"])
        self.assertFalse(os.path.exists(self.path("requirements.txt")))

    def test_apply_echoes_exactly_what_it_wrote(self):
        self._fixture()
        result = ds.reconcile(self.dir)
        applied = ds.apply(self.dir, result)
        body = Path(self.path("requirements.txt")).read_text(encoding="utf-8")
        for name in applied["applied"]:
            self.assertIn(name, body)
        self.assertEqual(sorted(applied["applied"]), ["rich", "starlette"])


class RenderTests(Case):

    def test_missing_lines_carry_the_importer_as_provenance(self):
        """写进清单的每一行都要能回答「凭什么说它缺」。"""
        write(self.path("requirements.txt"), "")
        write(self.path("api/handler.py"), "import starlette\n")
        result = ds.reconcile(self.dir)
        text = "\n".join(ds.missing_lines(result))
        self.assertIn("api/handler.py", text)
        self.assertIn("starlette", text)

    def test_render_says_the_weak_evidence_out_loud(self):
        write(self.path("requirements.txt"), "")
        write(self.path("a.py"), "import totally_absent_pkg\n")
        result = ds.reconcile(self.dir)
        self.assertIn("弱证据", "\n".join(result["notes"]))

    def test_render_does_not_claim_clean_when_incomplete(self):
        write(self.path("requirements.txt"), "")
        write(self.path("bad.py"), "def f(:\n")
        text = ds.render(ds.reconcile(self.dir))
        self.assertIn("不完整", text)

    def test_unused_is_reported_as_a_lead_not_a_finding(self):
        """pytest/ruff 是命令行用的，「没扫到 import」不等于「多余依赖」。"""
        write(self.path("requirements.txt"), "pytest\nrich\n")
        write(self.path("a.py"), "import rich\n")
        text = ds.render(ds.reconcile(self.dir))
        self.assertIn("只是线索", text)
        result = ds.reconcile(self.dir)
        self.assertTrue(result["ok"], "unused 不得让 ok 变假")


class RealRepoTests(unittest.TestCase):
    """对本仓自身跑一遍：确认工具在真代码上不会崩、也不会满屏假阳性。"""

    def setUp(self):
        if not os.path.isfile(os.path.join(ROOT, "requirements.txt")):
            self.skipTest("不是仓库根目录")

    def test_it_runs_on_this_repo(self):
        result = ds.reconcile(ROOT, paths=["api.py", "tools.py", "agent_runtime/deps_sync.py"])
        self.assertTrue(result["complete"], result["notes"])
        for row in result["missing"]:
            self.assertTrue(row["distribution"], row)
            self.assertNotEqual(row["confidence"], "unknown", row)

    def test_it_does_not_claim_this_repo_has_no_dependencies(self):
        """本仓确实有一批「import 了但没声明」的（starlette/pydantic 只靠 fastapi
        传递带进来）。如果哪天这个用例开始失败，先确认是真的修好了，而不是扫描
        悄悄变瞎了。"""
        result = ds.reconcile(ROOT, paths=["api.py"])
        self.assertTrue(result["missing_total"] >= 1,
                        "api.py 直接 import starlette 但清单里只声明了 fastapi")


if __name__ == "__main__":
    unittest.main()
