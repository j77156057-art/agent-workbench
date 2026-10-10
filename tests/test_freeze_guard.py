# -*- coding: utf-8 -*-
"""freeze_guard：跑测期间改被测文件，必须让这一轮失败。

## 为什么它值得一套判据

实测教训（2026-10-10~11）：守卫写好了、验过了，但**要人记得手动 `-p` 加载**，
于是我两次「全量跑到一半改 `config.py`/`api.py`」时它一声没响 ——
一份被污染的测量结果看起来完全合格。详见 `docs/measure-freeze-discipline.md`。

两件事必须被钉住：
1. **抓到污染就 fail**，不只是打印（silent green 比响一声危险得多）；
2. **没污染时零误报** —— 否则它会被人们想方设法关掉。

## ⚠ 两个测试设计上的坑（都踩过）

1. **守卫只跟踪「已 import 的模块」**。第一版探针只 `import pathlib`，
   从没 import victim.py，于是改它完全隐形、判据全绿。护的既然是"被测代码"，
   探针就必须真把它 import 进来。
2. **pytest 的 rootdir 由参数路径决定**。探针放在系统临时目录时 rootdir 变成
   那里，`ROOT/conftest.py` 不会加载，"默认加载"就测不到。所以探针必须放在
   **ROOT 之下**（用 `.tmp/` 子目录），并让 rootdir 落回 ROOT。
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD = "pytest_freeze_guard"
PROBE_ROOT = ROOT / ".tmp" / "fg_probe"


def _new_case(name):
    d = PROBE_ROOT / name
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_probe(d, probe_body, *, import_victim=True):
    """写探针：一个三用例的小套件，中间那条按 probe_body 行动。

    ⚠ `probe_body` 只写**方法体**（缩进 8 空格），不要带 `def` 行 ——
    `_write_probe` 自己会补。第一版把它写全了，结果生成两个 `def` 行、
    语法错、收集失败，判据看起来全红。空 body 也要补 `pass`。
    """
    head = [
        "import pathlib, sys, unittest",
        "HERE = pathlib.Path(__file__).resolve().parent",
        "sys.path.insert(0, str(HERE))",
    ]
    if import_victim:
        head.append("import victim          # 必须真 import，守卫只跟踪已加载的模块")
    head += [
        "TARGET = HERE / 'victim.py'",
        "",
        "",
        "class ProbeTests(unittest.TestCase):",
        "    def test_a_before(self):",
        "        assert TARGET.exists()",
        "",
        "    def test_b_middle(self):",
    ]
    body = (probe_body or "").strip("\n")
    if not body.strip():
        body = "        pass"
    tail = "    def test_c_after(self):\n        assert True\n"
    (d / "probe_test.py").write_text(
        "\n".join(head) + "\n" + body + "\n" + tail, encoding="utf-8")
    (d / "victim.py").write_text("ORIGINAL = 1\n", encoding="utf-8")


def _run(d, *, env_extra=None, extra_args=(), cwd=ROOT, rel=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "tools")
    env["PYTHONIOENCODING"] = "utf-8"
    env["DOCMIND_FREEZE_VERBOSE"] = "1"
    env["DOCMIND_FREEZE_REPORT"] = str(d / "report.json")
    if env_extra:
        env.update(env_extra)
    target = rel if rel else str(d / "probe_test.py")
    r = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "pytest", target, "-q",
         "-p", "no:cacheprovider", "--no-header", "--tb=no",
         "-p", GUARD, *extra_args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(cwd), env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _rm(d):
    shutil.rmtree(d, ignore_errors=True)


MODIFY = ("        t = TARGET.read_text(encoding='utf-8')\n"
          "        TARGET.write_text(t + '\\n# 污染\\n', encoding='utf-8')\n")


class GuardFailsOnPollutionTests(unittest.TestCase):
    """抓到污染 -> 非零退出码。"""

    def setUp(self):
        PROBE_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        _rm(PROBE_ROOT)

    def test_modifying_an_imported_module_fails(self):
        """**核心判据**：跑测期间改写已 import 的模块 -> 这一轮必须失败。"""
        d = _new_case("modify")
        _write_probe(d, "")
        rc, out = _run(d)
        # 先在干净态确认守卫在盯、且通过
        self.assertIn("[freeze-guard] 已盯", out)
        self.assertEqual(rc, 0)
        # 再造污染态
        _write_probe(d, MODIFY)
        rc2, out2 = _run(d)
        self.assertNotEqual(rc2, 0, "改了被测文件，退出码却是 0 —— silent green")
        self.assertIn("测量被污染", out2)
        self.assertIn("freeze-guard", out2)

    def test_deleting_an_imported_module_fails(self):
        d = _new_case("delete")
        _write_probe(d, "        TARGET.unlink()\n")
        rc, out = _run(d)
        self.assertNotEqual(rc, 0)
        self.assertIn("测量被污染", out)

    def test_violation_report_is_written_for_review(self):
        """违规必须落一份**可复核的 JSON**（哪个文件/哪种违规/哪条用例发现）。

        ⚠ 为什么必须有这条：只删掉落盘、保留 pytest.exit 时，上面几条判据
        照样全绿（变异实测抓到的洞）。而那份 JSON 是事后唯一能查
        "到底哪个文件被谁改了"的耐久证据，不该只活在 stdout 里。
        """
        import json
        d = _new_case("report")
        _write_probe(d, MODIFY)
        rc, out = _run(d)
        self.assertNotEqual(rc, 0)
        rep = d / "report.json"
        self.assertTrue(rep.exists(), "违规了却没落报告 —— 事后无从复核")
        rows = json.loads(rep.read_text(encoding="utf-8"))
        self.assertTrue(rows, "报告是空的")
        first = rows[0]
        self.assertIn("file", first)
        self.assertIn("kind", first)
        self.assertIn("test", first)
        self.assertTrue(str(first.get("file", "")).endswith("victim.py"),
                        "报告里指的不是真正被改的文件")

    def test_clean_run_writes_no_report(self):
        """干净一轮不该留报告文件，否则下次会误读成"上次污染的"。"""
        d = _new_case("noreport")
        _write_probe(d, "        pass\n")
        rc, out = _run(d)
        self.assertEqual(rc, 0)
        self.assertFalse((d / "report.json").exists(),
                         "干净跑却落了违规报告 —— 会误导下一次排查")

    def test_warn_only_escape_hatch_downgrades_to_zero(self):
        """逃生门：`DOCMIND_FREEZE_WARN_ONLY=1` 显式降级。

        ⚠ 这条同时证明 fail 不是硬编码 —— 否则逃生门是假的。
        """
        d = _new_case("warnonly")
        _write_probe(d, MODIFY)
        rc, out = _run(d, env_extra={"DOCMIND_FREEZE_WARN_ONLY": "1"})
        self.assertIn("测量被污染", out, "逃生门下仍应报告")
        self.assertEqual(rc, 0, "逃生门没生效 —— fail 被写死了")


class GuardNoFalsePositiveTests(unittest.TestCase):
    """反向守卫：干净跑一轮必须全绿。"""

    def setUp(self):
        PROBE_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        _rm(PROBE_ROOT)

    def test_clean_run_is_green_and_quiet(self):
        d = _new_case("clean")
        _write_probe(d, "        pass\n")
        rc, out = _run(d)
        self.assertEqual(rc, 0, "干净跑一轮却失败了 —— 守卫误报")
        self.assertNotIn("测量被污染", out)

    def test_reading_files_does_not_count_as_pollution(self):
        d = _new_case("readonly")
        _write_probe(d, "        for _ in range(3):\n"
                        "            TARGET.read_text(encoding='utf-8')\n")
        rc, out = _run(d)
        self.assertEqual(rc, 0)
        self.assertNotIn("测量被污染", out)

    def test_writing_outside_the_tracked_tree_is_fine(self):
        """往仓库外写 .py 不算污染（守卫只跟踪仓库内已加载的模块）。"""
        d = _new_case("outside")
        _write_probe(d, "        import tempfile, pathlib\n"
                        "        t = pathlib.Path(tempfile.mkdtemp())\n"
                        "        (t / 'x.py').write_text('X = 1\\n',"
                        " encoding='utf-8')\n")
        rc, out = _run(d)
        self.assertEqual(rc, 0)
        self.assertNotIn("测量被污染", out)


class GuardIsLoadedByDefaultTests(unittest.TestCase):
    """默认加载：不传 `-p` 也必须生效（这才是"纪律"而非"建议"）。"""

    def test_root_conftest_declares_the_plugin(self):
        p = ROOT / "conftest.py"
        self.assertTrue(p.exists(),
                        "根 conftest.py 不在 —— 守卫不会默认加载，"
                        "那就退回「要人记得手动 -p」的老问题")
        src = p.read_text(encoding="utf-8")
        self.assertIn("pytest_plugins", src)
        self.assertIn(GUARD, src)

    def test_a_plain_pytest_invocation_loads_it(self):
        """不传 `-p`、不设 PYTHONPATH，跑真实用例，守卫也要在盯。"""
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)          # 不留手工路径
        env["DOCMIND_FREEZE_VERBOSE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["DOCMIND_FREEZE_REPORT"] = str(
            ROOT / ".tmp" / "fg_report.json")
        r = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-m", "pytest",
             "tests/test_session_owner_project.py", "-q", "-p",
             "no:cacheprovider", "--no-header", "--tb=no"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=str(ROOT), env=env)
        out = (r.stdout or "") + (r.stderr or "")
        self.assertIn("[freeze-guard] 已盯", out,
                      "没传 -p 却没加载守卫 —— 它还不是默认生效的纪律")
        self.assertEqual(r.returncode, 0, "守卫在干净跑时误报")


if __name__ == "__main__":
    unittest.main(verbosity=2)
