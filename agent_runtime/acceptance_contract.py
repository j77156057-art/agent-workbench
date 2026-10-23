"""Durable, user-reviewed acceptance criteria for a development workflow."""
from __future__ import annotations

from typing import Any, Iterable, Mapping


class AcceptanceError(ValueError):
    pass


def from_tasks(tasks: Iterable[Mapping[str, Any]],
               *, runtime_required: bool = False) -> dict[str, Any]:
    """Propose checkable criteria from the plan without claiming they passed.

    `runtime_required=True`（游戏工作流）额外追加一条**真跑过**的必需项：
    它不认同"任务都完成"就等于"游戏能玩"，必须有运行期证据（见
    `runtime:engine` 的机器判定）。这样 plan 阶段起草、approval 阶段一起确认，
    review 才有收手的依据。
    """
    items = []
    for task in tasks:
        task_id = str(task.get("id") or "").strip()
        statement = str(task.get("task") or "").strip()
        if task_id and statement:
            items.append({"id": "accept-" + task_id, "statement": statement[:1000],
                          "method": "检查任务结果及项目效果", "evidence": ["task:" + task_id],
                          "required": not bool(task.get("optional")),
                          "user_approved": False})
    if runtime_required:
        items.append({
            "id": "accept-runtime-playtest",
            "statement": "游戏能从当前代码真实启动并运行；运行日志无错误行，且日志晚于最后一次代码改动。",
            "method": "engine_start → engine_logs（errors 为空、fresh=true）→ game_screenshot 观察画面",
            "evidence": ["runtime:engine"],
            "required": True,
            "user_approved": False,
        })
    return {"revision": 1, "approved_revision": 0, "items": items,
            "final_decision": "pending", "final_note": ""}


def revise(contract: Mapping[str, Any], raw_items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    items = list(raw_items)
    if not 1 <= len(items) <= 32:
        raise AcceptanceError("验收条件须有 1 至 32 项")
    normalized = []
    seen = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, Mapping):
            raise AcceptanceError("验收条件格式无效")
        item_id = str(raw.get("id") or "accept-%s" % (index + 1)).strip()[:80]
        statement = str(raw.get("statement") or "").strip()
        method = str(raw.get("method") or "人工检查").strip()
        if not item_id or item_id in seen or not statement or len(statement) > 1000:
            raise AcceptanceError("验收条件 ID 必须唯一，且表述不可为空或超过 1000 字")
        if len(method) > 200:
            raise AcceptanceError("验收方法不能超过 200 字")
        evidence = raw.get("evidence") or []
        if not isinstance(evidence, list) or len(evidence) > 16:
            raise AcceptanceError("每项最多指定 16 条证据")
        evidence = [str(value).strip()[:500] for value in evidence]
        if any(not value for value in evidence):
            raise AcceptanceError("证据路径或说明不能为空")
        seen.add(item_id)
        normalized.append({"id": item_id, "statement": statement, "method": method,
                           "evidence": evidence, "required": bool(raw.get("required", True)),
                           "user_approved": False})
    if not any(item["required"] for item in normalized):
        raise AcceptanceError("至少需要一项必需验收条件")
    return {"revision": int(contract.get("revision") or 0) + 1,
            "approved_revision": 0, "items": normalized,
            "final_decision": "pending", "final_note": ""}


def approve(contract: Mapping[str, Any]) -> dict[str, Any]:
    updated = dict(contract)
    if not updated.get("items"):
        raise AcceptanceError("请先设定验收条件")
    updated["approved_revision"] = int(updated.get("revision") or 0)
    updated["items"] = [dict(item, user_approved=True) for item in updated["items"]]
    return updated


def on_plan_changed(contract: Mapping[str, Any], tasks: Iterable[Mapping[str, Any]],
                    *, runtime_required: bool = False) -> dict[str, Any]:
    """Keep user-written conditions but revoke approval after the task plan changes."""
    if not contract.get("items") or int(contract.get("revision") or 0) <= 1:
        proposed = from_tasks(tasks, runtime_required=runtime_required)
        proposed["revision"] = int(contract.get("revision") or 0) + 1
        return proposed
    updated = dict(contract)
    updated["revision"] = int(contract.get("revision") or 0) + 1
    updated["approved_revision"] = 0
    updated["items"] = [dict(item, user_approved=False) for item in contract["items"]]
    updated["final_decision"] = "pending"
    updated["final_note"] = ""
    return updated
