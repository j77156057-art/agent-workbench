"""探针：用「词项重合」代替手写正则，量一下效果。

要回答三件事：
  1. 目标工具能不能排进前列？
  2. 阈值放哪？放多少工具进有效集（过窄=能力缺失，过宽=progressive 失效）
  3. 会不会误召回（不相关的工具被捞进来）

判据来自同文件已有的 `_skill_score`：拿问题与【数据】的词项重合打分，
而不是手写「跑|测试|装|提交」这类词表。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools as T  # noqa: E402
from agent_runtime.context_router import _TERM_SPLIT  # noqa: E402

#: 每条：问题 -> 我们期望它把哪个工具捞进来
CASES = [
    ("跑一下测试", "dev_testrun"),
    ("装个依赖", "dev_deps"),
    ("这条能提交吗", "dev_commit"),
    ("截个图看看页面", "preview_project"),
    ("查下高铁票", "web_search"),
    ("看看有哪些函数", "search_code"),
    ("这个报错了", "dev_diagnostics"),
    ("把补丁打上去", "dev_patch"),
    ("这个 bug 记一下", "dev_capture_bug"),
    ("看看物价", "web_search"),
    ("改一下 README 里的端口", "apply_edit"),
    ("重构这个模块", "dev_find_references"),
]


def score(question: str, tool: str) -> int:
    """与 `_skill_score` 同形：工具名命中权重更高，其余按词项长度计。"""
    hay = question.lower()
    s = 0
    name = tool.lower()
    if name in hay:
        s += 8
    meta = (T.TOOLS[tool].description or "").lower()
    for term in _TERM_SPLIT.split(meta):
        if len(term) >= 2 and term in hay:
            s += min(len(term), 6)
    return s


def main() -> None:
    print("  %-24s %-22s %-6s %-6s %s" % ("问题", "期望工具", "排名", "分数", "分数>=2 的工具数"))
    print("  " + "-" * 86)
    ranks_ok = 0
    for q, want in CASES:
        ranked = sorted(T.TOOLS, key=lambda n: -score(q, n))
        rank = ranked.index(want) + 1
        s = score(q, want)
        above2 = sum(1 for n in T.TOOLS if score(q, n) >= 2)
        ok = rank <= 3
        ranks_ok += ok
        print("  %-24s %-22s %-6s %-6s %-6s %s" % (
            q[:24], want, rank, s, above2, "OK" if ok else "**未进前3**"))
    print()
    print("  目标进前 3: %d/%d" % (ranks_ok, len(CASES)))

    print()
    print("  === 阈值 -> 进有效集的工具数（全量 %d）===" % len(T.TOOLS))
    for th in (1, 2, 3, 4, 6, 8):
        counts = []
        for q, _ in CASES:
            counts.append(sum(1 for n in T.TOOLS if score(q, n) >= th))
        print("    >%d : %s   平均 %.1f" % (
            th, counts, sum(counts) / len(counts)))

    print()
    print("  === 误召回抽查：这些工具会被捞进来吗 ===")
    for q, _ in CASES[:4]:
        top = sorted(T.TOOLS, key=lambda n: -score(q, n))[:6]
        print("    %-22s -> %s" % (q[:22], ", ".join(top)))


if __name__ == "__main__":
    main()