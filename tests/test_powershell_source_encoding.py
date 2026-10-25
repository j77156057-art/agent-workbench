# -*- coding: utf-8 -*-
"""钉住：含非 ASCII 文本的 `.ps1` 必须带 UTF-8 BOM。

## 这个坑是真的踩过的

`packaging/build_release.ps1` 里写的是中文提示（`throw "Python 命令失败：..."`）。
Windows PowerShell 5.1（系统自带的那个，本机 `powershell.exe` 就是它）**按
ANSI 代码页读 `.ps1`**，UTF-8 的中文字节被切成乱码后连字符串引号一起吞掉，
于是脚本**在 parse 阶段就炸**：

```
表达式或语句中包含意外的标记"}"
```

发布包就是这么打不出来的 —— 而 GitHub Actions 的 runner 是 Linux pwsh，
它按 UTF-8 读，所以 **CI 永远不会红**，只有"真要在 Windows 上打包发布"那个人
会撞上。这是最坏的一类 bug：没有报错日志、CI 全绿、只有发布流程断在半路。

## 为什么不直接要求"ps1 里别写中文"

中文提示是有用的 —— 它解释的是"为什么失败"，而失败往往发生在新同学第一次
打包的时候。要求 ASCII 等于把诊断信息删掉换绿，是倒退。加 3 个字节更便宜。

## 判据

只看**仓库自己的**脚本（不扫 `dist/` `web/` `.venv/` 等产物目录）：

- 含 >127 字节的 `.ps1` → 必须以 `EF BB BF` 开头；
- 带 BOM 的 → 正文必须是合法 UTF-8（别用 GBK 内容加个 BOM 蒙过去）。

pwsh 7 / Python / git 都能透明处理 BOM，所以这条不牺牲任何其它工具链。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOM = b"\xef\xbb\xbf"

# 产物 / 第三方目录：这些 .ps1 不是我们的，改它们等于和上游较劲。
SKIP_PARTS = ("dist/", "web/", ".venv/", ".tmp/", "node_modules/",
              ".docmind/", "build/", ".chroma/")


def _own_scripts() -> list[Path]:
    return [p for p in sorted(ROOT.rglob("*.ps1"))
            if not any(s in p.as_posix() for s in SKIP_PARTS)]


def test_repository_ships_powershell_scripts():
    """前提检查：扫描逻辑真的找到了脚本，不是 glob 写错让下面的用例空跑。"""
    scripts = _own_scripts()
    assert scripts, (
        "一个 .ps1 都没扫到 —— SKIP_PARTS 大概写宽了，"
        "那这条编码守卫就是永久绿灯的摆设")
    named = {p.name for p in scripts}
    for required in ("build_release.ps1", "uninstall_portable.ps1"):
        assert required in named, "%s 没被扫到，编码守卫覆盖不到它" % required


def test_non_ascii_scripts_carry_a_utf8_bom():
    """核心：Windows PowerShell 5.1 按 ANSI 读，中文提示会把脚本炸在 parse 阶段。"""
    offenders = []
    for p in _own_scripts():
        raw = p.read_bytes()
        if all(byte < 128 for byte in raw):
            continue                       # 纯 ASCII：没有编码问题
        if not raw.startswith(BOM):
            offenders.append(p.as_posix())
    assert not offenders, (
        "这些 .ps1 含中文但没有 UTF-8 BOM：%s\n"
        "Windows PowerShell 5.1（系统自带那个）按 ANSI 代码页读脚本，中文会被"
        "切成乱码并连带吞掉字符串引号，脚本在 parse 阶段就报"
        "「表达式或语句中包含意外的标记」—— 打包发布流程直接断掉，"
        "而 Linux 上的 CI 完全看不出来。补 3 个字节的 EF BB BF 即可。"
        % ", ".join(offenders))


def test_bom_prefixed_scripts_are_valid_utf8():
    """反向：带 BOM 不等于编码对了 —— 别用 GBK 内容加个 BOM 蒙过去。"""
    wrong = []
    for p in _own_scripts():
        raw = p.read_bytes()
        if not raw.startswith(BOM):
            continue
        try:
            raw[3:].decode("utf-8")
        except UnicodeDecodeError:
            wrong.append(p.as_posix())
    assert not wrong, (
        "这些 .ps1 带 BOM 但正文不是合法 UTF-8：%s —— BOM 只是声明，"
        "编码本身还是错的，PowerShell 5.1 仍会读坏" % ", ".join(wrong))


def test_build_script_parses_under_windows_powershell():
    """真拿 PowerShell 自己的解析器验一遍发布脚本，而不是只查字节序。

    只在 Windows 且装了系统 PowerShell 时跑；跑不了就**明确 skip**，
    绝不当成通过 —— 这条用例的价值就是"发布前在打包那台机器上验一次"。
    """
    if sys.platform != "win32":
        pytest.skip("PowerShell 5.1 的 ANSI 读取行为只发生在 Windows")
    ps = Path("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if not ps.exists():
        pytest.skip("本机没有 Windows PowerShell 5.1")

    script = ROOT / "packaging" / "build_release.ps1"
    assert script.exists(), "packaging/build_release.ps1 不见了，无法验证发布脚本"
    probe = (
        "$t=[System.IO.File]::ReadAllText('%s');"
        "$e=$null;"
        "[System.Management.Automation.PSParser]::Tokenize($t,[ref]$e)|Out-Null;"
        "if($e.Count -ne 0){Write-Output ('PARSE-ERRORS '+$e.Count);exit 1};"
        "Write-Output 'PARSE-OK'" % str(script).replace("'", "''"))
    out = subprocess.run([str(ps), "-NoProfile", "-Command", probe],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace", timeout=120)
    assert "PARSE-OK" in out.stdout, (
        "Windows PowerShell 解析 build_release.ps1 失败：%s%s"
        % (out.stdout.strip(), out.stderr.strip()[:300]))


if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))
