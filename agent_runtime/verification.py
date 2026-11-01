"""Verification evidence shared by applications; absence of evidence is not success."""


def finalize_verification(result):
    """Normalize legacy checker output without counting skipped checks as executed."""
    result = dict(result)
    ran = result.get("ran") or []
    skipped = list(result.get("skipped") or [])
    executed = []
    for check in ran:
        if "skip" in str(check).lower():
            skipped.append(check)
        else:
            executed.append(check)
    failures = result.get("failures") or []
    if failures:
        status = "failed"
    elif skipped and executed:
        status = "partial"
    elif skipped or not executed:
        status = "skipped"
    else:
        status = "passed"
    result.update(ran=executed, skipped=skipped, status=status,
                  passed=status == "passed")
    return result


def verification_message(result, target):
    """返回 `(说明文本, 结论)`。**结论是三态**：True=通过 / False=校验失败 / None=没能校验。

    「没能校验」（该文件类型没有校验器、被跳过、只跑了一部分）不能记成失败：
    一次纯文档写入会因此变成一条 `fail` 回执，学习环拿到的全是假的负面证据
    （实测 30 天 3 条回执全是 `.md` 写入的这种情况）。反过来也绝不能当通过 ——
    没跑过就是不知道。
    """
    result = finalize_verification(result)
    status = result["status"]
    if status == "passed":
        return "自验证通过：%s（改动 %s 的已执行检查通过）" % (
            "；".join(map(str, result["ran"][:4])), target), True
    if status in ("skipped", "partial"):
        detail = "；".join(map(str, result["skipped"][:4])) or result.get("note", "无可执行检查")
        return "[自验证未完成] %s：%s。部分检查通过或跳过不等于验证通过，不要声称已完成。" % (target, detail), None
    lines = ["- [%s] %s: %s" % (f.get("scope"), f.get("file"), f.get("error"))
             for f in result["failures"][:6]]
    return "自验证未通过，请修复后重试（不要声称已完成）：\n" + "\n".join(lines), False
