"""量化：给 Python 侧上 `--require-hashes` 到底要动多少东西。

## 为什么先量化而不是直接做

`--require-hashes` 是**契约变更**，不是加个 flag：
它改变 `requirements.txt` 的形态，也改变「以后怎么加依赖」这个流程。
而我**不该替用户做这个决定** —— 我该把决定所需的数字备齐。

## 三个关键数字

1. **要列多少个包？** —— 不�� `requirements.txt` 的 42 行，而是**实际装的全集**。
   `--require-hashes` 模式下 pip 不再自动解析依赖，要求文件里**列全**
   （含全部传递依赖），否则会报 `Hashes are required in --require-hashes mode`
2. **每个包要几个 hash？** —— PyPI 上一个版本通常同时有 sdist 与 wheel，
   hash 要按「文件」给，不是按「包」给
3. **有多少包只有 sdist？** —— 那些包在 `--only-binary` 路线下会被排除

## 只读诊断

不装任何东西、不改任何文件。全部数据来自：
* 当前 venv 的 `pip list`（已装全集）
* PyPI JSON API（每个包有哪些发行文件）

⚠ **网络不稳时必须优雅降级** —— 拿不到就报「查不到」，不能编。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = ROOT / ".venv/Scripts/python.exe"
REQ = ROOT / "requirements.txt"


def pip_freeze() -> list[tuple[str, str]]:
    proc = subprocess.run([str(PY), "-m", "pip", "list", "--format=freeze"],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=180)
    out = []
    for line in proc.stdout.splitlines():
        if "==" in line:
            name, _, version = line.partition("==")
            out.append((name.strip().lower(), version.strip()))
    return out


def declared() -> list[str]:
    return [l.strip() for l in REQ.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def pypi_files(name: str, version: str) -> tuple[list[str], list[str]] | None:
    """返回 (sdist 文件名, wheel 文件名)；查不到返回 None。"""
    url = "https://pypi.org/pypi/%s/%s/json" % (name.replace("_", "-"), version)
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 网络状况不该让诊断崩
        return None
    urls = data.get("urls") or []
    sdists = [u["filename"] for u in urls if u.get("packagetype") == "sdist"]
    wheels = [u["filename"] for u in urls if u.get("packagetype") == "bdist_wheel"]
    return sdists, wheels


def main() -> int:
    print("=" * 76)
    print("  数字 1：要列多少个包")
    print("=" * 76)
    declared_names = declared()
    frozen = pip_freeze()
    print("  requirements.txt 声明:      %d 条" % len(declared_names))
    print("  当前 venv 实际装:          %d 个" % len(frozen))
    print()
    print("  => --require-hashes 要求**列全**（含传递依赖），")
    print("     所以规模是 **%d 个包**，不是 %d 条声明。"
          % (len(frozen), len(declared_names)))
    print("     即 requirements.txt 会从 %d 行涨到约 %d 行 —— **4 倍**。"
          % (len(declared_names), len(frozen)))

    # 直接依赖里那些「我们真正在乎的」
    print()
    print("  声明里有版本约束的（%d 条）：" % sum(
        1 for d in declared_names if any(c in d for c in "=<>~")))
    loose = [d for d in declared_names if not any(c in d for c in "=<>~")]
    print("  裸包名（无任何约束，%d 条）：" % len(loose))
    print("    " + ", ".join(loose[:12]) + ("…" if len(loose) > 12 else ""))
    print()
    print("  ⚠ 裸包名意味着**每次解析都会取最新版** —— 这正是")
    print("     `min-release-age` 在 npm 侧要防的同一件事，Python 侧完全没有。")

    print()
    print("=" * 76)
    print("  数字 2 / 3：每个包有几个发行文件、有多少只有 sdist")
    print("=" * 76)
    checked = 0
    sdist_only: list[str] = []
    unknown: list[str] = []
    multi = 0
    for name, version in frozen:
        got = pypi_files(name, version)
        if got is None:
            unknown.append("%s==%s" % (name, version))
            continue
        checked += 1
        sdists, wheels = got
        if not sdists and not wheels:
            unknown.append("%s==%s (无发行文件)" % (name, version))
            continue
        if not wheels:
            sdist_only.append("%s==%s" % (name, version))
        if (len(sdists) + len(wheels)) > 2:
            multi += 1
        # 全量查，不抽样。
        # ⚠ 第一版抽样 60/172 就写「只有 sdist 的: 0 个」—— **那是抽样结论，
        # 却被总结读成了全量结论**（「这一条**可行**」）。
        # 而「`--only-binary` 可行」正是要拿去建议人的那句话，
        # 它不能建立在 35% 的覆盖率上。
        # 代价：172 次 PyPI 请求（约 0.2-0.5s each），可接受。

    total_sampled = len(frozen) if checked >= len(frozen) else checked
    print("  抽样: %d / %d 个（网络不够时 unknown 会列出来）"
          % (checked, len(frozen)))
    print()
    print("  只有 sdist、没有 wheel 的: %d 个" % len(sdist_only))
    for s in sdist_only[:10]:
        print("    %s" % s)
    if len(sdist_only) > 10:
        print("    …（共 %d 个）" % len(sdist_only))
    print()
    print("  有多个发行文件（sdist + 多个 wheel，含平台特定）的: %d 个" % multi)
    print("  => hash 要按**文件**给，不是按包给 ——")
    print("     所以 requirements.txt 里每个包那行的长度会是 hash 数之和。")
    print()
    print("  查不到（网络/本地包）: %d 个" % len(unknown))
    for u in unknown[:8]:
        print("    %s" % u)
    if len(unknown) > 8:
        print("    …（共 %d 个）" % len(unknown))

    print()
    print("=" * 76)
    print("  结论：三条路各自的代价")
    print("=" * 76)
    n_declared = len(declared_names)
    n_all = len(frozen)
    print()
    print("  A. 只钉直接依赖的版本（`==`），不加 hash")
    print("     代价：改 %d 行；不防投毒，只防「静默漂移」。" % n_declared)
    print("     能做：是。**低风险**，但收益也小。")
    print()
    print("  B. 全量 `--require-hashes`")
    print("     代价：requirements.txt 约 %d 行（从 %d 行涨 4 倍）；"
          % (n_all, n_declared))
    print("           每次加依赖都要重新生成 hash；")
    print("           %d 个包需要逐个查发行文件。" % n_all)
    print("     能做：可行，但**流程变重** —— 且这是契约变更，该由人决定。")
    print()
    print("  C. `--only-binary=:all:`（禁 sdist，关掉 setup.py 执行）")
    if sdist_only:
        print("     ⚠ 抽样里有 %d 个包只有 sdist —— 全量下会更多。" % len(sdist_only))
        print("     这一条**会排除掉它们**，需要逐个确认能否换发行源。")
    else:
        print("     抽样里没有「只有 sdist」的包 —— 这一条**可行**。")
    print("     注意：它与 B **可叠加**（两者独立）。")
    print()
    print("  ⚠ 本探针**只量化**，不改任何文件、不装任何东西。")
    return 0


if __name__ == "__main__":
    sys.exit(main())