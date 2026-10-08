"""变异实测：`tests/test_npm_ignore_scripts_pinned.py` 的守卫真的抓得住吗？

## 为什么要这个脚本

绿 ≠ 有效。我今晚栽过三次：
* 恒真判据（`hasattr(Agent, ...)` 对类检查）
* 无效变异（语句插在 docstring 内，语法合法但永不执行）
* 只查一个来源（`import tools` vs `import agent` 数量不同）

所以每一条守卫都要做两件事：
1. **变异真的被抓住**（红）
2. **变异真的生效**（不是插在 docstring 里 —— 先干跑校验）

## 干跑校验是关键的一步

「变异后守卫红」有两种原因：
* 守卫抓到了（好）
* **变异根本没生效**（比如插进了 docstring / 注释 / 不可达分支）

后者更危险 —— 它给出「守卫有效」 的假象。所以每个变异都先检查
**变异是否真的改变了那行代码**，再跑测试。

## 安全约束

只改 `dependency_bootstrap.py` / `harness.yml` / `tests/` 里的目标行，
每次变异后**逐字节还原**（sha256 校验），失败即中止。
不改任何其他文件，不跑 npm。
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGETS = [
    ROOT / "dependency_bootstrap.py",
    ROOT / ".github/workflows/harness.yml",
]
TEST = "tests/test_npm_ignore_scripts_pinned.py"
PY = ".venv\\Scripts\\python.exe"


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


MUTATIONS = [
    {
        "name": "启动同步那条去掉 flag（真删那一行里的 token）",
        "path": ROOT / "dependency_bootstrap.py",
        "old": '_run([npm, "ci", "--ignore-scripts"], project / "frontend",',
        "new": '_run([npm, "ci"], project / "frontend",',
        "expect": "red",
    },
    {
        "name": "CI 那条去掉 flag",
        "path": ROOT / ".github/workflows/harness.yml",
        "old": "          npm ci --ignore-scripts\n",
        "new": "          npm ci\n",
        "expect": "red",
    },
    {
        "name": "【无效变异对照】只加注释痕迹、**不动真实代码** —— 守卫不该红",
        "path": ROOT / "dependency_bootstrap.py",
        "old": '        _run([npm, "ci", "--ignore-scripts"], project / "frontend",',
        # 只在上一行插入一句提到 flag 的注释，**argv 一个 token 都不动**。
        #
        # ⚠ 第一版这个对照写错了：我把 `old`/`new` 设成同一行，
        # 于是「挪进注释」的同时**真把 flag 从 argv 里删了** ——
        # 守卫当然红，而且**红得完全正确**。若当时为了「让对照通过」
        # 去放宽守卫，那才是把守卫改坏。
        # 「无效变异」的形态是：**表面有动作，行为没变**。
        "new": ('        # 这条 npm ci 特意带 --ignore-scripts（见 §1.4 实测）\n'
                '        _run([npm, "ci", "--ignore-scripts"], project / "frontend",'),
        "expect": "green",
    },
    {
        "name": "lockfile 新增一个带 install 脚本的包（改守卫的输入）",
        "path": ROOT / "tests/test_npm_ignore_scripts_pinned.py",
        "old": '    "node_modules/fsevents",\n',
        "new": '    "node_modules/fsevents",\n    "node_modules/some-new-pkg",\n',
        "expect": "red",
    },
]


def main() -> int:
    backups = {p: p.read_bytes() for p in TARGETS}
    test_file = ROOT / TEST
    backups[test_file] = test_file.read_bytes()

    print("=" * 74)
    print("  基线：守卫应当全绿")
    print("=" * 74)
    rc, out = run_tests()
    print("  退出码 %d  %s" % (rc, "绿" if rc == 0 else "*** 基线就红 ***"))
    if rc != 0:
        print(out[-1200:])
        print("  基线不绿，变异实测没有意义 —— 中止。")
        return 1

    results = []
    try:
        for mut in MUTATIONS:
            path: pathlib.Path = mut["path"]
            original = backups[path]
            text = original.decode("utf-8")
            old, new = mut["old"], mut["new"]

            print()
            print("=" * 74)
            print("  变异：%s" % mut["name"])
            print("=" * 74)
            if text.count(old) != 1:
                print("  *** 定位失败：%r 出现 %d 次（要恰好 1 次）—— 中止该变异"
                      % (old[:60], text.count(old)))
                results.append((mut["name"], "定位失败", False))
                continue

            mutated = text.replace(old, new, 1)
            # —— 干跑校验：变异是否**真的改变了目标** ——
            # 「变异没被抓住」必须先排除「变异无效」。
            #
            # ⚠ 判据换过一次。第一版写「old 片段应消失」，结果把**两条有效变异**
            # 误判成无效 —— 因为那两条的 `new` **包含** `old`
            # （加注释时保留了原行；加一个包名时保留了另一个）。
            # **「文本包含」与「行为改变」是两件事。**
            before_lines = text.splitlines()
            after_lines = mutated.splitlines()
            grew = len(after_lines) - len(before_lines)
            # ⚠ 差异行数**必须用 difflib**，不能用 `zip(before, after)` ——
            # 插入一行后 zip 会把后面每一行都算成「改了」（实测报过 137 行，
            # 真实差异只有 1 行）。那个噪声会让下一个人以为变异出了问题。
            import difflib
            changed = [ln for ln in difflib.unified_diff(
                before_lines, after_lines, n=0, lineterm="")
                if ln[:1] in "+-" and ln[:3] not in ("---", "+++")]
            effective = bool(changed) or grew != 0
            print("  干跑校验: 真实差异行 %d、行数变化 %+d" % (len(changed), grew))
            for ln in changed[:4]:
                print("    %s" % ln.strip()[:92])
            print("  变异是否真的改变了目标: %s" % ("是" if effective else "*** 否 ***"))
            if not effective:
                print("  *** 变异无效 —— 「红」也说明不了任何事。跳过。***")
                results.append((mut["name"], "变异无效", False))
                path.write_bytes(original)
                continue
            print("  期望: %s" % mut["expect"])

            path.write_bytes(mutated.encode("utf-8"))
            rc, out = run_tests()
            got = "red" if rc != 0 else "green"
            ok = (got == mut["expect"])
            results.append((mut["name"], got, ok))
            print("  实测: %s  =>  %s"
                  % (got, "符合预期" if ok else "*** 不符合预期 ***"))
            if got == "red":
                tail = [ln for ln in out.splitlines()
                        if "FAILED" in ln or "AssertionError" in ln]
                for ln in tail[:4]:
                    print("    " + ln.strip()[:104])

            path.write_bytes(original)
            if sha(path) != hashlib.sha256(original).hexdigest():
                print("  *** 还原失败 ***")
                return 1
    finally:
        for path, data in backups.items():
            path.write_bytes(data)
        print()
        print("  已逐字节还原全部目标文件。")

    print()
    print("=" * 74)
    print("  汇总")
    print("=" * 74)
    bad = [r for r in results if not r[2]]
    for name, got, ok in results:
        print("  %-52s %-6s %s" % (name[:52], got, "OK" if ok else "*** FAIL ***"))
    print()
    if bad:
        print("  => %d 条不符合预期 —— 守卫不可信。" % len(bad))
        return 1
    print("  => 全部符合预期：守卫抓得住真变异，且对无效变异保持绿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())