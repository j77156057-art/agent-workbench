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
    #
    # **分隔符容错 {1,2}**：源码与 JSON 里写的是**转义形式**（D:\\WorkBuddy\\…），
    # 旧正则只认单个分隔符 → 整整一类写法在门禁面前是隐形的（实测：把 agent.py 的
    # 示例改成双反斜杠，门禁依然 0 命中）。
    "drive_path": re.compile(r"\b[A-Za-z]:[\\/]{1,2}(?:WorkBuddy|work|Temp|Users|study)"
                            r"[\\/]{1,2}[^\s\"'`,)\]]{0,90}"),
    # Linux 家目录（/Users/ 之外的那一半）。
    "abs_home": re.compile(r"/(?:home|Users)/[A-Za-z0-9_.-]{1,40}/"),
    # 本机状态目录里的**真实**会话文件。目录名 .docmind_sessions 本身是产品公开名，
    # 不判；判的是「目录 + 带时间戳的真实会话名」。
    "local_session_path": re.compile(r"\.docmind_sessions[\\/]{1,2}[A-Za-z0-9_.-]*-\d{8}-\d{6}"),
    # 真实会话 id：工作流（wf-）、跑批时间戳（golden-20261027-201309-G01）。
    "session_id": re.compile(r"\b(?:ask|web|dev)-[0-9a-f]{16,}\b"),
    "wf_session_id": re.compile(r"\bwf-[0-9a-f]{10,16}\b"),
    "run_stamp_id": re.compile(r"\b(?:golden|ask|web|dev|wf)-[0-9]{8}-[0-9]{6}-[A-Za-z0-9]+\b"),
    "secret": re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[bap]-[A-Za-z0-9-]{10,})"),
}

# 带理由的白名单：合成占位 id（全零），逐条计数打印，豁免量本身可见。
ALLOW = {
    "session_id": (
        re.compile(r"^(?:ask|web|dev)-0{8,}[0-9a-f]*$"),  # 全零合成占位（browser 套件默认值）
    ),
    # 工作流会话 id 的合成占位：8 个零起头 + 十六进制编号（保留「同一个 id 在多处
    # 出现」的关系，抹掉的是真实运行痕迹）。
    "wf_session_id": (re.compile(r"^wf-0{8,}[0-9a-f]*$"),),
    # Emscripten 运行时默认用户（浏览器沙箱里的固定串，不是本机身份）。
    "abs_home": (re.compile(r"^/home/web_user/?$"),),
}
# 脱敏测试夹具键：scrub/泄露守卫用例故意写的假键，不是真凭据。
FAKE_KEYS = {
    "sk-1234567890abcdef", "sk-abcdEFGH1234567890wxyz",
    "sk-REALPLAINTEXTKEY000000000000", "sk-abcdefghijklmnop",
}
# tests/ 里的盘符路径与家目录是夹具约定（C:\work 合成根、/home/u/ 合成基址）；
# 本地探针脚本（tools/）已统一
# 改用 tempfile.gettempdir()，这里保留前缀豁免只防回归，产品代码与文档仍按严判据核。
FIXTURE_PREFIXES = ("tests/", "tools/")


def tracked_files(root):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if out.returncode != 0:
        raise SystemExit("git ls-files 失败，隐私门 fail-closed")
    return [name for name in out.stdout.decode("utf-8", "replace").split("\0") if name]


def scan(root=ROOT):
    violations, allowed = [], 0
    for rel in tracked_files(root):
        # 扫描器自身的模式/注释字面量（如 /Users/ 正则片段）不是泄露；
        # 秘密扫描器排除自身是惯例，否则门会咬自己的pattern。
        if rel == "tools/privacy_scan.py":
            continue
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
                    if kind in ("drive_path", "abs_home") and rel.startswith(FIXTURE_PREFIXES):
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