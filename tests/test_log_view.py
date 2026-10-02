"""日志视图层（重复折叠 + 级别筛选）的回归。

这块的立命之处不是"折得狠"，而是**折了之后计数还能对上、且不假装看见过全文**：
- `tests/fixtures/uvicorn-access.log` 是真起 uvicorn、真打 24 次同一接口抓下来的日志。
  实测：逐行去重**一行都折不动**（每条 access 行带临时客户端端口，全是"唯一行"），
  按签名折 28 → 10 行。测试钉的就是这个差别，不是"看起来短了"。
- 上游 `_CappedBuffer` 是"头 4000 + 尾 4000，丢中段"，所以丢掉的中段必须被报出来；
  有中段被丢时禁止任何"日志里没有 X"的读法。
"""

import io
import json
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime import log_view as lv  # noqa: E402
# 模块级就导入 tools：电池 `job_logs` 要把被变异的 tools 塞进这个属性（BIND='tools'），
# 而电池的规矩是「BIND 必须能在测试模块里解析」。
import tools  # noqa: E402

#: 刻意用 .txt：仓库 .gitignore 里有一条 `*.log`，叫 .log 的夹具会【悄悄不被跟踪】，
#: 于是新检出上这批用例静默跳过而全量仍显示绿灯——本仓为这类判据付过学费。
FIXTURE = Path(ROOT) / "tests" / "fixtures" / "uvicorn-access.txt"
REAL_LOG = FIXTURE.read_text(encoding="utf-8") if FIXTURE.is_file() else ""

MIXED = """INFO: 构建开始
WARNING: 第 %d 次重试失败：连不上 registry
Traceback (most recent call last):
  File "build.py", line 12, in <module>
    raise SystemExit(1)
SystemExit: 1
ERROR: 真的炸了
""" % 3

TRACEBACK = """INFO: 启动
ERROR: 装配失败
Traceback (most recent call last):
  File "api.py", line 3, in <module>
ValueError: 坏值
INFO: 还有一条普通日志
"""


class RealLogTests(unittest.TestCase):
    """没有 skip：夹具是入库的真样本，缺了就该红，不该"跳过还全绿"。"""

    def setUp(self):
        if not REAL_LOG:
            self.fail("缺少真日志夹具 tests/fixtures/uvicorn-access.txt（它必须入库）")

    def test_the_real_sample_is_not_gone_outside_the_repo(self):
        """只管一件事：这份证据【能被入库】且真的在磁盘上。
        一次性脚本留在 .tmp/（gitignore）里，凭据就不能重跑；`.log` 后缀会被
        `.gitignore` 的 `*.log` 吃掉，所以这份叫 `.txt`——那条 glob 曾经让夹具
        "存在但永远进不了仓库"，新检出上整批用例静默跳过而全量仍显示绿灯。"""
        import subprocess
        relative = str(FIXTURE.relative_to(ROOT)).replace("\\", "/")
        self.assertTrue(FIXTURE.is_file(), "缺真日志夹具：%s" % relative)
        ignored = subprocess.run(["git", "check-ignore", "-q", "--", relative], cwd=ROOT,
                                 capture_output=True, text=True)
        self.assertNotEqual(ignored.returncode, 0,
                            "%s 被 .gitignore 吃了，它进不了仓库" % relative)
        # 行数下限钉住"这确实是有重复的真样本"，不是被人换成两行假数据
        self.assertGreaterEqual(len([r for r in REAL_LOG.split("\n") if r]), 25)

    def test_exact_line_dedup_folds_nothing_but_signatures_fold_most_of_it(self):
        """这条是整个功能的存在理由：整行去重 0% 增益，签名折叠把 28 行折成 10 行。"""
        lines = REAL_LOG.split("\n")
        distinct = len({row for row in lines})
        self.assertGreaterEqual(distinct - 1, len(lines) - 2, "样本里几乎没有整行重复")
        got = lv.summarize(REAL_LOG)
        self.assertGreaterEqual(got["lines_in"], 25)
        self.assertLessEqual(got["lines_out"], 12, got["lines_out"])
        self.assertEqual(got["top_repeats"][0]["times"], 21,
                         "24 次同一请求折成一段（前 4 行是启动日志）")
        self.assertIn("GET /api/projects", got["top_repeats"][0]["line"])

    def test_a_folded_run_keeps_its_first_and_last_line_verbatim(self):
        """折掉的是中间，不是两端：首尾留着才看得出"从什么时候刷到什么时候"。"""
        body = lv.summarize(REAL_LOG)["text"]
        self.assertIn("127.0.0.1:65279", body)     # 第一条 access 行
        self.assertIn("127.0.0.1:65303", body)     # 最后一条（404）
        self.assertIn("重复 21 次", body)

    def test_folding_never_drops_a_class_of_line(self):
        """折叠只能减少【同类】的行数，不能减少【类别】：少一类就等于悄悄丢信息。"""
        got = lv.summarize(REAL_LOG)
        before = {lv.signature(row) for row in REAL_LOG.split("\n") if row.strip()}
        after = {lv.signature(row) for row in got["text"].split("\n") if row.strip()}
        self.assertEqual(sorted(before - after), [s for s in sorted(before - after)
                                                 if not s.startswith("…")][:0],
                         "折叠后少了这些类别：%s" % sorted(before - after))

    def test_the_counts_add_up(self):
        """计数自洽：进来的行数 - 筛掉的 - 折掉的 + 折出来的合成提示行 == 出去的行数。
        对不上就说明视图在偷偷丢行（这是 dev_testrun 那套"摘要与退出码不自洽就判红"的同型要求）；
        `markers_added` 必须单列，因为「… ↑ 重复 N 次」是合成行，不是原日志。"""
        for kwargs in ({}, {"level": "warn"}, {"level": "error"}, {"fold": False}):
            with self.subTest(**kwargs):
                got = lv.summarize(REAL_LOG, **kwargs)
                self.assertEqual(got["lines_in"] - got["dropped_lines"]
                                 - got["folded_away"] + got["markers_added"],
                                 got["lines_out"], got)


class SignatureTests(unittest.TestCase):
    def test_the_noise_tokens_are_what_actually_differs(self):
        a = 'INFO:     127.0.0.1:56617 - "GET /api/projects HTTP/1.1" 200 OK'
        b = 'INFO:     127.0.0.1:59991 - "GET /api/projects HTTP/1.1" 200 OK'
        self.assertNotEqual(a, b)
        self.assertEqual(lv.signature(a), lv.signature(b))

    def test_different_status_codes_stay_different(self):
        """数字归一化不能把"200 成功"和"500 炸了"折成同一段——那是最贵的误折。"""
        ok = 'INFO: 127.0.0.1:1 - "GET /x HTTP/1.1" 200 OK'
        bad = 'INFO: 127.0.0.1:1 - "GET /x HTTP/1.1" 500 Internal Server Error'
        self.assertNotEqual(lv.signature(ok), lv.signature(bad))

    def test_long_hex_ids_collapse_so_trace_ids_stop_spamming(self):
        """git sha / trace id / container id 是真日志里另一类"每次都不同"的噪音；
        这条给 `#hex` 规则上牙：只把数字换掉的规则折不动字母数字混排的长 id。"""
        a = 'WARNING: trace 3f2a8b1c4d5e6f708192a3b4c5d6e7f8 重试'
        b = 'WARNING: trace deadbeefcafe1234567890abcdef1234 重试'
        self.assertEqual(lv.signature(a), lv.signature(b))
        got = lv.summarize("\n".join([a, b, a, b, a]))
        self.assertEqual(got["top_repeats"][0]["times"], 5, got)
        self.assertEqual(got["lines_out"], 3, got)

    def test_paths_inside_quotes_stay_distinct(self):
        """刻意不归一引号内容：把 `"/api/settings"` 与 `"/api/models"` 折成同一段，
        就是把两个不同事件合并了——比"折不动"更糟。"""
        a = 'INFO: 127.0.0.1:1 - "GET /api/settings HTTP/1.1" 404 Not Found'
        b = 'INFO: 127.0.0.1:2 - "GET /api/models HTTP/1.1" 200 OK'
        self.assertNotEqual(lv.signature(a), lv.signature(b))

    def test_uuid_hex_and_ip_collapse_but_paths_do_not(self):
        self.assertEqual(lv.signature("job 3f2a8b1c-0d0e-4f00-8000-123456789abc"),
                         lv.signature("job 9a8b7c6d-0000-1111-2222-333344445555"))
        self.assertNotEqual(lv.signature("GET /api/projects"), lv.signature("GET /api/models"))


class LevelTests(unittest.TestCase):
    def test_level_is_a_floor_not_an_equality(self):
        log = "DEBUG: 细节\nINFO: 普通\nWARNING: 要注意\nERROR: 要修\n"
        warn = lv.summarize(log, level="warn")
        self.assertEqual(warn["lines_out"], 2, warn)
        self.assertNotIn("DEBUG", warn["text"])
        self.assertNotIn("普通", warn["text"])
        self.assertIn("ERROR", lv.summarize(log, level="error")["text"])
        self.assertNotIn("WARNING", lv.summarize(log, level="error")["text"])

    def test_traceback_body_survives_a_level_filter(self):
        """逐行筛级别的错法：traceback 正文没有级别词，会被一起筛掉，只剩一个空标题。"""
        got = lv.summarize(TRACEBACK, level="error")
        body = got["text"]
        self.assertIn("ERROR: 装配失败", body)
        self.assertIn("ValueError: 坏值", body)
        self.assertIn('File "api.py"', body)
        self.assertNotIn("INFO: 启动", body)

    def test_untagged_lines_are_kept_not_guessed(self):
        """认不出级别的普通行（比如进度条）跟着当前级别走；在文件开头没标题时一律保留。"""
        got = lv.summarize("进度 12%\n进度 13%\n进度 14%\n进度 15%\n", level="error")
        self.assertIn("进度", got["text"], "无级别可继承时宁可多留，不能静默删")

    def test_unknown_level_is_refused(self):
        for bad in ("fatal-ish", "所有", "warn OR error"):
            with self.subTest(level=bad):
                self.assertFalse(lv.summarize(TRACEBACK, level=bad)["ok"])


class HonestyTests(unittest.TestCase):
    def test_a_buffer_gap_is_reported_as_a_gap(self):
        log = "INFO: 开头\n...[省略 40000 字符]...\nERROR: 结尾"
        got = lv.summarize(log)
        self.assertEqual(got["middle_omitted_chars"], 40000)
        self.assertIn("不能说", lv.render(got))

    def test_filtered_away_failure_marks_are_counted(self):
        """uvicorn 把 404 记成 INFO：`level: warn` 筛掉它是合规的，但视图必须说出
        "筛掉的里面有带 4xx/5xx 字样的行"，否则这份视图会被读成"没有错误"。"""
        got = lv.summarize(REAL_LOG or ("INFO: 1 - \"GET /a\" 404 Not Found\n"
                                       "INFO: 2 - \"GET /a\" 404 Not Found\n"
                                       "INFO: 3 - \"GET /a\" 404 Not Found\n"), level="warn")
        self.assertGreaterEqual(got["dropped_failure_marks"], 1, got)
        self.assertIn("不证明没有错误", lv.render(got))

    def test_the_line_cap_is_announced(self):
        log = "\n".join("WARNING: 第 %d 次" % i for i in range(lv.MAX_LOG_LINES + 50))
        got = lv.summarize(log)
        self.assertEqual(got["line_cap_hit"], 50)
        self.assertIn("只处理了前", lv.render(got))

    def test_fold_off_really_does_not_fold(self):
        """`fold: false` 必须原样保留每一行：分组本身就是在折叠，不关就成谎报。"""
        got = lv.summarize(REAL_LOG, fold=False)
        self.assertEqual(got["lines_out"], got["lines_in"])
        self.assertEqual(got["folded_away"], 0)
        self.assertNotIn("重复", got["text"])

    def test_runs_below_the_fold_threshold_stay_verbatim(self):
        """两行的重复不折：折两行省一行，却让人怀疑"是不是少了东西"，不划算。"""
        log = "WARNING: 连不上 registry\nWARNING: 连不上 registry\nERROR: 炸了\n"
        got = lv.summarize(log)
        self.assertEqual(got["lines_out"], 3)
        self.assertEqual(got["folded_away"], 0)


class RenderTests(unittest.TestCase):
    def test_counts_come_before_the_body(self):
        got = lv.summarize(MIXED.replace("%d", "3"))
        text = lv.render(got, title="构建")
        self.assertTrue(text.startswith("构建："))
        self.assertIn("WARNING: 第 3 次重试失败", text)

    def test_a_failed_view_says_why_instead_of_rendering_empty(self):
        text = lv.render({"ok": False, "error": "level 只支持 debug|info|warn|error"})
        self.assertIn("读取失败", text)


class ToolSurfaceTests(unittest.TestCase):
    """`fold:` / `level:` 必须真的落到模块上——参数没落到模块，本仓为此付过学费。"""

    def _patch_logs(self, logs, calls):
        import tools
        from agent_runtime import process_runner

        def fake_job_logs(job_id="", *, offset=0):
            calls.append({"job_id": job_id, "offset": offset})
            return {"ok": True, "job_id": job_id or "", "state": "running",
                    "logs": logs, "next_offset": len(logs), "total_chars": len(logs),
                    "truncated": False, "exit_code": None, "elapsed": 1.0, "timeout": 60}

        self._saved = process_runner.job_logs
        process_runner.job_logs = fake_job_logs
        self.addCleanup(setattr, process_runner, "job_logs", self._saved)
        return tools.TOOLS["dev_job_logs"]["func"]

    def test_fold_and_level_reach_the_module(self):
        spam = "\n".join('INFO: 127.0.0.1:%d - "GET /x HTTP/1.1" 200 OK' % (50000 + i)
                         for i in range(12))
        calls = []
        call = self._patch_logs(spam, calls)
        out = json.loads(call("job_id: j1\nfold: true"))
        self.assertEqual(out["view"]["lines_in"], 12)
        self.assertEqual(out["view"]["top_repeats"][0]["times"], 12, out["view"])
        self.assertIn("重复 12 次", out["logs"])
        self.assertNotIn("offset 被忽略", out.get("note", ""))

        out2 = json.loads(call("job_id: j1\nlevel: warn"))
        self.assertEqual(out2["view"]["dropped_runs"], 1)
        self.assertGreaterEqual(out2["view"]["dropped_failure_marks"], 0)

    def test_view_mode_refuses_to_pretend_incremental(self):
        """带 offset 的续读遇到视图模式必须明说被忽略，并把 next_offset 置空：
        留着旧 offset 会让人下一次重复读同一段，还以为在读新的。"""
        spam = "\n".join("WARNING: 重试 %d 次失败" % i for i in range(9))
        calls = []
        call = self._patch_logs(spam, calls)
        out = json.loads(call("job_id: j1\noffset: 30\nlevel: warn"))
        self.assertIn("offset 被忽略", out["note"])
        self.assertIsNone(out["next_offset"])
        self.assertEqual(calls[-1]["offset"], 30, "仍然按请求的 offset 取过原始文本")

    def test_raw_mode_keeps_the_text_and_adds_only_a_hint(self):
        spam = "\n".join("WARNING: 第 %d 次失败" % i for i in range(10))
        calls = []
        call = self._patch_logs(spam, calls)
        out = json.loads(call("job_id: j1"))
        self.assertEqual(out["logs"], spam, "原始读法不能改文本")
        self.assertIn("view_hint", out)
        self.assertIn("fold: true", out["view_hint"])

    def test_quiet_logs_get_no_hint_at_all(self):
        calls = []
        call = self._patch_logs("INFO: 一切正常\n", calls)
        out = json.loads(call("job_id: j1"))
        self.assertNotIn("view_hint", out, "没问题时不该多塞一行噪音")

    def test_bad_level_is_reported_as_text(self):
        calls = []
        call = self._patch_logs("INFO: x\n", calls)
        self.assertIn("level 只支持", call("job_id: j1\nlevel: fatal-ish"))


if __name__ == "__main__":
    unittest.main()
