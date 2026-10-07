"""子代理被授权卡住时，要上浮成「需要用户确认」而不是「失败」。

## 为什么需要第三种状态

`agent.py` 的 `_run_child` 返回里原本只有 `status: "ok" | "failed"`
（由反思是否通过决定），**没有任何字段能让父 agent 区分**：

- 子代理真的失败了
- 子代理被写授权问题卡住了

三处消费点（`delegate` / `_synth` / `_replanner`）全都只看 `status != "ok"`，
于是子代理一旦被卡，父 agent 只会看到「researcher 失败」，
**不知道该去问用户什么** —— 换个角色重试也解决不了（还是缺那份租约）。

## 为什么「拿不准」在子代理层是**真实**的

顶层那句「拿不准用户是否授权」是**猜的**（我们在从一句话推断用户的意图）。
子代理层不一样：它手里的 `question` 是 `spec["hint"]`（角色提示词），
它**真的**不知道用户说了什么。所以在子代理层说「拿不准，请向用户确认」
是如实的，不是推诿。

而父 agent 手里**有**用户的原话 —— 由它去问，才问得对人。

## `_synth` 本来就有这个能力

合成提示里的第 3 条写着「列出仍未解决、需要用户确认的点」。
它只是**收不到**这个信号 —— 本笔把这个缺口接上。

## 字段必须必填

`needs_confirmation` 无内容时是 `dict()` 而不是省略：缺失会让老消费点静默
把它当普通结果，于是上浮通道形同虚设，而没有任何报错。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import pytest  # noqa: E402

import agent as agent_mod  # noqa: E402
from config import set_runtime  # noqa: E402
from test_write_intent_three_valued import _FINAL, _ScriptedLLM, _act  # noqa: E402

ROLE = "designer"          # 提示词含「创建」，改前会被误放行；改后无租约即拦
ACTION = "create_file"


def _child_run(role=ROLE, action=ACTION, lease=False, question=None):
    """跑一个 depth=1 的子代理，返回 (agent, 调用过的写工具, 事件)。"""
    called: list[str] = []
    old_tools = agent_mod.TOOLS
    tools = {action: {"func": lambda a: (called.append(a), "已写入")[1]},
             "read_file": {"func": lambda _a: "内容"}}
    try:
        agent_mod.TOOLS = tools
        a = agent_mod.Agent(
            llm=_ScriptedLLM([_act(action, "path: design.md"), _FINAL]),
            tool_registry=dict(tools), depth=1,
            tool_allowlist=list(agent_mod._SUBAGENT_ROLES[role]["tools"]))
        if lease:
            a.capability_lease = a._mint_write_lease()
        events = list(a.run(question or agent_mod._SUBAGENT_ROLES[role]["hint"],
                            stream=True))
        return a, called, events
    finally:
        agent_mod.TOOLS = old_tools
        set_runtime("code_root", "")


# --------------------------------------------------------------------------
# 事件通道
# --------------------------------------------------------------------------

def test_a_blocked_child_emits_a_needs_confirmation_event():
    """核心：无租约的子代理写不了，且**必须**发出 needs_confirmation 事件。"""
    a, called, events = _child_run()
    assert not called, "无租约的子代理不该能写"
    kinds = [e.get("type") for e in events if isinstance(e, dict)]
    assert "needs_confirmation" in kinds, (
        f"没有发出上浮事件，事件类型：{kinds} —— "
        "父 agent 会把子代理被卡误当成失败"
    )
    ev = next(e for e in events if e.get("type") == "needs_confirmation")
    assert ev.get("action") == ACTION
    assert "需要用户确认" in ev.get("text", "")


def test_a_child_with_a_lease_writes_and_emits_nothing():
    """反向守卫：有租约就正常写，**不发**上浮事件。

    否则父 agent 会去问一个根本不用问的问题。
    """
    a, called, events = _child_run(lease=True)
    assert called, "有租约却没能写"
    assert not [e for e in events
                if isinstance(e, dict) and e.get("type") == "needs_confirmation"]


def test_the_top_turn_never_emits_it():
    """反向守卫：顶层 agent 不发这个事件。

    顶层手里有用户的原话，它走的是 `_WRITE_UNCERTAIN_OBS` 那套「直接问用户」
    的措辞，不需要经子代理通道上浮。
    """
    from config import set_runtime as _sr

    called: list[str] = []
    old = agent_mod.TOOLS
    try:
        agent_mod.TOOLS = {"apply_edit": {
            "func": lambda a: (called.append(a), "已写入")[1]}}
        a = agent_mod.Agent(llm=_ScriptedLLM(
            [_act("apply_edit", "path: a"), _FINAL]))
        events = list(a.run("分析一下架构", stream=True))
    finally:
        agent_mod.TOOLS = old
        _sr("code_root", "")
    assert not called, "顶层在无授权时不该能写"
    assert not [e for e in events
                if isinstance(e, dict) and e.get("type") == "needs_confirmation"], (
        "顶层发了 needs_confirmation —— 它该走 uncertain 措辞直接问用户"
    )


# --------------------------------------------------------------------------
# 三处消费点
# --------------------------------------------------------------------------

def test_delegate_reports_needs_confirmation_not_failure(monkeypatch):
    """`delegate` 的措辞**不能**是「失败」。

    措辞决定了主 Agent 的行为：看到「失败」会去重试或放弃，
    而正确做法是去问用户。

    这里打桩 `_run_child` 而不是真跑 —— 要测的是**消费点怎么措辞**，
    真跑会连带子代理的脚本化 LLM，测的就不是这一层了。
    """
    import agent as A

    monkeypatch.setattr(
        A.Agent, "_run_child",
        lambda self, role, task, turn=None: {
            "status": "failed", "conclusion": "", "steps": 0,
            "error": "", "needs_confirmation": {
                "action": "apply_edit", "reason": "子代理缺少写授权租约"},
        })
    msg = A.Agent(llm=None)._delegate("role: coder\ntask: 把 a.py 改一下")
    assert "失败" not in msg, msg
    assert "需要用户确认" in msg, msg
    assert "不要重试" in msg or "不要当成失败重试" in msg, (
        f"应当明说不要重试，否则主 Agent 会换个角色再试一遍（还是缺租约）：{msg}"
    )


def test_delegate_still_reports_a_real_failure_as_failure(monkeypatch):
    """反向守卫：**真失败仍是失败**。

    本笔不能让 `delegate` 变成什么都报「需要确认」—— 那会让真失败
    也去问用户，白白打扰。
    """
    import agent as A

    monkeypatch.setattr(
        A.Agent, "_run_child",
        lambda self, role, task, turn=None: {
            "status": "failed", "conclusion": "", "steps": 0,
            "error": "子代理反思未通过：结论缺证据",
            "needs_confirmation": {},
        })
    msg = A.Agent(llm=None)._delegate("role: coder\ntask: 把 a.py 改一下")
    assert "失败" in msg, msg
    assert "需要用户确认" not in msg, msg


def test_run_child_actually_forwards_the_event_into_the_return(monkeypatch):
    """**最关键的一条**：`_run_child` 必须把事件收进 `output["needs_confirmation"]`。

    第一版守卫只验「子代理被挡」这个**结果**，于是变异把事件收集分支整个删掉时
    它照样全绿 —— 因为结果照样是「被挡」，只是上浮通道静默失效了。
    **断言的必须是通道本身，不是它的后果。**

    这里直接验 `_run_child` 的返回值，而不是间接看事件流。
    """
    import agent as A

    captured: dict = {}
    real_run = A.Agent.run

    def spy(self, question, stream=True, **kw):
        # 转发一个 needs_confirmation 事件，然后立刻收尾
        if stream:
            yield {"type": "needs_confirmation", "action": "create_file"}
            yield {"type": "final", "text": "Final Answer: x"}
            return
        yield {"type": "needs_confirmation", "action": "create_file"}
        yield {"type": "final", "text": "Final Answer: x"}

    monkeypatch.setattr(A.Agent, "run", spy)
    parent = A.Agent(llm=None)
    out = parent._run_child("designer", "做点事", turn=None)
    assert out.get("needs_confirmation"), (
        "_run_child 没有把 needs_confirmation 事件收进返回值 —— "
        "上浮通道静默失效，父 agent 会把「等确认」当成失败"
    )
    assert out["needs_confirmation"].get("action") == "create_file", out


def test_the_return_carries_no_confirmation_when_nothing_was_blocked(monkeypatch):
    """反向守卫：没被挡时该字段**存在但为空**，不是缺失、也不是非空。"""
    import agent as A

    def quiet(self, question, stream=True, **kw):
        if stream:
            yield {"type": "final", "text": "Final Answer: x"}
            return
        yield {"type": "final", "text": "Final Answer: x"}

    monkeypatch.setattr(A.Agent, "run", quiet)
    parent = A.Agent(llm=None)
    out = parent._run_child("designer", "做点事", turn=None)
    assert "needs_confirmation" in out, "字段缺失 —— 老消费点会静默当普通结果"
    assert not out["needs_confirmation"], f"没被挡却带了确认信息：{out}"


def test_synth_puts_a_need_into_the_prompt_it_builds(monkeypatch):
    """`_synth` 必须真的把它当**待确认项**放进合成输入。

    前面的守卫只数了「三处消费点的代码在不在」，那是**形态匹配**——
    删掉分支体它照样通过。变异实测里 `_synth` 那条正是这样没咬住。

    这里真跑一次 `_synth`，检查合成提示里**出现了**那句「需要用户确认」。
    """
    import agent as A

    seen: dict = {}

    class _LLM:
        provider = "p"
        model = "m"

        def chat(self, messages, stream=False, **kw):
            seen["prompt"] = messages[0]["content"]
            return "（合成结果）"

        def count_tokens(self, t):
            return 0

    parent = A.Agent(llm=_LLM())
    parent._child_llm = lambda: _LLM()
    tasks = [{"id": "t1", "role": "coder", "task": "改 a.py"}]
    results = {"t1": {"status": "failed", "conclusion": "", "steps": 0,
                      "error": "x", "needs_confirmation": {
                          "action": "apply_edit", "reason": "缺租约"}}}
    parent._synth(tasks, results)
    prompt = seen.get("prompt", "")
    assert "需要用户确认" in prompt, (
        f"合成输入里没有「需要用户确认」—— 合成提示第 3 条"
        f"「列出仍未解决、需要用户确认的点」就收不到信号。实际输入：{prompt[:300]}"
    )
    assert "不要当成失败重试" in prompt, prompt[:300]


def test_the_field_is_always_present_even_with_no_block():
    """反向守卫：`needs_confirmation` **必填**。

    省略它会让老消费点静默把它当普通结果 —— 上浮通道形同虚设，
    而且没有任何报错。
    """
    a, called, events = _child_run(lease=True)
    out = {"status": "ok"}
    # 直接验字段存在性：真跑一次 _run_child 太重，这里验契约。
    src = (Path(agent_mod.__file__).read_text(encoding="utf-8"))
    assert '"needs_confirmation"' in src, "返回结构里没有这个字段"
    assert '"needs_confirmation": dict(' in src, (
        "字段应当写成 dict(...)，保证无内容时也在（缺失=静默失效）"
    )


@pytest.mark.parametrize("consumer", ["delegate", "synth", "replanner"])
def test_all_three_consumers_know_about_the_field(consumer):
    """反向守卫：三处消费点都得认这个字段。

    只改一处的话，编排路径（`_synth` / `_replanner`）仍会把「等确认」
    当成「失败」，而 delegate 路径已经会问 —— 行为不一致且难查。
    """
    src = Path(agent_mod.__file__).read_text(encoding="utf-8")
    hits = src.count('r.get("needs_confirmation")') + \
        src.count('out.get("needs_confirmation")') + \
        src.count('r["needs_confirmation"]')
    assert hits >= 3, (
        f"只有 {hits} 处消费点认这个字段（期望 3：delegate / _synth / _replanner）"
    )