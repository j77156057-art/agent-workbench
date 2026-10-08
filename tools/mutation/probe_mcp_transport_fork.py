"""核实：stdio 与 http 两个 transport 是否真共用一份契约？

这是 conformance suite 该不该做的**决定性判据**：

* 共用一条调用路径、只在传输层分叉 -> 已有事实上的单一实现，加套件价值低
* 各自分支实现同一契约 -> 行为可能漂移，套件正是解药

我先前判「只有一个实现、加套件是仪式」—— 那���探针没查 transport 就下的结论。
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "agent_runtime/mcp_bridge.py"


def main() -> None:
    text = BRIDGE.read_text(encoding="utf-8")
    lines = text.splitlines()

    # 1) 哪些行按 transport 分叉
    fork_lines = [(i + 1, l.strip()) for i, l in enumerate(lines)
                  if '"stdio"' in l or '"http"' in l
                  or "'stdio'" in l or "'http'" in l]
    print("  === 按 transport 分叉的位置（前 12 处）===")
    for ln, snippet in fork_lines[:12]:
        print("    %4d| %s" % (ln, snippet[:88]))
    print("  共 %d 处" % len(fork_lines))

    # 2) 分叉是否收敛到少数几个函数里
    tree = ast.parse(text)
    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            if '"stdio"' in body or "'stdio'" in body or '"http"' in body or "'http'" in body:
                funcs.append((node.name, node.lineno, node.end_lineno))
    print()
    print("  === 哪些函数内部按 transport 分叉 ===")
    for name, a, b in funcs:
        print("    %-28s 行 %d..%d" % (name, a, b))

    print()
    print("  === 判定 ===")
    if len(funcs) <= 2:
        print("  分叉收敛在 %d 个函数里 -> **事实上的单一实现**。" % len(funcs))
        print("  加 conformance suite 的价值有限：真出问题会直接在那 %d 个函数里红。" % len(funcs))
        print("  建议：**暂不做**，等分叉扩散到更多函数、或出现第三个 transport。")
    else:
        print("  分叉散在 %d 个函数里 -> 行为漂移的风险是真的，" % len(funcs))
        print("  conformance suite 有意义（正是 Pi telemetry 那套要解决的问题）。")
        print("  建议：**值得做**，先为这两个 transport 各写一条对同一夹具的用例。")


if __name__ == "__main__":
    main()