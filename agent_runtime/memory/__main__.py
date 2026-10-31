"""命令行：
python -m agent_runtime.memory rebuild [--verify] | stats
python -m agent_runtime.memory sweep --project prj-xxx
python -m agent_runtime.memory promote --project prj-xxx --id mem-xxxx
python -m agent_runtime.memory migrate [--force]
python -m agent_runtime.memory report [--days 7]
"""
from __future__ import annotations

import argparse
import json

from . import eval as eval_mod
from . import health as health_mod
from . import index, metrics, migrate, promote


def _current_project(value=""):
    if value:
        return str(value)
    try:
        import projects
        return projects.current_project_id() or "default"
    except Exception:  # noqa: BLE001
        return "default"


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agent_runtime.memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rebuild_cmd = sub.add_parser("rebuild", help="扫文件重建索引（幂等）")
    rebuild_cmd.add_argument("--verify", action="store_true", help="重建后校验条目数")
    sub.add_parser("stats", help="查看索引统计")
    sweep_cmd = sub.add_parser("sweep", help="扫描项目候选并按晋升门晋升")
    sweep_cmd.add_argument("--project", required=True)
    dedupe_cmd = sub.add_parser("dedupe", help="合并历史近似候选")
    dedupe_cmd.add_argument("--project", required=True)
    promote_cmd = sub.add_parser("promote", help="晋升单条候选")
    promote_cmd.add_argument("--project", required=True)
    promote_cmd.add_argument("--id", required=True)
    migrate_cmd = sub.add_parser("migrate", help="只读迁移旧版记忆库")
    migrate_cmd.add_argument("--force", action="store_true")
    report_cmd = sub.add_parser("report", help="固化开销计量")
    report_cmd.add_argument("--days", type=int, default=7)
    eval_cmd = sub.add_parser("eval", help="记忆专用评测（检索/晋升/去重/隔离）")
    eval_cmd.add_argument("--semantic", action="store_true", help="叠加真实嵌入语义排序")
    health_cmd = sub.add_parser("health", help="记忆健康快照")
    health_cmd.add_argument("--project", default="")
    maintain_cmd = sub.add_parser("maintain", help="去重/失效归档/重建索引（默认 dry-run）")
    maintain_cmd.add_argument("--project", default="")
    maintain_cmd.add_argument("--apply", action="store_true")
    maintain_cmd.add_argument("--retention-days", type=int, default=90)
    maintain_cmd.add_argument("--threshold", type=float, default=0.88)
    invalidate_cmd = sub.add_parser("invalidate", help="把共享条目标为被另一条取代")
    invalidate_cmd.add_argument("--old", required=True)
    invalidate_cmd.add_argument("--new", required=True)
    invalidate_cmd.add_argument("--reason", default="user")
    args = parser.parse_args(argv)

    if args.cmd == "rebuild":
        out = index.rebuild(verify=args.verify)
    elif args.cmd == "stats":
        out = index.stats()
    elif args.cmd == "sweep":
        out = promote.sweep(args.project)
    elif args.cmd == "dedupe":
        out = promote.dedupe_candidates(args.project)
    elif args.cmd == "promote":
        out = promote.promote(args.project, args.id)
    elif args.cmd == "migrate":
        out = migrate.run(force=args.force)
    elif args.cmd == "eval":
        out = eval_mod.run(semantic=args.semantic)
    elif args.cmd == "health":
        out = health_mod.report(_current_project(args.project))
    elif args.cmd == "maintain":
        out = health_mod.maintain(
            _current_project(args.project), apply=args.apply,
            retention_days=args.retention_days, threshold=args.threshold)
    elif args.cmd == "invalidate":
        old = promote.supersede_shared(args.old, args.new, reason=args.reason)
        out = {"ok": bool(old), "old": args.old, "new": args.new}
        if old is None:
            out["error"] = "未找到两条 active 共享条目"
    else:
        out = metrics.report(args.days)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.cmd == "eval":
        return 0 if out.get("ok") else 1
    if args.cmd == "invalidate" and not out.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
