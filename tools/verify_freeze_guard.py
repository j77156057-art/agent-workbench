"""验证 freeze_guard 真能抓住「跑测期间改被测文件」。

## ⚠ 上一版验证脚本自己是恒真的

它只看 stdout 里有没有「测量被污染」。**插件压根没加载成功**时
（`ModuleNotFoundError: tools is not a package`），stdout 里当然没有那串字
-> A 关判 False，B/C 关判 True -> 输出「B 没误报、C 删了就不报」，
**看起来三关都过，其实是「守卫根本没运行」**。

更糟的是 A 关 False 被当成「没抓到」，而真因（加载失败）被丢掉了。

本版加一条**加载自检**：stdout 里必须出现 `[freeze-guard] 已盯 N 个`，
否则直接判失败 —— 不许把「没运行」当成「没报警」。

## 四关

  L 加载自检：插件真的加载了吗（否则后面全是空判）
  A 正例  ：跑测期间改 agent.py -> 必须抓到，且说清哪个文件
  B 反例  ：什么都不改       -> 必须**不报**（否则恒真）
  C 恒真  ：删掉核心比对     -> 必须**抓不到**（证明它在干活）

B 关只能证明「不误报」，C 关才能证明「真在干活」。
"""

import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = pathlib.Path(r"D:\Temp\opencode\freeze_violations.json")
AGENT = ROOT / "agent.py"
GUARD = ROOT / "tools" / "pytest_freeze_guard.py"
AGENT_ORIG = AGENT.read_text(encoding="utf-8")
GUARD_ORIG = GUARD.read_text(encoding="utf-8")

PROBE = ROOT / "tests" / "test_freeze_probe.py"


def run_probe(touches):
    """跑一个三用例的小套件；中间那条按 `touches` 决定是否改 agent.py。

    ⚠ 探针**必须真的 import agent**。上一版只 import pathlib，于是 `agent.py`
    从没进 `sys.modules`，守卫根本没机会盯它 —— 那时 A 关失败是**探针的错**，
    不是守卫的错。守卫只跟「已被加载的模块」，它不是静态目录扫描器。
    """
    PROBE.write_text(
        "import pathlib, unittest\n"
        "\n"
        "TARGET = pathlib.Path(__file__).resolve().parents[1] / 'agent.py'\n"
        "import agent                      # 必须真的加载被测模块\n"
        "\n"
        "\n"
        "class ProbeTests(unittest.TestCase):\n"
        "    def test_a_first(self):\n"
        "        assert TARGET.exists()\n"
        "        assert agent.__file__.endswith('agent.py')\n"
        "\n"
        "    def test_b_touch(self):\n"
        "        if %r:\n"
        "            t = TARGET.read_text(encoding='utf-8')\n"
        "            TARGET.write_text(t + '\\n# 污染\\n', encoding='utf-8')\n"
        "\n"
        "    def test_c_last(self):\n"
        "        assert TARGET.exists()\n" % touches,
        encoding="utf-8")
    env = dict(os.environ)
    env["DOCMIND_FREEZE_VERBOSE"] = "1"
    env["PYTHONPATH"] = str(ROOT / "tools")
    r = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-m", "pytest",
         "tests/test_freeze_probe.py", "-q", "-p", "no:cacheprovider",
         "--no-header", "--tb=no", "-p", "pytest_freeze_guard"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env)
    PROBE.unlink(missing_ok=True)
    return (r.stdout or "") + (r.stderr or "")


def loaded(out):
    return "[freeze-guard] 已盯" in out


def reported(out):
    return "测量被污染" in out


try:
    print("=" * 76)
    print("  关 L：加载自检（不许把「没运行」当成「没报警」）")
    print("=" * 76)
    out0 = run_probe(0)
    ok_load = loaded(out0)
    print("  stdout 含「已盯 N 个」:", ok_load)
    if not ok_load:
        print("  *** 插件没加载，后面所有判据都是空判 ***")
        print(out0[:800])
    else:
        for l in out0.splitlines():
            if "freeze-guard" in l:
                print("   ", l.strip())

    print()
    print("=" * 76)
    print("  关 A：跑测期间改 agent.py -> 必须抓到")
    print("=" * 76)
    REPORT.unlink(missing_ok=True)
    out_a = run_probe(1)
    caught_a = reported(out_a) and loaded(out_a)
    print("  插件已加载:", loaded(out_a))
    print("  报出污染  :", caught_a)
    if REPORT.exists():
        v = json.loads(REPORT.read_text(encoding="utf-8"))
        print("  违规条数  :", len(v))
        print("  指向文件  :", [os.path.basename(x["file"]) for x in v][:3])
        print("  类型      :", [x["kind"] for x in v][:3])
        print("  首个发现于:", v[0]["test"])
    else:
        print("  报告文件  : *** 未落盘 ***")
    for l in out_a.splitlines():
        if "freeze-guard" in l or "被测文件" in l or "agent.py" in l:
            print("   ", l.strip()[:78])

    AGENT.write_text(AGENT_ORIG, encoding="utf-8")
    REPORT.unlink(missing_ok=True)

    print()
    print("=" * 76)
    print("  关 B：什么都不改 -> 必须**不报**")
    print("=" * 76)
    out_b = run_probe(0)
    caught_b = reported(out_b) and loaded(out_b)
    print("  插件已加载:", loaded(out_b))
    print("  误报      :", caught_b, " <- 必须 False")
    print("  报告落盘  :", REPORT.exists(), " <- 必须 False")

    print()
    print("=" * 76)
    print("  关 C：删掉核心比对 -> 必须抓不到（证明它在真干活）")
    print("=" * 76)
    broken = GUARD_ORIG.replace("        if now != before:",
                                "        if False:   # >>> 故意删掉比对 <<<")
    assert broken != GUARD_ORIG, "C 关锚点没命中"
    GUARD.write_text(broken, encoding="utf-8")
    try:
        REPORT.unlink(missing_ok=True)
        out_c = run_probe(1)
    finally:
        GUARD.write_text(GUARD_ORIG, encoding="utf-8")
        AGENT.write_text(AGENT_ORIG, encoding="utf-8")
    caught_c = reported(out_c) and loaded(out_c)
    print("  比对删掉后仍报警:", caught_c, " <- 必须 False")
    REPORT.unlink(missing_ok=True)
finally:
    AGENT.write_text(AGENT_ORIG, encoding="utf-8")
    GUARD.write_text(GUARD_ORIG, encoding="utf-8")
    PROBE.unlink(missing_ok=True)

print()
print("=" * 76)
print("  判定")
print("=" * 76)
print("  L 插件加载   :", ok_load)
print("  A 抓到了     :", caught_a)
print("  B 没误报     :", not caught_b)
print("  C 删了就不报 :", not caught_c)
print()
print("  agent.py 已还原:", AGENT.read_text(encoding="utf-8") == AGENT_ORIG)
print("  guard  已还原  :", GUARD.read_text(encoding="utf-8") == GUARD_ORIG)
print()
ok = ok_load and caught_a and not caught_b and not caught_c
print("  结论:", "守卫有效，且不是摆设" if ok else "*** 守卫无效 ***")
sys.exit(0 if ok else 1)
