"""README 里的命令必须真的能跑 —— 防文档漂移。

为什么需要这条测试
------------------
README 的示例命令写上去就没人验证了。改一次参数名、挪一次脚本位置、
改一次入口签名，文档就悄悄变成错的 —— 读者复制粘贴、报错、再回来找问题。
这条测试把「文档里的命令」变成可回归的断言。

验证范围（有意排除环境类命令）
------------------------------
README 里还有 `python -m venv` / `pip install` / `npm ci` / `npm run build` /
`uvicorn` 启动这类命令。它们的成败取决于机器环境与网络，在测试里跑既慢又不稳定，
所以**不在此验证**。这里只保证：命令所引用的**项目内脚本**存在、且能被无副作用地
调起（`--help` 退出码 0）。

这条边界是刻意的：能自动保证的部分收紧，依赖环境的部分不假装验证过。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 只抓「解释器 + 脚本路径」，不管后面跟什么参数 —— 参数会随时间变，
# 脚本路径不该变。这样新增参数不需要改测试。
SCRIPT_RE = re.compile(r'(?:python(?:\.exe)?|node)\s+([\w./\\-]+\.(?:py|mjs))', re.I)

READMES = ("README.md", "README_en.md")


def _scripts_in(text: str) -> list[str]:
    return [m.group(1).replace("\\", "/") for m in SCRIPT_RE.finditer(text)]


def _node() -> str | None:
    """CI 里 node 在 PATH；本地 Windows 常见安装位置也兜一下。"""
    found = shutil.which("node")
    if found:
        return found
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
    ):
        if Path(cand).exists():
            return cand
    return None


class ReadmeCommandsTest(unittest.TestCase):
    def test_documented_scripts_exist(self):
        """README 里出现的每个脚本路径都必须真实存在（防重命名/挪位置后文档失效）。"""
        missing: list[tuple[str, str]] = []
        for name in READMES:
            path = ROOT / name
            self.assertTrue(path.exists(), f"{name} 不存在")
            for script in _scripts_in(path.read_text(encoding="utf-8")):
                if not (ROOT / script).exists():
                    missing.append((name, script))
        self.assertEqual(missing, [], f"README 引用了不存在的脚本：{missing}")

    def test_documented_scripts_respond_to_help(self):
        """每个被引用的脚本都要能被无副作用地调起 —— 这也是"复制能跑"的最低证据。"""
        node = _node()
        seen: set[str] = set()
        broken: list[tuple[str, str, int, str]] = []
        for name in READMES:
            for script in _scripts_in((ROOT / name).read_text(encoding="utf-8")):
                if script in seen:
                    continue
                seen.add(script)
                target = ROOT / script
                if not target.exists():
                    continue  # 存在性由上一个用例负责
                if script.endswith(".mjs"):
                    if node is None:
                        self.skipTest("node 不在 PATH，跳过 mjs 命令验证")
                    cmd = [node, str(target), "--help"]
                else:
                    cmd = [sys.executable, str(target), "--help"]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=180, cwd=str(ROOT)
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "").strip()[-400:]
                    broken.append((name, script, proc.returncode, detail))
        self.assertEqual(
            broken,
            [],
            "以下 README 命令的 --help 未能正常退出（脚本签名变了或文档写错了）：\n"
            + "\n".join(f"  [{n}] {s} → exit {c}\n    {d}" for n, s, c, d in broken),
        )

    def test_every_readme_is_covered(self):
        """防止以后新增 README_xx.md 却忘了纳进验证范围。"""
        found = {p.name for p in ROOT.glob("README*.md")}
        self.assertEqual(
            found,
            set(READMES),
            f"仓库里的 README 集合变了（{sorted(found)}），请同步更新 READMES 常量",
        )

    def test_windows_block_has_no_unix_commands(self):
        """标为 cmd.exe 的安装块里不能出现 Unix 命令。

        实测踩出来的：原来写的是 `cp .env.example .env`（Unix），而整块命令是
        Windows 风格（`.venv\\Scripts\\activate`）。在装了 Git for Windows 的机器上
        它能侥幸跑通——于是没人发现——但干净 Windows 会报「不是内部或外部命令」。
        更要命的是 `cp` 会**静默覆盖已存在的 .env**，一次验证就把配置冲掉了。
        """
        unix_cmds = ("cp ", "rm ", "mv ", "chmod ", "touch ", "ln -s ")
        offenders: list[tuple[str, str]] = []
        for name in READMES:
            text = (ROOT / name).read_text(encoding="utf-8")
            for _lang, body in re.findall(r"```(\w*)\n(.*?)```", text, re.S):
                # 判定「Windows 块」：出现 cmd 风格的激活脚本或 cmd 的条件语法
                is_win = (".venv\\Scripts\\activate" in body) or ("if not exist" in body)
                if not is_win:
                    continue
                for line in body.splitlines():
                    stripped = line.strip().lower()
                    if stripped.startswith(("rem ", "#", "::")):
                        continue
                    for cmd in unix_cmds:
                        if stripped.startswith(cmd):
                            offenders.append((name, line.strip()))
        self.assertEqual(
            offenders,
            [],
            "Windows 命令块里出现了 Unix 命令（干净 Windows 上会失败，且可能覆盖已有文件）："
            f"{offenders}",
        )


if __name__ == "__main__":
    unittest.main()
