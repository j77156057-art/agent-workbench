# -*- coding: utf-8 -*-
"""项目上下文的两个已知限制（立档判据，不修）。

## 为什么这里只有「记录」而不是「要求修好」

`docs/known-defect-project-context-leaks-across-tests.md` 里的那个泄漏，
我试过**三种修法**，全部无效（证据见下）。它是**测试隔离**问题，
不影响产品；但要真修需要动 `config.py` 的模块级常量，而那有
**133 处引用、46 个文件**。

用户 2026-10-12 拍板：**只立档，不修。**

所以本文件钉的是**当前真实成立的事实**（可验证、可回归），
不是「应该修好的样子」。哪天有人真去治了 `config.py`，
本文件会红 —— 那正是提醒他「这个限制消失了」。

## 一、`DOCMIND_STATE_DIR` 这个 env 名不存在

`tests/test_session_owner_project.py:39` 设了它，旁边的注释也说
「先设 DOCMIND_STATE_DIR 让 STATE_ROOT 落到沙箱」。

但 `config.py` 只认 **`DOCMIND_STATE_ROOT`**：

    line 38:  STATE_ROOT = os.getenv("DOCMIND_STATE_ROOT") or BASE_DIR
    line 73:  not os.getenv("DOCMIND_STATE_ROOT")     # 决定是否自动隔离

全仓没有第二处读 `DOCMIND_STATE_DIR`。**那行是空操作** ——
文件以为自己设了沙箱，实际走的是 `config` 自动建的进程级沙箱。

这不是我推的，是 grep 出来的。

## 二、沙箱按**进程**隔离，不按**文件**隔离

实测（`tests/test_session_owner_project.py` + `test_session_restore_qa.py`）：

    [B] config already in modules = True
    [B] STATE_ROOT = D:\\Temp\\docmind_test_state_b88gju4u
    [A] STATE_ROOT = D:\\Temp\\docmind_test_state_b88gju4u     <- 同一个

`config.STATE_ROOT` 在 **import 那一刻**定下。第二个测试文件设自己的
env 已经太晚 —— `config` 在 `sys.modules` 里，`import config` 直接
返回缓存。

### 试过三种修法，全部无效（别重做）

| 修法 | 实测结果 |
|---|---|
| `tests/conftest.py` 里 `pytest_collectstart` 摘模块 | ❌ 太晚，模块已 import 完 |
| 同上，`pytest_collect_file` | ❌ **根本没触发** |
| 同上，`pytest_pycollect_makemodule` | ❌ 触发了、`config` 也摘了，但 `STATE_ROOT` 仍是旧的 |
| 测试侧 `teardown_module` 复位 `current_project_id` | ❌ **回落到注册表第一项**，照样泄漏 |

最后一条的反直觉之处：`current_project_id()` 的回落链是

    _stored_current() -> 若空或不在册 -> list_projects()[0]

所以**只要注册表非空，它必然返回某个项目**。「把 current 复位成空」
根本没用 —— 会被注册表里的第一个补上。

治本点：**沙箱要按文件隔离**，而那要求 `config.STATE_ROOT` 不再是
import 时定死的模块级常量（133 处引用、46 个文件）。用户选择不修。

## 这份文件钉住什么

* 那两个 env 名的**真实关系**（`DOCMIND_STATE_DIR` 无效、
  `DOCMIND_STATE_ROOT` 才有效）—— 这条是**硬事实**，将来谁修好了
  `DOCMIND_STATE_DIR`，这里会红，那是好事
* 跨文件共用一个 `STATE_ROOT` —— 同上

**不**钉「泄漏必须修好」—— 那是做不到的要求，做不到的要求写进判据
就是制造一条永远红的测试。

## 变异实测：2 抓住 / 1 无效（无效的不算证据）

| 变异 | 结果 |
|---|---|
| M1 `config.py` 开始认 `DOCMIND_STATE_DIR` | ✅ 抓住 |
| M2 那个文件改用 `DOCMIND_STATE_ROOT` | ✅ 抓住（**判据先改对才抓住的**） |
| M3 让 `config.STATE_ROOT` 动态求值 | ⚠️ **无效变异**，作废 |

### M2 第一版没抓住 —— 判据查了注释，不是查了行为

第一版写 `assertIn('DOCMIND_STATE_DIR', src)`。变异把**赋值**改成
`DOCMIND_STATE_ROOT` 之后判据仍绿，因为文件里那句**注释**
（「先设 DOCMIND_STATE_DIR 让 STATE_ROOT 落到沙箱」）里还有这个名字。

改成用正则匹配**真正的赋值语句**
（`os.environ["DOCMIND_STATE_..."] = ...`（后接正则））之后才抓住。

**判据查注释 = 判据没在工作** —— 这与今晚第 3 轮「恒真判据」
是同一族问题的另一面：那边是判据太松，这边是判据查错了对象。

### M3 为什么无效

把 `STATE_ROOT` 换成「每次 `__str__` 重读 env」的 str 子类之后，
我起子进程自证：

    自证：子进程里 config.STATE_ROOT = D:\\Temp\\M3BOX_ovfmdngp

**行为确实变了。** 但判据仍绿 —— 因为判据测的是「**同一进程内**
第二个文件能不能用自己的沙箱」，而我这个变异只推翻了「独立进程里
设 env 能不能生效」。`sys.modules['config']` 的缓存在同进程内依然存在。

**没推翻判据在测的那件事，就不算证据。** 作废，不充数。

## ⚠ 这条判据自己踩过的坑（照实记）

第一版这三条**全是红的**，但红因分别是：
子进程找不到仓库模块（`ModuleNotFoundError: config`）、
`set_current` 要求项目已登记、env 名写错。

**三个红都不是「泄漏没修」的证据**，是我判据自身写错了。
把它们当证据就会得出「修法有效/无效」的错误结论。

还有两个**静默失效**（输出看着绿，其实没跑/没测到）：

* 类名写成 `SandboxIsProcessScopedTests` —— pytest 默认只收 `Test*`，
  `--co` 只收集到 2 条，**第 3 条根本没跑**，输出却是「2 passed」。
  **静默失效的判据比没有判据更坏。**
* 纯 pytest 类里写 `self.assertIn` —— 报
  `AttributeError: 'TestSandboxIsProcessScoped' object has no attribute
  'assertIn'`。判据自己坏了，报的还是个看不出所以然的错。
"""

import pathlib
import subprocess
import unittest

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

pytest_plugins = ["pytester"]


class EnvNameTruthTests(unittest.TestCase):
    """`DOCMIND_STATE_DIR` 不存在 —— 钉住这个事实。"""

    def test_config_only_reads_dc_state_root(self):
        src = (REPO / "config.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("DOCMIND_STATE_ROOT")', src,
                      "config 应该读 DOCMIND_STATE_ROOT")
        self.assertNotIn(
            "DOCMIND_STATE_DIR", src,
            "config 里出现了 DOCMIND_STATE_DIR —— "
            "若这是有意新增的，请更新本判据与 "
            "docs/known-defect-project-context-leaks-across-tests.md")

    def test_the_test_file_that_sets_it_is_pointed_at(self):
        """那行**赋值**仍设着那个不存在的 env 名。

        这是**已记录的现状**，不是要求。哪天有人把它改对，这里会红，
        那是提醒「文档该更新了」。

        ## ⚠ 为什么必须匹配「赋值」而不是「出现过这个名字」

        第一版写的是 `assertIn('DOCMIND_STATE_DIR', src)`。变异把
        `os.environ["DOCMIND_STATE_DIR"] = ...` 改成 `DOCMIND_STATE_ROOT`
        之后，这条判据**仍然绿** —— 因为文件里那句**注释**
        （「先设 DOCMIND_STATE_DIR 让 STATE_ROOT 落到沙箱」）里还有这个名字。

        **判据查了注释，不是查了行为。** 变异实测当场抓住这个洞。

        所以这里用正则匹配**真正执行的赋值语句**
        （`os.environ["DOCMIND_STATE_xxx"] = ...`）之后才抓住。

        ⚠ 那段正则里的 `\\w` 若写在**非 raw 的 docstring** 里会触发
        `SyntaxWarning: invalid escape sequence`，警告会混进全量输出。
        下面真正的正则用 r 前缀字符串，避开这个问题。
        """
        import re

        p = REPO / "tests" / "test_session_owner_project.py"
        src = p.read_text(encoding="utf-8")
        assigns = re.findall(
            r'os\.environ\[\s*"(DOCMIND_STATE_\w+)"\s*\]\s*=', src)
        self.assertTrue(assigns,
                        "那个文件不再设 STATE_ROOT/DIR 了 —— "
                        "本判据与那份 known-defect 文档都需要更新")
        self.assertIn("DOCMIND_STATE_DIR", assigns,
                      "那行赋值已经改成有效 env 名 %r 了 —— "
                      "known-defect 文档要更新，本判据也要反过来写" % assigns)


class TestSandboxIsProcessScoped:
    """沙箱按进程隔离 —— 钉住这个事实（用真子进程验）。

    ⚠ 这里**不能用 `unittest.TestCase`**：`pytester` 是 pytest fixture，
    注入不到 unittest 的方法里。第一版写成 TestCase，报
    `TypeError: missing 1 required positional argument: 'pytester'`
    —— 那是我判据写法错，不是被测行为。

    ⚠⚠ 类名**必须**以 `Test` 开头。第一版叫
    `SandboxIsProcessScopedTests`（pytest 的默认 `python_classes = Test*`
    不收），于是 `--co` 只收集到 2 条、**这条根本没跑**，而输出是
    「2 passed」看起来一片绿 —— **静默失效的判据比没有判据更坏**。
    """

    @pytest.mark.timeout(300)
    def test_two_files_share_one_state_root(self, pytester):
        """两个文件读到**同一个** `STATE_ROOT` —— 这就是泄漏的机制。

        ⚠ 这条断言的是**当前行为**（共享），不是期望行为（隔离）。
        哪天有人把 `config.STATE_ROOT` 改成动态的，这里会红。
        """
        repo = str(REPO)
        a = pytester.path / "test_a_first.py"
        a.write_text(
            "import sys\n"
            "sys.path.insert(0, r'%s')\n"
            "import config\n"
            "def test_a():\n"
            "    print('ROOT_A=%%s' %% config.STATE_ROOT)\n" % repo,
            encoding="utf-8")

        r = pytester.runpytest_subprocess(
            "test_a_first.py", "-p", "no:cacheprovider", "-s")
        out = r.stdout.str()
        # ⚠ 纯 pytest 类里**没有** self.assertIn —— 第一版照抄 unittest 的写法，
        # 报 `AttributeError: 'TestSandboxIsProcessScoped' object has no
        # attribute 'assertIn'`。这里用裸 assert。
        assert "ROOT_A=" in out, \
            "第一个文件没打出 STATE_ROOT：\n%s" % out[-1500:]

        # 关键：A 设的 env 对**同一进程内**的后续 import 无效
        b = pytester.path / "test_b_second.py"
        b.write_text(
            "import os, sys, tempfile\n"
            "sys.path.insert(0, r'%s')\n"
            "_box = tempfile.mkdtemp(prefix='SHOULD_NOT_APPEAR_')\n"
            "os.environ['DOCMIND_STATE_ROOT'] = _box\n"
            "import config\n"
            "def test_b():\n"
            "    print('ROOT_B=%%s' %% config.STATE_ROOT)\n" % repo,
            encoding="utf-8")

        r2 = pytester.runpytest_subprocess(
            "test_a_first.py", "test_b_second.py",
            "-p", "no:cacheprovider", "-s")
        out2 = r2.stdout.str()
        assert "ROOT_B=" in out2, \
            "第二个文件没打出 STATE_ROOT：\n%s" % out2[-1500:]
        assert "SHOULD_NOT_APPEAR_" not in out2.split("ROOT_B=")[-1], \
            ("B 的沙箱**生效**了 —— config.STATE_ROOT 不再是 import 时定死的。"
             "那就该更新 known-defect 文档，并把本判据反过来写。"
             "\n实际输出：\n%s" % out2[-1200:])