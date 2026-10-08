"""探针二：改用 n-gram（中文无空格，标点切分得不到可用词项）。

上一版用 `_TERM_SPLIT` 切词，实测 1/12。根因不是参数，是**词项太长**：
`单元测试` 这种 4 字整块不会出现在「跑一下测试」（5 字）里。

中文无空格，正确做法是从描述里取 **2~4 字的 n-gram**，再与问题做重合。
仍是数据驱动（判据来自 description，不是手写词表）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools as T  # noqa: E402

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
    ("改一下 README 里的端口", "apply_edit"),
    ("重构这个模块", "dev_find_references"),
    ("给我做个 Excel", "create_artifact"),
]

#: 只保留中文与字母数字，避免标点造出无意义 n-gram
_KEEP = set("abcdefghijklmnopqrstuvwxyz0123456789")


def grams(text: str, lo: int = 2, hi: int = 4) -> set[str]:
    out: set[str] = set()
    buf = []
    for ch in (text or "").lower():
        if ch in _KEEP or "\u4e00" <= ch <= "\u9fff":
            buf.append(ch)
        else:
            _emit(buf, lo, hi, out)
            buf = []
    _emit(buf, lo, hi, out)
    return out


def _emit(buf, lo, hi, out) -> None:
    s = "".join(buf)
    for n in range(lo, hi + 1):
        for i in range(len(s) - n + 1):
            piece = s[i:i + n]
            if any("\u4e00" <= c <= "\u9fff" for c in piece) or piece.isalnum():
                out.add(piece)


def score(question: str, tool: str, name_weight: int = 10) -> int:
    hay = question.lower()
    s = 0
    if tool.lower() in hay:
        s += name_weight
    q_grams = grams(question)
    # 与问题重合的 n-gram 越长越有信息量
    s += sum(len(g) for g in grams(T.TOOLS[tool].description) & q_grams)
    return s


def main() -> None:
    print("  %-24s %-22s %-5s %-5s %s" % ("问题", "期望工具", "排名", "分数", "分数>=N 的工具数"))
    print("  " + "-" * 80)
    ok = 0
    for q, want in CASES:
        ranked = sorted(T.TOOLS, key=lambda n: -score(q, n))
        rank = ranked.index(want) + 1
        good = rank <= 3
        ok += good
        print("  %-24s %-22s %-5s %-5s %s" % (
            q[:24], want, rank, score(q, want), "OK" if good else "**未进前3**"))
    print()
    print("  目标进前 3: %d/%d" % (ok, len(CASES)))

    print()
    print("  === 阈值 -> 进有效集的工具数（全量 %d）===" % len(T.TOOLS))
    for th in (6, 10, 14, 18, 22, 26):
        counts = [sum(1 for n in T.TOOLS if score(q, n) >= th) for q, _ in CASES]
        print("    >=%-3d %s  平均 %.1f" % (th, counts, sum(counts) / len(counts)))

    print()
    print("  === 阈值 14 下的误召回抽查 ===")
    for q, _ in CASES[:5]:
        got = [n for n in sorted(T.TOOLS, key=lambda x: -score(q, x)) if score(q, n) >= 14]
        print("    %-22s -> %s" % (q[:22], ", ".join(got[:8])))


if __name__ == "__main__":
    main()