"""任务助手的纯编排契约：标准任务规格、失败续跑与自然人话回报。"""
from __future__ import annotations

import json
import re


def planner_prompt(goal):
    return (
        "你是任务助手，只负责理解意图和拆解任务，不能执行业务工作、不能调用工具。\n"
        "把用户要求细化为可由同级 Agent 独立执行的标准任务规格。"
        "只输出 JSON 对象，不要代码围栏，格式：\n"
        '{"tasks":[{"id":"t1","role":"researcher","goal":"明确目标",'
        '"input":["已知输入"],"outputs":["应交付内容"],"boundaries":["不得越过的边界"],'
        '"acceptance":["可检查的验收条件"],"depends_on":[],"optional":false}]}\n'
        "role 只能是 researcher/coder/reviewer/tester/dispatcher。"
        "任务之间是同级关系，用 depends_on 表达先后；安全的同级直接任务立即执行，"
        "执行 Agent 后续继续委派 subagent 时，跨层、跨项目、扩权或超限仍需审批。"
        "不要替执行 Agent 做分析、读文件、写代码或编造结果。\n\n用户要求：\n" + str(goal or "")
    )


def _json_value(raw):
    text = str(raw or "").strip()
    candidates = [text]
    candidates.extend(match.group(1).strip() for match in re.finditer(
        r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S))
    candidates.extend(text[index:] for index, char in enumerate(text) if char in "{[")
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError("任务助手没有返回可解析的任务规格 JSON")


def _strings(value, fallback):
    if isinstance(value, (list, tuple)):
        rows = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, dict):
        rows = [f"{key}: {item}" for key, item in value.items()]
    else:
        rows = [str(value or "").strip()]
    rows = [item for item in rows if item]
    return rows[:12] or list(fallback)


def parse_tasks(raw, goal):
    value = _json_value(raw)
    items = value.get("tasks") if isinstance(value, dict) else value
    if not isinstance(items, list) or not items:
        raise ValueError("任务助手没有给出可执行任务")
    tasks = []
    for index, raw_task in enumerate(items):
        if not isinstance(raw_task, dict):
            continue
        objective = str(raw_task.get("goal") or raw_task.get("task") or raw_task.get("desc") or "").strip()
        if not objective:
            continue
        task = dict(raw_task)
        task.update({
            "id": str(raw_task.get("id") or f"t{index + 1}"),
            "role": str(raw_task.get("role") or "researcher"),
            "task": objective,
            "goal": objective,
            "input": _strings(raw_task.get("input"), [str(goal or "")]),
            "outputs": _strings(raw_task.get("outputs") or raw_task.get("deliverables"),
                                ["结构化结论、证据与未解决项"]),
            "boundaries": _strings(raw_task.get("boundaries"), ["不编造结果；遵守用户边界与工具审批"]),
            "acceptance": _strings(raw_task.get("acceptance") or raw_task.get("acceptance_criteria"),
                                   ["产出与目标一致，并附可核对证据"]),
        })
        tasks.append(task)
    if not tasks:
        raise ValueError("任务规格缺少目标")
    return tasks


def render_spec(task):
    def block(label, key):
        return label + "：\n" + "\n".join("- " + str(item) for item in (task.get(key) or []))
    return "\n".join([
        "【标准化任务规格】", block("目标", "goal") if isinstance(task.get("goal"), list) else "目标：" + str(task.get("goal") or task.get("task") or ""),
        block("输入", "input"), block("产出物", "outputs"),
        block("边界", "boundaries"), block("验收条件", "acceptance"),
        "完成后返回结构化结果：status、conclusion、error、completed、resume_point。",
    ])


def envelope(task, result):
    out = dict(result or {})
    conclusion = str(out.get("conclusion") or "")
    status = str(out.get("status") or "failed")
    out["spec"] = {key: task.get(key) for key in
                   ("id", "role", "goal", "input", "outputs", "boundaries", "acceptance", "depends_on")}
    out["completed"] = ([conclusion] if conclusion else [])
    if status != "ok":
        out["failure_reason"] = str(out.get("error") or out.get("needs_confirmation", {}).get("reason") or "未记录原因")
        out["resume_point"] = ("等待用户在 Agent 树审批" if status == "pending"
                               else "从已完成部分继续；可换角色、换策略或拆小后重派")
    else:
        out["resume_point"] = ""
    return out


def recovery_prompt(failed, results, attempt):
    parts = []
    for task in failed:
        result = results.get(task["id"]) or {}
        parts.append(
            f"任务 {task['id']}：{task.get('goal') or task.get('task')}\n"
            f"失败原因：{result.get('failure_reason') or result.get('error') or '未记录'}\n"
            f"已完成部分：{result.get('completed') or []}\n续跑点：{result.get('resume_point') or '未记录'}")
    return (
        f"你是任务助手，正在进行第 {attempt} 次失败恢复；你不能自己执行业务工作。\n"
        "只能采用重派、换角色/策略、拆小任务三种方式。每个补救任务必须 depends_on 指向其补救的失败任务，"
        "并保留完整 goal/input/outputs/boundaries/acceptance 字段。"
        "只输出 JSON 任务数组，不要解释；确实无法补救时输出 []。\n\n" + "\n\n".join(parts)
    )


def summary_prompt(goal, tasks, results):
    rows = []
    for task in tasks:
        result = results.get(task["id"]) or {}
        rows.append(
            f"任务 {task['id']}（{task.get('role')}）目标：{task.get('goal') or task.get('task')}\n"
            f"状态：{result.get('status')}\n结论：{result.get('conclusion') or ''}\n"
            f"失败原因：{result.get('failure_reason') or result.get('error') or ''}\n"
            f"已完成部分：{result.get('completed') or []}\n续跑点：{result.get('resume_point') or ''}")
    return (
        "你是任务助手，请把下面的结构化执行结果翻译成自然语言回报给用户。\n"
        "先结论后证据；区分已完成、待审批、失败和阻断；失败必须说明原因、已完成部分和下一步。"
        "不得只贴 JSON，不得编造不存在的结果。\n\n总目标：" + str(goal or "") + "\n\n" + "\n\n".join(rows)
    )


def fallback_summary(report):
    if report.get("error"):
        return "任务助手未能完成编排：" + str(report["error"])
    pending = [task_id for task_id, result in (report.get("results") or {}).items()
               if result.get("status") == "pending"]
    failed = [task_id for task_id, result in (report.get("results") or {}).items()
              if result.get("status") == "failed"]
    ok = [task_id for task_id, result in (report.get("results") or {}).items()
          if result.get("status") == "ok"]
    parts = []
    if ok:
        parts.append("已完成 " + "、".join(ok))
    if pending:
        parts.append("等待风险审批 " + "、".join(pending) + "；批准前不会执行，批准后仅执行登记分支，原 DAG 不自动续跑")
    if failed:
        parts.append("失败 " + "、".join(failed) + "；失败原因和续跑点已保留，未静默放弃")
    if report.get("blocked"):
        parts.append("受上游状态阻断 " + "、".join(report["blocked"]))
    return "；".join(parts) or "任务助手未产生可执行结果"
