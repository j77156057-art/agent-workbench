# -*- coding: utf-8 -*-
"""会话是**全局一个**：同一 session_id 跨项目仍是一个会话、仍绑最初那个库。

## 实测事故（2026-10-10，乙方案的由来）

带新代码的隔离实例里：同一 session_id 在项目 A 下问一次 -> 绑 A；
切到项目 B 再问 -> **绑 B**。原缺陷「切项目顶掉会话的库」换个形式重演。

根因：会话存储按 `<project_id>/<slug>.json` **分桶**。
B 项目读不到 A 桶的文件 -> 当成新会话 -> 重新绑 B。
`_agent_key` 的 `pid::sid` 只分开了 **Agent 实例**，`session_id` 本身还是
同一个字符串。「同一个会话跨项目」在分桶的磁盘上**根本不存在**。

会话级隔离的前提是「这个会话一直用它绑过的那个库」。分桶让这个前提
不成立，所以分桶去掉：一个 session_id 就是一个会话，全局唯一。

## 契约

    落盘位置  唯一：`<SESSIONS_DIR>/<slug>.json`
    绑定      永久：切项目不改（bind_code_root 已绑定即返回 False）
    新会话    独立：新 session_id 按当时的代码库绑，互不影响
    历史穿透  旧分桶文件仍可读，绑定取最早的那份
    删除      彻底：全局 + 所有旧分桶同名文件一起删
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

_SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="docmind_sessglobal_"))
os.environ["DOCMIND_STATE_DIR"] = str(_SANDBOX)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import sessions as S            # noqa: E402

SID = "sess-global-probe"


def tearDownModule():
    shutil.rmtree(_SANDBOX, ignore_errors=True)


def _d():
    """当前生效的会话目录。

    ⚠ 必须每次现取 `S.SESSIONS_DIR`，不能存死自己沙箱的路径 ——
    别的测试文件（如 test_projects.py）的 setUp 会把它改指到自己的临时目录，
    存死路径就会读到别人的目录（实测：单独跑全绿、合跑 8 条红）。
    """
    return pathlib.Path(S.SESSIONS_DIR)


def _flat(sid=SID):
    return _d() / ("%s.json" % sid)


def _bucket(pid, sid=SID):
    return _d() / S._slug(pid) / ("%s.json" % sid)


class SessionIsGlobalTests(unittest.TestCase):
    """一个 session_id 就是一个会话 —— 与项目无关。"""

    def setUp(self):
        # 与项目其它测试同样地改指/还原：绝不写进用户真实会话目录。
        self._old = S.SESSIONS_DIR
        self.tmp = _SANDBOX / ("run-%s" % self.id().rsplit(".", 1)[-1])
        self.tmp.mkdir(parents=True, exist_ok=True)
        S.SESSIONS_DIR = str(self.tmp / "sessions")
        self.addCleanup(setattr, S, "SESSIONS_DIR", self._old)

    # ---------- 落盘位置 ----------

    def test_write_lands_on_one_global_file(self):
        self.assertTrue(S.save(SID, [{"user": "u", "assistant": "a"}]))
        self.assertTrue(_flat().exists(), "全局扁平文件没写")
        self.assertEqual(_flat().parent, _d(), "写到了子目录里 —— 分桶又回来了")

    def test_project_id_does_not_change_the_path(self):
        """project_id 只是签名兼容，不许再影响存储位置。"""
        S.save(SID, [{"user": "u", "assistant": "a"}], project_id="prj-aaaa")
        S.save(SID, [{"user": "u2", "assistant": "a2"}], project_id="prj-bbbb")
        self.assertTrue(_flat().exists())
        self.assertFalse((_d() / "prj-aaaa").exists(),
                         "project_id 又影响了路径")
        self.assertEqual(len(S.load(SID, project_id="prj-bbbb")["turns"]), 1,
                         "同一 session_id 被 project_id 分成了两个会话")

    def test_path_helper_ignores_project_id(self):
        """直接打 `_path` —— 它是「唯一路径」这个承诺的入口。

        ⚠ 这条是补的：上一版只测 save/load，而它们走 `_write` 的
        `_bucket_dirs()[0]`，**根本不经过 `_path`**。把 `_path` 改回分桶，
        12 条判据全绿 —— 测了一条没人走的路径，恒真。所以必须直接打它。
        """
        a = S._path("abc", project_id="prj-a")
        b = S._path("abc", project_id="prj-b")
        c = S._path("abc")
        self.assertEqual(a, b, "换了 project_id，_path 就变了 —— 分桶又回来了")
        self.assertEqual(a, c, "不传 project_id 时的路径与传了不同")
        self.assertEqual(pathlib.Path(a).parent, _d(),
                         "_path 指向子目录 —— 会话又被按项目分开了")
        self.assertTrue(a.endswith("abc.json"))

    # ---------- 绑定永久（本改动的核心） ----------

    def test_binding_survives_a_project_switch(self):
        """**核心判据**：绑过 A 之后，切到 B 不许改成 B。"""
        S.bind_code_root(SID, "D:/A", project_id="prj-a")
        self.assertFalse(S.bind_code_root(SID, "D:/B", project_id="prj-b"),
                         "第二次绑定被接受了 —— 切项目把库顶掉了")
        self.assertEqual(S.current_code_root(SID, project_id="prj-b"), "D:/A",
                         "切项目后会话的代码库变了 —— 原缺陷重演")

    def test_second_session_binds_its_own_root(self):
        """反向守卫：新会话不受老会话影响（否则全局就成了一种新的串台）。"""
        S.bind_code_root("old", "D:/A")
        S.bind_code_root("new", "D:/B")
        self.assertEqual(S.current_code_root("old"), "D:/A")
        self.assertEqual(S.current_code_root("new"), "D:/B")

    def test_unbound_session_falls_back_to_empty(self):
        """没绑过的会话返回空串，调用方回落项目/全局（行为同改动前）。"""
        S.save(SID, [{"user": "u", "assistant": "a"}])
        self.assertEqual(S.current_code_root(SID), "")

    # ---------- 历史穿透与合并 ----------

    def test_legacy_bucket_is_read_through(self):
        """旧分桶文件仍在：读要能读到，第一次写就迁到全局。"""
        b = _bucket("prj-legacy")
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text(json.dumps({
            "session_id": SID, "turns": [{"user": "旧问题", "assistant": "旧回答"}],
            "summary": "", "code_root": "D:/Legacy"}), encoding="utf-8")
        got = S.load(SID)
        self.assertEqual([t["user"] for t in got["turns"]], ["旧问题"],
                         "旧分桶读不到 —— 老会话历史会一夜消失")
        self.assertEqual(got["code_root"], "D:/Legacy", "旧绑定丢了")

    def test_merge_keeps_the_earliest_binding(self):
        """同名多份历史：`code_root` 取**最早绑定的**，不是最近写的。"""
        d1 = _bucket("prj-old")
        d1.parent.mkdir(parents=True, exist_ok=True)
        d1.write_text(json.dumps({
            "session_id": SID, "updated_at": "2026-10-01T10:00:00",
            "code_root": "D:/Old", "code_root_at": "2026-10-01T10:00:00",
            "turns": [{"user": "旧", "assistant": "旧"}]}), encoding="utf-8")
        d2 = _bucket("prj-new")
        d2.parent.mkdir(parents=True, exist_ok=True)
        d2.write_text(json.dumps({
            "session_id": SID, "updated_at": "2026-10-02T10:00:00",
            "code_root": "D:/New", "code_root_at": "2026-10-02T10:00:00",
            "turns": [{"user": "新", "assistant": "新"}]}), encoding="utf-8")
        got = S.load(SID)
        self.assertEqual(got["code_root"], "D:/Old",
                         "合并时取了最近写的那份绑定 —— 会话会被人抢走")
        self.assertEqual([t["user"] for t in got["turns"]], ["新"],
                         "turns 应取 updated_at 最新的那份")

    def test_merge_without_binding_timestamps_uses_updated_at(self):
        """老记录没有 code_root_at 时回退 updated_at，不许崩也不许乱。"""
        b = _bucket("prj-noat")
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text(json.dumps({
            "session_id": SID, "updated_at": "2026-10-01T10:00:00",
            "code_root": "D:/NoAt", "turns": []}), encoding="utf-8")
        _flat().write_text(json.dumps({
            "session_id": SID, "updated_at": "2026-10-05T10:00:00",
            "turns": [{"user": "新", "assistant": "新"}]}), encoding="utf-8")
        got = S.load(SID)
        self.assertEqual(got["code_root"], "D:/NoAt")
        self.assertEqual([t["user"] for t in got["turns"]], ["新"])

    def test_merge_tie_goes_to_the_global_file(self):
        """**平手规则**：updated_at 相同时取全局那一份，不是碰运气。

        这条是补的：第一版 `_merge_candidates` 用 `sorted(...)[-1]`，时间戳
        相同时靠排序稳定性 —— 实测旧分桶盖到了全局文件上（旧会话"复活"）。
        平手必须显式按优先级（`_read_all_candidates` 的顺序），不许交给排序。
        """
        b = _bucket("prj-tie")
        b.parent.mkdir(parents=True, exist_ok=True)
        stamp = "2026-10-01T10:00:00"
        b.write_text(json.dumps({
            "session_id": SID, "updated_at": stamp,
            "turns": [{"user": "桶", "assistant": "桶"}],
            "code_root": "D:/Bucket", "code_root_at": stamp}), encoding="utf-8")
        _flat().write_text(json.dumps({
            "session_id": SID, "updated_at": stamp,
            "turns": [{"user": "全局", "assistant": "全局"}],
            "code_root": "D:/Global", "code_root_at": stamp}), encoding="utf-8")
        got = S.load(SID)
        self.assertEqual([t["user"] for t in got["turns"]], ["全局"],
                         "平手时旧分桶盖过了全局文件")
        self.assertEqual(got["code_root"], "D:/Global",
                         "绑定也平手 —— 同样该取全局（靠前）的那份")

    # ---------- 列表与删除 ----------

    def test_list_sessions_dedupes_across_locations(self):
        b = _bucket("prj-dup")
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text(json.dumps({
            "session_id": SID, "turns": [{"user": "x", "assistant": "y"}],
            "code_root": "D:/Dup"}), encoding="utf-8")
        S.save(SID, [{"user": "x", "assistant": "y"}])
        rows = S.list_sessions()
        self.assertEqual(len([r for r in rows if r["session_id"] == SID]), 1,
                         "同一会话在列表里出现多次")

    def test_delete_removes_flat_and_all_buckets(self):
        b = _bucket("prj-del")
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_text(json.dumps({"session_id": SID, "turns": []}),
                     encoding="utf-8")
        S.save(SID, [{"user": "u", "assistant": "a"}])
        self.assertTrue(_flat().exists())
        S.delete(SID)
        self.assertFalse(_flat().exists(), "全局文件没删干净")
        self.assertFalse(b.exists(),
                         "旧分桶里的同名文件还在 —— 读穿透会把它捞回来，"
                         "用户看到「删了又出现」")

    # ---------- 兼容 ----------

    def test_load_of_unknown_session_is_empty(self):
        got = S.load("never-existed")
        self.assertEqual(got["turns"], [])
        self.assertEqual(got["code_root"], "")
        self.assertEqual(got["session_id"], "never-existed")

    def test_save_carries_binding_fields(self):
        S.bind_code_root(SID, "D:/Carry")
        S.save(SID, [{"user": "u", "assistant": "a"}])
        raw = json.loads(_flat().read_text(encoding="utf-8"))
        self.assertEqual(raw["code_root"], "D:/Carry")
        self.assertTrue(raw.get("code_root_at"), "code_root_at 没跟着落盘")
        S.save(SID, [{"user": "u2", "assistant": "a2"}])
        raw2 = json.loads(_flat().read_text(encoding="utf-8"))
        self.assertTrue(raw2.get("code_root_at"), "二次保存把 code_root_at 丢了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
