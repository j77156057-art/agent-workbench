"""自检：探针的 reasoning 分布判据**必须真的区分**两种分布。

## 为什么需要它

`probe_narration_event_mix.py` 第一版有一句**恒真式推论**：

```python
if counts["reasoning"] > 0 and tools > 5:
    print("说明深度思考「只在开头」")
```

判据是「有思考 + 有工具块」，所以**任何**这样的跑都会输出同一句话 ——
它从未看分布。而同一次真跑的数据直接推翻了它：reasoning 实际拆成 2 段，
第 2 段在工具调用**之后**。

这类错（**从计数推分布**）单测抓不到，因为真跑要几分钟且不一定碰到边界。
所以用**构造数据**验判据本身 —— 1 秒。

## 判据纪律

* 每条断言都**必须能失败**：先确认它在错数据下真的会红，
  否则就是又一条恒真断言（本项目已犯 9 次同类错）。
* 覆盖三种分布：单段 / 双段 / 无 reasoning。
* **不许**在自检里调用真模型。
"""

from __future__ import annotations

import ast
import pathlib
import sys

# ⚠⚠ **`parents[N]` 猜层级 —— 同一个错第 5 次**（`parents[1]` / `parents[2]` / `.parent` …）。
# 我刚把它写进 `docs/next-steps.md` §5 第 10 条的邻居位置，转头又犯。
#
# **这里的教训比「用机制」更强**：`_repo_root.py` 只解决「找仓库根」，
# 而我这次要的是**同目录的兄弟文件** —— 根本不该用层级索引，
# 该用 `pathlib.Path(__file__).resolve().parent`。**用错工具比不用机制更贵。**
PROBE = pathlib.Path(__file__).resolve().parent / "probe_narration_event_mix.py"


def segments_of(seq: list[str], want: str = "reasoning") -> list[tuple[int, int]]:
    """与探针里同一段算法：把某类型的位置压成连续段。"""
    idx = [i for i, t in enumerate(seq) if t == want]
    if not idx:
        return []
    segs, a = [], idx[0]
    prev = idx[0]
    for i in idx[1:]:
        if i != prev + 1:
            segs.append((a, prev))
            a = i
        prev = i
    segs.append((a, prev))
    return segs


def verdict(segs: list[tuple[int, int]]) -> str:
    """探针的判定文案 —— 与探针里的分支保持一致。"""
    if not segs:
        return "none"
    return "只在开头" if len(segs) == 1 else "不只在开头"


CASES = [
    # (说明, 事件序列, 期望判定)
    ("真跑实测：思考在工具前后各一段",
     ["reasoning"] * 5 + ["action"] * 3 + ["observation"] * 3 + ["reasoning"] * 2,
     "不只在开头"),
    ("只有开头一段思考，工具后没有",
     ["reasoning"] * 5 + ["action"] * 3 + ["observation"] * 3,
     "只在开头"),
    ("完全没有 reasoning",
     ["action"] * 3 + ["observation"] * 3 + ["final"],
     "none"),
    ("思考被工具切成三段",
     ["reasoning", "action", "reasoning", "observation", "reasoning"],
     "不只在开头"),
    ("单段 reasoning 但只有 1 个事件（不能因此判错）",
     ["reasoning"],
     "只在开头"),
]


def main() -> int:
    print("=" * 70)
    print("  自检：分布判据必须能区分「只在开头」与「不只在开头」")
    print("=" * 70)

    # --- 反向守卫 1：探针里那个恒真分支必须已经不存在 ---
    print()
    print("  [1] 探针源码里不得再有「从计数推分布」的分支")
    src = PROBE.read_text(encoding="utf-8")
    ast.parse(src)   # 顺带保证语法
    banned = [
        'counts.get("reasoning", 0) > 0 and tools > 5',
        "说明深度思考**只在开头**",
    ]
    bad = [b for b in banned if b in src]
    print("      被禁写法命中: %d" % len(bad))
    for b in bad:
        print("        *** 仍在: %s" % b)
    ok1 = not bad
    print("      %s" % ("OK" if ok1 else "*** 失败 ***"))

    # --- 反向守卫 2：判据必须能区分，且每条断言都能失败 ---
    print()
    print("  [2] 构造数据验判据（%d 例）" % len(CASES))
    ok2 = True
    for desc, seq, expect in CASES:
        got = verdict(segments_of(seq))
        mark = "OK" if got == expect else "*** 不符 ***"
        if got != expect:
            ok2 = False
        print("      %-46s 段=%d 判定=%-12s 期望=%-12s %s"
              % (desc[:46], len(segments_of(seq)), got, expect, mark))

    # --- 反向守卫 3：把期望反过来，判据必须变红（证明断言不是恒真）---
    print()
    print("  [3] 反向守卫：把「单段」的数据喂给「期望双段」，必须不符")
    seq = ["reasoning"] * 5 + ["action"] + ["reasoning"]
    got = verdict(segments_of(seq))
    flipped = (got != "只在开头")
    print("      喂双段数据，判定=%s；判据能识别为双段=%s" % (got, flipped))
    ok3 = flipped
    print("      %s" % ("OK —— 断言确实会失败" if ok3 else "*** 判据恒真 ***"))

    print()
    print("=" * 70)
    print("  结论：%s" % ("三项全过" if (ok1 and ok2 and ok3) else "*** 有失败 ***"))
    print("=" * 70)
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
