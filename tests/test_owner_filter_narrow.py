# -*- coding: utf-8 -*-
"""丙方案豁免收窄：旧分桶的归属要从**目录名**补出来。

## 为什么要有这条

`list_sessions(project_id=pid)` 的过滤是「有归属且不是我要的才排除」。
旧分桶 `<SESSIONS_DIR>/<pid>/` 里的文件**没有** `owner_project_id`
字段（`bind_code_root` 是丙方案之后才写的），于是它们被当成
「无归属 → 放行」，把过滤彻底淹没 —— 实测选一个只有 4 个会话的项目，
列表里出现 336 条。

**归属其实就写在目录名里**，只是读的时候被丢掉了。

## 用户已拍板

`golden-*` 夹具（真·无归属，28 个）**不该**出现在任何项目的列表里。

## ⚠ 沙箱纪律（今晚踩过，代价很具体）

必须建在 `config.STATE_ROOT` 之下。用 `tempfile.mkdtemp` 会让
`SESSIONS_DIR` 落在别处，于是 `tests/test_state_root_isolation.py`
在**组合跑**时变红、**单独跑不红** —— 极难发现。
本项目其他 21 个会话类测试都照此办。

顺序：先设 `DOCMIND_STATE_DIR` 让 `STATE_ROOT` 落到沙箱，再 import sessions；
然后直接覆盖 `sessions.SESSIONS_DIR`（比依赖 import 时序更可靠）。
"""

import json
import os
import pathlib
import shutil
import sys
import unittest


def setUpModule():
    import config as C
    base = pathlib.Path(C.STATE_ROOT)
    box = base / ".tmp" / "owner_filter_narrow"
    if box.exists():
        shutil.rmtree(box, ignore_errors=True)
    box.mkdir(parents=True, exist_ok=True)
    import sessions as S
    S.SESSIONS_DIR = str(box / "sessions")
    os.makedirs(S.SESSIONS_DIR, exist_ok=True)


def tearDownModule():
    """还原 SESSIONS_DIR，避免污染同进程里的其他用例。"""
    import sessions as S
    if not hasattr(S, "_REAL_SESSIONS_DIR"):
        return
    S.SESSIONS_DIR = S._REAL_SESSIONS_DIR


import config as _C  # noqa: E402
import sessions as S  # noqa: E402

if not hasattr(S, "_REAL_SESSIONS_DIR"):
    S._REAL_SESSIONS_DIR = S.SESSIONS_DIR


class LegacyBucketOwnerTests(unittest.TestCase):
    """旧分桶的归属从目录名补出来；夹具不再淹没过滤。"""

    def setUp(self):
        self.S = S
        self.dir = pathlib.Path(S.SESSIONS_DIR)
        for p in self.dir.glob("*.json"):
            p.unlink()
        for d in self.dir.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)

    def _flat(self, sid, owner=None):
        data = {"session_id": sid, "updated_at": "2026-10-11T10:00:00",
                "turns": [{"user": "u", "assistant": "a"}]}
        if owner:
            data["owner_project_id"] = owner
        (self.dir / (sid + ".json")).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _bucket(self, pid, sid, owner=None):
        d = self.dir / pid
        d.mkdir(parents=True, exist_ok=True)
        data = {"session_id": sid, "updated_at": "2026-10-11T10:00:00",
                "turns": [{"user": "u", "assistant": "a"}]}
        if owner:
            data["owner_project_id"] = owner
        (d / (sid + ".json")).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _ids(self, pid=None):
        return [r["session_id"]
                for r in S.list_sessions(limit=1000, project_id=pid)]

    def _owner_of(self, sid, pid=None):
        for r in S.list_sessions(limit=1000, project_id=pid):
            if r["session_id"] == sid:
                return r.get("owner_project_id")
        return None

    # ------------------------------------------------ 核心判据

    def test_bucket_copy_without_owner_is_attributed_to_its_directory(self):
        """**核心**：桶里那份没有 owner 字段 -> 归属取自**目录名**。

        修法的机制不是「无归属就放行」，而是「归属本来就在目录名里，
        把它读出来」。
        """
        self._bucket("prj-B", "only-in-B")
        self.assertIn("only-in-B", self._ids("prj-B"),
                      "桶里那份没被算进 prj-B —— 归属没从目录名补出来")
        self.assertEqual(self._owner_of("only-in-B", "prj-B"), "prj-B",
                         "桶里那份的 owner 应取自目录名")

    def test_bucket_copy_does_not_leak_into_other_projects(self):
        """桶里的会话**不能**出现在别的项目列表里。

        这是收窄的全部意义：选一个只有 1 个会话的项目，列表就该只有它。
        """
        self._bucket("prj-A", "a-session")
        self._bucket("prj-B", "b-session")
        ids_b = self._ids("prj-B")
        self.assertIn("b-session", ids_b)
        self.assertNotIn("a-session", ids_b,
                         "prj-A 的会话漏进 prj-B —— 收窄没生效")

    def test_ownerless_fixture_is_not_listed_for_a_project(self):
        """**用户拍板**：无归属夹具不该出现在任何项目的列表里。

        这条**改的是前提**：`test_session_owner_project.py` 里
        `test_ownerless_legacy_sessions_are_still_listed` 说的是反的。
        改前提不是删判据 —— 两条都留着，才能看见前提变没变。
        """
        self._flat("golden-baseline-fixture")
        self.assertNotIn("golden-baseline-fixture", self._ids("prj-A"),
                         "无归属夹具仍出现在项目列表里 —— 豁免没收窄")

    def test_ownerless_fixture_still_visible_without_filter(self):
        """不传 project_id 时仍能看到 —— 数据不能因为过滤而消失。"""
        self._flat("golden-baseline-fixture")
        self.assertIn("golden-baseline-fixture", self._ids(),
                      "不传 pid 就该全都看得到")

    def test_field_beats_directory_name(self):
        """文件里的 `owner_project_id` **优先于**目录名。

        否则「归属被改过」的会话会被目录名拉回去 —— 那是回退，不是迁移。
        """
        self._bucket("prj-A", "moved-session", owner="prj-B")
        self.assertNotIn("moved-session", self._ids("prj-A"),
                         "字段说了属于 prj-B，就不该出现在 prj-A")
        self.assertIn("moved-session", self._ids("prj-B"))

    def test_read_through_still_works_for_migrated_history(self):
        """**B10 的本意**：迁移后仍能读到旧历史。

        旧历史现在**带着从目录名补出的归属**被读到 ——
        不再靠「无归属放行」蒙混过关。这条保证收窄没打断读穿透。
        """
        self._bucket("prj-A", "legacy-in-A")
        self.assertIn("legacy-in-A", self._ids("prj-A"),
                      "读穿透断了 —— 迁移后的旧历史读不到")

    def test_dedup_still_prefers_flat(self):
        """同一 sid 两处都有时，仍以扁平那份为准（既有语义不变）。"""
        self._flat("both", owner="prj-A")
        self._bucket("prj-B", "both")
        rows = [r for r in S.list_sessions(limit=1000)
                if r["session_id"] == "both"]
        self.assertEqual(len(rows), 1, "同一个 sid 出现了两次")
        self.assertEqual(rows[0]["owner_project_id"], "prj-A")


if __name__ == "__main__":
    unittest.main(verbosity=2)