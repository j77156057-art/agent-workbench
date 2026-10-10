# -*- coding: utf-8 -*-
"""丙方案：会话有**归属项目**，跨项目隔离不再靠分桶。

## 为什么需要

乙方案把会话存储改成全局一份（一个 session_id 就是一个会话，跨项目仍是
同一个）。但 `/api/sessions` 有产品语义：**用户在项目 B 里不该看到 A 的会话**。

以前这条靠"按项目分桶"天然做到；分桶去掉后必须显式记住归属。

## 契约

* 会话首次绑定时定下 `owner_project_id`，之后**不随请求项目变**；
* `list_sessions(project_id=pid)` 只返回归属为 pid 的；
* `/api/sessions/{id}` 在归属不符时按"不存在"处理（空洞，不暴露存在性）；
* **老会话没有归属字段 -> 放行**：否则迁移后一夜之间所有历史会话都打不开，
  那比串台更糟；
* 存储仍是全局一份 —— 隔离不是靠"拆成两个会话"实现的。
"""

import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
# ⚠⚠ 沙箱必须建在 **STATE_ROOT 之下**，不能直接用 tempfile.mkdtemp。
#
# 实测：用 mkdtemp 会让 `sessions.SESSIONS_DIR` 落在 `D:\\Temp\\docmind_owner_xxx`
# 而 `config.STATE_ROOT` 是另一个 `D:\\Temp\\docmind_test_state_xxx` ——
# 于是 `tests/test_state_root_isolation.py::test_persistence_does_not_write_repo_root`
# 在**组合跑**时变红（单独跑不红，所以极难发现）。项目里其他 21 个会话类
# 测试都建在 STATE_ROOT 下，照做即可。
#
# 顺序：先设 DOCMIND_STATE_DIR 让 STATE_ROOT 落到沙箱，再 import sessions。
_SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="docmind_owner_"))
os.environ["DOCMIND_STATE_DIR"] = str(_SANDBOX)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config                            # noqa: E402
import sessions as S                     # noqa: E402

# 以 STATE_ROOT 为基准再开一个子目录当会话根，确保 `_under` 判据成立。
# ⚠ STATE_ROOT 是 str（config 里就是 str），先转 Path。
SESSIONS_UNDER_STATE = pathlib.Path(config.STATE_ROOT) / "sessions"
SESSIONS_UNDER_STATE.mkdir(parents=True, exist_ok=True)
S.SESSIONS_DIR = str(SESSIONS_UNDER_STATE)

SID = "own-probe"
PID_A = "prj-aaaa"
PID_B = "prj-bbbb"
ROOT_A = "D:/Owner/ProjA"
ROOT_B = "D:/Owner/ProjB"


def tearDownModule():
    shutil.rmtree(_SANDBOX, ignore_errors=True)


class OwnerFieldTests(unittest.TestCase):
    """归属字段本身。"""

    def setUp(self):
        self._old = S.SESSIONS_DIR
        # ⚠ 沙箱必须在 STATE_ROOT 之下。用 mkdtemp/独立临时目录会让
        # SESSIONS_DIR 落在 STATE_ROOT 外，组合跑时把
        # test_state_root_isolation 带红（实测：单独跑不红）。
        self.tmp = pathlib.Path(config.STATE_ROOT) / ("owner-t-%s" % self.id().rsplit(".", 1)[-1])
        (self.tmp / "sessions").mkdir(parents=True, exist_ok=True)
        S.SESSIONS_DIR = str(self.tmp / "sessions")
        self.addCleanup(setattr, S, "SESSIONS_DIR", self._old)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_bind_records_owner(self):
        """**核心判据**：首次绑定时归属被写进落盘。"""
        S.bind_code_root(SID, ROOT_A, PID_A)
        self.assertEqual(S.owned_by(SID, PID_A), PID_A)
        raw = S.load(SID, PID_A)
        self.assertEqual(raw.get("owner_project_id"), PID_A)

    def test_owner_uses_current_project_when_not_given(self):
        """调用方没给 project_id 时用「当前项目」，不留空。

        ⚠ 必须用**真注册的项目**：`projects.set_current` 只接受已登记的 pid，
        传假 id 会返回 False、current 不变（实测踩过）。所以这里先 ensure 一个。
        """
        import projects
        d = _SANDBOX / ("cur-%s" % self.id().rsplit(".", 1)[-1])
        d.mkdir(parents=True, exist_ok=True)
        pid = projects.ensure_project(str(d))
        old = projects.current_project_id()
        try:
            self.assertTrue(projects.set_current(pid), "自检：切入真项目失败")
            S.bind_code_root(SID, ROOT_A)          # 不传 project_id
            self.assertEqual(S.owned_by(SID, pid), pid,
                             "没传 project_id 时归属应是当前项目")
        finally:
            if old:
                projects.set_current(old)

    def test_owner_does_not_follow_the_requesting_project(self):
        """归属是会话自己的，换了请求项目也不改。"""
        S.bind_code_root(SID, ROOT_A, PID_A)
        # 用 B 的 project_id 反复读写
        S.save(SID, [{"user": "u", "assistant": "a"}], project_id=PID_B)
        S.snapshot(SID, [{"user": "u", "assistant": "a"}], project_id=PID_B)
        self.assertEqual(S.owned_by(SID, PID_B), PID_A,
                         "归属被请求项目改掉了")

    def test_second_bind_does_not_change_owner(self):
        """已绑定的会话：绑第二次不许连归属一起改。"""
        S.bind_code_root(SID, ROOT_A, PID_A)
        S.bind_code_root(SID, ROOT_B, PID_B)      # 应被拒绝
        self.assertEqual(S.owned_by(SID, PID_B), PID_A)
        self.assertEqual(S.current_code_root(SID, PID_B), ROOT_A)

    def test_save_never_overwrites_owner(self):
        """`save` 带 project_id 时**不许**把归属改成那个项目。

        为什么必须有这条：`bind_code_root` 有"已绑定就不动"的闸门，所以
        光测它测不出风险（第二次 bind 根本进不到写 owner 那行）。真正会
        冲掉归属的是 `save` / `snapshot` / `backfill` 这些没有闸门的入口 ——
        实测把 save 改成"带 pid 就覆盖 owner"，只有这条判据抓住。
        """
        S.bind_code_root(SID, ROOT_A, PID_A)
        S.save(SID, [{"user": "u", "assistant": "a"}], project_id=PID_B)
        S.snapshot(SID, [{"user": "u", "assistant": "a"}], project_id=PID_B)
        S.save(SID, [{"user": "u2", "assistant": "a2"}], project_id=PID_B)
        self.assertEqual(S.owned_by(SID, PID_B), PID_A,
                         "save/snapshot 把归属冲掉了")
        self.assertEqual(S.current_code_root(SID, PID_B), ROOT_A,
                         "连代码库一起被冲掉了")

    # ---------- 老会话兼容 ----------

    def test_legacy_session_without_owner_is_visible(self):
        """老会话没有归属字段 —— 不传 pid 时必须仍能列出来/读得到。

        否则升级后所有历史会话一夜消失，那比串台更糟。
        """
        S.save("legacy", [{"user": "old", "assistant": "old"}])
        self.assertEqual(S.owned_by("legacy", PID_A), "")
        rows = S.list_sessions()
        self.assertIn("legacy", {r["session_id"] for r in rows})
        self.assertEqual([t["user"] for t in S.load("legacy", PID_A)["turns"]],
                         ["old"])

    def test_backfill_owner_only_fills_empty(self):
        """补归属只填空，不许覆盖已有归属。"""
        S.bind_code_root(SID, ROOT_A, PID_A)
        self.assertFalse(S.backfill_owner(SID, PID_B),
                         "已有归属还被覆盖了")
        self.assertEqual(S.owned_by(SID, PID_B), PID_A)
        S.save("legacy2", [{"user": "x", "assistant": "y"}])
        self.assertTrue(S.backfill_owner("legacy2", PID_A))
        self.assertEqual(S.owned_by("legacy2", PID_A), PID_A)


class ListFilterTests(unittest.TestCase):
    """`list_sessions` 的归属过滤。"""

    def setUp(self):
        self._old = S.SESSIONS_DIR
        # 同上：沙箱在 STATE_ROOT 之下。
        self.tmp = pathlib.Path(config.STATE_ROOT) / ("owner-l-%s" % self.id().rsplit(".", 1)[-1])
        (self.tmp / "sessions").mkdir(parents=True, exist_ok=True)
        S.SESSIONS_DIR = str(self.tmp / "sessions")
        self.addCleanup(setattr, S, "SESSIONS_DIR", self._old)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_filter_returns_only_owned(self):
        S.bind_code_root("a1", ROOT_A, PID_A)
        S.bind_code_root("b1", ROOT_B, PID_B)
        S.save("a1", [{"user": "x", "assistant": "y"}], project_id=PID_A)
        S.save("b1", [{"user": "x", "assistant": "y"}], project_id=PID_B)
        a = {r["session_id"] for r in S.list_sessions(project_id=PID_A)}
        b = {r["session_id"] for r in S.list_sessions(project_id=PID_B)}
        self.assertEqual(a, {"a1"})
        self.assertEqual(b, {"b1"})

    def test_ownerless_legacy_sessions_are_still_listed(self):
        """**无归属的老会话必须仍被列出来** —— 哪怕显式按 pid 过滤。

        这条是 `tests/test_projects_qa.py::B10` 教我的：第一版丙方案在
        显式过滤时把无归属会话一刀切掉，B10 立刻红 —— 那是**真回归**，
        P2 的明确意图就是"迁移后用户既有历史不消失"。

        代价是项目 B 的列表会混进几个老会话；跑过 backfill_owner 后就没有了。
        """
        S.save("legacy", [{"user": "x", "assistant": "y"}])       # 无归属
        S.bind_code_root("a1", ROOT_A, PID_A)
        S.save("a1", [{"user": "x", "assistant": "y"}])
        S.bind_code_root("b1", ROOT_B, PID_B)
        S.save("b1", [{"user": "x", "assistant": "y"}])
        ids_b = {r["session_id"] for r in S.list_sessions(project_id=PID_B)}
        self.assertIn("legacy", ids_b, "无归属的老会话被过滤掉了 —— 历史会消失")
        self.assertIn("b1", ids_b, "同项目的会话也没了")
        self.assertNotIn("a1", ids_b, "A 的会话漏进 B 的列表了")
        # 有归属的**别的**项目会话在 A 的列表里仍必须被挡住
        ids_a = {r["session_id"] for r in S.list_sessions(project_id=PID_A)}
        self.assertNotIn("b1", ids_a, "B 的会话漏进 A 的列表了")
        self.assertIn("a1", ids_a)
        self.assertIn("legacy", ids_a, "无归属老会话在 A 的列表里也该在")
        # 行里带 owner，前端可自行区分
        row = [r for r in S.list_sessions(project_id=PID_A)
               if r["session_id"] == "legacy"][0]
        self.assertEqual(row["owner_project_id"], "")

    def test_filter_still_blocks_other_projects_sessions(self):
        """有归属的别项目会话必须被挡住（隔离本身没丢）。"""
        S.bind_code_root("a1", ROOT_A, PID_A)
        S.bind_code_root("b1", ROOT_B, PID_B)
        S.save("a1", [{"user": "x", "assistant": "y"}])
        S.save("b1", [{"user": "x", "assistant": "y"}])
        self.assertEqual({r["session_id"] for r in S.list_sessions(project_id=PID_A)},
                         {"a1"})
        self.assertEqual({r["session_id"] for r in S.list_sessions(project_id=PID_B)},
                         {"b1"})

    def test_no_pid_returns_everything(self):
        S.bind_code_root("a1", ROOT_A, PID_A)
        S.bind_code_root("b1", ROOT_B, PID_B)
        S.save("a1", [{"user": "x", "assistant": "y"}])
        S.save("b1", [{"user": "x", "assistant": "y"}])
        ids = {r["session_id"] for r in S.list_sessions()}
        self.assertEqual(ids, {"a1", "b1"})

    def test_rows_expose_owner(self):
        """结果里带归属，前端/运维才能看出哪些会话还没归属。"""
        S.bind_code_root("a1", ROOT_A, PID_A)
        S.save("a1", [{"user": "x", "assistant": "y"}])
        row = [r for r in S.list_sessions() if r["session_id"] == "a1"][0]
        self.assertEqual(row["owner_project_id"], PID_A)

    def test_same_sid_is_still_one_session(self):
        """**乙的核心**：同 sid 跨项目仍是同一个会话，内容读得到。"""
        S.bind_code_root("s", ROOT_A, PID_A)
        S.save("s", [{"user": "qa", "assistant": "ra"}], project_id=PID_A)
        got = S.load("s", project_id=PID_B)       # 换项目读
        self.assertEqual([t["user"] for t in got["turns"]], ["qa"],
                         "跨项目读不到同一会话 —— 又成分桶了")
        self.assertEqual(got.get("owner_project_id"), PID_A)

    def test_write_without_pid_is_readable_with_pid(self):
        """**乙的判据（补）**：不带 project_id 写、带 project_id 读，必须读得到。

        为什么必须有这条：先前的判据全都走 `bind + save + load` 同一 pid。
        而 `save` → `_write` → `_bucket_dirs()[0]` 走的是**全局目录**，
        不经 `_path` —— 所以把 `_path` 改回"按 project_id 分桶"也照样绿
        （实测过：`_path` 真的改坏、14 条判据全绿）。这一条刻意分开 pid，
        并且下面补一条直接打 `_path` / `_write` 的判据，堵住同一个洞。

        这正是生产的真实形状：Agent 构造时带 pid，前端详情接口带 pid，
        而 `save` 有时不带。任何一处按 pid 分桶都会让两边对不上。
        """
        S.save("nopid", [{"user": "u1", "assistant": "a1"}])       # 不带 pid
        for pid in (PID_A, PID_B, None):
            got = S.load("nopid", project_id=pid)
            self.assertEqual([t["user"] for t in got["turns"]], ["u1"],
                             "不带 pid 写的会话，pid=%r 读不到 —— 分桶又来了" % pid)

    def test_path_helper_ignores_project_id(self):
        """直接打 `_path` —— 它是 `delete` 与备用读取的路径来源。

        ⚠ 上一版的洞：`save/load` 走 `_bucket_dirs()[0]`，**从不经过 `_path`**，
        所以把 `_path` 改回分桶、判据全绿（实测 14 条全过）。同型事故：
        **测了一条没人走的路径**（今晚 B 缺陷刚栽过一次）。这里直接打它。
        """
        a = S._path("abc", project_id=PID_A)
        b = S._path("abc", project_id=PID_B)
        c = S._path("abc")
        self.assertEqual(a, b, "换了 project_id，_path 就变了 —— 分桶回来了")
        self.assertEqual(a, c, "不传与传 project_id 的路径不同")
        self.assertEqual(pathlib.Path(a).parent, pathlib.Path(S.SESSIONS_DIR),
                         "_path 指向子目录 —— 会话又被按项目分开了")

    def test_write_lands_flat_even_with_pid(self):
        """`save`/`bind` 带 pid 也必须落全局扁平目录（不经 _path）。"""
        S.bind_code_root("w1", ROOT_A, PID_A)
        S.save("w1", [{"user": "u", "assistant": "a"}], project_id=PID_A)
        d = pathlib.Path(S.SESSIONS_DIR)
        self.assertTrue((d / "w1.json").is_file(), "没落在全局扁平目录")
        self.assertFalse((d / PID_A).exists(), "又建了项目子目录")

    def test_dedup_prefers_flat_over_legacy_bucket(self):
        """同一 sid 在全局与旧分桶都有时，全局优先（迁移后的唯一真源）。"""
        bdir = self.tmp / "sessions" / PID_A
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "dup.json").write_text(
            '{"session_id":"dup","updated_at":"2026-10-01T10:00:00",'
            '"turns":[{"user":"桶","assistant":"桶"}],"owner_project_id":"' + PID_A + '"}',
            encoding="utf-8")
        S.save("dup", [{"user": "全局", "assistant": "全局"}], project_id=PID_B)
        self.assertEqual([t["user"] for t in S.load("dup")["turns"]], ["全局"])
        rows = [r for r in S.list_sessions() if r["session_id"] == "dup"]
        self.assertEqual(len(rows), 1, "同一 sid 只出现一次")
        # 归属取最早绑定的那份（这里两条都有；全局那条 PID_B 更晚 -> 取 PID_A）
        self.assertEqual(rows[0]["owner_project_id"], PID_A)

    def test_merge_keeps_owner_when_only_the_bucket_has_it(self):
        """**合并必须保住 owner** —— 只有桶里那份有归属时尤其如此。

        这正是迁移期间的真实形状：`backfill_owner` 把归属写进全局那份，
        但若全局那份还没有归属字段（或比桶里那份旧），合并后也该从桶里
        那份继承过来。漏了这条，会话会在迁移中途"丢掉归属"，然后从项目
        列表里悄悄消失（`list_sessions(project_id=pid)` 按 owner 过滤）。

        ⚠ 第一版我用 `bind_code_root` 造全局那份 —— 它会写下 PID_A 且
        `code_root_at` 更早，于是"最早绑定胜出"合理地返回 PID_A。
        那不是我想验的形状。这里刻意让全局那份**只有 turns、没有 owner**
        （用 save 直接写），桶里那份才有 owner。
        """
        S.save("m1", [{"user": "全局旧", "assistant": "全局旧"}])   # 无 owner
        bdir = self.tmp / "sessions" / PID_B
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "m1.json").write_text(
            '{"session_id":"m1","updated_at":"2099-01-01T00:00:00",'
            '"owner_project_id":"' + PID_B + '",'
            '"turns":[{"user":"桶新","assistant":"桶新"}]}',
            encoding="utf-8")
        got = S.load("m1")
        self.assertEqual([t["user"] for t in got["turns"]], ["桶新"],
                         "turns 应取 updated_at 最新的那份")
        self.assertEqual(got.get("owner_project_id"), PID_B,
                         "合并把 owner 丢了 —— 该会话会从项目列表里消失")

    def test_merge_takes_the_earliest_owner(self):
        """两份都有 owner 时取**最早绑定的**，不是最新写的。

        ⚠ 必须让 `newest` 的那份 owner **不同**，否则 `dict(newest)` 恰好
        也给出正确答案，判据测不到继承逻辑（实测：三份 owner 相同时，
        删掉整段继承逻辑判据仍绿）。所以这里让最新那份带**另一个** pid。
        """
        S.bind_code_root("m2", ROOT_A, PID_A)          # 全局：code_root_at 最早
        S.save("m2", [{"user": "g", "assistant": "g"}])
        # 桶里那份更新，且 owner 是另一个项目
        bdir = self.tmp / "sessions" / PID_B
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "m2.json").write_text(
            '{"session_id":"m2","updated_at":"2099-01-01T00:00:00",'
            '"code_root_at":"2099-01-01T00:00:00",'
            '"turns":[{"user":"桶新","assistant":"桶新"}],'
            '"owner_project_id":"' + PID_B + '"}',
            encoding="utf-8")
        got = S.load("m2")
        self.assertEqual(got.get("owner_project_id"), PID_A,
                         "取了后来绑的那份 —— 会话会被另一个项目认领")
        # 而 turns 仍应是最新那份（两件事互不干扰）
        self.assertEqual([t["user"] for t in got["turns"]], ["桶新"])

    def test_merge_inherits_owner_from_the_only_record_that_has_one(self):
        """只有一份有 owner 时，合并结果必须继承它（不许被 newest 抹掉）。

        这条直接隔离变量：`newest`（updated_at 最新）那份**没有** owner，
        有 owner 的是另一份。若继承逻辑被删，`dict(newest)` 会让 owner 变空，
        会话从此在项目列表里消失。实测删掉继承段后这条会红。
        """
        S.save("inh", [{"user": "全局新", "assistant": "g"}])   # 最新、无 owner
        bdir = self.tmp / "sessions" / PID_A
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / "inh.json").write_text(
            '{"session_id":"inh","updated_at":"2026-10-01T10:00:00",'
            '"code_root_at":"2026-10-01T10:00:00",'
            '"turns":[{"user":"桶旧","assistant":"桶旧"}],'
            '"owner_project_id":"' + PID_A + '"}',
            encoding="utf-8")
        got = S.load("inh")
        self.assertEqual(got.get("owner_project_id"), PID_A,
                         "继承逻辑失效 —— owner 被 newest 抹成空，会话会从列表消失")
        self.assertEqual([t["user"] for t in got["turns"]], ["全局新"],
                         "turns 应仍取 updated_at 最新的那份")


if __name__ == "__main__":
    unittest.main(verbosity=2)
