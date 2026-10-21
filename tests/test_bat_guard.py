# -*- coding: utf-8 -*-
r"""`agent_runtime/bat_guard.py` 的回归：批处理引号静态门。

这道门防的是 2026-10-02 那次真实事故：`--root "%~dp0"` 让 bootstrap 收到
`D:\WorkBuddy\rag-agent"`（多一个字面引号），双击启动就炸。它只在**运行时**暴露，
静态检查看不出来，所以判据必须留在纯函数层。

三类用例都在钉不同的东西：
* 判据表（坏的要红、要改对得下来）——`QuotedArgumentTests` / `UnquotedArgumentTests`；
* 「哪些位置由 cmd 自己解析」——`ArgumentResolutionTests` 直接比对实参清单，
  光看有没有报错是看不出实参认错了的；
* 语料层（真文件、编码、体积上界、扫不到东西不许当干净）——`CorpusFileTests`。

`RealRepoLauncherTests` 刻意**不进**变异电池（见 `LAYER_EXEMPT`）：它测的是别人的
启动器现在干不干净，而电池要的是「我的规则有没有牙」。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_runtime import bat_guard as bg  # noqa: E402

BS = chr(92)          # 反斜杠一律用 chr(92) 拼：.py 里的 `"...\\"` 读起来全是歧义
                      # 只用于**批处理正文**（cmd 的路径分隔符就是 `\`）；拼**文件系统
                      # 路径**必须用 pathlib——反斜杠在 Linux 上是文件名的一部分。
Q = '"'


def rules(text):
    return [row["rule"] for row in bg.find_issues(text)]


def issues(text):
    return bg.find_issues(text)


class QuotedArgumentTests(unittest.TestCase):
    r"""R1：交给外部程序的引号实参，展开后以 `\` 结尾 → 闭合引号被转义掉。"""

    BAD = [
        'python x.py --root "%~dp0"',
        'py -3 boot.py --root=' + Q + '%~dp0' + Q,
        'powershell.exe -File "%~dp0a.ps1" -InstallDir "%~dp0"',
        'python x.py ' + Q + 'D:' + BS + 'tools' + BS + Q,
        'if exist "%DOCMIND_ENTRY%" python x.py --root "%~dp0"',
        'echo hi & python x.py --root "%~dp0"',
        'dir /b x | python x.py --root "%~dp0"',
        'call python x.py --root "%~dp0"',
        'for %%f in (a) do python x.py --root "%~dp0"',
        'if "%X%"=="0" python x.py --root "%~dp0"',
        'python x.py --msg "hello %~dp0" world',
        'python x.py ^' + '\n  --root "%~dp0"',
        'set "ROOT=%~dp0"\npython x.py --root "%ROOT%"',
        '%DOCMIND_HOST_PYTHON% "%DOCMIND_BOOTSTRAP%" --root "%~dp0"',
    ]

    #: 全部是**对的写法**：门要是把它们判红，人就学会把门关掉了。
    GOOD = [
        'python x.py --root "%~dp0."',
        'python x.py --root "%ROOT:~0,-1%"',
        'python x.py "%~dp0a.py"',
        'python x.py --root "%~dp0sub\\a.py"',
        'python x.py "D:' + BS + 'data"',
        'python x.py "D:' + BS + 'data' + BS + BS + '"',
        'python x.py "plain"',
        'python x.py %1',
        'python x.py "a b" c',
        'REM  notes about %~dp0 quoting',
        ':: comment %~dp0',
        ':label',
        '@echo off',
    ]

    def test_each_bad_line_is_caught_once(self):
        for text in self.BAD:
            with self.subTest(text=text):
                self.assertEqual(rules(text), [bg.RULE_QUOTED], issues(text))

    def test_each_good_line_is_left_alone(self):
        for text in self.GOOD:
            with self.subTest(text=text):
                self.assertEqual(rules(text), [], issues(text))

    def test_finding_names_the_token_line_and_a_fix(self):
        row = bg.find_issues('python x.py --root "%~dp0"', source="run_desktop.bat")[0]
        self.assertEqual(row["path"], "run_desktop.bat")
        self.assertEqual(row["line"], 1)
        self.assertEqual(row["token"], '"%~dp0"')
        self.assertIn("%~dp0.", row["fix"])
        self.assertIn("--root", row["command"])

    def test_two_bad_arguments_on_one_line_are_two_findings(self):
        self.assertEqual(rules('python a.py "%~dp0" & python b.py "%~dp0"'),
                         [bg.RULE_QUOTED, bg.RULE_QUOTED])

    def test_odd_backslash_escapes_but_even_does_not(self):
        r"""`"D:\x\"` 炸；`"D:\x\\"` 是「一个字面反斜杠 + 闭合引号」，不炸。"""
        self.assertEqual(rules('py a "D:' + BS + 'x' + BS + '"'), [bg.RULE_QUOTED])
        self.assertEqual(rules('py a "D:' + BS + 'x' + BS + BS + '"'), [])


class UnquotedArgumentTests(unittest.TestCase):
    """R2：目录展开不加引号——路径里有空格就裂成两个参数。"""

    BAD = [
        'python x.py --root %~dp0',
        'node build.js %~dp0config',
        'set "ROOT=%~dp0"\npython x.py --root %ROOT%',
        'python x.py %~dp0y "%~dp0."',
    ]
    GOOD = [
        'python x.py --root "%~dp0."',
        'python x.py %1',
        'echo %~dp0',
        'set "X=%~dp0."',
    ]

    def test_bare_directory_expansion_is_caught(self):
        for text in self.BAD:
            with self.subTest(text=text):
                found = rules(text)
                self.assertIn(bg.RULE_UNQUOTED, found, issues(text))

    def test_bare_arguments_without_an_expansion_are_left_alone(self):
        for text in self.GOOD:
            with self.subTest(text=text):
                self.assertEqual(rules(text), [], issues(text))

    def test_both_rules_can_fire_on_one_line_and_say_different_things(self):
        rows = bg.find_issues('python x.py %~dp0y "%~dp0"')
        self.assertEqual([row["rule"] for row in rows],
                         [bg.RULE_UNQUOTED, bg.RULE_QUOTED])
        self.assertTrue(all(row["fix"] for row in rows))


class ArgumentResolutionTests(unittest.TestCase):
    """cmd 自己解析的位置**不是**实参——这条要靠比对清单，不能只看报不报错。"""

    def test_command_word_itself_is_never_an_argument(self):
        self.assertEqual(bg.arguments_of('"%DOCMIND_PYTHON%" desktop.py'), ["desktop.py"])

    def test_expansion_as_command_word_still_counts_as_external(self):
        self.assertEqual(bg.arguments_of('%HOST_PY% boot.py --root "%~dp0"'),
                         ["boot.py", '--root', '"%~dp0"'])

    def test_cmd_builtins_take_no_msvcrt_arguments(self):
        for line in ('cd /d "%~dp0"', 'set "ROOT=%~dp0"', 'echo %~dp0', 'pushd "frontend"',
                     'exit /b %ERRORLEVEL%', 'pause', 'goto :run', 'chcp 936 >nul',
                     'del /q "%TMP%' + BS + '"', 'type "%~dp0note.txt"'):
            with self.subTest(line=line):
                self.assertEqual(bg.arguments_of(line), [], line)

    def test_if_condition_is_cmd_parsed_but_the_inline_command_is_not(self):
        self.assertEqual(bg.arguments_of('if exist "%~dp0file.bat" goto x'), [])
        self.assertEqual(bg.arguments_of('if not errorlevel 1 set "X=py -3"'), [])
        self.assertEqual(bg.arguments_of('if not "%BUILD%"=="0" goto run'), [])
        self.assertEqual(bg.arguments_of('if exist "a b" py c d'), ["c", "d"])
        self.assertEqual(bg.arguments_of('if %A% == %B% py c d'), ["c", "d"])
        self.assertEqual(bg.arguments_of('if "%X%"=="0" py c d'), ["c", "d"])
        self.assertEqual(bg.arguments_of('if "%ROOT%"=="0" (py c d)'), ["c", "d"])

    def test_for_body_is_the_only_checked_part(self):
        self.assertEqual(bg.arguments_of('for %%a in ("%~dp0*.log") do set X=%%a'), [])
        self.assertEqual(bg.arguments_of('for %f in (%~dp0) do py x y'), ["x", "y"])

    def test_call_into_a_batch_is_cmd_parsed_but_call_into_a_program_is_not(self):
        """`call x.bat --root "%~dp0"` 是对的（参数交给被调批处理里的 cmd），
        `call python x.py --root "%~dp0"` 是错的（参数交给 MSVCRT）。"""
        self.assertEqual(bg.arguments_of('call "%~dp0lib.bat" --root "%~dp0"'), [])
        self.assertEqual(bg.arguments_of('call npm run build'), ["run", "build"])
        self.assertEqual(bg.arguments_of('call python x.py --root "%~dp0"'),
                         ["x.py", "--root", '"%~dp0"'])

    def test_redirections_are_not_arguments(self):
        self.assertEqual(bg.arguments_of('py x >nul 2>&1'), ["x"])
        self.assertEqual(bg.arguments_of('py x 2>&1 >>log.txt'), ["x"])

    def test_pipes_and_amperands_split_into_separate_commands(self):
        self.assertEqual(bg._segments('py x 2>&1 & echo done'), ["py x 2>&1", "echo done"])
        self.assertEqual(bg._segments('dir /b | findstr a && py x'),
                         ["dir /b", "findstr a", "py x"])

    def test_quoted_spaces_stay_inside_one_token(self):
        self.assertEqual(bg.arguments_of('py "a b" c'), ['"a b"', "c"])

    def test_start_is_an_external_launch(self):
        self.assertEqual(bg.arguments_of('start "Agent Workbench" "%DOCMIND_PACKAGED%"'),
                         ['"Agent Workbench"', '"%DOCMIND_PACKAGED%"'])


class DirectoryVariableTests(unittest.TestCase):
    """一跳追踪：`set "ROOT=%~dp0"` 之后的 `"%ROOT%"` 与 `"%~dp0"` 是同一个坑。"""

    def test_variable_holding_the_directory_expansion_is_tracked(self):
        text = 'set "ROOT=%~dp0"\npython x.py --root "%ROOT%"'
        self.assertEqual(rules(text), [bg.RULE_QUOTED])
        self.assertIn("ROOT", bg.find_issues(text)[0]["why"])

    def test_variable_holding_a_file_under_the_directory_is_not_a_directory(self):
        text = 'set "BOOT=%~dp0bootstrap.py"\npython x.py "%BOOT%"'
        self.assertEqual(rules(text), [], bg.find_issues(text))

    def test_tracked_chain_follows_declaration_order_not_alphabetically(self):
        """`set "B=%A%"` 出现在 `set "A=%~dp0"` 之前时，展开时 A 还是空的——
        单向一遍是**对的语义**，不是漏检。这条钉住它别被"顺手改成多遍"带跑。"""
        text = 'set "B=%A%"\nset "A=%~dp0"\npython x.py "%B%"'
        self.assertEqual(rules(text), [], bg.find_issues(text))

    def test_the_stripped_form_is_clean(self):
        self.assertEqual(rules('set "ROOT=%~dp0"\npython x.py --root "%ROOT:~0,-1%"'), [])


class LogicalLineTests(unittest.TestCase):
    def test_caret_continuation_is_joined_and_reported_at_the_start(self):
        text = 'python x.py --root ^' + '\n' + '  "%~dp0"\n'
        rows = bg.find_issues(text)
        self.assertEqual([row["line"] for row in rows], [1])

    def test_line_numbers_count_physical_lines(self):
        text = '@echo off\r\nset "X=1"\r\npython x.py --root "%~dp0"\r\n'
        self.assertEqual(bg.find_issues(text)[0]["line"], 3)

    def test_blank_and_comment_heavy_files_survive(self):
        self.assertEqual(bg.logical_lines("\n\n::x\n"), [(3, "::x")])


class CorpusFileTests(unittest.TestCase):
    """语料层：写真的 .bat 到临时目录，走 discover/read_source/audit/render 全流程。"""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="docmind_batguard_"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text, codec="gbk"):
        path = self.dir / name
        path.write_bytes(text.encode(codec))
        return path

    def test_a_clean_launcher_scans_clean(self):
        self.write("run.bat", '@echo off\r\ncd /d "%~dp0"\r\n'
                              '".venv' + BS + 'Scripts' + BS + 'python.exe" desktop.py\r\n')
        report = bg.audit(self.dir)
        self.assertEqual(report["findings"], [], report["findings"])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["files_scanned"], 1)

    def test_a_dirty_launcher_reports_the_file_and_the_line(self):
        path = self.write("run.bat", '@echo off\r\npy -3 boot.py --root "%~dp0"\r\n')
        report = bg.audit(self.dir)
        self.assertFalse(report["ok"])
        row = report["findings"][0]
        self.assertEqual(row["path"], str(path))
        self.assertEqual(row["line"], 2)
        self.assertEqual(row["rule"], bg.RULE_QUOTED)

    def test_nothing_scanned_is_never_reported_as_clean(self):
        report = bg.audit(self.dir)
        self.assertEqual(report["files_scanned"], 0)
        self.assertFalse(report["ok"])
        self.assertIn("没扫", bg.render(report))

    def test_discover_only_walks_batch_files_and_prunes_the_heavy_dirs(self):
        self.write("run.bat", 'py x\r\n')
        (self.dir / ".venv").mkdir()
        (self.dir / "node_modules").mkdir()
        # 路径必须用 pathlib 拼，不能拿 BS（chr(92)）拼：反斜杠在 Windows 上是分隔符，
        # 在 Linux 上是**文件名的一部分**。写成 ".venv\bad.bat" 时，Linux 会当成一个
        # 叫 `.venv\bad.bat` 的普通文件放在目录根下，剪枝根本没生效——这条用例在
        # ubuntu 上必红，整块电池的对照组随之红掉，35 条变异一条都跑不到
        # （CI 2026-10-21 实测：bat_guard 记 0/35，失败原因就是这一条）。
        self.write(Path(".venv") / "bad.bat", 'py x --root "%~dp0"\r\n')
        self.write(Path("node_modules") / "bad2.bat", 'py x --root "%~dp0"\r\n')
        (self.dir / "notes.txt").write_text("x", encoding="utf-8")
        found = [row.name for row in bg.discover(self.dir)]
        self.assertEqual(found, ["run.bat"])
        self.assertEqual(bg.audit(self.dir)["findings"], [])

    def test_source_is_decoded_the_way_cmd_reads_it(self):
        """启动器按 cp936 存（见 test_launchers.py 那套编码门）：中文注释不能把门读崩。"""
        path = self.write("run.bat", 'REM 中文注释：双击启动\r\npy x --root "%~dp0"\r\n')
        text, clipped = bg.read_source(path)
        self.assertIn("中文注释", text)
        self.assertFalse(clipped)
        self.assertEqual([row["line"] for row in bg.find_issues(text)], [2])

    def test_an_oversized_file_says_so_instead_of_looking_clean(self):
        body = "@echo off\r\n" + "REM pad\r\n" * (1 + bg.MAX_SOURCE_BYTES // 6)
        self.write("huge.bat", body)
        report = bg.audit(self.dir)
        self.assertEqual([row["rule"] for row in report["findings"]], [bg.RULE_TOO_LARGE])
        self.assertFalse(report["ok"])

    def test_an_unreadable_file_is_an_error_not_a_green(self):
        with self.assertRaises(bg.BatGuardError) as caught:
            bg.scan_paths([self.dir / "sub" / "gone.bat"])
        self.assertIn("gone.bat", str(caught.exception))

    def test_the_whole_shipped_fix_table_round_trips(self):
        """一把过：一个文件里同时放着每条**误修**与每条**正解**，逐条对上。"""
        bad_lines = ['py x --root "%~dp0"', 'py x --root %~dp0']
        good_lines = ['py x --root "%~dp0."', 'py x --root "%ROOT:~0,-1%"']
        text = "\r\n".join(bad_lines + good_lines) + "\r\n"
        rows = bg.find_issues(text)
        self.assertEqual([row["line"] for row in rows], [1, 2])
        self.assertEqual([row["rule"] for row in rows], [bg.RULE_QUOTED, bg.RULE_UNQUOTED])
        self.assertIn("%~dp0.", bg.render({"files_scanned": 1, "findings": rows}))


class RenderTests(unittest.TestCase):
    def test_findings_render_with_rule_line_and_fix(self):
        report = bg.audit(self.make_dirty_dir())
        text = bg.render(report)
        self.assertIn("run.bat:2", text)
        self.assertIn(bg.RULE_QUOTED, text)
        self.assertIn("怎么改", text)
        self.assertIn("所在命令", text)

    def make_dirty_dir(self):
        target = Path(tempfile.mkdtemp(prefix="docmind_batguard_render_"))
        (target / "run.bat").write_bytes(b"@echo off\r\npy x --root \"%~dp0\"\r\n")
        self.addCleanup(lambda: __import__("shutil").rmtree(target, ignore_errors=True))
        return target

    def test_clean_render_states_how_many_files_were_actually_read(self):
        target = Path(tempfile.mkdtemp(prefix="docmind_batguard_clean_"))
        (target / "run.bat").write_bytes(b'cd /d "%~dp0"\r\n')
        self.addCleanup(lambda: __import__("shutil").rmtree(target, ignore_errors=True))
        text = bg.render(bg.audit(target))
        self.assertIn("1 个批处理", text)
        self.assertNotIn("发现", text)


class RealRepoLauncherTests(unittest.TestCase):
    """这道门在**本仓真启动器**上的两层结论，分开钉：工作树（现在这台机器上要跑的东西）
    与 HEAD（已经提交、CI 看到的东西）。分开是因为它们会因不同的事而红。
    """

    def test_working_tree_launchers_are_clean(self):
        report = bg.audit(ROOT)
        if report["ok"]:
            return
        detail = "\n".join("%s:%s [%s] %s → %s" % (row["path"], row["line"], row["rule"],
                                                   row["token"], row["fix"])
                           for row in report["findings"])
        self.fail(
            "工作树里有启动器现在就会炸（不是这条门的 bug，是那个 .bat 的实参写法）。\n"
            "2026-10-02 已经为这个写法炸过一次：bootstrap 收到 "
            '"D:\\WorkBuddy\\rag-agent"。\n' + detail)

    def test_head_committed_launchers_are_clean(self):
        listing = self._git(["ls-tree", "-r", "--name-only", "-z", "HEAD"])
        if listing is None:
            self.skipTest("拿不到 git HEAD（不是 git 工作树），HEAD 层无从判断")
        names = sorted(row.decode("utf-8") for row in listing.split(b"\x00")
                       if row and row.lower().endswith(tuple(b.encode() for b in
                                                              bg.BATCH_SUFFIXES)))
        self.assertTrue(names, "HEAD 里一个批处理都没有？")
        for name in names:
            blob = self._git(["show", "HEAD:" + name])
            if blob is None:
                self.fail("读不到 HEAD:%s" % name)
            rows = bg.find_issues(blob.decode("gbk", errors="replace"),
                                  source="HEAD:" + name)
            self.assertEqual([(row["line"], row["token"]) for row in rows], [],
                             "提交出去的启动器不该带这个实参写法：%s" % rows)

    def _git(self, args):
        """回 stdout 的原始字节（文件名是 utf-8、内容是 cp936，只能让调用方自己解）。"""
        try:
            rep = subprocess.run(["git", "--no-pager", "-C", ROOT, *args],
                                 capture_output=True, timeout=30)
        except OSError:
            return None
        return rep.stdout if rep.returncode == 0 else None


class ModuleHonestyTests(unittest.TestCase):
    """文档里承诺过的东西，代码里必须真做；反过来也一样。"""

    def test_both_rules_have_a_fix_hint_and_a_test(self):
        for rule in (bg.RULE_QUOTED, bg.RULE_UNQUOTED, bg.RULE_TOO_LARGE):
            with self.subTest(rule=rule):
                self.assertIn(rule, bg._FIXES)
                self.assertTrue(bg._FIXES[rule].strip())

    def test_the_builtin_list_covers_the_words_the_docstring_exempts(self):
        for word in ("cd", "set", "del", "echo", "type", "pushd"):
            self.assertIn(word, bg.CMD_BUILTINS)

    def test_call_and_for_are_not_exempted_wholesale(self):
        """`call`/`for`/`if` 都不在 CMD_BUILTINS 里：它们各有需要往下查的一半。"""
        for word in ("call", "for", "if", "start"):
            self.assertNotIn(word, bg.CMD_BUILTINS)


if __name__ == "__main__":
    unittest.main()
