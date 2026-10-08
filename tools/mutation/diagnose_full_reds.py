"""核实：这次全量的 7 条红分别是什么，以及有没有一条是我的代码缺陷。

三类分开查，不混为一谈 —— 它们根因完全不同。
"""
from __future__ import annotations

import io
import pathlib
import re
import subprocess
import sys

#: 仓库根。这个脚本在 `tools/mutation/` 下，**要往上三层**——
#: `parents[1]` 会得到 `tools/`，于是 sys.path 插错、找不到 `tests/` 与 `agent.py`。
#: 我第一版就写错了，症状是「新文件存在? False」+「No module named 'agent'」。
ROOT = pathlib.Path(__file__).resolve().parents[2]


def read(p: str) -> str:
    return io.open(str(ROOT / p), encoding="utf-8").read()


def mtimes() -> tuple[float, float]:
    web = max((p.stat().st_mtime for p in (ROOT / "web").rglob("*.js") if p.is_file()),
              default=0.0)
    src = max((p.stat().st_mtime
               for p in (ROOT / "frontend/src").rglob("*")
               if p.is_file() and p.suffix in (".vue", ".ts", ".tsx")), default=0.0)
    return web, src


def run(targets: list[str]) -> str:
    r = subprocess.run([sys.executable, "-m", "pytest", *targets,
                        "-q", "--no-header", "-p", "no:cacheprovider"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return [ln for ln in r.stdout.splitlines() if ln.strip()][-1].strip()


def main() -> None:
    print("  === 段 5：脚本按旧文件名收集（我改名时全量正在跑）===")
    tail = io.open(r"D:\Temp\opencode\full_seg5.txt", encoding="utf-8").read()
    for line in tail.splitlines():
        if "not found" in line or "no tests ran" in line:
            print("    %s" % line.strip()[:96])
    old = ROOT / "tests/test_tool_search_expands_visibility.py"
    new = ROOT / "tests/test_tool_search_usage_only.py"
    print("    旧文件存在? %s   新文件存在? %s" % (old.exists(), new.exists()))

    print()
    print("  === 段 1 的 channel_protocol 4 条：单跑 ===")
    print("    %s" % run(["tests/test_channel_protocol_consistency.py"]))

    print()
    print("  === 那 4 条查的字串现在还在吗 ===")
    sys.path.insert(0, str(ROOT))
    import agent as A
    src = __import__("inspect").getsource(A.Agent._run)
    for probe in ("原生 function-calling", "模型最后一次输出",
                  "不要当作结论", "不要写任何 Action:"):
        print("    %-24s %s" % (probe, probe in src))

    print()
    print("  === 段 2 的 bundle 新鲜度：现在的状态 ===")
    web, src_m = mtimes()
    print("    web %s / src %s / fresh=%s" % (web, src_m, web >= src_m))
    print("    （全量跑那一刻前端 agent 正在改 frontend/src 并重建 ——")
    print("      我未参与那次改动，属共享工作树的竞态）")

    print()
    print("  === 段 1 的 app_test 2 条 ===")
    print("    %s" % run(["tests/test_app_test.py::LivePageTests"]))


if __name__ == "__main__":
    main()