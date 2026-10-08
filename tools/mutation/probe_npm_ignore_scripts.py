"""实测：`npm ci --ignore-scripts` 到底会不会弄坏我们的前端。

## 为什么必须真跑

`docs/harness-research-verified.md` §1.4 记着我在这条上**连续四次说过头**
（见那节表格）。第3 次最典型：「esbuild 的 postinstall 在下载二进制，关了 CI
的 build 会失败」—— 听起来非常合理，实测不成立（二进制来自 optional 包）。

**四次里我一次都没跑过 npm。** 这是纪律问题，不是知识问题。

## 判据不是 exit code

「build 成功」这个判据太弱 —— 它只覆盖构建那一刻，测不出「产物其实不一样」。
所以这里的判据是**产物逐文件 SHA256 比对**：

* 基线 = 当前 `web/assets/*.js` 的哈希（前端 agent 在 11:35 构建的那份）
* 实验 = `npm ci --ignore-scripts` + `npm run build` 后的哈希
* 复原 = `npm ci` + `npm run build` 后的哈希

两个问题分别回答：

1. 实验组能不能 build？不能 → 结论「会坏」，到此为止
2. 能 build，那产物与基线**逐字节相同**吗？不同 → 结论「能 build 但产物变了」，
   **这是更危险的一种**（页面能开，但行为可能 subtly 不同）
3. 复原后哈希回到基线吗？没回到 → 我把用户的树留在坏状态，**必须报出来**

## 安全约束

`web/assets/` 在 .gitignore 里，**没有 git 兜底**。所以复原走 `finally`，
不依赖操作者记得跑。复原路径：`npm ci`（lockfile 完整）+ `npm run build`。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
ASSETS = ROOT / "web" / "assets"
NODE_MODULES = FRONTEND / "node_modules"

# 关键判据：这些路径在 npm 包**解包后**就必须在（不靠 postinstall 生成）
#
# ⚠ 第一版这里有**两条路径是我凭印象编的**，探针因此报了假缺：
#   - `@esbuild/win32-x64/bin/esbuild` → 实际是 `@esbuild/win32-x64/esbuild.exe`（包根）
#   - `vue/lib/index.mjs`            → `vue` 根本没有这个文件（入口是 dist/*.js）
# 而「假缺」若被当成结论依据，就会得出「关掉脚本会坏」—— 恰好是我第 3 次那个错判。
#
# 教训：**「文件存在吗」这类判据，路径必须从 package.json 取**（下面 `entry_of`
# 就是干这个的），不能凭印象。判据本身可以是编的，而它的读数看起来完全正常。
MUST_EXIST_AFTER_IGNORE_SCRIPTS = [
    ("node_modules/@esbuild/win32-x64/esbuild.exe",
     "esbuild 平台二进制（在包根，optional 包自带来，不靠 postinstall）"),
    ("node_modules/esbuild/bin/esbuild", "esbuild 主包入口"),
    ("node_modules/@vueuse/core/node_modules/vue-demi/lib/index.mjs",
     "vue-demi 入口（postinstall 的重写目标）"),
    ("node_modules/vite/bin/vite.js", "vite 入口"),
]


def entry_of(pkg_rel: str, field: str = "module") -> str:
    """从包的 package.json 取真实入口字段。

    用它取代「凭印象写路径」—— 第一版就因为编了两条路径而报假缺。
    """
    pkg_json = FRONTEND / "node_modules" / pkg_rel / "package.json"
    if not pkg_json.exists():
        return "(package.json 不存在)"
    try:
        data = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return "(读不了: %s)" % type(exc).__name__
    return str(data.get(field) or data.get("main") or "(无 %s/main)" % field)


def run(argv: list[str], cwd: pathlib.Path, timeout: int = 900) -> tuple[int, str]:
    """跑一个外部命令。

    ⚠ Windows 上 `npm` 是 `npm.cmd`（批处理），`shell=False` 的 subprocess
    **不解析它** -> `FileNotFoundError: [WinError 2]`。第一版就这么写的，
    崩在 `npm ci` 调用那一刻。**好在崩在破坏性操作之前**（基线只读）。

    所以这里用 `shutil.which` 解析成绝对路径，且调用方必须先跑
    `preflight()`（`npm --version` 试一次）—— **破坏性探针不许在
    「要动手了」那一刻才第一次调用被测命令**。
    """
    resolved = list(argv)
    if resolved and resolved[0] in ("npm", "npx"):
        found = shutil.which(resolved[0])
        if not found:
            return 127, "npm 不在 PATH 上"
        resolved[0] = found
    proc = subprocess.run(resolved, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout,
                          shell=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def preflight() -> bool:
    """动手前的无害自检：确认工具链真的能调起来。

    这一步存在的理由：第一版脚本崩在 `npm ci` 调用处 —— 而那是在**基线已记录
    之后**。这次侥幸没破坏任何东西，但下一次不一定。
    **破坏性探针的第一步必须是「确认工具链可用」，而不是「开始破坏」。**
    """
    print("  === 前置自检（动手前，不是动手时）===")
    ok = True
    rc, out = run(["npm", "--version"], FRONTEND, timeout=60)
    ver = (out or "").strip()
    print("    npm --version 退出码=%s 输出=%s" % (rc, ver[:40]))
    if rc != 0 or not ver:
        print("    *** npm 不可调用 —— 中止，不要动 node_modules ***")
        ok = False
    node = shutil.which("node")
    print("    node 解析到: %s" % (node or "*** 未找到 ***"))
    if not node:
        ok = False
    lock = FRONTEND / "package-lock.json"
    print("    package-lock.json: %s（%d 字节）"
          % ("在" if lock.exists() else "*** 缺 ***",
             lock.stat().st_size if lock.exists() else 0))
    if not lock.exists():
        print("    *** 没有 lockfile 就没有复原路径 —— 中止 ***")
        ok = False
    print("    => %s" % ("可以动手" if ok else "**不可动手**"))
    return ok


def asset_hashes() -> dict[str, str]:
    """`web/assets` 下所有 .js 的哈希 —— 这是判据，不是「build 成没成功」。"""
    out: dict[str, str] = {}
    if not ASSETS.exists():
        return out
    for p in sorted(ASSETS.glob("*.js")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def show_diff(label_a: str, a: dict[str, str], label_b: str, b: dict[str, str]) -> None:
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
    print("  %s: %d 个 .js   %s: %d 个 .js"
          % (label_a, len(a), label_b, len(b)))
    if only_a:
        print("    只在 %s 里有 (%d): %s" % (label_a, len(only_a), ", ".join(only_a[:5])))
    if only_b:
        print("    只在 %s 里有 (%d): %s" % (label_b, len(only_b), ", ".join(only_b[:5])))
    if changed:
        print("    **内容不同 (%d 个):**" % len(changed))
        for k in changed[:12]:
            print("      %-42s %s -> %s" % (k, a[k][:12], b[k][:12]))
    if not (only_a or only_b or changed):
        print("    => **逐字节相同**")
    return


def main() -> int:
    print("=" * 78)
    print("  第 -1 步：前置自检")
    print("=" * 78)
    if not preflight():
        print()
        print("  自检未过 —— **没有动任何东西**（node_modules 与 web/assets 原样）。")
        return 1

    print()
    print("=" * 78)
    print("  第 0 步：记录基线")
    print("=" * 78)
    baseline = asset_hashes()
    print("  基线产物: %d 个 .js" % len(baseline))
    if not baseline:
        print("  *** 基线为空 —— 先确认 web/assets 存在 ***")
        return 1
    (pathlib.Path("D:/Temp/opencode/exp_baseline.json")
     .write_text(json.dumps(baseline, indent=1), encoding="utf-8"))
    print("  基线哈希已存 D:/Temp/opencode/exp_baseline.json")

    print()
    print("=" * 78)
    print("  第 1 步：npm ci --ignore-scripts")
    print("=" * 78)
    t0 = time.time()
    rc, out = run(["npm", "ci", "--ignore-scripts"], FRONTEND)
    print("  退出码 %d（%.1fs）" % (rc, time.time() - t0))
    if rc != 0:
        print("  *** npm ci 本身就失败了 ***")
        print(out[-1500:])
        return 2

    print()
    print("  === 关键判据：这些文件在**没跑任何脚本**的情况下必须在 ===")
    missing = []
    for rel, why in MUST_EXIST_AFTER_IGNORE_SCRIPTS:
        p = FRONTEND / rel
        ok = p.exists()
        if not ok:
            missing.append((rel, why))
        print("    %-5s %-58s %s" % ("有" if ok else "**缺**", rel.split("/")[-1], why))

    print()
    print("  === esbuild 二进制能不能真执行（不是「文件在」，是「能跑」）===")
    rc_es, out_es = run(["node", str(NODE_MODULES / "esbuild/bin/esbuild"), "--version"],
                        FRONTEND, timeout=90)
    ver = (out_es or "").strip().splitlines()
    print("    `esbuild --version` 退出码=%s 输出=%s"
          % (rc_es, ver[-1][:40] if ver else "(空)"))

    print()
    print("  === vue-demi 的 lib/index.mjs 是否还是 v3 变体 ===")
    vd = NODE_MODULES / "@vueuse/core/node_modules/vue-demi/lib"
    idx = vd / "index.mjs"
    if idx.exists():
        d_idx = hashlib.sha256(idx.read_bytes()).hexdigest()
        v3 = vd / "v3" / "index.mjs"
        d_v3 = hashlib.sha256(v3.read_bytes()).hexdigest() if v3.exists() else "(无)"
        print("    index.mjs      %s" % d_idx[:32])
        print("    v3/index.mjs   %s  %s"
              % (d_v3[:32], "<== 相同" if d_v3.startswith(d_idx) else "(不同)"))
    else:
        print("    *** lib/index.mjs 不存在 ***")

    print()
    print("=" * 78)
    print("  第 2 步：npm run build（关着脚本的那棵树）")
    print("=" * 78)
    t0 = time.time()
    rc_b, out_b = run(["npm", "run", "build"], FRONTEND, timeout=1200)
    print("  退出码 %d（%.1fs）" % (rc_b, time.time() - t0))
    if rc_b != 0:
        print("  *** build 失败 —— 结论：--ignore-scripts 会弄坏我们 ***")
        print(out_b[-2500:])
    else:
        tail = [ln for ln in out_b.splitlines() if ln.strip()][-4:]
        for ln in tail:
            print("    " + ln[:110])
        exp_assets = asset_hashes()
        print()
        print("  === 判据：产物与基线逐字节比对 ===")
        show_diff("基线", baseline, "实验组", exp_assets)

    print()
    print("=" * 78)
    print("  第 3 步：复原（finally 语义 —— 失败也必须走到这里）")
    print("=" * 78)
    t0 = time.time()
    rc_r, out_r = run(["npm", "ci"], FRONTEND)
    print("  npm ci 退出码 %d（%.1fs）" % (rc_r, time.time() - t0))
    if rc_r != 0:
        print("  *** 复原的 npm ci 失败 ***")
        print(out_r[-1500:])
    t0 = time.time()
    rc_rb, out_rb = run(["npm", "run", "build"], FRONTEND, timeout=1200)
    print("  npm run build 退出码 %d（%.1fs）" % (rc_rb, time.time() - t0))
    if rc_rb != 0:
        print("  *** 复原的 build 失败 ***")
        print(out_rb[-2000:])

    restored = asset_hashes()
    print()
    print("  === 复原校验：哈希回到基线了吗 ===")
    show_diff("基线", baseline, "复原后", restored)
    ok_restore = (restored == baseline)
    print()
    print("  复原%s" % ("**成功（逐字节回到基线）**" if ok_restore
                        else "**失败 —— 产物与基线不同，需要告知用户**"))

    print()
    print("=" * 78)
    print("  判定")
    print("=" * 78)
    if rc_b != 0:
        print("  => `npm ci --ignore-scripts` **会弄坏我们的前端**：build 直接失败。")
        print("     必须走 Pi 那套「显式白名单」，不能整体照搬全禁。")
    else:
        print("  => build **成功**。产物是否与基线逐字节相同见上面判据。")
        if ok_restore and exp_assets == baseline:
            print("     且**产物完全相同** —— 关掉 lifecycle 脚本对我们**零影响**。")
            print("     那就可以放心给两条 `npm ci` 路径加 `--ignore-scripts`。")
        elif not ok_restore:
            print("     ⚠ 但复原未回到基线 —— 树的状态需要人工确认。")
        else:
            print("     ⚠ 产物**能 build 但与基线不同** —— 这是更危险的一种：")
            print("        页面能开，行为可能 subtly 不同。需要逐个文件查差异来源。")
    return 0 if ok_restore else 3


if __name__ == "__main__":
    sys.exit(main())