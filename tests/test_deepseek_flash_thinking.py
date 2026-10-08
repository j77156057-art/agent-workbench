# -*- coding: utf-8 -*-
"""deepseek-flash 的思考能力画像与**真实发出的参数形状**。

## 现象与定性

用户在**开发台**打开「深度思考」，界面上不出现思考内容。

逐段核实后链路全是通的（`reasonIsOpen` 流式自动展开、cockpit 复用同一个
`ChatDock`、后端两条 reasoning 通道都在），唯一断点是**能力画像**：

    deepseek-flash  ->  thinking="none"

于是 `thinkingSupported=false` -> `thinkingOpt=null` -> `thinking_mode`
**字段根本不发**（`chat.ts:30`）-> 后端按关处理 -> 没有 reasoning 事件 ->
面板不出现。**面板不出现是对的，错的是画像。**

## 画像为什么错

`config.py` 的 `_TOGGLE_THINK_RULES` **只列了 qwen3 家族**，DeepSeek 完全不在
名单里；`_NATIVE_THINK_HINTS` 又只命中 `reasoner` / `deepseek-r1`。

而 DeepSeek 官方文档的 Thinking Mode 示例**直接用 `deepseek-flash`**：

    model="deepseek-flash",
    reasoning_effort="high",
    extra_body={"thinking": {"type": "enabled"}}
    # 流式返回 chunk.choices.delta.reasoning_content

（api-docs.deepseek.com/guides/thinking_mode；Change Log 见 api-docs.deepseek.com/updates）

## ⚠ 这个 bug 有两个形状，只改一个会更坏

1. 画像：把 provider 加进 `_TOGGLE_THINK_RULES`
2. **参数形状**：DeepSeek 要 `{"thinking": {"type": ...}}`，
   而 `llm.py` 统一发 qwen 的 `{"chat_template_kwargs": {"enable_thinking": ...}}`

只做 1 不做 2，会得到一个**能点但没效果**的开关 —— 比原来「诚实地报不支持」
更难排查。

## ⚠⚠ 本文件**不许复刻** llm.py 的逻辑

第一版的形状断言是自己 copy 了一遍 `llm.py` 的分支，所以把生产代码回退之后
它**照样 9 passed** —— 那次变异实测是**无效的**，等于给了一份额定心的摆设。

现在改为**真的调用 `LLMClient.chat`**，用一个假 client 捕获 `create()` 收到的
kwargs。断言的对象是**生产代码真正发出去的东西**。
"""

import json

import pytest

import llm as llm_mod
from config import model_thinking_mode


# ---------------------------------------------------------------------------
# 画像
# ---------------------------------------------------------------------------

class TestDeepSeekFlashThinkingProfile:
    @pytest.mark.parametrize("model", [
        "deepseek-flash",                  # 当前模型 id（V4.1-Flash）
        "deepseek-v4-flash",               # 旧名，已路由到同一模型
        "deepseek-v4-flash-vision-exp",    # 旧名，已退役并路由
    ])
    def test_flash_family_is_toggleable(self, model):
        assert model_thinking_mode("deepseek", model) == "toggle", (
            "%s 被判成不支持思考 —— 而官方文档的 Thinking Mode 示例就用它" % model)

    def test_reasoner_is_still_native(self):
        """⚠ 不许改坏：`reasoner` 是天生推理，语义与「可开关」不同。"""
        assert model_thinking_mode("deepseek", "deepseek-reasoner") == "native"
        assert model_thinking_mode("deepseek", "deepseek-chat-reasoner") == "native"

    def test_non_thinking_deepseek_models_stay_none(self):
        """别把整个 deepseek 都标成可开关 —— `deepseek-chat` 不在此列。"""
        assert model_thinking_mode("deepseek", "deepseek-chat") == "none"

    def test_qwen_family_is_unaffected(self):
        """回归：qwen3 家族原来就是 toggle。"""
        for prov in ("qwen", "ollama", "llamacpp", "custom", "siliconflow"):
            assert model_thinking_mode(prov, "qwen3-35b-a3b") == "toggle", prov


# ---------------------------------------------------------------------------
# 真实发出的参数（**调生产代码**，不复刻）
# ---------------------------------------------------------------------------

class _CapturingCompletions:
    """捕获 `create()` 收到的 kwargs，并回一个最小可迭代的流。"""

    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        # 非流式分支返回一个带 choices 的最小对象；本测试只关心 kwargs。
        class _Msg:
            content = "ok"
            tool_calls = None
        class _Choice:
            message = _Msg()
            finish_reason = "stop"
        class _Resp:
            choices = [_Choice()]
            usage = None
        return _Resp()


def _real_kwargs(provider, model, thinking_on):
    """真的构造 LLMClient 并调 chat()，返回 create() 收到的 kwargs。"""
    caps = _CapturingCompletions()
    c = llm_mod.LLMClient(provider=provider, model=model)
    c.client = type("C", (), {"chat": type("Chat", (), {
        "completions": caps})()})()
    c.chat([{"role": "user", "content": "hi"}], stream=False,
           enable_thinking=thinking_on)
    return caps.kwargs


class TestThinkingParameterShapeReallySent:
    """断言的对象是**生产代码真正发出去的 kwargs**。"""

    def test_deepseek_gets_its_own_parameter_shape(self):
        """DeepSeek 的开关是 `thinking.type`，**不是** `chat_template_kwargs`。

        发错形状的后果：上游静默忽略 -> 「开关能点了但思考没开」。
        """
        on = _real_kwargs("deepseek", "deepseek-flash", True)
        assert on.get("extra_body") == {"thinking": {"type": "enabled"}}, (
            "deepseek-flash 开启时发出去的参数不对：%r" % on.get("extra_body"))
        off = _real_kwargs("deepseek", "deepseek-flash", False)
        assert off.get("extra_body") == {"thinking": {"type": "disabled"}}, (
            "deepseek-flash 关闭时发出去的参数不对：%r" % off.get("extra_body"))

    def test_qwen_keeps_its_parameter_shape(self):
        """回归：qwen3 仍收 chat_template_kwargs。"""
        got = _real_kwargs("qwen", "qwen3-35b-a3b", True)
        assert got.get("extra_body") == {"chat_template_kwargs": {"enable_thinking": True}}, (
            "qwen3 的参数形状被 deepseek 分支带走了：%r" % got.get("extra_body"))

    def test_non_thinking_model_sends_no_parameter_at_all(self):
        """画像=none 时**一个参数都不发** —— 带上会被上游拒。"""
        got = _real_kwargs("deepseek", "deepseek-chat", True)
        assert "extra_body" not in got, (
            "明确不支持思考的模型也发了参数：%r" % got.get("extra_body"))

    def test_json_serialisable(self):
        """`extra_body` 最终要进 HTTP body —— 不可序列化等于发不出去。"""
        for prov, model in (("deepseek", "deepseek-flash"), ("qwen", "qwen3-35b-a3b")):
            got = _real_kwargs(prov, model, True)
            json.dumps(got.get("extra_body"))
            assert json.loads(json.dumps(got["extra_body"])) == got["extra_body"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
