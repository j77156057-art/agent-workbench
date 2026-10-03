"""Structured final-output audit for workflow and Subagent results."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|private[_-]?key)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SENSITIVE_TERMS = re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\b")
_LOCATION_QUERY = re.compile(
    r"(?i)(?:在哪|哪个文件|文件位置|路径|行号|定位|逻辑|实现|定义在哪|where|which file|line)"
)
#: 从结论正文里切「文件引用」。
#:
#: 前置断言用**显式的 ASCII 路径字符集**，而不是 `\w`——
#: Python 3 的 `\w` 在 str 模式下**包含 CJK**，于是「数值在behaviors/enemy.gd:7」
#: 这种紧贴中文的写法会因为前面的「在」被判成词字符而拒绝匹配，引擎一路挪到
#: `enemy.gd` 前（前面是 `/`，不算 `\w`）才咬住，于是把**正确**的引用
#: `behaviors/enemy.gd` 切成了 `enemy.gd` -> 报「路径打不开」。
#:
#: 实测（godot_sample 定位扣血逻辑）第一轮就是这样被判 references_exist 失败的：
#: 模型写对了，审计切错了，于是白白多花一轮工具调用去"修路径"。
#: 中文是本项目的主要语言，这个断言几乎必然踩中。
#:
#: 改成 ASCII 路径字符集后仍能挡住「路径后缀」误匹配：
#: `behaviors/enemy.gd` 里的 `enemy.gd` 前面是 `/`，在集合内 -> 被拒。
_FILE_REF = re.compile(
    r"(?<![A-Za-z0-9_./\\-])([A-Za-z0-9_./\\-]+\.(?:gd|py|ts|tsx|js|jsx|vue|cs|cpp|cc|h|hpp|json|md))"
    r"(?:\s*:\s*L?(\d+)(?:\s*[-~]\s*L?(\d+))?)?"
)


def _trace_steps(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = (result.get("trace") or {}).get("steps")
    if raw is None:
        raw = result.get("steps") or []
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, (list, tuple)) else []


def audit_evidence(result: Mapping[str, Any] | None, *, question: str = "",
                   code_root: str = "", require_read: bool | None = None) -> dict[str, Any]:
    """Check that a location answer is backed by a readable source file.

    This is intentionally a narrow, deterministic gate.  It does not claim to
    understand the semantics of a function; it verifies the minimum evidence
    contract first: location questions need a file reference, a successful
    ``read_file`` step, and line numbers that exist in the current code root.
    Non-location answers are left untouched unless ``require_read`` is true.
    """
    result = dict(result or {})
    text = str(result.get("conclusion") or result.get("text") or result.get("merged") or "")
    location_question = bool(_LOCATION_QUERY.search(str(question or "")))
    required = location_question if require_read is None else bool(require_read)
    steps = _trace_steps(result)
    actions = [str(item.get("action") or item.get("tool") or "").strip() for item in steps]
    read_steps = [item for item, action in zip(steps, actions) if action == "read_file"]
    refs = []
    for match in _FILE_REF.finditer(text):
        refs.append({"path": match.group(1).replace("\\", "/"),
                     "start_line": int(match.group(2)) if match.group(2) else None,
                     "end_line": int(match.group(3) or match.group(2)) if match.group(2) else None})
    file_checks = []
    root = Path(code_root).resolve() if code_root else None
    for ref in refs[:50]:
        path = Path(ref["path"])
        if root is not None and not path.is_absolute():
            path = (root / path).resolve()
        exists = False
        in_root = True
        line_ok = ref["start_line"] is None
        try:
            exists = path.is_file()
            if root is not None:
                in_root = path == root or root in path.parents
            if exists and ref["start_line"] is not None:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    line_count = sum(1 for _ in handle)
                line_ok = 1 <= ref["start_line"] <= line_count and ref["end_line"] <= line_count
        except (OSError, ValueError):
            exists = False
            line_ok = False
        file_checks.append(dict(ref, exists=exists, in_root=in_root, line_ok=line_ok))
    # A trace can be supplied by a workflow runner or by a direct Agent.  A
    # successful flag is the only portable signal; failure observations remain
    # evidence of an attempted read, not proof of verification.
    successful_read = any(item.get("ok", True) is not False for item in read_steps)
    valid_refs = [item for item in file_checks if item["exists"] and item["in_root"] and item["line_ok"]]
    checks = {
        "location_question": location_question,
        "reference_present": bool(refs) if required else True,
        "read_file_present": successful_read if required else True,
        "references_exist": all(item["exists"] and item["in_root"] for item in file_checks) if refs else not required,
        "reference_lines_valid": all(item["line_ok"] for item in file_checks) if refs else not required,
    }
    # ---- 门禁项 vs 参考项 -------------------------------------------------
    #
    # ``read_file_present``（「这一轮有没有调 read_file」）是**过程**指标，不是**事实**
    # 指标，而且已被证明不可靠：本次会话连续四次误报——并行读取看不见、完整读取看不见、
    # 连上了却被路径切分错误连累。它衡量的是 harness 自己的记录能力，不是答案质量。
    #
    # Claude Code 的验证 prompt 把这一点写成了明文：
    #     "The code looks correct based on my reading" — reading is not verification. Run it.
    # 即「读过」根本不被当作证据。拿它当闸门不只是多余，是**方向反了**。
    #
    # 而 ``references_exist`` / ``reference_lines_valid`` 是纯文件系统判定：路径能不能
    # 打开、行号是否越界。它们回答的是「你的引用能不能被核验」，属于**可核验性**，
    # 拦的是真错误（幻觉路径、越界行号），且无观察到的误报。
    #
    # 所以：门禁只看事实项；过程项降级为参考，不再驱动闸门与用户可见警告。
    gate_checks = {k: v for k, v in checks.items() if k != "read_file_present"}
    gate_ok = bool(all(v for k, v in gate_checks.items() if k != "location_question"))
    # PARTIAL 只留给**环境限制**（无法核验），不给「我不确定」当逃逸口——
    # 这是 Claude Code 明确设计过的约束（"PARTIAL is for environmental limitations
    # only ... not for 'I'm unsure whether this is a bug'"）。
    verdict = "PASS"
    if not gate_ok:
        # 能查、且查出了问题 -> FAIL。这是事实问题，不是"不确定"。
        verdict = "FAIL"
    elif required and not successful_read:
        # 引用全部可核验，但本轮没有成功读取记录：结论仍可用（引用能被打开且行号有效），
        # 只是没有"本轮读过"这一条旁证。归为 PARTIAL 而非 FAIL。
        verdict = "PARTIAL"
    # Without a project root, do not manufacture a filesystem failure; the
    # read_file trace is still useful and the caller can surface this gap.
    if required and refs and not root:
        checks["references_exist"] = successful_read
        checks["reference_lines_valid"] = successful_read
        checks["read_file_present"] = True
        # 没有代码根 = **环境限制**，不是答案有问题。按三值判定的定义这才是 PARTIAL
        # 的正当用途；此时不能声称 FAIL（那是把环境问题说成内容问题）。
        gate_checks["references_exist"] = True
        gate_checks["reference_lines_valid"] = True
        verdict = "PARTIAL" if successful_read else "FAIL"
    return {"ok": bool(all(value for key, value in checks.items()
                             if key != "location_question")),
            # 只看事实项的判定结果。调用方应当用它驱动闸门与用户可见结论，
            # ``ok`` 保留给需要"过程也达标"的旧调用方。
            "gate_ok": gate_ok,
            "gate_checks": gate_checks,
            "verdict": verdict,
            "required": required, "checks": checks,
            "references": file_checks, "verified_references": valid_refs,
            "read_steps": len(read_steps)}


def redact(text: Any) -> str:
    value = str(text or "").replace("\x00", "").strip()
    for pattern in _CREDENTIAL_PATTERNS[:1]:
        value = pattern.sub(lambda match: "%s=[REDACTED]" % match.group(1), value)
    value = _CREDENTIAL_PATTERNS[1].sub("[REDACTED]", value)
    value = _CREDENTIAL_PATTERNS[2].sub("[PRIVATE_KEY_REDACTED]", value)
    return value[:8000]


def audit_final_output(result: Mapping[str, Any] | None, *, max_tool_failures: int = 3,
                       question: str = "", code_root: str = "",
                       require_evidence: bool | None = None) -> dict[str, Any]:
    result = dict(result or {})
    raw_text = str(result.get("conclusion") or result.get("text") or result.get("merged") or "")
    credential_detected = any(pattern.search(raw_text) for pattern in _CREDENTIAL_PATTERNS)
    text = redact(raw_text)
    raw_steps = (result.get("trace") or {}).get("steps")
    if raw_steps is None:
        raw_steps = result.get("steps") or []
    steps = list(raw_steps) if isinstance(raw_steps, (list, tuple)) else []
    tool_failures = [step for step in steps
                     if isinstance(step, Mapping) and step.get("ok") is False]
    file_changes = result.get("file_changes") or result.get("files") or []
    tests = result.get("tests") or result.get("test_results") or []
    if isinstance(file_changes, Mapping):
        file_changes = [file_changes]
    if isinstance(tests, Mapping):
        tests = [tests]
    failed_tests = [item for item in tests if isinstance(item, Mapping) and item.get("ok") is False]
    evidence = audit_evidence(result, question=question, code_root=code_root,
                              require_read=require_evidence)
    checks = {
        "has_result": bool(text),
        "no_replacement_chars": "\ufffd" not in text,
        "credentials_absent": not credential_detected,
        "sensitive_terms_without_value": not bool(_SENSITIVE_TERMS.search(text)) or "[REDACTED]" in text,
        "tool_failures_below_limit": len(tool_failures) < max(1, int(max_tool_failures)),
        "status_ok": result.get("status", "ok") == "ok",
        "file_evidence_bounded": len(file_changes) <= 100,
        "test_evidence_bounded": len(tests) <= 100,
        "tests_ok": not failed_tests,
        "evidence_verified": evidence["ok"],
    }
    failures = []
    if not checks["credentials_absent"]:
        failures.append({"kind": "credential", "message": "输出疑似包含凭据，已阻断", "recovery": "删除凭据后重新生成并轮换已暴露密钥"})
    if not checks["no_replacement_chars"]:
        failures.append({"kind": "encoding", "message": "输出包含乱码替换字符", "recovery": "重新读取原文并使用 UTF-8 输出"})
    if tool_failures:
        failures.append({"kind": "tool", "message": "%s 个工具步骤失败" % len(tool_failures),
                         "recovery": "检查失败工具证据，切换替代工具或重新规划"})
    if failed_tests:
        failures.append({"kind": "test", "message": "%s 个测试失败" % len(failed_tests),
                         "recovery": "先修复失败测试，再重新执行验证任务"})
    if not evidence["ok"]:
        failures.append({"kind": "evidence", "message": "定位回答缺少可核验的文件原文或有效行号",
                         "recovery": "先用 search_code 定位，再用 read_file 读取原文；无法确认时明确说明"})
    return {"ok": bool(all(checks.values())), "checks": checks, "text": text,
            "failures": failures, "tool_failure_count": len(tool_failures),
            "failed_test_count": len(failed_tests),
            "evidence_audit": evidence,
            "evidence": {"files": list(file_changes)[:100], "tests": list(tests)[:100],
                         "tools": steps[-100:]},
            "sensitive_redacted": "[REDACTED]" in text}


__all__ = ["audit_evidence", "audit_final_output", "redact"]
