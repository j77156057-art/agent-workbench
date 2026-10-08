"""实测：只给 `proxy_tools` 一行加 `--hash`，会不会让 pip 要求**整个文件**进 hash 模式。

## 为什么必须先问这个

钉住 `proxy_tools==0.1.0` 看起来是「十分钟小活」。但如果给那一行加上
`--hash=sha256:...` 会触发 pip 的 **hash-checking mode** —— 而那个模式下
**所有行都必须带 hash**，否则报错。

那么「加一行 hash」就等于「172 行全量 hash 钉死」（= 方案 B），
我原先的代价估计就**完全错了**。

所以这一行是「小活」还是「全量工程」，取决于 pip 的这条规则 ——
**不能假设，必须跑。**

## 判据

三组对照，各自只改一行：

| 组 | requirements 内容 |
|---|---|
| 基线 | 42 条原样 |
| 实验 A | 42 条 + `proxy_tools==0.1.0`（**无 hash**） |
| 实验 B | 42 条 + `proxy_tools==0.1.0 --hash=sha256:ccb3…` |

* A 成功、B 成功 → 只钉版本即可，不必 hash（hash 是额外的、可能也是多余的）
* A 成功、**B 失败** → **hash 不能单独加**，只能用版本约束
* A 失败 → 连版本约束都会破坏现有安装（那说明有更深的问题）

## 安全约束

* 全程 `--dry-run`，**不动 `.venv`**
* 只在临时目录操作，**不碰仓库里的 requirements.txt**
* 只装 2 个包（`proxy_tools` + 一个无关的），让失败尽快暴露
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv/Scripts/python.exe")
REQ = ROOT / "requirements.txt"

PROXY_HASH = "sha256:ccb3751f529c047e2d8a58440d86b205303cf0fe8146f784d1cbcd94f0a28010"


def run(argv: list[str], cwd: pathlib.Path, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run([PY] + argv, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def probe(label: str, extra_lines: list[str], install_args: list[str]) -> tuple[int, str]:
    """在临时目录里造一份最小 requirements，跑 pip install --dry-run。

    ⚠ **`--ignore-installed` 是必需的**：第一版四组全返回
    "Requirement already satisfied" —— pip 看到本地已装就跳过了，
    **根本没下载任何东西**，于是「hash 会不会强制校验」这个问题
    **一次都没被验证**。

    **判据必须排除「什么都没发生」这个可能** —— 只看退出码 0 不够：
    「跳过」与「成功」是同一个读数。所以下面另报一个
    「pip 到底有没有真的去解析」的读数。
    """
    with tempfile.TemporaryDirectory() as tmp:
        req = pathlib.Path(tmp) / "requirements.txt"
        # 只放两行：一个稳定的包 + 那一行被试的。
        # 放 42 行会让失败被别的原因淹没（缺 wheel 的平台特定包等）。
        req.write_text("\n".join(extra_lines) + "\n", encoding="utf-8")
        rc, out = run(["-m", "pip", "install", "--dry-run", "--no-deps",
                       "--ignore-installed", "-r", str(req)] + install_args,
                      cwd=pathlib.Path(tmp))
    print()
    print("  " + "-" * 70)
    print("  %s" % label)
    print("  " + "-" * 70)
    for line in extra_lines:
        print("    requirements: %s" % line[:88])
    print("    退出码 = %d" % rc)
    interesting = [ln for ln in out.splitlines()
                   if any(k in ln for k in ("ERROR", "Hashes are required",
                                            "hash", "Would install",
                                            "Collecting", "Downloading",
                                            "Using cached", "no matching",
                                            "Cannot install", "THESE PACKAGES"))]
    for ln in interesting[:8]:
        print("      | %s" % ln.strip()[:104])
    if not interesting:
        for ln in out.splitlines()[-4:]:
            print("      | %s" % ln.strip()[:104])

    did_work = any(k in out for k in ("Collecting", "Downloading",
                                      "Using cached", "Would install",
                                      "hash of file"))
    skipped = "already satisfied" in out
    print("    >> pip 是否真的解析了: %s%s"
          % ("是" if did_work else "*** 否 -> 实验无效 ***",
             "（仅因已装而跳过）" if skipped else ""))
    return rc, out


def main() -> int:
    print("=" * 74)
    print("  前提：仓库里的 requirements.txt 不会被改")
    print("=" * 74)
    original = REQ.read_bytes()
    print("  requirements.txt sha256 = %s" % __import__("hashlib")
          .sha256(original).hexdigest()[:32])

    results = {}
    try:
        # 基线：只装 proxy_tools，不带约束
        results["基线"] = probe(
            "基线：proxy_tools（无约束）", ["proxy_tools"], [])

        # A：版本钉死，无 hash
        results["A. 只钉版本"] = probe(
            "A：proxy_tools==0.1.0（无 hash）", ["proxy_tools==0.1.0"], [])

        # B：版本 + hash
        results["B. 版本 + hash"] = probe(
            "B：proxy_tools==0.1.0 --hash=...",
            ["proxy_tools==0.1.0 --hash=%s" % PROXY_HASH], [])

        # C：**混合文件** —— 一行带 hash、另一行不带（模拟真加到仓库里）
        results["C. 混合（一行带 hash + 一行不带）"] = probe(
            "C：混合 —— proxy_tools 带 hash、idna 不带",
            ["proxy_tools==0.1.0 --hash=%s" % PROXY_HASH, "idna"],
            [])

        # D：**故意用错 hash** —— 这才是 hash 的强判据。
        # 正确 hash 通过只是弱证据（可能根本没校验）；错 hash 必须失败，
        # 才证明 pip **真的在校验内容**。
        results["D. 错 hash（必须失败）"] = probe(
            "D：故意错的 hash —— 期望失败",
            ["proxy_tools==0.1.0 --hash=sha256:%s" % ("0" * 64)],
            [])
    finally:
        import hashlib
        if hashlib.sha256(REQ.read_bytes()).hexdigest() != \
                hashlib.sha256(original).hexdigest():
            print("\n  *** requirements.txt 被改了！立即还原 ***")
            REQ.write_bytes(original)
            raise SystemExit(1)
        print("\n  requirements.txt 未被改动（sha256 校验通过）。")

    print()
    print("=" * 74)
    print("  判定")
    print("=" * 74)
    labels = ["基线", "A. 只钉版本", "B. 版本 + hash",
              "C. 混合（一行带 hash + 一行不带）", "D. 错 hash（必须失败）"]
    for label in labels:
        rc = results[label][0]
        print("    %-36s 退出码 %d" % (label, rc))

    base_ok = results["基线"][0] == 0
    a_ok = results["A. 只钉版本"][0] == 0
    b_ok = results["B. 版本 + hash"][0] == 0
    c_ok = results["C. 混合（一行带 hash + 一行不带）"][0] == 0
    d_failed = results["D. 错 hash（必须失败）"][0] != 0

    print()
    if not base_ok:
        print("  => 基线就失败（网络/环境问题），本次实验不成立。")
        return 1
    if not a_ok:
        print("  => **连只钉版本都失败。** 必须查清原因，别硬上。")
        return 2

    print("  A（只钉版本）可行：%s" % ("是" if a_ok else "否"))
    print("  B（单行带 hash）可行：%s" % ("是" if b_ok else "否"))
    print("  C（多行文件里混一个带 hash）可行：%s" % ("是" if c_ok else "否"))
    print("  D（错 hash 被拒绝）—— **hash 真的在校验内容**：%s"
          % ("是" if d_failed else "*** 否 —— hash 是摆设 ***"))
    print()

    if a_ok and b_ok and c_ok and d_failed:
        print("  => **可以只给 `proxy_tools` 一行钉版本 + hash，不牵连其他行。**")
        print("     而 D 组证明 pip **真的在校验内容** —— 那是这一行唯一的价值，")
        print("     只钉 `==0.1.0` 只能防「版本漂移」，防不住「同版本内容被替换」。")
        return 0
    if a_ok and b_ok and c_ok and not d_failed:
        print("  => 能装上，但**错 hash 也没被拒** —— pip 在这个组合下不校验内容。")
        print("     那 `--hash` 是摆设，**只能钉版本**。")
        return 0
    if a_ok and not (b_ok and c_ok):
        print("  => **单独加 `--hash` 会牵连其他行** —— 「只钉这一个包」做不到。")
        print("     只能用版本约束（`==`），hash 留给全量方案。")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())