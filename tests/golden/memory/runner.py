# -*- coding: utf-8 -*-
"""记忆/压缩黄金回归套件运行器（数据驱动、确定性、离线）。

用法::

    python tests/golden/memory/runner.py --all
    python tests/golden/memory/runner.py --case pinned_not_in_compact_prompt
    python tests/golden/memory/runner.py --update-golden --yes
    python tests/golden/memory/runner.py --all --live     # 只额外跑 live 组

设计约束（见同目录 README.md）：
- 每个用例在**独立临时目录**里由子进程执行，跑完即删；绝不碰仓库内真实记忆。
- 默认套件只用 StubLLM（固定响应），不联网、不依赖额度。
- 用例数据全在 cases/*.json，期望快照在 expected/*.json。
- 退出码：0 全过 / 1 有失败 / 2 配置或快照缺失。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
EXPECTED_DIR = HERE / "expected"
REPO = HERE.parents[2]

TOTAL_BUDGET_SECONDS = 60.0
PER_CASE_TIMEOUT_SECONDS = 20.0

_PASS, _FAIL, _SKIP, _MISSING = "PASS", "FAIL", "SKIP", "MISSING"


# ---------------------------------------------------------------------------
# 父进程：调度、超时、快照比对、输出表
# ---------------------------------------------------------------------------
def _case_files():
    return sorted(CASES_DIR.rglob("*.json"))


def _is_live(case_path: Path, payload: dict) -> bool:
    return bool(payload.get("live")) or "live" in case_path.parts


def _load_case(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _first_diff(expected: str, actual: str, window: int = 60) -> str:
    limit = min(len(expected), len(actual))
    index = 0
    while index < limit and expected[index] == actual[index]:
        index += 1
    left = max(0, index - window)
    right = index + window
    exp_slice = expected[left:right].replace("\n", "\\n")
    act_slice = actual[left:right].replace("\n", "\\n")
    return ("首处差异在第 %d 字符。期望: %r ... 实际: %r"
            % (index, exp_slice, act_slice))


def _child_env(workdir: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "DOCMIND_STATE_ROOT": str(workdir / "state"),
        "DOCMIND_MEMORY_DIR": str(workdir / "memory"),
        "DOCMIND_MEMORY_DB": str(workdir / "memory.sqlite3"),
        "DOCMIND_SESSIONS_DIR": str(workdir / "sessions"),
        "DOCMIND_TURN_TRACE_DIR": str(workdir / "traces"),
        "DOCMIND_MEMORY_EMBED": "0",
        "DOCMIND_MEMORY_ENHANCED": "0",
        "DOCMIND_MEMORY_DISTILL": "0",
        "DOCMIND_MEMORY_LEARN": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def _run_child(case_path: Path, workdir: Path, timeout: float) -> dict:
    result_path = workdir / "result.json"
    workdir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()),
               "--_child", str(case_path), "--workdir", str(workdir),
               "--result", str(result_path)]
    env = _child_env(workdir)
    try:
        completed = subprocess.run(
            command, cwd=str(REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"name": case_path.stem, "error":
                "用例超时（超过 %.1f 秒），这是本批最慢的一条。" % timeout}
    except Exception as exc:  # noqa: BLE001
        return {"name": case_path.stem, "error":
                "子进程启动失败：%s: %s" % (type(exc).__name__, exc)}
    if result_path.exists():
        try:
            with open(result_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            return {"name": case_path.stem, "error":
                    "结果文件损坏：%s: %s" % (type(exc).__name__, exc)}
    tail = (completed.stdout or "")[-1200:] + (completed.stderr or "")[-1800:]
    return {"name": case_path.stem, "error":
            "子进程未写出结果（退出码 %s）。尾部输出：%s"
            % (completed.returncode, tail.strip())}


def _print_table(rows):
    print("%-34s %-8s %8s" % ("用例", "结果", "耗时"))
    print("-" * 56)
    for row in rows:
        print("%-34s %-8s %7.2fs" % (row["name"], row["status"], row["seconds"]))


def _collect_failure(row):
    if row.get("error"):
        text = row["error"]
        if row.get("traceback_tail"):
            text += "\n  traceback 尾部:\n" + "\n".join(
                "  " + line for line in str(row["traceback_tail"]).splitlines()[-6:])
        return text
    failures = [item for item in row.get("assertion_results", []) if not item.get("ok")]
    if failures:
        first = failures[0]
        extra = "" if len(failures) == 1 else "（另有 %d 条断言失败）" % (len(failures) - 1)
        return "%s%s" % (first.get("detail") or "断言失败", extra)
    if row.get("diff"):
        return row["diff"]
    return ""


def main(argv=None):
    parser = argparse.ArgumentParser(description="记忆/压缩黄金回归套件")
    parser.add_argument("--all", action="store_true", help="运行全部离线用例")
    parser.add_argument("--case", default="", help="只运行指定用例（文件名或 case 名）")
    parser.add_argument("--update-golden", action="store_true",
                        help="用当前实际输出覆盖黄金快照（危险操作）")
    parser.add_argument("--yes", action="store_true",
                        help="与 --update-golden 搭配，显式确认覆盖")
    parser.add_argument("--live", action="store_true",
                        help="额外运行 live 组（会调用真实模型）")
    parser.add_argument("--timeout", type=float, default=PER_CASE_TIMEOUT_SECONDS,
                        help="单用例超时秒数（默认 20）")
    args = parser.parse_args(argv)

    if args.update_golden:
        print("你在覆盖既有期望：--update-golden 会把当前输出写成新的黄金快照。")
        if not args.yes:
            print("拒绝执行：必须显式加 --yes 才会覆盖（防止顺手把失败刷成通过）。")
            return 2

    if not args.all and not args.case:
        parser.print_help()
        return 2

    files = _case_files()
    if args.case:
        wanted = str(args.case).strip()
        files = [path for path in files
                 if path.stem == wanted or path.name == wanted
                 or path.name == wanted + ".json"]
        if not files:
            print("找不到用例：%s" % wanted)
            return 2

    rows = []
    details = []
    started = time.monotonic()
    config_missing = False

    for path in files:
        payload = _load_case(path)
        name = path.stem
        if _is_live(path, payload) and not args.live:
            rows.append({"name": name, "status": _SKIP, "seconds": 0.0})
            continue
        elapsed = time.monotonic() - started
        remaining = TOTAL_BUDGET_SECONDS - elapsed
        if remaining <= 0:
            rows.append({"name": name, "status": _FAIL, "seconds": 0.0,
                         "error": "全量已超过 %.0f 秒预算，该用例未执行（慢用例是前面的条目）。"
                                  % TOTAL_BUDGET_SECONDS})
            continue
        timeout = max(1.0, min(float(args.timeout), remaining))
        workdir = Path(tempfile.mkdtemp(prefix="dm_golden_%s_" % name))
        begin = time.monotonic()
        try:
            result = _run_child(path, workdir, timeout)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        seconds = time.monotonic() - begin
        row = {"name": name, "status": _PASS, "seconds": seconds,
               "assertion_results": [], "error": result.get("error"),
               "traceback_tail": result.get("traceback_tail")}
        if result.get("error"):
            row["status"] = _FAIL
        else:
            row["assertion_results"] = result.get("assertion_results", [])
            if any(not item.get("ok") for item in row["assertion_results"]):
                row["status"] = _FAIL
            if _is_live(path, payload):
                pass  # live 用例没有黄金快照，只看断言。
            else:
                expected_path = EXPECTED_DIR / ("%s.json" % name)
                observations = result.get("observations", {})
                if not expected_path.exists():
                    if args.update_golden and row["status"] == _PASS:
                        expected_text = _canonical(observations)
                        expected_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(expected_path, "w", encoding="utf-8", newline="\n") as handle:
                            handle.write(expected_text)
                        print("生成快照: %s" % expected_path)
                    else:
                        if row["status"] == _PASS:
                            row["status"] = _MISSING
                            config_missing = True
                        row["diff"] = "缺少快照 %s（先评审后运行 --update-golden --yes）" % expected_path
                else:
                    with open(expected_path, "r", encoding="utf-8") as handle:
                        expected = handle.read()
                    actual = _canonical(observations)
                    if expected != actual:
                        if args.update_golden and row["status"] == _PASS:
                            with open(expected_path, "w", encoding="utf-8", newline="\n") as handle:
                                handle.write(actual)
                            print("覆盖既有期望: %s" % expected_path)
                        else:
                            row["status"] = _FAIL
                            row["diff"] = _first_diff(expected, actual)
        rows.append(row)
        failure = _collect_failure(row)
        if failure:
            details.append((name, failure))

    _print_table(rows)
    if details:
        print("\n失败详情：")
        for name, failure in details:
            print("- %s: %s" % (name, failure))
            print("  （失败详情已带用例名；异常路径会打印完整 traceback 尾部。）")

    failed = any(row["status"] in (_FAIL, _MISSING) for row in rows)
    if failed:
        if config_missing and not any(row["status"] == _FAIL for row in rows):
            return 2
        return 1
    return 0


# ---------------------------------------------------------------------------
# 子进程：隔离环境、桩 LLM、各场景
# ---------------------------------------------------------------------------
def _install_isolated_env(workdir: Path):
    """子进程内的最后一道保险：即使父进程 env 漏项，也只写临时目录。"""
    root = workdir / "state"
    os.makedirs(root, exist_ok=True)
    os.environ["DOCMIND_STATE_ROOT"] = str(root)
    os.environ["DOCMIND_MEMORY_DIR"] = str(workdir / "memory")
    os.environ["DOCMIND_MEMORY_DB"] = str(workdir / "memory.sqlite3")
    os.environ["DOCMIND_SESSIONS_DIR"] = str(workdir / "sessions")
    os.environ["DOCMIND_TURN_TRACE_DIR"] = str(workdir / "traces")


class StubLLM:
    """固定响应桩：不联网、不依赖额度；同时记录真实送入的提示词原文。"""

    def __init__(self, replies=None, default_reply="", prompt_budget=0):
        self.replies = list(replies or [])
        self.default_reply = str(default_reply or "")
        self.prompts = []
        self.calls = 0
        self.prompt_budget = int(prompt_budget or 0)

    def chat(self, messages, stream=False, temperature=0.2, **kwargs):
        self.calls += 1
        prompt = "\n".join(str(item.get("content") or "")
                           for item in (messages or []) if isinstance(item, dict))
        self.prompts.append(prompt)
        if self.replies:
            return self.replies.pop(0)
        return self.default_reply


def _turn(value) -> dict:
    if not isinstance(value, dict):
        value = {"user": str(value or "")}
    return {"user": value.get("user", ""), "assistant": value.get("assistant", "")}


def _fmt(value, index):
    if isinstance(value, str):
        return value.replace("{i}", str(index))
    if isinstance(value, list):
        return [_fmt(item, index) for item in value]
    if isinstance(value, dict):
        return {key: _fmt(item, index) for key, item in value.items()}
    return value


def _get(observations, path):
    current = observations
    for part in str(path).split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError("观测字段不存在：%s（在 %s 处断了）" % (path, part))
        current = current[part]
    return current


def _summary_limit(sessions, budget):
    return int(sessions.summary_char_budget(int(budget or 11000)))


def _markers_intact(text):
    """检测被切开/截断的分节标记。

    不能只看“前缀是否存在”：如果标记被切成 `【近期进`，前缀 `【近期进展`
    本身也找不到，旧实现会误判为完整。这里改成**最宽开口 + 完整标记**计数：
    任何 `【固定` / `【近期` 开头都必须恰好对应一个完整标记；截断到 `【近期进`
    （连完整词都不是）时开口仍是 1、完整仍是 0，立刻判为损坏。
    """
    text = str(text or "")
    pinned_open = text.count("【固定")
    pinned_full = (text.count("【固定事实】")
                   + text.count("【固定事实｜pinned_from_legacy_head】"))
    rolling_open = text.count("【近期")
    rolling_full = text.count("【近期进展】")
    return pinned_open == pinned_full and rolling_open == rolling_full


def _markers_position_ok(text, require_both=False):
    """分节标记必须各自独占行首，且固定事实在近期进展之前。"""
    text = str(text or "")

    def at_line_start(position):
        return position == 0 or text[position - 1] == "\n"

    pinned = text.find("【固定事实】")
    rolling = text.find("【近期进展】")
    if pinned < 0 or not at_line_start(pinned):
        return False
    if rolling < 0:
        return not require_both
    return rolling > pinned and at_line_start(rolling)


def _markers_ok(text, require_pinned=True):
    """分节标记的唯一性/完整性检查，正例与反例共用同一套判定。"""
    text = str(text or "")
    pinned_count = (text.count("【固定事实】")
                    + text.count("【固定事实｜pinned_from_legacy_head】"))
    rolling_count = text.count("【近期进展】")
    if not _markers_intact(text) or pinned_count > 1 or rolling_count > 1:
        return False
    if require_pinned and "【固定事实】" not in text:
        return False
    return True


def _check_one(observations, assertion):
    kind = assertion.get("type")
    field = assertion.get("field")
    if kind in ("contains", "not_contains"):
        value = str(_get(observations, field))
        needle = str(assertion.get("value") or "")
        ok = (needle in value) if kind == "contains" else (needle not in value)
        return ok, "%s %s：%r %s 文本" % (
            field, "包含" if kind == "contains" else "不包含", needle,
            "在" if ok else "不在")
    if kind in ("is_true", "is_false"):
        value = bool(_get(observations, field))
        ok = value is (kind == "is_true")
        return ok, "%s 期望 %s，实际 %s" % (field, kind == "is_true", value)
    if kind == "equals":
        value = _get(observations, field)
        expected = assertion.get("value")
        ok = value == expected
        return ok, "%s 期望 %r，实际 %r" % (field, expected, value)
    if kind == "equals_field":
        left = _get(observations, field)
        right = _get(observations, assertion.get("other"))
        ok = left == right
        return ok, "%s 应与 %s 相等；实际 %r vs %r" % (
            field, assertion.get("other"), left, right)
    if kind in ("int_le", "int_ge", "int_eq"):
        value = int(_get(observations, field))
        limit = assertion.get("limit")
        if assertion.get("limit_field"):
            limit = _get(observations, assertion["limit_field"])
        limit = int(limit)
        if kind == "int_le":
            ok = value <= limit
        elif kind == "int_ge":
            ok = value >= limit
        else:
            ok = value == limit
        return ok, "%s 期望 %s %d，实际 %d" % (field, kind, limit, value)
    if kind == "starts_with":
        value = str(_get(observations, field))
        prefix = str(assertion.get("value") or "")
        ok = value.startswith(prefix)
        return ok, "%s 期望以 %r 开头，实际开头 %r" % (field, prefix, value[:60])
    if kind == "regex":
        value = str(_get(observations, field))
        pattern = str(assertion.get("pattern") or "")
        ok = re.search(pattern, value) is not None
        return ok, "%s 匹配 /%s/：%s" % (field, pattern, ok)
    raise ValueError("未知断言类型：%s" % kind)


def _eval_assertions(observations, assertions):
    results = []
    for assertion in assertions or []:
        try:
            ok, detail = _check_one(observations, assertion)
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = "断言执行异常：%s: %s" % (type(exc).__name__, exc)
        results.append({"ok": bool(ok), "type": assertion.get("type"),
                        "field": assertion.get("field"), "detail": detail})
    return results


def _seed_session(sessions, session_id, seed):
    turns = [_turn(item) for item in seed.get("turns") or []]
    sessions.save(session_id, turns, seed.get("summary") or "")
    return turns


def _cycle(sessions, session_id, current, spec, llm):
    turns = list(current) + [_turn(item) for item in spec.get("add") or []]
    kept, summary = sessions.maybe_compact(
        session_id, turns, llm=llm,
        trigger_tokens=spec.get("trigger_tokens", 1),
        keep_tokens=spec.get("keep_tokens", 1),
        prompt_budget=spec.get("prompt_budget"),
    )
    sessions.save(session_id, kept, summary)
    return kept, summary


def _repeat_cycles(sessions, session_id, current, summary, group, llm):
    cycles = 0
    times = int(group.get("times") or 0)
    for index in range(1, times + 1):
        spec = _fmt(group, index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)
        cycles += 1
    return current, summary, cycles


# ---- 16 个场景 -------------------------------------------------------------
def _s_hard_constraints_survive(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    seed = case["seed"]
    current = _seed_session(sessions, session_id, seed)
    first_reply = case["first_cycle"].get("llm_reply") or ""
    repeat = case.get("repeat") or {}
    llm = StubLLM([first_reply], repeat.get("llm_reply") or "本轮进展摘要。")
    llm.prompt_budget = int((case.get("budget") or {}).get("prompt_budget") or 0)
    current, summary = _cycle(sessions, session_id, current,
                              case["first_cycle"], llm)
    cycles = 1
    current, summary, extra = _repeat_cycles(
        sessions, session_id, current, summary, repeat, llm)
    cycles += extra
    budget = int((case.get("budget") or {}).get("prompt_budget") or 11000)
    parsed = sessions._parse_summary(summary)
    return {
        "final_summary": summary,
        "cycles": cycles,
        "final_turns": len(current),
        "summary_chars": len(summary),
        "summary_limit": _summary_limit(sessions, budget),
        "pinned_has_code": (case.get("code_token") or "") in parsed["pinned"],
        "pinned_has_api": (case.get("api_token") or "") in parsed["pinned"],
        "pinned_text": parsed["pinned"],
    }


def _s_pinned_not_in_compact_prompt(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    llm = StubLLM(case.get("llm_replies") or [], case.get("llm_default") or "进展摘要。")
    llm.prompt_budget = int((case.get("cycle") or {}).get("prompt_budget") or 0)
    current, summary = _cycle(sessions, session_id, current, case["cycle"], llm)
    marker = case.get("rolling_prompt_marker") or "【近期进展】压缩"
    pinned_text = case.get("pinned_text") or ""
    rolling_token = case.get("rolling_token") or ""
    rolling_prompts = [prompt for prompt in llm.prompts if marker in prompt]
    return {
        "final_summary": summary,
        "rolling_prompt_count": len(rolling_prompts),
        "pinned_in_rolling_prompt": any(pinned_text in prompt for prompt in rolling_prompts),
        "rolling_token_in_all_prompts": bool(rolling_prompts) and all(
            rolling_token in prompt for prompt in rolling_prompts),
        "llm_calls": llm.calls,
    }


def _s_rolling_evict(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    current, summary = _cycle(sessions, session_id, current, case["cycle"], None)
    parsed = sessions._parse_summary(summary)
    match = re.search(r"（最旧进展 \d+ 字已省略）", summary)
    return {
        "final_summary": summary,
        "final_summary_chars": len(summary),
        "has_oldest": (case.get("oldest_token") or "") in summary,
        "has_newest": (case.get("newest_token") or "") in summary,
        "evict_marker_found": bool(match),
        "evict_marker": match.group(0) if match else "",
        "rolling_text": parsed["rolling"],
    }


def _s_pinned_full_evict(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    llm = StubLLM([case["cycle"].get("llm_reply") or ""], case.get("llm_default") or "")
    llm.prompt_budget = int(case["cycle"].get("prompt_budget") or 0)
    current, summary = _cycle(sessions, session_id, current, case["cycle"], llm)
    parsed = sessions._parse_summary(summary)
    return {
        "final_summary": summary,
        "summary_chars": len(summary),
        "pinned_text": parsed["pinned"],
        "rolling_text": parsed["rolling"],
        "full_note_count": summary.count("（固定事实前缀已满）"),
        "pinned_has_new": (case.get("new_hard_token") or "") in parsed["pinned"],
        "rolling_has_new": (case.get("new_hard_token") or "") in parsed["rolling"],
        "rolling_has_evicted": (case.get("evicted_token") or "") in parsed["rolling"],
        "pinned_has_evicted": (case.get("evicted_token") or "") in parsed["pinned"],
    }


def _s_hard_absorbed(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    llm = StubLLM([case["cycle"].get("llm_reply") or ""], case.get("llm_default") or "")
    llm.prompt_budget = int(case["cycle"].get("prompt_budget") or 0)
    current, summary = _cycle(sessions, session_id, current, case["cycle"], llm)
    parsed = sessions._parse_summary(summary)
    return {
        "final_summary": summary,
        "final_summary_chars": len(summary),
        "pinned_text": parsed["pinned"],
        "pinned_has_new": (case.get("new_hard_token") or "") in parsed["pinned"],
        "full_note_present": "（固定事实前缀已满）" in summary,
    }


def _s_cap_extreme(case, modules):
    sessions = modules["sessions"]
    limit = int(case.get("limit") or 256)
    # 结构性改造 4：cap_summary_head_tail 不得接收含 pinned 的整段；
    # 注入侧走 inject_summary_text（内部先拆 pinned，只压 rolling）。
    capped = sessions.inject_summary_text(case["summary"], limit)
    pinned_count = capped.count("【固定事实】") + capped.count(
        "【固定事实｜pinned_from_legacy_head】")
    rolling_count = capped.count("【近期进展】")
    derived = case.get("derived_budget") or {}
    derived_limit = (int(sessions.summary_char_budget(int(derived["prompt_tokens"])))
                     if derived else 0)
    return {
        "capped": capped,
        "capped_chars": len(capped),
        "limit": limit,
        "derived_limit": derived_limit,
        "pinned_header_count": pinned_count,
        "rolling_header_count": rolling_count,
        "markers_unique": pinned_count <= 1 and rolling_count <= 1,
        "markers_intact": _markers_intact(capped),
        "markers_position_ok": _markers_position_ok(capped),
        "markers_ok": _markers_ok(capped),
        "pinned_header_present": "【固定事实】" in capped,
    }


def _s_idempotent(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    llm = StubLLM([case["cycle"].get("llm_reply") or ""], case.get("llm_default") or "")
    llm.prompt_budget = int(case["cycle"].get("prompt_budget") or 0)
    first_turns, first_summary = _cycle(
        sessions, session_id, current, case["cycle"], llm)
    calls_before = llm.calls
    second_turns, second_summary = sessions.maybe_compact(
        session_id, first_turns, llm=llm,
        trigger_tokens=case["cycle"].get("trigger_tokens", 1),
        keep_tokens=case["cycle"].get("keep_tokens", 1),
        prompt_budget=case["cycle"].get("prompt_budget"),
    )
    return {
        "first_summary": first_summary,
        "second_summary": second_summary,
        "first_turns": len(first_turns),
        "second_turns": len(second_turns),
        "second_llm_calls": llm.calls - calls_before,
        "equal": first_summary == second_summary and first_turns == second_turns,
    }


def _s_legacy_migration(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    seed = case["seed"]
    legacy = seed.get("summary") or ""

    def run_once(suffix):
        # 新不变量下 save 不再允许覆盖/缩短 pinned；两次运行必须各用全新会话，
        # 否则第二次的 seed 会被第一次的 pinned 合并保护挡回，比较的是累积态。
        run_id = "%s-%s" % (session_id, suffix)
        _seed_session(sessions, run_id, seed)
        llm = StubLLM(case.get("llm_replies") or [], case.get("llm_default") or "")
        llm.prompt_budget = int(case["cycle"].get("prompt_budget") or 0)
        current, summary = _cycle(sessions, run_id,
                                  [_turn(item) for item in seed.get("turns") or []],
                                  case["cycle"], llm)
        return summary

    first = run_once("run1")
    second = run_once("run2")
    parsed = sessions._parse_summary(first)
    pinned = parsed["pinned"]
    return {
        "run1_summary": first,
        "run2_summary": second,
        "two_runs_equal": first == second,
        "provenance_present": parsed["provenance"] == "pinned_from_legacy_head",
        "legacy_head_verbatim": bool(pinned) and legacy.strip().startswith(pinned),
        "pinned_text": pinned,
        "summary_chars": len(first),
        "summary_limit": _summary_limit(sessions,
                                        case["cycle"].get("prompt_budget")),
    }


def _s_write_inject(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    disk_summary = case["summary"]
    sessions.save(session_id, [], disk_summary)
    import agent as agent_module
    stub = StubLLM([], "", case.get("prompt_budget") or 0)
    instance = agent_module.Agent(
        llm=stub, session_id=session_id, project_id=None,
        application_id="developer", tool_registry={})
    messages = instance._build_messages(case.get("question") or "请复述约束。")
    prefix = "【早期对话摘要（更早的轮次已压缩，**可能有损/被截断，不代表已验证事实**）】\n"
    suffix = "\n（对其中精确标识符"
    injected = None
    for message in messages:
        content = str(message.get("content") or "")
        if message.get("role") == "system" and content.startswith(prefix):
            injected = content[len(prefix):]
            if suffix in injected:
                injected = injected.split(suffix, 1)[0]
            break
    if injected is None:
        raise AssertionError("注入侧没有找到早期对话摘要 system 消息")
    return {
        "injected": injected,
        "disk_summary": sessions.summary_text(session_id),
        "byte_identical": injected == sessions.summary_text(session_id),
        "injected_has_pinned": (case.get("pinned_token") or "") in injected,
    }


def _s_budget_matrix(case, modules):
    sessions = modules["sessions"]
    base_id = case["session_id"]
    observations = {}
    for budget in case.get("budgets") or [4000, 11000]:
        budget = int(budget)
        session_id = "%s-%d" % (base_id, budget)
        current = _seed_session(sessions, session_id, case["seed"])
        llm = StubLLM(case.get("llm_replies") or [], case.get("llm_default") or "进展摘要。")
        llm.prompt_budget = budget
        summary = ""
        for spec in case.get("cycles") or []:
            spec = dict(spec)
            spec.setdefault("prompt_budget", budget)
            current, summary = _cycle(sessions, session_id, current, spec, llm)
        parsed = sessions._parse_summary(summary)
        limit = _summary_limit(sessions, budget)
        observations["budget_%d" % budget] = {
            "summary": summary,
            "chars": len(summary),
            "limit": limit,
            "has_code": (case.get("code_token") or "") in summary,
            "has_api": (case.get("api_token") or "") in summary,
            "markers_ok": _markers_ok(summary),
            "pinned_has_code": (case.get("code_token") or "") in parsed["pinned"],
        }
    return observations


def _s_ordinary_fact_not_pinned(case, modules):
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    llm = StubLLM([case["cycle"].get("llm_reply") or ""], case.get("llm_default") or "")
    llm.prompt_budget = int(case["cycle"].get("prompt_budget") or 0)
    current, summary = _cycle(sessions, session_id, current, case["cycle"], llm)
    parsed = sessions._parse_summary(summary)
    fact = case.get("fact") or ""
    return {
        "final_summary": summary,
        "fact_in_pinned": fact in parsed["pinned"],
        "fact_in_rolling": fact in parsed["rolling"],
        "fact_in_summary": fact in summary,
        "pinned_text": parsed["pinned"],
        "rolling_text": parsed["rolling"],
    }


def _s_negative_checkers(case, modules):
    sessions = modules["sessions"]
    limit = int(case.get("limit") or 256)
    payloads = case.get("payloads") or {}
    pinned_token = case.get("pinned_token") or ""

    def len_ok(text):
        return len(str(text)) <= limit

    def markers_ok(text):
        return _markers_ok(text)

    def pinned_ok(text):
        return pinned_token in str(text)

    product_cap = sessions.inject_summary_text(
        case.get("render_summary") or "", limit)
    product_corrupted = product_cap.replace("【近期进展】", "【近期进", 1)
    return {
        "product_cap": product_cap,
        "product_markers_ok": _markers_ok(product_cap),
        "product_markers_intact": _markers_intact(product_cap),
        "product_markers_position_ok": _markers_position_ok(
            product_cap, require_both=True),
        "product_corrupted_markers_ok": _markers_ok(product_corrupted),
        "len_check_overlong": len_ok(payloads.get("overlong", "")),
        "len_check_within": len_ok(payloads.get("within", "")),
        "marker_check_bisected": markers_ok(payloads.get("bisected", "")),
        "marker_check_partial": markers_ok(
            payloads.get("partial_marker_with_valid_pinned", "")),
        "marker_check_intact": markers_ok(payloads.get("intact", "")),
        "pinned_check_missing": pinned_ok(payloads.get("missing_pinned", "")),
        "pinned_check_present": pinned_ok(payloads.get("with_pinned", "")),
    }


def _expand_item_list(value):
    """支持显式列表，或用 {prefix,count,width} 生成长列表（全部参数仍在 JSON 里）。"""
    if isinstance(value, dict):
        prefix = str(value.get("prefix") or "item-")
        count = int(value.get("count") or 0)
        width = int(value.get("width") or 3)
        start = int(value.get("start") or 1)
        return ["%s%0*d" % (prefix, width, index)
                for index in range(start, start + count)]
    return [str(item) for item in value or []]


def _s_taskbook_evict(case, modules):
    taskbook = modules["taskbook"]
    project_id = case.get("project_id") or "tb-golden"
    seed_items = _expand_item_list(case.get("seed_items"))
    appended = _expand_item_list(case.get("append_items"))
    mark_done = [str(item) for item in case.get("mark_done_texts") or []]
    taskbook.open_book(project_id, case.get("title") or "任务书", seed_items)
    for text in mark_done:
        row = next((item for item in taskbook.items(project_id)
                    if str(item.get("text")) == text), None)
        if row is None:
            raise AssertionError("种子任务书里找不到待完成条目：%s" % text)
        taskbook.complete(project_id, row["key"], user_override=True)
    taskbook.open_book(project_id, case.get("title") or "任务书",
                       seed_items + appended)
    rows = taskbook.items(project_id)
    texts = [str(item.get("text")) for item in rows]
    newest = appended[-1] if appended else ""
    oldest_done = mark_done[0] if mark_done else ""
    return {
        "item_count": len(rows),
        "max_items": int(taskbook.MAX_ITEMS),
        "has_newest": newest in texts,
        "has_oldest_done": oldest_done in texts,
        "newest_text": newest,
        "texts": texts,
    }


def _s_profile_panel(case, modules):
    profile = modules["profile"]
    context_panel = modules["context_panel"]
    profile.save(case.get("patch") or {})
    text = profile.context()
    view = context_panel.next_view(case.get("project_id") or "default",
                                   case.get("session_id") or "")
    layer = next((item for item in view.get("layers") or []
                  if item.get("key") == "profile"), None)
    if layer is None:
        raise AssertionError("上下文面板没有 profile 层")
    limit = int(profile.PROFILE_INJECT_MAX_CHARS)
    panel_budget = int(layer.get("budget") or 0)
    panel_file = REPO / "frontend" / "src" / "workbench" / "components" / "MemoryContextPanel.vue"
    source = panel_file.read_text(encoding="utf-8") if panel_file.exists() else ""
    return {
        "profile_text": text,
        "profile_chars": len(text),
        "profile_limit": limit,
        "panel_budget": panel_budget,
        "panel_chars": int(layer.get("chars") or 0),
        "same_source": panel_budget == limit,
        "injected_le": len(text) <= limit,
        "frontend_hardcodes_limit": "1800" in source,
        "frontend_uses_layer_budget": "layer.budget" in source,
    }


def _s_agent_memory_isolation(case, modules):
    memory = modules["agent_memory"]
    scope = case.get("scope") or "golden-iso"
    agent_a = case.get("agent_a") or "agent-A"
    agent_b = case.get("agent_b") or "agent-B"
    token = case.get("query_token") or "ORCHID-2718"
    memory.remember(scope, "fact", "A agent 记录",
                    token + " 只属于 A agent", agent_id=agent_a)
    memory.remember(scope, "fact", "B agent 记录",
                    "B-ONLY-记住这段文本", agent_id=agent_b)
    rows_a = memory.recall(scope, token, limit=8, agent_id=agent_a)
    rows_b = memory.recall(scope, token, limit=8, agent_id=agent_b)
    a_has = any(token in str(row.get("content") or "") for row in rows_a)
    b_has = any(token in str(row.get("content") or "") for row in rows_b)
    return {
        "recall_a_has": a_has,
        "recall_b_has": b_has,
        "recall_a_count": len(rows_a),
        "recall_b_count": len(rows_b),
    }


def _s_subagent_sessions(case, modules):
    sessions = modules["sessions"]
    main_id = case.get("main_session") or "golden-main"
    sub_id = case.get("subagent_session") or "sub-agent-golden"
    sessions.save(main_id, [{"user": "主会话问题", "assistant": "主会话回答"}],
                  summary="主会话摘要")
    sessions.save(sub_id, [{"user": "子会话问题", "assistant": "子会话回答"}],
                  summary="子会话摘要")
    default_rows = sessions.list_sessions(50)
    include_rows = sessions.list_sessions(50, include_subagents=True)
    default_ids = sorted(str(row.get("session_id")) for row in default_rows)
    include_ids = sorted(str(row.get("session_id")) for row in include_rows)
    return {
        "default_ids": default_ids,
        "include_ids": include_ids,
        "default_has_sub": sub_id in default_ids,
        "include_has_sub": sub_id in include_ids,
        "default_has_main": main_id in default_ids,
        "include_has_main": main_id in include_ids,
    }


def _s_live_smoke(case, modules):
    """live 组：仅 --live 时执行，调用真实 LLM，不参与快照比对。"""
    import llm as llm_module
    sessions = modules["sessions"]
    session_id = case.get("session_id") or "golden-live"
    current = _seed_session(sessions, session_id, case["seed"])
    client = llm_module.LLMClient()
    current, summary = _cycle(sessions, session_id, current, case["cycle"], client)
    return {"final_summary": summary, "summary_chars": len(summary)}


def _s_pinned_bytes_stable(case, modules):
    """用例 17：连续压缩 N 轮后，pinned 必须**逐字节**不变（不是“包含关键词”）。

    观测同时给出 pinned 原文与 sha256，快照里存的是字节指纹与内容；断言用
    `equals` 比较两个字段，等价于 `pinned_before.encode() == pinned_after.encode()`。
    """
    import hashlib
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    before = sessions._parse_summary(case["seed"]["summary"])["pinned"]
    rounds = int(case.get("rounds") or 90)
    llm = StubLLM([], case.get("llm_default") or "进展摘要。")
    llm.prompt_budget = int((case.get("cycle") or {}).get("prompt_budget") or 0)
    summary = case["seed"]["summary"]
    for index in range(1, rounds + 1):
        spec = _fmt(case["cycle"], index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)
    after = sessions._parse_summary(summary)["pinned"]
    return {
        "pinned_before": before,
        "pinned_after": after,
        "pinned_sha256_before": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "pinned_sha256_after": hashlib.sha256(after.encode("utf-8")).hexdigest(),
        "bytes_equal": before.encode("utf-8") == after.encode("utf-8"),
        "rounds": rounds,
        "llm_calls": llm.calls,
        "summary_chars": len(summary),
        "final_summary": summary,
    }


def _s_pinned_absent_all_prompts(case, modules):
    """用例 18：pinned 正文与 `【固定事实…】` 标记不得出现在**任何**提示词里。

    不是抽检：把 StubLLM 实际收到的每一份提示词都数一遍，命中数必须是 0。
    """
    import hashlib
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    pinned_token = case.get("pinned_token") or ""
    marker_hits = 0
    pinned_hits = 0
    llm = StubLLM(case.get("llm_replies") or [],
                  case.get("llm_default") or "进展摘要。")
    llm.prompt_budget = int((case.get("cycle") or {}).get("prompt_budget") or 0)
    rounds = int(case.get("rounds") or 1)
    summary = case["seed"]["summary"]
    for index in range(1, rounds + 1):
        spec = _fmt(case["cycle"], index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)
    for prompt in llm.prompts:
        if pinned_token and pinned_token in prompt:
            pinned_hits += 1
        if "【固定事实" in prompt:
            marker_hits += 1
    digests = [hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
               for prompt in llm.prompts]
    return {
        "prompt_count": len(llm.prompts),
        "pinned_hits": pinned_hits,
        "pinned_marker_hits": marker_hits,
        "all_prompts_clean": pinned_hits == 0 and marker_hits == 0,
        "prompt_lengths": [len(str(prompt)) for prompt in llm.prompts],
        "prompt_sha256": digests,
        "rolling_prompt_count": sum(
            1 for prompt in llm.prompts if "你是对话压缩器" in prompt),
        "final_summary": summary,
    }


def _s_det_cap_guard(case, modules):
    """用例 19：DET 回退路径下，cap 系列从未收到含 pinned 的串，裁剪标记不切 pinned。

    做法：在本进程内包一层记录器（不改生产代码），记录每次进入
    `_cap_summary` / `cap_summary_head_tail` / `cap_summary_tail` 的原文，
    再看最终摘要里 pinned 区域是否出现任何裁剪/淘汰标记。
    """
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    calls = []
    originals = {}

    def wrap(name):
        original = getattr(sessions, name, None)
        if original is None:
            return
        originals[name] = original
        markers = ("【固定事实", "【近期进展", "（固定事实前缀已满）")

        def recorder(text, *args, **kwargs):
            value = str(text or "")
            calls.append({"caller": name, "chars": len(value),
                          "has_pinned_marker": any(m in value for m in markers),
                          "sha256": __import__("hashlib").sha256(
                              value.encode("utf-8")).hexdigest()[:16]})
            return original(text, *args, **kwargs)

        setattr(sessions, name, recorder)

    for name in ("_cap_summary", "cap_summary_head_tail", "cap_summary_tail"):
        wrap(name)
    try:
        summary = case["seed"]["summary"]
        rounds = int(case.get("rounds") or 1)
        for index in range(1, rounds + 1):
            spec = _fmt(case["cycle"], index)
            # llm=None ⇒ 走 DET（确定性）回退路径。
            current, summary = _cycle(sessions, session_id, current, spec, None)
    finally:
        for name, original in originals.items():
            setattr(sessions, name, original)
    parsed = sessions._parse_summary(summary)
    before = sessions._parse_summary(case["seed"]["summary"])["pinned"]
    head = summary.split("【近期进展】")[0] if "【近期进展】" in summary else summary
    return {
        "cap_calls": len(calls),
        "cap_calls_with_pinned": sum(1 for item in calls if item["has_pinned_marker"]),
        "callers": sorted({item["caller"] for item in calls}),
        "pinned_verbatim": bool(before) and before in parsed["pinned"],
        "pinned_bytes_equal": before.encode("utf-8") == parsed["pinned"].encode("utf-8"),
        "marker_inside_pinned": any(mark in head for mark in ("（中间", "（最旧进展")),
        "mid_marker_count": summary.count("（中间"),
        "evict_marker_count": summary.count("（最旧进展"),
        "markers_ok": _markers_ok(summary),
        "summary_chars": len(summary),
        "summary_limit": _summary_limit(sessions,
                                        case["cycle"].get("prompt_budget")),
        "final_summary": summary,
    }


def _pinned_sig(entries, fields=("text", "source", "added_at")):
    """条目的稳定签名：只取“不得被改写”的三个字段。"""
    return [{key: str(item.get(key) or "") for key in fields}
            for item in entries or []]


def _pinned_prefix_state(before, after) -> dict:
    """压测口径：旧 pinned 必须逐字节成为新 pinned 的前缀，增长只能在尾部。

    规则本身允许（且必然发生）新增固定事实，所以“整块字节全等”是错误断言；
    正确断言是下面两条同时成立：
    1. `old_prefix_verbatim`：上一轮 pinned 的全部内容原样出现在新 pinned 最前面；
    2. `append_at_tail_only`：长度不缩短，且新增只发生在旧前缀之后。
    """
    before = str(before or "")
    after = str(after or "")
    return {
        "old_prefix_verbatim": bool(before) and after.startswith(before),
        "append_at_tail_only": bool(before) and len(after) >= len(before)
                               and after.startswith(before),
        "pinned_growth_chars": len(after) - len(before),
    }


def _s_pinned_model_no_delete(case, modules):
    """用例 20：模型路径尝试删除/缩短 pinned → 被忽略，条目数与旧条目不变。"""
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    before = sessions.pinned_view(session_id)["entries"]
    before_sig = _pinned_sig(before)

    # 攻击 1：模型写回一段“只剩一条”的摘要（最典型的丢失 pinned 情形）
    sessions.save(session_id, current, case["model_shrink_summary"])

    # 攻击 2：模型路径调用删除接口
    delete_out = sessions.delete_pinned(before[0]["id"], actor="model",
                                        session_id=session_id)

    # 攻击 3：模型路径显式传空列表要求“覆盖”
    append_out = sessions.append_pinned([], actor="model",
                                        origin="golden_model_append",
                                        session_id=session_id)

    view = sessions.pinned_view(session_id)
    after = view["entries"]
    actions = [item.get("action") for item in view["audit"]]
    summary = sessions.summary_text(session_id)
    return {
        "count_before": len(before),
        "count_after": len(after),
        "entries_unchanged": _pinned_sig(after[:len(before_sig)]) == before_sig,
        "delete_ok": bool(delete_out.get("ok")),
        "delete_reason": str(delete_out.get("reason") or ""),
        "append_added": len(append_out.get("added") or []),
        "shrink_rejected_audits": actions.count("shrink_rejected"),
        "delete_audits": actions.count("delete"),
        "all_texts_still_in_summary": all(item.get("text") in summary
                                          for item in before),
        "final_summary": summary,
    }


def _s_pinned_tombstone(case, modules):
    """用例 21：用户删除后，完整提取重跑不得让同一事实回流到 pinned。"""
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    token = case["fact_token"]
    entries = sessions.pinned_view(session_id)["entries"]
    target = next(item for item in entries if token in item.get("text", ""))
    delete_out = sessions.delete_pinned(target["id"], actor="user",
                                        session_id=session_id)
    after_delete = sessions.pinned_view(session_id)["entries"]

    llm = StubLLM([], case.get("llm_reply") or case["fact_text"])
    llm.prompt_budget = int((case.get("cycle") or {}).get("prompt_budget") or 0)
    summary = sessions.summary_text(session_id)
    for index in range(1, int(case.get("rounds") or 1) + 1):
        spec = _fmt(case["cycle"], index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)

    final = sessions.pinned_view(session_id)
    texts = [item.get("text", "") for item in final["entries"]]
    actions = [item.get("action") for item in final["audit"]]
    return {
        "delete_ok": bool(delete_out.get("ok")),
        "remaining_after_delete": len(after_delete),
        "deleted_gone": not any(token in item.get("text", "")
                                for item in after_delete),
        "readd_present": any(token in text for text in texts),
        "readd_suppressed_count": actions.count("readd_suppressed"),
        "tombstone_count": len(final["tombstones"]),
        "tombstones": [item.get("text", "") for item in final["tombstones"]],
        "final_entries": texts,
        "final_summary": summary,
    }


def _s_pinned_monotonic(case, modules):
    """用例 22：90 轮压测。旧 pinned 必须逐字节成为新 pinned 的前缀。

    只比条目字段不够：重排、在旧前缀中间插入/删除都会改变渲染结果。这里直接
    比对渲染后的 pinned 全文：旧前缀逐字节保留 + 增长只追加在尾部，条目数另做
    单调校验（非 user 路径不得减少）。
    """
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    before = sessions._parse_summary(sessions.summary_text(session_id))["pinned"]
    initial = sessions.pinned_view(session_id)["entries"]
    initial_sig = _pinned_sig(initial)
    counts = [len(initial)]
    decreased_rounds = []
    unchanged = True
    llm = StubLLM([], case.get("llm_default") or "进展摘要。")
    summary = sessions.summary_text(session_id)
    rounds = int(case.get("rounds") or 90)
    for index in range(1, rounds + 1):
        spec = _fmt(case["cycle"], index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)
        entries = sessions.pinned_view(session_id)["entries"]
        counts.append(len(entries))
        if len(entries) < counts[-2]:
            decreased_rounds.append(index)
        if _pinned_sig(entries[:len(initial_sig)]) != initial_sig:
            unchanged = False
    after = sessions._parse_summary(sessions.summary_text(session_id))["pinned"]
    prefix = _pinned_prefix_state(before, after)
    return {
        "rounds": rounds,
        "initial_count": len(initial),
        "final_count": len(counts) and counts[-1],
        "monotonic": not decreased_rounds,
        "decreased_rounds": decreased_rounds,
        "initial_entries_unchanged": unchanged,
        "old_prefix_verbatim": prefix["old_prefix_verbatim"],
        "append_at_tail_only": prefix["append_at_tail_only"],
        "pinned_growth_chars": prefix["pinned_growth_chars"],
        "pinned_chars_before": len(before),
        "pinned_chars_after": len(after),
        "count_curve": counts,
        "count_curve_sha256": __import__("hashlib").sha256(
            ",".join(str(value) for value in counts).encode("utf-8")
        ).hexdigest()[:16],
        "final_entries": [item.get("text", "")
                          for item in sessions.pinned_view(session_id)["entries"]],
        "final_summary_chars": len(summary),
    }


def _mutate_prefix_byte(value, prefix):
    """变异 1：把旧前缀里的第一个非空白字节改掉，新增尾部原样保留。"""
    index = next((i for i, ch in enumerate(prefix) if not ch.isspace()), None)
    if index is None:
        raise ValueError("旧前缀为空，无法做字节变异")
    replacement = "Z" if prefix[index] != "Z" else "Q"
    return prefix[:index] + replacement + prefix[index + 1:] + value[len(prefix):]


def _truncate_prefix_tail(value, prefix, cut=5):
    """变异 2：截掉旧前缀尾部若干字节，新增尾部原样保留。"""
    cut = min(int(cut), max(0, len(prefix) - 1))
    if cut <= 0:
        raise ValueError("旧前缀太短，无法做截断变异")
    return prefix[:len(prefix) - cut] + value[len(prefix):]


def _reorder_prefix_segments(value, prefix):
    """变异 3：把旧前缀的三段（或更多）重排，新增尾部原样保留。"""
    blocks = [block for block in prefix.split("\n\n") if block]
    if len(blocks) < 3:
        raise ValueError("旧前缀不足三段，无法做重排变异")
    return "\n\n".join([blocks[1], blocks[0]] + blocks[2:]) + value[len(prefix):]


def _s_pinned_prefix_checker_teeth(case, modules):
    """验证「旧前缀逐字节保留」断言本身有牙：三种破坏必须全部判红。

    做法与生产代码无关：先用真实压缩跑出 before/after，再对 after 的旧前缀做
    改字节 / 截断尾部 / 重排三段，逐个跑同一套判定；任何一种没被判红，说明
    断言形同虚设（用例自身会红）。
    """
    sessions = modules["sessions"]
    session_id = case["session_id"]
    current = _seed_session(sessions, session_id, case["seed"])
    before = sessions._parse_summary(sessions.summary_text(session_id))["pinned"]
    llm = StubLLM([], case.get("llm_default") or "进展摘要。")
    summary = sessions.summary_text(session_id)
    rounds = int(case.get("rounds") or 1)
    for index in range(1, rounds + 1):
        spec = _fmt(case["cycle"], index)
        current, summary = _cycle(sessions, session_id, current, spec, llm)
    after = sessions._parse_summary(sessions.summary_text(session_id))["pinned"]
    base = _pinned_prefix_state(before, after)

    mutations = {
        "prefix_byte_changed": _mutate_prefix_byte(after, before),
        "prefix_tail_truncated": _truncate_prefix_tail(after, before),
        "prefix_segments_reordered": _reorder_prefix_segments(after, before),
    }
    caught = {}
    for name, mutated in mutations.items():
        state = _pinned_prefix_state(before, mutated)
        # 断言由两条同时组成，任一条不成立即整条断言判红。
        caught[name] = not (state["old_prefix_verbatim"]
                            and state["append_at_tail_only"])
    return {
        "rounds": rounds,
        "baseline_ok": base["old_prefix_verbatim"] and base["append_at_tail_only"],
        "baseline_growth_chars": base["pinned_growth_chars"],
        "mutation_count": len(mutations),
        "caught": caught,
        "mutations_caught": all(caught.values()),
        "mutations_missed": sorted(name for name, hit in caught.items() if not hit),
        "pinned_chars_before": len(before),
        "pinned_chars_after": len(after),
    }


_SCENARIOS = {
    "pinned_model_no_delete": _s_pinned_model_no_delete,
    "pinned_tombstone": _s_pinned_tombstone,
    "pinned_monotonic": _s_pinned_monotonic,
    "pinned_prefix_checker_teeth": _s_pinned_prefix_checker_teeth,
    "pinned_bytes_stable": _s_pinned_bytes_stable,
    "pinned_absent_all_prompts": _s_pinned_absent_all_prompts,
    "det_cap_guard": _s_det_cap_guard,
    "hard_constraints_survive": _s_hard_constraints_survive,
    "pinned_not_in_compact_prompt": _s_pinned_not_in_compact_prompt,
    "rolling_evict": _s_rolling_evict,
    "pinned_full_evict": _s_pinned_full_evict,
    "hard_absorbed": _s_hard_absorbed,
    "cap_extreme": _s_cap_extreme,
    "idempotent": _s_idempotent,
    "legacy_migration": _s_legacy_migration,
    "write_inject": _s_write_inject,
    "budget_matrix": _s_budget_matrix,
    "ordinary_fact_not_pinned": _s_ordinary_fact_not_pinned,
    "negative_checkers": _s_negative_checkers,
    "taskbook_evict": _s_taskbook_evict,
    "profile_panel": _s_profile_panel,
    "agent_memory_isolation": _s_agent_memory_isolation,
    "subagent_sessions": _s_subagent_sessions,
    "live_smoke": _s_live_smoke,
}


def _load_modules():
    import config  # noqa: F401  （导入即隔离：路径来自 DOCMIND_STATE_ROOT）
    import sessions
    import agent_memory
    from agent_runtime.memory import taskbook, profile, context_panel
    return {"config": config, "sessions": sessions, "agent_memory": agent_memory,
            "taskbook": taskbook, "profile": profile,
            "context_panel": context_panel}


def _assert_isolated(modules, workdir: Path):
    """硬性保险：所有可写路径必须落在本用例临时目录内，否则立刻失败。"""
    root = workdir.resolve()
    candidates = {
        "sessions.SESSIONS_DIR": modules["sessions"].SESSIONS_DIR,
        "agent_memory.DB_PATH": modules["agent_memory"].DB_PATH,
        "config.STATE_FILE": modules["config"].STATE_FILE,
        "memory.store.root": modules["taskbook"].store.root(),
    }
    for label, value in candidates.items():
        resolved = Path(str(value)).resolve()
        if resolved != root and root not in resolved.parents:
            raise AssertionError(
                "隔离失败：%s 落在临时目录之外（%s）；拒绝运行以免写入真实记忆库。"
                % (label, resolved))


def _child_main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--_child", default="")
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    case_path = Path(args._child)
    workdir = Path(args.workdir)
    _install_isolated_env(workdir)
    sys.path.insert(0, str(REPO))

    result = {"name": case_path.stem}
    try:
        with open(case_path, "r", encoding="utf-8") as handle:
            case = json.load(handle)
        modules = _load_modules()
        _assert_isolated(modules, workdir)
        scenario = case.get("scenario") or ""
        if scenario not in _SCENARIOS:
            raise ValueError("未知 scenario：%s" % scenario)
        observations = _SCENARIOS[scenario](case, modules)
        result["observations"] = observations
        result["assertion_results"] = _eval_assertions(
            observations, case.get("assertions") or [])
    except Exception as exc:  # noqa: BLE001 —— 必须带用例名抛出
        result["error"] = "用例 %s 异常：%s: %s\n%s" % (
            case_path.stem, type(exc).__name__, exc,
            traceback.format_exc().strip().splitlines()[-1] if traceback else "")
        result["traceback_tail"] = traceback.format_exc()[-1600:]
        result["assertion_results"] = []
    with open(args.result, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
    failed = bool(result.get("error")) or any(
        not item.get("ok") for item in result.get("assertion_results", []))
    return 1 if failed else 0


if __name__ == "__main__":
    if "--_child" in sys.argv:
        sys.exit(_child_main(sys.argv[1:]))
    sys.exit(main())
