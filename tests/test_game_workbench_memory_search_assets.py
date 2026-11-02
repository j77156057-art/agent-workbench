"""覆盖 `game_workbench` 里五个没有用例走过的普通函数。

来历：把 `open()` 改成 `with` 之后，CI 与本地复算的改动行覆盖率门都点名了
`game_workbench.py` 的 13 行 —— 全部落在这五个函数里：
`project_memory` / `asset_dependencies` / `create_placeholder` / `impact_analysis` /
`_extract_symbols`。它们是产品功能（记忆文件、资产依赖、占位资源、影响面、符号提取），
不是手工脚本，所以补用例而不是加豁免。

夹具仍然放在仓库内的 .tmp：`impact_analysis` / `asset_dependencies` 会跳过
`.git/.venv/build/dist/.chroma/__pycache__`，而 `engine_inspect` 那一族还会跳过路径里带
`Temp` 的目录（本机默认临时目录就是 D:\\Temp）。统一放 .tmp 最省事。
"""

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import game_workbench as gw

_FIXTURE_BASE = Path(__file__).resolve().parent.parent / ".tmp"


@contextlib.contextmanager
def _fixture_root():
    _FIXTURE_BASE.mkdir(exist_ok=True)
    root = tempfile.mkdtemp(prefix="dm-gw-funcs-", dir=str(_FIXTURE_BASE))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class ProjectMemoryTests(unittest.TestCase):
    def test_writes_then_reads_and_missing_is_empty(self):
        with _fixture_root() as root:
            self.assertEqual(gw.project_memory(root), "", "没有记忆文件时应返回空串")
            gw.project_memory(root, "# 记忆\n第二行\n")
            self.assertEqual(gw.project_memory(root), "# 记忆\n第二行\n",
                             "写进去的内容读不回来")


class AssetDependenciesTests(unittest.TestCase):
    def test_finds_texture_references_in_source_files(self):
        with _fixture_root() as root:
            _write(root, "scripts/hero.py", 'hero = "texture:art/hero.png"\n')
            _write(root, "notes.txt", "这里不扫描：扩展名不在清单里\n")
            refs = gw.asset_dependencies(root)
            assets = [r.get("asset") for r in refs]
            self.assertIn("art/hero.png", assets, "资产引用没被识别：%r" % refs)
            self.assertTrue(all(r.get("source") for r in refs), "缺少 source 字段：%r" % refs)


class CreatePlaceholderTests(unittest.TestCase):
    def test_creates_the_file_and_refuses_to_overwrite(self):
        with _fixture_root() as root:
            info = gw.create_placeholder(root, "art/new.json", kind="json")
            self.assertTrue(info, "没有返回预览信息")
            path = os.path.join(root, "art", "new.json")
            self.assertTrue(os.path.isfile(path), "占位文件没落盘")
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "{}\n")
            with self.assertRaises(ValueError):
                gw.create_placeholder(root, "art/new.json", kind="json")


class ImpactAnalysisTests(unittest.TestCase):
    def test_falls_back_to_grep_when_the_index_has_nothing(self):
        # 本机可能装着真的 .chroma 代码库：向量分支会先返回别的文件。把 vectorstore
        # 置空让那句 import 直接失败，测到的才是**回退 grep** 这条路径
        # （CI 上没有 .chroma，同样走这里）。
        with _fixture_root() as root:
            _write(root, "scripts/hero.py", "def spawn_hero():\n    pass\n")
            with mock.patch.dict(sys.modules, {"vectorstore": None}):
                hits = gw.impact_analysis(root, "spawn_hero")
            # 这个函数返回的是 `os.path.relpath` 的原样结果：Windows 上是反斜杠，
            # Linux 上是正斜杠。断言按路径分隔符归一之后再比（不在测试里强推产品行为）。
            normalized = [h.replace("\\", "/") for h in hits]
            self.assertIn("scripts/hero.py", normalized, "回退 grep 没找到：%r" % hits)


class ExtractSymbolsTests(unittest.TestCase):
    def test_python_uses_ast_and_other_languages_use_patterns(self):
        with _fixture_root() as root:
            py = os.path.join(root, "mod.py")
            _write(root, "mod.py", "class Hero:\n    pass\n\ndef spawn():\n    pass\n")
            symbols = gw._extract_symbols(py)
            self.assertIn(("class", "Hero"), symbols, symbols)
            self.assertIn(("function", "spawn"), symbols, symbols)

            # 非 Python 走正则：`class Hero` 这种形状能把类名抓出来
            # （`public class Hero` 只会抓到 `class`，那是这条正则的实际形状，不在这条里断言）。
            cs = os.path.join(root, "Hero.cs")
            _write(root, "Hero.cs", "class Hero { }\n")
            self.assertIn(("symbol", "Hero"), gw._extract_symbols(cs))

    def test_a_broken_python_file_yields_nothing_instead_of_raising(self):
        with _fixture_root() as root:
            broken = os.path.join(root, "broken.py")
            _write(root, "broken.py", "def (:\n")
            self.assertEqual(gw._extract_symbols(broken), [])


if __name__ == "__main__":
    unittest.main()
