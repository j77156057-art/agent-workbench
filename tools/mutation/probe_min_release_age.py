"""实测：`min-release-age` 在本机 npm 11.12.1 上**是否真的生效**。

## 为什么必须实测

`min-release-age` 是 npm 的较新特性。若当前 npm **不识别**它，
`.npmrc` 里那行就是一个**静默无效的配置** —— 正是「看起来做完了、
实际没生效」那一类。而「不识别」的表现和「识别但当前无影响」**完全一样**
（都是「装成功了」），所以只能靠**构造一个必然失败的场景**来分辨。

## 判据设计：用极端值逼出差异

把 `min-release-age` 设成 **9999 天（约 27 年）** —— 那意味着
**任何 2026 年发布的包都不可用**。于是：

* 若生效 -> `npm install <任意近期包>` 必须**找不到可用版本**并报错
* 若不生效 -> 正常解析出版本号，装成功

这不是「看看有没有警告」，而是**让两种可能产生相反的可观测结果**。

## 安全约束

* 全程 `--dry-run`
* 在 `tempfile.TemporaryDirectory()` 里的**独立目录**操作，
  **不碰 `frontend/`**（它是我们唯一的构建依赖，且 `web/assets` 无 git 兜底）
* 不写任何全局/用户级 npm 配置
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

import shutil

# ⚠ **这个坑我今晚已经犯过一次**（`probe_npm_ignore_scripts.py` 里崩在
# `FileNotFoundError: [WinError 2]`），在新探针里又重犯了 —— 因为我按
# `sys.executable` 的目录去拼 `npm.cmd` 的路径。而 `sys.executable` 指向
# `.venv\Scripts\python.exe`，**npm 不在 venv 里**。
#
# 正确做法只有一个：`shutil.which`。按「已知可执行文件在已知目录」拼路径，
# 在 venv 布局下必然错 —— 而这**在同一个晚上连犯两次**。
NPM = shutil.which("npm") or shutil.which("npm.cmd")
if not NPM:
    print("  *** npm 不在 PATH 上 —— 无法实测 ***")
    raise SystemExit(1)


def run(argv: list[str], cwd: pathlib.Path, timeout: int = 300
        ) -> tuple[int, str]:
    proc = subprocess.run([NPM] + argv, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, shell=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def npm_version() -> str:
    rc, out = run(["--version"], pathlib.Path.cwd(), timeout=60)
    return out.strip() or "(取不到)"


def published_within(package: str, days: int) -> tuple[bool, str]:
    """用 registry API 查这个包的发布时间距离今���多少天。

    这是**独立于 npm 的第三方事实源** —— 若 registry 不可达就直接跳过该用例。
    """
    import urllib.request
    url = "https://registry.npmjs.org/%s" % package
    try:
        with urllib.request.urlopen(url, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 网络状况不该让探针崩
        return False, "registry 不可达 (%s)" % type(exc).__name__
    latest = (data.get("dist-tags") or {}).get("latest")
    if not latest:
        return False, "取不到 dist-tags.latest"
    stamp = ((data.get("time") or {}).get(latest) or "").replace("Z", "")
    if not stamp:
        return False, "取不到发布时间"
    try:
        when = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return False, "时间格式不认识: %r" % stamp
    age_days = (time.time() - when) / 86400.0
    return age_days <= days, "%s 发布于 %s（%.1f 天前）" % (latest, stamp[:10], age_days)


def main() -> int:
    print("=" * 76)
    print("  第 1 步：npm 版本与该特性的可用性")
    print("=" * 76)
    ver = npm_version()
    print("  npm 版本: %s" % ver)
    major = ver.split(".")[0] if ver[0].isdigit() else "?"
    print("  `min-release-age` 需要 npm >= 11.6.0（npm 官方 changelog）；"
          "本机 major=%s -> %s" % (major, "满足" if major.isdigit() and int(major) >= 11
                                    else "**低于 11，需实测**"))

    print()
    print("  === 未设任何配置时，npm 认不认这个键 ===")
    with tempfile.TemporaryDirectory() as tmp:
        rc, out = run(["config", "get", "min-release-age"], pathlib.Path(tmp))
        print("    `npm config get min-release-age` 退出码=%s 输出=%r"
              % (rc, out.strip()[:60]))

    print()
    print("=" * 76)
    print("  第 2 步：找一个「足够新」的包做样本")
    print("=" * 76)
    sample = None
    for candidate in ("vite", "vue", "typescript", "esbuild"):
        fresh, detail = published_within(candidate, 30)
        print("    %-12s 30 天内发布过? %-5s %s" % (candidate, fresh, detail))
        if fresh and sample is None:
            sample = candidate
    if sample is None:
        print()
        print("  *** 找不到 30 天内发布过的样本包 —— 换一个策略：")
        print("      用极端 min-release-age=9999（27 年）代替「新包」这个条件，")
        print("      因为**任何**现存包都发布于 2026 年之前。***")
    print()
    print("  样本包: %s" % (sample or "(无 —— 靠极端值策略)"))

    print()
    print("=" * 76)
    print("  第 3 步：极端值对照实验（全程 --dry-run，独立临时目录）")
    print("=" * 76)

    probe_pkg = sample or "is-odd"
    results: dict[str, tuple[int, str]] = {}

    for label, extra_config in (("对照组（不设 min-release-age）", None),
                                ("实验组（min-release-age=9999）", "min-release-age=9999\n")):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp)
            if extra_config:
                (proj / ".npmrc").write_text(extra_config, encoding="utf-8")
                rc_cfg, out_cfg = run(["config", "get", "min-release-age"], proj)
                print("  %s" % label)
                print("    写 .npmrc=%r，回读=%r（退出码 %d）"
                      % (extra_config.strip(), out_cfg.strip()[:40], rc_cfg))
            else:
                print("  %s" % label)
                print("    不写 .npmrc")

            rc, out = run(["install", probe_pkg, "--dry-run", "--no-save",
                           "--ignore-scripts", "--no-audit", "--no-fund"],
                          proj, timeout=300)
            results[label] = (rc, out)
            print("    `npm install %s --dry-run` 退出码=%d" % (probe_pkg, rc))
            for line in out.splitlines():
                if any(k in line for k in ("added", "packages in", "ERR", "error",
                                           "No matching", "notarget", "ETARGET",
                                           "age", "release")):
                    print("      | %s" % line.strip()[:104])
            print()

    print("=" * 76)
    print("  判定")
    print("=" * 76)
    ctrl_rc, ctrl_out = results["对照组（不设 min-release-age）"]
    exp_rc, exp_out = results["实验组（min-release-age=9999）"]

    print("  对照组 退出码 = %d" % ctrl_rc)
    print("  实验组 退出码 = %d" % exp_rc)
    print()

    if ctrl_rc != 0:
        print("  ⚠ **对照组自己就失败了** —— 那本次实验不成立。")
        print("    先查 %s:" % probe_pkg)
        for line in ctrl_out.splitlines()[-8:]:
            print("      %s" % line.strip()[:104])
        return 1

    if exp_rc != 0:
        print("  => **min-release-age 生效**：设 9999 天后连最近的包都装不了。")
        print("     这是我们要的效果（把「当天发布的包」挡在解析之外）。")
        for line in exp_out.splitlines():
            if any(k in line for k in ("ERR", "No matching", "notarget", "age",
                                       "release", "ETARGET")):
                print("     | %s" % line.strip()[:104])
        print()
        print("  ⚠ 但这也说明：**设了它之后，装一个刚发布的包会失败**。")
        print("     那正是 Pi 要的（避免解析到当天发布的依赖），代价是")
        print("     「刚发布的补丁版本要等两天才能装」。")
        return 0

    print("  => **min-release-age 在本机 npm 上不生效**（设 9999 天仍能装成功）。")
    print("     结论：`.npmrc` 里加这行会是**静默无效的配置** —— 不要加。")
    return 2


if __name__ == "__main__":
    sys.exit(main())