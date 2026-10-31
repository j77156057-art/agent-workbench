"""超长 Plan 运行器：后台连续跑回合，直到任务书全部划掉，再进入定期复盘。

边界（刻意如此）：
- 只有用户能停止；模型没有 stop 工具，停不下来。
- 停止执行不等于关闭 Plan：任务书全部完成后进入 review 阶段，按间隔反复问
  “审查/测试/完善/改进”，发现新工作会追加任务并回到执行。
- 每轮动作与结论写入 plan-log.jsonl，供用户事后审查。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .memory import store, taskbook
from . import plan_log

MAX_ACTIONS_LOGGED = 80
_LOCK = threading.RLock()
_ACTIVE = {}
_STOPS = {}


def _key(project_id, session_id):
    return str(project_id or ""), str(session_id or "")


def _path(project_id, session_id):
    return store.safe_path("projects", store.safe_id(project_id or "default"),
                           "session", store.safe_id(session_id or "global"),
                           "plan-run.json")


def _read(project_id, session_id):
    try:
        with open(_path(project_id, session_id), "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(project_id, session_id, state):
    target = _path(project_id, session_id)
    store.ensure_dir(os.path.dirname(target))
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.atomic_write(target, json.dumps(state, ensure_ascii=False, indent=2,
                                          sort_keys=True) + "\n")
    return state


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value):
    try:
        stamp = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _task_view(project_id):
    rows = taskbook.items(project_id)
    pending = [item for item in rows
               if item.get("state") not in ("done", "done_pending_merge")]
    done = len(rows) - len(pending)
    return rows, pending, done


def _closed(project_id):
    rows, pending, _done = _task_view(project_id)
    return bool(rows) and not pending


def _prompt_execute(goal, rows, pending):
    if not rows:
        return (
            "【Plan 长跑 · 执行】目标是：%s\n"
            "当前任务书为空。请立即用 dev_plan(action: open, title: <目标短名>, "
            "items: <3-8 个可执行、可验收的步骤>) 建立任务书，然后开始做第一件。\n"
            "规则：每完成一项用 dev_plan(action: check) 勾掉；涉及代码必须测试/验证；"
            "不要等待用户确认，不要因为一轮结束就停止。\n"
            "关键取舍用 plan_decision(decision: ..., rationale: ...) 记账。"
            "Plan 只能由用户停止，你不能自行关闭。" % goal
        )
    lines = ["【Plan 长跑 · 执行】目标：%s" % goal,
             "任务书还有 %d 项未完成：" % len(pending)]
    for item in pending[:12]:
        lines.append("  - [ ] %s" % str(item.get("text") or "")[:180])
    lines.append(
        "继续做下一件未完成的事；完成后用 dev_plan(action: check) 勾掉并跑验证。"
        "不要停下等用户，不要重复已完成项。关键取舍用 plan_decision 记录。"
        "只有任务书全部划掉后才进入复盘；Plan 只能由用户停止。"
    )
    return "\n".join(lines)


def _prompt_review(goal, done):
    return (
        "【Plan 长跑 · 定期复盘】目标：%s；任务书已完成 %d 项。\n"
        "请只回答并检查这四件事：\n"
        "1) 是否已经审查过全部改动/产物？\n"
        "2) 是否已经测试完毕（真实命令、真实输出）？\n"
        "3) 是否确定已经完善，没有明显遗漏？\n"
        "4) 还有哪些改进或提升空间？\n"
        "如果发现新工作，用 dev_plan(action: append, items: ...) 追加任务并继续执行；"
        "如果确实没有，明确说“本轮无需改进”。不要关闭 Plan，用户会决定何时停止。"
        % (goal, done)
    )


# 每一轮都重新声明的不变量。放在 system 而不是 user，是因为长跑里用户消息会
# 被逐轮历史稀释，而 system 每轮都在。
# 注意：这里刻意**不**复用 Agent.plan_mode——那个开关的既有语义是「先用 `Plan:`
# 前缀输出编号计划」，和长跑执行器无关，误开会每轮挤出一段计划文本。
_SYSTEM_ANCHOR = (
    "【Plan 长跑模式】用户不在场，你是无人值守执行器。\n"
    "1) 每轮必须推进任务书：读取未完成项 → 调用工具执行 → 用 dev_plan(action: check) 勾掉。\n"
    "2) 不许把「等用户确认」「下次再做」当作结论；确实做不动就写明阻塞原因并用 plan_decision 记账。\n"
    "3) 涉及代码的改动必须用真实命令验证；未验证不许勾掉任务。\n"
    "4) 关键取舍（选型、拆分、放弃的方案）用 plan_decision(decision, rationale) 记入决策账本。\n"
    "5) Plan 只能由用户在面板上停止；你没有关闭 Plan 的工具，不要因为「看起来做完了」就收手。\n"
)


def start(project_id, session_id, goal, *, llm=None, code_root="",
          review_interval_minutes=30, max_iterations=200):
    project_id, session_id = _key(project_id, session_id)
    goal = str(goal or "").strip()
    if not project_id or not session_id:
        return {"ok": False, "error": "缺少 project_id/session_id"}
    if not goal:
        return {"ok": False, "error": "goal 不能为空"}
    key = _key(project_id, session_id)
    with _LOCK:
        thread = _ACTIVE.get(key)
        if thread is not None and thread.is_alive():
            return {"ok": False, "error": "该会话已有 Plan 在运行"}
        state = _read(project_id, session_id)
        if state.get("status") in ("running", "reviewing") and state.get("user_stop") is not True:
            # 进程重启后留下的“running”没有活线程，允许重新开始。
            pass
        review = max(1, int(review_interval_minutes or 30))
        state = {
            "id": "plan-" + uuid.uuid4().hex[:12],
            "project": project_id, "session": session_id, "goal": goal,
            "status": "running", "phase": "execute", "iteration": 0,
            "review_interval_minutes": review,
            "next_review_at": "", "last_final": "", "last_error": "",
            "stop_reason": "", "user_stop": False, "created": _iso(_now()),
            "updated": _iso(_now()), "max_iterations": max(1, int(max_iterations or 200)),
            "taskbook": {"total": len(_task_view(project_id)[0]),
                         "done": _task_view(project_id)[2],
                         "pending": len(_task_view(project_id)[1])},
        }
        _write(project_id, session_id, state)
        stop = threading.Event()
        _STOPS[key] = stop
        thread = threading.Thread(
            target=_drive, name="docmind-plan-%s" % session_id[:24], daemon=True,
            args=(project_id, session_id, goal, llm, code_root, review,
                  int(max_iterations or 200), stop))
        _ACTIVE[key] = thread
        thread.start()
    plan_log.append(project_id, session_id, "start", goal,
                    review_interval_minutes=review)
    return {"ok": True, "state": _read(project_id, session_id)}


def stop(project_id, session_id, *, by="user"):
    project_id, session_id = _key(project_id, session_id)
    key = _key(project_id, session_id)
    with _LOCK:
        stop_event = _STOPS.get(key)
        if stop_event is not None:
            stop_event.set()
        state = _read(project_id, session_id)
        if not state:
            return {"ok": False, "error": "没有 Plan 记录"}
        state["status"] = "stopped"
        state["phase"] = "stopped"
        state["user_stop"] = True
        state["stop_reason"] = str(by or "user")
        _write(project_id, session_id, state)
        _ACTIVE.pop(key, None)
        _STOPS.pop(key, None)
    plan_log.append(project_id, session_id, "stop",
                    "用户停止 Plan", reason=str(by or "user"))
    return {"ok": True, "state": state}


def is_active(project_id, session_id):
    key = _key(project_id, session_id)
    thread = _ACTIVE.get(key)
    if thread is not None and thread.is_alive():
        return True
    state = _read(project_id, session_id)
    return state.get("status") in ("running", "reviewing") and thread is not None


def status(project_id, session_id):
    project_id, session_id = _key(project_id, session_id)
    state = _read(project_id, session_id)
    if not state:
        return {"ok": True, "active": False, "state": None,
                "taskbook": {"items": [], "done": 0, "total": 0},
                "log": []}
    key = _key(project_id, session_id)
    thread = _ACTIVE.get(key)
    active = bool(thread is not None and thread.is_alive())
    if state.get("status") in ("running", "reviewing") and not active:
        state["status"] = "interrupted"
        state["stop_reason"] = state.get("stop_reason") or "进程重启/线程已退出"
    rows, pending, done = _task_view(project_id)
    return {"ok": True, "active": active, "state": state,
            "taskbook": {"items": rows, "pending": len(pending),
                         "done": done, "total": len(rows)},
            "log": plan_log.rows(project_id, session_id, limit=120)}


def _run_turn(agent, prompt, stop_event):
    actions, final, error = [], "", ""
    try:
        for event in agent.run(prompt, stream=True, cancel_event=stop_event,
                               system_context=_SYSTEM_ANCHOR):
            kind = event.get("type")
            if kind == "action":
                actions.append(str(event.get("text") or event.get("tool") or "")[:300])
            elif kind == "final":
                final = str(event.get("text") or "")
            elif kind == "error":
                error = str(event.get("text") or event.get("message") or "")
    except Exception as exc:  # noqa: BLE001 —— 线程不能把异常抛给服务
        error = f"{type(exc).__name__}: {exc}"
    return {"actions": actions[:MAX_ACTIONS_LOGGED], "final": final, "error": error}


def _drive(project_id, session_id, goal, llm, code_root, review_minutes,
           max_iterations, stop_event):
    from agent import Agent
    key = _key(project_id, session_id)
    state = _read(project_id, session_id)
    agent = Agent(llm=llm, session_id=session_id, project_id=project_id,
                  application_id="developer")
    if code_root:
        agent._session_code_root = str(code_root)
    errors = 0
    try:
        while not stop_event.is_set():
            state = _read(project_id, session_id)
            if state.get("status") == "stopped":
                break
            rows, pending, done = _task_view(project_id)
            if rows and not pending:
                state["phase"] = "reviewing"
                state["status"] = "reviewing"
                next_at = _parse(state.get("next_review_at"))
                if next_at is None or next_at <= _now():
                    prompt = _prompt_review(goal, done)
                    phase = "review"
                    state["next_review_at"] = _iso(_now() + timedelta(minutes=review_minutes))
                else:
                    _write(project_id, session_id, state)
                    stop_event.wait(min(15, max(1, (next_at - _now()).total_seconds())))
                    continue
            else:
                state["phase"] = "execute"
                state["status"] = "running"
                prompt = _prompt_execute(goal, rows, pending)
                phase = "execute"
            if int(state.get("iteration") or 0) >= max_iterations:
                state["status"] = "stopped"
                state["phase"] = "stopped"
                state["stop_reason"] = "max_iterations"
                _write(project_id, session_id, state)
                plan_log.append(project_id, session_id, "stop",
                                "达到最大轮次，系统停止", reason="max_iterations")
                break
            state["iteration"] = int(state.get("iteration") or 0) + 1
            state["last_prompt"] = prompt[:400]
            _write(project_id, session_id, state)
            result = _run_turn(agent, prompt, stop_event)
            rows, pending, done = _task_view(project_id)
            state = _read(project_id, session_id)
            state["last_final"] = result["final"][:2000]
            state["last_error"] = result["error"][:500]
            state["taskbook"] = {"total": len(rows), "done": done,
                                 "pending": len(pending)}
            state["updated"] = _iso(_now())
            _write(project_id, session_id, state)
            plan_log.append(project_id, session_id,
                            "review" if phase == "review" else "turn",
                            result["final"][:1600] or result["error"] or "（无输出）",
                            phase=phase, iteration=state["iteration"],
                            actions=result["actions"],
                            taskbook={"total": len(rows), "done": done,
                                      "pending": len(pending)})
            if result["error"] and "cancelled" not in result["error"].lower():
                errors += 1
                if errors >= 3:
                    state = _read(project_id, session_id)
                    state["status"] = "error"
                    state["phase"] = "error"
                    state["stop_reason"] = "连续错误"
                    _write(project_id, session_id, state)
                    plan_log.append(project_id, session_id, "error",
                                    result["error"], retries=errors)
                    break
                stop_event.wait(5)
            else:
                errors = 0
            if stop_event.is_set():
                break
            stop_event.wait(1)
    finally:
        with _LOCK:
            _ACTIVE.pop(key, None)
            _STOPS.pop(key, None)
        state = _read(project_id, session_id)
        if state and state.get("status") not in ("stopped", "error"):
            state["status"] = "interrupted" if stop_event.is_set() else "stopped"
            state["phase"] = "stopped"
            state["stop_reason"] = state.get("stop_reason") or "runner_exit"
            _write(project_id, session_id, state)
