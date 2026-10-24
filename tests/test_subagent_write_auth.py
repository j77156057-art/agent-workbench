"""子代理的写授权来自结构，不来自角色提示词。

## 修的是什么

`agent.py:5669` 子代理跑 `child.run(question)`，而 `question = spec["hint"]`
—— **角色提示词，不是用户的请求**。护栏判的因此是那串硬编码文本：

| 角色 | 提示词里的字 | 改前判定 |
|---|---|---|
| coder | 「在授权范围内**改**代码」 | **denied** |
| designer | 「可以**创建**设计文档」 | granted |
| artist | 「**创建**或整理素材」 | granted |
| audio | 「在授权范围内**创建**配置」 | granted |
| researcher | 「不要改任何文件」 | denied |

**11 个角色里 10 个碰巧对，唯一错的就是 coder** —— 职责就是改代码的角色永远被拦。
而 designer/artist/audio 的写权限与用户说了什么**完全无关**。

端到端曾实测：用户「帮我改一下 player.gd 的数值配置」-> 主 agent 判 granted；
子代理判提示词 -> denied；拦截消息是**「用户没有要求创建或修改文件」**，
而用户明确要求了 —— 把不知道的说成了知道。

## 改后：权威来自两处结构性来源

1. **`tool_allowlist`** —— 4571 已拦不在名单里的工具；`_READ_ONLY_ROLES`
   又按 `ToolSpec` 把写工具从只读角色的请求里滤掉（那段代码的注释里
   已经写明「权威分类只有一份，不要手抄工具名清单」）。
2. **父 agent 的授权租约** —— `_run_child` 已继承（5430）。

**二者都要成立，缺一即拒。** 与 opencode 查授权表、Hermes 查工具开关同一个思路：
权威来自结构，不来自散文。

## 一个第一版引入的 fail-open（探针当场抓住）

`_lease_blocked` 在**没有租约时返回不拦**（`if not lease: return False, ""`）。
所以「租约不拦就放行」是错的 —— 那等于父 agent 从未授权也能写。
必须**额外要求租约存在**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

import agent as agent_mod  # noqa: E402
import tools as T  # noqa: E402

G, D = agent_mod._WRITE_INTENT_GRANTED, agent_mod._WRITE_INTENT_DENIED


def _child(role, lease=False):
    a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS), depth=1,
                        tool_allowlist=list(agent_mod._SUBAGENT_ROLES[role]["tools"]))
    if lease:
        a.capability_lease = a._mint_write_lease()
    return a


def _delegated_writes(a, action="apply_edit"):
    """**调生产代码**，不复刻它的逻辑。

    第一版这里写的是 `bool(a.capability_lease) and not a._lease_blocked(...)[0]`
    —— 一个副本。于是变异生产代码时守卫毫无反应：4 条变异只咬住 1 条，
    而没咬住的三条恰好全是安全方向最关键的。判据是「测的是不是被测物」。
    """
    return a._intent_for_delegated(action) == G


# --------------------------------------------------------------------------
# 正反两面：coder 必须能写，researcher 必须不能
# --------------------------------------------------------------------------

def test_coder_can_write_when_the_parent_granted():
    """**正向**：职责就是改代码的角色，父授权后必须能写。

    改前它被判 denied，因为提示词里是「在授权范围内**改**代码」而裸「改」
    不在词表里 —— 那个角色的存在意义被一句提示词的措辞否定了。
    """
    a = _child("coder", lease=True)
    assert _delegated_writes(a), "父已授权，coder 仍不能写"


def test_researcher_cannot_write_even_when_the_parent_granted():
    """**反向**：只读角色即使父授权也不该拿到写工具。

    与上一条是同一族的正反两面 —— 只测一面会漏。
    拦它的是 `tool_allowlist`（researcher 的工具表里没有写工具）。
    """
    a = _child("researcher", lease=True)
    assert "apply_edit" not in a.tool_allowlist, (
        "researcher 竟然拿到了 apply_edit —— 只读角色的工具表被改了"
    )


def test_coder_cannot_write_without_the_parent_granting():
    """**最关键**：父从未授权时，委派子代理**不能**写。

    这一条是第一版漏掉的 —— `_lease_blocked` 在没有租约时返回「不拦」，
    于是「租约不拦就放行」等于没有租约也能写。探针当场抓出来了。
    """
    a = _child("coder", lease=False)
    assert not a.capability_lease, "前置：本例不应有租约"
    assert not _delegated_writes(a), (
        "父 agent 从未授权，coder 却能写 —— 这是 fail-open"
    )


def test_an_expired_lease_does_not_authorize_the_child():
    """反向守卫：过期租约不等于授权。"""
    a = _child("coder")
    a.capability_lease = a._mint_write_lease(ttl_seconds=1)
    a.capability_lease["expires_at_epoch"] = 0.0     # 强行过期
    assert not _delegated_writes(a), "过期租约仍放行了委派写入"


def test_a_lease_missing_the_write_capability_does_not_authorize_the_child():
    """反向守卫：只有只读能力的租约不能授权写入。"""
    a = _child("coder")
    a.capability_lease = {"id": "x", "capabilities": ["read_local"],
                          "status": "active", "expires_at_epoch": 9e9}
    assert not _delegated_writes(a), "只读租约放行了写操作"


def test_a_released_lease_does_not_authorize_the_child():
    a = _child("coder")
    a.capability_lease = dict(a._mint_write_lease(), status="released")
    assert not _delegated_writes(a), "已释放的租约仍放行了委派写入"


# --------------------------------------------------------------------------
# 顶层回合必须完全不受影响
# --------------------------------------------------------------------------

def test_the_top_turn_gate_is_removed_always_granted():
    """「留地板、拆其余」：顶层回合（depth == 0）写意图护栏已拆，恒放行。

    旧行为里顶层仍读用户原话判 granted/denied；现在改由 tools.py 的灾难地板兜底，
    `_intent_for_top_turn` 不再据正则拦写。
    """
    def fresh():
        a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS))
        assert a.depth == 0, "前置：本例应是顶层 agent"
        return a

    assert fresh()._intent_for_top_turn("请修改 a.txt", "apply_edit") == G
    assert fresh()._intent_for_top_turn("分析一下架构", "apply_edit") == G
    assert fresh()._intent_for_top_turn("取消修改", "apply_edit") == G


def test_the_top_turn_mints_no_lease():
    """顶层不再铸造那张不含 EXEC、反而收窄能力的写租约。"""
    a = agent_mod.Agent(llm=None, tool_registry=dict(T.TOOLS))
    assert a._intent_for_top_turn("请修改 a.txt", "apply_edit") == G
    assert not a.capability_lease, "重构后顶层不应再铸租约"
    assert a._intent_for_top_turn("分析一下架构", "apply_edit") == G
    assert not a.capability_lease


def test_a_child_cannot_mint_its_own_lease():
    """反向守卫：委派层**不得**自己铸租约。

    铸租约是「用户批准了」这件事的物化，只有顶层能从用户的话里得出结论。
    子代理若也能铸，等于自己给自己授权。
    """
    a = _child("coder", lease=False)
    assert a.capability_lease == {}, "前置：起始应无租约"
    # 复刻 depth>0 分支：它只读租约，不写
    granted = a._intent_for_delegated("apply_edit") == G
    assert not granted
    assert a.capability_lease == {}, "委派层竟然铸出了租约"


# --------------------------------------------------------------------------
# 与角色表的一致性：声明与提示词不再各说各话
# --------------------------------------------------------------------------

def test_the_branch_is_chosen_by_depth_not_by_the_hint():
    """门级：**走主循环**，验「按 depth 选分支」这件事本身。

    前面那些守卫都直接调 `_intent_for_delegated`，所以它们**看不见调用点被改**。
    变异实测有一条因此没咬住：把 `if self.depth > 0` 改成
    `if self.depth > 0 and _write_intent(question) == "denied"` —— 那样
    designer（提示词 granted）会绕过委派分支、掉进顶层分支并**自己铸一份租约**。

    所以这里必须真跑一次主循环：depth=1 的 designer，无租约，尝试写。
    正确行为：委派分支 -> denied -> 拦截，且**没有**铸出租约。
    """
    from config import set_runtime
    from test_write_intent_three_valued import _FINAL, _ScriptedLLM, _act

    hint = agent_mod._SUBAGENT_ROLES["designer"]["hint"]
    called: list[str] = []
    old_tools = agent_mod.TOOLS

    def recorder(arg):
        called.append(arg)
        return "已写入"

    try:
        agent_mod.TOOLS = {"create_file": {"func": recorder},
                           "read_file": {"func": lambda _a: "内容"}}
        child = agent_mod.Agent(
            llm=_ScriptedLLM([_act("create_file", "path: design.md"), _FINAL]),
            tool_registry=dict(T.TOOLS), depth=1,
            tool_allowlist=list(agent_mod._SUBAGENT_ROLES["designer"]["tools"]))
        events = list(child.run(hint, stream=True))
    finally:
        agent_mod.TOOLS = old_tools
        set_runtime("code_root", "")

    assert not called, (
        "depth=1 且无租约时仍执行了写工具 —— 提示词里的「创建」不该决定权限"
    )
    assert not child.capability_lease, (
        "委派子代理自己铸出了租约 —— 铸租约是「用户批准了」这件事的物化，"
        "只有顶层能从用户的话里得出这个结论"
    )
    text = "\n".join(str(e.get("text", "")) for e in events if isinstance(e, dict))
    assert "未授权写操作" in text, text
    assert "未授权写操作" in text, text


def test_the_role_hint_no_longer_decides_write_permission():
    """记录一条**已被本笔消除**的缺陷。

    改前：护栏读提示词，于是 11 个角色里 10 个碰巧对、coder 错。
    这条断言现在成立，是因为提示词已**完全不再参与**写授权判定 ——
    权威只来自 `tool_allowlist` 与租约。
    """
    for role in ("designer", "artist", "audio"):
        hint = agent_mod._SUBAGENT_ROLES[role]["hint"]
        a = _child(role, lease=False)
        assert not _delegated_writes(a), (
            f"{role} 的提示词里有「创建」二字，曾让它在**无租约**时也被放行 —— "
            "现在提示词已不参与判定"
        )
        assert hint  # 提示词仍在（模型要读它），只是不再决定权限