# -*- coding: utf-8 -*-
"""容量越界必须**夹到边界**，不能回落 DEFAULTS。

## 这个 bug 的来龙去脉

WIP 把六个 DEFAULTS 全改成 0（= 不限），而 `normalize_settings` 越界时
**回落 DEFAULTS**。于是：

    用户填 5000  ->  判越界  ->  回落 0  =  不限

想收紧的人拿到最宽松的值，界面没有任何提示，行为与意图完全相反。
同一段注释里记着同类前情：早期 `0` 会被「修正」成 8。

## 修法

夹到声明范围 `[0, 上界]` —— 与前端 input 的 `min="0"` / `max="N"` 同一范围：

    5000 -> 1000        （数太大，取允许的最大值，语义仍是"收紧"）
      -5 -> 0           （夹到 min；0 在这套语义里就是"不限"）

## 必须同时满足的三件事

1. **0 必须原样保留** —— 它是合法的「不限」，不能因为「看起来像边界值」被修正
2. **上界必须真的夹住** —— 填超限大数得到的是上界，不是 0
3. **两侧口径一致** —— 前端 input 的 max 与这里夹取的上界是同一个数
   （由 tests/test_capacity_bounds_ui_backend_agree.py 锁住）
"""

import pytest

from agent_runtime.capacity import DEFAULTS, normalize_settings


class TestClampingNotDefaulting:
    """越界 -> 夹到边界；绝不 -> 回落默认。"""

    def test_oversized_number_clamps_to_the_cap_not_to_unlimited(self):
        """核心：填 5000 得到 1000，不是 0（不限）。"""
        for key in ("max_nudges", "max_final_continuations",
                    "main_max_steps", "child_max_steps"):
            out = normalize_settings({key: 5000})
            assert out[key] == 1000, (
                f"{key}=5000 被归一成 {out[key]}——"
                f"{'变成不限了，这正是要修的坑' if out[key] == 0 else '不是上界'}")

    def test_task_token_budget_clamps_to_its_own_cap(self):
        out = normalize_settings({"task_token_budget": 99_999_999})
        assert out["task_token_budget"] == 10_000_000, out["task_token_budget"]

    def test_max_agents_clamps_to_64(self):
        out = normalize_settings({"max_agents": 5000})
        assert out["max_agents"] == 64, out["max_agents"]

    def test_negative_raises_instead_of_becoming_unlimited(self):
        """负值 -> raise，绝不夹到 0（=不限）。

        这是本次改动的核心。旧行为是 `-1 -> 0`，而 0 在这套语义里是
        **不限** —— 手滑打个负号的人拿到最宽松的配置，且无任何提示。
        旧测试的方法名就叫 `..._is_still_rejected`，做的却是 clamp 到 0，
        名实不符；现在 raise 才对得起那个名字。
        """
        for key in ("max_nudges", "main_max_steps", "child_max_steps",
                    "max_final_continuations", "max_agents"):
            with pytest.raises(ValueError):
                normalize_settings({key: -5})


class TestZeroIsPreserved:
    """0 是合法的「不限」，必须原样保留。"""

    @pytest.mark.parametrize("key", [
        "max_agents", "main_max_steps", "child_max_steps",
        "max_nudges", "max_final_continuations", "task_token_budget",
    ])
    def test_zero_stays_zero(self, key):
        out = normalize_settings({key: 0})
        assert out[key] == 0, f"{key}=0 被改成了 {out[key]}"

    def test_defaults_are_all_zero_now(self):
        """这批 WIP 的语义基线：六个默认全是 0（不限）。

        若哪天有人把默认改回正数，这条会红 —— 那是故意的信号，
        不是噪声：默认值一改，界面兜底与本文档都要跟着复核。

        ⚠ **只对 int 项判，且必须排除 bool**：`local_auto_limit` 是布尔开关
        （显存自适应），不是步数上限，天生为 True。而 Python 里
        `isinstance(True, int)` 是 **True**（bool 是 int 的子类），所以
        `isinstance(v, int)` 挡不住它 —— 第一版就因此永远报
        「有默认值不是 0」，一条必红的断言比没有断言更糟。
        """
        numeric = {k: v for k, v in DEFAULTS.items()
                   if isinstance(v, int) and not isinstance(v, bool)}
        assert numeric, "DEFAULTS 没解析出数字项"
        nonzero = {k: v for k, v in numeric.items() if v != 0}
        assert not nonzero, f"有默认值不是 0 了：{nonzero}"


class TestNonNumericStillFallsBack:
    """非数字（None / 'abc'）无法夹取，仍回落默认 —— 这是唯一保留回落的地方。"""

    def test_garbage_falls_back_to_default(self):
        out = normalize_settings({"max_nudges": "abc"})
        assert out["max_nudges"] == DEFAULTS["max_nudges"]

    def test_none_falls_back_to_default(self):
        out = normalize_settings({"max_nudges": None})
        assert out["max_nudges"] == DEFAULTS["max_nudges"]

    def test_missing_key_falls_back_to_default(self):
        out = normalize_settings({})
        assert out["max_nudges"] == DEFAULTS["max_nudges"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
