# -*- coding: utf-8 -*-
"""启动器（.bat）编码护栏。

## 这个坑长什么样（2026-09-14 真实踩过）

批处理文件里的非 ASCII 字符，cmd 会用**当前代码页**去解析字节。文件按 UTF-8 保存、而系统
默认代码页是 936(GBK) 时，中文注释被误读成乱码；乱码的"前导字节"还会**吃掉行尾 CRLF、
甚至吃掉下一行开头的字符**，于是报出这些完全指不到根因的错：

    'op.py' 不是内部或外部命令                 <- desktop.py 被从中间切开
    '鍙屽紩鍙峰寘瑁癸紙~dp0"' 不是内部或外部命令    <- 中文注释乱码后 % 被吃掉

静态检查、语法检查全看不出来，只有双击运行时才炸。

## 护栏规则

1. 每个 .bat 必须能按 cp936 无损解码（中文 Windows 的默认代码页）；
2. **不能是"存成 UTF-8 却在 GBK 下解析"**——这是原始 bug 的判别器：
   同一份字节分别按 UTF-8 和 GBK 解一遍，数谁解出来的常用汉字多，就能判定真实编码；
3. **会执行的命令行里不允许出现非 ASCII**（中文只允许待在 REM / echo 文本里）。
   命令行是 ASCII 就与代码页无关，解析一定正确；echo 里的中文最坏只是显示乱码，不会执行错；
4. 含中文的文件必须显式 `chcp 936`，否则中文显示成乱码。
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只用 REM / 这些关键字开头的行允许带中文
_TOLERANT_PREFIXES = ('rem', '@echo', 'echo', '::')
# 常用汉字：用来判别"这份字节到底是 UTF-8 还是 GBK"（乱码里几乎不会出现它们）
_COMMON_CJK = set('的一是不了在人有我你他这中文件本错误路径启动运行请先用检失败成功提示注意'
                  '保存编码注释命令执行程序语言测试通过项目目录版本')


def _launchers():
    return sorted(glob.glob(os.path.join(ROOT, '*.bat')) +
                  glob.glob(os.path.join(ROOT, '*.cmd')))


def _cjk_score(text):
    return sum(1 for ch in text if ch in _COMMON_CJK)


def _has_non_ascii(text):
    return any(ord(ch) > 127 for ch in text)


class LauncherFailureVisibilityTests(unittest.TestCase):
    r"""启动器**失败时不能一闪而过**（Wave31）。

    实测形状：`启动DocMind.bat` 原来是 `call "%~dp0run_desktop.bat"` 紧接
    `exit /b %ERRORLEVEL%`。而 `run_desktop.bat` 的三条失败分支都调了 `pause`，
    **唯独成功路径**（`.venv\Scripts\python.exe desktop.py` 之后）没有——于是应用自己
    崩掉时，脚本结束、窗口立刻关掉，错误刷过去就没了。用 stub callee 实测：

        现状：exit=1，屏幕 25 字符，窗口**不停**
        修后：exit=1，屏幕 87 字符，窗口停住，且明说退出码

    这是"看得见的失败"与"闪退"的差别，不是行尾问题——同一台机器上实测这个文件
    CRLF 与 LF 行为**完全一致**。
    """

    LAUNCHER = '启动DocMind.bat'

    def _probe_hold(self, callee: bytes, hold_seconds: int = 5, feed: bool = False):
        """跑一次启动器，返回 (是否在 hold_seconds 内结束, 退出码, 屏幕)。

        原来这两条用例断言的是 `pause` 提示语的英文字面「Press any key」。那行字是
        cmd.exe 按控制台代码页印的：中文 Windows 上是「按任意键继续. . .」，而且 stdout
        是 cp936 —— 用 utf-8 解码只剩一串乱码。本机（zh-CN）实测因此**恒红**，而批处理
        行为其实是对的（窗口确实停住了），也就是说红的是断言，不是产品。
        现在断言的是它代指的那个机制：**失败路径会阻塞等人按键，成功路径不会**。

        两趟分工，缺一趟就少钉一件事：
        - `feed=False` 验「停没停」。stdin 是一根**保持打开、不写也不关**的管道，
          `pause` 拿不到字节就真的不返回。这里不能用 `communicate()` —— 它会先把
          stdin 关掉，`pause` 立刻走过去，我第一次改完就是这样假绿变假红。等待期间
          不读 stdout：屏幕内容只有我们回显的两行，远不到管道缓冲区上限。
        - `feed=True` 验「退出码传没传」。喂一个回车让 `pause` 走过去，脚本跑到
          `exit /b %DOCMIND_EXIT%`；`feed=False` 那一趟是被 kill 的，拿不到真实退出码。
        """
        import shutil
        import subprocess
        import tempfile
        import time
        real = open(os.path.join(ROOT, self.LAUNCHER), 'rb').read()
        root = tempfile.mkdtemp(prefix='dm_launcher_')
        try:
            with open(os.path.join(root, 'run_desktop.bat'), 'wb') as handle:
                handle.write(callee)
            with open(os.path.join(root, 'w.bat'), 'wb') as handle:
                handle.write(real)
            argv = ['cmd.exe', '/c', os.path.join(root, 'w.bat')]
            if feed:
                proc = subprocess.run(argv, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, cwd=root, input=b'\r\n',
                                      timeout=60)
                out, finished, code = proc.stdout, True, proc.returncode
            else:
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, cwd=root)
                deadline = time.monotonic() + hold_seconds
                while time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(0.2)
                finished = proc.poll() is not None
                if not finished:
                    proc.kill()
                out = proc.stdout.read()
                proc.wait()
                code = proc.returncode if finished else None
                if proc.stdin is not None:
                    proc.stdin.close()
            # 我们自己回显的两行是 ASCII，任何代码页解码都一致；不做语言假设。
            return finished, code, (out or b'').decode('utf-8', 'replace')
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @unittest.skipUnless(os.name == 'nt', '批处理行为要在 cmd.exe 上验')
    def test_a_failing_inner_launcher_keeps_the_window_and_names_the_code(self):
        broken = b'@echo off\r\necho inner: ModuleNotFoundError\r\nexit /b 1\r\n'
        finished, _code, _screen = self._probe_hold(broken)
        self.assertFalse(finished,
                         '脚本没等按键就退出了 —— 崩溃信息一闪而过，等于没有报错')
        # 停住那一趟是被 kill 的，退出码要另喂一个按键跑到 `exit /b` 才拿得到。
        finished, code, screen = self._probe_hold(broken, feed=True)
        self.assertTrue(finished, '喂了按键还停着：pause 之后没有正常收尾')
        self.assertEqual(code, 1, '失败码没传出去：外面只看得到「脚本跑完了」')
        self.assertIn('ModuleNotFoundError', screen, '内层的报错必须还在屏幕上')
        self.assertIn('Agent Workbench exited with code 1', screen, '失败时必须说出退出码')

    @unittest.skipUnless(os.name == 'nt', '批处理行为要在 cmd.exe 上验')
    def test_a_successful_run_is_not_held_open(self):
        """反过来也要钉：成功路径**不能**被 pause 拖住（那会让正常启动看起来像卡住）。"""
        finished, code, screen = self._probe_hold(
            b'@echo off\r\necho inner: all good\r\nexit /b 0\r\n')
        self.assertTrue(finished, '成功路径被停住了：正常启动看起来像卡死')
        self.assertEqual(code, 0)
        self.assertNotIn('exited with code', screen, '成功时不该报退出码')


class LauncherEncodingTests(unittest.TestCase):
    def test_primary_launcher_prefers_current_source(self):
        """真正跑源码的那段逻辑在 `run_desktop.bat` 里——`启动DocMind.bat` 只是薄包装。

        这条断言原来在 `启动DocMind.bat` 里找 `if exist "%DOCMIND_PYTHON%"`，
        而那段内容在 `e651c9c` 之后已经搬进 `run_desktop.bat`（主启动器被重写成
        "call 一下就走"的包装）。原来它先被 CRLF 那条断言挡着，所以这个"找错文件"
        的问题一直没暴露——CRLF 一放行它就会以 `ValueError` 冒出来。
        """
        with open(os.path.join(ROOT, 'run_desktop.bat'), 'rb') as f:
            text = f.read().decode('ascii')
        source_check = text.index('if exist "%DOCMIND_ENTRY%"')
        packaged_check = text.index('if exist "%DOCMIND_PACKAGED%"')
        self.assertLess(source_check, packaged_check,
                        '必须先试当前源码，再考虑打包版本')
        self.assertIn('%DOCMIND_HOST_PYTHON% "%DOCMIND_BOOTSTRAP%"', text)
        self.assertIn('".venv\\Scripts\\python.exe" "%DOCMIND_ENTRY%"', text)

    def test_only_launchers_that_use_labels_have_to_be_crlf(self):
        """**含标签的** .bat 必须是 CRLF；不含标签的不断言行尾（Wave31 实测后的收窄）。

        收窄的理由不是"测不出来就放过"，而是那条断言守的是一个**在本机复现不出的机制**：
        把带 `goto` 的批处理分别按 CRLF 与 LF 各跑一次（2026-10-03，本机 Windows），
        **两种行尾都正常到达标签**。而被断言的 `启动DocMind.bat` 只有 `call`、**没有 label**，
        LF 与 CRLF 实测行为完全一致（都 exit=0、callee 正常执行）。

        所以规则改成按**内容**判定而不是按**文件名**判定：真正会咬人的那类
        （一个用 goto 的启动器被写成 LF）照样被抓住；而没有 label 的文件不再被一条
        没有机制支撑的断言逼着改行尾——它当时正在逼人做"改启动器"还是"让 CI 绿"的二选一。

        **边界**：以上只是**这台 Windows** 上的实测。别的 Windows 版本上 goto 与行尾的
        关系没有证据，所以这里只说"复现不出"，不说"LF 没问题"。
        """
        for path in _launchers():
            with open(path, 'rb') as f:
                raw = f.read()
            text = raw.decode('gbk', 'replace')
            uses_labels = bool(re.search(r'^\s*goto\s+:\w+', text, re.M | re.I))
            if not uses_labels:
                continue                       # 没有标签的脚本，行尾与它无关
            with self.subTest(launcher=os.path.basename(path)):
                self.assertNotIn(
                    b'\n', raw.replace(b'\r\n', b''),
                    '%s 用了 goto 标签，必须是 CRLF（这条在本机被实测复现过，见方法注释）'
                    % os.path.basename(path))

    def test_bat_files_decode_as_cp936(self):
        for path in _launchers():
            with open(path, 'rb') as f:
                raw = f.read()
            try:
                raw.decode('gbk')
            except UnicodeDecodeError as e:
                self.fail('%s 无法按 cp936 解码（%s）。批处理必须存为 ANSI/GBK，'
                          '否则 cmd 会误读中文并可能吃掉后续字符。' % (os.path.basename(path), e))

    def test_not_utf8_saved_but_gbk_parsed(self):
        """判别器：不能"存成 UTF-8 但没声明 65001"——这正是原始 bug。"""
        for path in _launchers():
            name = os.path.basename(path)
            with open(path, 'rb') as f:
                raw = f.read()
            if not any(b >= 0x80 for b in raw):
                continue                                   # 纯 ASCII，任何代码页都一样
            text_gbk = raw.decode('gbk')
            declares_cp = re.search(r'chcp\s+(\d+)', text_gbk[:400].lower())
            declares = declares_cp.group(1) if declares_cp else ''
            try:
                text_utf8 = raw.decode('utf-8')
                utf8_decodable = True
            except UnicodeDecodeError:
                utf8_decodable = False

            if declares == '65001':
                self.assertTrue(utf8_decodable,
                                '%s 声明了 chcp 65001，但字节不是合法 UTF-8' % name)
                continue

            # 没声明 65001：它必须是 GBK。若按 UTF-8 能解出**更多常用汉字**，
            # 说明这份文件其实是 UTF-8 保存的 —— cmd 会按 GBK 读，必然乱码。
            if utf8_decodable:
                score_utf8, score_gbk = _cjk_score(text_utf8), _cjk_score(text_gbk)
                self.assertLessEqual(
                    score_utf8, score_gbk,
                    '%s 看起来是 UTF-8 保存的（UTF-8 解读命中常用汉字 %d 个，GBK 只有 %d 个），'
                    '但它没有声明 chcp 65001 —— cmd 会按默认代码页(936)误读中文，'
                    '乱码字节能吃掉行尾 CRLF 甚至下一行开头，报出 "xx 不是内部或外部命令" 这类怪错。'
                    '请用 ANSI/GBK 另存，或加 chcp 65001 并改用 UTF-8。'
                    % (name, score_utf8, score_gbk))

    def test_executable_lines_are_pure_ascii(self):
        """会执行的命令行必须纯 ASCII（中文只允许在 REM/echo 文本里）。"""
        for path in _launchers():
            name = os.path.basename(path)
            with open(path, 'rb') as f:
                text = f.read().decode('gbk')
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.lower().startswith(_TOLERANT_PREFIXES):
                    continue
                # `) else (echo 暂无日志)` 这类：中文在 echo 参数里，属于显示内容，放行
                if 'echo' in stripped.lower() and not _has_non_ascii(
                        stripped.split('echo', 1)[0]):
                    continue
                if _has_non_ascii(line):
                    self.fail('%s:%d 是命令行但含非 ASCII 字符：%r\n'
                              '中文请挪进 REM/echo 文本——命令行一旦含多字节字符，'
                              'cmd 在不同代码页下会解析成完全不同的东西。' % (name, lineno, line))

    def test_non_ascii_launchers_declare_codepage(self):
        for path in _launchers():
            name = os.path.basename(path)
            with open(path, 'rb') as f:
                raw = f.read()
            if not any(b >= 0x80 for b in raw):
                continue
            head = '\n'.join(raw.decode('gbk').splitlines()[:6]).lower()
            self.assertIn('chcp 936', head,
                          '%s 含中文但前几行没有 `chcp 936`，中文会显示成乱码' % name)

    def test_no_replacement_characters(self):
        for path in _launchers():
            with open(path, 'rb') as f:
                text = f.read().decode('gbk', errors='replace')
            bad = [i for i, ln in enumerate(text.splitlines(), 1) if '\ufffd' in ln]
            self.assertEqual(bad, [], '%s 在第 %s 行解码出乱码字符' % (os.path.basename(path), bad))


if __name__ == '__main__':
    unittest.main()
