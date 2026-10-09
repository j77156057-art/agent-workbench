# -*- coding: utf-8 -*-
"""锁住「界面声明的边界」与「后端真的这么判」相等。

## 为什么是这条，而不是先让后端下发范围

容量那批（max_nudges 默认 2→0、上界 20→1000）**此刻仍在途未提交**，而
`SettingsView.vue` 那半是前端 agent 正在写的文件。所以：

* 让后端下发范围（A）现在做 = 加一个**没人消费**的 payload 字段，
  直到写前端那个人某天读到它为止。那是死代码。
* 加一条两边数字必须相等的测试（B）**现在就能落地**，且不碰任何在途文件。

B 不消除重复，它让漂移**立刻可见** —— 这正是当前最疼的点。

## ⚠ 这条测试不修的那个真 bug

后端 `normalize_settings` 越界时回落到 `DEFAULTS`，而 WIP 把
`max_nudges` / `max_final_continuations` 的默认改成了 **0 = 不限**。
于是用户填 5000 会被静默写成不限 —— 行为与意图相反。

A、B 两条都不修它（A 只让前端显示正确范围，`type=number` 的 max 并不阻止
输入）。真正该做的是**越界夹到边界**（5000→1000）而不是回落默认。
已立 TODO，见 `docs/next-steps.md`。

## 与 tests/test_ui_guidance_reachability.py 同形状

那条测的是「界面上教的每个数，后端真的这么判」；这条是它的特例：
只盯容量边界。
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _backend_bounds() -> dict[str, int]:
    """从 agent_runtime/capacity.py 读出「字段 -> 上界」。

    读的是 `_clamp(<字段>, <CONST>)` 调用点，再把 CONST 解析成数字。

    ⚠ 为什么不读 `if x < 0 or x > N:`：后来把越界逻辑收进 `_clamp()` 后，
    那串判断从源码里消失了，解析器返回空、护栏报「判据恒真」。那次失败是
    有用的：**边界必须有一个可被外部读到的具名处**，而不是藏在调用点里。
    这也是把数字提成 `MAX_*_CAP` 常量的原因。

    ⚠ 为什么不手写「常量名 -> 字段」别名表：`child_max_steps` 与
    `main_max_steps` 共用 `MAX_STEPS_CAP`，`max_final_continuations` 与
    `max_nudges` 共用 `MAX_NUDGES_CAP` —— 手写别名会漏，而漏了不会报错，
    只会让那条字段 silently 不参与比对。直接解析调用点才对得上。
    """
    src = (ROOT / "agent_runtime" / "capacity.py").read_text(encoding="utf-8")
    caps = {m.group(1): int(m.group(2).replace("_", ""))
            for m in re.finditer(r"^(MAX_\w+)\s*=\s*([\d_]+)\s*$", src, re.M)}
    assert caps, "没解析到任何 MAX_*_CAP 常量 —— 判据恒真"

    out: dict[str, int] = {}
    for m in re.finditer(r"_clamp\(\s*[\"'](\w+)[\"']\s*,\s*\w+\s*,\s*(MAX_\w+)\s*\)", src):
        field, const = m.group(1), m.group(2)
        if const in caps:
            out[field] = caps[const]
    return out


def _ui_max_attrs() -> dict[str, int]:
    """从 SettingsView.vue 里读出每个容量输入框的 max。

    真实形状是**静态属性**（不是 Vue 绑定）：

        <input v-model="maxNudges" type="number" min="0" max="1000" ... />

    第一版写成 `:max="N"`（带冒号的绑定），结果**一个都没匹配到** —— 幸好有
    「解析为空即判失败」的护栏，否则会变成一条恒真绿灯，看着像「两边一致」。
    """
    src = (ROOT / "frontend" / "src" / "workbench" / "components"
           / "SettingsView.vue").read_text(encoding="utf-8")
    out: dict[str, int] = {}
    for m in re.finditer(r"v-model=\"(\w+)\"[^>]*?\bmax=\"([\d_]+)\"", src):
        out[m.group(1)] = int(m.group(2).replace("_", ""))
    return out


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
    """六个字段，两边上界必须逐个相等。"""
    be = _backend_bounds()
    ui = _ui_max_attrs()
    assert be, "没从 normalize_settings 解析出任何边界 —— 解析器失效，判据恒真"
    assert ui, "没从 SettingsView 解析出任何 :max —— 解析器失效，判据恒真"

    drift = []
    for ui_key, be_key in UI_TO_BACKEND.items():
        if ui_key not in ui:
            drift.append(f"{ui_key}：界面上没有 :max")
            continue
        if be_key not in be:
            drift.append(f"{be_key}：后端没有边界判断")
            continue
        if ui[ui_key] != be[be_key]:
            drift.append(f"{be_key}：界面 {ui[ui_key]} != 后端 {be[be_key]}")
    assert not drift, "容量边界两边不一致：\n  " + "\n  ".join(drift)


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
