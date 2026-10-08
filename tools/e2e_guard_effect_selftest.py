"""用假 agent/client 走完 `e2e_guard_effect.run_group` 的 4 轮控制流。

## 为什么需要它

第一版把「第 4 轮前重置 fixture」那段插到了 `for` 语句**外面**，
于是 `UnboundLocalError: cannot access local variable 'turn_no'`。

**`ast.parse` 抓不到这个** —— 那是运行时错误、要真进那个分支才炸。
所以「语法 OK」不等于「能跑」。

这个脚本用假的 `Agent`（`run()` 直接返回固定事件、不碰模型）把
`run_group` 走完 4 轮，**不烧任何模型时间**，专门抓这类控制流错误。

## 它验什么 / 不验什么

**验**：轮次循环、第 4 轮重置分支、埋点、fixture 哈希比对、异常路径。
**不验**：模型行为、护栏效果 —— 那些要真模型。

用完即弃，但它比「跑一次 10 分钟的端到端然后看报错」快两个数量级。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import e2e_guard_effect as E  # noqa: E402

RESETS: list[int] = []


def fake_client():
    class _C:
        provider = "ollama"
        model = "fake"

        def chat(self, *a, **k):
            return "fake"

        def clone(self):
            return self
    return _C()


def main() -> int:
    import agent as agent_mod

    original_agent_cls = agent_mod.Agent
    seen_questions: list[str] = []

    class _FakeAgent:
        """第 1 轮「改文件」，其余轮不动 —— 模拟上一次的观测形态。"""

        def __init__(self, *a, **k):
            self.n = 0

        def run(self, question, stream=True):
            self.n += 1
            seen_questions.append(question)
            if self.n == 1:
                # 改 a.py，让「文件系统变化」这一路也被走到
                import agent_runtime  # noqa: F401  （确保运行时配置已加载）
                root = getattr(self, "_root", None)
                if root is not None:
                    (root / "a.py").write_text("def add(a, b):\n    return a + b\n",
                                               encoding="utf-8")
            return iter([{"type": "token", "text": "x"},
                         {"type": "final", "text": "done"}])

    # 让 fake agent 能拿到 fixture 根 —— run_group 内部创建它，
    # 所以这里通过 config 的 code_root 拿（run_group 会 set_runtime 成临时目录）
    from config import get_runtime

    class _Agent2(_FakeAgent):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._root = pathlib.Path(get_runtime("code_root", "") or ".")

    agent_mod.Agent = _Agent2
    try:
        print("=" * 70)
        print("  用假 agent 走完 4 轮控制流（不烧模型）")
        print("=" * 70)
        res = E.run_group("假 agent 组", True, fake_client)
        print()
        print("  实际跑到的题目：")
        for q in seen_questions:
            print("    %s" % q[:52])
        print()
        print("  轮数: %d（应为 4）" % len(seen_questions))
        ok = len(seen_questions) == 4
        # 第 4 轮必须是 TURN4（且它前面的重置分支要能跑）
        if seen_questions and seen_questions[-1] == E.TURN4:
            print("  ✓ 第 4 轮走到 TURN4（重置分支未再抛 UnboundLocalError）")
        else:
            print("  ✗ 第 4 轮没走到 TURN4 —— 最后一句是 %r" %
                  (seen_questions[-1] if seen_questions else None))
            ok = False
        # 埋点里应有 4 条
        print("  埋点条数: %d（应为 4）" % len(res["turns"]))
        ok = ok and len(res["turns"]) == 4

        # ---- 第二段：直接跑 main() 的判定段 ----
        # ⚠ **第一版自检只走 run_group，所以真跑时 `NameError: ctrl` 溜过去了。**
        # 判定段的 `ctrl = next(...)` 是在真跑 8 轮（约 3 分钟）之后才炸的 ——
        # 也就是说自检本来能 1 秒抓到，却因为少跑了一段而没抓到。
        print()
        print("  --- 第二段：main() 的判定段 ---")
        rc = _drive_judgement()
        print("  判定段返回码 = %s%s"
              % (rc, "" if rc in (0, 3, 5, 0) else "   *** 异常 ***"))
        ok = ok and rc in (0, 3, 5)
        return 0 if ok else 1
    finally:
        agent_mod.Agent = original_agent_cls


def _drive_judgement() -> int:
    """用**构造的**两组结果直接进 main() 的判定段。

    做法：临时把 `run_group` 换成返回预设结果，让 `main()` 跑完
    「分组 → 取 ctrl/exp → 判第 1 轮 → 复核词表」那一整段。

    复现的正是这次真跑炸掉的那个 `NameError: name 'ctrl' is not defined`。
    """
    def fake_run_group(label, guard_on, client_factory):
        """轮 1 按 guard_on 决定改不改；轮 4（granted）两组都改。"""
        turns = [
            {"turn": 1, "question": E.TURN1, "write_intent": "denied",
             "changed_files": [] if guard_on else ["a.py"]},
            {"turn": 2, "question": E.TURN2, "write_intent": "denied",
             "changed_files": []},
            {"turn": 3, "question": E.TURN3, "write_intent": "denied",
             "changed_files": [] if guard_on else ["a.py"]},
            {"turn": 4, "question": E.TURN4, "write_intent": "granted",
             "changed_files": ["a.py"]},
        ]
        return {"label": label, "guard_on": guard_on, "turns": turns,
                "fixture_before": {}, "fixture_after": {}}

    original = E.run_group
    E.run_group = fake_run_group
    try:
        E.main()
        return 0
    except NameError as exc:
        print("  *** 判定段抛 NameError（就是这次真跑炸的那个）: %s" % exc)
        return 9
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        E.run_group = original


if __name__ == "__main__":
    sys.exit(main())