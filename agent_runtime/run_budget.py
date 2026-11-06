"""Shared per-task token budget for the main agent and workflow helpers."""
from __future__ import annotations

import threading
from typing import Any, Mapping, Sequence

#: 字符 → token 的折算除数。**实测值，不是拍脑袋的 4。**
#:
#: 2026-11-06 用本机 Qwen3.5 tokenizer（`D:\llama.cpp\llama-tokenize.exe`
#: 读本机 GGUF，只做本地分词，不发起任何模型请求）量了「最小提示词」的三种形态：
#:
#:     无记忆   22626 字符 -> 11416 token   （1.982 字符/token）
#:     带记忆   22936 字符 -> 11580 token   （1.981 字符/token）
#:     live     22799 字符 -> 11510 token   （1.981 字符/token）
#:
#: 旧的 4 会把中文为主的提示词低估约一半 —— `chars/4` 只有真实 token 的 50%，
#: 于是按它算出来的「任务 Token 下界」在真实运行态里撑不住承诺的轮数。
#: 取 **1.9**（比实测 1.98 略保守，留约 4% 余量），提示词越偏中文越安全。
#: 若提示词的中英文构成发生明显变化，应重新量一次再调这个值。
CHARS_PER_TOKEN = 1.9


def _usage_value(usage: Mapping[str, Any] | None, keys: Sequence[str]) -> int:
    if not isinstance(usage, Mapping):
        return 0
    for key in keys:
        value = usage.get(key)
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
    return 0


class RunBudget:
    """One budget shared by every model call belonging to a top-level task.

    The budget is deliberately independent from provider billing.  It is a
    local safety rail that also works for local models and providers without
    reliable cost data.
    """

    def __init__(self, token_limit: int = 0):
        self.token_limit = max(0, int(token_limit or 0))
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls = 0
        self.estimated_calls = 0
        self.exhausted = False
        self._lock = threading.RLock()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def remaining(self) -> int | None:
        if self.token_limit <= 0:
            return None
        return max(0, self.token_limit - self.total_tokens)

    def preflight(self, prompt_chars: int = 0, output_tokens: int = 0) -> bool:
        """Return whether a call is allowed before it is sent to the model."""
        with self._lock:
            if self.token_limit <= 0:
                return True
            # 除数来自 tokenizer 实测（见模块顶部 CHARS_PER_TOKEN）。
            estimated_prompt = max(1, int(max(0, prompt_chars) / CHARS_PER_TOKEN))
            projected = self.total_tokens + estimated_prompt + max(0, int(output_tokens or 0))
            if projected > self.token_limit:
                self.exhausted = True
                return False
            self.estimated_calls += 1
            return True

    def record(self, usage: Mapping[str, Any] | None = None,
               *, prompt_chars: int = 0, output_chars: int = 0) -> dict[str, int | bool | None]:
        """Record provider usage, falling back to a conservative text estimate."""
        prompt = _usage_value(usage, ("prompt_tokens", "input_tokens", "prompt_eval_count", "in"))
        completion = _usage_value(usage, ("completion_tokens", "output_tokens", "eval_count", "out"))
        if not prompt:
            prompt = max(0, int(max(0, prompt_chars) / CHARS_PER_TOKEN))
        if not completion:
            completion = max(0, int(max(0, output_chars) / CHARS_PER_TOKEN))
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.calls += 1
            if self.token_limit > 0 and self.total_tokens >= self.token_limit:
                self.exhausted = True
            return self.snapshot()

    def snapshot(self) -> dict[str, int | bool | None]:
        with self._lock:
            return {
                "limit": self.token_limit,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "remaining": self.remaining,
                "calls": self.calls,
                "exhausted": bool(self.exhausted),
            }


__all__ = ["RunBudget"]
