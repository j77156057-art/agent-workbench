"""用真模型产出「它自己会说的话」，再拿那些话去测写意图护栏。

## 版本沿革（两个真 bug 驱动了它）

**第一版**：没设 `code_root`。于是模型看不到任何文件，转而
**吐原生 tool-calling JSON**（`thought":` / `action":`）并抱怨环境
（「代码库尚未配置，无法直接搜索文件」）。产出 130 句、104 句 denied ——
**而那不是护栏的问题，是我没给模型工作目录**。
（机制本身是验证过的：`e2e_guard_effect.py` 用同一个 `set_runtime("code_root", 临时目录)` 跑通过。）

**第二版**（本文件）：修上面那条，并加第二处修正 ——

> 第一版拿模型输出**切碎**去测，而切碎之后大量句子是
> **技术内容**（「用 `ruff check --select F401` 检测」），
> 不是**授权表达**。前者测出来 denied 是必然的，那是误测。

所以本版：
* 给一个**真有 `a.py` 的临时目录**当 `code_root`
* 问法改成「**一句话**说你打算怎么开始」
* **按句长过滤**：长段技术内容不是「用户说的话」，不测

## 为什么这样能挖出词表缺口

已知的「补」缺口是我**猜**出来的。而真实用户/模型会说什么，
比我能枚举的多。所以让**模型自己产出句子**，再逐句测 `_write_intent`。

* 句子不是我写的 → 不是「猜词」
* 来自真实推理 → 这个部署里真会出现的表述
* 判据仍是规则函数 → 秒级、不烧模型

⚠ **只做发现，不做修。** 用途是「设计结构化授权时知道实际会说什么」——
**不加词**（见 `docs/known-defect-write-verb-bushu-missing.md` §5）。

## 完整回答会落盘

「回答质量」要看**完整原文** —— 模型胡说、跑题、对护栏的解释，
全在被截断的那部分里。所以全文落盘，供人判断。
⚠ **只落盘、不自动评分** —— 我不能自己给自己发的报告打分。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

# ⚠⚠ **这是同一个错的第 4 次**（parents[N] 猜层级）。
# 而我**已经建好了机制** 	ools/_repo_root.py（按内容找仓库根），
# 还写过一句「教训写进注释没有阻止复发，说明缺的是机制」——
# **然后在同一件事上又犯了。**
#
# 机制是对的，我只是**没在重写文件时用它**。
# 根因不是「不知道」，是「顺手写了熟悉的写法」。
# **所以除了机制，还必须有守卫扫全部脚本** ——
# 见 tests/test_tools_repo_root.py。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ROOT = ensure_on_path()

MODEL = os.getenv("DOCMIND_E2E_MODEL", "qwen3.6:35b-a3b")

# 只给**只读**工具 —— 保证模型只能说话，不会真去改文件
READONLY = ("read_file", "list_dir", "grep", "search_code", "calculate")

HARVEST_JSON = pathlib.Path(os.getenv(
    "DOCMIND_HARVEST_OUT", "D:/Temp/opencode/intent_harvest.json"))

#: 超过这个长度的句子**不测** —— 长段技术内容不是「用户说的话」。
#: 阈值来自第一版的观察：用户/模型的授权表达都是短句，
#: 而被误测的技术说明普遍 40+ 字符。
MAX_SENTENCE_CHARS = 36

#: 元动作声明的线索 —— 含这些的句子才是「它在说它要做什么」
META_ACTION_HINTS = ("我来", "我这就", "让我", "我先", "我现在", "开始",
                     "动手", "帮你", "给你", "去做", "执行", "修改", "改")

# ⚠ **问法要求「一句话」** —— 第一版没要求，模型输出了整套方案，
# 于是切碎后全是技术内容。
PROMPTS = [
    "我要给 a.py 里的 add 函数补一个 docstring。你打算怎么做？**用一句话说**，不要列步骤。",
    "帮我修改 a.py 的 add 函数。**一句话**说你会怎么开始。",
    "需要清理一下项目里没用到的导入。**一句话**说你打算怎么动手。",
    "给 test_a.py 补几个测试用例。**一句话**说。",
    "把 a.py 里那个 add 的实现修一下。**一句话**说你准备怎么改。",
    "接下来该做什么？**一句话**说你的判断。",
    "我想让代码更规范一点。**一句话**说你会怎么做。",
    "确认一下：现在可以写文件了吗？**一句话**回答。",
]

FIXTURE = {
    "a.py": "def add(a, b):\n    return a - b\n",
    "b.py": "def mul(a, b):\n    return a * b\n",
    "test_a.py": "from a import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}

#: 第一版观察到的**坏形态** —— 出现即说明模型在吐原生 JSON 或抱怨环境
BAD_MARKERS = ('thought":', 'action":', 'action_input":',
               "尚未配置", "无法直接", "无法检索", "尚未建立索引", "未配置代码库")


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"[。？!?；;\n]+", text or "")
    out = []
    for p in parts:
        s = p.strip(" \t`*-—·「」\"'()（）#")
        if len(s) >= 4:
            out.append(s)
    return out


def looks_broken(text: str) -> str:
    """返回坏形态标记（空串=正常）。"""
    for marker in BAD_MARKERS:
        if marker in text:
            return marker
    return ""


def main() -> int:
    print("=" * 76)
    print("  用真模型产出表述，再拿它们测写意图护栏（第二版）")
    print("=" * 76)
    print("  模型: %s" % MODEL)
    print("  任务: %d 个措辞 × 真实推理（每次一句）" % len(PROMPTS))
    print("  只读工具集: %s" % ", ".join(sorted(READONLY)))
    print("  句长上限: %d 字符（超出的不测 —— 长段技术内容不是「用户说的话」）"
          % MAX_SENTENCE_CHARS)

    try:
        from llm import LLMClient
        import agent as agent_mod
        import tools as tools_mod
        from agent import (_WRITE_INTENT_DENIED, _WRITE_INTENT_GRANTED,
                           _WRITE_INTENT_UNCERTAIN, _write_intent)
        from config import set_runtime
    except Exception as exc:
        print("  *** 导入失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    registry = {k: v for k, v in tools_mod.TOOLS.items() if k in READONLY}

    tmpdir = tempfile.mkdtemp(prefix="intent_harvest_")
    try:
        for name, body in FIXTURE.items():
            pathlib.Path(tmpdir, name).write_text(body, encoding="utf-8")
        set_runtime("code_root", tmpdir)
        print("  code_root = %s（已写入 3 个 fixture 文件）" % tmpdir)
        print()

        client = LLMClient(provider="ollama", model=MODEL)
        transcripts: list[dict] = []
        candidates: list[tuple[str, str]] = []

        for i, prompt in enumerate(PROMPTS, 1):
            try:
                a = agent_mod.Agent(llm=client, tool_registry=dict(registry))
                events = list(a.run(prompt, stream=True))
            except Exception as exc:
                print("  [%d/%d] *** 推理失败: %s: %s"
                      % (i, len(PROMPTS), type(exc).__name__, str(exc)[:70]))
                continue
            final = next((e for e in reversed(events)
                          if isinstance(e, dict) and e.get("type") == "final"), {})
            text = str(final.get("text", "")).strip()
            bad = looks_broken(text)
            sents = split_sentences(text)
            kept = [s for s in sents if len(s) <= MAX_SENTENCE_CHARS]
            transcripts.append({
                "prompt": prompt, "full_text": text, "n_events": len(events),
                "bad_marker": bad, "all_sentences": sents, "kept": kept,
            })
            flag = ("  ⚠ 坏形态: %s" % bad) if bad else ""
            print("  [%d/%d] %2d 句（留 %d 句 ≤%d 字）%s"
                  % (i, len(PROMPTS), len(sents), len(kept),
                     MAX_SENTENCE_CHARS, flag))
            if kept:
                print("       例: %s" % kept[0][:60])
            for s in kept:
                candidates.append((prompt[:18], s))

        print()
        print("=" * 76)
        print("  判定（只测短句；元动作声明另标）")
        print("=" * 76)
        if not candidates:
            print("  *** 一句都没留下 —— 本次不产出任何结论 ***")
            if any(t["bad_marker"] for t in transcripts):
                print("     有 %d 题命中坏形态（吐原生 JSON / 抱怨环境）"
                      % sum(1 for t in transcripts if t["bad_marker"]))
            return 2

        rows = []
        for src, s in candidates:
            g = _write_intent(s)
            meta = any(h in s for h in META_ACTION_HINTS)
            rows.append((src, s, g, meta))

        granted = [r for r in rows if r[2] == _WRITE_INTENT_GRANTED]
        uncertain = [r for r in rows if r[2] == _WRITE_INTENT_UNCERTAIN]
        denied = [r for r in rows if r[2] == _WRITE_INTENT_DENIED]
        print("  共 %d 句：granted %d / uncertain %d / denied %d"
              % (len(rows), len(granted), len(uncertain), len(denied)))

        meta_denied = [r for r in denied if r[3]]
        print("  其中**含元动作线索**的（真正值得看的那批）: %d 句" % len(meta_denied))
        print()
        print("  --- 含元动作线索却判 denied（缺口候选）---")
        for src, s, g, meta in meta_denied[:20]:
            print("    %s" % s[:70])
        if len(meta_denied) > 20:
            print("    …（共 %d 句）" % len(meta_denied))

        try:
            HARVEST_JSON.write_text(json.dumps({
                "model": MODEL, "max_sentence_chars": MAX_SENTENCE_CHARS,
                "code_root_fixtures": sorted(FIXTURE),
                "transcripts": transcripts,
                "verdicts": [{"src": a, "sentence": b, "verdict": str(c),
                              "meta_action": d} for a, b, c, d in rows],
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            print()
            print("  完整回答 + 逐句判定已落盘：%s" % HARVEST_JSON)
            print("  ⚠ **只落盘、不自动评分** —— 「答得好不好」需要你看。")
        except Exception as exc:
            print("  *** 落盘失败: %s" % exc)

        n_bad = sum(1 for t in transcripts if t["bad_marker"])
        print()
        print("  === 质量自查 ===")
        print("    坏形态命中: %d/%d 题%s"
              % (n_bad, len(transcripts),
                 "  <- 仍有模型在吐原生 JSON，环境配置可能还不够" if n_bad else ""))
        print("    空回答: %d 题" % sum(1 for t in transcripts if t["empty"] if "empty" in t
                                       or not t["full_text"]))
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print()
        print("  临时 fixture 目录已清理：%s" % tmpdir)


if __name__ == "__main__":
    sys.exit(main())