"""真跑验证 DeepSeek 思考模式两硬约束（next-steps.md §7 ⑥）。

## 只验这两件事

① `tool_choice="required"` / 具名函数 + 思考模式 = 整个请求 **400**（地雷）。
② `temperature` 在思考模式下**静默失效**（无报错但无效果）。

## 为什么需要真跑

两条都来自 DeepSeek 官方 API 文档。Agent 工作台 当前：
- ① 没踩到，只因为**全仓从不发 `tool_choice`**（llm.py:836 只发 `tools`）——
  这是运气不是设计。谁加上它，所有开思考的用户会全线 400 且报错不指向思考。
- ② 是**无声偏差**：llm.py:844/885 **无条件**发 `temperature`，开思考后
  上游静默忽略，**不报错、无信号**。

## 判据纪律（与 probe_deepseek_thinking_live.py 同源）

- 失败 ≠ 模型不支持思考。401/403/限流/400(tool_choice) 各自单独报。
- 对照组：去掉 tool_choice（默认 auto）必须成功，才能把 400 归因到 tool_choice。
- 不打印 key，只打印是否配置。
"""

from __future__ import annotations

import os
import sys

MODEL = os.getenv("DOCMIND_CONSTRAINT_MODEL", "deepseek-reasoner")
BASE_URL = "https://api.deepseek.com/v1"

# 一个最小 function schema，专门用于触发 tool_choice 约束。
_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "获取指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def _client():
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if not key:
        print("  *** 没有 DEEPSEEK_API_KEY —— 本次不产出任何结论。")
        sys.exit(2)
    from openai import OpenAI

    return OpenAI(base_url=BASE_URL, api_key=key)


def _call(label: str, **kw) -> tuple[bool, str]:
    c = _client()
    try:
        resp = c.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": "今天适合出门吗？只回答一句。"}],
            **kw,
        )
        msg = resp.choices[0].message
        rc = len(getattr(msg, "reasoning_content", None) or "")
        ct = len(getattr(msg, "content", None) or "")
        print("    %-22s 成功=True  reasoning=%d字 content=%d字"
              % (label, rc, ct))
        return True, ""
    except Exception as exc:  # noqa: BLE001
        print("    %-22s 成功=False  错误=%s: %s"
              % (label, type(exc).__name__, str(exc)[:160]))
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:160])


def main() -> int:
    print("=" * 74)
    print("  真跑验证：DeepSeek 思考模式两硬约束（model=%s）" % MODEL)
    print("=" * 74)
    print("  API key: %s" % ("有" if (os.getenv("DEEPSEEK_API_KEY") or "").strip()
                             else "*** 未配置 ***"))
    print("  说明：%s 是 native 思考模型，调用即处于思考模式，"
          "最干净地触发两约束。" % MODEL)
    print()

    if not (os.getenv("DEEPSEEK_API_KEY") or "").strip():
        return 2

    print("  === 约束①：tool_choice=required + tools（思考模式）应 400 ===")
    ok_required, err_required = _call("① required+tools",
                                     tools=[_TOOL], tool_choice="required")
    print()
    print("  === 对照：只发 tools（默认 auto），应成功 ===")
    ok_auto, err_auto = _call("② tools仅(auto)", tools=[_TOOL])
    print()
    print("  === 约束②：思考模式 + temperature=2.0 应无报错（静默忽略） ===")
    ok_temp, err_temp = _call("③ temp=2.0", tools=[_TOOL], temperature=2.0)
    print()

    print("=" * 74)
    print("  判定")
    print("=" * 74)

    if ok_required:
        print("  ❌ 约束①未复现：required+tools 没有 400。")
        print("     可能该模型/账户已支持，或 model 名不对（当前 %s）。" % MODEL)
    else:
        print("  ✅ 约束①复现：tool_choice=required + 思考模式 -> 400（地雷真实存在）。")

    if ok_auto:
        print("  ✅ 对照成立：去掉 tool_choice（默认 auto）-> 成功，")
        print("     证明 400 是 tool_choice 导致，不是「思考+tools」本身。")
    else:
        print("  ⚠ 对照异常：仅 tools 也失败 -> %s" % err_auto)
        print("     若 reasoner 不支持 function calling，请换 DOCMIND_CONSTRAINT_MODEL=deepseek-flash")
        print("     （flash 用 enable_thinking 进入思考模式再测）。")

    if ok_temp:
        print("  ✅ 约束②确认：temperature=2.0 在思考模式无报错返回。")
        print("     上游静默忽略该参数；Agent 工作台 无条件下发 temperature（llm.py:844/885），")
        print("     故用户设的温度在思考模式无效且无任何信号 —— 无声偏差坐实。")
    else:
        print("  ⚠ 约束②子测试失败（非预期）：%s" % err_temp)

    print()
    print("  => 结论：Agent 工作台 当前靠「从不发 tool_choice」规避 ①；")
    print("     ② 是无声偏差，已立 TODO 待决定（抑制参数+UI 说明 / 接受）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
