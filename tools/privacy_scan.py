# -*- coding: utf-8 -*-
"""公开仓库隐私门：扫描 git 跟踪文本里的本机路径 / 真实会话 id / 密钥样式。

背景（2026-11-16/17 实测）：基线假化靠手工替换，第一遍漏了 dev-* 会话 id，
靠一次性全量扫描才补上。「需要人记得」的假化不是机制——这道门把那次扫描
变成常驻判据：任何新入库文本带真实会话 id / 本机盘符路径 / 密钥样式即红。

判据：
  * 只扫 git 跟踪文件（未跟踪的本机产物不该入库，另有 .gitignore 管）；
  * 跳过二进制与超大文件（>2MB）；
  * ALLOW 是带理由的白名单（通用系统路径 / 合成占位 id），逐条计数打印，
    豁免量本身可见，不许静默。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf",
            ".zip", ".gz", ".sqlite3", ".pyc", ".pdf", ".mp3", ".mp4", ".webm", ".glb",
            ".bin", ".exe", ".dll", ".so", ".dylib", ".lock"}
MAX_BYTES = 2 * 1024 * 1024

PATTERNS = {
    # 只认真本机根：示例路径（D:\MyGame、D:\path\to 之类）不泄露机器身份，
    # 而 WorkBuddy/work/Temp/Users/study 下的路径带开发者目录与项目名。
    "drive_path": re.compile(r"\b[A-Za-z]:[\\/](?:WorkBuddy|work|Temp|Users|study)[\\/][^\s\"'`,)\]]{0,90}"),
    "user_dir": re.compile(r"(?:C:\\\\?Users\\\\?|/Users/)[A-Za-z0-9_' -]{1,40}"),
    "session_id": re.compile(r"\b(?:ask|web|dev)-[0-9a-f]{16,}\b"),
    "secret": re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{10,})"),
}
# 带理由的白名单：合成占位 id（全零），逐条计数打印，豁免量本身可见。
ALLOW = {
    "session_id": (
        re.compile(r"^(?:ask|web|dev)-0{8,}[0-9a-f]*$"),  # 全零合成占位（browser 套件默认值）
    ),
}
# 脱敏测试夹具键：scrub/泄露守卫用例故意写的假键，不是真凭据。
FAKE_KEYS = {
    "sk-1234567890abcdef", "sk-abcdEFGH1234567890wxyz",
    "sk-REALPLAINTEXTKEY000000000000", "sk-abcdefghijklmnop",
}
# tests/ 与 tools/ 里的盘符路径是夹具约定（C:\work 合成根、D:/Temp/opencode
# _scratch 约定），非真本机根；产品代码与文档仍按严判据核。
FIXTURE_PREFIXES = ("tests/", "tools/")


def tracked_files(root):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if out.returncode != 0:
        raise SystemExit("git ls-files 失败，隐私门 fail-closed")
    return [name for name in out.stdout.decode("utf-8", "replace").split("\0") if name]


def scan(root=ROOT):
    violations, allowed = [], 0
    for rel in tracked_files(root):
        ext = os.path.splitext(rel)[1].lower()
        if ext in SKIP_EXT:
            continue
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > MAX_BYTES:
                continue
            with open(path, "rb") as handle:
                raw = handle.read()
            if b"\0" in raw[:4096]:
                continue
            text = raw.decode("utf-8", "replace")
        except OSError:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            for kind, pat in PATTERNS.items():
                for m in pat.finditer(line):
                    val = m.group(0)
                    if any(a.match(val) for a in ALLOW.get(kind, ())):
                        allowed += 1
                        continue
                    if kind == "secret" and val in FAKE_KEYS:
                        allowed += 1
                        continue
                    if kind == "drive_path" and rel.startswith(FIXTURE_PREFIXES):
                        allowed += 1
                        continue
                    violations.append("%s:%d [%s] %s" % (rel, no, kind, val[:90]))
    return violations, allowed


def main():
    violations, allowed = scan()
    print("隐私门：白名单豁免 %d 处（通用系统路径/合成占位）" % allowed)
    if violations:
        print("不通过 %d 处：" % len(violations))
        for item in violations[:80]:
            print("  ·", item)
        return 1
    print("未发现本机路径 / 真实会话 id / 密钥样式")
    return 0


if __name__ == "__main__":
    sys.exit(main())