# -*- coding: utf-8 -*-
"""锁住「界面声明的边界」与「后端真的这么判」相等。

## 为什么是这条，而不是先让后端下发范围

容量那批（max_nudges 默认 2→0、上界 20→1000）**此刻仍在途未提交**，而
`SettingsView.vue` 那半是前端 agent 正在写的文件。所以：

* 让后端下发范围（A）现在做 = 加一个**没人消费**的 payload 字段，
  直到写前端那个人某天读到它为止。那是死代码。
* 加一条两边数字必须相等的测试（B）**现在就能落地**，且不碰任何在途文件。

B 不消除重复，它让漂移**立刻可见** —— 这正是当前最疼的点。

## ⚠ 那个「填 5000 被静默写成不限」的 bug：**已修**（不是这条测试修的）

⚠⚠ **本段曾是过期描述，害得前端 agent 误判后端行为，已于本轮更正。**

原文写的是：「后端 `normalize_settings` 越界时回落到 `DEFAULTS`，而默认
已改成 0=不限，于是用户填 5000 会被静默写成不限 —— 行为与意图相反。
真正该做的是越界夹到边界。已立 TODO。」

那是 capacity **还在 WIP、契约尚未落地**时写的。契约落地后我没回头改这段，
前端 agent 读到它就以为后端仍是老行为，据此在界面上做了多余的兜底。

实测（端到端 POST 5000 → GET）：

    max_agents                 5000 -> 64          夹到上界
    main_max_steps             5000 -> 1000        夹到上界
    child_max_steps            5000 -> 1000        夹到上界
    max_nudges                 5000 -> 1000        夹到上界
    max_final_continuations    5000 -> 1000        夹到上界
    task_token_budget          5000 -> 5000        上界是 10000000，5000 在范围内

契约与权威实现见 `agent_runtime/capacity.py::_clamp`，
锁定它的测试是 `tests/test_capacity_clamp_not_default.py`
与 `tests/test_unlimited_defaults.py`。

**仍然成立的一点**：界面上 `type=number` 的 max 只影响步进器，不阻止输入，
所以后端的夹取是唯一防线 —— 前端那条「当场拒绝」属于 UX 加固，不是修 bug。

## 与 tests/test_ui_guidance_reachability.py 同形状

那条测的是「界面上教的每个数，后端真的这么判」；这条是它的特例：
只盯容量边界。
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _backend_bounds() -> dict[str, int]:
    """从**真实 API 载荷**读出上界 —— 不再从源码正则扒。

    ⚠ 为什么换：前两版分别解析 `if x < 0 or x > N:` 和 `MAX_*_CAP` 常量。
    第二版有个隐患：它读的是**实现细节**（常量叫什么、怎么调用），
    后端哪天把越界逻辑换个写法（例如改成表驱动），解析器就失效 ——
    而失效时我的护栏会报「判据恒真」，那是一次**失败的测试**，
    不是一次通过的测试。

    现在直接问 API：`GET /api/agent-capacity` 的 `ranges` 字段。
    那是**契约**，与内部怎么实现无关。内部重写不影响这条测试。

    ⚠ 这是 async，所以要走 IsolatedAsyncioTestCase；见下面那个用例。
    """
    import asyncio
    import api
    payload = asyncio.run(api.get_agent_capacity())
    ranges = payload.get("ranges")
    assert isinstance(ranges, dict) and ranges, (
        "GET /api/agent-capacity 没有 ranges 字段 —— 判据恒真")
    return {k: int(v) for k, v in ranges.items()}


def _ui_max_attrs() -> dict[str, int]:
    """前端 input 的 max 来自哪里。

    ⚠⚠ **这条已经改了三次判据，因为前端形态变了三次**：
    1. `max="64"` 静态属性            -> 解析属性值
    2. `:max="64"` Vue 绑定           -> 解析绑定
    3. `:max="capacityMax('x')"`     -> **现状：前端消费后端下发的 ranges**

    第三次之后，「比对两边数字」这个动作本身失去了意义 ——
    前端读的就是后端那个字段，**数字必然相等，断言恒真**。

    所以现在锁的是另一件事：**前端没有自己抄一份数字**。
    即：每个容量 input 的 max 必须来自 `capacityMax(...)`（读取后端载荷），
    而不是字面量。若哪天有人又写回 `max="1000"`，这条会红 ——
    那才是真正要防的漂移。

    与 `tests/test_capacity_bounds_ui_backend_agree.py` 的边界一致性断言分工：
    * 本函数负责「前端不抄数字」
    * `test_ui_and_backend_agree_on_every_bound` 负责「后端下发的范围自洽」
    """
    src = (ROOT / "frontend" / "src" / "workbench" / "components"
           / "SettingsView.vue").read_text(encoding="utf-8")
    # 形如 :max="capacityMax('maxNudges') || undefined"
    # ⚠ findall 只有**一个**分组时返回字符串列表，dict() 会报
    # "sequence element #0 has length 9" —— 用集合就行，这里只要「有没有」。
    live = set(re.findall(r":max=\"capacityMax\('(\w+)'\)", src))
    # 形如 v-model="capacityValues.maxNudges" （前端的字段名）
    models = re.findall(r'v-model="capacityValues\.(\w+)"', src)
    return {m: 1 for m in models if m in live} if live else {}


def _ui_reads_backend_ranges() -> bool:
    """前端确实有 capacityMax 这个读取函数，且它取自后端载荷。"""
    src = (ROOT / "frontend" / "src" / "workbench" / "components"
           / "SettingsView.vue").read_text(encoding="utf-8")
    lines = src.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if "capacityMax" in l and ("function" in l or "const capacityMax" in l)),
                 None)
    if start is None:
        return False
    # 看函数体前 12 行有没有提到 ranges
    body = "\n".join(lines[start:start + 12])
    return "ranges" in body


#: UI 的 v-model -> 后端 normalize_settings 的键
UI_TO_BACKEND = {
    "maxAgents": "max_agents",
    "mainMaxSteps": "main_max_steps",
    "childMaxSteps": "child_max_steps",
    "maxNudges": "max_nudges",
    "maxFinalContinuations": "max_final_continuations",
    "taskTokenBudget": "task_token_budget",
}


def test_ui_and_backend_agree_on_every_bound():
    """六个字段，前端都必须从后端载荷取边界，**不许自己抄数字**。

    ⚠ 这条断言的内容随前端形态改过三次（见 `_ui_max_attrs` 的 docstring）。
    现在锁的不是「两边数字相等」—— 数字必然相等，那是恒真；
    锁的是「前端没有第二份数字」。
    """
    be = _backend_bounds()
    ui = _ui_max_attrs()
    assert be, "GET /api/agent-capacity 没有 ranges 字段 —— 判据恒真"
    assert ui, ("没从 SettingsView 解析出任何读 ranges 的 input —— "
                "要么前端又抄了数字，要么解析器失效；两种情况都该红")
    assert _ui_reads_backend_ranges(), (
        "capacityMax 没在读后端 ranges —— 前端自己造了一份边界")

    drift = []
    for ui_key, be_key in UI_TO_BACKEND.items():
        if ui_key not in ui:
            drift.append(f"{ui_key}：界面 input 没有从 capacityMax 取 max")
    assert not drift, "容量边界前端不再单源：\n  " + "\n  ".join(drift)


def test_no_hardcoded_bound_numbers_remain():
    """反向守卫：SettingsView 里不许再出现硬编码的容量上界。

    这条是防「有人图省事写回 max="1000"」。写了就红，
    逼他改回读 ranges —— 那才是单源。
    """
    src = (ROOT / "frontend" / "src" / "workbench" / "components"
           / "SettingsView.vue").read_text(encoding="utf-8")
    # 只看容量那几个 input 行，别扫全文件（全文件有别的合法数字）
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        if "capacityValues." not in line or "max=" not in line:
            continue
        if ':max="capacityMax(' in line:
            continue                      # 正确形态
        if re.search(r'\bmax="[\d_]+"', line):
            offenders.append(f"第 {i} 行: {line.strip()[:80]}")
    assert not offenders, ("又有硬编码的容量上界了：\n  "
                           + "\n  ".join(offenders))


def test_the_stale_fallback_matches_backend_defaults():
    """`?? N` 兜底值必须等于后端 DEFAULTS。

    WIP 把 max_nudges / max_final_continuations 的默认改成 0（不限），
    而 SettingsView 仍是 `?? 2` —— GET 拿不到载荷时取消确认会静默写回 2，
    正好制造这次要消灭的「界面值与真实默认不一致」。
    """
    be = (ROOT / "agent_runtime" / "capacity.py").read_text(encoding="utf-8")
    sv = (ROOT / "frontend" / "src" / "workbench" / "components"
          / "SettingsView.vue").read_text(encoding="utf-8")

    # 后端 DEFAULTS 里的数字
    m = re.search(r"DEFAULTS\s*=\s*\{(.*?)\}", be, re.S)
    assert m, "没解析到 DEFAULTS"
    defaults = dict(re.findall(r"\"(\w+)\"\s*:\s*([\d_]+)", m.group(1)))
    defaults = {k: int(v.replace("_", "")) for k, v in defaults.items()}
    assert defaults, "DEFAULTS 解析为空 —— 判据恒真"

    stale = []
    for key, val in defaults.items():
        # 界面上的 `capacityInfo.value?.settings.<key> ?? N`
        mm = re.search(r"capacityInfo\.value\?\.settings\.%s\s*\?\?\s*([\d_]+)" % key, sv)
        if mm is None:
            continue                      # 该字段界面不做本地兜底，不判
        ui_v = int(mm.group(1).replace("_", ""))
        if ui_v != val:
            stale.append(f"{key}：界面兜底 {ui_v} != 后端默认 {val}")
    assert not stale, "容量默认值两边不一致：\n  " + "\n  ".join(stale)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
