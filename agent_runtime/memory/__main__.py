"""命令行：python -m agent_runtime.memory rebuild [--verify] | stats"""
from __future__ import annotations

import argparse
import json

from . import index


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agent_runtime.memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rebuild_cmd = sub.add_parser("rebuild", help="扫文件重建索引（幂等）")
    rebuild_cmd.add_argument("--verify", action="store_true", help="重建后校验条目数")
    sub.add_parser("stats", help="查看索引统计")
    args = parser.parse_args(argv)
    if args.cmd == "rebuild":
        out = index.rebuild(verify=args.verify)
    else:
        out = index.stats()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
