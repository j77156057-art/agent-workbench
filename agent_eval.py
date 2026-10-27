"""黄金题自动打分 + baseline 回归门。

给 run_golden.py 产出的 results.jsonl 打分：默认走**确定性规则**（离线可跑，适合 CI），
可选叠加 LLM-judge（需要模型）。并与 baseline jsonl 对比，pass→fail 即为回归（退出码 1）。

题库里每题可带 `expect` 字段（run_golden 会把它原样写进结果行，结果文件自包含）：

  {
    "id": "Q1",
    "question": "在 godot_sample 找定义 _ready() 的 .gd 文件，用 文件名:行号 格式回答",
    "expect": {
      "must_include": ["player.gd:", "enemy.gd:"],   // 每条都必须出现
      "any_of": [".gd:", "behaviors/"],               // 至少出现一条
      "must_not_include": ["player.py"],              // 幻觉标记，出现即失败
      "regex": "\\\\w+\\\\.gd:\\\\d+",                  // 正则必须命中
      "must_call": ["search_code"],                   // 必须调用过的工具（按前缀匹配，AND：全部都要）
      "any_of_call": ["grep", "search_code"],         // 至少调用过其中之一（OR： interchangeable 检索工具）
      "min_actions": 1, "max_actions": 8,
      "no_error": true,
      "degraded": true,                               // final 是否必须被标降级（未验证写=true / 干净已验证写=false）
      "verification_status": "unverified",            // 期望的写后自验证终态（如未验证）
      "judge_criteria": "答案是否给出了具体的文件与行号，且未编造"  // 仅 --judge 时生效
    }
  }

命令行：
  python agent_eval.py --results results_r1.jsonl                     # 打分并打印
  python agent_eval.py --results r1.jsonl --baseline baseline.jsonl   # 回归门（有回归则退出码 1）
  python agent_eval.py --results r1.jsonl --judge --provider ollama   # 叠加 LLM-judge
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def _final(rec):
    return rec.get("final") or ""


def _actions(rec):
    return [str(a) for a in (rec.get("actions") or [])]


def _action_called(tool, actions):
    return any(a == tool or a.startswith(tool + "(") for a in actions)


#: 子句分隔符：中文句读 + 换行。用来把「命中禁用词的那一小句」切出来单独判否定。
_CLAUSE_SPLIT_RE = re.compile(r"[。！？；;!?]\s*|\n")

#: 否定/辟谣线索。命中即认为该子句是在**否定**这个字符串（如「不是 12」
#: 「不会直接照搬」「k8s/deployment.yaml 不存在」「这是禁用清单里的反例」），
#: 而不是模型真的把幻觉当成事实输出。宁可稍宽——真正的肯定式编造（「项目里有
#: X，在第 N 行」）句子里不含任何否定线索，仍会被判违规。
_NEGATION_CUE_RE = re.compile(
    r"不|没|无|非|别|未|勿|拒绝|禁止|禁|错|误|反例|幻觉|幻|编造|捏造|虚假|并不|压根|而非"
)


def _all_mentions_negated(term, text):
    """`term` 出现在 `text` 里的每一处都落在带否定线索的子句中 → 返回 True（可豁免）。

    只要有一处命中的子句**不含**任何否定线索，就说明它是肯定式陈述，返回 False。
    """
    saw_hit = False
    for clause in _CLAUSE_SPLIT_RE.split(text):
        if term in clause:
            saw_hit = True
            if not _NEGATION_CUE_RE.search(clause):
                return False
    # 没有任何子句命中（term 可能跨标点）：保守起见视为未豁免。
    return saw_hit


def score_record(rec, llm=None, judge=False):
    """对单条结果打分。返回 {id, scored, checks[], passed, score}。"""
    exp = rec.get("expect") or {}
    final = _final(rec)
    actions = _actions(rec)
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    if rec.get("error"):
        add("no_error", False, str(rec.get("error"))[:120])
    elif exp.get("no_error"):
        add("no_error", True)

    for s in exp.get("must_include") or []:
        add(f"must_include:{s}", s in final, "" if s in final else "未在最终答案中出现")
    any_of = exp.get("any_of") or []
    if any_of:
        hit = [s for s in any_of if s in final]
        add("any_of", bool(hit), f"命中 {hit[:3]}" if hit else f"均未出现：{any_of[:3]}")
    for s in exp.get("must_not_include") or []:
        present = s in final
        if present and _all_mentions_negated(s, final):
            add(f"must_not_include:{s}", True, "仅出现于否定/辟谣语境，豁免")
        else:
            add(f"must_not_include:{s}", not present,
                "" if not present else "出现幻觉标记")
    if exp.get("regex"):
        try:
            add("regex", bool(re.search(exp["regex"], final)), exp["regex"])
        except re.error as e:
            add("regex", False, f"正则非法：{e}")
    for tool in exp.get("must_call") or []:
        add(f"must_call:{tool}", _action_called(tool, actions), f"actions={actions[:5]}")
    any_of_call = exp.get("any_of_call") or []
    if any_of_call:
        hit = [t for t in any_of_call if _action_called(t, actions)]
        add("any_of_call", bool(hit),
            f"命中 {hit}" if hit else f"均未调用：{any_of_call}；actions={actions[:5]}")
    if exp.get("min_actions") is not None:
        add("min_actions", len(actions) >= int(exp["min_actions"]),
            f"实际 {len(actions)}")
    if exp.get("max_actions") is not None:
        add("max_actions", len(actions) <= int(exp["max_actions"]),
            f"实际 {len(actions)}")

    # 诚实性元标志判据：只在题目 expect 显式要求时才检查，老题不受影响。
    # degraded / verification_status 由 run_golden 从 final 事件落进结果行。
    if "degraded" in exp:
        want = bool(exp["degraded"])
        got = bool(rec.get("degraded"))
        add("degraded", got == want,
            "" if got == want else f"期望 degraded={want}，实际 {rec.get('degraded')!r}")
    if exp.get("verification_status") is not None:
        want = exp["verification_status"]
        got = rec.get("verification_status")
        add("verification_status", got == want,
            "" if got == want else f"期望 verification_status={want!r}，实际 {got!r}")

    if judge and llm is not None and exp.get("judge_criteria"):
        ok, detail = _llm_judge(llm, rec, exp["judge_criteria"])
        add("llm_judge", ok, detail)

    scored = bool(checks)
    passed = scored and all(c["ok"] for c in checks)
    return {
        "id": rec.get("id"),
        "scored": scored,
        "passed": passed,
        "score": (sum(1 for c in checks if c["ok"]) / len(checks)) if checks else 0.0,
        "checks": checks,
    }


_JUDGE_PROMPT = (
    "你是严格的答案评审。根据下面的评判标准，判断【答案】是否达标。\n"
    "只输出一行 JSON：{\"pass\": true/false, \"reason\": \"不超过40字\"}。\n\n"
    "评判标准：{criteria}\n\n问题：{question}\n\n答案：{answer}\n"
)


def _llm_judge(llm, rec, criteria):
    try:
        out = llm.chat([{
            "role": "user",
            "content": _JUDGE_PROMPT.format(
                criteria=criteria, question=rec.get("question", ""),
                answer=_final(rec)[:2000],
            ),
        }], stream=False, temperature=0.0)
        m = re.search(r"\{.*\}", out or "", re.S)
        obj = json.loads(m.group(0)) if m else {}
        return bool(obj.get("pass")), str(obj.get("reason", ""))[:80]
    except Exception as e:  # noqa: BLE001 —— judge 失败不能拖垮评分
        return False, f"judge 调用失败：{type(e).__name__}"


def load_results(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def score_file(path, llm=None, judge=False):
    """对结果文件整体打分，返回聚合报告。"""
    items = [score_record(r, llm=llm, judge=judge) for r in load_results(path)]
    scored = [i for i in items if i["scored"]]
    passed = [i for i in scored if i["passed"]]
    return {
        "file": path,
        "total": len(items),
        "scored": len(scored),
        "unscored": len(items) - len(scored),
        "passed": len(passed),
        "failed": len(scored) - len(passed),
        "pass_rate": round(len(passed) / len(scored), 4) if scored else 0.0,
        "avg_score": round(sum(i["score"] for i in scored) / len(scored), 4) if scored else 0.0,
        "items": items,
    }


def compare_to_baseline(report, baseline_path):
    """对比 baseline：pass→fail 的题、以及整体 pass_rate 下滑，都算回归。"""
    base = score_file(baseline_path)
    base_map = {i["id"]: i["passed"] for i in base["items"] if i["scored"]}
    regressions, improvements = [], []
    current_ids = {i["id"] for i in report["items"] if i["scored"]}
    missing = sorted(set(base_map) - current_ids)
    for i in report["items"]:
        if not i["scored"] or i["id"] not in base_map:
            continue
        was, now = base_map[i["id"]], i["passed"]
        if was and not now:
            regressions.append(i["id"])
        elif not was and now:
            improvements.append(i["id"])
    rate_drop = round(base["pass_rate"] - report["pass_rate"], 4)
    return {
        "baseline": baseline_path,
        "baseline_pass_rate": base["pass_rate"],
        "current_pass_rate": report["pass_rate"],
        "rate_drop": rate_drop,
        "regressions": regressions,
        "improvements": improvements,
        "missing": missing,
        "regressed": bool(regressions or missing) or rate_drop > 0,
    }


def golden_gate_ok(results_path, baseline_path=None):
    """冻结发布门：结果文件**全部通过**且无 baseline 回退才放行。

    - 无 baseline：要求存在已评分题且 scored 题全部 passed。
    - 有 baseline：在「全部通过」基础上额外要求无 pass→fail 回退、整体 pass_rate 不下滑。
    返回 bool，供冻结发布流水线（game_release_check / CI）直接判定。
    """
    try:
        # The release gate is stricter than the exploratory scorer: malformed,
        # duplicate or unscored records cannot disappear from acceptance evidence.
        with open(results_path, encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        ids = [row.get("id") for row in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            return False
        report = score_file(results_path)
    except Exception:  # noqa: BLE001
        return False
    if report["scored"] == 0:
        return False
    if report["failed"] > 0 or report["unscored"] > 0:
        return False
    if baseline_path:
        try:
            gate = compare_to_baseline(report, baseline_path)
        except Exception:  # noqa: BLE001
            return False
        if gate.get("regressed"):
            return False
    return True


def _print_report(report):
    print(f"结果文件：{report['file']}")
    print(f"  共 {report['total']} 题（已评分 {report['scored']}，无评分标准 {report['unscored']}）")
    print(f"  通过 {report['passed']} / 失败 {report['failed']}  通过率 {report['pass_rate']:.1%}"
          f"  平均得分 {report['avg_score']:.2f}")
    for i in report["items"]:
        if not i["scored"]:
            print(f"  ?  {i['id']}  无评分标准（expect 为空）")
        elif i["passed"]:
            print(f"  ok {i['id']}")
        else:
            bad = [c["name"] + (f" ({c['detail']})" if c["detail"] else "")
                   for c in i["checks"] if not c["ok"]]
            print(f"  FAIL {i['id']}  → {'；'.join(bad)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="黄金题打分 / 回归门")
    ap.add_argument("--results", required=True, help="run_golden.py 产出的 results.jsonl")
    ap.add_argument("--baseline", help="baseline results.jsonl；给出则做回归判定")
    ap.add_argument("--judge", action="store_true", help="叠加 LLM-judge（需 --provider）")
    ap.add_argument("--provider", default="", help="judge 用的 provider（如 ollama）")
    ap.add_argument("--model", default="", help="judge 用的模型")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON（CI 用）")
    args = ap.parse_args(argv)

    llm = None
    if args.judge:
        from llm import LLMClient
        llm = LLMClient(provider=args.provider or None, model=args.model or None)

    report = score_file(args.results, llm=llm, judge=args.judge)
    gate = compare_to_baseline(report, args.baseline) if args.baseline else None

    if args.json:
        print(json.dumps({"report": report, "gate": gate}, ensure_ascii=False, indent=1))
    else:
        _print_report(report)
        if gate:
            print(f"\n回归门：baseline {gate['baseline_pass_rate']:.1%} → 当前 {gate['current_pass_rate']:.1%}"
                  f"（下滑 {gate['rate_drop']:.1%}）")
            if gate["regressions"]:
                print(f"  回退的题：{', '.join(gate['regressions'])}")
            if gate["improvements"]:
                print(f"  改善的题：{', '.join(gate['improvements'])}")
            print("  判定：" + ("REGRESSED（存在回退）" if gate["regressed"] else "PASS"))

    return 1 if (gate and gate["regressed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
