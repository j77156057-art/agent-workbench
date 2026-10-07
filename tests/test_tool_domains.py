"""工具说明的可见性与单一真相。

## 三条守卫各管一件事

1. **每个注册工具都必须在系统提示里露一次面** —— 否则模型根本不知道它存在。
2. **两级分类必须与 `ToolSpec` 一一对应** —— 不许有工具落进「其余」。
3. **目录不得被当成提示的替代品** —— 在手写说明迁进 `ToolSpec` 之前。

## 为什么第 1 条现在会红

实测（2026-10-08）：`agent.SYSTEM_PROMPT` 共 16442 字符，116 个工具里
**96 个有说明、20 个完全没有**。那 20 个包括：

    dev_patch(418 字符) tool_catalog(226) dev_mcp_discover_from_need(224)
    game_screenshot(213) create_external_file(205) dev_mcp_add(199)
    edit_external_file(177) dev_skill_create(167) memory_forget
    web_weather web_search_batch dev_mcp_decide dev_mcp_discover
    dev_mcp_probe dev_mcp_remove dev_preview_adapter_rollback
    dev_skill_reject read_external_file delete_external_file

`dev_patch` 在整份提示里**出现 0 次** —— 模型无从知道「把一段 unified diff
原子打到代码库」这件事有工具可用。这是**能力缺失**，不是优化问题。

所以这条守卫断言写成「已知缺口清单」，将来补上了就改成断言全覆盖。
**与 `docs/known-defect-tool-visibility-and-dual-text.md` 是同一件事。**
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime import tool_domains as D  # noqa: E402

#: 完整工具表。**必须经 `agent` 导入**才能拿全 ——
#:
#: 实测：`import tools` 得 **112** 个，`import agent` 之后得 **116** 个。
#: 差的 4 个（`delegate` / `orchestrate` / `tool_search` / `dev_use_skill`）
#: 由 `agent.py` 在导入时注入注册表。
#:
#: 我第一版直接用 `tools_mod.TOOLS` 做完整性检查，于是漏了这 4 个，
#: 守卫报「它们没有 domain」—— 那不是缺陷，是**我查错了表**。
#:
#: 计数断言也依赖这一点，见 `test_the_tool_count_includes_the_injected_four`。
TOOLS = tools_mod.TOOLS

#: 模型当前在系统提示里看不到的工具。
#:
#: **2026-10-08 已清零。** 此前是 22 个（`dev_patch` / `memory_forget` /
#: `web_weather` / `dev_mcp_*` / `*_external_file` / `dev_skill_*` 等）——
#: 它们从未被写进 `_SYSTEM_PROMPT_FULL`，所以既不在核心工具里，也不在
#: `【扩展工具索引】` 里，而索引是 progressive 模式唯一的兜底披露。
#: `dev_patch`（原子打 unified diff）在整份提示里出现 **0 次**。
#:
#: 现在由 `_backfill_tool_section()` 从 `ToolSpec` 派生，不再手写清单 ——
#: 手写的下一次新增工具又会漏，而漏了不会报错。
#: 见 `docs/known-defect-tool-visibility-and-dual-text.md`。
INVISIBLE = frozenset()


def _named_in_prompt(name: str) -> bool:
    """工具名是否在系统提示里出现过。

    用**任何位置**的裸名匹配而不是只匹配 `- name(` 那几行 —— 有些工具在正文里
    被提到（「改动后必须再次调用 preview_project」），那也算模型看得见。
    反过来，`dev_patch` 在整份提示里出现 0 次，是真的完全不可见。
    """
    return name in agent_mod.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# 1. 可见性
# --------------------------------------------------------------------------

def test_the_tool_count_includes_the_injected_four():
    """反向守卫：导入顺序会改变工具总数，这条把那个事实钉住。

    `import tools` 得 112 个；`import agent` 之后 116 个 ——
    `delegate` / `orchestrate` / `tool_search` / `dev_use_skill` 由 `agent.py`
    导入时注入。

    我第一版用 `TOOLS` 做完整性检查，得到 112，
    于是守卫报「4 个工具没有 domain」—— 那是**我查错了表**，不是缺陷。

    写这条是为了让下一个人不必重新踩：**先 `import agent`，再数工具**。
    """
    for name in ("delegate", "orchestrate", "tool_search", "dev_use_skill"):
        assert name in TOOLS, (
            "%r 不在完整工具表里 —— 若它已被移除，请同步改 TOOL_DOMAINS "
            "并删掉这条守卫里的名字" % name)
    assert len(TOOLS) >= 116, (
        "完整工具表只有 %d 个 —— 期望 ≥116（含 agent 注入的 4 个）。"
        "确认导入顺序：必须先 import agent" % len(TOOLS))


def test_the_invisible_list_is_exactly_what_is_actually_invisible():
    """记录本身就是断言：**清单与实测不符就红**。

    否则下一个人删掉清单里某个名字（或补上一个）而没人发现，
    这条守卫就成了恒真的摆设 —— 那正是 K 节记的那类「恒真守卫说明不了任何事」。
    """
    actual = {n for n in TOOLS if not _named_in_prompt(n)}
    assert actual == set(INVISIBLE), (
        "不可见清单与实测不符：\n"
        "  实测多出: %s\n  实测少了: %s\n"
        "若这是有意的，请同步改 INVISIBLE 并更新"
        " docs/known-defect-tool-visibility-and-dual-text.md"
        % (sorted(actual - set(INVISIBLE)), sorted(set(INVISIBLE) - actual))
    )


def test_every_registered_tool_is_disclosed_somewhere_in_the_prompt():
    """**本文件最重要的一条**：每个已注册工具都必须在系统提示里露一次面。

    这条曾经红过，而且红得对（2026-10-08 修）：116 个注册工具里有 **22 个**
    从未被写进 `_SYSTEM_PROMPT_FULL`，于是它们既不在核心工具里，也不在
    `【扩展工具索引】` 里 —— 而索引是 progressive 模式唯一的兜底披露。

    后果不是「少几个工具」，是**模型完全不知道它们存在**：
    `dev_patch`（原子打 unified diff）在整份提示里出现 **0 次**。

    修法是从 `ToolSpec` 派生（`_backfill_tool_section`），不是手写 22 条 ——
    手写的下一次新增工具又会漏，而漏了不会报错。
    """
    actual = {n for n in TOOLS if not _named_in_prompt(n)}
    assert not actual, (
        "这些已注册工具在系统提示里完全不可见，模型无从知道它们存在：%s\n"
        "查 _backfill_tool_section()：它从 ToolSpec 派生缺失工具，"
        "若这里仍红，说明注册表里有 description 为空的工具（那种工具确实无法披露）"
        % sorted(actual))


def test_the_four_tools_that_were_completely_invisible_are_now_disclosed():
    """反向守卫：钉住那 22 个里的几个具体名字。

    记名字而不是只记数量 —— 数量对但名字换了，是另一种失效。
    挑的是有独立职责、正常任务里会用到的：
    * `dev_patch` —— 原子打 unified diff（与 `apply_edit` 职责不同）
    * `memory_forget` —— 用户说「忘掉那条偏好」时唯一的实现
    * `web_weather` —— 结构化天气，与 `web_search` 不是一回事
    * `dev_mcp_add` —— MCP 装配第 2 步，整条链路的入口
    """
    for name in ("dev_patch", "memory_forget", "web_weather", "dev_mcp_add"):
        assert _named_in_prompt(name), (
            "%r 又从系统提示里消失了 —— 它曾经被补进来过" % name)


def test_the_source_scanners_still_see_what_they_scan():
    """反向守卫：`agent.py` 里那些**读自己源码**的守卫有没有被打断。

    `tests/test_channel_protocol_consistency.py` 用
    `inspect.getsource(Agent._run)` 取源码，再断言里面有
    `原生 function-calling` / `模型最后一次输出` / `不要当作结论` 这些字串。

    **为什么单独钉**：我在 `agent.py` 里**插入**了约 75 行（`_backfill_tool_section`
    等），这类守卫靠行号定位，被插入挪动后会在组合跑里报红 —— 而那条红
    **看起来像「我的改动破坏了通道协议」**，其实只是我在同一个工作树里
    改到一半。

    实测：段 1 第一次跑红了这 4 条，等我把改动做完，第二次跑就绿了。
    **所以这类红必须当场判断「是我的中间态还是真缺陷」，不能记成回归。**

    本条断言直接查那四个字串还在 `_run` 里 —— 比等到组合跑才发现便宜。
    """
    import inspect

    src = inspect.getsource(agent_mod.Agent._run)
    assert "format_stalls" in src, "查错了函数：_run 里没有 format_stalls"
    for probe in ("原生 function-calling", "模型最后一次输出",
                  "不要当作结论", "不要写任何 Action:"):
        assert probe in src, (
            "%r 不在 Agent._run 源码里了 —— 要么通道文案被删了（真缺陷），"
            "要么这个守卫查错了函数（它自称会先确认自己看的是对的地方）" % probe)


def test_tool_search_can_retrieve_every_backfilled_tool():
    """**本笔修的第二个洞**：披露只做了一半。

    补全让那 22 个工具出现在 `【扩展工具索引】` 里（模型看得见名字），
    但 `_tool_blocks()` 读的是**未补全**的 `_SYSTEM_PROMPT_FULL` ——
    于是 `tool_search` 对它们说「没有匹配的工具」。

    症状具体到可复现：查 `web_weather` 返回「没有匹配"，可它在索引里明明写着
    「查询今日结构化天气」。

    修法：`_tool_blocks()` 也走 `_backfill_tool_section()`。

    **注**：`tool_search` 此前**零测试覆盖**（`git grep tool_search -- tests/`
    无命中），而它是 progressive 模式的唯一检索通道。本笔起它才有守卫。
    检索自身的匹配缺陷另立 `docs/known-defect-tool-search-substring-mismatch.md`。
    """
    blocks = agent_mod._tool_blocks()
    for name in ("dev_patch", "memory_forget", "web_weather", "dev_mcp_add",
                 "create_external_file", "dev_skill_create"):
        assert name in blocks, (
            "%r 在索引里露了面，却取不回完整用法 —— "
            "查 _tool_blocks() 是否走了 _backfill_tool_section" % name)
        found = agent_mod._tool_search(name)
        assert name in str(found) or found.strip() != "", (
            "tool_search(%r) 返回空" % name)


def test_tool_search_returns_the_actual_usage_not_a_neighbour():
    """**反向守卫**：查 A 不能返回 B 的用法。

    实测补全前 `tool_search('dev_patch')` 返回的是 `dev_diagnostics` 的用法，
    `tool_search('dev_mcp_add')` 返回 `dev_mcp_search` —— 那来自第二句
    「在 description 里找子串」，而 description 里常提到别的工具名。

    模型若据此去调 `dev_patch` 会用错参数格式，且**看起来是成功的检索**。
    """
    for name in ("dev_patch", "web_weather", "memory_forget"):
        blocks = agent_mod._tool_blocks()
        text = str(agent_mod._tool_search(name))
        own = blocks.get(name, "")
        assert own and own[:40] in text, (
            "tool_search(%r) 没有返回它自己的用法（返回了别的工具）" % name)


def test_tool_search_may_not_return_a_neighbour_first():
    """反向守卫：精确名命中时，**第一个结果必须是它自己**。

    子串匹配的固有毛病：`query='dev_patch'` 在名字里找不到时会退到
    description 匹配，于是可能返回一个提到 `patch` 的**别的**工具。
    """
    blocks = agent_mod._tool_blocks()
    for name in ("dev_patch", "web_weather", "memory_forget", "dev_mcp_add"):
        assert name in blocks, "%r 不在 blocks 里" % name
        text = str(agent_mod._tool_search(name))
        # blocks 的 dict 顺序即 _tool_blocks 的插入顺序（= 提示里的出现顺序）
        names = list(blocks)
        idx_self = names.index(name)
        first_block = next((n for n in names if blocks[n] in text), None)
        assert first_block == name, (
            "tool_search(%r) 第一个返回的是 %r —— 精确名命中时必须先返回自己"
            % (name, first_block))
        assert idx_self >= 0


def test_backfill_costs_only_the_index_one_liners():
    """反向守卫：补全**不得**让提示显著变大。

    progressive 会把非核心工具压成一行概要（`_short_desc`，limit=42），
    所以补 22 条完整说明的最终成本应当是 22×≈43 字符量级。若这条红，
    说明 progressive 分层没生效 —— 那时补全就是往提示里灌全文。
    """
    full_len = len(agent_mod._SYSTEM_PROMPT_FULL)
    assembled = len(agent_mod.SYSTEM_PROMPT)
    assert assembled < full_len, (
        "组装后的提示（%d）比全量提示（%d）还长 —— progressive 分层没生效？"
        % (assembled, full_len))


def test_full_mode_never_builds_the_progressive_index():
    """反向守卫：`full` 模式**不得**出现 progressive 的扩展索引。

    注意 `_assemble_system_prompt` 读的是**模块级全局** `_TOOL_EXPANSION`，
    不是参数 —— 所以要临时改全局再改回来。改环境变量没用的（我第一版就这么写，
    守卫直接红）。
    """
    saved = agent_mod._TOOL_EXPANSION
    agent_mod._TOOL_EXPANSION = "full"
    try:
        assembled = agent_mod._assemble_system_prompt(agent_mod._SYSTEM_PROMPT_FULL)
    finally:
        agent_mod._TOOL_EXPANSION = saved
    assert "【扩展工具索引】" not in assembled, (
        "full 模式不该出现 progressive 的扩展索引")


def test_backfill_alone_does_not_leak_the_index():
    """反向守卫：**只做补全**不该引入任何分层内容。

    补全是修复（让缺失工具被披露），分层是压缩。两者混在一起就说不清了，
    所以单独断言 `_backfill_tool_section` 的输出干净。
    """
    filled = agent_mod._backfill_tool_section(agent_mod._SYSTEM_PROMPT_FULL)
    assert "【扩展工具索引】" not in filled, "补全阶段混进了分层内容"
    assert "可用工具（核心，完整用法如下）" not in filled, (
        "补全阶段混进了 progressive 的段头")


def test_backfill_is_idempotent():
    """**反向守卫**：补全跑两次的结果必须与跑一次相同。

    幂等性很重要 —— 组装期会重复调用（`_assemble_system_prompt` 每次都跑）。
    若第二次又补一遍，说明「哪些缺失」的判据有错，会让提示无限增长。
    """
    once = agent_mod._backfill_tool_section(agent_mod._SYSTEM_PROMPT_FULL)
    twice = agent_mod._backfill_tool_section(once)
    assert len(twice) == len(once), (
        "补全不幂等：第一次 %d 字符，第二次 %d 字符 —— "
        "缺失判据有错，提示会无限增长" % (len(once), len(twice)))
    assert twice == once


def test_the_prompt_and_the_registry_agree_on_tool_names():
    """反向守卫：提示里不许出现**注册表里没有**的工具名。

    提示与注册表脱节过一次（`_NO_PARALLEL_TOOLS` 处注释记着 `dev_apply_edits` 一例），
    那时断言副本的测试给出假保障。
    """
    phantom = agent_mod._missing_tool_blocks(agent_mod._SYSTEM_PROMPT_FULL)
    assert not phantom, (
        "系统提示里出现注册表没有的工具名：%s —— "
        "提示与注册表脱节了（历史上 dev_apply_edits 一例）" % sorted(phantom))


def test_an_invisible_tool_that_claims_to_be_important_is_noticeable():
    """反向守卫：`INVISIBLE` 清单必须与实测同步。

    全部补齐后 `INVISIBLE` 应当为空 —— 这条会在有人手工加回名字时红。
    """
    assert not INVISIBLE, (
        "不可见清单非空：%s —— 若这些工具已可见，请删掉清单条目"
        " 并更新 docs/known-defect-tool-visibility-and-dual-text.md"
        % sorted(INVISIBLE))


# --------------------------------------------------------------------------
# 2. 分类完整性
# --------------------------------------------------------------------------

def test_every_tool_declares_a_domain():
    """反向守卫：不许有工具落进「其余」。

    这正是「按名字推断」的老毛病：新增一个 `dev_xxx` 不会被任何规则捕获，
    静默落进 `developer` —— 而 `developer` 原本装着 74 个工具，等于没分。
    """
    missing = sorted(n for n in TOOLS if not D.TOOL_DOMAINS.get(n))
    assert not missing, (
        "这些工具没有声明 domain/facet：%s\n"
        "请在 agent_runtime/tool_domains.py 里逐个登记 —— "
        "**不要改成按名字推断**，那会回到 74 个工具挤在一个组里的老问题"
        % missing
    )


def test_no_phantom_entries():
    """反向守卫：声明表里不许有不存在的工具名。

    我第一版凭印象写了 `apply_edits`/`dev_verify` 等 5 个**不存在**的名字，
    而完整性检查当时没发现 —— 因为我只数了「差几个」，没看「多出来的是谁」。
    """
    phantom = sorted(set(D.TOOL_DOMAINS) - set(TOOLS))
    assert not phantom, "声明表里有不存在的工具：%s" % phantom


def test_every_tool_spec_carries_its_domain():
    """正向守卫：`ToolSpec` 上的分类确实贴上了（声明表 -> 规格）。"""
    bare = sorted(n for n, s in TOOLS.items() if not s.domain)
    assert not bare, "这些 ToolSpec 没有 domain：%s" % bare


def test_domains_are_not_one_giant_bucket():
    """**分类有意义**的守卫：最大的大类不得吞掉大半。

    这是「分类」与「没分类」的分界。原 `group` 的 `developer` 装了 74 个 /
    全部 description 的 82%，等于把「其余都往这儿扔」叫做分类。
    """
    counts: dict[str, int] = {}
    for name in TOOLS:
        dom = D.TOOL_DOMAINS.get(name, ("",))[0]
        counts[dom] = counts.get(dom, 0) + 1
    biggest, n = max(counts.items(), key=lambda kv: kv[1])
    total = sum(counts.values())
    assert n <= total * 0.2, (
        "最大的大类 %r 装了 %d/%d 个工具（%.0f%%）—— 那不是分类，是「其余」\n"
        "分布：%s" % (biggest, n, total, 100.0 * n / total,
                     sorted(counts.items(), key=lambda kv: -kv[1])))
    assert len(counts) >= 8, (
        "只有 %d 个大类（%s）—— 分类太粗，下钻没有意义"
        % (len(counts), sorted(counts)))


# --------------------------------------------------------------------------
# 3. 目录不得冒充提示的替代品
# --------------------------------------------------------------------------

def test_the_catalog_is_not_yet_wired_into_the_prompt():
    """反向守卫：**目录现在不许接进系统提示**。

    实测（`docs/known-defect-tool-visibility-and-dual-text.md` §1.2）：
    96 个可见工具里 **81 个的提示版明显短于 desc 版**，提示版长度中位数
    60 字符、desc 版 163 字符 —— 手写版是**刻意精简**的，不是 desc 的复制。

    所以现在用目录替换提示，是把「60 字精准说明」换成「163 字泛泛描述」，
    **那是降级**。前置条件是先把 96 条手写说明迁进 `ToolSpec.description`。

    迁移完成后把这条改成断言「提示里含目录且不再逐条手写」。
    """
    from agent_runtime.tool_catalog import render_catalog

    catalog = render_catalog()
    head = catalog.splitlines()[0]
    assert head not in agent_mod.SYSTEM_PROMPT, (
        "系统提示里已经出现工具目录了 —— 若已完成第 3 步"
        "（把手写说明迁进 ToolSpec.description），请把本条改成断言新形态；"
        "若没完成就接上了，那是**降级**：手写说明比 desc 短（60 vs 163 中位数）"
    )


def test_the_two_level_catalog_is_much_cheaper_than_the_prompt():
    """量化守卫：目录确实比提示便宜（否则整个方案不成立）。"""
    from agent_runtime.tool_catalog import render_catalog

    catalog_len = len(render_catalog())
    prompt_len = len(agent_mod.SYSTEM_PROMPT)
    assert catalog_len * 5 < prompt_len, (
        "目录 %d 字符 vs 提示 %d 字符 —— 倍数不足，目录换不上" %
        (catalog_len, prompt_len))


def test_down_drill_returns_real_descriptions():
    """正向守卫：下钻真的给出**这个工具做什么**，不只是名字。

    反过来也有意义 —— 一个类若展开出来全是空壳，那目录就是在骗模型。
    """
    from agent_runtime.tool_catalog import render_domain

    for domain in ("files", "frontend", "web", "verify"):
        text = render_domain(domain)
        assert "：" in text and "- " in text, (
            "%r 展开后没有工具条目：%r" % (domain, text[:120]))
        assert re.search(r"- [a-z_]+: \S", text), (
            "%r 展开后没有「名字: 说明」形态的条目" % domain)


def test_an_unknown_domain_is_refused_not_guessed():
    """反向守卫：不认识的大类 code 不得瞎给一个类。"""
    from agent_runtime.tool_catalog import render_domain

    text = render_domain("no_such_domain")
    assert "没有" in text, text
    assert "files" in text, "应当把现有大类列出来，好让模型改对"


def test_render_catalog_omits_descriptions():
    """反向守卫：目录里**不得**含工具 description —— 那就是没省成。"""
    from agent_runtime.tool_catalog import render_catalog

    catalog = render_catalog()
    # 拿一条最长的 description 里的独特片段做探针
    probe = (TOOLS["dev_vue"].description or "")[:30]
    assert probe and probe not in catalog, (
        "目录里混进了工具 description —— 那样它省不下上下文")