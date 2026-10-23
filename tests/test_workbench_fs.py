"""workbench_fs 沙箱与护栏单元测试。

全程在 tempfile 临时代码库中进行，reindex=False 且删除用 .gd/.txt 等非 _CODE_EXT 文件，
保证不触碰 chroma/embedding（无需 Ollama）。git 用例在无 git 环境自动跳过。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import workbench_fs as wb  # noqa: E402


def have_git():
    return shutil.which("git") is not None


def git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, capture_output=True, timeout=30, check=False)


def git_out(args, cwd):
    """跑 git 并取回 stdout（`git()` 丢弃输出，某些断言需要比对文本）。"""
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30,
                          check=False).stdout


def find(nodes, path):
    """按相对路径在嵌套树中查找节点。"""
    target = path.replace("\\", "/")
    stack = list(nodes)
    while stack:
        n = stack.pop()
        if n["path"] == target:
            return n
        stack.extend(n.get("children") or [])
    return None


class WbTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wbfs_test_")
        wb.invalidate_status()

    def tearDown(self):
        wb.invalidate_status()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, content="x = 1\n"):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return p

    def enable_regions(self):
        with open(os.path.join(self.root, "regions.json"), "w", encoding="utf-8") as fh:
            json.dump({"regions": [
                {"key": "values", "dir": "values", "name": "数值区", "desc": "",
                 "access": "", "depends_on": [], "exports": [], "verify": ""},
                {"key": "behaviors", "dir": "behaviors", "name": "角色行为区", "desc": "",
                 "access": "", "depends_on": ["values"], "exports": [], "verify": ""},
            ]}, fh, ensure_ascii=False)

    def init_repo(self, rel="."):
        d = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(d, exist_ok=True)
        git(["init", "-q"], cwd=d)
        git(["config", "user.email", "test@local"], cwd=d)
        git(["config", "user.name", "Test"], cwd=d)
        return d


class TestSandbox(WbTestBase):
    def test_no_root(self):
        with self.assertRaises(wb.FsError) as cm:
            wb.build_tree("")
        self.assertEqual(cm.exception.status, 400)

    def test_traversal_rejected(self):
        for bad in ("../x.py", "values/../../x.py", "/etc/passwd", "C:/Windows/x.py", ".."):
            with self.assertRaises(wb.FsError) as cm:
                wb.read_full(self.root, bad)
            self.assertEqual(cm.exception.status, 403, bad)

    def test_backslash_traversal(self):
        with self.assertRaises(wb.FsError) as cm:
            wb.read_full(self.root, r"values\..\..\secret.py")
        self.assertEqual(cm.exception.status, 403)

    def test_empty_path(self):
        with self.assertRaises(wb.FsError):
            wb.read_full(self.root, "  ")


class TestTree(WbTestBase):
    def test_structure_and_filters(self):
        self.write("values/a.py")
        self.write("values/sub/b.gd")
        self.write("note.txt", "hello")
        self.write("values/__pycache__/junk.pyc", "junk")
        self.write("values/node_modules/lib/index.js", "junk")
        self.write("数值/角色.gd", "pass")
        res = wb.build_tree(self.root)
        self.assertTrue(res["ok"])
        self.assertIsNotNone(find(res["nodes"], "values"))
        self.assertIsNotNone(find(res["nodes"], "values/a.py"))
        self.assertIsNotNone(find(res["nodes"], "values/sub/b.gd"))
        self.assertIsNotNone(find(res["nodes"], "note.txt"))
        self.assertIsNotNone(find(res["nodes"], "数值/角色.gd"))
        self.assertIsNone(find(res["nodes"], "values/__pycache__/junk.pyc"))
        self.assertIsNone(find(res["nodes"], "values/node_modules/lib/index.js"))
        a = find(res["nodes"], "values/a.py")
        self.assertEqual(a["lang"], "python")
        self.assertTrue(a["writable"])

    def test_region_metadata_when_enabled(self):
        self.enable_regions()
        self.write("values/a.py")
        self.write("loose.py")
        res = wb.build_tree(self.root)
        self.assertTrue(res["regions_enabled"])
        self.assertEqual(find(res["nodes"], "values/a.py")["region"], "values")
        self.assertEqual(find(res["nodes"], "values/a.py")["region_name"], "数值区")
        self.assertIsNone(find(res["nodes"], "loose.py")["region"])

    def test_depth_limit(self):
        self.write("a/b/c/d/e.py")
        res = wb.build_tree(self.root, depth=2)
        self.assertIsNone(find(res["nodes"], "a/b/c/d/e.py"))
        self.assertIsNotNone(find(res["nodes"], "a/b"))

    def test_tree_resilient_to_vanishing_file(self):
        # 回归：遍历过程中文件被并发写入者删除（本工程常态：两并发写入者）时，
        # 不应抛出未捕获异常 -> 上层 HTTP 500；应把该节点记为无大小并继续完成遍历。
        self.write("a.py")
        self.write("b.txt", "hello")
        orig = os.path.getsize

        def fake(p):
            if os.path.basename(p).lower() == "a.py":
                raise FileNotFoundError(p)
            return orig(p)

        os.path.getsize = fake
        try:
            res = wb.build_tree(self.root)
        finally:
            os.path.getsize = orig
        self.assertTrue(res["ok"])
        node = find(res["nodes"], "a.py")
        self.assertIsNotNone(node)
        self.assertIsNone(node["size"])
        self.assertIsNotNone(find(res["nodes"], "b.txt"))

    @unittest.skipUnless(have_git(), "无 git")
    def test_git_status_fields(self):
        self.enable_regions()
        self.init_repo("values")
        tracked = self.write("values/tracked.py", "v = 1\n")
        git(["add", "-A"], cwd=os.path.dirname(tracked))
        git(["commit", "-q", "-m", "init"], cwd=os.path.dirname(tracked))
        self.write("values/untracked.txt", "new")  # 提交后新建 → 未跟踪
        self.write("values/tracked.py", "v = 2\n")  # 已跟踪但有改动
        res = wb.build_tree(self.root)
        t = find(res["nodes"], "values/tracked.py")
        u = find(res["nodes"], "values/untracked.txt")
        self.assertTrue(t["tracked"])
        self.assertTrue(t["dirty"])
        self.assertFalse(u["tracked"])


class TestReadSave(WbTestBase):
    def test_read_404_and_dir(self):
        with self.assertRaises(wb.FsError) as cm:
            wb.read_full(self.root, "nope.py")
        self.assertEqual(cm.exception.status, 404)
        os.makedirs(os.path.join(self.root, "d"))
        with self.assertRaises(wb.FsError) as cm:
            wb.read_full(self.root, "d")
        self.assertEqual(cm.exception.status, 400)

    def test_read_binary_and_unsupported(self):
        self.write("pic.png", "\x00\x01PNG")
        image = wb.read_full(self.root, "pic.png")
        self.assertEqual(image["preview_kind"], "image")
        self.assertFalse(image["writable"])
        self.assertEqual(image["content"], "")
        self.assertTrue(image.get("preview_url", "").startswith("/api/fs/raw?path="))
        binary_path = os.path.join(self.root, "blob.bin")
        with open(binary_path, "wb") as fh:
            fh.write(b"\x00\x01\xffBINARY")
        binary = wb.read_full(self.root, "blob.bin")
        self.assertEqual(binary["preview_kind"], "binary")
        self.assertIn("SHA-256", binary["content"])
        self.assertFalse(binary["writable"])
        with self.assertRaises(wb.FsError):
            wb.save_file(self.root, "blob.bin", "corrupt", reindex=False)

    def test_gitignore_is_editable_and_binary_scene_is_read_only(self):
        self.write(".gitignore", ".godot/\n*.bin\n")
        text = wb.read_full(self.root, ".gitignore")
        self.assertTrue(text["writable"])
        self.assertIn("*.bin", text["content"])
        saved = wb.save_file(self.root, ".gitignore", ".godot/\n", reindex=False)
        self.assertTrue(saved["changed"])
        scene = os.path.join(self.root, "cached.scn")
        with open(scene, "wb") as fh:
            fh.write(b"RSRC\x00\x01\x02\x03scene-name")
        preview = wb.read_full(self.root, "cached.scn")
        self.assertEqual(preview["preview_kind"], "binary")
        self.assertFalse(preview["writable"])
        self.assertIn("Godot", preview["content"])

    def test_read_utf8_bom_tolerated(self):
        p = os.path.join(self.root, "bom.gd")
        with open(p, "wb") as fh:
            fh.write("\ufeffextends Node\n".encode("utf-8"))
        r = wb.read_full(self.root, "bom.gd")
        self.assertTrue(r["content"].startswith("extends Node"))

    def test_save_new_and_unchanged(self):
        r1 = wb.save_file(self.root, "values/a.py", "x = 1\n", reindex=False)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "values", "a.py")))
        self.assertTrue(r1["changed"])
        r2 = wb.save_file(self.root, "values/a.py", "x = 1\n", if_mtime=r1["mtime"], reindex=False)
        self.assertFalse(r2["changed"])

    def test_save_syntax_error_blocks_write(self):
        p = self.write("a.py", "x = 1\n")
        with self.assertRaises(wb.FsError) as cm:
            wb.save_file(self.root, "a.py", "def bad(:\n", reindex=False)
        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(cm.exception.extra.get("lineno"), 1)
        with open(p, encoding="utf-8") as fh:  # 原文件未动
            self.assertEqual(fh.read(), "x = 1\n")

    def test_save_mtime_conflict(self):
        r1 = wb.save_file(self.root, "a.py", "x = 1\n", reindex=False)
        with open(os.path.join(self.root, "a.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 99\n")  # 编辑器外被改
        # 用显式过期的客户端 mtime 模拟冲突（同秒写入落在 1.5s 容差内属允许场景）
        with self.assertRaises(wb.FsError) as cm:
            wb.save_file(self.root, "a.py", "x = 2\n", if_mtime=r1["mtime"] - 100, reindex=False)
        self.assertEqual(cm.exception.status, 409)
        self.assertIn("server_mtime", cm.exception.extra)

    def test_save_protected_and_ext_whitelist(self):
        self.enable_regions()
        for bad in ("regions.json", "values/.git/config", ".docmind_backups/x.py"):
            with self.assertRaises(wb.FsError) as cm:
                wb.save_file(self.root, bad, "{}", reindex=False)
            self.assertEqual(cm.exception.status, 403, bad)
        with self.assertRaises(wb.FsError) as cm:
            wb.save_file(self.root, "evil.exe", "MZ", reindex=False)
        self.assertEqual(cm.exception.status, 403)

    def test_save_size_limit(self):
        old = wb.WB_MAX_FILE_BYTES
        wb.WB_MAX_FILE_BYTES = 64
        try:
            with self.assertRaises(wb.FsError) as cm:
                wb.save_file(self.root, "a.py", "x" * 200, reindex=False)
            self.assertEqual(cm.exception.status, 413)
        finally:
            wb.WB_MAX_FILE_BYTES = old


class TestCreateRenameDelete(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="wbfs_test_")
        wb.invalidate_status()

    def tearDown(self):
        wb.invalidate_status()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, content="x = 1\n"):
        p = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        return p

    def enable_regions(self):
        with open(os.path.join(self.root, "regions.json"), "w", encoding="utf-8") as fh:
            json.dump({"regions": [
                {"key": "values", "dir": "values", "name": "数值区", "desc": "",
                 "access": "", "depends_on": [], "exports": [], "verify": ""},
                {"key": "behaviors", "dir": "behaviors", "name": "角色行为区", "desc": "",
                 "access": "", "depends_on": ["values"], "exports": [], "verify": ""},
            ]}, fh, ensure_ascii=False)

    def test_create_file_and_folder(self):
        wb.create_path(self.root, "d1", "folder")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "d1")))
        wb.create_path(self.root, "d1/a.gd", "file", "pass")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "d1", "a.gd")))
        with self.assertRaises(wb.FsError) as cm:
            wb.create_path(self.root, "d1", "folder")
        self.assertEqual(cm.exception.status, 409)

    def test_rename_cross_region_blocked_but_allowed_without_regions(self):
        self.enable_regions()
        os.makedirs(os.path.join(self.root, "behaviors"), exist_ok=True)
        self.write("values/a.gd")
        with self.assertRaises(wb.FsError) as cm:
            wb.rename_path(self.root, "values/a.gd", "behaviors/a.gd")
        self.assertEqual(cm.exception.status, 403)

    def test_rename_within_region_and_disabled_mode(self):
        self.write("values/a.gd", "pass")
        wb.rename_path(self.root, "values/a.gd", "values/b.gd", reindex=False)
        self.assertFalse(os.path.isfile(os.path.join(self.root, "values", "a.gd")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "values", "b.gd")))
        os.makedirs(os.path.join(self.root, "other"), exist_ok=True)
        wb.rename_path(self.root, "values/b.gd", "other/b.gd", reindex=False)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "other", "b.gd")))

    @unittest.skipUnless(have_git(), "无 git")
    def test_rename_tracked_uses_git_mv(self):
        self.enable_regions()
        repo = self.init_repo("values")
        self.write("values/a.gd", "pass")
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", "init"], cwd=repo)
        r = wb.rename_path(self.root, "values/a.gd", "values/b.gd", reindex=False)
        self.assertTrue(r["git_renamed"])

    def init_repo(self, rel="."):
        d = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(d, exist_ok=True)
        git(["init", "-q"], cwd=d)
        git(["config", "user.email", "test@local"], cwd=d)
        git(["config", "user.name", "Test"], cwd=d)
        return d

    def test_delete_untracked_requires_force(self):
        self.write("values/new.gd", "pass")
        with self.assertRaises(wb.FsError) as cm:
            wb.delete_path(self.root, "values/new.gd")
        self.assertEqual(cm.exception.status, 409)
        wb.delete_path(self.root, "values/new.gd", force=True)
        self.assertFalse(os.path.exists(os.path.join(self.root, "values", "new.gd")))

    @unittest.skipUnless(have_git(), "无 git")
    def test_delete_tracked_directly(self):
        self.enable_regions()
        repo = self.init_repo("values")
        self.write("values/old.gd", "pass")
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", "init"], cwd=repo)
        r = wb.delete_path(self.root, "values/old.gd")
        self.assertTrue(r["recoverable"])
        self.assertFalse(os.path.isfile(os.path.join(self.root, "values", "old.gd")))

    def test_delete_folder_rules(self):
        self.write("d/sub/a.txt", "a")
        with self.assertRaises(wb.FsError) as cm:
            wb.delete_path(self.root, "d")
        self.assertEqual(cm.exception.status, 409)
        # 未跟踪内容：recursive 但无 force 仍拦截
        with self.assertRaises(wb.FsError) as cm:
            wb.delete_path(self.root, "d", recursive=True)
        self.assertEqual(cm.exception.status, 409)
        wb.delete_path(self.root, "d", recursive=True, force=True)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "d")))

    def test_delete_protected(self):
        with self.assertRaises(wb.FsError):
            wb.delete_path(self.root, "regions.json")

    @unittest.skipUnless(have_git(), "无 git")
    def test_gitlog(self):
        self.enable_regions()
        repo = self.init_repo("values")
        self.write("values/a.gd", "pass")
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", "first commit"], cwd=repo)
        r = wb.git_log(self.root, "values/a.gd")
        self.assertEqual(len(r["commits"]), 1)
        self.assertIn("first commit", r["commits"][0]["message"])
        with self.assertRaises(wb.FsError) as cm:
            wb.git_log(self.root, "nope.gd")
        self.assertEqual(cm.exception.status, 404)


@unittest.skipUnless(have_git(), "无 git")
class GitRevertTests(WbTestBase):
    """P3：文件级回滚 + 历史版本恢复。"""

    def _commit(self, repo, msg):
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", msg], cwd=repo)

    def test_revert_dirty_file(self):
        repo = self.init_repo(".")
        self.write("a.gd", "var v = 1\n")
        self._commit(repo, "c1")
        self.write("a.gd", "var v = 2\n")
        r = wb.revert_file(self.root, "a.gd")
        self.assertTrue(r["reverted"])
        with open(os.path.join(repo, "a.gd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "var v = 1\n")
        # 含暂存区改动也一并回滚
        self.write("a.gd", "var v = 3\n")
        git(["add", "a.gd"], cwd=repo)
        r2 = wb.revert_file(self.root, "a.gd")
        self.assertTrue(r2["reverted"])
        with open(os.path.join(repo, "a.gd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "var v = 1\n")

    def test_revert_clean_is_noop(self):
        repo = self.init_repo(".")
        self.write("a.gd", "var v = 1\n")
        self._commit(repo, "c1")
        r = wb.revert_file(self.root, "a.gd")
        self.assertFalse(r["reverted"])
        self.assertEqual(r["reason"], "clean")

    def test_revert_untracked_rejected(self):
        self.init_repo(".")
        self.write("new.gd", "var x\n")
        with self.assertRaises(wb.FsError) as cm:
            wb.revert_file(self.root, "new.gd")
        self.assertEqual(cm.exception.status, 409)

    def test_revert_outside_repo_rejected(self):
        self.write("a.gd", "var v = 1\n")
        with self.assertRaises(wb.FsError) as cm:
            wb.revert_file(self.root, "a.gd")
        self.assertEqual(cm.exception.status, 400)

    def test_revert_protected_contract(self):
        repo = self.init_repo(".")
        self.write("regions.json", "{}\n")
        self._commit(repo, "c1")
        self.write("regions.json", '{"x":1}\n')
        with self.assertRaises(wb.FsError) as cm:
            wb.revert_file(self.root, "regions.json")
        self.assertEqual(cm.exception.status, 403)

    def test_show_and_restore_history(self):
        repo = self.init_repo(".")
        self.write("a.gd", "var v = 1\n")
        self._commit(repo, "c1")
        old = wb.git_log(self.root, "a.gd")["commits"][0]["full_hash"]
        self.write("a.gd", "var v = 2\n")
        self._commit(repo, "c2")

        shown = wb.git_show_at(self.root, "a.gd", old)
        self.assertEqual(shown["content"], "var v = 1\n")

        with self.assertRaises(wb.FsError) as cm:
            wb.git_show_at(self.root, "a.gd", "--evil")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(wb.FsError) as cm2:
            wb.git_show_at(self.root, "a.gd", old + "^{tree}")
        self.assertEqual(cm2.exception.status, 400)

        r = wb.restore_file_at(self.root, "a.gd", old, reindex=False)
        self.assertTrue(r["restored"])
        with open(os.path.join(repo, "a.gd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "var v = 1\n")
        # 恢复后只是未提交修改，HEAD 仍是 c2，可再次一键回滚
        back = wb.revert_file(self.root, "a.gd")
        self.assertTrue(back["reverted"])
        with open(os.path.join(repo, "a.gd"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "var v = 2\n")


@unittest.skipUnless(have_git(), "无 git")
class GitChangesTests(WbTestBase):
    """P4：仓库级「改动总览」。

    与文件树状态点的区别：状态点只有 tracked/dirty 布尔，看不出**是新增还是
    删除还是改名**——而总览面板恰恰要靠这个分类来分组显示、并决定图标。
    所以这里逐条钉住 6 种语义的归类，以及 R 记录的**路径方向**。

    R 记录方向是本轮实际写错过的地方：`git status --porcelain -z` 的改名记录
    是 `R  <新名>\\0<旧名>\\0`（**新名在前**），与"源在前"的直觉相反，
    写反了界面会显示成 `新名 -> 旧名` 的倒序。
    """

    def _commit_all(self, repo, msg):
        git(["add", "-A"], cwd=repo)
        git(["commit", "-q", "-m", msg], cwd=repo)

    def test_no_repo_returns_empty_not_error(self):
        """不在任何仓库里不是错误——面板该显示「无改动」而不是报错。"""
        self.write("a.txt", "x\n")
        r = wb.git_changes(self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(r["repos"], [])
        self.assertEqual(r["total"], 0)
        self.assertTrue(r["empty"])

    def test_clean_repo_is_empty(self):
        repo = self.init_repo(".")
        self.write("a.txt", "x\n")
        self._commit_all(repo, "c1")
        r = wb.git_changes(self.root)
        self.assertEqual(len(r["repos"]), 1)
        self.assertEqual(r["repos"][0]["files"], [])
        self.assertTrue(r["empty"])
        self.assertEqual(r["repos"][0]["branch"], "master")

    def test_all_five_working_states_are_classified(self):
        repo = self.init_repo(".")
        self.write("mod.txt", "1\n")
        self.write("del.txt", "1\n")
        self._commit_all(repo, "base")

        self.write("mod.txt", "2\n")            # 修改
        os.remove(os.path.join(repo, "del.txt"))  # 删除
        self.write("untracked.txt", "n\n")      # 未跟踪
        self.write("staged.txt", "s\n")         # 新增（已暂存）
        git(["add", "staged.txt"], cwd=repo)

        r = wb.git_changes(self.root)
        got = {f["path"]: f["kind"] for f in r["repos"][0]["files"]}
        self.assertEqual(got.get("mod.txt"), "modified")
        self.assertEqual(got.get("del.txt"), "deleted")
        self.assertEqual(got.get("untracked.txt"), "untracked")
        self.assertEqual(got.get("staged.txt"), "added")
        self.assertEqual(r["total"], 4)

    def test_rename_keeps_source_and_target_in_the_right_order(self):
        """改名必须显示成 `旧 -> 新`。porcelain 是**新名在前**，容易写反。"""
        repo = self.init_repo(".")
        self.write("old.txt", "x\n")
        self._commit_all(repo, "c1")
        git(["mv", "old.txt", "new.txt"], cwd=repo)

        files = wb.git_changes(self.root)["repos"][0]["files"]
        self.assertEqual(len(files), 1)
        f = files[0]
        self.assertEqual(f["kind"], "renamed")
        self.assertEqual(f["path"], "old.txt", "path 必须是源（旧名）")
        self.assertEqual(f["path_to"], "new.txt", "path_to 必须是目标（新名）")

    def test_conflict_ranks_first(self):
        """冲突排最前——它是唯一必须人工介入的状态，不该被淹没在几十条里。"""
        repo = self.init_repo(".")
        self.write("c.txt", "base\n")
        self._commit_all(repo, "base")
        git(["checkout", "-q", "-b", "other"], cwd=repo)
        self.write("c.txt", "other\n")
        self._commit_all(repo, "other")
        git(["checkout", "-q", "master"], cwd=repo)
        self.write("c.txt", "mine\n")
        self._commit_all(repo, "mine")
        git(["merge", "other"], cwd=repo)  # 故意冲突，不 check 返回值
        self.write("plain.txt", "p\n")     # 一条普通未跟踪，用来验排序

        files = wb.git_changes(self.root)["repos"][0]["files"]
        self.assertTrue(files, "冲突状态下不该是空列表")
        self.assertEqual(files[0]["kind"], "conflict",
                         "冲突必须排在第一条")

    def test_multiple_repos_are_listed_separately(self):
        """根目录 + 分区各是独立仓库时，两个都要收（与文件树口径一致）。"""
        self.enable_regions()
        root_repo = self.init_repo(".")
        self.write("root.txt", "r\n")
        self._commit_all(root_repo, "root")
        self.write("root.txt", "r2\n")

        sub = self.init_repo("values")
        self.write("values/v.gd", "pass\n")
        self._commit_all(sub, "v1")
        self.write("values/v.gd", "pass  # changed\n")

        r = wb.git_changes(self.root)
        by_dir = {repo["dir"]: repo for repo in r["repos"]}
        self.assertIn("", by_dir)
        self.assertIn("values", by_dir)
        root_paths = {f["path"] for f in by_dir[""]["files"]}
        self.assertIn("root.txt", root_paths)
        sub_paths = {f["path"] for f in by_dir["values"]["files"]}
        self.assertIn("v.gd", sub_paths)

    def test_truncates_instead_of_exploding(self):
        """巨型仓库不能把前端拖垮：超上限要截断并置 truncated 标志。"""
        repo = self.init_repo(".")
        self.write("seed.txt", "x\n")
        self._commit_all(repo, "base")
        for i in range(wb.WB_CHANGES_MAX + 5):
            self.write("f%04d.txt" % i, "n\n")

        r = wb.git_changes(self.root)
        files = r["repos"][0]["files"]
        self.assertEqual(len(files), wb.WB_CHANGES_MAX)
        self.assertTrue(r["truncated"], "截断时必须置位，否则前端以为这就是全部")

    def test_cjk_path_is_not_escaped(self):
        """中文路径必须原样返回（-z 的用途），否则前端显示乱码。"""
        repo = self.init_repo(".")
        self.write("docs/说明.txt", "x\n")
        self._commit_all(repo, "base")
        self.write("docs/说明.txt", "y\n")

        files = wb.git_changes(self.root)["repos"][0]["files"]
        self.assertEqual([f["path"] for f in files], ["docs/说明.txt"])

    def test_it_never_writes_to_disk(self):
        """只读承诺：跑完前后，仓库的 git status 必须逐字一致。"""
        repo = self.init_repo(".")
        self.write("a.txt", "1\n")
        self._commit_all(repo, "base")
        self.write("a.txt", "2\n")
        before = git_out(["status", "--porcelain"], repo)
        wb.git_changes(self.root)
        after = git_out(["status", "--porcelain"], repo)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
