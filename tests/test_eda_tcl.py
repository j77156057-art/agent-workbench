"""M3 EDA 结构化通路：报告解析 → 结构化事实的行为守卫（纯离线，无需 EDA 工具）。

验收目标对齐开工文档 §4 M3："一个样例工程能拿到综合后时序/面积/DRC 数"——
这里用烤真的 report 文本喂解析器，断言：
- 数值/布尔/失败计数被正确抠出，且带 provenance 可回溯；
- timing_met / drc_clean 只在证据齐时是布尔，否则 None（不下没根据的结论）；
- 截断 / 空 / 脏 / 非本类报告 → 一律 fail-closed（ok:false），绝不谎报通过或补 0；
- 缺失的具体字段老实进 missing，不被 0 顶替；
- 注入假 runner 的 run_and_parse 集成路径（工具不可用也 fail-closed）。
"""
from agent_runtime.eda_tcl import (
    EdaFact,
    ExecResult,
    parse_report,
    parse_vivado_timing,
    parse_vivado_utilization,
    parse_klayout_drc,
    parse_verdi_sim,
    run_and_parse,
    run_tcl,
)

# ---- 真实形状的报告样本 ----------------------------------------------------

VIVADO_TIMING_MET = """
----------------------------------------------------------------
| Tool Version : Vivado v.2023.2 (win64) Build 3865809
| Design       : soc_top
| Device       : xc7k325tfb676-1
----------------------------------------------------------------

Design Timing Summary
---------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints     WPWS(ns)     TPWS(ns)
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------     --------     --------
      0.412        0.000                      0                52314        0.035        0.000                      0                 52314        1.500        0.000

Clock  Waveform (ns)         Period(ns)      Frequency(MHz)
-----  ------------         ----------      -------------
sys_clk  {0.000 2.500}        5.000           200.000
slow_clk  {0.000 8.333}       16.667          60.000
"""

VIVADO_TIMING_VIOLATED = """
Design Timing Summary
---------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)
     -0.123       -1.560                     42                52314        0.021        0.000
"""

VIVADO_UTIL_FULL = """
3. SLR Resources
-------------------
    | Site Type      | Used | Fixed | Available | Util% |
    | CLB LUTs*      | 1234 |     0 |     20150 |  6.12 |
    | CLB Registers* |  567 |     0 |     40300 |  1.41 |
    | Block RAM Tile |   12 |     0 |       110 | 10.91 |
    | DSPs           |    8 |     0 |       240 |  3.33 |
"""

VIVADO_UTIL_NO_DSP = """
    | Site Type      | Used | Fixed | Available | Util% |
    | CLB LUTs*      |  100 |     0 |     20150 |  0.50 |
    | CLB Registers* |   50 |     0 |     40300 |  0.12 |
"""

KLayout_DRC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<report>
  <description>DRC run: metal rules</description>
  <checks>
    <check description="M1.1: min metal spacing">
      <item layer="M1"/>
      <item layer="M1"/>
      <item layer="M1"/>
    </check>
    <check description="M1.2: min metal width">
      <item layer="M1"/>
    </check>
    <check description="contact.enclosure">
    </check>
  </checks>
</report>
"""

KLayout_DRC_CLEAN = """<?xml version="1.0" encoding="UTF-8"?>
<report><checks><check description="M1.1"><item layer="M1"/></check></checks></report>
""".replace('<item layer="M1"/>', '')  # 唯一 check 0 item → clean

KLayout_NO_CHECKS = """<?xml version="1.0" encoding="UTF-8"?>
<report><checks></checks></report>
"""

VERDI_WITH_ERRORS = """
Chronologic VCS simulator (64-bit)
Parsing design file "tb.sv"
Error-[SE] Syntax error
Error-[UM] Undefined module
Warning-[TMBIN] Too many braces
5 Errors
12 Warnings
"""

VERDI_CLEAN_FINISH = """
Parsing design file "tb.sv"
0 Error
0 Warning
Simulation Time: 123456.0 ns
$finish called from file "tb.sv", line 88.
"""


# ---- Vivado 时序 -----------------------------------------------------------

def test_timing_met_positive():
    f = parse_vivado_timing(VIVADO_TIMING_MET)
    assert f.ok is True
    assert f.metrics["wns_ns"] == 0.412
    assert f.metrics["tns_ns"] == 0.0
    assert f.metrics["whs_ns"] == 0.035
    assert f.metrics["timing_met"] is True
    # 最差频率 = 频率最低那个时钟
    assert f.metrics["fmax_mhz"] == 60.0
    assert f.metrics["worst_clock"] == "slow_clk"
    assert f.metrics["total_endpoints"] == 52314


def test_timing_violated_negative():
    f = parse_vivado_timing(VIVADO_TIMING_VIOLATED)
    assert f.ok is True
    assert f.metrics["wns_ns"] == -0.123
    assert f.metrics["timing_met"] is False  # WNS<0 → 时序没守住


def test_timing_provenance_present():
    f = parse_vivado_timing(VIVADO_TIMING_MET)
    fields = {p["field"] for p in f.provenance}
    assert "wns_ns" in fields  # 数值可回溯到报告原行


def test_timing_keyval_fallback_and_uncertain_met():
    # 没有表头，只有 `WNS = ... / TNS = ...`：wns/tns 拿到，但 whs 缺失 → timing_met 不下结论。
    f = parse_vivado_timing("WNS = -0.234 ns\nTNS = -3.500 ns\n")
    assert f.ok is True
    assert f.metrics["wns_ns"] == -0.234
    assert f.metrics["tns_ns"] == -3.5
    assert f.metrics["timing_met"] is None
    assert "whs_ns" in f.missing
    assert "fmax_mhz" in f.missing


def test_timing_empty_failclosed():
    f = parse_vivado_timing("")
    assert f.ok is False
    assert f.error == "empty_report"


def test_timing_no_metrics_failclosed_not_fake_pass():
    # 一份跟时序无关的文本 → 必须判拿不到，而不是给出 ok:true 的空壳。
    f = parse_vivado_timing("just some unrelated notes about the design flow\nwith no numbers\n")
    assert f.ok is False
    assert f.error == "timing_metrics_not_found"


# ---- Vivado 面积/资源 ------------------------------------------------------

def test_utilization_full():
    f = parse_vivado_utilization(VIVADO_UTIL_FULL)
    assert f.ok is True
    assert f.metrics["lut_used"] == 1234
    assert f.metrics["ff_used"] == 567
    assert f.metrics["bram_used"] == 12
    assert f.metrics["dsp_used"] == 8
    assert f.metrics["logic_cells_used"] == 1234 + 567
    assert f.metrics["lut_util_pct"] == 6.12
    assert f.metrics["lut_available"] == 20150
    assert f.missing == []


def test_utilization_missing_resource_goes_to_missing_not_zero():
    # 报告里没 DSP/BRAM 行 → 列进 missing，绝不悄悄当 0。
    f = parse_vivado_utilization(VIVADO_UTIL_NO_DSP)
    assert f.ok is True
    assert f.metrics["lut_used"] == 100
    assert "dsp_used" in f.missing
    assert "bram_used" in f.missing
    assert "dsp_used" not in f.metrics  # 缺失字段不能同时又是值


def test_utilization_no_rows_failclosed():
    f = parse_vivado_utilization("no table here\n")
    assert f.ok is False
    assert f.error == "utilization_rows_not_found"


# ---- KLayout DRC -----------------------------------------------------------

def test_drc_counts_per_check_and_total():
    f = parse_klayout_drc(KLayout_DRC_XML)
    assert f.ok is True
    assert f.metrics["total_violations"] == 4
    assert f.metrics["drc_clean"] is False
    per = {c["check"]: c["violations"] for c in f.metrics["checks"]}
    assert per["M1.1: min metal spacing"] == 3
    assert per["contact.enclosure"] == 0


def test_drc_clean():
    f = parse_klayout_drc(KLayout_DRC_CLEAN)
    assert f.ok is True
    assert f.metrics["total_violations"] == 0
    assert f.metrics["drc_clean"] is True


def test_drc_no_checks_does_not_lie_clean():
    # 解析了 XML 却一条 check 都没有 → fail-closed，绝不谎报 clean。
    f = parse_klayout_drc(KLayout_NO_CHECKS)
    assert f.ok is False
    assert f.error == "no_checks_parsed"
    assert "drc_clean" not in f.metrics


def test_drc_text_fallback():
    f = parse_klayout_drc("DRC run finished.\nTotal violations: 7\n")
    assert f.ok is True
    assert f.metrics["total_violations"] == 7


def test_drc_unreadable_failclosed():
    f = parse_klayout_drc("random text with no violation info at all")
    assert f.ok is False
    assert f.error == "drc_report_unreadable"


# ---- Verdi / VCS 仿真 ------------------------------------------------------

def test_sim_with_errors():
    f = parse_verdi_sim(VERDI_WITH_ERRORS)
    assert f.ok is True
    assert f.metrics["error_count"] == 5
    assert f.metrics["warning_count"] == 12
    assert f.metrics["completed"] is False
    assert "completed" in f.missing  # 没收尾要明标未确认，不当跑完


def test_sim_clean_finish():
    f = parse_verdi_sim(VERDI_CLEAN_FINISH)
    assert f.ok is True
    assert f.metrics["error_count"] == 0
    assert f.metrics["completed"] is True
    assert f.metrics["sim_time_ns"] == 123456.0
    assert f.metrics["has_fatal"] is False


def test_sim_not_a_log_failclosed():
    f = parse_verdi_sim("totally unrelated prose\nno signals here\n")
    assert f.ok is False
    assert f.error == "not_a_simulation_log"


# ---- 统一分派 + 封装 -------------------------------------------------------

def test_parse_report_dispatch_unknown_failclosed():
    f = parse_report("quartus", "power", "whatever")
    assert f.ok is False
    assert f.error == "unsupported_parser:quartus/power"


def test_edafact_to_dict_shape():
    d = parse_vivado_timing(VIVADO_TIMING_MET).to_dict()
    assert set(d) == {"ok", "tool", "kind", "metrics", "missing", "provenance", "error"}
    assert d["tool"] == "vivado" and d["kind"] == "timing"


# ---- 注入 runner 的集成路径 ------------------------------------------------

def test_run_and_parse_with_fake_runner():
    def fake_runner(tool, script, *, cwd=None, timeout=None):
        return ExecResult(ok=True, rc=0, stdout=VIVADO_TIMING_MET, stderr="")
    f = run_and_parse("vivado", "timing", "report_timing_summary", runner=fake_runner)
    assert f.ok is True
    assert f.metrics["wns_ns"] == 0.412


def test_run_and_parse_tool_unavailable_failclosed():
    def fake_runner(tool, script, *, cwd=None, timeout=None):
        return ExecResult(ok=False, rc=None, stdout="", stderr="", error="tool_unavailable:vivado")
    f = run_and_parse("vivado", "timing", "report_timing_summary", runner=fake_runner)
    assert f.ok is False
    assert f.error == "tool_unavailable:vivado"  # 工具没起来，绝不返回"看起来通过"


def test_run_tcl_uses_injected_runner():
    seen = {}
    def fake_runner(tool, script, *, cwd=None, timeout=None):
        seen["tool"] = tool; seen["script"] = script
        return ExecResult(ok=True, rc=0, stdout="x", stderr="")
    res = run_tcl("do something", "klayout", runner=fake_runner)
    assert res.ok is True
    assert seen["tool"] == "klayout" and seen["script"] == "do something"
