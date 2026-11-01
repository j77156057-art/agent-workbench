# -*- coding: utf-8 -*-
"""文档引用自检：Markdown 里每条 `路径:行号` 都必须真的可核。

为什么要常驻：一次真实丢档之后重做的设计稿（docs/notes）靠坐标说话，而坐标会漂 ——
`agent_eval.py:143` 在下一个提交可能就是别的内容。设计稿 §16.28 也踩过同类问题
（一处 `turns.py:105` 写成裸文件名，且行号早已漂移）。

判据（确定性，无依赖）：
  1. 路径解析后的文件必须存在（裸文件名不算 —— 本仓库同名文件散在多个目录）；
  2. 行号与区间端点都必须在文件实际行数范围内。

「示例」行的处理：文档里**故意写错**的反例坐标（如越界示例）不能当失败，
但也不许静默跳过 —— 脚本逐条打印出来，让豁免量本身可见。
"""
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOCS = (
    "docs/notes/passed-vs-score.md",
    "docs/notes/citation-precision.md",
    "docs/notes/evidence-checkability.md",
    "docs/memory-v2-p0.md",
    "README.md",
    "README_en.md",
)
REF = re.compile(r"([A-Za-z0-9_./\\-]+\.(?:py|md|json|ts|vue|js)):(\d{1,5})(?:-(\d{1,5}))?")


def check(docs=DOCS, root=ROOT):
    failures, exempted, total = [], [], 0
    for doc in docs:
        path = os.path.join(root, doc)
        if not os.path.isfile(path):
            failures.append("%s 不存在" % doc)
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        for no, text in enumerate(lines, 1):
            for m in REF.finditer(text):
                rel, start, end = m.group(1), m.group(2), m.group(3)
                if "示例" in text:
                    exempted.append("%s:%d → %s:%s（示例行，未校验）" % (doc, no, rel, start))
                    continue
                total += 1
                target = os.path.join(root, rel.replace("\\", "/"))
                if not os.path.isfile(target):
                    failures.append("%s:%d → %s 文件不存在（裸文件名要写全路径）" % (doc, no, rel))
                    continue
                with open(target, encoding="utf-8", errors="replace") as handle:
                    n_lines = sum(1 for _ in handle)
                lo, hi = int(start), int(end or start)
                if not (1 <= lo <= n_lines) or not (1 <= hi <= n_lines):
                    failures.append("%s:%d → %s:%d-%d 超出实际行数 %d" % (doc, no, rel, lo, hi, n_lines))
    return total, exempted, failures


def main():
    total, exempted, failures = check()
    print("检查引用 %d 条（%d 份文档）" % (total, len(DOCS)))
    if exempted:
        print("示例豁免 %d 条（逐条列出，非静默）：" % len(exempted))
        for item in exempted:
            print("  -", item)
    if failures:
        print("不通过 %d 条：" % len(failures))
        for item in failures:
            print("  ·", item)
        return 1
    print("全部引用可核")
    return 0


if __name__ == "__main__":
    sys.exit(main())
