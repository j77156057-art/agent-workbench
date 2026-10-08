# -*- coding: utf-8 -*-
"""过程解说（默认开）与 reasoning 事件合并 —— 承重断言。

## 为什么这两样要有 pytest 覆盖

`tools/narration_filter_selftest.py` 与 `tools/reasoning_coalescer_selftest.py`
是手动跑的 1 秒自检，**不进回归**。而这两样东西各自有一个失效模式
**不会报错、只会静默劣化**，没有自动覆盖就会在几个月后被人改回去：

| 失效模式 | 后果 | 为什么难以靠读代码发现 |
|---|---|---|
| 少了 `parsed["action"]` 条件 | **最终答案本身**被当解说再发进时间线 | 不抛异常，只是用户看到同一段话两次 |
| 合并器动了内容（裁剪/去重/strip） | **静默丢模型输出** | reasoning 是用户唯一能看到的「模型在想什么」 |

## 一条纪律：断言必须对着机制，不是对着表面读数

「合并后事件变少」这种断言**很容易恒真** —— 只要实现里多丢点数据，
事件数当然会变少。所以本文件的核心断言是**内容守恒**：
把所有 chunk 拼起来 == 把所有输入拼起来，逐字节相等。
"""

import json

import agent as agent_mod
import pytest


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
            # ⚠ **必须能表达这个形态**：真模型在**同一条消息里**既写散文又发 tool_call。
            # 没有它，批次路径的散文永远是空串，测试就测不到「散文有没有被路由」。
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


#: 实测原样（2026-10-08, qwen3.6:35b-a3b）：契约明文禁止 `Thought:`，模型仍吐。
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


def _run(steps, narration, **kw):
    a = agent_mod.Agent(llm=ScriptedLLM(steps), narration=narration)
    return list(a.run("扣血在哪算？", stream=True, **kw))


def _events(events, kind):
    return [e for e in events if e.get("type") == kind]


# ---------------------------------------------------------------------------
# 过程解说：标签分类
# ---------------------------------------------------------------------------

class TestNarrationLabelClass:
    """`Thought:` 后面是人话，`Action:` 后面是协议正文 —— 两者不能当一类。

    ⚠ 第一版把两类当一类（凡是行首带标记就丢整行），实测那条 80 字解说
    **全部消失**（剥后 0 字）。这个 bug 不抛异常，只是解说凭空不见了。
    """

    def test_prose_label_keeps_the_words_after_it(self):
        f = agent_mod._PromptNarrationFilter()
        got = f.strip_protocol(REAL_PROTOCOL_TEXT)
        assert "用户问的是" in got, "把人话也剥掉了"
        assert len(got) > 40, "剥完只剩残句：%r" % got

    def test_no_protocol_marker_survives(self):
        f = agent_mod._PromptNarrationFilter()
        got = f.strip_protocol(
            "Thought: 我先看 a.py。\nAction: read_file\nAction Input: path=a.py\n"
            "Observation: === a.py ===\nThought: 里面是减法。")
        for lab in f.LABELS:
            assert lab not in got, "残留协议标记 %r -> %r" % (lab, got)
        assert "我先看 a.py。" in got and "里面是减法。" in got

    def test_body_labels_drop_their_content_entirely(self):
        """协议正文进活动时间线是噪音：那里已经有 action/observation 事件了。"""
        f = agent_mod._PromptNarrationFilter()
        got = f.strip_protocol("Action: read_file\nAction Input: path=a.py")
        assert got == "", "协议正文没被丢掉：%r" % got

    def test_every_label_has_a_kind(self):
        """单一标签源必须**处处有分类** —— 漏一个就等于把 prose 当 body 处理。"""
        f = agent_mod._PromptNarrationFilter()
        missing = [lab for lab in f.LABELS if lab not in f._KIND]
        assert not missing, "标签没有分类，会被当成 body 丢掉：%r" % missing
        unknown = {k for k in f._KIND.values()} - {"prose", "body"}
        assert not unknown, "未知分类：%r" % unknown


# ---------------------------------------------------------------------------
# 过程解说：去重与噪音
# ---------------------------------------------------------------------------

class TestNarrationDedupe:
    """实测症状：一次工具轮产出**两条逐字相同**的 thought
    （对应 Hermes 记录的 issue #10478）。不去重则时间线出现重复行。"""

    def test_same_text_twice_in_one_turn_is_offered_once(self):
        f = agent_mod._PromptNarrationFilter()
        assert f.offer(REAL_PROTOCOL_TEXT)
        assert f.offer(REAL_PROTOCOL_TEXT) == ""

    def test_dedupe_survives_whitespace_and_markdown_differences(self):
        """只认逐字相等的话，模型换个标点就能绕过去重 —— 那等于没去重。"""
        f = agent_mod._PromptNarrationFilter()
        first = f.offer("我先读 a.py 和 b.py 看看实现。")
        second = f.offer("我先读  a.py  和  b.py  看看实现。\n")
        assert first
        assert second == "", "空白/装饰差异绕过了去重"

    def test_empty_shells_are_not_offered(self):
        """模型有时只写「好的。」—— 那是噪音，不是解说。"""
        f = agent_mod._PromptNarrationFilter()
        for shell in ("好的。", "Thought:", "思考: 嗯", "ok"):
            assert f.offer(shell) == "", "空壳被放行：%r" % shell


# ---------------------------------------------------------------------------
# 过程解说：路由（最危险的一条）
# ---------------------------------------------------------------------------

class TestNarrationRouting:
    """少了 `parsed["action"]` 条件，**最终答案本身**会被当解说再发一次。

    这条不抛异常 —— 只是用户看到同一段话出现两次，所以必须有测试钉住。
    """

    def test_final_answer_never_enters_the_activity_timeline(self):
        events = _run([("text", REAL_ANSWER)], narration=True)
        finals = _events(events, "final")
        assert finals, "final 缺失 —— 前提不成立，判据失效"
        assert REAL_ANSWER in (finals[-1].get("text") or ""), "答案没原样送达"
        leaked = [e for e in _events(events, "thought")
                  if REAL_ANSWER[:30] in (e.get("text") or "")]
        assert not leaked, "答案被当成解说发进时间线了：%r" % leaked

    def test_the_action_guard_is_load_bearing(self):
        """反向守卫：证明那个条件真的承重，而不只是「源码里有这行」。

        做法是把答案直接喂给 offer()：若路由层没有 action 条件挡着，
        它就会被放行。也就是说「条件在 → 答案不漏；条件不在 → 答案必漏」。
        """
        f = agent_mod._PromptNarrationFilter()
        assert f.offer(REAL_ANSWER), ("答案被挡下了 —— 那么 action 条件"
                                      "就不是承重的，本测试失去意义")

    def test_narration_off_changes_nothing(self):
        """关掉时行为必须与改动前逐字一致（零行为变化是回滚安全的前提）。"""
        events = _run([("tools", [("list_dir", {"path": "."})]),
                       ("text", REAL_ANSWER)], narration=False)
        stray = [e for e in _events(events, "thought")
                 if (e.get("text") or "").strip()
                 and not (e.get("text") or "").startswith(("检测到", "工具 ", "事实项"))]
        assert not stray, "关掉后仍有解说事件：%r" % stray

    def test_narration_fires_on_the_PARALLEL_BATCH_path(self):
        """⚠⚠ 这一条是**真跑发现的缺陷**，单调用假 LLM 测不到。

        实测（2026-10-08 真模型）一次发 3 个 `read_file` -> 走**并行批次**分支。
        而批次分支（`agent.py:4841`）**只设 `_pending_batch`、不设 `native_override`**，
        所以 `acc` 里没有合成的 `Action:` 标记 -> `parse_response(acc)` 得到
        `parsed["action"] == ""`。

        于是守卫写成 `elif self.narration and parsed["action"]:` 时，
        **恰好在最常见的路径上永远是假的** —— 叙述功能被静默关掉，
        而且不报错、不掉事件，只是「一直没解说」。

        真模型才批量派发；写单调用假 LLM 的测试永远走不到这条分支。
        """
        prose = "我先把这三个文件都读一遍，再判断 add 到底是加还是减。"
        events = _run([
            # 同一条消息里既写散文又发**两个** tool_call -> 并行批次分支
            ("tools_prose", prose,
             [("read_file", {"path": "a.py"}), ("read_file", {"path": "b.py"})]),
            ("text", REAL_ANSWER),
        ], narration=True)

        batch_actions = _events(events, "action")
        assert len(batch_actions) >= 2, \
            "前提不成立：这一轮没走批量（%d 个 action），测的不是目标路径" % len(batch_actions)
        thoughts = [t for t in _events(events, "thought")
                    if prose[:12] in (t.get("text") or "")]
        assert thoughts, (
            "**并行批次路径上散文没成为解说** —— 守卫把 `parsed[\"action\"]` 当成唯一入口，"
            "而批次分支（agent.py:4841）只设 `_pending_batch`、不设 `native_override`，"
            "于是 `parsed[\"action\"]` 恒空")
        assert not any(REAL_ANSWER[:30] in (t.get("text") or "")
                       for t in _events(events, "thought")), \
            "答案又漏进时间线了"

    def test_narration_fires_on_the_SINGLE_call_path_too(self):
        """单调用分支（`native_override`）也必须能出解说 —— 两个入口都要通。"""
        prose = "我先读一下 a.py，确认 add 的实现。"
        events = _run([
            ("tools_prose", prose, [("read_file", {"path": "a.py"})]),
            ("text", REAL_ANSWER),
        ], narration=True)
        assert _events(events, "action"), "前提不成立：没派发工具"
        assert any(prose[:12] in (t.get("text") or "")
                   for t in _events(events, "thought")), "单调用分支上散文没成为解说"


# ---------------------------------------------------------------------------
# 过程解说：提示词接线
# ---------------------------------------------------------------------------

class TestNarrationPromptWiring:
    """`_prompt_for_channel` 有三条早退分支，**漏一条**就意味着那条路径上
    叙述没开 —— 而这不会报错，只会让用户发现「有时候说话有时候不说话」。"""

    def _prompt(self, narration):
        return agent_mod._prompt_for_channel(
            agent_mod._SYSTEM_PROMPT_FULL, True, narration=narration)

    def test_clause_appears_only_when_enabled(self):
        assert agent_mod._NARRATION_CLAUSE.strip() in self._prompt(True)
        assert agent_mod._NARRATION_CLAUSE.strip() not in self._prompt(False)

    def test_clause_must_be_last(self):
        """位置错了模型就照错的契约走 —— 这条实测教训记在函数 docstring 里。"""
        p = self._prompt(True).rstrip()
        assert p.endswith(agent_mod._NARRATION_CLAUSE.strip()[-40:]), \
            "叙述条款不在末尾，它会被后面的内容盖过"

    def test_native_contract_rule3_still_present_when_enabled(self):
        """开启叙述**不能**削弱第 3 条（不许用散文代替真正的调用）。

        那个坑不是提示词写错，是防真实故障：模型用散文代替调用 → 工具没派发出去。
        所以只撤销它的副作用，不动本意。
        """
        p = self._prompt(True)
        assert "不要用正文描述" in p, "第 3 条被删了 —— 工具可能被散文顶掉"

    def test_default_is_on(self):
        """「默认开」必须是**真默认**，而不是靠调用方记得传参。"""
        assert agent_mod.NARRATION is True
        assert agent_mod.Agent(llm=ScriptedLLM([("text", REAL_ANSWER)])).narration is True


# ---------------------------------------------------------------------------
# reasoning 事件合并
# ---------------------------------------------------------------------------

def _real_shape(total_chars=129270, avg=2.30):
    """复刻实测那次跑的粒度：56210 事件 / 129270 字符 / 平均 2.30 字符。"""
    n = int(total_chars / avg)
    out, used = [], 0
    for i in range(n - 1):
        piece = "思" if i % 3 == 0 else ("考" if i % 3 == 1 else "。")
        out.append(piece)
        used += len(piece)
    out.append("x" * (total_chars - used))
    return out


class TestReasoningCoalescer:
    """核心断言是**内容守恒**，不是「事件变少了」。

    「事件变少」极易恒真：多丢点数据就让计数变好看。合并器的职责只有改切点。
    """

    def test_content_is_conserved_byte_for_byte(self):
        src = _real_shape()
        c = agent_mod._ReasoningCoalescer()
        pending, chunks = [], []
        for i, piece in enumerate(src):
            pending.append(piece)
            chunks.extend(c.take(pending, final=(i == len(src) - 1)))
        assert "".join(chunks) == "".join(src), "合并改了内容（丢字或插字）"
        assert len(chunks) < len(src) / 50, \
            "降幅只有 %d 倍，合并器基本没生效" % (len(src) / max(1, len(chunks)))

    def test_pending_is_cleared_in_place(self):
        """`pending` 就是 `chat_kwargs['reasoning_sink']` ——
        不清空的话上游会持续 append，同一段会被重复发多次。"""
        c = agent_mod._ReasoningCoalescer()
        pending = ["a", "b", "c"]
        c.take(pending)
        assert pending == []

    def test_never_cuts_below_min_chars(self):
        """提前切就等于退回逐 token —— 那样合并器白写。"""
        c = agent_mod._ReasoningCoalescer(min_chars=200, max_chars=1200)
        assert c.take(["a" * 50]) == []

    def test_final_flush_emits_the_remainder(self):
        """轮末不 flush 就丢掉模型最后的思考 —— 而那通常是结论前的关键一步。"""
        c = agent_mod._ReasoningCoalescer(min_chars=200)
        pending = ["尾巴" * 30]          # 60 字符，达不到 min
        pre = c.take(pending)
        post = c.take(pending, final=True)
        assert len(pre) + len(post) == 1
        assert (pre + post)[0] == "尾巴" * 30

    def test_break_variant_would_be_caught(self):
        """变异守卫：故意让切点多截 5 字，守恒断言必须失败。

        没有这条，「内容守恒」也可能因为判据写错而恒真。
        """
        class Broken(agent_mod._ReasoningCoalescer):
            def take(self, pending, final=False):
                for item in pending:
                    self._buf += str(item)
                pending.clear()
                out = []
                while True:
                    if final:
                        if self._buf:
                            out.append(self._buf)
                            self._buf = ""
                        break
                    cut = self._cut_at(self._buf)
                    if cut <= 0:
                        break
                    out.append(self._buf[:cut - 5 if cut > 5 else 0])
                    self._buf = self._buf[cut:]
                return out

        src = _real_shape(3000)
        b = Broken()
        pending, out = [], []
        for i, piece in enumerate(src):
            pending.append(piece)
            out.extend(b.take(pending, final=(i == len(src) - 1)))
        assert "".join(out) != "".join(src), "变异体竟然守恒 —— 守恒判据恒真"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
