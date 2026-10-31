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
from . import index, metrics, migrate, promote


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
    else:
        out = metrics.report(args.days)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.cmd == "eval":
        return 0 if out.get("ok") else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
