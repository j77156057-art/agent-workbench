"""EDA 结构化真值通路（M3）：把 Vivado / KLayout / Verdi 的 report 解析成结构化事实。

信条（docs/multimodal-verification-engine-eda-kickoff.md §0、§3.2）——先建这一层，**不碰视觉**：
- **脚本拿事实，视觉补感知。** EDA 的真相九成在综合/时序/面积/DRC 这些 report 文本里，
  能精确解析的绝不用眼睛看（视觉又慢又贵又会幻觉）。
- **fail-closed，不造假。** 解析不到就是拿不到：缺哪个字段就老实列进 `missing`，
  绝不拿上一个值或 0 糊弄。整份读不懂 → `ok:false` + 原因，而不是给个"看起来通过"的空结果。
- **每个数值都带 provenance**（报告里的原始行），可回溯、可复核，治的就是"日志一片绿但人一眼看穿"。
- **布尔判定证据不齐就不下**：`timing_met` / `drc_clean` 这类"过没过"，只有相关指标全到手
  才给 True/False，否则 `None`（uncertain），交上层合并门按未核实处理。
- **本机没有 EDA 工具也能离线跑通**：真机执行走可注入的 `tcl_runner`（默认按 `shutil.which`
  找二进制，找不到即 `tool_unavailable` fail-closed）；解析层只吃 report 文本，与 M1/M2 注入
  假 grabber 自检是同一套路数。样例工程的"综合后时序/面积/DRC"就是这么离线验收的。

依赖：只用标准库（re / xml.etree / shutil / subprocess / dataclasses），不引任何 EDA SDK。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---- 统一事实封装 -----------------------------------------------------------

# 关键：provenance 里每个字段回指报告原文的一小段（截断即可），审计时能一眼核对。
_PROV_EXCERPT_MAX = 240


@dataclass
class EdaFact:
    """一次解析的产物。`to_dict()` 是给 agent / 合并门吃的规范化结构。"""

    ok: bool
    tool: str
    kind: str
    metrics: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    provenance: list[dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None

    def add(self, name: str, value: Any, excerpt: str = "") -> None:
        self.metrics[name] = value
        if excerpt:
            self.provenance.append({"field": name, "excerpt": excerpt[:_PROV_EXCERPT_MAX]})

    def miss(self, *names: str) -> None:
        for n in names:
            if n not in self.missing:
                self.missing.append(n)

    def fail(self, reason: str) -> "EdaFact":
        self.ok = False
        self.error = reason
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "kind": self.kind,
            "metrics": self.metrics,
            "missing": self.missing,
            "provenance": self.provenance,
            "error": self.error,
        }


def _f(s: str) -> Optional[float]:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN 视为拿不到


def _i(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


# ---- Vivado：report_timing_summary -----------------------------------------

# Design Summary 里那行六个数：WNS TNS <Failing> <Total> WHS THS（点后还更多列，够前六个即可）。
_WNS_ROW = re.compile(
    r"^\s*(-?\d+\.\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(-?\d+\.\d+)"
)
# Clock 表一行：  clk_name  {0.000 3.333}  6.667  150.000   （表头没有 {}，正则天然跳过）
_CLK_ROW = re.compile(r"^\s*([A-Za-z_]\w*)\s*\{[^}]*\}\s*(\d+\.\d+)\s+(\d+\.\d+)")
# 兜底：某些 flow 只在文末写 `WNS = -0.123 ns`
_WNS_FALLBACK = re.compile(r"WNS\s*\(?(?:ns)?\)?\s*[:=]\s*(-?\d+\.\d+)")
_TNS_FALLBACK = re.compile(r"TNS\s*\(?(?:ns)?\)?\s*[:=]\s*(-?\d+\.\d+)")


def parse_vivado_timing(text: str) -> EdaFact:
    """解析 Vivado `report_timing_summary`：WNS/TNS/WHS/THS、失败端点数、最差频率。"""
    fact = EdaFact(ok=False, tool="vivado", kind="timing")
    if not text or not text.strip():
        return fact.fail("empty_report")

    lines = text.splitlines()
    header_idx = next(
        (i for i, ln in enumerate(lines) if "WNS(ns)" in ln and "TNS(ns)" in ln),
        None,
    )
    wns = tns = whs = ths = None
    failing = total = None
    if header_idx is not None:
        for ln in lines[header_idx + 1:]:
            m = _WNS_ROW.match(ln)
            if m:
                wns, tns = _f(m[1]), _f(m[2])
                failing, total = _i(m[3]), _i(m[4])
                whs, ths = _f(m[5]), _f(m[6])
                fact.add("wns_ns", wns, ln.strip())
                fact.add("tns_ns", tns, ln.strip())
                fact.add("whs_ns", whs, ln.strip())
                fact.add("ths_ns", ths, ln.strip())
                if failing is not None:
                    fact.add("tns_failing_endpoints", failing, ln.strip())
                if total is not None:
                    fact.add("total_endpoints", total, ln.strip())
                break
    # 表头在但没抓到数据行（被截断）→ 试兜底 key=val
    if wns is None:
        mb = _WNS_FALLBACK.search(text)
        if mb:
            wns = _f(mb[1])
            fact.add("wns_ns", wns, mb.group(0))
    if tns is None:
        mb = _TNS_FALLBACK.search(text)
        if mb:
            tns = _f(mb[1])
            fact.add("tns_ns", tns, mb.group(0))

    # 最差频率（周期最长的时钟 = 最低 fmax）
    best = None  # (fmax, clock, period, line)
    for ln in lines:
        m = _CLK_ROW.match(ln)
        if not m:
            continue
        clock, period, freq = m[1], _f(m[2]), _f(m[3])
        if freq is None or clock.lower() in {"clock", "waveform(ns)"}:
            continue
        if best is None or freq < best[0]:
            best = (freq, clock, period, ln.strip())
    if best:
        fact.add("fmax_mhz", best[0], best[3])
        fact.add("worst_clock", best[1], best[3])
        if best[2] is not None:
            fact.add("worst_clock_period_ns", best[2], best[3])
    else:
        fact.miss("fmax_mhz")

    # fail-closed：一个时序主指标都没抓到 → 判这份不是可解析的时序报告。
    if wns is None and tns is None:
        return fact.fail("timing_metrics_not_found")
    if wns is None:
        fact.miss("wns_ns")
    if tns is None:
        fact.miss("tns_ns")
    if whs is None:
        fact.miss("whs_ns")

    # timing_met 只在证据齐全时才下：WNS/WHS 都拿到才判；否则 uncertain(None)。
    if wns is not None and whs is not None:
        fact.add("timing_met", bool(wns >= 0.0 and whs >= 0.0))
    else:
        fact.add("timing_met", None)
        fact.miss("timing_met")

    fact.ok = True
    return fact


# ---- Vivado：report_utilization --------------------------------------------

# | Site Type      | Used | Fixed | Available | Util% |
_UTIL_ROW = re.compile(
    r"^\s*\|\s*([A-Za-z][^|]*?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|"
)
# 规范化的资源别名 → 统一键
_UTIL_ALIAS = {
    "clb luts": "lut", "lut as logic": "lut", "clb registers": "ff", "ff": "ff",
    "block ram tile": "bram", "block ram": "bram", "dsps": "dsp", "dsp": "dsp",
}


def _canon_resource(name: str) -> Optional[str]:
    key = name.strip().lower().rstrip("*").strip()
    for frag, canon in _UTIL_ALIAS.items():
        if key.startswith(frag) or frag in key:
            return canon
    return None


def parse_vivado_utilization(text: str) -> EdaFact:
    """解析 Vivado `report_utilization`：LUT/FF/BRAM/DSP 用量与占比，另给派生逻辑单元数。"""
    fact = EdaFact(ok=False, tool="vivado", kind="utilization")
    if not text or not text.strip():
        return fact.fail("empty_report")

    resources: dict[str, dict[str, Any]] = {}
    any_row = False
    for ln in text.splitlines():
        m = _UTIL_ROW.match(ln)
        if not m:
            continue
        name = m[1].strip().rstrip("*").strip()
        used, avail, util = _i(m[2]), _i(m[4]), _f(m[5])
        if used is None:
            continue
        any_row = True
        canon = _canon_resource(name)
        entry = {"raw": name, "used": used, "available": avail, "util_pct": util}
        if canon and canon not in resources:
            resources[canon] = entry
            fact.add(f"{canon}_used", used, ln.strip())
            if avail is not None:
                fact.add(f"{canon}_available", avail, ln.strip())
            if util is not None:
                fact.add(f"{canon}_util_pct", util, ln.strip())
    if not any_row:
        return fact.fail("utilization_rows_not_found")

    fact.metrics["resources"] = resources
    lut = resources.get("lut", {}).get("used")
    ff = resources.get("ff", {}).get("used")
    if lut is not None and ff is not None:
        fact.metrics["logic_cells_used"] = lut + ff
    # 四类核心资源哪个没抓到就老实列出，别当 0。
    for canon in ("lut", "ff", "bram", "dsp"):
        if canon not in resources:
            fact.miss(f"{canon}_used")
    fact.ok = True
    return fact


# ---- KLayout：DRC 报告（XML 优先，退回文本汇总）-----------------------------

def parse_klayout_drc(text: str) -> EdaFact:
    """解析 KLayout DRC 报告：每条 check 的违规数 + 总数 + 是否干净。

    KLayout 的 DRC 结果通常是 XML（<report><checks><check description=...><item/></check>…</checks>），
    这里以 ElementTree 数 `<check>` 下的 `<item>`；拿不到 XML 再退回文本里 `Total violations: N` 之类。
    """
    fact = EdaFact(ok=False, tool="klayout", kind="drc")
    if not text or not text.strip():
        return fact.fail("empty_report")

    per_check: list[dict[str, Any]] = []
    total = 0
    parsed_xml = False
    try:
        root = ET.fromstring(text)
        parsed_xml = True
        for check in root.iter("check"):
            desc = check.get("description") or check.get("name") or "unnamed"
            items = list(check.iter("item"))
            cnt = len(items)
            per_check.append({"check": desc, "violations": cnt})
            total += cnt
    except ET.ParseError:
        parsed_xml = False

    if not parsed_xml:
        # 文本兜底：KLayout DRC 收尾常见 `Total violations: N` / `N violations found`。
        m = re.search(r"total(?:\s+\w+)*\s+violations\s*[:=]\s*(\d+)", text, re.I)
        if not m:
            m = re.search(r"(\d+)\s+violations?\b", text, re.I)
        if m:
            total = _i(m[1]) or 0
            per_check.append({"check": "(summary)", "violations": total})
            fact.add("_source", "text-summary", m.group(0))
        else:
            return fact.fail("drc_report_unreadable")

    if not per_check:
        # 解析了 XML 却一条 check 都没有：可能不是 DRC 报告，也可能全 clean 但没列 check。
        # 宁可 fail-closed 说"没识别到任何检查项"，也不谎报 clean。
        return fact.fail("no_checks_parsed")

    fact.metrics["total_violations"] = total
    fact.metrics["checks"] = per_check
    # drc_clean 证据：有 check 且总数拿到才判。
    fact.add("drc_clean", bool(total == 0))
    fact.ok = True
    return fact


# ---- Verdi / VCS：仿真日志（错误/告警计数 + 是否正常收尾）--------------------

_V_ERR_CODE = re.compile(r"^\s*Error-\[", re.M)
_V_WARN_CODE = re.compile(r"^\s*Warning-\[", re.M)
_V_ERR_SUM = re.compile(r"(\d+)\s+Error", re.I)
_V_WARN_SUM = re.compile(r"(\d+)\s+Warning", re.I)
_V_FINISH = re.compile(r"\$finish\b")
_V_SIMTIME = re.compile(r"Simulation\s+Time\s*[:=]\s*([\d.]+)\s*ns", re.I)
_V_FATAL = re.compile(r"\bFATAL\b")


def parse_verdi_sim(text: str) -> EdaFact:
    """解析 VCS/Verdi 仿真日志：error/warning 数、是否 `$finish` 正常收尾、仿真时长。

    错误数优先取显式汇总行（`N Error(s)`），否则数 `Error-[` 出现次数；两者都拿不到才 miss。
    `completed=False`（没有 $finish）是**有效事实**，不是解析失败——照常 ok:true 报出去。
    """
    fact = EdaFact(ok=False, tool="verdi", kind="simulation")
    if not text or not text.strip():
        return fact.fail("empty_report")

    err_sum = _V_ERR_SUM.search(text)
    warn_sum = _V_WARN_SUM.search(text)
    if err_sum:
        fact.add("error_count", _i(err_sum[1]), err_sum.group(0))
    else:
        n = len(_V_ERR_CODE.findall(text))
        if n:
            fact.add("error_count", n)
        else:
            fact.miss("error_count")
    if warn_sum:
        fact.add("warning_count", _i(warn_sum[1]), warn_sum.group(0))
    else:
        n = len(_V_WARN_CODE.findall(text))
        if n:
            fact.add("warning_count", n)
        else:
            fact.miss("warning_count")

    completed = bool(_V_FINISH.search(text))
    fact.add("completed", completed)
    if not completed:
        fact.miss("completed")  # 没收尾 → 明确标注"未确认收尾"，不当已通过

    m = _V_SIMTIME.search(text)
    if m:
        fact.add("sim_time_ns", _f(m[1]), m.group(0))
    else:
        fact.miss("sim_time_ns")

    # fatal 是强否决信号，单独标出来。
    fact.add("has_fatal", bool(_V_FATAL.search(text)))

    # fail-closed：整份里啥有效信号都没有（无 error/warning 计数、没收尾、无时长）→ 判不是仿真日志。
    # 注意：completed=False（没 $finish）本身是有效事实，不算"没信号"；只要有任一计数或时长就正常出。
    got_error = "error_count" in fact.metrics
    got_warn = "warning_count" in fact.metrics
    got_time = "sim_time_ns" in fact.metrics
    if not (got_error or got_warn or completed or got_time):
        return fact.fail("not_a_simulation_log")
    fact.ok = True
    return fact


# ---- 分派 ------------------------------------------------------------------

_PARSERS: dict[tuple[str, str], Callable[[str], EdaFact]] = {
    ("vivado", "timing"): parse_vivado_timing,
    ("vivado", "utilization"): parse_vivado_utilization,
    ("klayout", "drc"): parse_klayout_drc,
    ("verdi", "simulation"): parse_verdi_sim,
}


def parse_report(tool: str, kind: str, text: str) -> EdaFact:
    """按 (tool, kind) 选解析器。未知组合 → fail-closed，绝不瞎猜一个默认解析。"""
    parser = _PARSERS.get((tool.lower(), kind.lower()))
    if parser is None:
        f = EdaFact(ok=False, tool=tool, kind=kind)
        return f.fail(f"unsupported_parser:{tool}/{kind}")
    return parser(text)


# ---- 真机执行（可注入 runner）----------------------------------------------

# 各工具的批处理调用模板（本机没有则 shutil.which 返回 None → tool_unavailable）。
_TOOL_COMMAND = {
    "vivado": ["vivado", "-mode", "batch", "-nojournal", "-source"],
    "klayout": ["klayout", "-rd", "mode=drc", "-zz"],
    "verdi": ["vcs"],  # 仿真由 vcs/verdi 驱动，脚本经 -f 或命令行传入
}


@dataclass
class ExecResult:
    ok: bool
    rc: Optional[int]
    stdout: str
    stderr: str
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "rc": self.rc, "stdout": self.stdout,
                "stderr": self.stderr, "error": self.error}


def default_tcl_runner(tool: str, script: str, *, cwd: Optional[str] = None,
                       timeout: Optional[float] = 600) -> ExecResult:
    """生产默认 runner：按 `shutil.which` 找工具二进制，批处理跑 `script`，回传 stdout。

    fail-closed：找不到二进制 → `ok:false, error:'tool_unavailable:<tool>'`，绝不返回空 stdout
    让上层误当成"报告显示通过"。真机联调（Linux/装了 Vivado 的机器）才走通这一路；离线测试用注入的假 runner。
    """
    argv = _TOOL_COMMAND.get(tool.lower())
    if not argv:
        return ExecResult(ok=False, rc=None, stdout="", stderr="",
                          error=f"no_command_template:{tool}")
    binary = shutil.which(argv[0])
    if not binary:
        return ExecResult(ok=False, rc=None, stdout="", stderr="",
                          error=f"tool_unavailable:{argv[0]}")
    import tempfile
    import os
    cmd = list(argv)
    # vivado/klayout 的脚本走文件参数；vcs 的脚本内联到 -f（此处保守落到临时文件）。
    if tool.lower() == "vivado":
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False, encoding="utf-8") as fh:
            fh.write(script)
            path = fh.name
        cmd.append(path)
    else:
        path = None
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout or 600)
        return ExecResult(ok=(proc.returncode == 0), rc=proc.returncode,
                          stdout=proc.stdout, stderr=proc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        return ExecResult(ok=False, rc=None, stdout="", stderr=str(exc),
                          error=f"run_failed:{type(exc).__name__}")
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def run_tcl(script: str, tool: str = "vivado", *, runner: Optional[Callable[..., ExecResult]] = None,
            cwd: Optional[str] = None, timeout: Optional[float] = 600) -> ExecResult:
    """执行 EDA 脚本。`runner` 可注入（默认 `default_tcl_runner`），便于离线自检与换后端。"""
    runner = runner or default_tcl_runner
    return runner(tool, script, cwd=cwd, timeout=timeout)


def run_and_parse(tool: str, kind: str, script: str, *,
                  runner: Optional[Callable[..., ExecResult]] = None,
                  cwd: Optional[str] = None, timeout: Optional[float] = 600) -> EdaFact:
    """跑脚本 → 拿 stdout → 解析成结构化事实。工具不可用/执行失败一律 fail-closed，不猜。"""
    res = run_tcl(script, tool, runner=runner, cwd=cwd, timeout=timeout)
    if not res.ok:
        f = EdaFact(ok=False, tool=tool, kind=kind)
        return f.fail(res.error or f"run_returned_nonzero:{res.rc}")
    return parse_report(tool, kind, res.stdout)
