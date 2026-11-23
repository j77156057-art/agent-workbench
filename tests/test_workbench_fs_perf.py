"""工作台文件树的性能回归测试。

来历（2026-11-23 的端点普查）：`/api/fs/tree` 要 2.7 秒、
`/api/fs/relation-graph` 要 3.3 秒，但它们的产出都很小（183 个树节点 /
1759 个关系节点）。profile 定位到两处**重复劳动**，不是算法本身慢：

1. `_git_fields` 对**每个节点**调一次 `os.path.realpath`（实测 7736 次 =
   1.9 秒，占 build_tree 六成）。Windows 上 realpath 要走
   `nt._getfinalpathname`，几百微秒一次；而节点里绝大多数是文件，它们共享
   同一个父目录 —— 父目录的 realpath 结果可以直接复用。
2. 同一个 .py 文件被 `ast.parse` **两遍**：`symbols._extract_python` 一遍
   （出符号）、`build_relation_graph` 再一遍（找继承边与调用边）。
   全仓 799 个 .py，重复解析要 3.2 秒。

这里锁住两件事：
1. **口径不变** —— 缓存只影响速度，tracked/dirty 判定与 AST 内容必须一致；
2. **缓存会失效** —— 文件改了要重新解析，不能拿旧结果糊弄。
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import symbols as symlib  # noqa: E402
import workbench_fs as wb  # noqa: E402


class RealpathCacheTests(unittest.TestCase):
    """目录级 realpath 缓存。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wbfs_rp_")
        wb.invalidate_status()

    def tearDown(self):
        wb.invalidate_status()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cached_realpath_matches_os_realpath(self):
        """缓存结果必须与 os.path.realpath 逐字一致。"""
        os.makedirs(os.path.join(self.root, "a", "b"), exist_ok=True)
        target = os.path.join(self.root, "a", "b")
        expected = os.path.realpath(target)
        self.assertEqual(wb._realpath(target), expected)
        # 第二次走缓存，结果仍要一致
        self.assertEqual(wb._realpath(target), expected)

    def test_cache_is_cleared_by_invalidate_status(self):
        """写操作后缓存要清掉（目录可能被改名/删除）。"""
        target = os.path.join(self.root, "x")
        os.makedirs(target, exist_ok=True)
        first = wb._realpath(target)
        self.assertIn(target, wb._realpath_cache)
        wb.invalidate_status()
        # 清缓存后条目应当消失（重新解析的结果仍要一致）
        self.assertNotIn(target, wb._realpath_cache)
        self.assertEqual(wb._realpath(target), first)

    def test_git_fields_unaffected_by_cache(self):
        """不在任何仓库里的节点：tracked/dirty 仍是 None。"""
        p = os.path.join(self.root, "f.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        snap = wb._git_snapshot(self.root)
        tracked, dirty = wb._git_fields(p, "f.py", snap)
        self.assertIsNone(tracked)
        self.assertIsNone(dirty)


class PythonAstCacheTests(unittest.TestCase):
    """共享 Python AST 缓存。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wbfs_ast_")
        symlib.clear_caches()

    def tearDown(self):
        symlib.clear_caches()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_returns_same_tree_as_direct_parse(self):
        """缓存里的 AST 与直接 ast.parse 等价。"""
        import ast
        p = os.path.join(self.root, "m.py")
        src = "class A:\n    def go(self):\n        return helper()\n\ndef helper():\n    return 1\n"
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(src)
        tree = symlib.python_ast(p)
        self.assertIsNotNone(tree)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        self.assertIn("helper", names, names)

    def test_syntax_error_returns_none(self):
        """语法错误返回 None（不抛），调用方据此跳过。"""
        p = os.path.join(self.root, "bad.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("def broken(:\n")
        self.assertIsNone(symlib.python_ast(p))

    def test_cache_invalidates_when_file_changes(self):
        """文件改了必须重新解析 —— 不能拿旧 AST 糊弄。"""
        p = os.path.join(self.root, "c.py")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("def first():\n    return 1\n")
        tree1 = symlib.python_ast(p)
        self.assertIsNotNone(tree1)
        import ast
        names1 = [n.name for n in ast.walk(tree1) if isinstance(n, ast.FunctionDef)]
        self.assertEqual(names1, ["first"])
        # 改文件（mtime 变了）
        time.sleep(0.02)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("def second():\n    return 2\n")
        tree2 = symlib.python_ast(p)
        names2 = [n.name for n in ast.walk(tree2) if isinstance(n, ast.FunctionDef)]
        self.assertEqual(names2, ["second"], "文件改了却拿到旧 AST：%r" % names2)

    def test_missing_file_returns_none(self):
        """文件不存在返回 None，不抛。"""
        self.assertIsNone(symlib.python_ast(os.path.join(self.root, "nope.py")))


if __name__ == "__main__":
    unittest.main()
