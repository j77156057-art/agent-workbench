# -*- coding: utf-8 -*-
"""反向验证：撤掉 pinned 验收要求对应的修复，确认用例确实会变红。

覆盖：黄金 20 / 21 / 22；需求 5（must-keep 结构化）；需求 6（负例拒绝）。
运行：
    .venv\\Scripts\\python.exe verify_shots\\_reverse_pinned.py
只读仓库代码，工作目录用临时目录，不碰真实会话库。
"""
import importlib.util
import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TMP = tempfile.mkdtemp(prefix="dm_reverse_pinned_")
os.environ["DOCMIND_STATE_ROOT"] = os.path.join(TMP, "state")
os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(TMP, "memory")
os.environ["DOCMIND_MEMORY_DB"] = os.path.join(TMP, "memory.sqlite3")
os.environ["DOCMIND_SESSIONS_DIR"] = os.path.join(TMP, "sessions")
os.environ["DOCMIND_TURN_TRACE_DIR"] = os.path.join(TMP, "traces")
for _key in ("DOCMIND_MEMORY_EMBED", "DOCMIND_MEMORY_ENHANCED",
             "DOCMIND_MEMORY_DISTILL", "DOCMIND_MEMORY_LEARN"):
    os.environ[_key] = "0"

_spec = importlib.util.spec_from_file_location(
    "golden_runner", os.path.join(REPO, "tests", "golden", "memory", "runner.py"))
runner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

MODULES = runner._load_modules()
sessions = MODULES["sessions"]
CASES = os.path.join(REPO, "tests", "golden", "memory", "cases")


def load_case(name):
    with open(os.path.join(CASES, name + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


def run_golden(name):
    case = load_case(name)
    try:
        observations = runner._SCENARIOS[case["scenario"]](case, MODULES)
    except Exception as exc:  # noqa: BLE001 —— 反向验证里异常也算变红
        return [{"ok": False, "detail": "%s: %s" % (type(exc).__name__, exc)}]
    results = runner._eval_assertions(observations, case["assertions"])
    return [item for item in results if not item.get("ok")]


def report(label, failures):
    if failures:
        print("[RED   ] %-28s -> %d 条断言失败；首条：%s"
              % (label, len(failures), failures[0].get("detail")))
    else:
        print("[GREEN!] %-28s -> 反向验证无效（用例没变红）" % label)


print("== 基线（未撤修复）==")
for _name in ("pinned_model_no_delete", "pinned_tombstone", "pinned_monotonic"):
    _fail = run_golden(_name)
    print("%-28s %s" % (_name, "PASS" if not _fail else "FAIL"))

# ---------------------------------------------------------------------------
# 20：撤掉“只增不减”守卫 + 放开模型删除权限
# ---------------------------------------------------------------------------
_o_merge = sessions._merge_pinned_entries
_o_delete = sessions.delete_pinned


def loose_merge(existing, incoming, actor="model", origin="", tombstones=()):
    merged, info = _o_merge(existing, incoming, actor=actor, origin=origin,
                            tombstones=tombstones)
    if actor != "user" and len(incoming or []) < len(existing or []):
        # 模拟旧行为：上游给了更短的列表就整批覆盖。
        merged = [sessions._normalize_pinned_item(e) for e in incoming or []]
        info = dict(info, shrink_rejected=False, total=len(merged))
    return merged, info


def loose_delete(entry_id, actor="user", **kwargs):
    return {"ok": True, "deleted": True, "reason": "", "id": entry_id}


print("\n== 反向验证 20：放开 shrink 守卫与模型删除权限 ==")
sessions._merge_pinned_entries = loose_merge
sessions.delete_pinned = loose_delete
try:
    report("20 pinned_model_no_delete", run_golden("pinned_model_no_delete"))
finally:
    sessions._merge_pinned_entries = _o_merge
    sessions.delete_pinned = _o_delete

# ---------------------------------------------------------------------------
# 21：撤掉墓碑（规范化后为空）
# ---------------------------------------------------------------------------
_o_tomb = sessions._normalize_tombstones

print("\n== 反向验证 21：让墓碑读出来永远为空 ==")
sessions._normalize_tombstones = lambda value: []
try:
    report("21 pinned_tombstone", run_golden("pinned_tombstone"))
finally:
    sessions._normalize_tombstones = _o_tomb

# ---------------------------------------------------------------------------
# 22：恢复旧行为（pinned 满了就把最旧条目踢出去）
# ---------------------------------------------------------------------------
print("\n== 反向验证 22：满配额时淘汰最旧条目（旧 escape hatch）==")


def evicting_merge(existing, incoming, actor="model", origin="", tombstones=()):
    merged, info = _o_merge(existing, incoming, actor=actor, origin=origin,
                            tombstones=tombstones)
    if actor != "user" and len(merged) > 4:
        merged = merged[1:]
        info = dict(info, total=len(merged))
    return merged, info


sessions._merge_pinned_entries = evicting_merge
try:
    report("22 pinned_monotonic", run_golden("pinned_monotonic"))
finally:
    sessions._merge_pinned_entries = _o_merge

# ---------------------------------------------------------------------------
# 需求 5：must-keep 退回“直接追加到文本尾部”
# ---------------------------------------------------------------------------
_o_must_keep = sessions._append_must_keep

print("\n== 反向验证 需求5：must-keep 退回纯文本尾部追加 ==")


def plain_tail_append(summary, turns, limit=None):
    text = str(summary or "")
    pending = [str(t.get("user") or "") for t in (turns or [])
               if isinstance(t, dict) and str(t.get("user") or "").strip()]
    if not pending:
        return text
    return (text + "\n\n" + "\n".join(pending)).strip()


sessions._append_must_keep = plain_tail_append
try:
    prev = sessions._render_summary("必须保持 Python 3.9 兼容。", "", "")
    out = sessions.maybe_compact("rev-req5", [
        {"user": "【未完成现场 must-keep】索引构建未完，下一轮继续。",
         "assistant": "收到。"}], llm=None, trigger_tokens=1, keep_tokens=1,
        prompt_budget=4000)
    entries = sessions.load("rev-req5").get("pinned") or []
    struct_ok = any("must-keep" in (e.get("text") or "")
                    and e.get("source") and e.get("added_at")
                    and e.get("actor") == "model" for e in entries)
    if struct_ok:
        print("[GREEN!] 需求5 -> 反向验证无效（仍是结构化条目）")
    else:
        print("[RED   ] 需求5 -> 未生成带 source/added_at/actor 的 pinned 条目"
              "（pinned=%d 条）" % len(entries))
finally:
    sessions._append_must_keep = _o_must_keep

# ---------------------------------------------------------------------------
# 需求 6：撤掉收紧判定（任何含义务词的行都算硬事实）
# ---------------------------------------------------------------------------
_o_is_hard = sessions._is_hard_entry

print("\n== 反向验证 需求6：硬事实判定退回“只看义务词” ==")


def loose_is_hard(entry):
    text = str(entry or "")
    return bool(text.strip()) and bool(
        sessions._HARD_FACT_RE.search(text))


sessions._is_hard_entry = loose_is_hard
try:
    prev = sessions._render_summary("必须保持 Python 3.9 兼容。", "", "")
    negative = "旧代码保持原样，仅用于对比，不是长期规则。"
    out = sessions._merge_summary(prev, negative, None, 1200)
    parsed = sessions._parse_summary(out)
    if "旧代码保持原样" in parsed["pinned"]:
        print("[RED   ] 需求6 -> 负例被吸进 pinned")
    else:
        print("[GREEN!] 需求6 -> 反向验证无效（负例仍在 rolling）")
finally:
    sessions._is_hard_entry = _o_is_hard

# ---------------------------------------------------------------------------
# 压测断言 1：旧 pinned 必须逐字节成为新 pinned 的前缀，增长只追加在尾部
# 三种破坏各做一次：改旧前缀一个字节 / 截断旧前缀尾部 / 重排旧前缀三段。
# ---------------------------------------------------------------------------
print("\n== 反向验证 断言1：改写旧 pinned 前缀（改字节 / 截断 / 重排）==")


def prefix_break_merge(mode):
    calls = {"n": 0, "done": False}

    def wrapper(existing, incoming, *, actor="model", origin="", tombstones=()):
        merged, info = _o_merge(existing, incoming, actor=actor, origin=origin,
                                tombstones=tombstones)
        calls["n"] += 1
        if not calls["done"] and calls["n"] > 1 and merged:
            if mode == "byte":
                text = str(merged[0].get("text") or "")
                merged[0]["text"] = ("Z" if not text.startswith("Z") else "Q") + text[1:]
                calls["done"] = True
            elif mode == "truncate":
                merged[0]["text"] = str(merged[0].get("text") or "")[:-5]
                calls["done"] = True
            elif mode == "reorder" and len(merged) >= 3:
                merged = [merged[1], merged[0]] + merged[2:]
                calls["done"] = True
        return merged, info

    return wrapper


for _mode in ("byte", "truncate", "reorder"):
    _case = load_case("pinned_monotonic")
    _case["session_id"] = "rev-prefix-" + _mode
    _case["rounds"] = 8
    sessions._merge_pinned_entries = prefix_break_merge(_mode)
    try:
        _obs = runner._SCENARIOS["pinned_monotonic"](_case, MODULES)
        _caught = not (_obs["old_prefix_verbatim"] and _obs["append_at_tail_only"])
        if _caught:
            print("[RED   ] 断言1/%s -> 旧前缀被破坏已判红（prefix=%s tail=%s）"
                  % (_mode, _obs["old_prefix_verbatim"], _obs["append_at_tail_only"]))
        else:
            print("[GREEN!] 断言1/%s -> 反向验证无效（旧前缀被破坏仍未判红）" % _mode)
    finally:
        sessions._merge_pinned_entries = _o_merge

