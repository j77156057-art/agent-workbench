"""实测：设了 `min-release-age` 之后，**`npm ci` 会不会受影响**。

## 为什么这是决定性实验

我们的两条自动化路径（启动同步 `dependency_bootstrap.py:414`、
CI `harness.yml:59`）**都走 `npm ci`**。推理上 `npm ci` 严格按 lockfile 装、
不重新解析，所以 `min-release-age`（一个**解析阶段**的特性）不该影响它。

**但那是推理。** 而今晚的教训正是「推理听起来合理 ≠ 事实」——
我曾因「esbuild 的 postinstall 在下载二进制」断定会坏，实测不成立。

若 `npm ci` 也受影响，那么加这一行的后果是
**应用启动失败 + CI 全红** —— 那是灾难级改动。

## 判据

用极端值 `min-release-age=9999`（27 年）：那意味着 lockfile 里**任何**包
都不可用。若 `npm ci` 仍成功 -> 该特性确认对 `npm ci` 无影响。

## 安全约束

* **不碰 `frontend/`**（那是我们唯一的构建依赖树，`web/assets` 无 git 兜底）
* 在临时目录里放一份 `package.json` + `package-lock.json` 的副本做实验
* 仍用 `--dry-run` 兜底
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
NPM = shutil.which("npm") or shutil.which("npm.cmd")
if not NPM:
    print("  *** npm 不在 PATH 上 ***")
    raise SystemExit(1)


def run(argv: list[str], cwd: pathlib.Path, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run([NPM] + argv, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, shell=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    print("=" * 76)
    print("  前提：lockfile 里最新的包有多新")
    print("=" * 76)
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    print("  lockfile 包条目: %d" % len(lock.get("packages", {})))
    print("  说明：lockfile 里记的是**版本号**，不记发布时间，")
    print("        所以「这个版本发布多久了」要从 registry 查（下一步）。")

    print()
    print("=" * 76)
    print("  实验：min-release-age=9999 下，npm ci 是否受影响")
    print("=" * 76)

    results: dict[str, tuple[int, str]] = {}
    for label, npmrc in (("对照组（无 .npmrc）", None),
                         ("实验组（min-release-age=9999）", "min-release-age=9999\n")):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp)
            # 只复制清单，不复制 node_modules —— 实验要的是「按 lockfile 装」。
            shutil.copy(FRONTEND / "package.json", proj / "package.json")
            shutil.copy(FRONTEND / "package-lock.json", proj / "package-lock.json")
            if npmrc:
                (proj / ".npmrc").write_text(npmrc, encoding="utf-8")

            print()
            print("  %s" % label)
            print("    .npmrc: %s" % (npmrc.strip() if npmrc else "(无)"))
            rc, out = run(["ci", "--ignore-scripts", "--dry-run", "--no-audit",
                           "--no-fund"], proj)
            results[label] = (rc, out)
            print("    `npm ci --ignore-scripts --dry-run` 退出码=%d" % rc)
            for line in out.splitlines():
                if any(k in line for k in ("added", "packages in", "npm error",
                                           "ENOTSUP", "ENOVERSIONS", "removed")):
                    print("      | %s" % line.strip()[:104])

    print()
    print("=" * 76)
    print("  判定")
    print("=" * 76)
    ctrl_rc, _ = results["对照组（无 .npmrc）"]
    exp_rc, exp_out = results["实验组（min-release-age=9999）"]

    print("  对照组 npm ci 退出码 = %d" % ctrl_rc)
    print("  实验组 npm ci 退出码 = %d" % exp_rc)
    print()

    if ctrl_rc != 0:
        print("  ⚠ 对照组自己就失败 —— 实验不成立。")
        for line in exp_out.splitlines()[-6:]:
            print("    %s" % line.strip()[:104])
        return 1

    if exp_rc == 0:
        print("  => **确认：`min-release-age` 对 `npm ci` 无影响。**")
        print("     设 9999 天（lockfile 里任何包都不可用）npm ci 仍然成功 ——")
        print("     因为 `npm ci` 严格按 lockfile 装，**不进入版本解析阶段**。")
        print()
        print("     这意味着加这一行的风险面只有一处：**人工跑 `npm install`**。")
        return 0

    print("  => **意外：`npm ci` 也受 `min-release-age` 影响。**")
    print("     那就不能加 —— 否则应用启动与 CI 都会失败。")
    for line in exp_out.splitlines():
        if "npm error" in line:
            print("     | %s" % line.strip()[:104])
    return 2


if __name__ == "__main__":
    sys.exit(main())