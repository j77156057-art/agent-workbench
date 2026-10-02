"""`dev_job_logs` 的 fold/level 胶水层的变异电池（TARGET 是 tools.py）。

模块本体由 `battery_log_view` 管；这块只管"参数有没有真的落到模块、视图模式有没有谎报
可续读、原始读法有没有被偷偷改写"——这类漏接本仓为 `coverage:` 付过学费。
"""

BATTERY_NAME = 'job_logs'
MODULE = 'tools'
TARGET = 'tools.py'
CASE_ID = 'log-view-collapses-real-spam'
TEST_MODULE = 'tests.test_log_view'
BIND = 'tools'
UNIT_CLASSES = ('ToolSurfaceTests',)

MINIMUM_MUTATIONS = 6      # 删一条变异要显式改这里
LIVE = ()

#: 这个测试模块里除 ToolSurfaceTests 之外都是 log_view 本体的用例，
#: 由 battery_log_view（TARGET=log_view.py）判；这块只变异 tools.py 的胶水。
LAYER_EXEMPT = {
    "RealLogTests": "log_view 本体由 battery_log_view 判",
    "SignatureTests": "log_view 本体由 battery_log_view 判",
    "LevelTests": "log_view 本体由 battery_log_view 判",
    "HonestyTests": "log_view 本体由 battery_log_view 判",
    "RenderTests": "log_view 本体由 battery_log_view 判",
}

MUTATIONS = [
    ("level 没传给视图（level: warn 静默不生效）",
     '    view = log_view.summarize(str(report.get("logs") or ""), level=level, fold=folding)',
     '    view = log_view.summarize(str(report.get("logs") or ""), fold=folding)'),
    ("视图模式继续谎报 next_offset（下一次还以为是增量）",
     '    if offset:\n        report["next_offset"] = None',
     '    if False:\n        report["next_offset"] = None'),
    ("原始读法也被折叠改写（默认行为变了而没人要求）",
     '            report["view_hint"] = ("有 %d 行是重复的（加 fold: true 折成 %d 行；只看问题加 "',
     '            report["logs"] = log_view.summarize(str(report.get("logs") or ""))["text"]\n'
     '            report["view_hint"] = ("有 %d 行是重复的（加 fold: true 折成 %d 行；只看问题加 "'),
    ("没有重复也硬塞 view_hint",
     '        if stats["ok"] and (stats["folded_away"] >= 5 or stats["middle_omitted_chars"]):',
     '        if stats["ok"]:'),
    ("视图计数被整块丢掉（只剩折过的文本，无法核查）",
     '    report["view"] = {k: view[k] for k in (',
     '    report["view"] = {k: 0 for k in ('),
    ("offset 不再传给上游（续读位置被静默忽略）",
     '    report = process_runner.job_logs(job_id, offset=offset)',
     '    report = process_runner.job_logs(job_id, offset=0)'),
]
