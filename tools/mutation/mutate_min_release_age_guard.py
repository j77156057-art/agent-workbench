"""变异实测：`tests/test_min_release_age_pinned.py` 抓得住吗？

四条变异，前三条期望红、最后一条（无效变异）期望绿。

第 4 条是**反向守卫的自我检验** —— 那个「回读 null 即无效」的坏注释
如果有人加进来，守卫必须红。而它恰恰是「看起来只是加了一行注释」的
改动，所以要专门验一次。
"""

from __future__ import annotations

import difflib
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
NPMRC = ROOT / "frontend" / ".npmrc"
TEST = "tests/test_min_release_age_pinned.py"
PY = ".venv\\Scripts\\python.exe"


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ⚠ **定位按行、不按字符串。** 第一版用 `old="min-release-age=2"` 做替换，
# 结果它在文件里出现 **2 次**（注释里提到过一次）—— 五条变异全部「定位失败」。
# 「定位失败」与「守卫没抓住」在汇总里长得一样，所以必须在这里分开。
# 正确做法：取**唯一一行**（`^min-release-age=` 且不在注释里）作为锚点。
MUTATIONS = [
    {
        "name": "注释掉 min-release-age 那一行",
        "line_prefix": "min-release-age=",
        "new": "# min-release-age=2",
        "expect": "red",
    },
    {
        "name": "改成 0（等于没设）",
        "line_prefix": "min-release-age=",
        "new": "min-release-age=0",
        "expect": "red",
    },
    {
        "name": "改成 9999（过严，会卡住安全补丁）",
        "line_prefix": "min-release-age=",
        "new": "min-release-age=9999",
        "expect": "red",
    },
    {
        "name": "加入坏注释「回读 null 即无效」（反向守卫要抓）",
        "append": "# 补充：回读 null 即无效。",
        "expect": "red",
    },
    {
        "name": "【无效变异对照】只加一句无关注释、配置不动",
        "append": "# 这行是给 CI 看的标记，不影响解析。",
        "expect": "green",
    },
]


def main() -> int:
    original = NPMRC.read_bytes()

    print("=" * 74)
    print("  基线：守卫应全绿")
    print("=" * 74)
    rc, out = run_tests()
    print("  退出码 %d  %s" % (rc, "绿" if rc == 0 else "*** 基线就红 ***"))
    if rc != 0:
        print(out[-1200:])
        return 1

    results = []
    try:
        for mut in MUTATIONS:
            text = original.decode("utf-8")
            lines = text.splitlines()
            print()
            print("=" * 74)
            print("  变异：%s" % mut["name"])
            print("=" * 74)

            prefix = mut.get("line_prefix")
            append = mut.get("append")
            if prefix:
                hits = [i for i, ln in enumerate(lines)
                        if ln.startswith(prefix) and not ln.lstrip().startswith("#")]
                if len(hits) != 1:
                    print("  *** 定位失败：行首 %r 且非注释的行有 %d 条（要 1 条）***"
                          % (prefix, len(hits)))
                    results.append((mut["name"], "定位失败", False))
                    continue
                idx = hits[0]
                lines[idx] = mut["new"]
            else:
                lines.append(append)
            mutated = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
            # 干跑校验：变异必须真的改变内容。**用 difflib 而不是 zip** ——
            # 后者在插入行后会把后面每行都算成「改了」（上一次踩过，报 137 行）。
            changed = [ln for ln in difflib.unified_diff(
                text.splitlines(), mutated.splitlines(), n=0, lineterm="")
                if ln[:1] in "+-" and ln[:3] not in ("---", "+++")]
            print("  干跑校验: 真实差异行 %d" % len(changed))
            for ln in changed[:4]:
                print("    %s" % ln.strip()[:92])
            if not changed:
                print("  *** 变异无效 —— 「红」也说明不了任何事。跳过。***")
                results.append((mut["name"], "变异无效", False))
                continue
            print("  期望: %s" % mut["expect"])

            NPMRC.write_bytes(mutated.encode("utf-8"))
            rc, out = run_tests()
            got = "red" if rc != 0 else "green"
            ok = got == mut["expect"]
            results.append((mut["name"], got, ok))
            print("  实测: %s  =>  %s" % (got, "符合预期" if ok else "*** 不符合 ***"))
            if got == "red":
                for ln in out.splitlines():
                    if ln.strip().startswith("E ") or "FAILED" in ln:
                        print("    " + ln.strip()[:104])
                        break
            NPMRC.write_bytes(original)
            if hashlib.sha256(NPMRC.read_bytes()).hexdigest() != \
                    hashlib.sha256(original).hexdigest():
                print("  *** 还原失败 ***")
                return 1
    finally:
        NPMRC.write_bytes(original)
        print()
        print("  已逐字节还原 frontend/.npmrc。")

    print()
    print("=" * 74)
    print("  汇总")
    print("=" * 74)
    bad = [r for r in results if not r[2]]
    for name, got, ok in results:
        print("  %-50s %-8s %s" % (name[:50], got, "OK" if ok else "*** FAIL ***"))
    print()
    print("  => %s" % ("全部符合预期。" if not bad
                        else "%d 条不符合预期 —— 守卫不可信。" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())