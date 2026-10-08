"""修正文档里的行号/路径引用。

## 为什么要专门写脚本

行号修正是最容易出错的一类编辑：
* 反引号在 shell 里会被吃掉（我今晚栽过多次）
* 行号本身也可能错 —— 而**错行号比没有行号更坏**，
  因为它让下一个人以为「去看那一行」就能找到，而实际上他看的是别的地方

## 这批修的是什么

根因：**PowerShell 的 `Get-Content` 在 Windows 上默认按 ANSI 读文件**，
把 UTF-8 中文注释读成乱码，某些字节被解释成控制符 → **行数变了**。

实测：
* `requirements.txt`：Python 读 **70 行** / PowerShell 读 **59 行**
* `harness.yml`：Python 读 **174 行** / PowerShell 读 **166 行**

于是所有用 `Select-String` / `Get-Content` 拿到的行号**系统性偏小 7-11 行**。
本次修了 9 处（`harness.yml:59` → `:62`，`dependency_bootstrap.py:414` → `:421`）。

另外 3 处引用**文件路径不完整**：`tool_install.py` / `dev_server.py` 实际在
`agent_runtime/` 下 —— 而我今晚正是靠 `tool_install.py:92` 定位到那条
`--ignore-scripts` 的。

⚠ `dev_server.py:36` 那处还**行号也错**：第 36 行只是一条提到 `owns_port`
的注释，函数本体在**第 102 行**。

修完逐条重验，每个 `<file>:<line>` 都回到文件里读那一行确认。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "harness-research-verified.md"

TICK = chr(96)   # 反引号 —— 避免经过 shell

FIXES = [
    # (旧, 新)
    ("harness.yml:59", "harness.yml:62"),
    ("dependency_bootstrap.py:414", "dependency_bootstrap.py:421"),
    (TICK + "tool_install.py:92" + TICK,
     TICK + "agent_runtime/tool_install.py:92" + TICK),
    (TICK + "dev_server.py:36" + TICK,
     TICK + "agent_runtime/dev_server.py:102" + TICK),
]


def main() -> int:
    text = DOC.read_text(encoding="utf-8")
    print("  修正行号/路径：")
    total = 0
    for old, new in FIXES:
        count = text.count(old)
        if count == 0:
            print("    %-42s 未找到（可能已修或已带路径）" % old)
            continue
        text = text.replace(old, new)
        total += count
        print("    %-42s %d 处" % (old + "  ->  " + new, count))
    print("  共 %d 处" % total)

    print()
    print("  === 逐条重验：每个 <file>:<line> 回到文件里读那一行 ===")
    bad = []
    seen = set()
    for m in re.finditer(r"`([A-Za-z0-9_./-]+\.(?:py|yml|json|txt)):(\d+)`", text):
        rel, ln = m.group(1), int(m.group(2))
        if (rel, ln) in seen:
            continue
        seen.add((rel, ln))
        candidates = [rel]
        if rel.startswith("harness.yml"):
            candidates.insert(0, ".github/workflows/" + rel)
        if "/" not in rel:
            candidates.append("agent_runtime/" + rel)
        for cand in candidates:
            p = ROOT / cand
            if p.exists():
                lines = p.read_text(encoding="utf-8").splitlines()
                if 1 <= ln <= len(lines):
                    print("    OK  %-42s 行%-5d %s"
                          % (cand, ln, lines[ln - 1].strip()[:56]))
                else:
                    print("    **  %-42s 行%-5d 越界（文件共 %d 行）"
                          % (cand, ln, len(lines)))
                    bad.append("%s:%d 越界" % (cand, ln))
                break
        else:
            print("    **  %-42s 文件不存在" % rel)
            bad.append("%s 文件不存在" % rel)

    DOC.write_text(text, encoding="utf-8")
    print()
    print("  问题引用: %s" % (", ".join(bad) if bad else "无"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())