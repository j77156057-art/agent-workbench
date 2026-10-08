"""实测：`min-release-age=2`（Pi 的值）之下，人工 `npm install` 能否成功。

## 前提已定

* `min-release-age` 在 npm 11.12.1 上**生效**（`probe_min_release_age.py`
  实测 `ENOVERSIONS`）
* 它对 `npm ci` **无影响**（`probe_min_release_age_vs_ci.py` 实测：
  设 9999 天仍装成功 215 个包）
* 所以风险面只剩**人工 `npm install`** —— 而我们 6 处文档/提示让人跑它

**那道关就是：设成 2 天之后，人工安装会不会失败？** 若失败，
用户按 README 走会撞墙 —— 那就不能设这个值。

## 判据

* 实验组 = `min-release-age=2` + `npm install`（按现有 lockfile，不带包名）
* 对照组 = 无 `.npmrc` + 同样命令
* 全程 `--dry-run`，且在**只复制清单的临时目录**里做

## 为什么这一步不能省

Pi 选 2 天是**它自己那棵树**的数字 —— 它的依赖发布节奏与我们的不同。
**照抄数字而不实测，等于把「它安全」当成「我们安全」**（今晚第四次同款错误）。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
NPM = shutil.which("npm") or shutil.which("npm.cmd")
if not NPM:
    raise SystemExit("  *** npm 不在 PATH 上 ***")


def run(argv: list[str], cwd: pathlib.Path, timeout: int = 900) -> tuple[int, str]:
    proc = subprocess.run([NPM] + argv, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, shell=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def newest_package_age() -> tuple[str, float] | None:
    """查 lockfile 里**版本最新**的那个包，报告它的发布时间距今多少天。

    lockfile 只记版本号不记时间，所以这一步必须查 registry。
    找「最新的」是为了问最坏情况：**我们最年轻的那个依赖有多年轻？**
    """
    lock = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))
    entries = [(k, v.get("version")) for k, v in lock.get("packages", {}).items()
               if isinstance(v, dict) and v.get("version")
               and not k.startswith("node_modules/@types/")]
    youngest: tuple[str, float] | None = None
    checked = 0
    for key, version in entries:
        name = key.split("node_modules/")[-1]
        if not name or name.count("/") > 1:
            continue
        url = "https://registry.npmjs.org/%s" % name.replace("/", "%2f")
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - 网络不稳不该让探针崩
            continue
        stamp = ((data.get("time") or {}).get(version) or "").replace("Z", "")
        if not stamp:
            continue
        try:
            when = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        age = (time.time() - when) / 86400.0
        checked += 1
        if youngest is None or age < youngest[1]:
            youngest = ("%s@%s" % (name, version), age)
        if checked >= 40:      # 只查前 40 个，避免探针太慢
            break
    return youngest


def main() -> int:
    print("=" * 76)
    print("  第 1 步：lockfile 里最年轻的依赖有多年轻")
    print("=" * 76)
    youngest = newest_package_age()
    if youngest is None:
        print("  *** registry 查不到发布时间 —— 下面只能靠 npm 自己报 ***")
    else:
        name, age = youngest
        print("  最年轻的依赖: %s" % name)
        print("  发布距今: %.1f 天" % age)
        for limit in (1, 2, 3, 7):
            verdict = "会被挡" if age < limit else "可用"
            print("    min-release-age=%d -> %s" % (limit, verdict))

    print()
    print("=" * 76)
    print("  第 2 步：min-release-age=2 下，人工 npm install 能否成功")
    print("=" * 76)
    results = {}
    for label, npmrc in (("对照组（无 .npmrc）", None),
                         ("实验组（min-release-age=2）", "min-release-age=2\n")):
        with tempfile.TemporaryDirectory() as tmp:
            proj = pathlib.Path(tmp)
            shutil.copy(FRONTEND / "package.json", proj / "package.json")
            shutil.copy(FRONTEND / "package-lock.json", proj / "package-lock.json")
            if npmrc:
                (proj / ".npmrc").write_text(npmrc, encoding="utf-8")
            print()
            print("  %s  .npmrc=%s" % (label, npmrc.strip() if npmrc else "(无)"))
            rc, out = run(["install", "--ignore-scripts", "--dry-run",
                           "--no-audit", "--no-fund"], proj)
            results[label] = (rc, out)
            print("    `npm install --ignore-scripts --dry-run` 退出码=%d" % rc)
            for line in out.splitlines():
                if any(k in line for k in ("added", "changed", "up to date",
                                           "npm error", "ENOVERSIONS")):
                    print("      | %s" % line.strip()[:104])

    print()
    print("=" * 76)
    print("  判定")
    print("=" * 76)
    ctrl_rc, _ = results["对照组（无 .npmrc）"]
    exp_rc, exp_out = results["实验组（min-release-age=2）"]
    print("  对照组退出码 = %d   实验组退出码 = %d" % (ctrl_rc, exp_rc))
    print()

    if ctrl_rc != 0:
        print("  ⚠ 对照组失败 —— 实验不成立。")
        return 1
    if exp_rc == 0:
        print("  => **`min-release-age=2` 不影响按现有 lockfile 的人工安装。**")
        print("     合理：lockfile 里的版本都已发布超过 2 天，解析阶段不会退到它们。")
        print()
        print("     它只在「有人装一个**刚发布**的包」时才拦 —— 那正是它要防的。")
        return 0
    print("  => **`min-release-age=2` 会让人工安装失败。**")
    print("     不能设这个值；先查是哪个包太新：")
    for line in exp_out.splitlines():
        if "npm error" in line:
            print("     | %s" % line.strip()[:104])
    return 2


if __name__ == "__main__":
    sys.exit(main())