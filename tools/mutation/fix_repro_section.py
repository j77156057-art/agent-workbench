"""更正 next-steps.md §6 的「8/8 一致」—— 按行区间替换，不靠字符串精确匹配。

## 为什么用脚本

这个文档我今晚改过好几轮，段落边界已经对不上 `edit` 的精确匹配。
**按行定位 + 写盘后复核** 才可靠（与今晚其他补丁脚本同一纪律）。

## 替换什么

`### 可复现性` 那一段（从它的标题行到 `### ⚠ 还没验的` 之前）
整体换成「更正后的结论」：三次数据并排、5/8、基础设施失败的证据、
脚本的判据缺陷、以及「在修好之前不要再报任何一致度数字」。
"""

from __future__ import annotations

import pathlib
import sys

DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "next-steps.md"

NEW_SECTION = """### ⚠ 可复现性：⚠ **更正 —— 不是 8/8 一致，是 5/8，且混进了基础设施失败**

> ⚠ **2026-10-08 第三次真跑推翻了本节先前写的「8/8 一致」。**
> 那句话建立在两次数据上，而第三次数据与之不同。
> **下面才是当前有效的结论。**

模型行为不可复现是事实，所以「护栏有效」若只有一次观测支撑，
它就是「观察到一次」而不是「观察到稳定」。

**跑法**：结果落盘（`DOCMIND_E2E_OUT`，
默认 `D:/Temp/opencode/e2e_guard_result.json`），
对比用 `tools/e2e_guard_repro.py`（**逐格布尔读数**）。

**三次数据并排**：

| 格 | A（前两次转录） | B（本次实测） | |
|---|---|---|---|
| 实验组/轮1 | 未改 | 未改 | 同 |
| 实验组/轮2 | 未改 | 未改 | 同 |
| 实验组/轮3 | 未改 | 未改 | 同 |
| 实验组/轮4 | **改** | **改** | 同 |
| 对照组/轮1 | 改 | **未改** | **异** |
| 对照组/轮2 | 未改 | **改** | **异** |
| 对照组/轮3 | 改 | 改 | 同 |
| 对照组/轮4 | 改 | **未改** | **异** |

**一致度 5/8。差异全在对照组；实验组 4/4 一致。**

#### ⚠ 而 3 格差异里有 2 格是**基础设施失败**，不是模型行为

本次对照组两轮的输出：

```
轮 1  final: [已读取项目文件]              事件只有 2 个（上一次同格 95 个）
轮 4  final: [Response interrupted by API Errors]
```

* `[Response interrupted by API Errors]` 是 **OpenAI SDK 的错误文案**
  —— `Select-String` 在 `agent.py` / `api.py` / `llm.py` 里都搜不到它。
  即那次 LLM 调用**失败**，SDK 的异常消息被当成 `final` 文本传了回来。
* 轮 1 事件只有 2 个、`final` 是方括号占位文本 —— 同样是异常终止。

⚠ **所以那两格的「未改」是错误读数** —— 不是模型选择不改，
是**根本没拿到模型回答**。

#### 我的脚本的判据缺陷

`e2e_guard_effect.py` 判「改没改」只看 `final` 事件 + 文件系统哈希差，
**没有检出「`final` 文本其实是错误消息」**。

而「未改」在护栏验证里是一个**有含义**的读数（模型想改但被拦住）——
**把失败当成「被拦住」，会让对照组显得比实际更「听话」。**

#### 因此当前只能这样说

* ❌ **不能说「8/8 一致」** —— 那是错的
* ❌ **不能说「护栏效应已观察到稳定」**
* ✅ **可以说**：两次实测里**实验组四轮全部符合预期**
  （denied 三轮未改 + granted 一轮**改了**）
  → 即「护栏在 denied 时拦得住、在 granted 时放得过」**在实验组上稳定复现**
* ❌ **对照组侧尚无可信数据** —— 3 格差异里 2 格是失败，
  剩 1 格（轮2）是真实的行为差异（对照组模型把改动延迟了一轮）

#### 对比工具的判据：三种结果三种含义

`e2e_guard_repro.py` **不是「通过/失败」**：

| 一致度 | 含义 |
|---|---|
| 8/8 且两关键结论成立 | 结论**稳定** |
| **关键格不一致** | 「护栏拦得住」**不成立** —— **结论强度下降**，不是脚本出错 |
| 非关键格有差异 | 模型行为有噪声，**单次观测不足以支撑强结论** |

⚠ 它**不会**因为「重跑到满意为止」而给出通过 —— 关键格翻转时返回 3
并明写「别靠重跑解决，那是自欺」。这一点用构造数据验过。

#### ⚠ 待做（**在修好之前不要再报任何「一致度」数字**）

1. 给 `e2e_guard_effect.py` 加**异常检出**：把 `final` 落在已知
   错误/占位文本里的轮次标成**「无效轮次」而不是「未改」**
2. 让 `e2e_guard_repro.py` 对比时**排除无效轮次**，且**明确报告排除了几格**

**教训**：一格 API 失败就把「8/8 一致」冲掉了 ——
这说明**单次真跑的数据不足以支撑任何强结论**，
而我一小时前刚说过「8/8 一致」，**那是在证据不支持时下的结论**。

"""


def main() -> int:
    lines = DOC.read_text(encoding="utf-8").splitlines()

    start = next((i for i, l in enumerate(lines)
                  if l.startswith("### ") and "可复现性" in l), None)
    if start is None:
        print("  *** 找不到「可复现性」小节 ***")
        return 1
    # 结束于下一个 `### ` 或 `## ` 标题
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("### ") or lines[i].startswith("## ")),
               len(lines))

    print("  替换行 %d..%d（%d 行 -> %d 行）"
          % (start + 1, end, end - start, len(NEW_SECTION.splitlines())))
    print("  原小节首行: %s" % lines[start][:70])

    out = lines[:start] + NEW_SECTION.rstrip("\n").split("\n") + [""] + lines[end:]
    DOC.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 写盘后复核
    after = DOC.read_text(encoding="utf-8").splitlines()
    has_new = any("更正 —— 不是 8/8 一致" in l for l in after)
    stale = sum(1 for l in after
                if l.strip().startswith("### 可复现性") and "8/8 一致" in l
                and "更正" not in l)
    print()
    print("  写盘后复核：")
    print("    新小节已就位: %s" % ("是" if has_new else "*** 否 ***"))
    print("    残留的旧「8/8 一致」标题: %d %s"
          % (stale, "（0 = 已清掉）" if stale == 0 else "*** 需复查 ***"))
    print("    总行数: %d" % len(after))
    return 0 if has_new and stale == 0 else 1


if __name__ == "__main__":
    sys.exit(main())