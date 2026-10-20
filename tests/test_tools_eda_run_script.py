"""agent 工具 `eda_run_script` 的注册与行为守卫（离线，不装 EDA 工具也能跑）。

背景：后端早就能真跑 Vivado/KLayout（`agent_runtime/eda_tcl.run_and_parse`），
但此前既没有 HTTP 端点也没有 agent 工具 —— 模型只能解析**已有的**报告文本，
拿不到「帮我跑一遍时序报告」这种必须真跑工具才有事实的能力。

覆盖：
- 注册完整性：进 TOOLS、带 input_schema(tool/kind/script required)、被归类为 EXEC
  （执行类能力 → 只读角色拿不到它）、声明了 domain/facet（过 test_tool_domains 守卫）。
- 护栏：TCL 里的删文件 / 拉子进程 / 越界绝对路径 / 凭据路径一律 script_blocked，
  **且不执行**（离线也拦得住 —— 护栏在执行器的出口，先于真正跑工具）。
- fail-closed：工具没装 → tool_unavailable，绝不返回空输出冒充「报告显示通过」；
  (tool,kind) 无解析器 → unsupported_parser。
- 输出形状：给模型的是紧凑观察（判定 + 依据 + provenance），不是原始 stdout 倾倒。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
from agent_runtime import eda_tcl  # noqa: E402
from agent_runtime import tool_domains as D  # noqa: E402
from agent_runtime.tools import Capability, SideEffect, coerce_tool_spec  # noqa: E402

#: agent.py 里判定"工具失败"的标记串（本工具的正常/不确定输出都不该命中，
#: 否则会被当崩溃重试）。与 test_tools_eda_report_gate.py 同源。
FAILURE_MARKERS = (
    "计算失败", "搜索失败", "搜索未返回结果", "交通票价查询失败", "天气查询失败",
    "字幕提取失败", "读取失败", "文件不存在", "拒绝访问", "未提供", "安全限制",
    "拒绝写入", "参数缺失",
)

TIMING_MET = """
Design Timing Summary
---------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
    -------      -------  ---------------------  -------------------      -------      -------
      0.412        0.000                      0                52314        0.035        0.000

Clock  Waveform (ns)         Period(ns)      Frequency(MHz)
-----  ------------           ----------      -------------
sys_clk  {0.000 2.500}        5.000           200.000
"""


def _no_marker(text):
    return not any(m in text for m in FAILURE_MARKERS)


def _run(monkeypatch, root, arg, runner=None):
    """跑一次工具：把 code_root 指到 root，可选注入假 runner。"""
    monkeypatch.setattr(tools, "_get_code_root", lambda: str(root))
    if runner is not None:
        monkeypatch.setattr(eda_tcl, "default_tcl_runner", runner)
    return tools.eda_run_script(arg)


# ---- 注册完整性 ----

def test_tool_is_registered_with_schema():
    entry = tools.TOOLS["eda_run_script"]
    assert callable(entry["func"])
    schema = entry["input_schema"]
    assert set(schema["required"]) == {"tool", "kind", "script"}
    assert {"tool", "kind", "script", "timeout"} <= set(schema["properties"])


def test_tool_classified_as_exec():
    spec = coerce_tool_spec("eda_run_script", tools.TOOLS["eda_run_script"])
    # EXEC → 只读角色不会拿到它，且 tool_mutates 判它"会改东西"。
    assert spec.capability == Capability.EXEC
    assert spec.side_effect == SideEffect.MUTATING
    assert spec.parallel_safe is False


def test_tool_declares_a_domain():
    assert D.TOOL_DOMAINS.get("eda_run_script"), "eda_run_script 未声明 domain/facet"


# ---- 护栏（在执行器出口，离线也拦得住）----

def test_guard_blocks_dangerous_tcl_operations():
    for script, hint in (
        ("file delete -force old.v", "file delete"),
        ("exec ls /tmp", "exec"),
        ("exec cmd.exe /c dir", "exec"),
        ("!ls", "shell"),
        ("file rename a b", "file rename"),
        ("cd C:/Windows", "cd"),
    ):
        allowed, why = eda_tcl.guard_tcl_script(script, root=os.getcwd())
        assert not allowed, "该拦没拦：%r" % script
        assert why, "拦下但没给原因：%r" % script


def test_guard_allows_benign_report_script():
    allowed, why = eda_tcl.guard_tcl_script(
        "open_run report_timing_summary -file timing.rpt\nputs done", root=os.getcwd())
    assert allowed, why


def test_guard_blocks_absolute_path_outside_root(tmp_path):
    outside = str(tmp_path.parent / "elsewhere" / "x.rpt").replace("\\", "/")
    allowed, why = eda_tcl.guard_tcl_script('open %s' % outside, root=str(tmp_path))
    assert not allowed and "越界" in why


def test_guard_blocks_credential_paths():
    allowed, why = eda_tcl.guard_tcl_script("open .env", root=os.getcwd())
    assert not allowed and "凭据" in why


def test_blocked_script_is_not_executed(monkeypatch, tmp_path):
    called = []

    def spy(tool, script, *, cwd=None, timeout=None):
        called.append(script)
        return eda_tcl.ExecResult(ok=True, rc=0, stdout="", stderr="")

    out = json.loads(_run(monkeypatch, tmp_path, json.dumps({
        "tool": "vivado", "kind": "timing", "script": "file delete -force all.v"}), runner=spy))
    assert not called, "被拦下的脚本绝不能真跑"
    assert out["ok"] is False
    assert "script_blocked" in (out["error"] or "")


# ---- fail-closed ----

def test_tool_fails_closed_when_tool_missing(monkeypatch, tmp_path):
    def missing(tool, script, *, cwd=None, timeout=None):
        return eda_tcl.ExecResult(ok=False, rc=None, stdout="", stderr="",
                                   error="tool_unavailable:%s" % tool)

    out = json.loads(_run(monkeypatch, tmp_path, json.dumps({
        "tool": "vivado", "kind": "timing", "script": "report_timing_summary"}), runner=missing))
    assert out["ok"] is False
    assert "tool_unavailable" in (out["error"] or "")
    assert not out.get("metrics"), "拿不到事实时不得给指标"


def test_unsupported_tool_kind_fails_closed(monkeypatch, tmp_path):
    def ok_runner(tool, script, *, cwd=None, timeout=None):
        return eda_tcl.ExecResult(ok=True, rc=0, stdout="junk", stderr="")

    out = json.loads(_run(monkeypatch, tmp_path, json.dumps({
        "tool": "nope", "kind": "nope", "script": "x"}), runner=ok_runner))
    assert out["ok"] is False
    assert "unsupported_parser" in (out["error"] or "")


def test_missing_script_returns_needs(monkeypatch, tmp_path):
    out = json.loads(_run(monkeypatch, tmp_path, json.dumps({"tool": "vivado", "kind": "timing"})))
    assert out["ok"] is False
    assert "script" in (out.get("needs") or [])


def test_requires_code_root(monkeypatch):
    monkeypatch.setattr(tools, "_get_code_root", lambda: "")
    out = tools.eda_run_script(json.dumps({"tool": "vivado", "kind": "timing", "script": "x"}))
    assert "尚未配置代码库" in out


# ---- 正常路径 ----

def test_parses_real_report_into_structured_facts(monkeypatch, tmp_path):
    def runner(tool, script, *, cwd=None, timeout=None):
        return eda_tcl.ExecResult(ok=True, rc=0, stdout=TIMING_MET, stderr="")

    out = json.loads(_run(monkeypatch, tmp_path, json.dumps({
        "tool": "vivado", "kind": "timing", "script": "report_timing_summary"}), runner=runner))
    assert out["ok"] is True
    assert out["metrics"]["wns_ns"] == 0.412
    assert out["provenance"], "每个数值都要能回指报告原行"
    assert _no_marker(json.dumps(out, ensure_ascii=False)), "正常输出不能命中失败标记串"


def test_output_is_compact_not_raw_stdout(monkeypatch, tmp_path):
    def runner(tool, script, *, cwd=None, timeout=None):
        return eda_tcl.ExecResult(ok=True, rc=0, stdout="X" * 5000 + TIMING_MET, stderr="")

    raw = _run(monkeypatch, tmp_path, json.dumps({
        "tool": "vivado", "kind": "timing", "script": "report_timing_summary"}), runner=runner)
    assert len(raw) < 4000, "不该把原始 stdout 整段倾倒给模型：%d 字符" % len(raw)
    assert "XXXX" not in raw
