# -*- coding: utf-8 -*-
"""依赖升级的**用户决定权**：问、不问、拒绝后怎么算。

## 为什么要改

`dependency_bootstrap.ensure_dependencies` 原来只看清单哈希：

    python_changed = force or created_venv or previous["requirements_hash"] != req_hash
    if python_changed:  pip install --upgrade -r requirements.txt

`requirements.txt` 一变（厂商下发新版本）就在**启动路径上自动装**，用户只看到一段
`pip install` 输出。这次实测一次就是 919 MB 下载。而那次 `--upgrade` 还会把清单里
26 条**没有版本约束**的包一并升到最新 —— 范围超出了「装清单里声明的东西」。

所以要的是：**版本变化时先问，拒绝就记下来别再烦。**控制台提示即可（类似市面软件）。

## 六个场景（探针 `.tmp/probe_upgrade_prompt.py` 实测出来的形状）

| 场景 | 期望 |
|---|---|
| A 首次安装 | 不问，直接装 |
| B 同版本再启动 | **不问**，不装 |
| C 清单变了但版本没变 | **不问**（同一版本内的开发不该被打扰），照常装 |
| D 版本变了 + 拒绝 | 问；不装；**之后同一版本不再问** |
| E 版本变了 + 同意 | 问；装；状态记为已装该版本 |
| F 非交互（`--yes`/无 tty） | **不问**，自动装，且**绝不能挂住等输入** |

## 反向守卫：最容易出的错是「拒绝只生效一次」

第一版把拒绝记成「本次跳过」（`requirements_hash` 存旧值），看着对，但下一启动：

    又问了吗 = 0   装了 = ['同步 Python 依赖']   ← 拒绝了却又装了

哈希仍对不上 -> 又要同步 -> 又不问（因为已否决）-> **默默装上**。决定权只在第一次
生效，等于没有。所以下面专门有一条守卫钉住「拒绝后**连装都不许发生**」。
"""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dependency_bootstrap as bootstrap  # noqa: E402


def _make_venv(venv: Path) -> None:
    """造一个「看起来完整」的 venv 目录（按当前平台的布局）。

    venv 的目录布局随平台变：Windows 是 `Scripts\\python.exe`，POSIX 是 `bin/python`。
    生产侧的 `_python_for_venv()` 按平台算路径；夹具写死 Windows 形状的话，
    在 Linux 上会被判成 `virtual environment is incomplete` —— 那是**夹具**错了。
    这里独立地按平台拼一遍（不直接调用生产那个函数），夹具才有资格校验生产规则。
    """
    folder = venv / ("Scripts" if os.name == "nt" else "bin")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("python.exe" if os.name == "nt" else "python")).touch()
    (folder / ("pip.exe" if os.name == "nt" else "pip")).touch()


def _make(root: Path, version: str, reqs: str, lock: str = '{"packages":{}}'):
    (root / "VERSION").write_text(version, encoding="utf-8")
    (root / "requirements.txt").write_text(reqs, encoding="utf-8")
    (root / "frontend").mkdir(exist_ok=True)
    (root / "frontend" / "package-lock.json").write_text(lock, encoding="utf-8")
    _make_venv(root / ".venv")


class _Recorder:
    def __init__(self):
        self.labels = []

    def __call__(self, command, cwd, *, label):
        self.labels.append(label)


class _Box:
    """跑一次 ensure_dependencies，记录装了啥、弹了几次提示、用户答了什么。"""

    def __init__(self, answers=(), interactive=True, **kw):
        self.answers = list(answers)
        self.interactive = interactive
        self.kw = kw
        self.labels = []
        self.prompts = []
        self.asked = 0

    def prompt(self, project, summary):
        self.asked += 1
        self.prompts.append(summary)
        answer = self.answers.pop(0) if self.answers else "y"
        return answer in ("", "y", "yes")

    def __enter__(self):
        rec = _Recorder()
        self.labels = rec.labels
        self._patches = [
            patch.object(bootstrap, "_run", side_effect=rec),
            patch.object(bootstrap, "_console_prompt", side_effect=self.prompt),
            patch.object(bootstrap, "_find_npm", return_value=None),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        return False

    def run(self, root):
        self.result = bootstrap.ensure_dependencies(
            root, frontend=False, interactive=self.interactive, **self.kw)
        return self.result


def _state(root):
    path = root / ".docmind" / "dependency-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


class UserDecidesTests(unittest.TestCase):
    def _decline_once(self, root):
        """真的走一次「用户拒绝」，并当场确认它被记下来了。

        第一版把这几处写成 `with _Box(answers=["n"]): pass` —— **忘了 `.run()`**，
        于是那次「拒绝」根本没发生。后面的断言照样成立（因为根本没被拒绝过），
        守卫就成了装饰：变异 B（拒绝只生效一次）与 F（force 也被拦住）都因此 0 红放过去。

        **写「先做一件事」的上下文时，`with ...: pass` 永远不该出现。**
        """
        with _Box(answers=["n"]) as box:
            box.run(root)
            self.assertTrue(box.result["update_declined"],
                            "这次拒绝没有被记下来 —— 后面的断言就没有意义")
            self.assertEqual(box.labels, [], "拒绝时却装了依赖")

    def test_first_run_installs_without_asking(self):
        """A：首次安装不该打扰用户 —— 没有「上一版」可对比。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            self.assertEqual(box.asked, 0)
            self.assertIn("同步 Python 依赖", box.labels)
            self.assertEqual(_state(root).get("installed_version"), "0.1.0-beta.1")

    def test_same_version_is_never_asked(self):
        """B：版本没动就别问。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            with _Box() as box2:
                box2.run(root)
            self.assertEqual(box2.asked, 0)

    def test_a_requirements_change_inside_one_version_does_not_ask(self):
        """C：同一版本号下继续开发（清单变了、版本没变）不该被打扰。

        这条很容易被写错成「哈希变了就问」——那等于每次开发都弹一次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            (root / "requirements.txt").write_text("aaa==1.1\n", encoding="utf-8")
            with _Box() as box2:
                box2.run(root)
            self.assertEqual(box2.asked, 0, "版本没变却问了 —— 同一版本内的开发被打扰")
            self.assertIn("同步 Python 依赖", box2.labels)

    def test_a_new_version_asks_and_installing_on_yes(self):
        """E：版本变了要问；同意就装，并把版本记成已装。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            (root / "VERSION").write_text("0.1.0-beta.2", encoding="utf-8")
            (root / "requirements.txt").write_text("aaa==2.0\n", encoding="utf-8")
            with _Box(answers=["y"]) as box2:
                box2.run(root)
            self.assertEqual(box2.asked, 1)
            self.assertIn("同步 Python 依赖", box2.labels)
            self.assertEqual(_state(root).get("installed_version"), "0.1.0-beta.2")

    def test_declining_installs_nothing(self):
        """D：拒绝就是不装。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            (root / "VERSION").write_text("0.1.0-beta.2", encoding="utf-8")
            (root / "requirements.txt").write_text("aaa==2.0\n", encoding="utf-8")
            with _Box(answers=["n"]) as box2:
                result = box2.run(root)
            self.assertEqual(box2.asked, 1)
            self.assertEqual(box2.labels, [], "拒绝了却还是装了依赖")
            self.assertTrue(result["update_declined"])
            self.assertEqual(_state(root).get("declined_version"), "0.1.0-beta.2")

    def test_reverse_guard_declining_keeps_working_and_never_sneaks_install(self):
        """**反向守卫**：拒绝之后**不许偷偷装**。

        第一版把拒绝记成「本次跳过」（`requirements_hash` 存旧值），看着对。实测下一启动：

            又问了吗 = 0   装了 = ['同步 Python 依赖']

        哈希仍对不上 -> 又要同步 -> 又不问（已否决）-> **默默装上了**。于是决定权
        只在第一次生效，等于没有。这条守住「后续启动连装都不许发生」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            (root / "VERSION").write_text("0.1.0-beta.2", encoding="utf-8")
            (root / "requirements.txt").write_text("aaa==2.0\n", encoding="utf-8")
            self._decline_once(root)
            with _Box(answers=["n"]) as second:
                second.run(root)
            # 关键判据是**没装**。至于还问不问：那取决于实现，
            # 问一次给出「重新考虑」的机会也算合理，所以不拿它当硬要求。
            self.assertEqual(second.labels, [],
                             "已拒绝却仍然安装了依赖 —— 用户的否决被绕过了")
            self.assertTrue(second.result["update_declined"],
                            "这一轮也该被认作「已否决状态」")
            # 「还问不ask」这条我一度写成软断言（问一次给「重新考虑」的机会也算合理），
            # 结果变异 B（`already_declined = False`）0 红放过去了 —— 因为那条路径
            # 正是「不装、但又问一次」。用户明说不升你还反复问，就是没在尊重决定。
            self.assertEqual(second.asked, 0,
                             "已拒绝的版本又被问了一次 —— 每次启动都弹，应用就没法用了")

    def test_force_can_override_a_decline(self):
        """**反向守卫**：「已拒绝」不能是一个连 `--force` 都出不来的死角。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box() as box:
                box.run(root)
            (root / "VERSION").write_text("0.1.0-beta.2", encoding="utf-8")
            (root / "requirements.txt").write_text("aaa==2.0\n", encoding="utf-8")
            self._decline_once(root)
            with _Box(force=True) as forced:
                forced.run(root)
            self.assertIn("同步 Python 依赖", forced.labels,
                          "用户显式 --force 时必须能越过否决")

    def test_non_interactive_never_blocks_and_still_installs(self):
        """F：CI / 无人值守时不能挂在等输入上，也**不能**因为「不问」就跳过依赖。

        反向：把非交互当成「跳过」是个很自然但**危险**的简化 —— 那样 CI 会在旧依赖上
        跑测试，绿得毫无意义。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            with _Box(interactive=False) as box:
                box.run(root)
            (root / "VERSION").write_text("0.1.0-beta.2", encoding="utf-8")
            (root / "requirements.txt").write_text("aaa==2.0\n", encoding="utf-8")
            with _Box(interactive=False) as box2:
                box2.run(root)
            self.assertEqual(box2.asked, 0)
            self.assertIn("同步 Python 依赖", box2.labels,
                          "非交互时静默跳过依赖 —— CI 会在旧依赖上跑，绿得没有意义")

    def test_the_real_console_prompt_returns_yes_when_stdin_is_not_a_tty(self):
        """**反向守卫**：直接测真正的 `_console_prompt`，不 patch 掉它。

        上面那条 `interactive=False` 只走到「根本不问」的分支 —— `_console_prompt`
        本身从没被执行过。于是变异 C（把非交互分支改成 `return False`，即静默跳过
        依赖）0 红放过去：测试根本没进到那个分支里。

        这里用假 stdin 直接打真实的那个函数。
        """
        import io as _io
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")

            class _NotATty(_io.StringIO):
                def isatty(self):
                    return False

            with patch.object(sys, "stdin", _NotATty("")):
                self.assertTrue(bootstrap._console_prompt(root, "摘要"),
                                "非交互时必须直接升级，不许静默跳过")

            class _Tty(_io.StringIO):
                def isatty(self):
                    return True

            with patch.object(sys, "stdin", _Tty("n\n")):
                self.assertFalse(bootstrap._console_prompt(root, "摘要"),
                                 "交互模式下回答 n 必须是不升级")

            # 空输入（直接回车）= 默认同意
            with patch.object(sys, "stdin", _Tty("\n")):
                self.assertTrue(bootstrap._console_prompt(root, "摘要"),
                                "直接回车应当按同意处理（默认装上，符合旧行为）")

            # EOF / 输入被关掉：当成跳过，而不是崩掉或硬装
            with patch.object(sys, "stdin", _Tty("")):
                self.assertFalse(bootstrap._console_prompt(root, "摘要"),
                                 "输入被关掉时应当跳过，不该崩也不该硬装")

    def test_choosing_details_prints_the_changes_and_asks_again(self):
        """`[d]` 是「先看详情」：打完明细**再问一次**，不当作同意也不当作拒绝。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            bootstrap._write_snapshot(root, bootstrap.SNAPSHOT_REQUIREMENTS,
                                      "aaa==1.0\n")
            (root / "requirements.txt").write_text("aaa==1.0\nfastapi==0.145.0\n",
                                                   encoding="utf-8")
            import io as _io

            class _Tty(_io.StringIO):
                def isatty(self):
                    return True

            import builtins  # 放在使用之前：第一版写在 spy_input 下面 -> UnboundLocalError
            printed = []
            prompts = []
            real_input = builtins.input

            def spy_input(prompt=""):
                prompts.append(prompt)
                return real_input(prompt)

            with patch.object(sys, "stdin", _Tty("d\nn\n")), \
                    patch("builtins.print", side_effect=lambda *a, **k: printed.append(
                        " ".join(str(x) for x in a))), \
                    patch("builtins.input", side_effect=spy_input):
                decided = bootstrap._console_prompt(root, "摘要")
            self.assertFalse(decided, "看完详情后回答 n 仍应是不升级")
            self.assertTrue(any("fastapi" in line for line in printed),
                            "按 [d] 之后必须把具体变更打出来：%r" % printed)
            # 提示语走 `input()` 的 prompt 参数、不经 `print()`。第一版只patch 了
            # print 就想数「问了几次」，于是断言永远失败 —— 那是我观测手段错了，
            # 不是实现错了（实测输出里明细和第二次提问都在）。
            self.assertEqual(len(prompts), 2,
                             "看完详情要**再问一次**，不能自己替用户决定：%r" % prompts)


class VersionIdentityTests(unittest.TestCase):
    """版本号必须存在、可读、且测试阶段照实标「测试版」。"""

    def test_the_repo_ships_an_explicit_version(self):
        repo = Path(__file__).resolve().parent.parent
        self.assertTrue((repo / "VERSION").is_file(),
                        "仓库必须有 VERSION 文件 —— 依赖清单的哈希对人没有意义")
        self.assertTrue(bootstrap.read_version(repo))

    def test_a_prerelease_is_labelled_as_such(self):
        self.assertIn("测试版", bootstrap.version_label("0.1.0-beta.1"))
        self.assertIn("测试版", bootstrap.version_label("0.2.0-alpha"))
        self.assertNotIn("测试版", bootstrap.version_label("1.0.0"))

    def test_a_missing_version_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bootstrap.read_version(Path(tmp)), "")
            self.assertEqual(bootstrap.version_label(""), "未标版本")

    def test_the_version_is_not_guessed_from_the_requirements_hash(self):
        """版本身份不许从哈希推导 —— 那样用户看到的是 `af9a783f…`。"""
        self.assertNotIn("sha", bootstrap.version_label("0.1.0-beta.1").lower())
        self.assertIn("0.1.0", bootstrap.version_label("0.1.0-beta.1"))


class DetailsViewTests(unittest.TestCase):
    """详情视图：报「有新版本」之外，还得能说出**具体更新了什么**。"""

    def test_it_names_added_removed_and_upgraded_packages(self):
        old = "aaa==1.0\nbbb==2.0\nccc==3.0\n"
        new = "aaa==1.1\nbbb==2.0\nddd==4.0\n"
        out = bootstrap.requirement_changes(old, new)
        self.assertEqual(out["changed"], ["aaa"])
        self.assertEqual(out["added"], ["ddd"])
        self.assertEqual(out["removed"], ["ccc"])

    def test_comments_and_blank_lines_are_not_packages(self):
        out = bootstrap.requirement_changes("# 说明\n\naaa==1.0\n", "aaa==1.0\n")
        self.assertEqual(out["added"], [])
        self.assertEqual(out["removed"], [])
        self.assertEqual(out["changed"], [])

    def test_the_details_view_uses_the_last_synced_snapshot(self):
        """详情要跟**上一次真正装进去的**清单比，不是跟空比。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n")
            bootstrap._write_snapshot(root, bootstrap.SNAPSHOT_REQUIREMENTS,
                                      "aaa==1.0\n")
            (root / "requirements.txt").write_text("aaa==1.0\nfastapi==0.145.0\n",
                                                   encoding="utf-8")
            text = bootstrap.format_update_details(root)
            self.assertIn("+ fastapi", text)
            # 已经装进去的 aaa==1.0 不该被报成新增
            self.assertNotIn("+ aaa", text)
            self.assertIn("无变化", text)   # 前端那半没变，要说清楚

    def test_an_unparsable_lock_file_is_reported_not_crashed(self):
        out = bootstrap.frontend_changes("not json", '{"packages":{}}')
        self.assertFalse(out["parsed"])
        self.assertEqual(out["changed"], [])

    def test_reverse_guard_no_history_means_no_invented_counts(self):
        """**反向守卫**：没有可比的历史时**不许**报变更项数。

        快照是随本功能引入的，老安装第一次进来没有历史可比。实测那时的输出是
        「新增 41 项、前端新增 259 项」—— 全是假账（所有包都被算成「新增」）。
        谎报的详情比没有详情更坏：用户会以为这一版改了 41 个包。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.2", "aaa==1.0\nfastapi==0.145.0\n",
                  lock='{"packages":{"x":{"version":"1.0.0"}}}')
            # **故意不写快照** —— 模拟「首次运行，没有历史」
            details = bootstrap.format_update_details(root)
            summary = bootstrap.format_update_summary(root, "0.1.0-beta.1", "0.1.0-beta.2")
            self.assertIn("没有可比", details)
            self.assertNotIn("+ aaa", details)
            self.assertIn("没有可比", summary)
            self.assertNotIn("变更 2 项", summary)
            self.assertNotIn("变更 1 项", summary)
            # 版本信息仍然要给 —— 那部分不是猜的
            self.assertIn("0.1.0-beta.2", summary)

    def test_the_summary_counts_both_halves(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root, "0.1.0-beta.1", "aaa==1.0\n",
                  lock='{"packages":{"x":{"version":"1.0.0"},"y":{"version":"2.0.0"}}}')
            bootstrap._write_snapshot(root, bootstrap.SNAPSHOT_REQUIREMENTS, "aaa==1.0\n")
            bootstrap._write_snapshot(
                root, bootstrap.SNAPSHOT_LOCK,
                '{"packages":{"x":{"version":"1.0.0"},"y":{"version":"1.0.0"}}}')
            (root / "requirements.txt").write_text("aaa==1.0\nbbb==1.0\n", encoding="utf-8")
            (root / "frontend" / "package-lock.json").write_text(
                '{"packages":{"x":{"version":"1.0.0"},"y":{"version":"2.0.0"}}}', encoding="utf-8")
            summary = bootstrap.format_update_summary(root, "0.1.0-beta.1", "0.1.0-beta.2")
            self.assertIn("Python 依赖变更 1 项", summary)
            self.assertIn("前端依赖变更 1 项", summary)
            self.assertIn("0.1.0-beta.2", summary)


if __name__ == "__main__":
    unittest.main()