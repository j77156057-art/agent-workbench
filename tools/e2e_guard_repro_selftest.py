"""验 `e2e_guard_repro.py` 的**排除 invalid** 修正。

## 为什么用构造数据而不是真跑

这次修正的全部要点是「遇到 API 失败的轮次会怎么处理」。
而真跑**不一定碰得到** API 失败（要 Ollama 恰好抖动）——
上次碰到过一次是运气。

**构造数据能确定性地覆盖三个分支**：
1. 有 invalid 格 → 必须被排除、且排除数量被报出来
2. 有 suspicious 格 → 必须**不被排除**（只标记）
3. 旧格式（无 grade 字段）→ 必须被报出来「按 ok 处理」

⚠ 而这正是今晚的教训：**断言必须能被确定性地触发**，
不能靠「跑一次看看会不会红」。

## 它验什么

* `e2e_guard_effect.classify_round` —— 三级分类本身
* `e2e_guard_repro.py` 的排除/标记/报数逻辑（走它的 `main()`）

**不验**：模型行为、护栏效果（那些要真模型）。
"""

from __future__ import annotations

import io
import json
import pathlib
import contextlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import e2e_guard_effect as E  # noqa: E402

OK = "正常回答，模型做了它该做的事。"
FAIL_API = "[Response interrupted by API Errors]"
HELD = "apply_edit(a.py) [已拦截：未授权写入]"


def test_classify() -> None:
    print("=" * 72)
    print("  1) classify_round：三级分类")
    print("=" * 72)
    cases = [
        # (final 文本, 事件数, 期望等级, 说明)
        (OK, 95, "ok", "正常长回答"),
        (FAIL_API, 8, "invalid", "API 失败（真观测到的那个）"),
        ("", 2, "invalid", "final 为空"),
        ("[已读取项目文件]", 2, "suspicious", "方括号占位 + 事件极少（真观测到的那个）"),
        (HELD, 40, "ok", "⚠ 护栏拦截文本也是方括号 —— **不能**判成可疑/无效"),
        ("[已读取项目文件]", 60, "ok", "方括号但事件很多 —— 大概是合法输出"),
        ("APIConnectionError: Connection reset", 30, "invalid", "另一种 API 错误"),
    ]
    bad = 0
    for text, n, want, why in cases:
        got, reason = E.classify_round(text, n)
        ok = got == want
        bad += 0 if ok else 1
        print("  %-5s %-11s %-46s %s"
              % ("OK" if ok else "FAIL", got, text[:44] or "(空)", why))
        if not ok:
            print("        期望 %s，实际 %s（%s）" % (want, got, reason))
    print()
    print("  => %s" % ("全部符合预期" if bad == 0 else "%d 条不符" % bad))
    return bad == 0


def _grid(grade_map: dict) -> dict:
    base = {
        "对照组/轮1": ("denied", True), "对照组/轮2": ("denied", False),
        "对照组/轮3": ("denied", True), "对照组/轮4": ("granted", True),
        "实验组/轮1": ("denied", False), "实验组/轮2": ("denied", False),
        "实验组/轮3": ("denied", False), "实验组/轮4": ("granted", True),
    }
    out = {}
    for k, (intent, changed) in base.items():
        g, reason = grade_map.get(k, ("ok", ""))
        out[k] = {"question": "q", "write_intent": intent, "changed": changed,
                  "changed_files": ["a.py"] if changed else [], "seconds": 1,
                  "n_events": 95, "grade": g, "grade_reason": reason}
    return out


def _run_repro(a_grid: dict, b_grid: dict, tag: str) -> tuple[int, str]:
    import e2e_guard_repro as R
    p = pathlib.Path("D:/Temp/opencode/_t_a.json")
    q = pathlib.Path("D:/Temp/opencode/_t_b.json")
    p.write_text(json.dumps({"model": "fake", "grid": a_grid}, ensure_ascii=False),
                 encoding="utf-8")
    q.write_text(json.dumps({"model": "fake", "grid": b_grid}, ensure_ascii=False),
                 encoding="utf-8")
    old_argv = sys.argv
    sys.argv = [str(R.__file__), str(p), str(q)]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = R.main()
    finally:
        sys.argv = old_argv
        p.unlink(missing_ok=True)
        q.unlink(missing_ok=True)
    print()
    print("=" * 72)
    print("  %s" % tag)
    print("=" * 72)
    print(buf.getvalue().rstrip())
    print("  返回码 = %s" % rc)
    return rc, buf.getvalue()


def main() -> int:
    ok1 = test_classify()

    print()
    print("=" * 72)
    print("  2) 对比工具：有 invalid 格必须被排除，且报出数量")
    print("=" * 72)
    # A：全 ok；B：两格 invalid（复刻真观测到的 API 失败）
    b = _grid({
        "对照组/轮1": ("suspicious", "final 是方括号占位文本且事件只有 2 个"),
        "对照组/轮4": ("invalid", "命中已确认的 API/SDK 错误文案：Response interrupted by API Errors"),
    })
    rc, out = _run_repro(_grid({}), b, "场景1：A 全 ok，B 有 1 invalid + 1 suspicious")
    # ⚠ **下面两条断言我第一版写错了，被构造数据当场抓出来：**
    #   * 「一致度按剩余格算」—— 我写的候选是 5/6、4/6、5/7，而实际是
    #     **7/7**（8 格减 1 格 invalid）。我算错了：suspicious **不排除**，
    #     所以只有 invalid 那一格被拿掉。
    #   * 「报出可疑但不排除」—— 我搜的字串是「可疑 1 格」，而工具打的是
    #     「**标记 1 格可疑（未排除）**」—— 顺序不同。
    #
    # 这正是构造数据的价值：**真跑 3 分钟且不一定碰得到失败分支，
    # 而构造数据 1 秒就把我自己的断言错误抓了出来。**
    checks = {
        "排除了 1 格": "排除 1 格 invalid" in out,
        "可疑被标记且未排除": ("标记 1 格可疑（未排除）" in out
                          and "对照组/轮1" in out),
        "一致度按剩余 7 格算（suspicious 不排除）":
            "7/7（已排除 1 格 invalid）" in out,
        "提示样本变小": "样本越小" in out,
        "两个关键结论仍成立": ("「拦得住」" in out and "「放得过」" in out),
    }
    ok2 = all(checks.values())
    for name, good in checks.items():
        print("  %-5s %s" % ("OK" if good else "FAIL", name))

    print()
    print("=" * 72)
    print("  3) 旧格式数据（无 grade 字段）必须被报出来")
    print("=" * 72)
    legacy = _grid({})
    for v in legacy.values():
        v.pop("grade", None)
        v.pop("grade_reason", None)
    rc2, out2 = _run_repro(legacy, _grid({}), "场景2：两份都是旧格式（无 grade）")
    ok3 = "没有 grade 字段" in out2
    print("  %-5s 报出「无 grade 字段」: %s" % ("OK" if ok3 else "FAIL", ok3))

    print()
    print("=" * 72)
    print("  汇总")
    print("=" * 72)
    for tag, good in (("classify_round 三级分类", ok1),
                      ("对比工具排除 invalid + 报数", ok2),
                      ("旧格式被报出", ok3)):
        print("  %-34s %s" % (tag, "OK" if good else "*** FAIL ***"))
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())