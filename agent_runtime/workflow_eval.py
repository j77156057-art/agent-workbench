"""Deterministic evaluator and regression gate for game workflow runs.

The local evaluator is the source of truth.  LangSmith integration consumes
the same JSON-shaped reports and is optional, so offline CI and production
workers keep identical quality semantics.
"""
from __future__ import annotations

import copy
import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping


DEFAULT_DATASET_NAME = "docmind-game-workflows"

# Synthetic, stable examples.  They contain no repository paths, credentials,
# user conversations, or expected source code; only workflow categories and
# quality standards are shared with a remote evaluator.
GAME_WORKFLOW_DATASET: tuple[dict[str, Any], ...] = (
    {
        "id": "game-prototype-2d",
        "inputs": {"scenario": "2d_prototype", "engine": "godot",
                   "goal_class": "playable_prototype"},
        "outputs": {"required_checks": ["completed", "review_ok", "no_failed_tasks",
                                         "no_in_doubt_tool"],
                    "max_steps": 24, "max_replans": 2},
    },
    {
        "id": "game-playtest-repair",
        "inputs": {"scenario": "playtest_repair", "engine": "generic",
                   "goal_class": "verified_iteration"},
        "outputs": {"required_checks": ["completed", "review_ok", "no_failed_tasks",
                                         "steps_within_limit", "replans_within_limit"],
                    "max_steps": 24, "max_replans": 2},
    },
    {
        "id": "game-research-gated",
        "inputs": {"scenario": "research_gated", "engine": "generic",
                   "goal_class": "source_aware_plan"},
        "outputs": {"required_checks": ["completed", "review_ok", "no_failed_tasks",
                                         "no_in_doubt_tool"],
                    "max_steps": 24, "max_replans": 2},
    },
)


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:240]}


def _is_completed(status: Any) -> bool:
    """「工作流完成」含「完成但未做独立验证」。

    实测：只读工作流在独立复核为 `unverified` 时，后端三值结局给的是
    `completed_unverified`（见 `game_workflow.settle_status`）。它**是**完成的
    ——四任务全 ok、十项检查全 True、引用不过 0 条。若这里只认 `"completed"`，
    「工作流完成」这项检查会恒为假，而这块评估此前因为同样的字面量判断
    **从来没被启动过**（它要求 `status == "completed"` 才跑）。

    注意本模块**不 import game_workflow**：那会造成循环导入（game_workflow
    会 import 本模块）。这里独立判断，与 `SUCCESS_STATUSES` 保持同一取值。
    """
    return str(status or "") in ("completed", "completed_unverified")


def evaluate_workflow(state: Mapping[str, Any] | None) -> dict[str, Any]:
    state = dict(state or {})
    policy = dict(state.get("policy") or {})
    report = dict(state.get("results") or {})
    results = dict(report.get("results") or {})
    review = dict(state.get("review") or {})
    checks = [
        _check("completed", _is_completed(state.get("status")), state.get("status", "")),
        _check("review_ok", review.get("ok") is True, str(review.get("checks") or "")),
        _check("no_failed_tasks", not any(
            isinstance(item, Mapping) and item.get("status") in {"failed", "blocked"}
            for item in results.values())),
        _check("steps_within_limit", int(state.get("steps") or 0) <= int(
            policy.get("max_steps") or 24), "%s/%s" % (state.get("steps", 0), policy.get("max_steps", 24))),
        _check("replans_within_limit", int(state.get("replans") or 0) <= int(
            policy.get("max_replans") or 2), "%s/%s" % (state.get("replans", 0), policy.get("max_replans", 2))),
        _check("no_in_doubt_tool", not any(
            isinstance(item, Mapping) and (item.get("error_kind") == "idempotency_in_doubt" or
                                            item.get("error") == "idempotency_in_doubt")
            for item in results.values())),
    ]
    passed = bool(checks) and all(item["ok"] for item in checks)
    return {
        "workflow_id": state.get("workflow_id"),
        "passed": passed,
        "score": round(sum(item["ok"] for item in checks) / len(checks), 4),
        "checks": checks,
        "task_count": len(results),
        "replans": int(state.get("replans") or 0),
        "steps": int(state.get("steps") or 0),
        "metrics": {
            "completed": bool(_is_completed(state.get("status"))),
            "review_ok": bool(review.get("ok") is True),
            "failed_tasks": sum(
                1 for item in results.values()
                if isinstance(item, Mapping) and item.get("status") == "failed"),
            "blocked_tasks": sum(
                1 for item in results.values()
                if isinstance(item, Mapping) and item.get("status") == "blocked"),
            "idempotency_in_doubt": sum(
                1 for item in results.values()
                if isinstance(item, Mapping) and (
                    item.get("error_kind") == "idempotency_in_doubt" or
                    item.get("error") == "idempotency_in_doubt")),
        },
    }


def dataset_cases() -> list[dict[str, Any]]:
    """Return isolated copies suitable for local or LangSmith dataset upload."""
    return copy.deepcopy(list(GAME_WORKFLOW_DATASET))


def evaluate_case(state: Mapping[str, Any] | None,
                  case: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate a workflow and enforce a dataset case's quality contract."""
    evaluation = evaluate_workflow(state)
    case = dict(case or {})
    expected = dict(case.get("outputs") or {})
    checks = list(evaluation.get("checks") or [])
    by_name = {str(item.get("name")): bool(item.get("ok"))
               for item in checks if isinstance(item, Mapping)}
    required = [str(name) for name in expected.get("required_checks") or by_name]
    contract_checks = [
        _check("dataset:%s" % name, by_name.get(name, False), "required by %s" % (case.get("id") or "case"))
        for name in required
    ]
    if expected.get("max_steps") is not None:
        contract_checks.append(_check(
            "dataset:max_steps", int(evaluation.get("steps") or 0) <= int(expected["max_steps"]),
            "%s/%s" % (evaluation.get("steps", 0), expected["max_steps"])))
    if expected.get("max_replans") is not None:
        contract_checks.append(_check(
            "dataset:max_replans", int(evaluation.get("replans") or 0) <= int(expected["max_replans"]),
            "%s/%s" % (evaluation.get("replans", 0), expected["max_replans"])))
    passed = bool(contract_checks) and all(item["ok"] for item in contract_checks)
    return {
        "case_id": case.get("id"),
        "passed": passed,
        "score": round(sum(item["ok"] for item in contract_checks) / len(contract_checks), 4),
        "checks": contract_checks,
        "workflow": evaluation,
    }


def aggregate_evaluations(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate case reports into a stable pass-rate report."""
    rows = [dict(item) for item in items if isinstance(item, Mapping)]
    passed = sum(1 for item in rows if item.get("passed"))
    return {
        "total": len(rows),
        "scored": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "pass_rate": round(passed / len(rows), 4) if rows else 0.0,
        "items": rows,
    }


def compare_evaluation(current: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two evaluator outputs; any pass-to-fail check is a regression."""
    current_checks = {str(item.get("name")): bool(item.get("ok"))
                      for item in current.get("checks", []) if isinstance(item, Mapping)}
    baseline_checks = {str(item.get("name")): bool(item.get("ok"))
                       for item in baseline.get("checks", []) if isinstance(item, Mapping)}
    regressions = sorted(name for name, ok in baseline_checks.items()
                         if ok and not current_checks.get(name, False))
    missing = sorted(set(baseline_checks) - set(current_checks))
    return {"regressed": bool(regressions or missing), "regressions": regressions,
            "missing": missing, "baseline_passed": bool(baseline.get("passed")),
            "current_passed": bool(current.get("passed"))}


def regression_gate(current: Mapping[str, Any],
                    baseline: Mapping[str, Any] | None = None,
                    *, min_pass_rate: float | None = None) -> dict[str, Any]:
    """Apply case-level and aggregate pass-rate regression checks."""
    current = dict(current or {})
    baseline = dict(baseline or {})
    threshold = float(min_pass_rate if min_pass_rate is not None else
                      os.getenv("DOCMIND_WORKFLOW_MIN_PASS_RATE", "1.0"))
    threshold = max(0.0, min(1.0, threshold))
    current_rate = float(current.get("pass_rate") or 0.0)
    baseline_rate = float(baseline.get("pass_rate") or 0.0) if baseline else None
    current_items = {str(item.get("case_id")): item for item in current.get("items", [])
                     if isinstance(item, Mapping)}
    baseline_items = {str(item.get("case_id")): item for item in baseline.get("items", [])
                      if isinstance(item, Mapping)}
    regressions: list[str] = []
    for case_id, old in baseline_items.items():
        new = current_items.get(case_id)
        if bool(old.get("passed")) and not bool((new or {}).get("passed")):
            regressions.append(case_id)
    missing = sorted(set(baseline_items) - set(current_items))
    rate_drop = (round(baseline_rate - current_rate, 4)
                 if baseline_rate is not None else 0.0)
    return {
        "regressed": bool(regressions or missing or current_rate < threshold or
                          (baseline_rate is not None and current_rate < baseline_rate)),
        "baseline_pass_rate": baseline_rate,
        "current_pass_rate": current_rate,
        "min_pass_rate": threshold,
        "rate_drop": rate_drop,
        "regressions": sorted(regressions),
        "missing": missing,
        "improvements": sorted(
            case_id for case_id, new in current_items.items()
            if bool(new.get("passed")) and not bool((baseline_items.get(case_id) or {}).get("passed"))),
    }


def load_baseline(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a persisted aggregate report without accepting malformed JSON."""
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise ValueError("workflow baseline must be an aggregate report")
    return value


def save_baseline(report: Mapping[str, Any], path: str | os.PathLike[str]) -> None:
    """Persist a stable, human-readable aggregate report for CI/release gates."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


def _self_check_report() -> dict[str, Any]:
    """Build a deterministic healthy report for the checked-in evaluator baseline."""
    rows = []
    for case in dataset_cases():
        state = {
            "workflow_id": "baseline:%s" % case.get("id"),
            "status": "completed",
            "steps": 2,
            "replans": 0,
            "policy": {"max_steps": 24, "max_replans": 2},
            "results": {"results": {"prototype": {"status": "ok"}}},
            "review": {"ok": True},
        }
        rows.append(evaluate_case(state, case))
    return aggregate_evaluations(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Workbench game workflow regression gate")
    parser.add_argument("--current", help="current aggregate JSON report")
    parser.add_argument("--baseline", default=".github/workflow-eval-baseline.json")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        current = (_self_check_report() if args.self_check else
                   load_baseline(args.current) if args.current else None)
        if current is None:
            parser.error("use --self-check or --current")
        if args.write_baseline:
            save_baseline(current, args.baseline)
            gate = {"regressed": False, "written": args.baseline,
                    "current_pass_rate": current.get("pass_rate")}
        else:
            baseline = load_baseline(args.baseline)
            gate = regression_gate(current, baseline)
        print(json.dumps(gate, ensure_ascii=False, indent=2) if args.as_json else gate)
        return 1 if gate.get("regressed") else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("workflow regression gate failed: %s" % exc, file=sys.stderr)
        return 2


__all__ = [
    "DEFAULT_DATASET_NAME", "GAME_WORKFLOW_DATASET", "dataset_cases",
    "evaluate_workflow", "evaluate_case", "aggregate_evaluations",
    "compare_evaluation", "regression_gate", "load_baseline", "save_baseline",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
