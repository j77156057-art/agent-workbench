"""守卫：等待逻辑在「pid 还没写完」时必须**等**，而不是拿空串继续。

## 为什么需要单独造这个场景

变异实测发现：把等待逻辑改回旧写法（`exists()` + `or "0"`）**在空载下咬不住**
——竞态窗口命中率太低（复刻 60/60 没撞上）。

所以不能靠「跑一遍绿」来说明改动有效。得**人工制造**那个窗口，让两种写法
必然分出高下：父进程先建文件、**停一下**、再写 pid。

    旧写法（exists + or "0"）  -> 立刻读到空串 -> pid=0 -> pid_alive(0)=True
                                 -> 最后那句 assertFalse **恒失败**
    新写法（等结束标记）        -> 一直等到 pid 真的写完 -> 正常走完

这个场景把「窗口是否存在」从概率问题变成确定问题。

## 它守的是哪个约定

`read_text()` 在文件被 `open()` 但还没 `write()` 时返回空串。原来的
`int(... or "0")` 把这个空串兜成 pid 0，而 `pid_alive(0)` 恒为 True ——
于是断言恒红，而被测机制（作业对象回收）根本没问题。

这里把「等一个写完的 pid」抽成可测的纯等待逻辑，直接喂它一个**故意延迟写入**
的目录，验证它返回的是**真实 pid**而不是 0。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import temp_state  # noqa: E402


def wait_for_written_pid(pidfile: pathlib.Path, deadline_s: float) -> int:
    """等 pidfile 里出现**带结束标记的正 pid**。

    这是从 `test_visual_acceptance_runtime.py` 里那段等待循环原样搬出来的 ——
    搬出来是为了能**人工制造**写入延迟，而不用去真的起进程碰运气。
    """
    deadline = time.time() + deadline_s
    raw = ""
    while time.time() < deadline:
        try:
            raw = pidfile.read_text()
        except OSError:
            raw = ""
        if raw.endswith("\n") and raw.strip().isdigit():
            return int(raw.strip())
        time.sleep(0.01)
    return 0


def _fake_written_pid() -> int:
    """拿一个当前确实活着的进程 pid 当「孙进程」。"""
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    # 不 wait()，让它活着供本模块使用；由 atexit 之外的模块级引用保持
    _KEEP.append(child)
    return child.pid


_KEEP: list = []


def test_it_waits_for_a_pid_that_is_actually_written() -> None:
    """核心：文件已存在但**内容还是空的**时，必须继续等。"""
    with tempfile.TemporaryDirectory() as root:
        pidfile = pathlib.Path(root) / "child.txt"
        real = _fake_written_pid()

        # 人工制造窗口：先建文件（exists() 立刻为真），**再**过一会儿才写 pid。
        handle = pidfile.open("w")
        try:
            assert pidfile.exists(), "前置条件：文件此刻已存在"
            assert pidfile.read_text() == "", "前置条件：此刻内容确实是空的"

            def _writer() -> None:
                time.sleep(0.4)
                handle.write(str(real) + "\n")
                handle.close()

            import threading

            threading.Thread(target=_writer, daemon=True).start()
            got = wait_for_written_pid(pidfile, deadline_s=10)
        finally:
            try:
                handle.close()
            except OSError:
                pass

    assert got == real, (
        f"等到了 pid={got}，期望 {real}。"
        "若得到 0，说明把「还没写完」当成了「pid 是 0」—— "
        "而 temp_state.pid_alive(0) 恒为 True，下游断言会恒红"
    )


def test_an_unwritten_file_times_out_to_zero_rather_than_pretending() -> None:
    """反向守卫：真的没写出来时，返回 0，**由调用方明确判失败**。

    这条不许自己悄悄返回一个看起来合法的 pid —— 那比返回 0 危险得多。
    """
    with tempfile.TemporaryDirectory() as root:
        pidfile = pathlib.Path(root) / "child.txt"
        pidfile.write_text("")          # 建了，永远不写
        started = time.time()
        got = wait_for_written_pid(pidfile, deadline_s=1.0)
        elapsed = time.time() - started

    assert got == 0, f"应当超时返回 0，却返回了 {got}"
    assert 0.9 <= elapsed <= 5.0, f"应当在 ~1 秒后放弃，实际 {elapsed:.2f} 秒"


def test_a_truncated_pid_is_not_accepted_as_complete() -> None:
    """反向守卫：半截数字不算写完。

    写入被打断时可能读到「12」（真实是「54321」）。若照单全收，`pid_alive(12)`
    说的是**系统里另一个进程**，下游断言照样恒红 —— 而且更难查。
    """
    with tempfile.TemporaryDirectory() as root:
        pidfile = pathlib.Path(root) / "child.txt"
        pidfile.write_text("12")          # 半截，没有结束标记
        got = wait_for_written_pid(pidfile, deadline_s=1.0)

    assert got == 0, (
        f"半截 pid「12」被当成写完了，返回 {got}。"
        "结束标记（末尾换行）就是用来挡这个的"
    )


def test_pid_alive_zero_is_always_true_so_a_zero_pid_is_never_meaningful() -> None:
    """把「为什么 0 是毒值」钉死，免得有人去改 `pid_alive`。

    `pid_alive(0) == True` 是它刻意的失败关闭设计（宁可不删，绝不误删），
    `tests/test_temp_state.py` 也有一条守卫。**正因为它恒真**，把 0 当成
    「孙进程的 pid」会让下游断言恒红 —— 修法在等待端，不在探针端。
    """
    assert temp_state.pid_alive(0) is True
    assert temp_state.pid_alive(-1) is True
    assert temp_state.pid_alive(-99999) is True