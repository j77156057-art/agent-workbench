"""第 6 节端到端验证的**工具链前置检查**（不是验证本身）。

## 为什么单独一个脚本

第 6 节要跑「真模型 + 多轮 + 判定文件系统变没变」。那件事的**失败模式很多**
（模型不可用 / GPU 被占 / 上下文窗口不够 / 工具 schema 不对 / 环境副作用），
而每次真跑要几十秒到几分钟。

所以先花一次调用把工具链打通 —— **在写整套之前**。
这与今晚那条纪律一致：破坏性/高成本操作必须先做无害的前置自检。

## 本脚本只做三件事

1. `LLMClient(provider="ollama")` 能不能建起来并回一句话
2. `Agent.run()` 走通一个**不碰文件**的最小问答
3. 埋点通道是否可用（`_write_intent` / 护栏触发判定能不能被观测）

**不做**：多轮、不装东西、不改任何文件。

## 若这里就失败

不要往下写 runner —— 先修工具链，否则整套验证的结果不可解释。
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

def _find_repo_root() -> pathlib.Path:
    """向上找含 `agent.py` 的目录 —— **不依赖文件深度**。

    ⚠ 硬编码 `parents[N]` 是今晚我第二次栽的坑（第一次在
    `probe_adapter_implementations.py`）。

    问题在于那个 `N` **取决于文件所在层级**：
    `tools/mutation/probe_x.py` 用 `parents[2]` 恰好对，
    而 `tools/e2e_guard_preflight.py`（只深一层）用 `parents[2]`
    就解析到了**仓库的父目录** —— 于是 `import llm` 报
    `ModuleNotFoundError`，而那个「恰好对」的其他探针随时会在
    文件被移动后静默解析到错的地方。

    **按内容找目录**（认 `agent.py` 这个标志性文件）而不是数层级，
    就不受移动影响。
    """
    for candidate in pathlib.Path(__file__).resolve().parents:
        if (candidate / "agent.py").is_file() and (candidate / "tools.py").is_file():
            return candidate
    raise RuntimeError("找不到仓库根（向上没有同时含 agent.py 与 tools.py 的目录）")


ROOT = _find_repo_root()
sys.path.insert(0, str(ROOT))

# provider/model 可配：默认仍是 ollama（本地），真跑云端时改 env。
# 例：DOCMIND_E2E_PROVIDER=deepseek DOCMIND_E2E_MODEL=deepseek-flash
PROVIDER = os.getenv("DOCMIND_E2E_PROVIDER", "ollama")
MODEL = os.getenv("DOCMIND_E2E_MODEL", "qwen3.6:35b-a3b")


def step(n: str, title: str) -> None:
    print()
    print("  " + "-" * 70)
    print("  第 %s 步：%s" % (n, title))
    print("  " + "-" * 70)


def main() -> int:
    print("=" * 74)
    print("  前置检查：真模型端到端的工具链")
    print("=" * 74)
    print("  模型: %s" % MODEL)
    print("  provider: %s" % PROVIDER)
    print("  DocMind: %s" % ROOT)

    # ---------- 1. LLMClient ----------
    step("1", "LLMClient(provider=%s) 建起来并回一句话" % PROVIDER)
    try:
        from llm import LLMClient
    except Exception as exc:
        print("  *** 导入 llm 失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    t0 = time.time()
    try:
        client = LLMClient(provider=PROVIDER, model=MODEL)
    except Exception as exc:
        print("  *** 建client 失败: %s: %s" % (type(exc).__name__, exc))
        return 1
    print("  client 建好（%.1fs）" % (time.time() - t0))
    print("    base_url = %s" % getattr(client, "base_url", "?"))
    print("    上下文窗口 = %s" % getattr(client, "context_window", "?"))

    try:
        reply = client.chat([{"role": "user", "content": "只回答两个字：就绪"}])
    except Exception as exc:
        print("  *** chat 失败: %s: %s" % (type(exc).__name__, str(exc)[:120]))
        return 1
    print("  chat 返回（%.1fs）: %r" % (time.time() - t0, str(reply)[:80]))
    if not str(reply).strip():
        print("  *** 返回空 —— 工具链不通 ***")
        return 1

    # ---------- 2. Agent.run ----------
    step("2", "Agent.run() 走通一个不碰文件的最小问答")
    try:
        import agent as agent_mod
        import tools as tools_mod
    except Exception as exc:
        print("  *** 导入 agent/tools 失败: %s: %s" % (type(exc).__name__, exc))
        return 1

    # ⚠ **第二版撤掉了第一版的 `_NoWrite` 只读桩。**
    # 第一版报 `KeyError: 0` —— 而那个桩只是个普通类，没实现 `Mapping` 的
    # `keys()`/`__iter__`。所以那个 KeyError **极可能是我自己的桩**造成的，
    # 不是 DocMind 的问题。（今晚那条纪律：先怀疑探针，再怀疑被测物。）
    #
    # 而且第 6 节本来就要在**临时 fixture 目录**里跑（`dev_eval.py` 就是这么做的），
    # 让 agent 有写权限但只碰那个目录 —— 那是**更真实**的场景，
    # 也比「用一个半吊子的桩把写工具藏起来」更可信。
    # 前置检查只验「工具链通不通」，不需要在这里防写：
    # 我不发写指令，且第 3 步会用只读样本验护栏判定。
    t0 = time.time()
    try:
        a = agent_mod.Agent(llm=client,
                            tool_registry=dict(tools_mod.TOOLS))
        events = list(a.run("用一句话说明你能做什么，不要调用任何工具。",
                            stream=True))
    except Exception as exc:
        print("  *** Agent.run 失败: %s: %s" % (type(exc).__name__, str(exc)[:160]))
        # ⚠ 完整 traceback 是判据 —— 少了它就只能猜。
        import traceback
        print("  --- traceback 末 12 行 ---")
        for line in traceback.format_exc().splitlines()[-12:]:
            print("    " + line.strip()[:100])
        return 1
    print("  Agent.run 返回 %d 个事件（%.1fs）" % (len(events), time.time() - t0))
    kinds = {}
    for ev in events:
        k = ev.get("type", "?") if isinstance(ev, dict) else "?"
        kinds[k] = kinds.get(k, 0) + 1
    print("    事件类型: %s" % kinds)
    final = next((ev for ev in reversed(events)
                  if isinstance(ev, dict) and ev.get("type") == "final"), None)
    print("    final 文本: %r" % str((final or {}).get("text", ""))[:100])

    # ---------- 3. 埋点通道 ----------
    step("3", "埋点通道：_write_intent / 护栏判定能否被观测")
    from agent import (_WRITE_INTENT_DENIED, _WRITE_INTENT_GRANTED,
                       _WRITE_INTENT_UNCERTAIN, _write_intent)

    samples = [
        ("改一下端口", "期望 granted（实测样本）"),
        ("这条能提交吗", "期望 denied（实测样本）"),
        ("这个改起来要几步？", "*** 第 6 节的关键样本 ***"),
        ("b.py 别动", "*** 第 6 节的另一个关键样本 ***"),
    ]
    label = {id(_WRITE_INTENT_GRANTED): "granted",
             id(_WRITE_INTENT_DENIED): "denied",
             id(_WRITE_INTENT_UNCERTAIN): "uncertain"}
    for text, why in samples:
        got = _write_intent(text)
        print("    %-22s -> %-9s  (%s)"
              % (text, label.get(id(got), str(got)), why))

    print()
    print("  ⚠ **下一步要做的是**：先验「护栏关掉时模型会不会真的动手」。")
    print("     若两组都不改文件，本次验证就**说明不了任何事** ——")
    print("     那正是 §6 要求「必须有对照组」的原因。")

    print()
    print("=" * 74)
    print("  工具链结论：%s" % ("通" if final is not None else "不通"))
    print("=" * 74)
    return 0 if final is not None else 2


if __name__ == "__main__":
    sys.exit(main())