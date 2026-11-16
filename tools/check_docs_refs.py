# -*- coding: utf-8 -*-
"""文档引用自检：Markdown 里每条 `路径:行号` 都必须真的可核。

为什么要常驻：一次真实丢档之后重做的设计稿（docs/notes）靠坐标说话，而坐标会漂 ——
`agent_eval.py:143` 在下一个提交可能就是别的内容。设计稿 §16.28 也踩过同类问题
（一处 `turns.py:105` 写成裸文件名，且行号早已漂移）。

判据（确定性，无依赖）：
  1. 路径必须在 **git 跟踪**里（2026-11-17 收窄：本机存在 ≠ 随公开仓库分发；
     被 gitignore 的内部文档在本机判绿、空白克隆上就是死链，实测漏过两处）；
  2. 行号与区间端点都必须在文件实际行数范围内；
  3. 反引号独占或 Markdown 链接里的裸路径（无行号）同样按跟踪判据核。

「示例」行的处理：文档里**故意写错**的反例坐标（如越界示例）不能当失败，
但也不许静默跳过 —— 脚本逐条打印出来，让豁免量本身可见。
EXEMPT 是带理由的白名单（叙述性夹具名 / 运行时产物），同样逐条打印。
"""
import os
import re
import subprocess
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
BARE = re.compile(r"(?<![\w./])((?:docs|tools|tests|frontend|agent_runtime|golden|scripts)/"
                  r"[A-Za-z0-9_./-]+\.(?:jsonl|json|mjs|md|py|ts|vue|js|css))")
# 带理由的豁免：叙述性文字里的夹具/示例文件名（不是链接），或文档声明的运行时产物。
EXEMPT = {
    "tests/test_a.py": "叙述 pytest 失败输出示例，非仓库文件",
    "agent_runtime/mod.py": "叙述覆盖率夹具的合成模块名，非仓库文件",
    "tests/fixtures/recorder.py": "历史实验夹具叙述，文件已不存在；保留叙述不改写日志",
    "tests/test_evidence_checkability.py": "设计稿里的用例骨架代码，尚未实现",
    "golden/results.jsonl": "README 声明的 run_golden 运行时产物，不入库",
}


def _tracked(root):
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    return {line.replace("\\", "/") for line in out.stdout.splitlines()}


def check(docs=DOCS, root=ROOT):
    failures, exempted, total = [], [], 0
    tracked = _tracked(root)
    for doc in docs:
        path = os.path.join(root, doc)
        if not os.path.isfile(path):
            failures.append("%s 不存在" % doc)
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        in_fence = False
        for no, text in enumerate(lines, 1):
            if text.lstrip().startswith("```"):
                in_fence = not in_fence
            for m in REF.finditer(text):
                rel, start, end = m.group(1), m.group(2), m.group(3)
                if "示例" in text:
                    exempted.append("%s:%d → %s:%s（示例行，未校验）" % (doc, no, rel, start))
                    continue
                total += 1
                rel_n = rel.replace("\\", "/")
                if rel_n in EXEMPT:
                    exempted.append("%s:%d → %s（%s）" % (doc, no, rel_n, EXEMPT[rel_n]))
                    continue
                if tracked is not None and rel_n not in tracked:
                    failures.append("%s:%d → %s 不在 git 跟踪（空白克隆死链）" % (doc, no, rel_n))
                    continue
                target = os.path.join(root, rel_n)
                if not os.path.isfile(target):
                    failures.append("%s:%d → %s 文件不存在（裸文件名要写全路径）" % (doc, no, rel))
                    continue
                with open(target, encoding="utf-8", errors="replace") as handle:
                    n_lines = sum(1 for _ in handle)
                lo, hi = int(start), int(end or start)
                if not (1 <= lo <= n_lines) or not (1 <= hi <= n_lines):
                    failures.append("%s:%d → %s:%d-%d 超出实际行数 %d" % (doc, no, rel, lo, hi, n_lines))
            if in_fence:
                continue
            for m in BARE.finditer(text):
                rel_n = m.group(1)
                before = text[m.start() - 1:m.start()]
                after = text[m.end():m.end() + 1]
                is_link = before == "`" and after == "`"
                is_md = text[max(0, m.start() - 2):m.start()] == "]("
                if not (is_link or is_md):
                    continue
                if "示例" in text:
                    exempted.append("%s:%d → %s（示例行，未校验）" % (doc, no, rel_n))
                    continue
                total += 1
                if rel_n in EXEMPT:
                    exempted.append("%s:%d → %s（%s）" % (doc, no, rel_n, EXEMPT[rel_n]))
                    continue
                if tracked is not None and rel_n not in tracked:
                    failures.append("%s:%d → %s 不在 git 跟踪（空白克隆死链）" % (doc, no, rel_n))
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
