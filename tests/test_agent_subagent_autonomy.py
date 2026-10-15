"""主 agent 能自定义子代理的工具集与角色名。

## 为什么要放开

`harness` 的职责是**给模型兜底**，不是**替模型决定**。此前 `_SUBAGENT_ROLES`
那 11 个手写角色是硬编码的**上限**：

- 主 agent 指定工具集时，代码与 `role_tools` 取**交集** → 只能往下收窄，
  造不出「能读文件、也能写设计文档、但不改代码」这种角色（`tester` 里没有
  `create_file`，`researcher` 里没有写工具）；
- 角色名不在那 11 个里 → 直接返回失败。

于是「该派一个 agent 去读那份长文档」这种判断只能由人硬塞进枚举，而它本该
由**看过问题的主 agent** 自己做。一个替模型把事全做完的 harness，和一段
手写的顺序 if-else 没有区别。

## 护栏没有被一起拆掉

- **真实工具集是唯一上限**：不存在的工具名仍然进不来。
- **套娃由 `SUBAGENT_MAX_DEPTH` 兜住**（硬拦、环境变量可调），不再依赖
  「模型不会主动要求 delegate」这种假设。
- 既有 11 个角色**行为不变**：不填工具集时仍用该角色的默认表。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"


def source() -> str:
    return AGENT.read_text(encoding="utf-8")


def _child_runner(tree: ast.AST) -> ast.FunctionDef:
    """取造子代理的那个函数体。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            body = ast.unparse(node)
            if "_SUBAGENT_ROLES.get(" in body and "SUBAGENT_MAX_DEPTH" in body:
                return node
    pytest.fail("找不到造子代理的函数——守卫扫不到它，本次改动可能漏改")


# --------------------------------------------------------------------------
# 第 1 点：工具集以主 agent 给的为准
# --------------------------------------------------------------------------


def test_requested_tools_are_not_intersected_with_the_role_table():
    """核心守卫：指定工具集时**不得**再与整张角色表取交集。

    反向守卫：把 `if str(name) in role_tools` 加回去，这条就红。交集意味着
    「角色表是上限」，而那正是要拆掉的东西。

    注意现在有两条分支（只读 / 非只读），**两条都要查**——只查第一条会被
    「在第二条里加回交集」溜过去。
    """
    fn = _child_runner(ast.parse(source()))
    offenders = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "allowed" not in targets:
            continue
        seg = ast.unparse(node.value)
        if "requested_tools" in seg and "role_tools" in seg:
            offenders.append(seg)
    assert not offenders, (
        "allowed 又在和 role_tools 取交集了——那等于角色表又是上限：\n"
        + "\n".join(offenders)
    )


def test_the_read_only_role_set_is_exactly_planner_and_dispatcher():
    """反向守卫：`_READ_ONLY_ROLES` 的成员必须**恰好**是那两个。

    这条是被实测逼出来的：把集合扩大成 `{"planner","dispatcher","coder"}`
    （于是 coder 也被判只读、拿不到写工具）时，**14 条守卫全绿** —— 之前只查
    「只读判定有没有被加额外条件」，没查常量本身。

    只读角色多一个，就有一个本该能写代码的角色被夺走工具；而少一个，就是一个
    自称只读的角色拿到了写工具。两头都是错。
    """
    import agent as agent_mod

    assert set(agent_mod._READ_ONLY_ROLES) == {"planner", "dispatcher"}, (
        f"只读角色集合被改了：{sorted(agent_mod._READ_ONLY_ROLES)}"
    )
    # coder / researcher / 自造角色都不能被误判只读
    for role in ("coder", "researcher", "reviewer", "文档撰写专员"):
        assert role not in agent_mod._READ_ONLY_ROLES, (
            f"{role} 被误判成只读角色"
        )


def test_the_write_tool_set_is_not_a_shadow_copy():
    """反向守卫：`agent.py` **不得**再持有工具分类的副本。

    我第一版实现是手写一个 `_WRITE_TOOLS = frozenset({...})` —— 被
    `tests/test_tool_taxonomy.py` 的 `NoShadowTaxonomyTests` 当场抓住，那条
    守卫的原话是：「它不在运行路径上，断言它等于断言一份与真实行为无关的清单
    ——这正是 `dev_apply_edits` 漏登记却一直绿灯的原因」。

    所以现在钉住：只读角色的拦截**必须**引用权威表（`agent_runtime.tools`），
    `agent.py` 里不得再出现自己的分类副本。
    """
    import agent as agent_mod

    for name in ("_WRITE_TOOLS", "_EXEC_TOOLS", "_ADMIN_TOOLS",
                 "_READ_ONLY_BLOCKED"):
        assert not hasattr(agent_mod, name), (
            f"agent.py 又出现了自己的分类副本 {name}——影子副本必然与权威表漂移"
        )


def _effective_tools_for_read_only(registry: dict, requested: list[str] | None, *,
                                   role: str = "planner", mcp_policy: str = "deny") -> set[str]:
    """真跑一次 `_run_child`，返回子代理**实际**拿到的工具清单。

    断言的是子代理提示里「允许工具=」那一行 —— 也就是真正生效的那份，
    不是任何中间变量。
    """
    import agent as agent_mod

    parent = agent_mod.Agent(llm=None)
    parent.tools = registry
    seen: list[str] = []

    class _Capture:
        provider = "p"
        model = "m"

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            for item in messages[-1:]:
                if isinstance(item, dict):
                    seen.append(str(item.get("content") or ""))
            text = "Final Answer: 分析完成"
            return iter([text]) if stream else text

        def count_tokens(self, text):
            return 1

    parent.llm = _Capture()
    kwargs = {"mcp_policy": mcp_policy, "reflect": False}
    if requested is not None:
        kwargs["tool_allowlist"] = requested
    parent._run_child(role, "只读分析", **kwargs)
    prompt = "\n".join(seen)
    assert "允许工具=" in prompt, prompt[:400]
    line = prompt.rsplit("允许工具=", 1)[-1].split("\n", 1)[0]
    return set(line.replace("，", ",").replace("、", ",")
               .replace(" ", ",").split(",")) - {""}


def test_explicit_allowlist_filtered_empty_does_not_restore_role_defaults():
    registry = {
        "read_file": {"func": lambda _a: ""},
        "dev_mcp_call": {"func": lambda _a: "", "capability": "network",
                         "side_effect": "irreversible", "parallel_safe": False},
        "apply_edit": {"func": lambda _a: "", "capability": "write_local",
                       "side_effect": "mutating", "parallel_safe": False},
    }
    assert _effective_tools_for_read_only(
        registry, ["dev_mcp_call"], role="planner", mcp_policy="deny") == set()
    assert _effective_tools_for_read_only(
        registry, ["missing_tool"], role="coder", mcp_policy="auto") == set()
    assert _effective_tools_for_read_only(
        registry, ["apply_edit"], role="planner", mcp_policy="deny") == set()


def test_missing_allowlist_still_uses_role_defaults():
    import agent as agent_mod
    parent = agent_mod.Agent(llm=None)
    parent.tools = {"read_file": {"func": lambda _a: ""}}
    assert _effective_tools_for_read_only(
        parent.tools, None, role="planner", mcp_policy="deny") == {"read_file"}


def test_explicit_allowlist_blocks_mcp_allow_promotion():
    """`mcp_policy=allow` 不得向显式 `tool_allowlist` 追加 MCP 工具。

    显式白名单是**硬上限**；`allow` 只是「未指定时给默认权限」的策略信号，
    不是「把 MCP 工具强制塞进去」的命令。回退到 OR 拼接会让 `[read_file]`
    + `allow` 实际拿到 `dev_mcp_call`，绕过调用方边界。
    """
    import agent as agent_mod
    from agent_runtime.tools import Capability, SideEffect

    registry = {
        "read_file": {"func": lambda _a: ""},
        "dev_mcp_call": {
            "func": lambda _a: "", "capability": Capability.NETWORK,
            "side_effect": SideEffect.IRREVERSIBLE, "parallel_safe": False,
        },
    }
    # 显式 allowlist=[read_file] + mcp_policy=allow → 只剩 read_file
    assert _effective_tools_for_read_only(
        registry, ["read_file"], role="coder", mcp_policy="allow") == {"read_file"}
    # 未提供 allowlist + mcp_policy=allow → 仍可拿 MCP（policy 仍生效）
    parent = agent_mod.Agent(llm=None)
    parent.tools = registry
    assert _effective_tools_for_read_only(
        registry, None, role="coder", mcp_policy="allow") >= {"read_file", "dev_mcp_call"}


def test_read_only_roles_cannot_widen_into_any_mutating_tool():
    """只读角色的拦截必须覆盖**每一个**会改东西的工具 —— 一个都不能漏。

    这条是被三次实测逼出来的，每次漏得都不一样：

    1. 最早只用 `WRITE_TOOLS` —— `python_exec` / `run_command` 在 `EXEC_TOOLS` 里，
       只读 planner 拿到了执行任意代码的工具。
    2. 后来改成三张表的并集 —— 实测**又漏了 14 个**会改东西的工具
       （`dev_patch` / `dev_move` / `memory_save` / `dev_lanes` …）：那些工具在
       注册项里显式声明了 capability，`coerce_tool_spec` 压根不查表。
    3. 补进表之后，并集在**今天**恰好等价了 —— 于是「并集」和「逐个问 spec」
       在行为上分不出来。没有守卫的话，下一个人加一个「注册声明、没进表」的
       工具，并集就会重新漏，而没有任何东西会红。

    所以这里刻意用**合成工具**造出「下一个新工具」的场景：它在注册项里声明
    自己会改东西，但**不在任何一张按名字索引的表里**（那正是那 14 个当时的
    处境）。判据只有逐个问 spec 才拦得住它。

    用合成工具而不是真工具，是因为真注册表今天已经补齐了——拿真工具测不出
    「并集 vs spec」的差别，那种测试会一直是绿的，而绿得没有意义。
    """
    mutating_but_unregistered = {
        # 声明自己写本地文件，但没进 WRITE_TOOLS
        "dev_write_thing": {"func": lambda _a: "", "capability": "write_local",
                            "side_effect": "mutating", "parallel_safe": False},
        # 声明自己改共享状态，但没进 ADMIN_TOOLS
        "dev_touch_state": {"func": lambda _a: "", "capability": "admin",
                            "side_effect": "mutating", "parallel_safe": False},
        # capability 是 network、但副作用不可逆：只看 capability 会把它当无害
        "dev_side_effect_call": {"func": lambda _a: "", "capability": "network",
                                 "side_effect": "irreversible",
                                 "parallel_safe": False},
    }
    registry = {"read_file": {"func": lambda _a: ""}, **mutating_but_unregistered}

    effective = _effective_tools_for_read_only(
        registry, ["read_file", *mutating_but_unregistered])
    for banned in mutating_but_unregistered:
        assert banned not in effective, (
            f"只读 planner 拿到了 {banned}（它不在任何名字表里，只按名字算就会"
            f"漏放它）：实际拿到 {sorted(effective)}"
        )
    assert "read_file" in effective, f"只读 planner 连读都读不了：{sorted(effective)}"


def test_the_read_only_predicate_asks_both_capability_and_side_effect():
    """反向守卫：**两个维度都要问**，少一个就等于把某一类工具放过去。

    变异实测过：把 `or spec.side_effect is not SideEffect.PURE` 删掉，
    全部守卫仍然绿 —— 因为我上一版查的是「函数体里有没有 `side_effect` 这个词」，
    而 `_run_child` 里别处本来就出现过这个词。**形态匹配天生可被绕开**。

    改成问行为，两个方向各造一个样本：

    · capability 无害但副作用不可逆（真注册表里的 `dev_mcp_call` 就是
      network + irreversible，可它被 `mcp_policy="deny"` 独立拦住了，
      测不出差别）→ 只有问 side_effect 才拦得住；
    · capability 说它改东西但 side_effect 写成了 pure → 只有问 capability
      才拦得住。这一条在今天**所有真实工具上都测不出来**（探针实测：真实
      注册表里 capability 会改东西的工具，side_effect 无一例外也是非 PURE，
      两个判据碰巧等价），所以必须造一个自相矛盾的合成工具，把这一维钉住。
      等价是巧合，不是判据——下一次有人给某个管理类工具标上 side_effect=pure
      （比如「这个操作幂等」），就正好是这一维在起作用。
    """
    synthetic = {
        "dev_network_but_irreversible": {
            "func": lambda _a: "", "capability": "network",
            "side_effect": "irreversible", "parallel_safe": False},
        "dev_admin_but_claims_pure": {
            "func": lambda _a: "", "capability": "admin",
            "side_effect": "pure", "parallel_safe": False},
    }
    registry = {"read_file": {"func": lambda _a: ""}, **synthetic}
    effective = _effective_tools_for_read_only(
        registry, ["read_file", *synthetic])
    assert "dev_network_but_irreversible" not in effective, (
        "capability 是 network 就被当成无害了——只看 capability 的写法拦不住它："
        f"实际拿到 {sorted(effective)}"
    )
    assert "dev_admin_but_claims_pure" not in effective, (
        "自己声明 side_effect=pure 就被放过了——只看 side_effect 的写法拦不住它："
        f"实际拿到 {sorted(effective)}"
    )


def test_every_mutating_tool_in_the_real_registry_is_blocked_for_read_only():
    """全量行为守卫：对**真实注册表**里每一个会改东西的工具逐个试一遍。

    上两条用合成工具是为了让「并集 vs spec」分得出来；这条负责兜住真实世界：
    今天 48 个工具会改东西，一个都不许漏放给只读角色。

    判据取自运行期 `ToolSpec`（capability ∈ 写/管/执行，或 side_effect 非 PURE），
    不查任何名字表 —— 名字表正是会漂移的那份。
    """
    import agent as agent_mod
    import tools as tools_mod
    from agent_runtime.tools import Capability, SideEffect, coerce_tool_spec

    caps = {Capability.WRITE_LOCAL, Capability.WRITE_EXTERNAL,
            Capability.ADMIN, Capability.EXEC}
    mutating = [n for n in tools_mod.TOOLS
                if (lambda s: s.capability in caps
                    or s.side_effect is not SideEffect.PURE)(
                        coerce_tool_spec(n, tools_mod.TOOLS[n]))]
    assert len(mutating) >= 40, (
        f"会改东西的工具只有 {len(mutating)} 个——分类表被大改过，"
        "这条守卫的前提需要重新确认")

    parent = agent_mod.Agent(llm=None)
    parent.tools = dict(tools_mod.TOOLS)
    seen: list[str] = []

    class _Capture:
        provider = "p"
        model = "m"

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            for item in messages[-1:]:
                if isinstance(item, dict):
                    seen.append(str(item.get("content") or ""))
            text = "Final Answer: 分析完成"
            return iter([text]) if stream else text

        def count_tokens(self, text):
            return 1

    parent.llm = _Capture()
    parent._run_child("planner", "只读分析", tool_allowlist=mutating,
                      mcp_policy="deny", reflect=False)
    effective = set("\n".join(seen).rsplit("允许工具=", 1)[-1]
                    .split("\n", 1)[0]
                    .replace("，", ",").replace("、", ",").replace(" ", ",")
                    .split(",")) - {""}
    leaked = sorted(effective & set(mutating))
    assert not leaked, (
        "只读 planner 拿到了这些会改东西的工具：%s\n"
        "角色自称只读、实际改了文件，而报告里这次改动仍记为只读分析" % leaked)


def test_the_external_file_tools_are_now_in_the_authoritative_table():
    """根因已修：external 三兄弟不再是「靠手写例外名单拦住」。

    早先这条守卫钉的是**现状**：它们不在任何权威表里，`agent.py` 旁边手写
    一份例外名单才拦住。那是影子副本——正是 `test_tool_taxonomy.py` 存在的
    意义所反对的形态。

    现在它们在 `WRITE_TOOLS` 里，行为不变（仍然拦得住），但拦住它们的是
    权威表而不是那份名单。所以这条守卫改成断言**根因已修**：
    在表里，且只读角色仍拿不到。
    """
    import agent as agent_mod
    import tools
    from agent_runtime.tools import WRITE_TOOLS

    EXTERNAL = ("create_external_file", "edit_external_file",
                "delete_external_file")
    for name in EXTERNAL:
        assert name in WRITE_TOOLS, (
            f"{name} 仍不在权威写工具表里——只读角色拦住它靠的又是手写名单，"
            "下一个新工具照样会漏"
        )

    parent = agent_mod.Agent(llm=None)
    parent.tools = {"read_file": {"func": lambda _a: ""}}
    parent.tools.update({name: dict(tools.TOOLS[name]) for name in EXTERNAL})
    seen: list[str] = []

    class _Capture:
        provider = "p"
        model = "m"

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            for item in messages[-1:]:
                if isinstance(item, dict):
                    seen.append(str(item.get("content") or ""))
            text = "Final Answer: 分析完成"
            if stream:
                return iter([text])
            return text

        def count_tokens(self, text):
            return 1

    parent.llm = _Capture()
    parent._run_child("planner", "只读分析",
                      tool_allowlist=["read_file", *EXTERNAL],
                      reflect=False)
    effective = "\n".join(seen).rsplit("允许工具=", 1)[-1].split("\n", 1)[0]
    for banned in EXTERNAL:
        assert banned not in effective, (
            f"只读 planner 拿到了 {banned}：{effective}"
        )
    assert "read_file" in effective, f"只读 planner 连读都读不了：{effective}"


def test_the_nesting_denylist_is_not_reintroduced_on_the_main_branch():
    """反向守卫：防止有人把那层硬排除悄悄加在**非只读**分支上。

    之前那条只查「函数体里有没有 delegate/orchestrate」这种粗粒度形态，
    实测把排除加回 `else` 分支（自造角色那条路）时它不响。而那里加它是对的
    ——已经明确移除的那层，不该被顺手恢复。
    """
    fn = _child_runner(ast.parse(source()))
    body = ast.unparse(fn)
    m = re.search(r'else:\n(.*?allowed = \[.*?\])\n', body, re.S)
    assert m, "找不到非只读分支的 allowed 构造"
    seg = m.group(1)
    for banned in ("delegate", "orchestrate", "start_workflow"):
        assert banned not in seg, (
            f"非只读分支又加回了对 {banned} 的硬排除——那层已被明确移除"
        )


def test_both_branches_still_filter_against_the_real_tool_registry():
    """反向守卫：两个分支都必须以 `self.tools` 为上限。

    实测把非只读分支改成不筛（凭空造的工具名也能进来）时不响——之前那条正则
    只匹配 `allowed = [` 的**第一个**出现，落在只读分支上，而那条没变。
    """
    fn = _child_runner(ast.parse(source()))
    branches = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "allowed" in targets:
                seg = ast.unparse(node.value)
                # 只查「主 agent 指定」那条路。`list(spec['tools'])`（不填工具
                # 时用角色默认表）不需要筛 —— 函数后面本来有一道
                # `[name for name in allowed if name in self.tools]` 兜全部来源。
                if "requested_tools" in seg:
                    branches.append(seg)
    assert len(branches) == 2, (
        f"应当有两条使用 requested_tools 的分支（只读/非只读），实际 {len(branches)}：\n"
        + "\n".join(branches)
    )
    for seg in branches:
        assert "self.tools" in seg, (
            f"这条分支没有以真实工具集为上限：\n  {seg}"
        )


def test_omitting_tools_still_uses_the_role_default():
    """反向守卫：**不能**把既有行为的另一半也改了。

    偷懒修法会把 `else: allowed = list(spec["tools"])` 一起删掉，或改成
    `allowed = list(self.tools)`——那样每个角色都拿到全量工具，11 个角色的
    默认表形同虚设。

    注意用 AST 查而不是正则：`ast.unparse` 会把 `"tools"` 规范化成
    `'tools'`，正则按双引号写会永远不成立（实测连续踩了两次）。
    """
    fn = _child_runner(ast.parse(source()))
    found = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "allowed" not in targets:
            continue
        seg = ast.unparse(node.value)
        # list(spec['tools']) —— 角色默认表
        if seg in {"list(spec['tools'])", 'list(spec["tools"])'}:
            found = True
    assert found, "不指定工具集时必须仍用该角色的默认表 list(spec['tools'])"


def test_existing_roles_keep_their_defaults():
    """既有 11 个角色的默认工具表必须一个都没变。"""
    import agent as agent_mod

    roles = agent_mod._SUBAGENT_ROLES
    assert "researcher" in roles and "coder" in roles and "tester" in roles
    # 之前记录过的关键事实，防止「顺手改表」
    assert "create_file" not in roles["researcher"]["tools"]
    assert "create_file" not in roles["tester"]["tools"]
    assert "apply_edit" in roles["coder"]["tools"]
    assert "run_command" in roles["tester"]["tools"]


# --------------------------------------------------------------------------
# 第 2 点：角色名不再限于那 11 个
# --------------------------------------------------------------------------


def test_an_unknown_role_is_no_longer_rejected():
    """核心守卫：不在枚举里的角色名**不得**直接失败。

    这条曾经只查字面量 `"未知角色「{"` —— 实测把拒绝逻辑用 `%s` 格式化
    加回去，**11 条守卫全绿**。所以现在改成查**控制流**：`_run_child` 里
    不得存在「角色未命中枚举 → return 失败」这条路径。
    """
    fn = _child_runner(ast.parse(source()))
    role_name = "长文阅读专员"          # 故意不在 _SUBAGENT_ROLES 里
    source_text = source()
    assert f'"{role_name}"' not in source_text, "这个角色名意外已在枚举里"

    # 结构上：命中/未命中两条路都必须能走到函数末尾（造子代理），
    # 不能有一条是提前 return failed。
    body = ast.unparse(fn)
    early_returns = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.FunctionDef):
            continue
        # 找到那个构造 child 的赋值，它后面不该再有「角色未命中就返回」的分支
    # 简化但严格：函数体里所有 return 的返回值中，不得有「未知角色」类文案，
    # 且 ad_hoc_role 分支必须不是 return。
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            seg = ast.unparse(node.value)
            if "角色" in seg and ("未知" in seg or "可选" in seg or "没给" not in seg):
                # 「没给角色名」是允许的（缺输入要说人话），其余拒绝不允许
                if "没给角色名" not in seg:
                    early_returns.append(seg)
    assert not early_returns, (
        "仍然存在「角色未命中就拒绝」的返回路径：\n" + "\n".join(early_returns)
    )
    assert "ad_hoc_role" in body, "自造角色的分支不见了"


def test_an_unknown_role_actually_runs(tmp_path):
    """**行为级**守卫：真造一个不在枚举里的子代理，看它是否被成功创建。

    前面几条都是结构/字面量断言——它们已经被证明会假绿。这条直接调
    `_run_child`，用一个假 LLM 跑一遍，断言拿到的是「跑了」而不是
    「未知角色」。只有它能真正把这条护栏钉住。
    """
    import agent as agent_mod

    class _FakeLLM:
        def chat(self, messages, stream=False, **kwargs):
            return "Final Answer: 已读完那份长文档，关键约束 3 条。"

    parent = agent_mod.Agent(llm=_FakeLLM())
    try:
        parent.llm = _FakeLLM()
    except Exception:
        pass

    out = parent._run_child(
        "长文阅读专员",
        "通读那份 300 页的引擎规格书，抽出与碰撞检测相关的约束",
        persona="你专门负责长文档通读，产出带页码的要点清单",
        tool_allowlist=["read_file"],
    )
    assert "未知角色" not in str(out.get("error") or ""), (
        f"自造角色被拒绝了：{out.get('error')}"
    )
    assert str(out.get("status")) in {"ok", "failed"}, out
    # 真正的失败原因不该是「角色不认识」
    assert "长文阅读专员" in str(out.get("conclusion") or "") or \
        "未知角色" not in str(out.get("error") or "")


def test_a_missing_role_name_still_gets_a_usable_message():
    """反向守卫：完全不给角色名仍然要说清怎么办，而不是造一个空角色。

    这是「放开」和「变成没边界」的区别 —— 缺输入要说人话。
    """
    text = source()
    assert "没给角色名" in text, "没给角色名时应给出可执行的提示"
    assert "也可以自己起一个" in text, "提示要告诉主 agent 它可以自造角色"


def test_an_ad_hoc_role_tells_the_child_who_it_is():
    """自造角色时，子代理必须知道自己是「主 agent 为这次任务专门设的」。

    反向守卫：只把 `spec["hint"]` 置空而不补说明的话，子代理收到的第一句
    直接跳到「工具策略」——它不知道主 agent 为什么派它出来。这条对应用户
    描述的模型：「子 agent 的身份、应该做什么，都是主 agent 设计的」。
    """
    fn = _child_runner(ast.parse(source()))
    body = ast.unparse(fn)
    assert "ad_hoc_role" in body
    lines = body.splitlines()
    hint_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.strip() == "question = spec['hint']"),
        -1,
    )
    assert hint_idx >= 0, f"找不到 question = spec['hint']\n{body[:400]}"
    after = "\n".join(lines[hint_idx:hint_idx + 12])
    assert "ad_hoc_role" in after, "自造角色时没有补身份说明"
    assert "主 Agent" in after or "主Agent" in after, (
        "补的身份说明里要写明是主 agent 派出来的"
    )


def test_ad_hoc_roles_are_not_forced_read_only():
    """反向守卫：**不能**把所有非枚举角色都当成 planner/dispatcher。

    只读判定用的是 `_READ_ONLY_ROLES`（恰好 planner + dispatcher），且主 agent
    自造的角色名不会命中它——自造角色不受只读约束。

    这里踩过两次坑：
    1. 最初用字面量 `if role in {"planner", "dispatcher"}` 钉——换个写法就漏；
    2. 改成正则只提取**集合成员**，仍然漏：变异把条件改成
       `if role in {...} or not spec.get("hint")` 时，集合没变（断言照样通过），
       而「非枚举角色一律只读」这条尾巴已经挂上去了。

    所以改成查**整个 if 条件文本** —— 集合之后不许再跟任何额外条件。
    """
    fn = _child_runner(ast.parse(source()))
    body = ast.unparse(fn)
    conditions = re.findall(r'^\s*(if role_name in \{[^}]*\}[^\n:]*):', body, re.M)
    conditions += re.findall(r'^\s*(if role_name in _READ_ONLY_ROLES[^\n:]*):',
                             body, re.M)
    assert conditions, "找不到只读角色的判定"
    for cond in conditions:
        seg = cond.split("if", 1)[1]
        if "{planner" in seg or "'planner'" in seg or '"planner"' in seg:
            extra = re.sub(r'^\s*role_name in \{[^}]*\}', "", seg).strip()
            assert not extra, (
                "只读判定被加了额外条件——自造角色会被误判只读："
                f"\n  {cond.strip()}"
            )
            members = set(re.findall(r"'([^']+)'", seg))
            members |= set(re.findall(r'"([^"]+)"', seg))
            assert members == {"planner", "dispatcher"}, (
                f"只读角色的集合被改了：{sorted(members)}"
            )


def test_read_only_roles_still_cannot_widen_into_write_tools():
    """反向守卫：**只读角色的只读是它名字本身的一部分**，不随「放开限制」一起被拆。

    `planner` / `dispatcher` 这两个名字就是「只分析、不改文件」的承诺。若允许
    它们通过 `tool_allowlist` 拿到 `apply_edit`，就会出现「角色自称只读、实际
    改了文件，而报告里这次改动仍记为只读分析」——记录与事实不符。

    这条与「自造角色可以自由给工具」不冲突：自造角色走的是另一条路。
    """
    import agent as agent_mod

    parent = agent_mod.Agent(llm=None)
    parent.tools = {
        "search_code": {"func": lambda _a: ""},
        "read_file": {"func": lambda _a: ""},
        "apply_edit": {"func": lambda _a: ""},
        "python_exec": {"func": lambda _a: ""},
    }
    out = parent._run_child(
        "planner", "拆解文件和依赖",
        tool_allowlist=["apply_edit", "python_exec"],
        mcp_policy="deny", reflect=False,
    )
    # 断言真正生效的地方：子代理收到的提示里写了哪些「允许工具」
    seen: list[str] = []

    class _Capture:
        provider = "p"
        model = "m"

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            for item in messages[-1:]:
                if isinstance(item, dict):
                    seen.append(str(item.get("content") or ""))
            text = "Final Answer: 拆解完成"
            if stream:
                return iter([text])
            return text

        def count_tokens(self, text):
            return 1

    parent.llm = _Capture()
    parent._run_child("planner", "拆解文件和依赖",
                      tool_allowlist=["apply_edit", "python_exec"],
                      mcp_policy="deny", reflect=False)
    prompt = "\n".join(seen)
    assert "允许工具=" in prompt, prompt[:300]
    effective = prompt.rsplit("允许工具=", 1)[-1].split("\n", 1)[0]
    assert "apply_edit" not in effective, f"只读 planner 拿到了写工具：{effective}"
    assert "python_exec" not in effective, f"只读 planner 拿到了执行工具：{effective}"
    # 显式 allowlist 是**硬上限**（见 9f8ef17 /
    # test_explicit_allowlist_filtered_empty_does_not_restore_role_defaults）：
    # 本例请求的全是写/执行工具，被 _mutates 过滤后有效集应为空——绝不因"空了
    # 就回填角色默认工具"而变相放宽。只读承诺据此守住：没有任何写工具进有效集。
    # 「只读 planner 仍能读」这条能力由 test_missing_allowlist_still_uses_role_defaults
    # （不传 allowlist → 角色默认只读集）单独覆盖，不在这里用旧的回填语义重复钉。
    assert effective.strip() == "", (
        f"只请求写工具时有效集应为空（硬上限，不回填默认）：{effective!r}"
    )
    # python_exec / run_command / dev_commit 那三张表的覆盖由
    # test_read_only_blocklist_covers_all_three_authoritative_tables 钉，不重复。


def test_an_ad_hoc_role_can_still_get_write_tools():
    """反向守卫：只读约束**只**作用于那两个角色名，别顺手扩到自造角色上。

    主 agent 自己起的角色、自己写的人设，要能写就直说——否则「自造角色」这条
    新开的路对最常见的「派个 agent 去写文档/改代码」依然是死的。
    """
    import agent as agent_mod

    parent = agent_mod.Agent(llm=None)
    parent.tools = {"read_file": {"func": lambda _a: ""},
                    "create_file": {"func": lambda _a: ""}}
    seen: list[str] = []

    class _Capture:
        provider = "p"
        model = "m"

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            for item in messages[-1:]:
                if isinstance(item, dict):
                    seen.append(str(item.get("content") or ""))
            text = "Final Answer: 文档写好了"
            if stream:
                return iter([text])
            return text

        def count_tokens(self, text):
            return 1

    parent.llm = _Capture()
    parent._run_child("文档撰写专员", "把规格整理成文档",
                      tool_allowlist=["read_file", "create_file"],
                      reflect=False)
    prompt = "\n".join(seen)
    effective = prompt.rsplit("允许工具=", 1)[-1].split("\n", 1)[0]
    assert "create_file" in effective, (
        f"自造角色拿不到写工具——这条新开的路就白开了：{effective}"
    )


# --------------------------------------------------------------------------
# 护栏仍然在
# --------------------------------------------------------------------------


def test_nesting_is_still_bounded_by_depth():
    """放开工具限制**不等于**放开无限套娃 —— 深度上限必须还在。"""
    text = source()
    assert "SUBAGENT_MAX_DEPTH" in text
    assert re.search(r"if self\.depth >= SUBAGENT_MAX_DEPTH", text), (
        "深度硬拦不见了——放开工具限制的同时必须留着它"
    )


def test_the_removed_tool_denylist_is_not_silently_reintroduced():
    """反向守卫：防止有人把那层「不给委派类工具」悄悄加回来。

    它已被有意移除（理由写在了代码注释里）。加回来是**改变已定的产品判断**，
    必须先跟用户确认，而不是在一次「顺手收紧」里发生。
    """
    fn = _child_runner(ast.parse(source()))
    body = ast.unparse(fn)
    for banned in ("delegate", "orchestrate", "start_workflow"):
        # 只允许它出现在**注释性文字**之外的真实代码里
        code_only = re.sub(r"#.*", "", body)
        assert not re.search(rf'not in \{{[^}}]*{banned}', code_only), (
            f"委派类工具 {banned} 的硬排除又回来了——那层已被明确移除"
        )


def test_comments_do_not_reference_removed_code():
    """注释不许指向已经不存在的代码。

    实测踩到过：移除那层硬排除后，注释里还留着「下面的『不给子代理委派类
    工具』」——而那段代码已经没了。读代码的人会去找一个不存在的东西。
    """
    fn = _child_runner(ast.parse(source()))
    text = ast.unparse(fn)
    assert "role_tools" not in text, (
        "函数体里还引用 role_tools，但它已经被移除"
    )
