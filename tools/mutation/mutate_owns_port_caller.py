"""变异验证：`test_it_still_has_no_production_caller` 会咬吗？

造一个真实的 `owns_port(...)` 调用，看那条守卫是否报红。
用行号定位插入 —— 之前按字符串匹配失败过（反引号与编码）。
"""
from __future__ import annotations

import io
import subprocess
import sys

TARGET = "agent_runtime/visual_acceptance.py"


def run() -> str:
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_dev_server_owns_port.py",
         "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [ln for ln in r.stdout.splitlines() if ln.strip()][-1].strip()


def main() -> None:
    bak = io.open(TARGET, "rb").read()
    lines = bak.decode("utf-8").split("\n")
    # 必须插到**函数体内**。第一版插在 docstring 里（那一行在 `"""` 之前），
    # 插入的语句根本不在可执行路径上 —— 那是**无效变异**，
    # 「没被抓住」不代表守卫漏。这里用语法检查确认插入点合法。
    hit = None
    in_doc = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        if "probe = probe or dev_server.probe" in line:
            hit = i
            break
    if hit is None:
        print("  *** 没找到函数体内的插入点 ***")
        return
    indent = len(lines[hit]) - len(lines[hit].lstrip())
    lines.insert(hit + 1,
                 " " * indent + "_probe = dev_server.owns_port(5173, markers=['x'])  # MUTATED")
    io.open(TARGET, "w", encoding="utf-8", newline="").write("\n".join(lines))
    try:
        import ast
        ast.parse(io.open(TARGET, encoding="utf-8").read())
        print("  插入点语法 ok（行 %d）" % (hit + 1))
        print("  变异「插入真实调用」: %s" % run())
    finally:
        io.open(TARGET, "wb").write(bak)
    print("  复原后             : %s" % run())


if __name__ == "__main__":
    main()