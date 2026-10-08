"""核实 4.2① 的前提：MCP connector 有几个实现？

「加 conformance suite」只有在**存在第二个实现**时才值得 ——
那是套件要解决的问题（行为漂移）。只有一个实现时，加套件是仪式。

同时核实 preview adapter 那条边界有几个实现。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

TARGETS = {
    "mcp_bridge": ROOT / "agent_runtime/mcp_bridge.py",
    "mcp_server": ROOT / "agent_runtime/mcp_server.py",
    "adapter_catalog": ROOT / "agent_runtime/adapter_catalog.py",
}

TRANSPORT = re.compile(r"[\"'](stdio|http|sse|streamable|websocket)[\"']")


def main() -> None:
    for label, path in TARGETS.items():
        if not path.exists():
            print("  %-16s *** 文件不存在 ***" % label)
            continue
        text = path.read_text(encoding="utf-8")
        kinds = sorted(set(TRANSPORT.findall(text)))
        print("  %-16s %5d 行   transport 取值: %s"
              % (label, len(text.splitlines()), kinds or "(无字面量)"))

    print()
    print("  === 结论依据 ===")
    bridge = TARGETS["mcp_bridge"]
    if bridge.exists():
        text = bridge.read_text(encoding="utf-8")
        n_impl = len(re.findall(r"^\s*(?:async\s+)?def\s+\w+", text, re.M))
        print("  mcp_bridge 里的函数数: %d" % n_impl)
        print("  transport 有 %d 种: %s" % (len(set(TRANSPORT.findall(text))),
                                           sorted(set(TRANSPORT.findall(text)))))
        print()
        if len(set(TRANSPORT.findall(text))) >= 2:
            print("  => **有第二个实现**，conformance suite 有意义：")
            print("     两个 transport 走同一份契约，行为可能已经漂移。")
        else:
            print("  => 只有一个实现 —— 现在加 conformance suite 是仪式，不是修复。")


if __name__ == "__main__":
    main()