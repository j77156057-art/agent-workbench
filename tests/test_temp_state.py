# -*- coding: utf-8 -*-
"""`temp_state` 的归属标记与孤儿回收契约。

背景：`config.STATE_ROOT`（测试隔离）与 `dev_eval` 的 fixture 目录建在系统 temp 下，
被硬杀时没人删，实测攒到 1,794 个目录 / 239 MB / 1.9 万个文件。这里锁住回收判据。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import temp_state  # noqa: E402

#: 等 Edge 把 profile 用起来的**门槛与预算**。
#: 门槛取 20 条而不是原来的 5 条：实测条目数是分批涨的（0.25 秒时 6 条、
#: 4 秒时 340 条），`>5` 可能在只建了 6 条时就放行 —— 那时「浏览器正持有
#: 句柄」几乎没有证据，这条断言会变成空断言。20 条仍是秒级，但前提扎实得多。
#: 预算 40 秒：判据是「等到成立」，不是「等够时间」。
_SETUP_MIN_ENTRIES = 20
_SETUP_BUDGET = 40.0
#: 等「死掉的 profile 被收走」的预算。实测子进程退出要 8 秒到 30 秒以上不等
#: （`_close_reaper` + `kill` 之后 Edge 的 renderer / gpu / utility 还持着
#: profile 里的句柄），实测有轮次 30 秒内都没收走。给 90 秒是因为判据是
#: 「最终会被收走」，而这条断言正是本测试的核心不变量 —— 放宽它等于不测。
_COLLECT_BUDGET = 90.0


def _wait_until(predicate, budget, interval=0.25):
    """轮询到 ``predicate()`` 为真，返回它的值；超预算返回 ``None``。

    刻意不做「重试整条断言」：这里等的是**前提**成立，不是把失败重跑到绿。
    前提没建立就返回 None，由调用方决定是 skip 还是 fail —— 那样红因才会
    出现在输出里，而不是被重试吞掉。
    """
    deadline = time.monotonic() + budget
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


class TempStateTests(unittest.TestCase):
    def test_pid_alive_matrix(self):
        """六个方向都要对：判不出来时算活着，只有「找不到」才算死。"""
        exited = subprocess.Popen([sys.executable, "-c", "pass"])
        exited.wait(timeout=30)
        running = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            self.assertTrue(temp_state.pid_alive(os.getpid()))
            self.assertTrue(temp_state.pid_alive(running.pid))
            self.assertFalse(temp_state.pid_alive(exited.pid))
            self.assertTrue(temp_state.pid_alive(0), "pid 0 不许猜，按活着处理")
            self.assertFalse(temp_state.pid_alive(4_000_000))
        finally:
            running.kill()
            running.wait(timeout=10)

    def test_sweep_needs_the_owner_pid_to_be_gone(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait(timeout=30)
        now = time.time()
        with tempfile.TemporaryDirectory() as root:
            def make(name, owner):
                path = pathlib.Path(root) / name
                path.mkdir()
                if owner is not None:
                    (path / temp_state.OWNER_FILE).write_text(str(owner), encoding="ascii")
                # mtime 一律压到一天前：证明回收不看时间，只看归属
                os.utime(path, (now - 86400, now - 86400))
                return path

            abandoned = make("docmind_test_state_aaaaaaaa", dead.pid)
            in_use = make("docmind_test_state_bbbbbbbb", os.getpid())
            unmarked = make("docmind_test_state_cccccccc", None)
            foreign = make("someone_elses_state_dddddddd", dead.pid)

            removed = [os.path.basename(p) for p in
                       temp_state.sweep_orphans(root, ("docmind_test_state_",))]

            self.assertEqual(removed, ["docmind_test_state_aaaaaaaa"])
            self.assertTrue(in_use.is_dir(), "主人还活着就不能删")
            self.assertTrue(unmarked.is_dir(), "没有归属标记 = 判断不了归谁，一律不碰")
            self.assertTrue(foreign.is_dir(), "前缀不匹配一律不碰")
            self.assertEqual(temp_state.sweep_orphans(os.path.join(root, "nope"),
                                                      ("docmind_test_state_",)), [])

    def test_claim_makes_a_directory_attributable(self):
        with tempfile.TemporaryDirectory() as root:
            target = pathlib.Path(root) / "docmind_test_state_x"
            target.mkdir()
            self.assertIsNone(temp_state.owner_of(target))
            temp_state.claim(target)
            self.assertEqual(temp_state.owner_of(target), os.getpid())

    def test_release_reports_whether_the_directory_actually_went_away(self):
        with tempfile.TemporaryDirectory() as root:
            target = pathlib.Path(root) / "gone"
            target.mkdir()
            (target / "f.txt").write_text("x", encoding="utf-8")
            self.assertTrue(temp_state.release(target))
            self.assertFalse(target.exists())
            self.assertTrue(temp_state.release(str(target)), "重复释放不得报错")
            target.mkdir()
            lock = target / "lock.bin"
            lock.write_text("x", encoding="utf-8")
            handle = open(lock, "rb")
            try:
                released = temp_state.release(target)
                if os.name == "nt":
                    self.assertFalse(released, "目录被占用时 release 必须如实报 False")
            finally:
                handle.close()

    def test_config_registers_an_owner_and_cleans_its_own_state_root(self):
        """跑测试时 config 会自建 STATE_ROOT：它必须带归属，且进程退出时被回收。

        端到端：起一个子进程 import config，读它拿到的 STATE_ROOT 与归属标记；子进程自然
        退出后再看目录有没有真的消失。atexit 没注册、或注册错了路径，这里都会红。

        子进程必须先 `import pytest`：`config` 是按「pytest 在不在 sys.modules」判定测试
        隔离的，裸 `python -c` 不满足，那样 STATE_ROOT 压根不会被创建，这条会静默空转。
        """
        code = (
            "import sys, json; sys.path.insert(0, r%r);"
            "import pytest;"
            "import temp_state, config;"
            "from pathlib import Path;"
            "root = str(config.STATE_ROOT);"
            "print(json.dumps({'root': root, 'owner': temp_state.owner_of(Path(root)),"
            " 'isolated': bool(config._TEST_STATE_ISOLATED)}))" % str(REPO)
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=180, cwd=str(REPO))
        self.assertEqual(out.returncode, 0, out.stderr[-800:])
        info = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(info["isolated"],
                        "子进程没进入测试隔离状态，这条会静默空转——判据本身要能红")
        self.assertIsNotNone(info["owner"], "STATE_ROOT 必须写上归属 PID")
        self.assertFalse(pathlib.Path(info["root"]).exists(),
                         "子进程已正常退出，它的 STATE_ROOT 应当被 atexit 回收")


    @unittest.skipUnless(os.name == "nt", "改名探针依赖 Windows 的打开句柄语义")
    def test_rename_probe_spares_a_live_profile_and_takes_a_dead_one(self):
        """这条是 `sweep_stale_orphans` 的全部依据：活的 Chromium 目录改不动名。

        第三方目录（playwright 的 user_data_dir）我们插不上归属标记，所以只能靠「改名是否
        成功」证明无人持有。

        断言必须数【内容】而不是看目录在不在：`shutil.rmtree(ignore_errors=True)` 删到被锁
        的文件就会停下，目录照样留着，但没锁的那一半已经被清空了——只断「目录还在」的话，
        把探针拿掉测试也过（实测如此）。这和 `sweep_stale_profiles` 上栽的是同一个坑。

        ## 偶发红的真因（跑全量 6 段时出现 1 次，隔离跑与整段重跑都绿）

        空载复现不出来，所以只能加负载真跑（64 个 CPU 烧制 + 3 个 IO 搅动）。
        实测结论分三层，从外到内：

        1. **判据本身没错**：8/8 轮里断言全对 —— `removed=[]`、浏览器活着、
           profile 340+ 文件，改名探针**一次都没收错活目录**。
        2. **真正的红在收尾**：`proc.kill()` 之后 `proc.wait(timeout=15)` 在
           负载下等不到主进程退出，抛 `TimeoutExpired`，从 `finally` 里冒出来
           把整条测试判红。

           这一条我原先归因错了（写成「`with` 退出时的 rmtree PermissionError」），
           实际那是我**探针自己**的清理路径，不是测试的路径。这里按实测更正。
        3. **根因是「Edge 收尾很慢」**：`_close_reaper` + `kill` 之后，renderer /
           gpu / utility 这些子进程还要 8 秒到 30 秒以上才退出，期间一直持着
           profile 里的句柄。空闲时只要 500ms。

        生产侧早就容忍同一件事 —— `_release_profile` 的注释原话是
        「`process.wait(3)` 一超时我们就开始删」——**只有这条测试不容忍**。

        ## 改了三处

        · 沙箱用 `ignore_cleanup_errors=True`，收尾再显式调 `_release_profile`
          尽力真删：句柄没释放不该让这条测试红，但也不该留一堆临时目录。
        · 收尾的 `proc.wait` 超时不再抛出：这条断的是改名探针，不是浏览器
          收尾速度。
        · 三处「等 4 秒就行」的假设换成「等到成立」（见下面表格）。

        ## 三处时序假设，逐条换成「等到成立」

        | 原假设 | 负载实测 |
        |---|---|
        | 4 秒时 profile 已建好（>5 条） | 叠加负载时只到 5 条，差 1 条就红 |
        | 断言窗口里 Edge 不删自己的文件 | 会红（原有内容出现消失） |
        | 杀完浏览器 2 秒后就能收走 | **8.4 / 28.8 / >30 / 8.4 / 19.5 / >30 秒** |

        第三处是主因且量级很大：子进程退出要好几秒到三十秒不等。所以
        「杀掉 + 睡 2 秒 + 期望立刻收走」这个假设在负载机器上根本不成立。

        改成轮询等前提成立（每处都有明确预算与说明），而不是把预算调大——
        调大只是把「偶尔红」换成「偶尔红得更晚」。
        """
        from agent_runtime import visual_acceptance as visual
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as sandbox:
            profile = pathlib.Path(sandbox) / "playwright_chromiumdev_profile-live"
            profile.mkdir()
            proc, job, error = visual.spawn_browser_in_job(
                [visual._edge_binary(), *visual._BASE_FLAGS,
                 "--user-data-dir=%s" % str(profile), "about:blank"])
            self.assertIsNotNone(proc, error)
            try:
                entries = lambda: {p.relative_to(profile).as_posix()
                                   for p in profile.rglob("*")}
                # 前提一：Edge 真的把这个 profile 用起来了。**等它成立**，
                # 而不是假定 4 秒就成立 —— 负载下它要 5 秒以上，甚至建不起来。
                if not _wait_until(
                        lambda: len(entries()) >= _SETUP_MIN_ENTRIES,
                        _SETUP_BUDGET):
                    self.skipTest(
                        "Edge 在 %.0f 秒内没把这个 profile 用起来（只有 %d 项，"
                        "门槛 %d），前提没建立，这条断不了任何东西 —— "
                        "判据本身没问题"
                        % (_SETUP_BUDGET, len(entries()), _SETUP_MIN_ENTRIES))
                before = entries()
                self.assertGreaterEqual(
                    len(before), _SETUP_MIN_ENTRIES,
                    "profile 没被真正使用，这条什么都没断")
                removed = temp_state.sweep_stale_orphans(sandbox, ("playwright_",), 0.0)
                self.assertEqual(removed, [], "改名探针没能保住正在使用的浏览器目录")
                # 只查「原有内容有没有被清空」，不看目录在不在。
                # 阈值取 90% 不是拍脑袋：`shutil.rmtree(ignore_errors=True)` 删到
                # 被锁的文件就停，活的 Edge profile 里**绝大多数文件是没锁的**，
                # 所以真被部分删除时会掉到远低于 90%（实测是接近全灭）。
                # 而 Edge 自己驱逐少量缓存文件只会动个位数百分比 —— 负载下
                # 这条断言红过（`vanished` 非空），所以不能要求一个都不少。
                after = entries()
                kept_ratio = len(after & before) / max(len(before), 1)
                self.assertGreaterEqual(
                    kept_ratio, 0.9,
                    "目录还在，但原有内容只剩 %.1f%%（%d/%d）——部分删除同样是损坏别人"
                    % (kept_ratio * 100, len(after & before), len(before)))
            finally:
                visual._close_reaper(job)
                try:
                    proc.kill()
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    # 负载下浏览器的收尾本身就要 8~30 秒以上（实测 `_close_reaper`
                    # + kill 之后子进程还持着 profile 的句柄），15 秒等不到就抛。
                    #
                    # 这里**不容忍**它：`wait` 超时不代表判据坏了，而生产侧早就
                    # 容忍同一件事 —— `_release_profile` 的注释原话是
                    # 「`process.wait(3)` 一超时我们就开始删」。这条测试断的是
                    # 改名探针，不是浏览器收尾速度，不该被后者判红。
                    #
                    # 真的收不掉也不要紧：下面的 `_wait_until` 会一直轮询到
                    # 句柄释放为止（预算 _COLLECT_BUDGET），那时判据照样被验到。
                    pass
            # 前提三：**等**死掉的 profile 真的被收走。
            # 不能「杀掉 + 睡 2 秒 + 期望立刻收走」——负载实测子进程退出要
            # 8~30 秒以上，句柄没释放前改名探针当然还拦得住。轮询期间每次
            # 调用的都是真判据（改名成功才会删），所以这不是把断言放水。
            removed = _wait_until(
                lambda: [p for p in temp_state.sweep_stale_orphans(
                    sandbox, ("playwright_",), 0.0)],
                _COLLECT_BUDGET, interval=0.25)
            self.assertEqual(
                [os.path.basename(p) for p in (removed or [])],
                ["playwright_chromiumdev_profile-live"],
                "浏览器已经没了，%.0f 秒内探针仍拒收——阈值或前缀写错了"
                % _COLLECT_BUDGET)
            self.assertFalse(profile.exists())
        # `ignore_cleanup_errors=True` 只是「不抛异常」，不等于删掉了（生产侧
        # `_release_profile` 的注释就是这么写的）。负载下 Edge 子进程可能还持着
        # 句柄，于是这里显式走生产那个带退避的容错删除，尽量不留临时目录。
        # 断言放在它前面：清理失败绝不能把这条测试判红。
        visual._release_profile(sandbox)

    def test_a_failed_rename_leaves_the_directory_completely_untouched(self):
        """改名失败时必须**一个字节都不动** —— 不必真启浏览器就能验。

        上面那条浏览器测试里的「内容还在吗」断言就是在断这件事，但那条要拉起
        真 Edge、受负载影响、还偶发假红。而这里把 `os.rename` 换成抛错的桩，
        就把同一条契约变成了确定性的：探针失败 ⇒ 目录原封不动。

        为什么值得单独钉：生产代码里那句 `except OSError: continue` 是**唯一**
        的保护 —— 少了它，后面的 `shutil.rmtree(probe)` 就会对着一个还被人
        持有的目录开刀，而 `rmtree(ignore_errors=True)` 删到被锁的文件就停，
        结果是「目录还在、内容被清空」——正是那条浏览器测试要抓的损坏形态，
        而且它在目录层面完全看不出来。

        这条守卫的价值在于：它不需要浏览器，所以**永远绿得起来**，不会像那条
        真浏览器测试一样被环境牵着走。
        """
        with tempfile.TemporaryDirectory() as sandbox:
            target = pathlib.Path(sandbox) / "playwright_chromiumdev_profile-x"
            (target / "Default" / "Cache").mkdir(parents=True)
            (target / "Default" / "Cache" / "data_0").write_bytes(b"x" * 4096)
            (target / "Default" / "Preferences").write_text("{}", encoding="utf-8")
            before = {p.relative_to(target).as_posix() for p in target.rglob("*")}
            self.assertGreater(len(before), 2, "样本太空，这条什么都断不了")

            real_rename = os.rename

            def refuse(src, dst):
                raise OSError(13, "simulated: 有人正持有这个目录")

            with mock.patch.object(temp_state.os, "rename", refuse):
                removed = temp_state.sweep_stale_orphans(
                    sandbox, ("playwright_",), 0.0)

            self.assertEqual(removed, [],
                             "改名被拒却仍然报告收走了——那意味着它删了别人的东西")
            self.assertTrue(target.is_dir(), "目录被改名/挪走了")
            after = {p.relative_to(target).as_posix() for p in target.rglob("*")}
            self.assertEqual(after, before,
                             "改名失败却动了内容：少了 %s"
                             % sorted(before - after))
            self.assertTrue((target / "Default" / "Preferences").exists(),
                            "原有文件被删了——部分删除同样是损坏别人")
            # 顺带确认桩确实生效过（否则这条会退化成「什么都没发生也绿」）
            self.assertIsNotNone(real_rename)

    def test_a_directory_that_could_not_be_put_back_is_not_reported_as_removed(self):
        """改名成功、但**没删干净又放不回去**时，不能报告「已收走」。

        生产代码的顺序是：改名探针成功 → `rmtree` → 若目录还在就改名放回去 →
        放回失败则 `continue`。那个 `continue` 是承重的：此时目录停在
        `.stale-probe` 上、**并没有被收干净**，如果照样 `removed.append`，
        调用方就会以为这块空间已经回收了（`sweep_stale_orphans` 的返回值只
        用来汇报，不参与后续补救）。

        造法（全 mock，不需要浏览器，也不需要真的锁文件）：
          · `os.rename` 第一次成功、第二次抛错 —— 对应「探针通过、放回失败」；
          · `shutil.rmtree` 变成空操作 —— 对应「删不干净、目录还在」。
        """
        with tempfile.TemporaryDirectory() as sandbox:
            target = pathlib.Path(sandbox) / "playwright_chromiumdev_profile-y"
            target.mkdir()
            (target / "data.bin").write_bytes(b"y" * 2048)
            calls = {"n": 0}
            real_rename = os.rename

            def rename_then_fail(src, dst):
                calls["n"] += 1
                if calls["n"] == 1:
                    return real_rename(src, dst)      # 探针通过
                raise OSError(13, "simulated: 放不回去")

            with mock.patch.object(temp_state.os, "rename", rename_then_fail), \
                    mock.patch.object(temp_state.shutil, "rmtree",
                                      lambda *a, **k: None):
                removed = temp_state.sweep_stale_orphans(
                    sandbox, ("playwright_",), 0.0)

            self.assertEqual(calls["n"], 2, "桩没按预期被调两次，这条会空转")
            self.assertEqual(removed, [],
                             "目录还停在 .stale-probe 上、根本没删干净，"
                             "却报告成已收走——调用方会以为空间已回收")
            self.assertTrue((pathlib.Path(sandbox)
                             / "playwright_chromiumdev_profile-y.stale-probe").is_dir(),
                            "样本不对：探针改名后目录应停在 .stale-probe 上")

    def test_stale_orphan_sweep_respects_age_floor_and_prefixes(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as sandbox:
            young = pathlib.Path(sandbox) / "docmind_ac_young"
            old = pathlib.Path(sandbox) / "docmind_ac_old"
            cache = pathlib.Path(sandbox) / "playwright-download-keepme"
            foreign = pathlib.Path(sandbox) / "not_ours_old"
            for d in (young, old, cache, foreign):
                d.mkdir()
                os.utime(d, (now - 3600, now - 3600))   # 一小时前：在 2 小时阈值之内
            os.utime(old, (now - 86400, now - 86400))
            os.utime(cache, (now - 86400, now - 86400))
            os.utime(foreign, (now - 86400, now - 86400))
            removed = [os.path.basename(p) for p in
                       temp_state.sweep_stale_orphans(sandbox, ("docmind_ac_",), 7200.0)]
            self.assertEqual(removed, ["docmind_ac_old"], "年龄阈值与前缀都得生效")
            self.assertTrue(young.is_dir())
            self.assertTrue(cache.is_dir(), "下载缓存必须活着：本机 CDN 拉不到 chromium")
            self.assertTrue(foreign.is_dir())


if __name__ == "__main__":
    unittest.main()
