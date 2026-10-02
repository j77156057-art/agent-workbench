"""工具失败**按类别分级处置**，而不是一律丢回给模型换思路。

## 现状与问题

`execute_tool` 早就把失败分了类（`ToolResult.error_kind`）：timeout / exception /
invalid_result / legacy_failure / idempotency_unavailable / idempotency_in_doubt /
project_stage_blocked。但 agent 主循环**一视同仁**：不管哪一类，都只给模型一句
"换一种方式重试（改用其他工具或改写查询）"。

于是三类本不该重试的失败被当成"再试一次说不定就成了"：

| error_kind | 真实性质 | 该做什么 |
|---|---|---|
| `timeout` | 这一次的网络/进程超时 | 值得原样重试 —— 大概率瞬时抖动 |
| `exception` | 工具内部抛错 | 取决于错误文本，可能是代码 bug，重试无意义 |
| `invalid_result` | 工具返回值不符合协议 | **重试必然同样失败**，这是代码 bug |
| `idempotency_in_doubt` | 上次执行中途挂了，完成状态未知 | **绝不重放** —— 会造成重复副作用 |
| `idempotency_unavailable` | 幂等账本不可用 | 重试无意义，且对写工具不安全 |
| `project_stage_blocked` | 试做区禁止该工具 | 重试必然被拒 |

`idempotency_in_doubt` 那条尤其要紧：`execute_tool` 已经**拒绝执行**了，但如果主
循环还提示模型"换个参数再试"，模型会换一个参数再调一次 —— 而幂等账本会按
新参数算出新 key，于是这道唯一挡住重复副作用的闸门被绕开了。

## 处置分级

    ·可重试（transient）  timeout / legacy_failure / 搜索相关性不足
                          → 明确告诉模型「重试同一次调用」，并给建议
    ·不可重试（permanent） invalid_result / idempotency_* / project_stage_blocked
                          → 明确禁止重试，直接换工具或收尾
    ·不确定（unknown）     exception
                          → 看错误文本；含 bug 迹象就归不可重试

**明确禁止重试**这件事必须写进给模型的提示里。理由很直接：模型在拿到
"未返回有效结果"时会本能地重试，而对 `invalid_result` 这类失败，重试 100% 还是
同样的错——浪费步数，还把真正的原因埋在观察堆里。
"""

#: 值得原样重试的失败类别。同一调用再来一次，大概率能成。
TRANSIENT_ERROR_KINDS = frozenset({
    "timeout",             # 这一次的网络/进程超时，与代码无关
    "legacy_failure",      # 工具自己判定的"这次没成"（检索无果等）
    "",                    # 非失败路径留空时也归这一类
})

#: 重试必然同样失败的类别。提示模型重试是**错的信息**。
PERMANENT_ERROR_KINDS = frozenset({
    "invalid_result",          # 返回值不符合协议 —— 代码 bug
    "idempotency_in_doubt",    # 上次执行中途挂了，状态未知：绝不重放
    "idempotency_unavailable", # 幂等账本不可用，无法安全重试
    "project_stage_blocked",   # 试做区禁止此类工具
    "safety_blocked",          # 护栏类拦截（若上游细分出该类）
})

#: `exception` 归到不可重试的错误特征。命中说明是代码 bug，而不是环境抖动。
_BUG_HINTS = (
    "nonetype", "attributeerror", "nameerror", "typeerror", "keyerror",
    "indexerror", "syntaxerror", "notimplemented", "unboundlocal",
    "recursionerror", "zerodivision", "importerror", "modulenotfound",
)


def classify_failure(error_kind: str, text: str = "") -> str:
    """把一次工具失败归成 transient / permanent / unknown。

    返回 `permanent` 的两种来源：
      · 类别本身就在PERMANENT_ERROR_KINDS 里；
      · 类别是 exception，但错误文本带代码 bug 的特征。
    """
    kind = str(error_kind or "")
    if kind in PERMANENT_ERROR_KINDS:
        return "permanent"
    if kind in TRANSIENT_ERROR_KINDS:
        return "transient"
    if kind == "exception":
        low = str(text or "").lower()
        if any(hint in low for hint in _BUG_HINTS):
            return "permanent"
        return "unknown"
    # 未知类别：fail-open 归 unknown，让模型自己判断，但不得当成必然可重试。
    return "unknown"


#: 每类的处置措辞。都写明"能不能再试"——这是本模块存在的全部理由。
_GUIDANCE = {
    "transient": (
        "这是一次**可重试**的失败（{kind}），最可能是环境瞬时抖动。"
        "可以用**完全相同的参数**直接重试一次；若仍失败再换思路。"
    ),
    "permanent": (
        "这类失败（{kind}）**重试必然同样失败**，不要再用任何参数重试它——"
        "重试只是浪费步数。请立刻改用别的工具/别的做法，或直接基于已有证据收尾。"
    ),
    "unknown": (
        "这是一次不确定的失败（{kind}）。如果你能看出原因，换一种**针对性**的做法；"
        "看不出就直接换工具，不要原样重试。"
    ),
}


def failure_guidance(error_kind: str, text: str = "") -> tuple:
    """返回 `(类别, 给模型看的一句话)`。

    类别用于主循环决定计数策略（permanent 不该累加"连续失败"到熔断），
    句子用于拼进 Reflection 提示。
    """
    category = classify_failure(error_kind, text)
    label = str(error_kind or "unspecified")
    return category, _GUIDANCE[category].format(kind=label)


def retry_budget_cost(category: str) -> int:
    """这一类失败该不该消耗"连续失败"计数。

    permanent 类不该消耗：它本来就不该重试，累加它只会让熔断阈值提前触发，
    在模型还没试够别的路之前就把整轮收尾掉。
    """
    return 0 if category == "permanent" else 1