"""宪法条款 → 真实代码闸门的绑定表。

这里不是第二套规则引擎，而是**审计与回归用的单一清单**：
- 宪法加载时校验每条 `machine_check` 都有落点；未绑定的条款会在面板/ledger 显式提示；
- 测试逐条调用真实闸门，证明条款不是只躺在提示词里。

真正的执行仍在各自模块：命令黑名单在 tools，回执门在 taskbook，脱敏在 promote，
删除确认在 api。
"""
from __future__ import annotations

GATES = {
    "git_push_requires_approval": {
        "clause": "C-001", "where": "tools._cmd_is_blocked", "kind": "command",
    },
    "git_commit_requires_approval": {
        "clause": "C-002", "where": "tools._cmd_is_blocked", "kind": "command",
    },
    "done_requires_receipt": {
        "clause": "C-003", "where": "taskbook.complete", "kind": "evidence",
    },
    "constitution_readonly": {
        "clause": "C-004", "where": "tools (no write path)", "kind": "readonly",
    },
    "secret_rejected": {
        "clause": "C-005", "where": "promote.secret_hit", "kind": "scrub",
    },
    "destructive_needs_confirm": {
        "clause": "C-006", "where": "tools._cmd_is_blocked", "kind": "command",
    },
    "promotion_scrubbed": {
        "clause": "C-007", "where": "promote.genericize", "kind": "scrub",
    },
    "memory_delete_needs_confirm": {
        "clause": "C-008", "where": "api delete endpoints", "kind": "confirm",
    },
}


def missing_checks(clauses):
    """返回没有绑定代码闸门的条款 id（空 = 全部可机检）。"""
    missing = []
    for clause in clauses or []:
        if not isinstance(clause, dict):
            continue
        check = str(clause.get("machine_check") or "").strip()
        if check and check not in GATES:
            missing.append(str(clause.get("id") or check))
    return missing


def require_confirm(confirm, action):
    if not confirm:
        raise ValueError("%s需要 confirm=true" % action)
