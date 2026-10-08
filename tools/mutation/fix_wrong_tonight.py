"""修掉我写错的 6 处「今晚」—— 实际是 2026-10-08 下午。

## 错误的来源

会话开始时有一份 checkpoint 摘要，里面用了「今晚」这个词
（那份摘要本身来自更早的会话，可能确实是晚上）。
我**沿用了那个措辞**、并且在整场会话里反复用，**没核实实际时间**。

结果：7 处「今晚」写进了文档，而实际时间是 **2026-10-08 15:30（周四下午）**。

## 为什么这是实质错误，不只是措辞

`docs/next-steps.md` 第 1 行就写着「**写给上下文重置后的自己 / 接手的人**」。
而「今晚」对三个月后接手的人是**无意义的** —— 他不知道是哪个晚上。
**相对时间词在交接文档里等于信息丢失。**

## 只改我这次写错的，不动之前会话的

`known-defect-context-router-recall.md:56` 那句「写意图那一族今晚已被证过一次」
是**更早的会话**写的（那一次可能真的是晚上），**不该动** ——
把它改成 2026-10-08 反而是**伪造**记录。

## 替换策略

在两个文件顶部加**明确日期锚点**，正文里的「今晚」改成「本轮」。
这样既去掉错误的时间断言，又不把「第四次/第六次」这类计数改得别扭。

按行定位 + 写盘后逐条复核（不靠 `grep` 计数当成功判据）。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"

# (文件, [(行号, 原文片段, 替换后片段)])
FIXES = {
    "harness-research-verified.md": [
        (125, "今晚我自己的两次", "本轮我自己的两次"),
        (181, "但今晚的教训正是", "但本轮的教训正是"),
        (278, "一条贯穿今晚的引用坑", "一条贯穿本轮的引用坑"),
        (312, "（今晚第四次「判据选错」）", "（本轮第四次「判据选错」）"),
        (498, "这是今晚第六次同类", "这是本轮第六次同类"),
    ],
    "known-defect-write-verb-bushu-missing.md": [
        (75, "今晚已经实测到同样的形状", "本轮已经实测到同样的形状"),
    ],
}

# 不动的（并说明为什么）
LEAVE_ALONE = [
    ("known-defect-context-router-recall.md", 56,
     "更早会话写的，那一次可能真的是晚上 —— 改成日期等于伪造记录"),
]

ANCHOR = ("> ⚠ **本篇里的「本轮」指 2026-10-08 那一轮**（下午，不是晚上 ——\n"
          "> 我原先写「今晚」是沿用了旧上下文的措辞、没核实时间，已全部改掉）。\n"
          "> 相对时间词在交接文档里等于信息丢失。\n\n")


def main() -> int:
    print("  实际时间核实：2026-10-08（周四）下午 —— 「今晚」是错的")
    print()
    total = 0
    for rel, edits in FIXES.items():
        path = DOCS / rel
        lines = path.read_text(encoding="utf-8").splitlines()
        print("  %s" % rel)
        for ln, old, new in edits:
            idx = ln - 1
            if idx >= len(lines):
                print("    行%-5d *** 越界（文件 %d 行）***" % (ln, len(lines)))
                continue
            if old not in lines[idx]:
                print("    行%-5d *** 原文不匹配 ***" % ln)
                print("      实际: %s" % lines[idx].strip()[:80])
                continue
            lines[idx] = lines[idx].replace(old, new)
            print("    行%-5d %s -> %s" % (ln, old, new))
            total += 1
        # 顶部加锚点
        if not lines or "本轮」指 2026-10-08" not in (lines[1] if len(lines) > 1 else ""):
            lines.insert(1, ANCHOR.rstrip("\n"))
            print("    顶部已加日期锚点")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print()

    print("  共修正 %d 处" % total)
    print()
    print("  === 不动的（并说明理由）===")
    for rel, ln, why in LEAVE_ALONE:
        print("    %s:%d" % (rel, ln))
        print("      %s" % why)

    print()
    print("  === 写盘后复核：docs/ 里还剩几处「今晚」===")
    left = []
    for p in sorted(DOCS.glob("*.md")):
        for i, l in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "今晚" in l:
                left.append("%s:%d  %s" % (p.name, i, l.strip()[:60]))
    for item in left:
        print("    " + item)
    print("    剩余 %d 处（应等于 %d 处「不动」的）" % (len(left), len(LEAVE_ALONE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())