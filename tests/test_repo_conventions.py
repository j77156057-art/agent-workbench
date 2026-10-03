"""仓库自身的两条**入库约定**——扫的是 git 里真实入库的 blob，不是工作树。

## 为什么要有这一份

这两条都曾经以"看起来是环境问题"的形态出现过，而 CI 从未在 GitHub 上真跑过一次，
所以没人看见它们：

1. **`.py` 的 BOM**：`.gitattributes` 写明 `*.py working-tree-encoding=UTF-8`，
   BOM 会让 `ast.parse` 直接 `SyntaxError: invalid non-printable character U+FEFF`。
   `7aaf365` 修过一次 `llm.py` 顶部残留的 BOM —— 说明它会再犯。
2. **守卫自己有没有瞎**：扫 blob 用的是 `git ls-tree`，而 git 默认把非 ASCII 路径转义成
   `\345\220\257…` 输出。预检脚本第一版就因此**整个跳过了 `启动DocMind.bat`**——
   守卫报全绿，而要找的东西就在里面。

## 关于 `.bat` 的 CRLF：这条**不在本文件**

它归 `tests/test_launchers.py::LauncherEncodingTests::
test_only_launchers_that_use_labels_have_to_be_crlf`（按**内容**判定：用到 goto 标签的
才要求 CRLF）。第一版在本文件里也写了一条全称版本，于是同一个文件被两条不同口径的规则
管着——一处绿一处红，而绿的那条其实是错的（Wave31 实测：本机 CRLF 与 LF 在 goto 上行为
一致，全称规则复现不出来）。**一条规则只许有一个家**：分散的代价不是"多写一次"，
是"两份会分叉，然后你不知道该信哪个"。

## 为什么扫 blob 而不是工作树

工作树的内容取决于**谁 checkout、什么 core.autocrlf**；而"入库时丢了 CRLF / 带了 BOM"
这件事只存在于 blob 里。扫工作树会在 Windows 上因为自动转换而看不出问题，
也会在别人正在改文件时给出假红。
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: cmd 的标签跳转：`goto :label` 与 `:label`。只有用到它的脚本才需要 CRLF。
GOTO_LABEL = re.compile(rb'^\s*goto\s+:\w+', re.M | re.I)
BOM = b'\xef\xbb\xbf'


def _git(*args, binary=False):
    """跑 git；默认**不转义非 ASCII 路径**。

    git 默认把 `启动DocMind.bat` 输出成 `\\345\\220\\257…`（core.quotepath）——
    预检脚本第一版就因为这个**整个跳过了那个文件**，差点漏掉要找的东西本身。
    """
    env = dict(os.environ)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotepath",
                "GIT_CONFIG_VALUE_0": "false"})
    got = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, env=env)
    if got.returncode:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args),
                                             got.stderr.decode("utf-8", "replace")[:200]))
    return got.stdout if binary else got.stdout.decode("utf-8", "replace")


def _tracked():
    """受版本控制的文件（NUL 分隔，避免路径里的空格与转义）。"""
    raw = _git("ls-tree", "-r", "-z", "--name-only", "HEAD", binary=True)
    return [name for name in raw.decode("utf-8", "replace").split("\0") if name]


class RepoBlobConventionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.names = _tracked()
        except RuntimeError as exc:
            raise unittest.SkipTest("拿不到 git 的文件清单：%s" % exc)

    def test_a_goto_using_launcher_would_be_caught_if_it_went_lf(self):
        """反向自查：那条**收窄后**的 CRLF 规则（`test_launchers.py` 那条）确实还有牙。

        收窄规则最典型的失败方式就是"收着收着谁都不报了"。这里不依赖被测文件本身，
        而是把判定单独拿出来喂一个构造出来的坏例子。
        """
        label_free = b'@echo off\r\ncall "%~dp0other.bat"\r\nexit /b 0\r\n'
        with_goto_lf = b"@echo off\nsetlocal\ngoto :work\n:work\nexit /b 0\n"
        self.assertFalse(GOTO_LABEL.search(label_free),
                         '这条正则把没有标签的脚本也算成有标签了')
        self.assertTrue(GOTO_LABEL.search(with_goto_lf),
                        '这条正则认不出 goto :label，收窄后的规则就成了永不触发的装饰')
        # 判据的形状：裸 LF 数出来 > 0 就该被拒，而没标签的那个是 0
        self.assertGreater(with_goto_lf.replace(b"\r\n", b"").count(b"\n"), 0)
        self.assertEqual(label_free.replace(b"\r\n", b"").count(b"\n"), 0)

    def test_no_python_blob_carries_a_bom(self):
        """`.gitattributes` 说 `*.py working-tree-encoding=UTF-8`——BOM 会让 ast.parse 直接炸。"""
        offenders = [name for name in self.names
                     if name.endswith(".py") and _git("show", "HEAD:" + name,
                                                      binary=True).startswith(BOM)]
        self.assertEqual(offenders, [],
                         "这些 .py 的 blob 带 UTF-8 BOM（ast.parse 会 SyntaxError）：%s"
                         % offenders[:10])

    def test_the_check_actually_sees_the_launchers(self):
        """防"这条守卫自己瞎了"：它必须真的看得见那三个 .bat。

        第一版预检脚本用 `git ls-tree`（会转义非 ASCII 路径），于是
        `启动DocMind.bat` 根本没进结果——守卫全绿，而要找的东西就在里面。
        """
        launchers = [name for name in self.names
                     if name.lower().endswith((".bat", ".cmd"))]
        self.assertGreaterEqual(len(launchers), 3, launchers)
        self.assertIn("启动DocMind.bat", launchers,
                      "非 ASCII 的文件名被 git 转义掉了，这条守卫正瞎着")


if __name__ == "__main__":
    unittest.main()
