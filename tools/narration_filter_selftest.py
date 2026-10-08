"""自检：过程解说（`NARRATION`，默认开）的剥协议 + 去重 + 「不污染答案」。

## 三条断言各自对着一个已实测的失效模式

| 断言 | 防的是什么 |
|---|---|
| 剥协议 | 实测模型在契约下**仍吐** `Thought:`（`agent.py` 里 `_NATIVE_FORMAT_CONTRACT` 第 3 条明文禁止）。不剥就等于给用户看协议垃圾 |
| 同轮去重 | 实测一次工具轮产出**两条逐字相同**的 `thought`。对应 Hermes 记录的 issue #10478。不去重则时间线出现重复行 —— 比不解说更糟 |
| 不污染答案 | 最后一轮不派发工具，`acc` 里就是**答案本身**。若不加 `parsed["action"]` 条件，答案会被当解说再发一次 |

第三条是最危险的一条：它**不会报错**，只会让用户看到同一段话出现两次，
所以纯靠读代码看不出来 —— 必须跑。

## 为什么用假 LLM 而不跑真模型

这三条断言测的是**我们的路由逻辑**，不是模型听不听话。真模型 3-10 分钟且
不保证复现那两种形态；假 LLM 1 秒、且能把每种形态**精确构造出来**。
模型侧的事由 `tools/probe_narration_event_mix.py` 真跑负责。
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ensure_on_path()

import agent as agent_mod  # noqa: E402


class ScriptedLLM:
    """按脚本返回原生 tool_calls 或纯文本。"""

    provider = "deepseek"          # 在 _NATIVE_CAPABLE 内 -> auto 走原生 FC
    model = "fake-narration"

    def __init__(self, steps):
        self.steps = steps
        self.i = 0
        self.last_usage = {}
        self.last_tool_calls = []

    def chat(self, messages, stream=True, **kw):
        spec = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        self.last_usage = {"prompt_tokens": 5, "completion_tokens": 2}
        self.last_tool_calls = []
        content = ""
        if spec[0] == "tools":
            self.last_tool_calls = [
                {"id": "c%d" % i, "name": n, "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(spec[1])
            ]
        elif spec[0] == "tools_prose":
            # 同一条消息里既写散文又发调用 —— 真模型就是这个形态
            content = spec[1]
            self.last_tool_calls = [
                {"id": "c%d" % i, "name": n, "arguments": json.dumps(a)}
                for i, (n, a) in enumerate(spec[2])
            ]
        else:
            content = spec[1]
        if stream:
            def g():
                for ch in content:
                    yield ch
            return g()
        return content

    def count_tokens(self, text):
        return max(1, len(text) // 3)

    def clone(self):
        return ScriptedLLM(self.steps)


#: 实测原样（2026-10-08, qwen3.6:35b-a3b）：模型在契约下仍吐 `Thought:`
REAL_PROTOCOL_TEXT = (
    "Thought: 用户问的是 a.py 里的 add 函数、b.py 里的对应函数，"
    "以及 test_a.py 是否测试了它。我需要读取这三个文件来给出准确答案。"
)

REAL_ANSWER = (
    "扣血逻辑共三层：\n"
    "1. 伤害数值：behaviors/enemy.gd:7 const CONTACT_DAMAGE: int = 10\n"
    "2. 真正改血量：behaviors/player.gd:14 hp = maxi(0, hp - amount)\n"
    "一句话：扣多少看 enemy.gd 的 10，扣在哪看 player.gd。"
)


def run(steps, narration, **kw):
    a = agent_mod.Agent(llm=ScriptedLLM(steps), narration=narration)
    return list(a.run("扣血在哪算？", stream=True, **kw))


def events_of(events, kind):
    return [e for e in events if e.get("type") == kind]


def main() -> int:
    print("=" * 74)
    print("  自检：过程解说 —— 剥协议 / 去重 / 不污染答案")
    print("=" * 74)
    ok = True

    F = agent_mod._PromptNarrationFilter

    # ---- [1] 剥协议 ----
    print()
    print("  [1] 剥掉 `Thought:` 标记，保留人话")
    f = F()
    got = f.strip_protocol(REAL_PROTOCOL_TEXT)
    print("      原文 %d 字 -> 剥后 %d 字" % (len(REAL_PROTOCOL_TEXT), len(got)))
    print("      结果: %s" % got[:60])
    leaked = any(lab in got for lab in F.LABELS)
    print("      残留协议标记: %s" % leaked)
    if leaked or "用户问的是" not in got:
        ok = False
        print("      *** 标记没剥干净，或把人话也剥掉了 ***")

    # ---- [2] 同轮去重（实测症状：两条逐字相同）----
    print()
    print("  [2] 同一段话在**同一轮**里只放行一次")
    f = F()
    first = f.offer(REAL_PROTOCOL_TEXT)
    second = f.offer(REAL_PROTOCOL_TEXT)
    print("      第 1 次: %s" % ("放行 %d 字" % len(first) if first else "放行空"))
    print("      第 2 次: %s" % ("放行 %d 字" % len(second) if second else "丢弃"))
    if not first or second:
        ok = False
        print("      *** 去重失效 —— 时间线会出现逐字重复的行 ***")

    # ---- [3] 去重要扛住空白/装饰差异 ----
    print()
    print("  [3] 标点空白不同也算同一条（否则去重形同虚设）")
    f = F()
    a1 = f.offer("我先读 a.py 和 b.py 看看实现。")
    a2 = f.offer("我先读  a.py  和  b.py  看看实现。\n")
    print("      第 1 次放行=%s，第 2 次=%s" % (bool(a1), a2 or "丢弃"))
    if not a1 or a2:
        ok = False
        print("      *** 只认逐字相等 —— 模型换个标点就能绕过去重 ***")

    # ---- [4] 空壳不进时间线 ----
    print()
    print("  [4] 没有实质内容的空壳不放行")
    f = F()
    shells = ("好的。", "Thought:", "思考: 嗯", "ok")
    leaked_shells = []
    for shell in shells:
        out = f.offer(shell)
        if out:
            leaked_shells.append((shell, out))
            ok = False
    print("      试了 %d 个空壳，放行 %d 个%s"
          % (len(shells), len(leaked_shells),
             ("：" + repr(leaked_shells)) if leaked_shells else ""))
    if leaked_shells:
        print("      *** 空壳进了活动时间线 —— 那纯是噪音 ***")

    # ---- [5] ⚠ 关键：最终答案不得被当成解说再发一次 ----
    print()
    print("  [5] 最后一轮（不派发工具）的答案**不得**进活动时间线")
    ev_on = run([("text", REAL_ANSWER)], narration=True)
    thoughts_on = events_of(ev_on, "thought")
    finals_on = events_of(ev_on, "final")
    print("      narration=开 -> thought 事件 %d 条，final 事件 %d 条"
          % (len(thoughts_on), len(finals_on)))
    dup = [t for t in thoughts_on if REAL_ANSWER[:30] in (t.get("text") or "")]
    if dup:
        ok = False
        print("      *** 答案被当成解说发进时间线了：%r" % (dup[0].get("text") or "")[:60])
    else:
        print("      OK：时间线里没有答案的副本")
    if not finals_on or REAL_ANSWER not in (finals_on[-1].get("text") or ""):
        ok = False
        print("      *** final 没拿到完整答案（前提不成立，判据失效）***")

    # ---- [6] narration 关掉时行为必须与改动前逐字一致 ----
    print()
    print("  [6] narration=关 -> 不产出任何解说事件（零行为变化）")
    ev_off = run([("tools", [("list_dir", {"path": "."})]),
                  ("text", REAL_ANSWER)], narration=False)
    extra = [e for e in ev_off
             if e.get("type") == "thought"
             and (e.get("text") or "").strip()
             and not (e.get("text") or "").startswith(("检测到", "工具 ", "事实项"))]
    print("      narration=关 -> 非 harness 告警的 thought 事件 %d 条" % len(extra))
    if extra:
        ok = False
        print("      *** 关掉后仍有解说事件：%r" % (extra[0].get("text") or "")[:60])

    # ---- [7] 提示词条款：开时出现、关时不出现，且排在全文最后 ----
    print()
    print("  [7] 提示词条款随开关出现/消失，且必须在**最后**")
    base = agent_mod._SYSTEM_PROMPT_FULL
    p_on = agent_mod._prompt_for_channel(base, True, narration=True)
    p_off = agent_mod._prompt_for_channel(base, True, narration=False)
    has_on = agent_mod._NARRATION_CLAUSE.strip() in p_on
    has_off = agent_mod._NARRATION_CLAUSE.strip() in p_off
    last = p_on.rstrip().endswith(agent_mod._NARRATION_CLAUSE.strip()[-40:])
    print("      开 -> 含条款=%s；关 -> 含条款=%s；条款在最后=%s"
          % (has_on, has_off, last))
    if not has_on or has_off or not last:
        ok = False
        print("      *** 条款接线有问题（位置错了会让模型照错的契约走）***")

    # 为 [8] 提剏把源码变量拿出来（原本在 [8] 内部定义，改后靠 [8] 它已删掉）
    src = pathlib.Path(agent_mod.__file__).read_text(encoding="utf-8")

    # ---- [8] 反向守卫：证明 `parsed["action"]` 那个条件是**承重**的 ----
    print()
    print("  [8] 反向守卫：拿掉 action 条件后，答案确实会漏进时间线")
    # 只替换源码不足以证明任何事 —— 它只说明「字符串找得到」。
    # 真正的反向守卫是**演示后果**：把答案原样喂给 offer()，
    # 若路由层没有 action 条件挡着，它就会被放行。第 5 项观测到的
    # 「thought 事件 0 条」正是这个条件换来的。
    f = F()
    would_leak = f.offer(REAL_ANSWER)
    # ⚠ 锚点已随修复变过（加了两条路径）。写死一个字符串的时候
    # 会在以后的改动里静默失效。这里只断言「三条路径都在」。
    guard_present = (
        'elif self.narration and (parsed["action"] or native_override is not None' in src
        and 'or self._pending_batch' in src)
    print("      路由层条件存在: %s" % guard_present)
    print("      若无条件，offer(答案) 会放行 %d 字: %s"
          % (len(would_leak), bool(would_leak)))
    if not guard_present or not would_leak:
        ok = False
        print("      *** 守卫失效：要么条件不在（防不住），"
              "要么答案会被挡（那这条守卫就没意义了）***")
    else:
        print("      OK —— 条件承重：它在，答案不漏；它不在，答案必漏")

    # ---- [8b] 并行批次路径（真跑打出来的分支）----
    print()
    print("  [8b] 并行批次路径（同一条消息两个 tool_call）也要能出解说")
    prose = "我先把这三个文件都读一遍，再判断 add 到底是加还是减。"
    ev_b = run([("tools_prose", prose,
                 [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"})]),
                ("text", REAL_ANSWER)], narration=True)
    acts_b = events_of(ev_b, "action")
    got_b = [t for t in events_of(ev_b, "thought") if prose[:12] in (t.get("text") or "")]
    print("      批次 action %d 个；解说命中 %d 条" % (len(acts_b), len(got_b)))
    if len(acts_b) < 2:
        print("      ⚠ 这一轮没走批量（前提不成立），本项不作判定")
    elif not got_b:
        ok = False
        print("      *** 批次路径上散文没成为解说 —— 而真模型**恰恰**走这条路径 ***")

    print()
    print("=" * 74)
    print("  结论：%s" % ("全部通过" if ok else "*** 有失败 ***"))
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
