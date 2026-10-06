"""被合并的任务（`deduped`）在界面上必须说清「并进了谁」。

## 这一族

`dedup_tasks`（`orchestrator.py`）按 `(role, 归一化 task)` 判重复，保留拓扑序
最靠前的副本，后续副本标 `dropped` 并把下游依赖重路由。`game_workflow.py`
给被合并的那个 id 记：

    {"status": "deduped", "conclusion": "", "kept": <并进了谁>, "optional": True}

**它表达的是「这个任务从未独立执行过」。** 而前端原先直接渲染原始 status
字符串，于是那一行显示英文 `deduped`，用户既看不懂、也不知道并进了谁 ——
容易以为计划里每条都被独立验证过。那正是这族本体在界面上的形状。

## 守卫的分工

- 标签：必须有人类可读的中文，不许裸露英文
- 说明：必须带 `kept`（并进了谁）
- 反向守卫：不得把 `deduped` 算进完成数；不得把说明行去掉
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VUE = ROOT / "frontend" / "src" / "workbench" / "components" / "AutonomousCockpit.vue"
ORCH = ROOT / "orchestrator.py"
WF = ROOT / "agent_runtime" / "game_workflow.py"


def vue() -> str:
    return VUE.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """剥掉注释与 HTML 注释——注释解释意图，不证明代码用它。"""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"//.*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return text


# --------------------------------------------------------------------------
# 后端契约：kept 必须真的存在
# --------------------------------------------------------------------------


def test_a_dropped_task_carries_where_it_went():
    """反向守卫：`kept` 字段被删掉，下面所有前端守卫都会假绿。"""
    src = WF.read_text(encoding="utf-8")
    assert '"kept": dedup_remap.get(task_id)' in src, (
        "被合并的任务不再带 kept —— 前端就无从说明「并进了谁」"
    )
    assert '"status": "deduped"' in src, "deduped 状态名变了"


def test_dedup_really_fires_on_identical_tasks():
    """**行为级**：造两个重复任务，确认真会被合并。

    只查源码形状不够——判据在 `orchestrator.dedup_tasks` 里，而那是从
    `game_workflow` 里 import 的（我第一次探针 import 就错了）。
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from orchestrator import dedup_tasks

    kept, dropped, remap = dedup_tasks(
        [{"id": "t1", "role": "coder", "task": "实现玩家移动控制器"},
         {"id": "t2", "role": "coder", "task": "实现玩家移动控制器"}],
        max_tasks=0,
    )
    assert [t["id"] for t in kept] == ["t1"]
    assert dropped == ["t2"]
    assert remap == {"t2": "t1"}, remap


# --------------------------------------------------------------------------
# 界面：标签与说明
# --------------------------------------------------------------------------


def test_the_status_label_is_human_readable():
    """`deduped` 必须有中文标签，不许裸露英文。"""
    code = _code_only(vue())
    assert re.search(r"TASK_STATUS_LABEL", code), "没有任务状态标签表"
    m = re.search(r"TASK_STATUS_LABEL[^=]*=\s*\{(.*?)\}", code, re.S)
    assert m, "找不到标签表内容"
    body = m.group(1)
    assert re.search(r"deduped\s*:\s*'已合并'", body), (
        f"deduped 没有中文标签：{body[:200]}"
    )


def test_every_backend_status_has_a_label():
    """反向守卫：只给 deduped 加标签，其他状态仍裸露英文。

    那会让情况更糟——同一个列表里一半中文一半英文。
    """
    code = _code_only(vue())
    m = re.search(r"TASK_STATUS_LABEL[^=]*=\s*\{(.*?)\}", code, re.S)
    assert m
    body = m.group(1)
    for status in ("ok", "failed", "blocked", "deduped", "pending"):
        assert re.search(rf"\b{status}\s*:", body), f"{status} 没有标签"
        value = re.search(rf"\b{status}\s*:\s*'([^']+)'", body)
        assert value, f"{status} 的标签不是字符串"
        assert not value.group(1).isascii() or value.group(1) in {"running"}, (
            f"{status} 的标签 {value.group(1)!r} 仍是英文"
        )


def test_the_row_uses_the_label_helper():
    """反向守卫：模板不回到直接渲染原始 status。"""
    code = _code_only(vue())
    i = code.find("acp-row")
    assert i > 0, "找不到任务行"
    row = code[i:i + 1400]
    assert "taskStatusLabel(task)" in row, (
        "任务行仍在直接渲染原始 status —— deduped 会裸露英文"
    )
    assert not re.search(r"taskResults\[String\(task\.id\)\]\?\.status\s*\|\|", row), (
        "任务行仍在用原始 status 兜底"
    )


def test_the_merged_row_says_where_it_went():
    """**核心守卫**：必须显示 `kept`（并进了谁）。"""
    code = _code_only(vue())
    i = code.find("acp-row")
    row = code[i:i + 1800]
    assert "deduped" in row, "任务行没有针对 deduped 的分支"
    assert "kept" in row, "deduped 那行没有显示 kept（并进了谁）"
    # 还要说清「未单独执行」—— 只说「重复」不足以让人知道它没跑
    assert re.search(r"未单独执行|没有单独执行|未独立执行", row), (
        "没有说明该任务未单独执行"
    )


def test_the_explanation_names_the_target():
    """反向守卫：说明里要真的插值出目标 id，不是写死的占位。"""
    code = _code_only(vue())
    # 不按固定窗口截取——上一次取了 600 字符，而 kept 的插值在 1000+ 字符处，
    # 于是这条守卫假红（而代码是对的）。改成在整份源码里找那个插值。
    assert re.search(r"\{\{[^}]*\bkept\b[^}]*\}\}", code), (
        "kept 没有被插值到界面上"
    )


def test_deduped_is_not_counted_as_done():
    """反向守卫：`deduped` **不得**计入完成数。

    它没被执行过，算作完成就是「把没做的记成做了」——正是这一族本体。

    注意：这里查的是 **`doneCount` 那条赋值语句本身**，不是在它附近找字符
    ——上次我在 420 字符的窗口里找 `deduped`，结果匹配到了它**上面那段注释**
    （注释里就写着「deduped 必须有人类可读的中文」），于是假红。
    """
    code = _code_only(vue())
    # 用「取到下一个空行为止」而不是括号层级正则——`)).length` 的括号
    # 数量我数错过一次，正则直接匹配不到（而代码是对的）。
    lines = code.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if "const doneCount" in ln), -1)
    assert start >= 0, "找不到 doneCount"
    body = ""
    for ln in lines[start:start + 6]:
        body += ln + "\n"
        if ln.rstrip().endswith(".length)") or ln.rstrip().endswith("length"):
            break
    assert "status === 'ok'" in body, body[:300]
    assert "deduped" not in body, (
        f"doneCount 的条件里出现了 deduped —— 合并掉的任务被算成完成了：{body[:300]}"
    )


def test_the_explanation_row_has_a_style():
    """新加的那行要有样式，且**模板里的 class 与样式里的规则必须配对**。

    上一版只查「`.acp-deduped { color: … }` 在样式里存在」——于是把模板里的
    `class="acp-deduped"` 改成别的名字，样式规则仍然留着，**这条照样绿**
    （实测 9 条全绿）。那正是「装饰残留在、而用它的地方没了」——比一开始
    就没有更坏：读者会以为它有样式。

    所以这条要查的是**配对**：模板里用了它，样式里就得有它的规则。
    """
    code = _code_only(vue())
    used = re.findall(r'class="([^"]*\bacp-deduped\b[^"]*)"', code)
    assert used, "模板里没有用 acp-deduped 这个 class"
    m = re.search(r"\.acp-deduped\s*\{([^}]*)\}", code)
    assert m and m.group(1).strip(), (
        "模板用了 acp-deduped，样式里却没有对应规则"
    )
    assert "color" in m.group(1), ".acp-deduped 没有设颜色"
