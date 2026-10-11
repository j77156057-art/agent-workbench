# -*- coding: utf-8 -*-
"""P2 项目数据层 —— QA 独立对抗性验证（严过关，software-qa-engineer-2）。

与实现者自测（tests/test_projects.py）**独立**：本文件不复用其用例，而是
① 复现桌面安装的真实用户场景（旧扁平目录里的历史会话在迁移后必须仍可见/可读/可删）；
② 对抗性边界（归一化、幂等、显式优先、损坏/空/缺失目录、桶优先稳定性、误删、特性开关、导入副作用、api 默认路径）；
③ 反例：多项目下 current_project_id 被启动迁移无条件覆盖（真实 Bug）。

隔离保证
--------
- `config.STATE_FILE` 与 `sessions.SESSIONS_DIR` 在 setUp 内一律改指临时目录；
- 子进程用例额外注入 `DOCMIND_STATE_ROOT` / `DOCMIND_SESSIONS_DIR` 指向临时目录；
- 每个用例的 tearDown 断言仓库真实 `.docmind_state.json` / `.docmind_sessions/` 字节级不变，
  任何污染都会立刻暴露。绝不写仓库根。

运行：`.venv/Scripts/python.exe -B -m unittest tests.test_projects_qa -v`
"""
import asyncio  # noqa: F401  (供子进程脚本引用说明)
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import projects  # noqa: E402
import sessions  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_STATE = os.path.join(_REPO_ROOT, ".docmind_state.json")
_REPO_SESSIONS = os.path.join(_REPO_ROOT, ".docmind_sessions")

# 桌面安装的真实状态与历史会话（P2 迁移的真实背景）
_REAL_STATE = r"D:\WorkBuddy\DocMind\_internal\.docmind_state.json"
_REAL_LEGACY = r"D:\WorkBuddy\DocMind\_internal\.docmind_sessions\web-118ac75087ff.json"


def _snapshot_repo():
    st = None
    if os.path.isfile(_REPO_STATE):
        with open(_REPO_STATE, "rb") as f:
            st = f.read()
    sess = sorted(os.listdir(_REPO_SESSIONS)) if os.path.isdir(_REPO_SESSIONS) else None
    return st, sess


def _real_legacy_bytes():
    if os.path.isfile(_REAL_LEGACY):
        with open(_REAL_LEGACY, "rb") as f:
            return f.read()
    return None


class QABase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_qa_")
        self._old_state = config.STATE_FILE
        self._old_sessions = sessions.SESSIONS_DIR
        self._old_runtime = dict(config._RUNTIME)
        config.STATE_FILE = os.path.join(self.tmp, ".docmind_state.json")
        sessions.SESSIONS_DIR = os.path.join(self.tmp, "sessions")
        self._env = mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "1"})
        self._env.start()
        self._repo_before = _snapshot_repo()

    def tearDown(self):
        self._env.stop()
        config.STATE_FILE = self._old_state
        sessions.SESSIONS_DIR = self._old_sessions
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        # 污染哨兵：仓库真实状态必须字节级不变
        self.assertEqual(_snapshot_repo(), self._repo_before,
                         "用例污染了仓库真实 .docmind_state.json/.docmind_sessions")
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def _mkdir(self, name):
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d

    def _write_state(self, payload):
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _flat_path(self, sid):
        return os.path.join(sessions.SESSIONS_DIR, sessions._slug(sid) + ".json")

    def _write_flat(self, sid, turns, summary="", updated_at="2024-01-01T00:00:00"):
        os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
        p = self._flat_path(sid)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"session_id": sid, "updated_at": updated_at,
                       "summary": summary, "turns": turns}, f, ensure_ascii=False)
        return p

    def _seed_current_project(self):
        d = self._mkdir("CurProj")
        pid = projects.ensure_project(d)
        self.assertTrue(projects.set_current(pid))
        return d, pid


# =====================================================================
# A. 复现用户真实场景（最高优先级）
# =====================================================================
class A_UserScenarioReadThrough(QABase):
    """真实桌面：STATE_FILE 有 code_root、历史会话在旧扁平目录。迁移后不得丢历史。"""

    def test_legacy_history_survives_migration_and_is_manageable(self):
        # ① 旧扁平目录里放一条会话（优先使用真实安装的原始文件，缺失则合成）
        raw_bytes = _real_legacy_bytes()
        if raw_bytes is not None:
            os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
            legacy_sid = os.path.splitext(os.path.basename(_REAL_LEGACY))[0]
            flat = self._flat_path(legacy_sid)
            with open(flat, "wb") as f:
                f.write(raw_bytes)
            src = "真实桌面文件副本"
        else:
            legacy_sid = "web-118ac75087ff"
            flat = self._write_flat(legacy_sid, [{"user": "q", "assistant": "a"}])
            src = "合成"
        turns_before = len(sessions._read_raw(flat).get("turns") or [])
        self.assertGreaterEqual(turns_before, 1, "前置：旧会话必须含 turns")

        # ② STATE_FILE 写 code_root=<真实存在目录>（真实背景为 D:\WorkBuddy\godot_sample）
        proj_root = None
        if os.path.isfile(_REAL_STATE):
            try:
                with open(_REAL_STATE, encoding="utf-8") as f:
                    rc = json.load(f).get("code_root")
                if isinstance(rc, str) and os.path.isdir(rc):
                    proj_root = rc  # 只读其路径字符串做 isdir/hash，不写该目录
            except (OSError, ValueError):
                proj_root = None
        if proj_root is None:
            proj_root = self._mkdir("godot_sample")
        self._write_state({"code_root": proj_root})
        config._RUNTIME.pop("code_root", None)

        # ③ 调用迁移（前置条件：注册块不依赖 runtime，只要 code_root 是存在目录即登记）
        config._apply_persisted_state()

        pid = projects.project_id(proj_root)
        # 断言 1：current_project_id 指向该目录派生 pid
        self.assertEqual(projects.current_project_id(), pid,
                         "迁移后当前项目应指向 code_root 派生的 pid")
        # 断言 2：list_sessions 仍能列出旧会话，且不重复
        ids = [x["session_id"] for x in sessions.list_sessions()]
        self.assertIn(legacy_sid, ids, "旧扁平目录的历史会话迁移后必须仍可见（否则复现原始 Bug）")
        self.assertEqual(len(ids), len(set(ids)), "不得重复")
        # 断言 3：load 能取回 turns
        data = sessions.load(legacy_sid)
        self.assertEqual(len(data.get("turns") or []), turns_before)
        self.assertTrue(sessions.history(legacy_sid))
        # 断言 4：delete 真删旧扁平目录文件
        self.assertTrue(os.path.isfile(flat))
        self.assertTrue(sessions.delete(legacy_sid))
        self.assertFalse(os.path.isfile(flat), "删除必须同时清掉旧扁平目录文件")

        print(f"[A] code_root={proj_root!r} ({src}) pid={pid} legacy_sid={legacy_sid} "
              f"turns={turns_before} listed={len(ids)} -> 旧会话仍可见=True")


# =====================================================================
# B. 对抗性检查
# =====================================================================
class B1_ProjectIdNormalization(QABase):
    def test_drive_case_and_slash_variants_share_id(self):
        a = self._mkdir("Gamma")
        base = projects.project_id(a)
        self.assertEqual(projects.project_id(a.replace(os.sep, "/")), base, "斜杠方向应归一")
        if os.name == "nt":
            self.assertEqual(projects.project_id(a.upper()), base, "盘符/路径大小写应归一(normcase)")
            self.assertEqual(projects.project_id(a.lower()), base)
            self.assertEqual(projects.project_id(a + os.sep), base, "尾部斜杠应归一(abspath)")
        self.assertNotEqual(projects.project_id(self._mkdir("Gamma2")), base, "不同目录须不同 id")

    def test_dotdot_is_canonicalized(self):
        base = self._mkdir("Base")
        sub = self._mkdir("Base/sub")
        self.assertEqual(projects.project_id(os.path.join(sub, "..")), projects.project_id(base),
                         "`..` 应被 abspath 规范化")

    def test_exotic_paths_documented(self):
        """`\\\\?\\` 前缀 / UNC：只记录结论（不要求修）。"""
        a = self._mkdir("Exotic")
        norm = projects.project_id(a)
        results = {}
        if os.name == "nt":
            prefix = projects.project_id("\\\\?\\" + a)
            unc = projects.project_id("\\\\server\\share\\proj")
            results["extended_prefix_same"] = (prefix == norm)
            results["unc_has_id"] = unc.startswith("prj-") and len(unc) == 16
            print(f"[B] extended(\\\\?\\)前缀同id={prefix == norm}（通常 False）；"
                  f"UNC 独立 id={unc}")
        self.assertTrue(norm.startswith("prj-") and len(norm) == 16)
        # 仅断言不抛异常且形态正确；前缀/UNC 差异作为已知结论报告


class B2_MigrationIdempotent(QABase):
    def test_three_calls_registry_and_current_stable(self):
        d = self._mkdir("Idem")
        self._write_state({"code_root": d})
        config._RUNTIME.pop("code_root", None)
        for _ in range(3):
            config._apply_persisted_state()
        rows = projects.list_projects()
        self.assertEqual(len(rows), 1, "重复迁移不得重复登记")
        pid = projects.project_id(d)
        self.assertEqual(rows[0]["project_id"], pid)
        self.assertEqual(projects.current_project_id(), pid, "current 应稳定")

    def test_migration_with_existing_registry_no_growth(self):
        a, b = self._mkdir("P1"), self._mkdir("P2")
        projects.ensure_project(a)
        pidb = projects.ensure_project(b)
        projects.set_current(pidb)
        self._write_state({**config.load_state("x", {}),
                           "code_root": a,
                           "projects": {r["project_id"]: r for r in projects.list_projects()},
                           "current_project_id": pidb})
        # 直接改 STATE_FILE 后再读
        with open(config.STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        for _ in range(3):
            config._apply_persisted_state()
        self.assertEqual(len(projects.list_projects()), 2, "既有 2 项目不得增长")
        print(f"[B] 幂等：projects={len(projects.list_projects())} "
              f"current={projects.current_project_id()}")

    def test_missing_or_corrupt_state_no_raise(self):
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()  # STATE_FILE 不存在
        self.assertEqual(projects.list_projects(), [])
        with open(config.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        config._apply_persisted_state()  # 损坏 JSON
        self.assertEqual(projects.list_projects(), [])

    def test_not_registered_when_code_root_not_dir(self):
        self._write_state({"code_root": os.path.join(self.tmp, "does_not_exist")})
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()
        self.assertEqual(projects.list_projects(), [], "不存在的 code_root 不应登记")

    def test_no_registration_when_flag_off(self):
        d = self._mkdir("FlagOff")
        self._write_state({"code_root": d})
        config._RUNTIME.pop("code_root", None)
        with mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "0"}):
            config._apply_persisted_state()
        self.assertEqual(projects.list_projects(), [], "开关关闭时不得迁移登记")


class B3_ExplicitPriority(QABase):
    """显式优先铁律：set_runtime('code_root', X) 不被持久化值 Y 覆盖（历史翻车点）。"""

    def test_explicit_code_root_wins(self):
        x, y = self._mkdir("ExplicitX"), self._mkdir("FromStateY")
        self._write_state({"code_root": y})
        config.set_runtime("code_root", x)
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("code_root"), x,
                         "显式 code_root 必须优先于持久化值")
        print(f"[B] 铁律 OK：显式 code_root={config.get_runtime('code_root')!r} 未被持久化值覆盖")


class B6_CurrentProjectClobber(QABase):
    """迁移覆盖回归守卫（第 1 轮曾是真 Bug，第 2 轮已修：迁移条件化）。

    首轮 config._apply_persisted_state 的迁移块对 code_root **无条件** ensure_project +
    set_current，覆盖用户持久化的 current_project_id。修复后仅当注册表为空才迁移，
    本用例断言「已有注册表时 current 保持用户选择」——防止回归。
    """

    def test_current_project_not_clobbered_on_restart(self):
        a, b = self._mkdir("ProjA"), self._mkdir("ProjB")
        pida = projects.ensure_project(a)
        pidb = projects.ensure_project(b)
        self.assertTrue(projects.set_current(pidb), "用户切到 B")
        # P2 不更新 code_root，重启时 STATE_FILE 里 code_root 仍可能指向 A
        with open(config.STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st["code_root"] = a
        self._write_state(st)
        config._RUNTIME.pop("code_root", None)

        config._apply_persisted_state()  # 模拟 lifespan 重启迁移

        self.assertEqual(projects.current_project_id(), pidb,
                         "重启后 current_project_id 应保持用户选择的 B，而非被 code_root(A) 覆盖")
        print(f"[B][guard] pidA={pida} pidB={pidb} "
              f"-> 迁移后 current={projects.current_project_id()} (==pidB)")


class B4_ListSessionsRobust(QABase):
    def setUp(self):
        super().setUp()
        self._seed_current_project()

    def test_missing_legacy_dir(self):
        self.assertFalse(os.path.isdir(sessions.SESSIONS_DIR))
        self.assertEqual(sessions.list_sessions(), [])
        self.assertIsNone(sessions._read_raw(self._flat_path("x")))

    def test_empty_legacy_dir(self):
        os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
        self.assertEqual(sessions.list_sessions(), [])

    def test_corrupt_json_skipped(self):
        os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
        with open(self._flat_path("broken"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        sessions.save("good", [{"user": "q", "assistant": "a"}])
        ids = [x["session_id"] for x in sessions.list_sessions()]
        self.assertIn("good", ids)
        self.assertNotIn("broken", ids, "损坏 JSON 应被跳过而非抛错")

    def test_non_dict_json_skipped(self):
        os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
        with open(self._flat_path("arr"), "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(sessions.list_sessions(), [])

    def test_load_corrupt_returns_empty_struct(self):
        os.makedirs(sessions.SESSIONS_DIR, exist_ok=True)
        with open(self._flat_path("broken"), "w", encoding="utf-8") as f:
            f.write("!!!")
        d = sessions.load("broken")
        self.assertEqual(d.get("turns"), [])
        self.assertEqual(d.get("summary"), "")


class B5_BucketPriorityStable(QABase):
    def setUp(self):
        super().setUp()
        self._seed_current_project()

    def test_bucket_wins_and_order_stable(self):
        self._write_flat("dup-1", [{"user": "legacy-q", "assistant": "legacy-a"}],
                         updated_at="2099-01-01T00:00:00")
        sessions.save("dup-1", [{"user": "bucket-q", "assistant": "bucket-a"}])
        sessions.save("other", [{"user": "o", "assistant": "o"}])

        self.assertEqual(sessions.load("dup-1")["turns"][0]["user"], "bucket-q",
                         "同 sid 桶内存在时桶优先")
        seqs = [tuple(x["session_id"] for x in sessions.list_sessions()) for _ in range(3)]
        self.assertEqual(seqs[0], seqs[1], "重复调用顺序须一致（稳定）")
        self.assertEqual(seqs[1], seqs[2])
        rows = [x for x in sessions.list_sessions() if x["session_id"] == "dup-1"]
        self.assertEqual(len(rows), 1, "同 sid 只出现一次")

    def test_global_file_is_the_only_record(self):
        """会话只有**一份**（全局扁平），不存在「桶内 turns 为空要回退」的场景。

        旧判据的前提是分桶：桶里一份、扁平目录另有一份。乙方案后只剩一份，
        这条改为钉住新契约 —— 全局文件里 turns 为空就是空，不许从别处拼出来。
        """
        self._write_flat("fb-1", [])
        self.assertEqual(sessions.load("fb-1")["turns"], [],
                         "空会话凭空多出了轮次")
        self._write_flat("fb-2", [{"user": "legacy-q", "assistant": "legacy-a"}])
        self.assertEqual(sessions.load("fb-2")["turns"][0]["user"], "legacy-q")


class B7_RemoveProjectNoDiskDelete(QABase):
    def test_remove_project_keeps_sessions_on_disk(self):
        """注销项目只改注册表，绝不动磁盘上的会话文件。

        旧断言「桶文件仍在」已改为「全局会话文件仍在」—— 会话不再按项目分桶，
        但「注销不删会话」这个承诺本身不变。
        """
        _, pid = self._seed_current_project()
        sessions.save("keep", [{"user": "u", "assistant": "a"}], project_id=pid)
        legacy = self._write_flat("keep-old", [{"user": "o", "assistant": "o"}])
        global_file = os.path.join(sessions.SESSIONS_DIR,
                                   sessions._slug("keep") + ".json")

        self.assertTrue(projects.remove_project(pid))
        self.assertIsNone(projects.get_project(pid))
        self.assertTrue(os.path.isfile(global_file),
                        "注销不得删除磁盘上的会话文件")
        self.assertTrue(os.path.isfile(legacy), "注销不得删除旧扁平目录文件")
        self.assertFalse(projects.remove_project(pid), "重复注销应返回 False")
        # 注销当前项目后 current 清空（并观察 runtime 残留）
        self.assertEqual(config.load_state("current_project_id", ""), "")
        print(f"[B] remove_project 后 会话文件存在="
              f"{os.path.isfile(global_file)} "
              f"runtime_project_id={config.get_runtime('project_id')!r}")


class B8_FlagOffParity(QABase):
    def test_flag_off_uses_flat_only(self):
        d = self._mkdir("FlagP")
        pid = projects.ensure_project(d)
        projects.set_current(pid)
        with mock.patch.dict(os.environ, {"DOCMIND_PROJECTS": "0"}):
            self.assertFalse(projects.enabled())
            self.assertEqual(projects.current_project_id(), projects.LEGACY_ID)
            self.assertEqual(projects.code_collection(), config.CODE_COLLECTION_NAME)
            self.assertEqual(projects.code_collection(pid), config.CODE_COLLECTION_NAME)
            # 写 / 读 / 列 / 删 全走旧扁平目录
            self.assertTrue(sessions.save("leg", [{"user": "x", "assistant": "y"}]))
            flat = self._flat_path("leg")
            self.assertTrue(os.path.isfile(flat), "开关关闭应写旧扁平目录")
            self.assertFalse(os.path.isdir(os.path.join(sessions.SESSIONS_DIR, sessions._slug(pid))),
                             "开关关闭不得创建项目子目录")
            self.assertEqual([x["session_id"] for x in sessions.list_sessions()], ["leg"])
            self.assertEqual(len(sessions.load("leg")["turns"]), 1)
            self.assertTrue(sessions.delete("leg"))
            self.assertFalse(os.path.isfile(flat))
        # 恢复开关回到项目语义
        self.assertEqual(projects.code_collection(), "docmind_code__" + pid)


class B9_ImportSideEffects(QABase):
    def test_import_projects_has_no_disk_side_effect(self):
        root = tempfile.mkdtemp(prefix="dm_qa_imp_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        env = dict(os.environ)
        env["DOCMIND_STATE_ROOT"] = root
        env["DOCMIND_SESSIONS_DIR"] = os.path.join(root, "sessions")
        code = (
            "import os, sys;"
            "import projects;"
            "sf = os.path.join(sys.argv[1], '.docmind_state.json');"
            "assert not os.path.exists(sf), 'import projects 创建了 STATE_FILE';"
            "assert not os.path.isdir(sys.argv[2]), 'import projects 创建了 sessions 目录';"
            "print('OK')"
        )
        proc = subprocess.run([sys.executable, "-B", "-c", code, root, env["DOCMIND_SESSIONS_DIR"]],
                              cwd=_REPO_ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)


class B10_ApiDefaultPath(QABase):
    """api.py 未改：/api/sessions（api.py:1604 只传 limit，不传 project_id）的默认行为。

    无当前项目时 == 改动前（只读旧扁平目录）；迁移后（有当前项目）仍经读穿透看到旧历史。
    """

    _SCRIPT = r'''
import asyncio, json, os, sys
import api, config, sessions as S, projects
def ids():
    r = asyncio.run(api.sessions_ep(50))
    return [x["session_id"] for x in r.get("items", [])]
out = {}
out["before"] = ids()            # import 后、未跑 lifespan 迁移：无当前项目
config._apply_persisted_state()  # 模拟 lifespan 迁移
out["after"] = ids()
out["current"] = projects.current_project_id()
print("JSON:" + json.dumps(out))
'''

    def test_endpoint_default_and_after_migration(self):
        root = self._mkdir("ApiRoot")
        sd = os.path.join(root, "sessions")
        os.makedirs(sd, exist_ok=True)
        legacy_sid = "web-118ac75087ff"
        raw_bytes = _real_legacy_bytes()
        proj_root = self._mkdir("ApiProj")
        # ⚠ 桶目录名**必须等于**迁移后的当前项目 id —— 见类 docstring。
        #
        # `api.sessions_ep` 走 `_request_project_id()` 拿当前项目，
        # 归属过滤要拿它跟会话的 owner 比。归属现在有两个来源：
        # 文件里的 `owner_project_id`，或（缺失时）**桶目录名**。
        # 所以这里先按 `proj_root` 算出项目 id，再用它命名桶目录 ——
        # 不能随手写个名字，那样过滤会把这条历史滤掉（第一版就栽在这）。
        bucket_pid = projects.project_id(proj_root)
        legacy_bucket = os.path.join(sd, bucket_pid)
        os.makedirs(legacy_bucket, exist_ok=True)
        with open(os.path.join(legacy_bucket, legacy_sid + ".json"), "wb") as f:
            f.write(raw_bytes if raw_bytes is not None
                    else json.dumps({"session_id": legacy_sid, "turns": [{"user": "q", "assistant": "a"}],
                                     "summary": "", "updated_at": "2024-01-01"}).encode("utf-8"))
        with open(os.path.join(root, ".docmind_state.json"), "w", encoding="utf-8") as f:
            json.dump({"code_root": proj_root}, f, ensure_ascii=False)

        env = dict(os.environ)
        env["DOCMIND_STATE_ROOT"] = root
        env["DOCMIND_SESSIONS_DIR"] = sd
        env["DOCMIND_PROJECTS"] = "1"
        proc = subprocess.run([sys.executable, "-B", "-c", self._SCRIPT],
                              cwd=_REPO_ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [ln for ln in proc.stdout.splitlines() if ln.startswith("JSON:")][-1]
        data = json.loads(line[5:])

        self.assertIn(legacy_sid, data["before"],
                      "无当前项目时 /api/sessions 应与改动前一致（读到旧扁平会话）")
        self.assertIn(legacy_sid, data["after"],
                      "迁移后有当前项目，读穿透仍须看到旧会话")
        self.assertEqual(data["current"], projects.project_id(proj_root))
        print(f"[B] api /api/sessions before={data['before']} after={data['after']} "
              f"current={data['current']}")


class B10b_OwnerlessStillReachable(QABase):
    """反向守卫：**无归属的老会话没有消失** —— 收窄不许把它砍掉。

    ## 为什么需要这条

    B10 的前提在 2026-10-11 改了：读穿透改用**旧桶目录**里的老会话。
    因为「无归属一律放行」那条豁免把 311 个旧副本一起放行了，
    把归属过滤淹没（实测：只有 4 个会话的项目，列表里出现 336 条）。

    但那条豁免**同时**在保护一类真实数据：**扁平目录里、无归属的存量老会话**
    —— 那可能是真实用户历史，不只是夹具。收窄不该把它们从「拿得到」
    变成「拿不到」。

    所以这里钉住收窄的**下界**：不传 `project_id` 时仍全部可见；
    按 id 定点读也仍拿得到。丢的只是「按项目过滤的列表视图」，不是数据。

    ⚠ 沙箱在 STATE_ROOT 之下（`QABase._mkdir` 就是这么做的）。
    """

    def test_ownerless_legacy_session_is_still_readable(self):
        """扁平目录里无 `owner_project_id` 的老会话，不传 pid 仍可见。"""
        root = self._mkdir("B10bRoot")
        sd = os.path.join(root, "sessions")
        os.makedirs(sd, exist_ok=True)
        sid = "web-ownerless-legacy"
        with open(os.path.join(sd, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"session_id": sid,
                       "turns": [{"user": "q", "assistant": "a"}],
                       "summary": "", "updated_at": "2024-01-01"}, f)

        env = dict(os.environ)
        env["DOCMIND_STATE_ROOT"] = root
        env["DOCMIND_SESSIONS_DIR"] = sd
        env["DOCMIND_PROJECTS"] = "1"
        script = "\n".join([
            "import json, sys",
            "import sessions as S",
            "sid = %r" % sid,
            "rows = S.list_sessions(limit=1000)",
            "print('JSON:' + json.dumps([r['session_id'] for r in rows]))",
            "print('LOAD:' + json.dumps(bool(S.load(sid))))",
        ])
        proc = subprocess.run([sys.executable, "-B", "-c", script],
                              cwd=_REPO_ROOT, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        ids = json.loads([ln for ln in proc.stdout.splitlines()
                          if ln.startswith("JSON:")][-1][5:])
        self.assertIn(sid, ids,
                      "无归属的老会话在**不传 pid** 时也看不到了 —— "
                      "收窄把真实用户数据砍掉了")

        loaded = json.loads([ln for ln in proc.stdout.splitlines()
                             if ln.startswith("LOAD:")][-1][5:])
        self.assertTrue(loaded,
                        "无归属的老会话按 id 也读不到了 —— 历史真的消失了")

    def test_ownerless_session_is_excluded_from_project_view_only(self):
        """收窄的**边界**：只在「按项目过滤的列表」里排除，数据本身不动。

        与 B10 是同一个道理的两个方向：B10 说「读穿透要读得到」，
        这条说「过滤要滤得掉」。两条都在，收窄才不是乱砍。
        """
        root = self._mkdir("B10bFilter")
        sd = os.path.join(root, "sessions")
        os.makedirs(sd, exist_ok=True)
        sid = "web-ownerless-for-filter"
        with open(os.path.join(sd, sid + ".json"), "w", encoding="utf-8") as f:
            json.dump({"session_id": sid,
                       "turns": [{"user": "q", "assistant": "a"}],
                       "summary": "", "updated_at": "2024-01-01"}, f)

        env = dict(os.environ)
        env["DOCMIND_STATE_ROOT"] = root
        env["DOCMIND_SESSIONS_DIR"] = sd
        env["DOCMIND_PROJECTS"] = "1"
        script = "\n".join([
            "import json",
            "import sessions as S",
            "a = [r['session_id'] for r in S.list_sessions(limit=1000,"
            " project_id='prj-A')]",
            "print('FILTER:' + json.dumps(a))",
        ])
        proc = subprocess.run([sys.executable, "-B", "-c", script],
                              cwd=_REPO_ROOT, env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        got = json.loads([ln for ln in proc.stdout.splitlines()
                          if ln.startswith("FILTER:")][-1][7:])
        self.assertNotIn(sid, got,
                         "无归属会话仍出现在项目列表里 —— 收窄没生效")


# =====================================================================
# 第 2 轮：验证「迁移条件化」修复本身（首次仍迁移 / 不再覆盖 / 边界 / remove 一致性）
# =====================================================================
class R2_MigrationConditional(QABase):
    def _reg_row(self, root, last_opened):
        return {"root": root, "name": os.path.basename(os.path.normpath(root)),
                "created_at": last_opened, "last_opened": last_opened}

    def test_req1_first_upgrade_still_migrates(self):
        """STATE_FILE 只有 code_root（无 projects 键）= 老用户首次升级 → 仍须迁移。"""
        d = self._mkdir("FirstUpgrade")
        self._write_state({"code_root": d})
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()
        pid = projects.project_id(d)
        self.assertEqual(len(projects.list_projects()), 1, "首次升级必须登记项目")
        self.assertEqual(projects.current_project_id(), pid, "首次升级必须设为当前")
        with open(config.STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st.get("current_project_id"), pid)
        print(f"[R2-1] 首次迁移 OK pid={pid}")

    def test_req2_existing_registry_not_overwritten(self):
        """{code_root:A, projects:{A,B}, current_project_id:B} → 迁移后 current 仍 B。"""
        a, b = self._mkdir("RcA"), self._mkdir("RcB")
        pida, pidb = projects.project_id(a), projects.project_id(b)
        config.save_state("projects", {
            pida: self._reg_row(a, "2024-01-01T00:00:00"),
            pidb: self._reg_row(b, "2025-01-01T00:00:00"),
        })
        config.save_state("current_project_id", pidb)
        self._write_state({"code_root": a, "projects": config.load_state("projects", {}),
                           "current_project_id": pidb})
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()
        self.assertEqual(projects.current_project_id(), pidb, "不得被 code_root(A) 覆盖")
        self.assertEqual(len(projects.list_projects()), 2, "不得新增/删除项目")
        print(f"[R2-2] 不再覆盖 OK current={projects.current_project_id()} (pidb={pidb})")

    def test_req3_missing_current_falls_back_to_latest_last_opened(self):
        """{code_root:A, projects:{A,B}} 无 current → 取 last_opened 最新者(B)，不得选 A。"""
        a, b = self._mkdir("FlA"), self._mkdir("FlB")
        pida, pidb = projects.project_id(a), projects.project_id(b)
        self._write_state({"code_root": a, "projects": {
            pida: self._reg_row(a, "2024-01-01T00:00:00"),
            pidb: self._reg_row(b, "2030-12-31T00:00:00"),
        }})
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()
        cur = projects.current_project_id()
        self.assertEqual(cur, pidb, "无 current 时应取 last_opened 最新者(B)")
        self.assertNotEqual(cur, pida, "不得因 code_root=A 就选 A")
        print(f"[R2-3] 兜底 OK current={cur} (pidb={pidb}, pida={pida})")

    def test_req4_explicit_code_root_still_wins(self):
        x, y = self._mkdir("ExX"), self._mkdir("ExY")
        self._write_state({"code_root": y})
        config.set_runtime("code_root", x)
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("code_root"), x, "显式优先铁律不得回退")
        print(f"[R2-4] 显式优先 OK code_root={config.get_runtime('code_root')!r}")

    def test_req6_empty_projects_dict_treated_as_first_migration(self):
        d = self._mkdir("EmptyReg")
        self._write_state({"code_root": d, "projects": {}})
        config._RUNTIME.pop("code_root", None)
        config._apply_persisted_state()
        self.assertEqual(len(projects.list_projects()), 1, "空 dict 应视作「为空」走首次迁移")
        self.assertEqual(projects.current_project_id(), projects.project_id(d))

    def test_req6_corrupt_projects_type_no_raise(self):
        for bad in (["x", "y"], "not-a-dict", 123, None):
            with self.subTest(bad=bad):
                d = self._mkdir("Corrupt_" + str(bad)[:6].replace("/", "_"))
                self._write_state({"code_root": d, "projects": bad})
                config._RUNTIME.pop("code_root", None)
                try:
                    config._apply_persisted_state()
                except Exception as e:  # noqa: BLE001
                    self.fail(f"损坏 projects 类型 {bad!r} 抛了异常：{e!r}")
        print("[R2-6] 损坏 projects 类型不抛异常 OK")


class R2_RemoveProjectConsistency(QABase):
    def _seed(self, root, last_opened):
        return {"root": root, "name": os.path.basename(os.path.normpath(root)),
                "created_at": last_opened, "last_opened": last_opened}

    def test_delete_current_falls_back_to_latest_remaining(self):
        a, b, c = self._mkdir("Da"), self._mkdir("Db"), self._mkdir("Dc")
        pida, pidb, pidc = (projects.project_id(x) for x in (a, b, c))
        config.save_state("projects", {
            pida: self._seed(a, "2030-01-01T00:00:00"),
            pidb: self._seed(b, "2025-01-01T00:00:00"),  # 当前
            pidc: self._seed(c, "2020-01-01T00:00:00"),
        })
        config.save_state("current_project_id", pidb)
        config.set_runtime("project_id", pidb)

        self.assertTrue(projects.remove_project(pidb))
        self.assertEqual(config.load_state("current_project_id", ""), pida,
                         "删除当前项目应落到剩余 last_opened 最新者(A)")
        self.assertEqual(config.get_runtime("project_id"), pida,
                         "runtime.project_id 应同步为 A，不得残留已删的 B")
        print(f"[R2-5] 删当前→回落 OK current={config.get_runtime('project_id')}")

    def test_delete_current_to_empty_clears_runtime_key(self):
        a = self._mkdir("Only")
        pida = projects.project_id(a)
        config.save_state("projects", {pida: self._seed(a, "2030-01-01T00:00:00")})
        config.save_state("current_project_id", pida)
        config.set_runtime("project_id", pida)

        self.assertTrue(projects.remove_project(pida))
        self.assertEqual(config.load_state("current_project_id", ""), "")
        self.assertNotIn("project_id", config._RUNTIME, "删到空 runtime 键应被清除而非残留")
        self.assertEqual(projects.list_projects(), [])
        print(f"[R2-5] 删到空 OK runtime keys={[k for k in config._RUNTIME if k == 'project_id']}")

    def test_delete_noncurrent_keeps_current(self):
        a, b = self._mkdir("Ka"), self._mkdir("Kb")
        pida, pidb = projects.project_id(a), projects.project_id(b)
        config.save_state("projects", {
            pida: self._seed(a, "2020-01-01T00:00:00"),
            pidb: self._seed(b, "2030-01-01T00:00:00"),
        })
        config.save_state("current_project_id", pidb)
        config.set_runtime("project_id", pidb)

        self.assertTrue(projects.remove_project(pida))
        self.assertEqual(config.load_state("current_project_id", ""), pidb, "删非当前项目不得动 current")
        self.assertEqual(config.get_runtime("project_id"), pidb)
        self.assertEqual(projects.current_project_id(), pidb)


if __name__ == "__main__":
    unittest.main()
