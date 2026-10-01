"""跑变异电池：`python tests/mutation/run_all.py [--only vue,app_test] [--no-live] [-q]`

默认把现场层（真浏览器）也算上；`--no-live` 只跑纯层与门禁层，标了现场的变异记成
「本环境未验证」。退出码非零的三种情况：**有变异活下来**、**锚点与源码脱钩**、
**对照组红**（后者意味着"抓住"是环境造成的假象，直接不作数）。

CI 用 `--no-live`：那两层在 ubuntu 上跑得动，且已经把围栏、判定表、新鲜度、清理判据
全部钉住；现场层的缺口会打印出来而不是被算成通过。
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for row in (str(REPO), str(HERE)):
    if row not in sys.path:
        sys.path.insert(0, row)

from tests.mutation import engine  # noqa: E402


def discover(only: list[str] | None = None) -> list[Any]:
    names = sorted(p.stem[len("battery_"):] for p in HERE.glob("battery_*.py"))
    if only:
        wanted = set(only)
        unknown = wanted - set(names)
        if unknown:
            raise SystemExit("没有这些电池：%s（现有：%s）" % (",".join(sorted(unknown)),
                                                              ",".join(names)))
        names = [n for n in names if n in wanted]
    return [importlib.import_module("tests.mutation.battery_%s" % n) for n in names]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑变异电池")
    parser.add_argument("--only", default="", help="逗号分隔的电池名，默认全跑")
    parser.add_argument("--no-live", action="store_true",
                        help="不跑现场层（真浏览器）；相关变异记成未验证")
    parser.add_argument("-q", "--quiet", action="store_true", help="只报汇总")
    parser.add_argument("--list", action="store_true", help="列出电池名后退出")
    args = parser.parse_args(argv)
    batteries = discover([row for row in args.only.split(",") if row.strip()])
    if args.list:
        print("\n".join(b.BATTERY_NAME for b in batteries))
        return 0
    started = time.monotonic()
    reports = []
    for battery in batteries:
        if not args.quiet:
            print("=== %s（%d 条变异%s）===" % (
                battery.BATTERY_NAME, len(battery.MUTATIONS),
                "" if args.no_live or not getattr(battery, "LIVE", ()) else "，含现场层"))
        reports.append(engine.run_battery(battery, allow_live=not args.no_live,
                                          verbose=not args.quiet))
    print("\n--- 变异电池汇总 ---")
    for row in reports:
        print(engine.summarize(row))
        for problem in row["problems"]:
            print("   ! %s" % problem)
        for label in row["survivors"]:
            print("   [ ] 活下来：%s" % label)
    total = sum(row["total"] for row in reports)
    caught = sum(row["caught"] for row in reports)
    unverified = sum(len(row["unverified"]) for row in reports)
    survivors = sum(len(row["survivors"]) for row in reports)
    broken = [row["name"] for row in reports if not engine.is_green(row)]
    print("合计 %d/%d 抓到，%d 条本环境未验证，%d 条活下来，用掉 %.0f 秒" % (
        caught, total, unverified, survivors, time.monotonic() - started))
    if broken:
        print("不合格的电池：%s" % ", ".join(broken))
        return 1
    print("所有电池都抓住了本环境能验的每一条变异。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
