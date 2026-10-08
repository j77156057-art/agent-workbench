"""变异实测：`tests/test_proxy_tools_pinned.py` 抓得住吗？

五条变异。要点：**两条是「顺着人的直觉走」的动作**，
而下一个人完全有理由那么做 —— 所以守卫必须挡住它们。

* 删掉整行 ——「没人 import 它，删了」
* 改成裸 `proxy_tools` ——「版本号太老，会挡住新版本」
* 加 `--hash` ——「钉死更彻底」
* 删掉「不能加 hash」那段解释 ——「注释太多了」
* （对照）只挪动空行、不改语义
"""

from __future__ import annotations

import difflib
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
REQ = ROOT / "requirements.txt"
TEST = "tests/test_proxy_tools_pinned.py"
PY = ".venv\\Scripts\\python.exe"


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def drop_pin(lines: list[str]) -> list[str]:
    return [ln for ln in lines if not ln.startswith("proxy_tools")]


def add_hash(lines: list[str]) -> list[str]:
    return [ln + " --hash=sha256:" + "0" * 64
            if ln.startswith("proxy_tools") else ln for ln in lines]


def drop_explanation(lines: list[str]) -> list[str]:
    return [ln for ln in lines
            if "require-hashes" not in ln and "必须用" not in ln]


MUTATIONS = [
    {
        "name": "删掉整行（「没人 import 它」）",
        "fn": drop_pin,
        "expect": "red",
    },
    {
        "name": "改成裸 proxy_tools（「版本太老会挡新版本」）",
        "fn": lambda ls: ["proxy_tools" if ln.startswith("proxy_tools") else ln
                          for ln in ls],
        "expect": "red",
    },
    {
        "name": "顺手加 --hash（「钉死更彻底」）",
        "fn": add_hash,
        "expect": "red",
    },
    {
        "name": "删掉「不能加 hash」那段解释",
        "fn": drop_explanation,
        "expect": "red",
    },
    {
        "name": "【无效变异对照】在末尾加一个空行 —— 守卫不该红",
        "fn": lambda ls: ls + [""],
        "expect": "green",
    },
]


def main() -> int:
    original = REQ.read_bytes()

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
            mutated_lines = mut["fn"](lines)
            mutated = "\n".join(mutated_lines) + ("\n" if text.endswith("\n") else "")

            print()
            print("=" * 74)
            print("  变异：%s" % mut["name"])
            print("=" * 74)
            if mutated == text:
                print("  *** 变异无效（文本没变）—— 「红」也说明不了任何事。跳过。***")
                results.append((mut["name"], "变异无效", False))
                continue
            changed = [ln for ln in difflib.unified_diff(
                lines, mutated_lines, n=0, lineterm="")
                if ln[:1] in "+-" and ln[:3] not in ("---", "+++")]
            print("  干跑校验: 真实差异行 %d" % len(changed))
            for ln in changed[:3]:
                print("    %s" % ln.strip()[:92])
            print("  期望: %s" % mut["expect"])

            REQ.write_bytes(mutated.encode("utf-8"))
            rc, out = run_tests()
            got = "red" if rc != 0 else "green"
            ok = got == mut["expect"]
            results.append((mut["name"], got, ok))
            print("  实测: %s  =>  %s" % (got, "符合预期" if ok else "*** 不符合 ***"))
            if got == "red":
                for ln in out.splitlines():
                    if ln.strip().startswith("E   ") or "FAILED" in ln:
                        print("    " + ln.strip()[:100])
                        break
            REQ.write_bytes(original)
            if hashlib.sha256(REQ.read_bytes()).hexdigest() != \
                    hashlib.sha256(original).hexdigest():
                print("  *** 还原失败 ***")
                return 1
    finally:
        REQ.write_bytes(original)
        print()
        print("  已逐字节还原 requirements.txt。")

    print()
    print("=" * 74)
    print("  汇总")
    print("=" * 74)
    bad = [r for r in results if not r[2]]
    for name, got, ok in results:
        print("  %-48s %-8s %s" % (name[:48], got, "OK" if ok else "*** FAIL ***"))
    print()
    print("  => %s" % ("全部符合预期。" if not bad
                        else "%d 条不符合预期。" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())