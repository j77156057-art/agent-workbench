"""回合收尾：把一次问答落成结构化回合 + 钩子/错误/回执。

只读 turn 与 journal 的结构化字段，不碰逐字 trail；任何新写入都不影响主流程
（调用方包 try，本模块也逐段容错）。
"""
from __future__ import annotations

import json
import re

from . import episodes, errors, hooks, promote, receipts, store, taskbook
from .injection import enabled

# 只有明确的"要记住"才升成约定钩子，避免把普通请求都写进钩子层。
_AGREEMENT = re.compile(r"记住|记下|以后都|今后都|约定|别忘了|不要忘记|务必记得")
_STATUS = {"completed", "failed", "interrupted", "partial"}


def _clip(value, limit):
    return str(value or "")[:limit]


_FAIL_SIGNAL = re.compile(
    r"(?i)(failed|failure|error|traceback|exception|文件不存在|读取失败|参数缺失|"
    r"拒绝写入|拒绝访问|失败)")
_FAIL_NOISE = re.compile(r"(?i)(warning|deprecat|warnings\.warn)")


def _failure_excerpt(text, limit=300):
    """钩子只留可执行的失败证据：优先失败/异常行，跳过 pytest 警告噪声。"""
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    picked = [line for line in lines if _FAIL_SIGNAL.search(line) and not _FAIL_NOISE.search(line)]
    if not picked:
        picked = lines[-4:]
    return _clip("\n".join(picked[-4:]), limit)


def _plan_next_step():
    try:
        from agent_runtime import plan_state
        from config import get_runtime
        plan = plan_state.read(get_runtime("code_root", ""))
        return next((str(item.get("text") or "") for item in plan.get("items") or []
                     if isinstance(item, dict) and not item.get("done")), "")
    except Exception:  # noqa: BLE001
        return ""


def _unverified_next_step(turn, journal):
    """本回合改了文件却还没有通过回执 → 下一步就是去验证它。

    「下一步」通道原先唯一的生产者是 Plan 模式的 `plan_state`：普通回合没有计划
    文件，于是 77 个 episode 里 0 条带 next，L4 的这一支等于没接线。验证缺口是
    普通回合里真实存在、且不需要编造的待办 —— 正好是「没有回执不得声称完成」
    要盯的那件事。已验证或没改文件一律返回空，绝不造一个下一步出来。
    """
    targets = dict(getattr(turn, "verification_targets", None) or {})
    if not targets:
        return ""
    tested = dict(getattr(turn, "test_command_targets", None) or {})
    if getattr(turn, "test_command_ok", False) and tested == targets:
        return ""
    if journal.get("verified"):
        return ""
    paths = sorted(targets)
    shown = ", ".join(paths[:3]) + (" 等" if len(paths) > 3 else "")
    return ("验证改动：" + shown + "（跑测试或验收，取得回执后才算完成）")[:160]


def record_turn(*, session_id, project_id, question, final_text, status="completed",
                turn=None, journal=None, error="", next_step=""):
    """写入一条结构化回合；flag 关或无会话时返回空 dict。"""
    if not enabled() or not str(session_id or "").strip():
        return {}
    journal = journal if isinstance(journal, dict) else {}
    project = project_id or "default"
    session = session_id
    episode_id = episodes.new_id()
    data_status = str(status or "completed")
    if data_status not in _STATUS:
        data_status = "completed"

    actions = []
    for step in getattr(turn, "steps", None) or []:
        if not isinstance(step, dict):
            continue
        actions.append({"tool": str(step.get("action") or ""),
                        "ok": bool(step.get("ok")),
                        "arg_chars": int(step.get("arg_chars") or 0),
                        "obs_chars": int(step.get("obs_chars") or 0),
                        "ms": int(step.get("latency_ms") or 0)})

    files = []
    for change in journal.get("file_changes") or []:
        if isinstance(change, dict) and change.get("path"):
            files.append(str(change["path"]))

    receipt_ids = []
    verification = journal.get("verification")
    if isinstance(verification, dict) and verification:
        if journal.get("verified"):
            outcome = "pass"
        elif any(not bool(value) for value in verification.values()):
            outcome = "fail"
        else:
            outcome = "uncertain"
        receipt_ids.append(receipts.record(
            project, session=session, type="verify", result=outcome, cmd="self_verify",
            evidence=json.dumps(verification, ensure_ascii=False)[:2000]))
    if getattr(turn, "test_command_ok", False):
        targets = dict(getattr(turn, "verification_targets", None) or {})
        tested = dict(getattr(turn, "test_command_targets", None) or {})
        if targets and tested == targets:
            receipt_ids.append(receipts.record(
                project, session=session, type="test", result="pass",
                cmd=_clip(getattr(turn, "test_command", ""), 200),
                evidence=json.dumps({"exit_code": 0, "source": "run_command",
                                     "targets": sorted(targets)},
                                    ensure_ascii=False)[:2000]))

    recorded_errors = []
    failure_count = int(getattr(turn, "failure_count", 0) or 0)
    failure_text = _failure_excerpt(
        getattr(turn, "last_failure", "") or journal.get("last_obs") or "")
    tool = str(getattr(turn, "last_failure_tool", "") or journal.get("last_action") or "").strip()
    if failure_count > 0 and failure_text:
        sig = errors.signature(tool, failure_text)
        errors.record(project, session, sig, tool=tool, cause=failure_text, label=tool or sig)
        hooks.add(project, session, "error",
                  (f"{tool} 失败：{failure_text}" if tool else f"失败：{failure_text}")[:300],
                  source_turn=episode_id, verified=False, pinned=True,
                  key="error|" + sig)
        recorded_errors.append({"sig": sig, "cause": failure_text[:200]})

    question_text = str(question or "")
    if _AGREEMENT.search(question_text):
        hooks.add(project, session, "user_agreement", question_text[:200],
                  source_turn=episode_id, verified=True, pinned=True)

    if not next_step:
        next_step = _plan_next_step()
    if not next_step:
        next_step = _unverified_next_step(turn, journal)
    try:
        task_id = taskbook.current_id(project)
    except Exception:  # noqa: BLE001
        task_id = ""

    episode = {
        "id": episode_id,
        "session": store.safe_id(session),
        "project": store.safe_id(project),
        "task": task_id,
        "goal": question_text,
        "actions": actions,
        "files": files,
        "result": {"status": data_status, "summary": final_text, "error": error},
        "decisions": [],
        "errors": recorded_errors,
        "next": next_step,
        "receipts": receipt_ids,
        # 漏斗要能回答「这一回合到底跑过测试命令没有」，而 actions 里只有工具名和
        # ok，看不出跑的是不是测试。`test_command` 由派发点在识别出测试命令时写入，
        # 这里落一个布尔，避免下游再去猜参数。
        "test_run": bool(str(getattr(turn, "test_command", "") or "").strip()),
        "tokens": {"in": int(getattr(turn, "prompt_tokens", 0) or 0),
                   "out": int(getattr(turn, "completion_tokens", 0) or 0)},
    }
    episodes.append(project, session, episode)
    try:
        taskbook.maybe_draft(project, question_text, next_step, len(actions))
    except Exception:  # noqa: BLE001
        pass
    try:
        promote.sweep(project)
    except Exception:  # noqa: BLE001
        pass
    return {"episode": episode_id, "receipts": receipt_ids,
            "errors": len(recorded_errors), "record": episode}
