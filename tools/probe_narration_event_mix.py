"""真跑一次，数「用户可见的事件」由哪几类构成 —— 定位「边干边说话」缺在哪一层。

## 要回答的问题

用户观察：只有开头的深度思考有发言，后面就是一串工具调用和代码检索，
直到出结果才说话。**这句话对不对？缺的是产出侧还是渲染侧？**

## 为什么必须真跑

读代码能推出机制（`thought` 只在模型文本里写了 `Thought:` 时才产出；
`native_override` 路径下 `thought` 恒为空），但**推不出实际占比** ——
而占比正是用户体验的全部。所以这里只做一件事：**数真实事件流**。

## 判据纪律

* **恒真防护**：总事件数为 0 时直接退出非 0，**不产出任何结论**。
  否则「thought 占 0%」在「一个事件都没有」时会和「跑了但模型没写 Thought」混为一谈 ——
  这正是今晚踩过的坑（把「没数据」当成「观测到的 0」）。
* **分母写清**：报每类事件数 + 占比，**并明确写出占比的分母是总事件数**。
* **不合并**：token / reasoning / action / observation / thought / reflection 分开数，
  因为它们在**不同通道**渲染（正文 / 独立面板 / 工具轨迹）。

## 只读，不改代码

全篇只读 + 一次真推理。不改前端、不改 agent。
"""

from __future__ import annotations

import collections
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ROOT = ensure_on_path()

MODEL = os.getenv("DOCMIND_E2E_MODEL", "qwen3.6:35b-a3b")

READONLY = ("read_file", "list_dir", "grep", "search_code", "calculate")

#: 需要**多次工具调用**才能答完的题 —— 单步题测不出「中间过程」的占比。
QUESTION = os.getenv(
    "DOCMIND_NARRATION_Q",
    "a.py 里的 add 函数到底是加法还是减法？b.py 呢？"
    "顺便看看 test_a.py 有没有测它。你要读几个文件才能答？",
)

FIXTURE = {
    "a.py": "def add(a, b):\n    return a - b\n",
    "b.py": "def mul(a, b):\n    return a * b\n",
    "test_a.py": "from a import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}

#: 前端渲染通道（核实自 frontend/src/workbench/composables/useChatStream.ts）
CHANNELS = {
    "token": "正文气泡（assistant text）",
    "reasoning": "独立「深度思考」面板",
    "thought": "工具轨迹列表（标签「分析摘要」）",
    "action": "工具轨迹列表（标签「执行动作」）",
    "observation": "工具轨迹列表（标签「返回结果」）",
    "reflection": "工具轨迹列表（标签「执行提示」）",
    "notice": "消息级通知条",
    "plan": "计划卡片",
    "final": "最终答案",
}


def main() -> int:
    print("=" * 78)
    print("  真跑数事件构成：用户到底能看见什么")
    print("=" * 78)
    print("  模型: %s" % MODEL)
    print("  题目: %s" % QUESTION)

    try:
        from llm import LLMClient
        import agent as agent_mod
        import tools as tools_mod
        from config import set_runtime
    except Exception as exc:
        print("  *** 导入失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    registry = {k: v for k, v in tools_mod.TOOLS.items() if k in READONLY}
    tmpdir = tempfile.mkdtemp(prefix="narration_mix_")
    try:
        for name, body in FIXTURE.items():
            pathlib.Path(tmpdir, name).write_text(body, encoding="utf-8")
        set_runtime("code_root", tmpdir)
        print("  code_root = %s（3 个 fixture）" % tmpdir)
        print("  只读工具集: %s" % ", ".join(sorted(registry)))
        print()

        # ⚠ **必须开 thinking** —— 用户抱怨的「开头的深度思考」就是它；
        # 不开就测不到。第一版探针忘了开，导致 reasoning=0 是
        # 「未启用」而非「模型不产出」——两者不能混。
        thinking = os.getenv("DOCMIND_NARRATION_THINKING", "1") != "0"
        client = LLMClient(provider="ollama", model=MODEL)
        a = agent_mod.Agent(llm=client, tool_registry=dict(registry))
        events = list(a.run(QUESTION, stream=True, thinking_enabled=thinking))
        print("  thinking_enabled = %s" % thinking)
        print()

        # ⚠ **完整原文落盘** —— 只数事件不够，因为「协议文本泄漏到结案」
        # 只能靠看**完整**原文判，截断后的 96 字会把真实结案隐掉。
        dump = pathlib.Path(os.getenv("DOCMIND_NARRATION_DUMP",
                                        "D:/Temp/opencode/narration_events.json"))
        try:
            dump.write_text(json.dumps([
                {"type": str(e.get("type")), "text": str(e.get("text", ""))}
                for e in events if isinstance(e, dict)
            ], ensure_ascii=False, indent=1), encoding="utf-8")
            print("  完整事件原文已落盘：%s" % dump)
        except Exception as exc:
            print("  *** 落盘失败: %s" % exc)
        print()

        counts: collections.Counter = collections.Counter()
        sample: dict[str, list[str]] = collections.defaultdict(list)
        for ev in events:
            if not isinstance(ev, dict):
                continue
            t = str(ev.get("type", "?"))
            counts[t] += 1
            if len(sample[t]) < 4:
                sample[t].append(str(ev.get("text", ""))[:96])

        total = sum(counts.values())

        # ⚠ 恒真防护：没有事件就不是「观测到 0」，是「没测到」。
        if total == 0:
            print("  *** 一个事件都没有 —— **本次不产出任何结论**")
            print("     （否则「thought 占 0%」会与「根本没跑起来」混淆）")
            return 2
        if counts.get("final", 0) == 0:
            print("  ⚠ 没有 final 事件 —— 本轮可能异常中断，构成不可信")

        print("=" * 78)
        print("  事件构成（总 %d 个）" % total)
        print("=" * 78)
        print("  %-13s %5s %7s  %s" % ("类型", "次数", "占比", "前端通道"))
        print("  " + "-" * 74)
        for t, n in counts.most_common():
            print("  %-13s %5d %6.1f%%  %s"
                  % (t, n, 100.0 * n / total, CHANNELS.get(t, "（前端无处理 → 丢弃）")))
        print()
        print("  ⚠ 占比的分母是**总事件数 %d**，不是轮数。" % total)
        print()

        print("=" * 78)
        print("  每类的前几条原文（看形态，不只看计数）")
        print("=" * 78)
        for t, _ in counts.most_common():
            print("  --- %s (%d) ---" % (t, counts[t]))
            for s in sample[t]:
                print("      %s" % s)
        print()

        # ---- 结论：把计数翻译成用户体验 ----
        print("=" * 78)
        print("  翻译成用户看到的东西")
        print("=" * 78)
        narr = counts.get("thought", 0) + counts.get("reasoning", 0)
        tools = counts.get("action", 0) + counts.get("observation", 0)
        print("  「人话」类事件（reasoning + thought）: %d" % narr)
        print("  「工具」类事件（action + observation）: %d" % tools)
        print("  比值 人话:工具 = %s" % ("n/a" if not tools else "%.2f" % (narr / tools)))
        print()
        if counts.get("thought", 0) == 0 and tools > 0:
            print("  ⚠ **thought = 0 而 action/observation > 0**")
            print("     这就是你观察到的现象，且原因可定位：")
            print("     `thought` 只在模型文本里写了 `Thought:` 时才产出")
            print("     （agent.py:4576 `if parsed[\"thought\"]:`）；")
            print("     而 `native_override` 路径下它恒为空 —— 走原生工具调用，")
            print("     模型不写 `Thought:`，于是**中间过程没有任何人话**。")
        # ⚠⚠ **这里一律不得从计数推断分布。**
        # 第一版写的是 if reasoning > 0 and tools > 5: → 「思考只在开头」。
        # 那是**恒真式**：任何「有思考 + 有工具块」的跑都输出同一句话，
        # 它从未看分布。同一次跑的实测已经推翻它：reasoning 拆成
        # **2 段**，第 2 段在工具调用**之后**（7 段/其中 177 个事件）。
        # 局部数据在下面，不在这里推。
        seq = [str(e.get("type", "?")) for e in events if isinstance(e, dict)]
        r_idx = [i for i, t in enumerate(seq) if t == "reasoning"]
        if r_idx:
            segments, a = [], r_idx[0]
            prev = r_idx[0]
            for i in r_idx[1:]:
                if i != prev + 1:
                    segments.append((a, prev)); a = i
                prev = i
            segments.append((a, prev))
            lens = [len(str(e.get("text", ""))) for e in events
                    if isinstance(e, dict) and e.get("type") == "reasoning"]
            chars = sum(lens)
            print("  —— reasoning 的**实测分布**（不从计数推）——")
            print("     %d 个事件 / %d 字符 / 平均 %.2f 字符每事件"
                  % (len(lens), chars, chars / max(1, len(lens))))
            print("     连续段数: %d" % len(segments))
            for si, (x, y) in enumerate(segments, 1):
                print("       第 %d 段  索引 %6d..%6d  %6d 个事件" % (si, x, y, y - x + 1))
            if len(segments) > 1:
                print("     → 思考**不只在开头**：第 2 段在工具块之后。")
                print("       它不可见是因为**面板默认折叠**，不是因为模型没说话。")
            else:
                print("     → 实测本次确实只有一段（开头思考），工具阶段无思考事件。")
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print()
        print("  临时 fixture 已清理")


if __name__ == "__main__":
    sys.exit(main())
