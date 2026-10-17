# -*- coding: utf-8 -*-
"""A1 修复回归：eda_report_gate 的多字段入参不再只认 JSON。

模型 ReAct 首拍常把入参写成换行 `key: value`（YAML 风格），原生 function-calling 通道
经 `llm.args_to_input` 也会把多字段 dict 扁平成同一形状；此前 `_parse_eda_report_gate_request`
只认 JSON，于是这两种合理写法一律被打回 `{}` → needs[tool,kind] → 首拍必坏。
本测试钉住：兜底 keyed 解析能救回这些形状、结构化字段按类型还原、且 fail-closed 不破。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402
from llm import args_to_input  # noqa: E402

TIMING_MET = (
    "Design Timing Summary\n"
    "---------------------\n"
    "    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)\n"
    "      0.412        0.000                      0                52314        0.035        0.000\n"
    "Clock  Waveform (ns)         Period(ns)      Frequency(MHz)\n"
    "sys_clk  {0.000 2.500}        5.000           200.000\n"
)


# ---- 单元：_parse_eda_report_gate_request ----

def test_json_path_still_works():
    req = tools._parse_eda_report_gate_request(
        json.dumps({"tool": "vivado", "kind": "timing", "fetch": "inline"}))
    assert req == {"tool": "vivado", "kind": "timing", "fetch": "inline"}


def test_dict_passthrough():
    d = {"tool": "vivado", "kind": "drc"}
    assert tools._parse_eda_report_gate_request(d) is d


def test_keyvalue_react_yaml_style():
    # 模型首拍写成换行 key: value
    arg = ("tool: vivado\n"
           "kind: timing\n"
           "fetch: inline\n"
           "content: " + TIMING_MET + "\n"
           "visual: {\"verdict\": \"pass\"}")
    req = tools._parse_eda_report_gate_request(arg)
    assert req.get("tool") == "vivado"
    assert req.get("kind") == "timing"
    assert req.get("fetch") == "inline"
    assert "Design Timing Summary" in (req.get("content") or "")
    assert req.get("visual") == {"verdict": "pass"}  # JSON 值还原成 dict


def test_keyvalue_native_flattened_python_repr():
    # 原生通道：args_to_input 把多字段 dict 扁平成 key: value，dict/list/bool 变 Python repr
    native = {"tool": "vivado", "kind": "timing", "fetch": "inline",
              "content": TIMING_MET, "visual": {"verdict": "regression"},
              "vlm_verified": True, "critical": ["timing_met"]}
    arg = args_to_input(json.dumps(native))
    # 确认 args_to_input 确实产出的是非 JSON 的 key: value 形状（这正是 A1 的触发条件）
    assert arg.lstrip().startswith("tool:")
    req = tools._parse_eda_report_gate_request(arg)
    assert req.get("tool") == "vivado"
    assert req.get("kind") == "timing"
    assert req.get("visual") == {"verdict": "regression"}   # literal_eval 救回
    assert req.get("vlm_verified") is True                   # 字符串 "True" → bool
    assert req.get("critical") == ["timing_met"]             # 字符串 "['timing_met']" → list


def test_multiline_content_preserved():
    body = "line one\nline two\nstill content\n"
    arg = "tool: klayout\nkind: drc\ncontent: " + body + "fetch: inline"
    req = tools._parse_eda_report_gate_request(arg)
    assert "line one" in req["content"] and "still content" in req["content"]
    assert req["content"].strip().endswith("still content")
    assert req.get("fetch") == "inline"  # content 值在下一个已知键处正确截断


def test_fail_closed_no_tool_kind():
    # 认不出 tool/kind 的乱码 → 维持旧行为：返回 {} 交给 needs 提示，绝不猜
    assert tools._parse_eda_report_gate_request("just some random prose") == {}
    assert tools._parse_eda_report_gate_request("foo: bar\nbaz: qux") == {}
    assert tools._parse_eda_report_gate_request("") == {}


def test_visual_unparseable_is_dropped():
    # visual 值解析不出 dict → 丢弃（None），上层按缺视觉证据处理（fail-closed），不当通过
    arg = "tool: vivado\nkind: timing\nfetch: inline\ncontent: x\nvisual: not a dict"
    req = tools._parse_eda_report_gate_request(arg)
    assert req.get("tool") == "vivado"
    assert req.get("visual") is None


# ---- 端到端：eda_report_gate 直接吃 key: value 串也能出门禁观察 ----

def test_tool_accepts_keyvalue_end_to_end():
    arg = ("tool: vivado\n"
           "kind: timing\n"
           "fetch: inline\n"
           "content: " + TIMING_MET + "\n"
           "visual: {\"verdict\": \"pass\"}")
    out = json.loads(tools.eda_report_gate(arg))
    assert out["ok"] is True
    assert out.get("needs") is None                       # 不再首拍哑火
    assert out["structured"]["fact_ok"] is True
    assert out["gate"]["state"] == "met"
    assert out["gate"]["decision"] == "accept"
    assert out["executes_rollback"] is False


def test_tool_keyvalue_non_report_escalates():
    arg = ("tool: vivado\nkind: timing\nfetch: inline\n"
           "content: 某厂商宣布战略投资，加速构建数字电路全流程工具系统。\n"
           "visual: {\"verdict\": \"pass\"}")
    out = json.loads(tools.eda_report_gate(arg))
    assert out["ok"] is True
    assert out["structured"]["fact_ok"] is False
    assert out["gate"]["state"] == "uncertain"
    assert out["gate"]["decision"] == "escalate"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
