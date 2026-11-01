# `passed` 与 `score` 的区别（agent_eval.py）

1. **`passed` 是布尔合取**：一条题的所有 checks 全过才算过，且必须先"有 checks"。
   出处：`agent_eval.py:143` — `passed = scored and all(c["ok"] for c in checks)`
2. **`score` 是比例**：过了几条 check 除以 check 总数，与"全不全过"无关。
   出处：`agent_eval.py:148` — `"score": (sum(1 for c in checks if c["ok"]) / len(checks)) if checks else 0.0`
3. **回归门只认 `passed`，不认 `score`**：baseline 映射存的是每题的 `passed`，pass→fail 才算回归；汇总里的 `pass_rate` 也按 `passed` 计数，`avg_score` 只是旁证。
   出处：`agent_eval.py:210` — `base_map = {i["id"]: i["passed"] for i in base["items"] if i["scored"]}`

## 为什么要分开

`score` 会掩盖"差一条就崩"的题：8 条 check 过 7 条 → `score=0.875` 但 `passed=False`，
发布门（`agent_eval.py:255`：`if report["failed"] > 0 or report["unscored"] > 0`）照样红。
反过来 `passed` 也看不出改进幅度，所以两个都要报（`agent_eval.py:201` 的 `pass_rate`
与 `agent_eval.py:202` 的 `avg_score` 是并列字段，不是一个指标的两种写法）。

## 一个容易踩的边界

没有 `expect`（或 expect 里一条检查都没触发）时 `checks` 为空 → `scored=False`
（`agent_eval.py:142`：`scored = bool(checks)`）→ 这条题 `passed=False`、`score=0.0`，
但它**不进 `avg_score` 的分母**（`agent_eval.py:202` 只对 `scored` 求和），而是计入
`unscored`（`agent_eval.py:198`）并被发布门当成失败（`agent_eval.py:255`）。
也就是说"没评分"不等于"零分"，也不等于"及格"，它是第三种状态。
