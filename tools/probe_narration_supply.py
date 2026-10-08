"""定性问题：叙述开关开了却 0 条解说 —— 模型**没写**，还是**写进了 thinking**？

## 为什么必须区分这两种可能

`probe_narration_event_mix.py` 的 A 轮（叙述开、thinking 开）实测 **thought = 0**。
但事件计数**分不开**这两种情况：

* **(a) 模型把叙述全放进 reasoning**，工具轮的**正文散文为空**
* **(b) 模型确实没写任何正文散文**，与 thinking 无关

两者的修法完全相反：
* (a) → 提示词改动无效，**该做的是把已有的 reasoning 显示出来**（流式自动展开）
* (b) → 提示词有效但被别的东西挡住了，**该查挡它的是谁**

第一次跑（thinking **关**）实测 `thought = 2`，第二次（thinking 开）实测 `0` ——
方向相反，说明 thinking 是关键变量。但那仍然只是相关，不是判定。

## 怎么判定：观测 `_PromptNarrationFilter.offer` 的入参与拒绝理由

不改生产代码，只**包一层**把每次调用的入参和返回记录下来。这比在 agent 里
加日志轻得多，也不会影响被判测对象的行为（offer 是纯函数）。

三种拒绝理由要分开看，它们指向完全不同的结论：

| 拒绝理由 | 说明 | 指向 |
|---|---|---|
| `acc` 为空/仅空白 | 模型这一轮**根本没写正文** | (b)，或模型只走 tool_call |
| 剥完为空 | 写了，但**全是协议标记**（`Action:`/`Final Answer:` 等） | 契约压制成功，(b) |
| 长度 < 8 或被去重 | 写了但没实质内容 / 与前一条重复 | 无害，不是问题 |

⚠ **恒真防护**：若整个 run 一次 `offer` 都没被调用，说明挂钩点失效
（生产代码改了路径），此时**必须退出非 0 且不产出任何结论** ——
否则「没抓到」会被当成「模型没写」。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ROOT = ensure_on_path()

MODEL = os.getenv("DOCMIND_E2E_MODEL", "qwen3.6:35b-a3b")
READONLY = ("read_file", "list_dir", "grep", "search_code", "calculate")

QUESTION = os.getenv(
    "DOCMIND_NARRATION_Q",
    "a.py 里的 add 函数到底是加法还是减法？b.py 呢？"
    "顺便看看 test_a.py 有没有测它。你要读几个文件才能答？",
)

FIXTURE = {
    "a.py": "def add(a, b):\n    return a - b\n",
    "b.py": "def mul(a, b):\n    return a * b\n",
    "test_a.py": "from a import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}


def _reason(raw: str, cleaned: str, verdict: str) -> str:
    """给出拒绝/放行理由。三类必须分开 —— 它们指向不同的结论。"""
    if verdict:
        return "放行"
    if not (raw or "").strip():
        return "acc 为空 —— 模型这一轮没写任何正文"
    if not cleaned:
        return "剥完为空 —— 写了但全是协议标记"
    if len(cleaned) < 8:
        return "长度 < 8 —— 写了但没实质内容"
    return "被去重 —— 与本轮前一条重复"


def main() -> int:
    print("=" * 78)
    print("  叙述供给侧定性问题：模型写没写正文散文？")
    print("=" * 78)
    print("  模型: %s" % MODEL)
    print("  NARRATION = %s（默认应为 True）" % os.getenv("DOCMIND_NARRATION", "1"))
    print("  thinking  : %s" % os.getenv("DOCMIND_NARRATION_THINKING", "1"))

    try:
        from llm import LLMClient
        import agent as agent_mod
        import tools as tools_mod
        from config import set_runtime
    except Exception as exc:
        print("  *** 导入失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    # ---- 包一层 offer，把入参与理由记下来（不改生产逻辑）----
    seen: list[dict] = []
    real_offer = agent_mod._PromptNarrationFilter.offer

    def spy_offer(self, text):                       # noqa: ANN001
        cleaned = self.strip_protocol(text or "")
        verdict = real_offer(self, text)
        seen.append({
            "acc_len": len(text or ""),
            "acc_head": (text or "")[:220],
            "cleaned_len": len(cleaned),
            "cleaned_head": cleaned[:220],
            "verdict_len": len(verdict),
            "reason": _reason(text or "", cleaned, verdict),
        })
        return verdict

    agent_mod._PromptNarrationFilter.offer = spy_offer

    registry = {k: v for k, v in tools_mod.TOOLS.items() if k in READONLY}
    tmpdir = tempfile.mkdtemp(prefix="narr_supply_")
    try:
        for name, body in FIXTURE.items():
            pathlib.Path(tmpdir, name).write_text(body, encoding="utf-8")
        set_runtime("code_root", tmpdir)
        client = LLMClient(provider="ollama", model=MODEL)
        a = agent_mod.Agent(llm=client, tool_registry=dict(registry))
        events = list(a.run(QUESTION, stream=True,
                            thinking_enabled=os.getenv(
                                "DOCMIND_NARRATION_THINKING", "1") != "0"))

        import collections
        counts = collections.Counter(str(e.get("type")) for e in events
                                     if isinstance(e, dict))
        print()
        print("  事件: 总 %d  %s" % (len(events), dict(counts.most_common())))

        # ---- 恒真防护 ----
        print()
        if not seen:
            print("  *** `offer` 一次都没被调用 —— **本次不产出任何结论**")
            print("     （生产代码可能已不走这条路径；「没抓到」不等于「模型没写」）")
            return 2

        print("  === offer 被调用 %d 次，逐次理由 ===" % len(seen))
        for i, r in enumerate(seen, 1):
            print("    [%d] acc %d 字 -> 剥后 %d 字 -> 放行 %d 字"
                  % (i, r["acc_len"], r["cleaned_len"], r["verdict_len"]))
            print("        理由: %s" % r["reason"])
            if r["acc_head"]:
                print("        acc 开头: %s" % r["acc_head"].replace("\n", "\\n")[:100])
        print()

        reasons = collections.Counter(r["reason"] for r in seen)
        print("  === 理由分布 ===")
        for k, v in reasons.most_common():
            print("    %-42s %d 次" % (k, v))
        print()

        # ---- 判定 ----
        print("  === 判定 ===")
        empties = reasons.get("acc 为空 —— 模型这一轮没写任何正文", 0)
        protocol = reasons.get("剥完为空 —— 写了但全是协议标记", 0)
        granted = reasons.get("放行", 0)
        if granted:
            print("  ✅ 模型**确实写了**可用的正文散文（放行 %d 次）" % granted)
            print("     => 供给侧没问题；若前端看不到，是渲染/折叠问题。")
        elif empties >= max(1, len(seen) - 1):
            print("  📌 **(b) 模型这一轮没写任何正文散文**（%d/%d 次 acc 为空）" % (empties, len(seen)))
            print("     => 提示词改动对这个模型**无效**；真正的内容在 reasoning 里。")
            print("     => 该做的是把已有 reasoning 显示出来，而不是继续加提示词。")
        elif protocol:
            print("  📌 模型写了正文，但**全是协议标记**（%d 次）" % protocol)
            print("     => 契约压制**是生效的**；要人话需要换写法而不是加权限。")
        else:
            print("  ⚠ 混合情形，逐条看上面的理由。")
            print("     **单次跑不足以定性** —— 同一判据至少要 3 次。")

        dump = pathlib.Path(os.getenv("DOCMIND_SUPPLY_DUMP",
                                       "D:/Temp/opencode/narration_supply.json"))
        try:
            dump.write_text(json.dumps(
                {"model": MODEL, "question": QUESTION, "calls": seen,
                 "event_counts": dict(counts)}, ensure_ascii=False, indent=1),
                encoding="utf-8")
            print()
            print("  逐次入参已落盘: %s" % dump)
        except Exception as exc:
            print("  *** 落盘失败: %s" % exc)
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
