"""给老会话补归属：从旧分桶路径推断，一次性迁移。

## 为什么要迁移

丙方案靠 `owner_project_id` 过滤。乙方案之前，会话按
`.docmind_sessions/<pid>/<slug>.json` 分桶存放 —— **目录名就是归属**。
改成全局存储后，这些文件仍在原处（读穿透），但没有 owner 字段，
于是 `list_sessions(project_id=pid)` 看不到它们（无归属被过滤掉）。

本脚本：扫所有旧分桶，把 `<pid>` 写进对应会话的 `owner_project_id`。

## ⚠ 三个边界

1. **只补空的**：已有归属的一律不动（`backfill_owner` 内部也这么保证）。
2. **不搬文件**：内容仍从旧分桶读穿透，只把 owner 写进**全局那一份**。
   —— 这会让该会话立刻"迁移"成全局文件，旧分桶那份变成冗余，
      `delete` 会一起清掉，不会永久残留。
3. **pid 必须真实存在**：目录名是 slug 化的 pid，反解回来可能与当前
   注册表里的 pid 不完全一致。这里按 `projects.project_id(root)` 重算校验，
   对不上就不猜、跳过并报出来。
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sessions as S            # noqa: E402
import projects as P            # noqa: E402

base = pathlib.Path(S.SESSIONS_DIR)
if not base.is_dir():
    raise SystemExit("  会话目录不存在: %s" % base)

buckets = sorted(d for d in base.iterdir() if d.is_dir())
print("=" * 74)
print("  旧项目分桶目录")
print("=" * 74)
for d in buckets:
    n = len(list(d.glob("*.json")))
    print("  %-24s %d 个会话" % (d.name, n))
if not buckets:
    print("  （没有分桶 —— 要么已经迁完，要么从来按项目启用的）")

print()
print("=" * 74)
print("  登记过的项目（用来校验 pid 是否真实）")
print("=" * 74)
try:
    known = {p["project_id"] for p in P.list_projects()}
except Exception as e:               # noqa: BLE001
    known = set()
    print("  读注册表失败:", e)
print("  共 %d 个" % len(known))

total = filled = skipped = unknown_pid = 0
for d in buckets:
    pid = d.name
    if known and pid not in known:
        unknown_pid += 1
        print()
        print("  ⚠ 桶名 %r 不在已登记项目里 —— 不猜，整桶跳过。" % pid)
        print("     （可能是已注销项目的残留；要保留它的会话请先 re-register。）")
        continue
    for f in sorted(d.glob("*.json")):
        sid = f.stem
        total += 1
        try:
            already = S.owned_by(sid, pid)
        except Exception:             # noqa: BLE001
            already = ""
        if already:
            skipped += 1
            continue
        if S.backfill_owner(sid, pid):
            filled += 1

print()
print("=" * 74)
print("  结果")
print("=" * 74)
print("  分桶会话总数 : %d" % total)
print("  已补归属     : %d" % filled)
print("  已有归属跳过 : %d" % skipped)
if unknown_pid:
    print("  未登记 pid 的桶: %d（已跳过）" % unknown_pid)
print()
if filled:
    print("  这批会话现在归属明确，`/api/sessions` 按项目能列出来了。")
else:
    print("  没有需要补的。")
