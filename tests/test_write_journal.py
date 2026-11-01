# -*- coding: utf-8 -*-
"""回合级撤销的底座：写账本 + 反序恢复 + 写盘漏斗。

## 为什么有这一套

`lanes` 那张写账本只存**改前内容的 sha256**，不存内容，而且**只有绑了 lane 才记**
（注释写明"单用户模式零 I/O"）。于是单人日常用下来：agent 改了文件、没 commit，
`git revert` 与 `restore_file_at` 都救不了 —— 最容易真丢东西的那一档没有兜底。

## 四条设计约束（都是实测出来的坑）

1. **`after_sha` 取写完再读回来的磁盘字节**，不是把新内容 encode 一下。Windows 文本
   模式会做换行翻译，撤销时的冲突比较读的也是磁盘字节；两侧口径不一致会让每次撤销
   都被自己误判成"别人改过"。
2. **turn 用 contextvar，不用 threading.local**：并行工具批次走 `copy_context()`，
   线程本地在工作线程里读不到，那一批写入会记成"没有回合"，撤销时整批漏掉。
3. **冲突就跳过，不覆盖**；改前内容没存下就明说恢复不了。宁可少退一个文件，
   也不把别人（或后续回合）的改动抹掉。
4. **`after` 也要落 blob**，且 `prune()` 的引用集合同时算上 `before_sha` 与 `after_sha`：
   只记指纹不存字节时，文件一旦被账本外手段删掉，那份"最新内容"在本系统里就不存在了
   （2026-11-01 真实丢档）；反过来把 after 全当引用又会让保留期形同不存在。
"""
import ast
import contextlib
import contextvars
import os
import shutil
import tempfile
import threading
import unittest

import config
from agent_runtime import write_journal
import tools as agent_tools


class _StateRoot(unittest.TestCase):
    """code_root + STATE_ROOT 都指向临时目录，不碰仓库真实状态。"""

    def setUp(self):
        self._prev_state = config.STATE_ROOT
        self._prev_root = config.get_runtime("code_root")
        self.state = tempfile.mkdtemp(prefix="docmind_wj_state_")
        self.repo = tempfile.mkdtemp(prefix="docmind_wj_repo_")
        config.STATE_ROOT = self.state
        config.set_runtime("code_root", self.repo)

    def tearDown(self):
        config.STATE_ROOT = self._prev_state
        config.set_runtime("code_root", self._prev_root)
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def write(self, rel, content):
        """模拟一次 agent 写入：走真漏斗，所以账本里会有这一笔。"""
        target = os.path.join(self.repo, rel)
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        op = "modify" if os.path.isfile(target) else "create"
        agent_tools._write_project_file(target, content, rel=rel, root=self.repo, op=op)
        return target

    def read_bytes(self, rel):
        with open(os.path.join(self.repo, rel), "rb") as handle:
            return handle.read()

    def read(self, rel):
        with open(os.path.join(self.repo, rel), encoding="utf-8") as handle:
            return handle.read()


class RecordTests(_StateRoot):
    def test_before_content_is_stored_and_restorable(self):
        self.write("a.py", "一\n")
        token = write_journal.bind_turn("t1")
        try:
            self.write("a.py", "二\n")
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t1")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["restorable"], "改前内容存下来了就必须标可恢复")
        self.assertTrue(rows[0]["before_sha"])
        # after_sha 必须是**磁盘上的字节**：Windows 文本模式把 "\n" 翻成 "\r\n"，
        # 拿 encode 后的内容比会立刻对不上（第一版断言就是这么写错的）。
        self.assertEqual(rows[0]["after_sha"], write_journal.sha256_bytes(self.read_bytes("a.py")))

    def test_create_needs_no_before_content(self):
        token = write_journal.bind_turn("t1")
        try:
            self.write("new.py", "内容\n")
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t1")
        self.assertEqual(rows[0]["op"], "create")
        self.assertTrue(rows[0]["restorable"], "撤销新建 = 删掉它，不需要旧内容")
        self.assertEqual(rows[0]["before_sha"], "")

    def test_oversized_before_is_not_stored_and_says_so(self):
        big = "x" * (write_journal.MAX_BLOB_BYTES + 10)
        self.write("big.py", big)
        token = write_journal.bind_turn("t1")
        try:
            self.write("big.py", "小\n")
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t1")
        self.assertFalse(rows[0]["restorable"], "超限内容不该被谎称可恢复")

    def test_after_content_survives_deleting_the_file(self):
        """最新内容也进账本：文件被账本之外的手段删掉，还能按指纹取回字节。

        2026-11-01 真实事故：三份 docs/notes 被误删后无法逐字节恢复 —— 当时
        `create` 只记了 after_sha 这个指纹，字节只存在于工作树一处。
        """
        token = write_journal.bind_turn("t1")
        try:
            self.write("note.md", "正文内容\n")
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t1")
        self.assertTrue(rows[0]["after_restorable"], "内容存下来了就必须标可恢复")
        on_disk = self.read_bytes("note.md")
        os.remove(os.path.join(self.repo, "note.md"))
        self.assertEqual(write_journal.read_blob(self.repo, rows[0]["after_sha"]), on_disk)

    def test_oversized_after_is_not_stored_and_says_so(self):
        big = "x" * (write_journal.MAX_BLOB_BYTES + 10)
        token = write_journal.bind_turn("t1")
        try:
            self.write("big.py", big)
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t1")
        self.assertFalse(rows[0]["after_restorable"], "超限内容不该被谎称可恢复")
        self.assertIsNone(write_journal.read_blob(self.repo, rows[0]["after_sha"]))

    def test_read_blob_refuses_non_hex_digest(self):
        """指纹来自账本文件，不能照它直接拼路径（../ 会跑出 blob 区）。"""
        for bad in ("", "../evil", "0" * 63, "z" * 64):
            self.assertIsNone(write_journal.read_blob(self.repo, bad))


class UndoTests(_StateRoot):
    def _turn(self, turn_id, body):
        token = write_journal.bind_turn(turn_id)
        try:
            body()
        finally:
            write_journal.reset_turn(token)

    def test_undo_restores_modified_file(self):
        self.write("a.py", "一\n")
        self._turn("t1", lambda: self.write("a.py", "二\n"))
        result = write_journal.undo_turn(self.repo, turn="t1")
        self.assertTrue(result["ok"], result["skipped"])
        self.assertEqual(self.read("a.py"), "一\n")

    def test_undo_deletes_file_created_in_the_turn(self):
        self._turn("t1", lambda: self.write("new.py", "内容\n"))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "new.py")))
        result = write_journal.undo_turn(self.repo, turn="t1")
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new.py")),
                         "本回合新建的文件必须被删掉")

    def test_undo_reverses_in_order(self):
        """同一回合改两次同一文件：撤销要退到**回合开始前**，不是退到中间态。"""
        self.write("a.py", "一\n")
        self._turn("t1", lambda: (self.write("a.py", "二\n"), self.write("a.py", "三\n")))
        write_journal.undo_turn(self.repo, turn="t1")
        self.assertEqual(self.read("a.py"), "一\n")

    def test_conflict_is_skipped_not_overwritten(self):
        self.write("a.py", "一\n")
        self._turn("t1", lambda: self.write("a.py", "二\n"))
        # 这一回合之后别人又改了同一个文件（绕过账本直接落盘）
        with open(os.path.join(self.repo, "a.py"), "w", encoding="utf-8") as handle:
            handle.write("别人的改动\n")
        result = write_journal.undo_turn(self.repo, turn="t1")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("又被改过", result["skipped"][0]["reason"])
        self.assertEqual(self.read("a.py"), "别人的改动\n", "冲突时必须一个字都不动")

    def test_unrestorable_entry_is_reported_not_faked(self):
        big = "x" * (write_journal.MAX_BLOB_BYTES + 10)
        self.write("big.py", big)
        self._turn("t1", lambda: self.write("big.py", "小\n"))
        result = write_journal.undo_turn(self.repo, turn="t1")
        self.assertFalse(result["ok"])
        self.assertIn("恢复不了", result["skipped"][0]["reason"])
        self.assertEqual(self.read("big.py"), "小\n")

    def test_second_undo_is_refused_and_nothing_is_lost(self):
        """同一回合不许退第二次：把补偿记录再重放一遍只会让文件来回搬。

        实测过这个漏洞：第二次撤销把 `op=undo` 也当原始写入处理，结果同一文件被
        搬两次，"退回来"是假的。现在拒绝二次撤销，同时撤销前的内容仍存成 blob。
        """
        self.write("a.py", "一\n")
        self._turn("t1", lambda: self.write("a.py", "二\n"))
        first = write_journal.undo_turn(self.repo, turn="t1")
        self.assertTrue(first["ok"], first["skipped"])
        self.assertEqual(self.read("a.py"), "一\n")
        second = write_journal.undo_turn(self.repo, turn="t1")
        self.assertFalse(second["ok"])
        self.assertIn("已经撤销过", second["error"])
        self.assertEqual(self.read("a.py"), "一\n", "拒绝二次撤销时不许再动文件")
        # 撤销前那一刻的"二"还在账本+blob 里，数据没丢
        compensation = [row for row in write_journal.entries_for(self.repo, turn="t1")
                        if str(row.get("op")) == "undo"]
        self.assertEqual(len(compensation), 1)
        self.assertTrue(compensation[0]["before_sha"])
        self.assertTrue(os.path.isfile(
            write_journal._blob_path(self.repo, compensation[0]["before_sha"])))

    def test_dry_run_touches_nothing(self):
        self.write("a.py", "一\n")
        self._turn("t1", lambda: self.write("a.py", "二\n"))
        result = write_journal.undo_turn(self.repo, turn="t1", dry_run=True)
        self.assertEqual(self.read("a.py"), "二\n")
        self.assertEqual(len(write_journal.entries_for(self.repo)), 2, "干跑不许记账")
        self.assertIn("将恢复", result["restored"][0]["action"])

    def test_undo_without_turn_uses_the_last_one(self):
        self._turn("tA", lambda: self.write("a.py", "一\n"))
        self._turn("tB", lambda: self.write("a.py", "二\n"))
        result = write_journal.undo_turn(self.repo)
        self.assertEqual(result["turn"], "tB")
        self.assertEqual(self.read("a.py"), "一\n")


class FunnelTests(_StateRoot):
    """写盘漏斗必须真的覆盖所有项目内写点 —— 漏一处就是那个文件退不回去。"""

    PROJECT_WRITERS = ("apply_edit", "dev_apply_edits", "create_file", "confirm_edit")

    def test_write_project_file_records_the_turn(self):
        token = write_journal.bind_turn("t9")
        try:
            self.write("x.py", "内容\n")
        finally:
            write_journal.reset_turn(token)
        rows = write_journal.entries_for(self.repo, turn="t9")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "x.py")

    def test_no_bare_open_write_left_in_project_writers(self):
        """AST 守卫：那四个写函数里不许再出现 `open(..., "w")`。

        写成源码扫描而不是行为断言，是因为漏掉一个写点时行为测试照样全绿 ——
        这正是原来的状态：三处记了账、五处没记。
        """
        path = os.path.join(os.path.dirname(os.path.abspath(agent_tools.__file__)), "tools.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())

        def open_mode(call):
            mode = None
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                mode = call.args[1].value
            for kw in call.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            return mode

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in self.PROJECT_WRITERS:
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                if (getattr(call.func, "id", "") or getattr(call.func, "attr", "")) != "open":
                    continue
                mode = open_mode(call)
                if mode and "w" in str(mode):
                    offenders.append("%s:%d" % (node.name, call.lineno))
        self.assertEqual(offenders, [], "这些写点绕过了漏斗：%s" % offenders)

    def test_funnel_helper_is_the_only_writer(self):
        path = os.path.join(os.path.dirname(os.path.abspath(agent_tools.__file__)), "tools.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_write_project_file":
                opens = [c for c in ast.walk(node) if isinstance(c, ast.Call)
                         and (getattr(c.func, "id", "") == "open")]
                self.assertEqual(len(opens), 3, "1 写 + 2 读（改前/写后）")
                return
        self.fail("_write_project_file 不见了")


class ToolTests(_StateRoot):
    def test_status_on_empty_journal_says_so(self):
        out = agent_tools.dev_undo_turn("action: status")
        self.assertIn("账本是空的", out)

    def test_undo_defaults_to_dry_run(self):
        self.write("a.py", "一\n")
        token = write_journal.bind_turn("t1")
        try:
            self.write("a.py", "二\n")
        finally:
            write_journal.reset_turn(token)
        out = agent_tools.dev_undo_turn("action: undo")
        self.assertIn("干跑", out)
        self.assertEqual(self.read("a.py"), "二\n", "没给 apply 就不许落盘")

    def test_undo_applies_only_with_explicit_flag(self):
        self.write("a.py", "一\n")
        token = write_journal.bind_turn("t1")
        try:
            self.write("a.py", "二\n")
        finally:
            write_journal.reset_turn(token)
        out = agent_tools.dev_undo_turn("action: undo\napply: true")
        self.assertIn("已撤销", out)
        self.assertEqual(self.read("a.py"), "一\n")

    def test_registered_in_tools_and_domains(self):
        self.assertIn("dev_undo_turn", agent_tools.TOOLS)
        from agent_runtime import tool_domains
        # 没登记 domain 的工具不会出现在按域裁剪的清单里（`TOOL_DOMAINS[name]=(域,侧面)`）
        self.assertIn("dev_undo_turn", tool_domains.TOOL_DOMAINS)
        self.assertEqual(tool_domains.TOOL_DOMAINS["dev_undo_turn"][0], "verify")


class ContextVarTests(unittest.TestCase):
    def test_turn_id_survives_copy_context(self):
        """并行工具批次走 copy_context()：换成 threading.local 这里就会红。"""
        token = write_journal.bind_turn("t-parallel")
        seen = {}

        def worker():
            seen["turn"] = write_journal.current_turn()

        try:
            thread = threading.Thread(target=contextvars.copy_context().run, args=(worker,))
            thread.start()
            thread.join(timeout=5)
        finally:
            write_journal.reset_turn(token)
        self.assertEqual(seen.get("turn"), "t-parallel")


class PruneTests(_StateRoot):
    def test_prune_keeps_recent_turns_and_dry_runs_by_default(self):
        for index in range(3):
            token = write_journal.bind_turn("t%d" % index)
            try:
                self.write("a.py", "内容%d\n" % index)
            finally:
                write_journal.reset_turn(token)
        preview = write_journal.prune(self.repo, keep_turns=1)
        self.assertEqual(preview["dropped_entries"], 2)
        self.assertEqual(len(write_journal.entries_for(self.repo)), 3, "dry-run 不许真删")
        applied = write_journal.prune(self.repo, keep_turns=1, apply=True)
        self.assertEqual(len(write_journal.entries_for(self.repo)), 1)
        self.assertEqual(applied["dropped_entries"], 2)

    def test_prune_removes_only_unreferenced_blobs(self):
        self.write("a.py", "一\n")
        token = write_journal.bind_turn("t1")
        try:
            self.write("a.py", "二\n")
        finally:
            write_journal.reset_turn(token)
        blob_dir = os.path.join(os.path.dirname(write_journal._ledger(self.repo)), "blobs")
        self.assertTrue(os.path.isdir(blob_dir))
        write_journal.prune(self.repo, keep_turns=10, max_age_days=999, apply=True)
        self.assertTrue(os.listdir(os.path.join(blob_dir, os.listdir(blob_dir)[0])),
                        "还在账本引用里的 blob 不许删")

    def test_prune_keeps_after_blobs_and_drops_the_rest(self):
        """引用集合必须同时含 before_sha 与 after_sha：漏了后者会删掉存活条目的最新内容，
        全当引用又会让保留期形同不存在 —— 两个方向都要红。"""
        token = write_journal.bind_turn("tGone")
        try:
            self.write("gone.md", "该裁的\n")
        finally:
            write_journal.reset_turn(token)
        gone_sha = write_journal.entries_for(self.repo, turn="tGone")[0]["after_sha"]
        token = write_journal.bind_turn("tKeep")
        try:
            self.write("keep.md", "该留的\n")
        finally:
            write_journal.reset_turn(token)
        keep_sha = write_journal.entries_for(self.repo, turn="tKeep")[0]["after_sha"]
        self.assertTrue(os.path.isfile(write_journal._blob_path(self.repo, keep_sha)))
        applied = write_journal.prune(self.repo, keep_turns=1, max_age_days=999, apply=True)
        self.assertEqual(applied["dropped_entries"], 1)
        self.assertTrue(os.path.isfile(write_journal._blob_path(self.repo, keep_sha)),
                        "存活条目的 after blob 不许当孤儿删掉")
        self.assertFalse(os.path.isfile(write_journal._blob_path(self.repo, gone_sha)),
                         "被裁条目的 blob 要真删，否则保留期只是说说")


if __name__ == "__main__":
    unittest.main()
