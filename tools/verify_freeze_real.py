"""真实场景验证：守卫在**真实源码扫描测试**旁边能不能抓到污染。

## 上一版为什么没抓到

我用 PowerShell 的 `Start-Job` + `Start-Sleep -Seconds 3` 注入。实测
`agent.py` 确实被改了（第 6758 行，`git diff` 确认 +1 行），守卫却报
「全程无污染」。

两个可能：
  (a) 注入发生在**基线建立之前** —— 守卫盯着改后的状态，自然不报
  (b) 守卫真漏了

区分：把注入放进**用例执行期**（和 verify 脚本里已验证有效的那种一样）。
若那样能抓到 => 是 (a)，守卫没漏，是我注入时机错了。

顺带看：被改坏之后，那些真实扫描测试会不会红 —— 这才是事故现场。
"""

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"
ORIG = AGENT.read_text(encoding="utf-8")
REPORT = pathlib.Path(r"D:\Temp\opencode\real_freeze3.json")
PROBE = ROOT / "tests" / "test_freeze_realprobe.py"

PROBE_SRC = '''import pathlib
import unittest

import agent  # noqa: F401  必须真的加载被测模块

TARGET = pathlib.Path(__file__).resolve().parents[1] / "agent.py"


class RealProbe(unittest.TestCase):
    def test_a1_modify(self):
        """在**用例执行期**改 agent.py —— 这才是真的「跑测期间改被测文件」。"""
        t = TARGET.read_text(encoding="utf-8")
        TARGET.write_text(t + "\\n# 中途插入的一行（真实场景注入）\\n",
                          encoding="utf-8")
'''

SCAN_TESTS = ["tests/test_channel_protocol_consistency.py",
              "tests/test_stall_probe.py"]

env = dict(os.environ)
env["DOCMIND_FREEZE_VERBOSE"] = "1"
env["DOCMIND_FREEZE_REPORT"] = str(REPORT)
REPORT.unlink(missing_ok=True)
PROBE.write_text(PROBE_SRC, encoding="utf-8")

try:
    r = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "pytest",
         *SCAN_TESTS, "tests/test_freeze_realprobe.py",
         "-q", "-p", "no:cacheprovider", "--no-header", "--tb=no",
         "-p", "pytest_freeze_guard"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env)
    out = (r.stdout or "") + (r.stderr or "")
finally:
    PROBE.unlink(missing_ok=True)
    AGENT.write_text(ORIG, encoding="utf-8")

print("=" * 76)
print("  真实场景：源码扫描测试 + 用例执行期改 agent.py")
print("=" * 76)
for l in out.splitlines():
    s = l.strip()
    if ("freeze-guard" in s or "污染" in s or s.endswith("passed")
            or s.endswith("failed") or "agent.py " in s):
        print("   ", s[:86])

print()
print("=" * 76)
print("  判定")
print("=" * 76)
loaded = "[freeze-guard] 已盯" in out
caught = "测量被污染" in out
print("  守卫已加载        :", loaded)
print("  报出污染          :", caught)
if REPORT.exists():
    v = json.loads(REPORT.read_text(encoding="utf-8"))
    print("  违规条数          :", len(v))
    print("  指向              :", [os.path.basename(x["file"]) for x in v][:4])
    print("  发现于            :", v[0]["test"])
else:
    print("  报告              : *** 未落盘 ***")

print()
if caught:
    print("  ✅ 守卫在真实场景下有效 —— 上一版没抓到是**我注入时机错了**")
    print("     （固定 sleep 3 秒落在基线建立之前）")
else:
    print("  ❌ 守卫在真实场景下仍漏 —— 这是守卫的真缺陷，必须继续修")

print()
print("  agent.py 已还原:", AGENT.read_text(encoding="utf-8") == ORIG)
