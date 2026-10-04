"""从工作流证据生成脱敏的自我复盘。

复盘是对已发生事实的结构化总结，不允许把模型原始输出、工具参数或
凭据直接暴露到开发舱。它服务于长任务续跑和用户验收，因此所有结论都
带有来源语义：已验证、待用户确认或仍不确定。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

_SECRET = re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|private[_-]?key|authorization)\s*[:=]\s*[^\s,;]+")
_MAX = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(value: Any, limit: int = 360) -> str:
    text = str(value or "").replace("\x00", "").replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET.sub(lambda match: match.group(1) + "=[REDACTED]", text)
    return re.sub(r"\s+", " ", text)[:limit]


def _append(rows: list[str], value: Any, *, limit: int = _MAX) -> None:
    text = _text(value)
    if text and text not in rows and len(rows) < limit:
        rows.append(text)


def _review_wording(workflow_review: Mapping[str, Any] | None) -> str:
    """把复核结论写成一句**不撒谎**的话。

    ## 为什么不能直接读 `review["ok"]`

    `review["ok"]` 是 `agent_ok and project.ok` 的**与**，只有一个布尔，于是
    「无法验证」被翻译成了「有未通过项」。

    实测（六次真跑，`workflow_reflection.py` 旧第125 行**无条件**加那半句）：

        wf-f609ea8daf22  已完成 3/3 个任务；仍需检查未通过项   <- 非ok任务：无
        wf-fc3cf86c5eaf  已完成 4/4 个任务；仍需检查未通过项   <- 非ok任务：无
        wf-fd543f79c197  已完成 3/3 个任务；仍需检查未通过项   <- 非ok任务：无
        wf-e042489bf0fe  已完成 4/4 个任务；仍需检查未通过项   <- 非ok任务：无
        wf-af6a2e4a9b2b  已完成 4/4 个任务；仍需检查未通过项   <- 非ok任务：无

    `review.project.status` 是 `unverified`，四个原因**全都是「无法验证」**：

        试做区没有容器级命令隔离 / 变更文件超过单轮审核上限
        执行前快照有遗漏 / 项目有变更但未配置独立测试命令

    那里**没有任何未通过项**。「仍需检查未通过项」这句话是在编造事实，
    而且它和 `uncertainties` 里已经如实记着的「当前没有足够的独立验证证据」
    自相矛盾。

    ## 为什么只放行 `unverified`

    `independent_review.py` 的 status 取值里还有一个 **`failed`**
    （"独立测试失败或沙箱不可用"）——**那才是真的有东西没过**，必须照旧说
    「仍需检查未通过项」。所以判据是 status 而不是 `project.ok`：
    `ok=False` 有两种完全不同的含义，只有一种叫「未通过」。

    与 `24c845c` / `3a5f8dc` 同一族：同一个被压扁的布尔，这是第三个下游消费者。
    """
    review = workflow_review if isinstance(workflow_review, Mapping) else {}
    if review.get("ok") is True:
        return "工作流复核通过"
    project = review.get("project")
    project = project if isinstance(project, Mapping) else {}
    if review.get("agent_ok") is True and project.get("status") == "unverified":
        return "内容复核通过 · 未做独立验证"
    return "仍需检查未通过项"


def _result_map(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = state.get("results")
    if not isinstance(raw, Mapping):
        return {}
    values = raw.get("results") if isinstance(raw.get("results"), Mapping) else raw
    return {str(key): value for key, value in values.items() if isinstance(value, Mapping)}


def build_self_review(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build a bounded review projection from durable workflow evidence."""
    state = dict(state or {})
    result_map = _result_map(state)
    changed: list[str] = []
    verified: list[str] = []
    uncertainties: list[str] = []
    next_steps: list[str] = []
    task_count = len(result_map)
    ok_count = 0

    for task_id, result in list(result_map.items())[:64]:
        status = str(result.get("status") or "")
        if status in {"ok", "deduped"}:
            ok_count += 1
        if status in {"failed", "blocked"}:
            _append(uncertainties, f"任务 {task_id} 未完成（{status}）")
            _append(next_steps, f"检查任务 {task_id} 的失败证据后再决定是否重试")
        for item in list(result.get("file_changes") or [])[:8]:
            if isinstance(item, Mapping):
                _append(changed, item.get("path") or item.get("label") or item.get("kind"))
            else:
                _append(changed, item)
        for item in list(result.get("artifacts") or [])[:8]:
            if isinstance(item, Mapping):
                _append(changed, item.get("label") or item.get("path") or item.get("kind"))
        if result.get("conclusion") and status in {"ok", "deduped"}:
            _append(changed, f"任务 {task_id}：{result.get('conclusion')}")
        review = result.get("review") if isinstance(result.get("review"), Mapping) else {}
        for check in list(review.get("checks") or [])[:8]:
            if isinstance(check, Mapping) and check.get("ok"):
                _append(verified, check.get("name") or check.get("detail"))
        reflection = result.get("reflection") if isinstance(result.get("reflection"), Mapping) else {}
        for issue in list(reflection.get("issues") or [])[:8]:
            _append(uncertainties, f"任务 {task_id}：{issue}")
        _append(next_steps, reflection.get("next_step"))

    workflow_review = state.get("review") if isinstance(state.get("review"), Mapping) else {}
    for check in list(workflow_review.get("checks") or [])[:12]:
        if isinstance(check, Mapping):
            if check.get("ok"):
                _append(verified, check.get("name") or check.get("detail"))
            else:
                _append(uncertainties, f"复核未通过：{check.get('name') or check.get('detail')}")

    contract = state.get("acceptance_contract")
    contract_items = list(contract.get("items") or []) if isinstance(contract, Mapping) else []
    contract_decision = str((contract or {}).get("final_decision") or "pending") if isinstance(contract, Mapping) else "pending"
    if contract_items and contract_decision != "accepted":
        _append(uncertainties, "验收条件尚未得到用户最终确认")
        _append(next_steps, "请对照验收条件检查实际项目效果并提交最终验收")
    elif contract_decision == "accepted":
        _append(verified, "用户已确认最终验收")

    if state.get("preview", {}).get("artifacts") if isinstance(state.get("preview"), Mapping) else False:
        _append(verified, "已生成预览或证据 artifact")
    if state.get("capability_lease", {}).get("status") == "released" if isinstance(state.get("capability_lease"), Mapping) else False:
        _append(verified, "工具权限租约已释放")

    status = str(state.get("status") or "unknown")
    if status == "interrupted":
        _append(uncertainties, "工作流被暂停，尚未完成全部计划")
        _append(next_steps, "恢复工作流后从时间线中的未完成任务继续")
    if not changed and task_count:
        _append(changed, "已执行任务，但没有登记可展示的文件或预览变化")
    if not verified:
        _append(uncertainties, "当前没有足够的独立验证证据")
        _append(next_steps, "补充自动测试、运行结果或视觉证据")
    if not next_steps and status in ("completed", "completed_unverified"):
        _append(next_steps, "由用户完成最终验收；如不满足条件，提交具体反馈继续修改")

    confidence = 0.0
    if task_count:
        confidence = ok_count / task_count
    if workflow_review.get("ok") is True:
        confidence = min(1.0, confidence + 0.2)
    # 注意 `confidence` 这里**刻意**继续读压扁的 `review["ok"]`：`unverified`
    # 让它拿不到那 +0.2，是**对的**——没有独立验证就该少一点自信。
    # 被改掉的是 summary 那句**陈述事实**的话（见 `_review_wording`）：
    # 少一点自信是判断，宣称「有未通过项」是造谣，两者不该一起改。
    if uncertainties:
        confidence = max(0.0, confidence - min(0.4, len(uncertainties) * 0.05))
    return {
        "status": "ready" if status in {"completed", "completed_unverified", "failed", "interrupted"} else "in_progress",
        "generated_at": _now(),
        "source": "execution_evidence",
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "summary": _text(
            "已完成 %d/%d 个任务；%s" % (ok_count, task_count, _review_wording(workflow_review)),
            240,
        ),
        "changed": changed[:_MAX],
        "verified": verified[:_MAX],
        "uncertainties": uncertainties[:_MAX],
        "next_steps": next_steps[:_MAX],
    }


__all__ = ["build_self_review"]
