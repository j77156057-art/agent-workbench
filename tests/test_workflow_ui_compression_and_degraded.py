"""工作台档的前端漏点：`degraded` 计数与依赖上下文压缩。

## 同一个族的两个漏点

**漏点一：计数没排除 `degraded`。** 后端判据已经是 `status == "ok" and
not degraded`（`game_workflow.counts_as_ok`，提交 `f7bcf0e`），但前端的
「n/m 个任务完成」还只按 `status` 数 —— 于是标题上会出现「3/3 个任务完成」，
而其中一个的结论其实是 harness 写的诊断。**判据只改后端不改前端，等于
没改完。**

**漏点二：压缩信号带了没人读。** 后端真在调 `compress_context_async`，
`subagent_complete` / `wave_start` 两个事件都带了 `context_compression`
（`original_chars` / `compressed_chars` / `truncated` / `items`，实测确认），
但 `frontend/src` 里**一个字符都没有**。要紧的不是「压缩率高」，而是
`truncated` —— 那表示确实丢了东西，子代理是在**部分依据**上完成的，
而用户看到的时间线只写「子代理完成」。

## 守卫怎么钉

Vue 单文件里没有可 import 的纯函数，所以这层用 `ast`/文本层守卫 + 真跑
`compress_context` 验证字段形状。`ast` 取不到 `.vue`（不是合法 JS），所以
用正则+行定位，并**显式钉住字段名**而不是「包含某段文本」。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COCKPIT = ROOT / "frontend" / "src" / "workbench" / "components" / "AutonomousCockpit.vue"
API = ROOT / "frontend" / "src" / "workbench" / "api" / "agent.ts"


def cockpit() -> str:
    return COCKPIT.read_text(encoding="utf-8")


def api() -> str:
    return API.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """剥掉注释，只留真代码。

    **不要拿注释当证据。** 变异把 `truncated` 改成 `truncated_x` 之后，
    文档注释里那句「而是 `truncated`——」还在，`\\btruncated\\b` 照样匹配，
    守卫就假绿了（实测踩到过一次）。注释解释意图，不证明代码用它。
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", " ", text)
    text = re.sub(r"(?<![:'\"])//[^\n]*$", " ", text, flags=re.M)
    return text


# --------------------------------------------------------------------------
# 漏点一：计数必须排除 degraded
# --------------------------------------------------------------------------


def _done_count_block(text: str, span: int = 500) -> str:
    """截出 `doneCount` 那一段。

    不要用 `computed\\([^)]*` 这种正则：`Object.values(...)` 和 `.filter(...)`
    里的右括号会提前收尾，守卫就会**假红**——它根本匹配不到 `!r.degraded`
    就停了。宁可按行定位。
    """
    i = text.find("const doneCount")
    assert i > 0, "doneCount 不见了"
    return text[i:i + span]


def test_done_count_excludes_degraded():
    """反向守卫：把 `!r.degraded` 去掉，这条就红。

    这条最容易被当成"多余的一层保险"删掉——而它不是：后端判据管的是
    报告和 API，这里管的是**用户第一眼看到的那个数字**。
    """
    assert re.search(r"!\s*\w+\.degraded", _done_count_block(cockpit())), (
        "doneCount 没有排除 degraded——标题上的『n/m 完成』会把降级任务算进去"
    )


def test_done_count_still_counts_real_ok():
    """反向守卫：防「一律不算」——把条件改成恒假或干脆返回 0 都会让这条红。"""
    snippet = _done_count_block(cockpit())
    assert re.search(r"\w+\.status\s*===\s*'ok'", snippet), (
        "doneCount 应该在排除 degraded 的前提下仍数真 ok"
    )
    assert "degraded" in snippet


# --------------------------------------------------------------------------
# 漏点二：压缩信号必须到达界面
# --------------------------------------------------------------------------


def test_api_declares_the_compression_contract():
    """契约要显式声明。

    `WorkflowEvent` 有 `[key: string]: unknown` 兜底，所以不写这个字段
    TypeScript 也不报错——但那样它就是「碰巧能用」，不是契约。后端改
    字段名时前端不会被发现。
    """
    text = api()
    assert "interface ContextCompressionMeta" in text
    for field in ("original_chars", "compressed_chars", "truncated", "items"):
        assert re.search(rf"\b{field}\?", text), f"ContextCompressionMeta 缺 {field}"
    assert "context_compression?: ContextCompressionMeta" in text


@pytest.mark.parametrize("field", ["original_chars", "compressed_chars", "truncated", "items"])
def test_the_real_metadata_field_is_read(field):
    """字段名必须**整个匹配**地出现在**真代码**里，不能靠注释或子串蒙对。

    两条踩过的坑：
    - `truncated_x` 里 `re.search("truncated")` 会命中（子串）——所以用 `\\b`。
    - 注释里的 ``truncated`` 也会命中——变异改掉代码后注释还在，守卫就假绿了。
      所以这里匹配的是剥掉注释的源码。
    """
    code = _code_only(cockpit())
    assert re.search(rf"\b{field}\b", code), f"前端没在代码里读压缩字段 {field}"


def test_truncated_is_marked_on_the_timeline_row():
    """`truncated` 必须在**标题**上标出来。

    只放进 detail 不够：时间线默认只显示折叠标题，用户要点开才看到——
    而「子代理是在被截断的依据上完成的」属于必须一眼看到的信息。
    """
    code = _code_only(cockpit())
    i = code.find("function timelineLabelOf")
    assert i > 0, "timelineLabelOf 不见了"
    snippet = code[i:i + 500]
    assert "context_compression" in snippet, "timelineLabelOf 没有看 context_compression"
    assert re.search(r"\btruncated\b", snippet), "timelineLabelOf 没有看 truncated"
    assert "截断" in snippet, "标题里必须出现「截断」二字，而不是悄悄不显示"


def test_detail_shows_the_numbers_and_says_it_is_partial():
    """detail 要给出原字数→压缩后字数，并明说「基于部分依据」。

    反向守卫：把这句话软化成「已优化」，或者只显示压缩率——
    那等于把「丢了东西」说成「省了空间」，是更坏的一种错。
    """
    code = _code_only(cockpit())
    i = code.find("function compressionNote")
    assert i > 0, "compressionNote 不见了"
    snippet = code[i:i + 700]
    assert "original_chars" in snippet
    assert "compressed_chars" in snippet
    assert "基于部分依据" in snippet, "截断时必须明说结论基于部分依据"
    assert "未截断" in snippet, "没截断时也要说清，否则用户分不清两种情况"


def test_compression_note_is_actually_wired_into_the_detail():
    """反向守卫：把函数定义了但忘了调用，这条就红。

    这是最阴的失败模式——`compressionNote` 写得再对，不被调用就是死代码。
    """
    text = cockpit()
    assert "compressionNote(event)" in text
    detail_i = text.find("function timelineDetail")
    assert detail_i > 0
    snippet = text[detail_i:text.find("function", detail_i + 10)]
    assert "compressionNote" in snippet, "timelineDetail 没有调用 compressionNote"


def test_that_return_line_is_still_valid_javascript():
    """反向守卫：光检查「有没有 `compressionNote(event)`」**不够**。

    实测踩到过：那行被写成了
        return `${task}${String(detail || '已记录')ce(/\\s+/g, ' ').slice(0, 320)}${compressionNote(event)}`
    ——调用还在、守卫 13 条全绿，而那行已经不是合法 JS（`.replace` 变成了 `ce(`）。
    任何只查「关键调用在不在」的守卫都会漏掉这类截断。

    所以这里钉住**整行的完整形状**：`.replace(` 必须在，`.slice(` 必须在，
    拼接链不能少环节。
    """
    code = _code_only(cockpit())
    i = code.find("function timelineDetail")
    assert i > 0
    # 只看这个函数的函数体，不越界到别的函数
    j = code.find("function", i + 10)
    body = code[i:j if j > 0 else i + 800]
    ret = [ln for ln in body.splitlines() if "return" in ln]
    assert ret, "timelineDetail 里没有 return 行"
    line = ret[-1]
    assert ".replace(" in line, f"拼接链缺 .replace(：{line.strip()}"
    assert ".slice(" in line, f"拼接链缺 .slice(：{line.strip()}"
    assert "已记录')" in line, f"读取 detail 的那一步被截断了：{line.strip()}"
    # 不能再出现被截断的残留（'已记录') 后面直接跟非 . 的字符）
    assert not re.search(r"已记录'\)[A-Za-z]{2}\(", line), (
        f"拼接链被截断了：{line.strip()}"
    )


def test_compression_note_is_safe_on_events_without_it():
    """反向守卫：没有压缩信息的普通事件不能因此报错或显示 "undefined"。

    绝大多数事件（工具调用、计划、审批）都不带 `context_compression`。
    正确写法是**提前返回空串**；这里就钉这个意图，不去逼它在下游写兜底
    （`meta` 若为 undefined，下游兜底也是错的——那说明 meta 检查漏了）。
    """
    i = cockpit().find("function compressionNote")
    assert i > 0, "compressionNote 不见了"
    snippet = cockpit()[i:i + 500]
    assert re.search(r"if\s*\(\s*!meta", snippet), (
        "compressionNote 必须在 meta 缺失时直接返回空串"
    )
    # 提前返回的那个分支必须返回空串，不能返回 undefined / 'null'
    guard = snippet[snippet.find("if (!meta"):]
    early = guard[:guard.find("}")] if "}" in guard else guard
    assert re.search(r"return\s+''", early), "meta 缺失时必须返回空串，不能是 undefined"


# --------------------------------------------------------------------------
# 字段形状与后端实测一致
# --------------------------------------------------------------------------


def test_compression_meta_matches_what_the_backend_actually_returns():
    """真跑一次压缩，拿真实 metadata 的键，与前端接口声明比对。

    这条防的是**两边各写各的**：后端改字段名而前端照旧，前端 TS 不会报错
    （有 `[key: string]: unknown` 兜底），界面就悄悄显示 "undefined"。
    """
    sys.path.insert(0, str(ROOT))
    from agent_runtime.context_router import compress_context

    _, meta = compress_context({"": "很长的上下文。" * 400}, 200)
    assert isinstance(meta, dict), f"metadata 不是 dict：{type(meta)}"
    api_text = api()
    for key in meta:
        assert re.search(rf"\b{key}\??:", api_text), (
            f"后端真实返回了 {key!r}，但前端接口没声明它——界面会显示 undefined"
        )


def test_truncated_is_reachable_at_all():
    """`truncated: true` 必须真的会出现，否则那条标注是死分支。

    如果压缩永远不截断，前端那句「基于部分依据」就永远不会显示——
    那说明判断写错了地方。
    """
    sys.path.insert(0, str(ROOT))
    from agent_runtime.context_router import compress_context

    _, meta = compress_context({"": "很长的上下文。" * 400}, 200)
    assert meta.get("truncated") is True
    assert meta["original_chars"] > meta["compressed_chars"]
