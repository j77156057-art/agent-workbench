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
#:
#: ## 扩展名表为什么按「长的在前」排
#:
#: 实测（`wf-2a548e060abf` 的 `design_retry`）：模型交出 3769 字符的设计说明，
#: 引用了 18 处**完全正确**的路径，却整份被 `review_output` 拒掉——
#: `evidence_verified` 判红的真正原因是
#:
#:     "path": "behaviors/player.ts",  "exists": false
#:
#: **`.tscn` 被咬成 `.ts`**。原表里有 `ts` 却没有 `tscn`（Godot 场景文件，
#: 本项目最核心的一类），正则匹配到 `ts` 就结束，剩下的 `cn` 被丢掉。
#: 于是审计拿着一条**模型没写过的路径**说「路径打不开」。
#:
#: 同一张表里 `ts|tsx` 的顺序还有同一个毛病：`.tsx` 也会被咬成 `.ts`。
#: 所以两处一起修：表按长度降序排，且尾部加 `(?![A-Za-z0-9_])` 兜底——
#: 就算将来又有人往表里插一个前缀相同的扩展名，也不会再截断。
#:
#: 这已经是同一族里的第三次了（上一轮：`behaviors/enemy.gd` 被切成 `enemy.gd`）。
#: **教训**：凡是「正则从正文里切路径」的地方，扩展名表就是**判据的一部分**，
#: 补一条扩展名不是加个词，是让一类引用从「误判不存在」变成「真的去核验」。
_FILE_REF = re.compile(
    r"(?<![A-Za-z0-9_./\\-])([A-Za-z0-9_./\\-]+\.(?:tscn|tsx|import|gdshader|gdignore"
    r"|tres|godot|json|jsonc|yaml|yml|toml|html|css|scss|vue|jsx|js|ts|py|pyi|md"
    r"|txt|cfg|xml|sh|bat|ps1|sql|csv|cpp|cc|cxx|hpp|h|java|kt|go|rs|swift|cs|gd"
    r"|svg|png|jpe?g|gif|webp|ico|mp3|wav|ogg|ttf|woff2?))"
    r"(?![A-Za-z0-9_])"
    r"(?:\s*:\s*L?(\d+)(?:\s*[-~]\s*L?(\d+))?)?"
)


def _tool_name(action: str) -> str:
    """从动作文本里取出工具名：`read_file(path: x)` -> `read_file`。

    工作流的 `_child_trace` 存的是带参数的整句（`traj` 里的
    `{"action": "read_file(path: project.godot)"}`），不是裸工具名。此前这里
    写的是 `action == "read_file"` 精确匹配，于是**工作流路径下 `read_steps`
    恒为空**，`read_file_present` 恒为 False——而 `evidence_verified` 用的正是
    含这一项的聚合。结果是每个工作流子代理的结论都被 Harness 复核拒掉，
    无论内容多好（实测 `wf-87c0df3224aa` 的 `design_probe`：4028 字符、每条都
    带 `文件:行号`，照样被拒）。
    """
    text = str(action or "").strip()
    head = text.split("(", 1)[0].strip()
    return head or text


def _strip_uri_scheme(path: str) -> str:
    """去掉 `res://` / `user://` 这类 URI 前缀，只留**相对代码根**的部分。

    实测（`wf-87c0df3224aa` 的 `design_probe`）：模型交出 4028 字符、每条都带
    `文件:行号` 的证据清单（`project.godot:6 config/name="StarVoyager"`…），
    又被整份拒掉，理由是 6 条引用「路径打不开」：

        //addons/docmind_bridge/docmind_bridge.gd
        //behaviors/enemy.tscn
        //scenes/Main.tscn
        …

    **模型写的是 Godot 原生的 `res://behaviors/player.gd`。** 正则的路径字符集
    含 `/`，于是从 `res:` 后面那两个斜杠开始咬，把 **scheme 吃进了路径**。
    结果是：模型**每写一个 Godot 原生路径，这条判据就误报一次**。

    这个项目是 Godot 优先的，`res://` 是它的母语——所以这不是边角情况，
    是主干路径。

    顺带把 Windows 的反斜杠与前导 `./` 也归一掉：同一个目录的两种写法不该
    被判成两个不同的引用。
    """
    text = str(path or "").replace("\\", "/").strip()
    # `res://x` / `user://x` / `file://x` -> `x`；只去 scheme，不动后面的内容。
    for scheme in ("res://", "user://", "file://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


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
    # 动作文本在工作流路径里是 **带参数的整句**（`_child_trace` 存的是
    # `traj` 里的 `{"action": "read_file(path: project.godot)"}`），而不是裸工具名。
    # 此前写的是 `action == "read_file"` 精确匹配，于是工作流里 `read_steps`
    # **恒为空**、`read_file_present` 恒为 False，而 `evidence_verified` 用的
    # 正是含这一项的聚合 —— 结果是**每个工作流子代理的结论都被 Harness 复核
    # 拒掉**，无论内容多好。实测 `wf-87c0df3224aa` 的 `design_probe`：
    # 4028 字符、每条都带 `文件:行号`，照样被拒。
    read_steps = [item for item, action in zip(steps, actions)
                  if _tool_name(action) == "read_file"]
    refs = []
    for match in _FILE_REF.finditer(text):
        # 存**归一后**的路径：判定（`exists` / `line_ok`）是拿它去做的，
        # 记录里若还留着 `res://` 的残渣，读到这条记录的人重新核验会得到
        # 不同答案——那就成了「报告与事实不一致」。
        refs.append({"path": _strip_uri_scheme(match.group(1)),
                     "start_line": int(match.group(2)) if match.group(2) else None,
                     "end_line": int(match.group(3) or match.group(2)) if match.group(2) else None})
    file_checks = []
    root = Path(code_root).resolve() if code_root else None
    for ref in refs[:50]:
        rel = ref["path"]
        path = Path(rel)
        if root is not None and not path.is_absolute():
            path = (root / path).resolve()
        exists = False
        in_root = True
        # 行号检查只在**能打开文件**时才有意义。打不开时 line_count 从未数过，
        # 此时判 line_ok=False 会让 `reference_lines_valid` 与 `references_exist`
        # 一起失败，于是纠偏文案会说「行号超出了文件实际行数」——**而我们根本没数过
        # 行数**。那条建议模型无法执行，也是不该给出的。
        # （实测：模型写裸文件名 enemy.gd 时，两条失败同时出现。）
        # 所以：打不开 -> 行号检查标记为「不适用」，只报路径问题。
        line_ok = True
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
            line_ok = True  # 同上：打不开 -> 行号问题不适用，不是「越界」
        file_checks.append(dict(ref, exists=exists, in_root=in_root, line_ok=line_ok))
    # A trace can be supplied by a workflow runner or by a direct Agent.  A
    # successful flag is the only portable signal; failure observations remain
    # evidence of an attempted read, not proof of verification.
    successful_read = any(item.get("ok", True) is not False for item in read_steps)
    valid_refs = [item for item in file_checks if item["exists"] and item["in_root"] and item["line_ok"]]
    # 裸文件名（`player.tscn`，不带目录）**照常判定**：它在代码根下确实不
    # 存在，那就是一个打不开的引用。本文件曾一度把它们从判定里摘出去，理由
    # 是「我们没去找它在哪一份」——**那是把一条关于行号的规矩挪用到路径存在性
    # 上了**。既有测试已经把裸文件名钉成「打不开且必须拦住门禁」
    # （`test_a_bare_basename_is_caught_as_an_unopenable_reference`），其原则
    # 出自 Claude Code：「If you can run the check, you must decide PASS or FAIL」——
    # 这条检查跑得了，所以必须判。
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
    #
    # 两种 PARTIAL 的**含义完全不同**，措辞必须分开：
    #   no_root       —— 没有代码根，**根本无法检查**，一个字都不能说；
    #   no_read_record—— 引用已逐一核实通过，只是本轮没有读取记录作为旁证。
    # 早先两者共用「环境限制，无法核验」一句话，于是第二种明明核实过了却被说成
    # "无法核验"；更糟的是第一种会把每一条引用都列成「未通过（路径打不开）」——
    # 我们压根没检查过，凭什么断言路径有问题。**在说一件它不知道的事。**
    verdict = "PASS"
    verdict_reason = ""
    if not gate_ok:
        verdict = "FAIL"
    elif required and not successful_read:
        verdict = "PARTIAL"
        verdict_reason = "no_read_record"
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
        verdict_reason = "no_root"
    return {"ok": bool(all(value for key, value in checks.items()
                             if key != "location_question")),
            # 只看事实项的判定结果。调用方应当用它驱动闸门与用户可见结论，
            # ``ok`` 保留给需要"过程也达标"的旧调用方。
            "gate_ok": gate_ok,
            "gate_checks": gate_checks,
            "verdict": verdict,
            "verdict_reason": verdict_reason,
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
        # 这里用 `ok`（含 `read_file_present`）是**刻意的**，不是漏改。
        #
        # `audit_evidence` 里另有 `gate_ok`（只看事实项）并注释说「调用方应当
        # 用它驱动闸门」，`test_verification_gate_reshape.py` 也是按那条原则
        # 写的；而 `test_output_audit.py::test_location_answer_requires_read_and_
        # valid_line` 要求「定位类问题必须真读过文件」。两个契约互相矛盾。
        #
        # 本轮实测站在**更严的一边**：`read_file_present` 之所以在工作流路径下
        # 恒为 False，是 `action == "read_file"` 精确匹配认不出
        # `read_file(path: x)` 这个真实形状——那是**bug**，修掉之后这一项
        # 本身就判得准了。既然 bug 已修，就没必要再把口径放宽：否则等于用
        # 「过程项不可靠」当理由，放过一个「模型没读过也能报出 文件:行号」的
        # 真实漏洞。
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
