"""`voice_loop` 的变异电池。

Wave8 语音行为回路里那段胶水是照 `AutonomousCockpit.vue` **转写**的，不是从 `.vue` import
出来的，所以它自己必须被守着：判定表里每一条（静音门限、尾巴内间隔、barge-in 标签）
都对应一次真实的改错。
"""

BATTERY_NAME = 'voice_loop'
MODULE = 'agent_runtime.voice_loop'
TARGET = 'agent_runtime/voice_loop.py'
CASE_ID = 'voice-glue-catches-unconditional-pumping'
TEST_MODULE = 'tests.test_voice_loop'
BIND = 'vl'
UNIT_CLASSES = (
        'PageGlueTests',
    'BundleTests',
    'TailStatsTests',
    'VerdictTableTests',
    'ToolSurfaceTests',)
LIVE = ()

#: 故意不作为电池的层：它们要么慢得离谱、要么必须 import 整个工具注册面，
#: 与「这条守卫会不会红」无关。写在这里必须说清原因（否则就该进 UNIT_CLASSES）。
LAYER_EXEMPT = {'LiveVoiceLoopTests': '现场类，但没有一条变异只有现场才看得见——跑它是白等'}

#: 本块电池的变异条数下限：删一条变异必须显式改这里，「守卫悄悄变少」不该是绿灯
MINIMUM_MUTATIONS = 16

MUTATIONS = [
    # (名字, 原文, 变异后)
    ("tail_stats 用回尾内间隔（gaps[after-1]）",
     'gap = round(float(gaps[after]) if after < len(gaps) else 0.0, 3)',
     'gap = round(float(gaps[after - 1]) if after - 1 < len(gaps) else 0.0, 3)'),
    ("尾后截断仍算停了（旧的 stopped_then_nothing 洞）",
     'stopped = ("yes" if trailing_observation >= RESUME_GAP_SECONDS else "not_observed")',
     'stopped = "yes"'),
    ("单包也算 sequence 单调",
     '"sequence_monotonic": len(sequences) >= 2 and len(sequences) == len(audio)',
     '"sequence_monotonic": len(sequences) >= 1'),
    ("非整数 sequence 不过滤（字符串比较蒙混）",
     'sequences = [int(value) for value in raw_sequences if isinstance(value, int)]',
     'sequences = [0, 1]'),
    ("多条连接也算一条",
     '"single_connection": int(connections or 0) == 1,',
     '"single_connection": int(connections or 0) >= 1,'),
    ("探针说收口就算收口（不再要求页面收到）",
     'for row in sent) and "model.delta" in server_events,',
     'for row in sent),'),
    ("console 错误不进判定",
     'for item in (console_errors or [])]',
     'for item in (console_errors and [] or [])]'),
    ("failed_requests 不进判定",
     'page_failed = [str(item.get("url") or "") for item in (failed_requests or [])]',
     'page_failed = []'),
    ("tail 上限不再判无条件推流",
     '"tail_is_bounded_not_always_pumping": 0 < stats["tail_after_speech"] <= tail_max,',
     '"tail_is_bounded_not_always_pumping": True,'),
    ("静音期漏流不设限",
     '"gate_closed_during_silence": stats["leading_quiet"] <= allowed_leading,',
     '"gate_closed_during_silence": True,'),
    ("GLUE_CALLS 退回整页找名字",
     'body = page.split("parseRealtimeServerEvent } from", 1)',
     'body = ["", page]'),
    ("render 打印常量而不是实际阈值",
     'thresholds = result.get("thresholds") or {}',
     'thresholds = {"tail_min": TAIL_MIN, "tail_max": TAIL_MAX}'),
    ("开麦时重置 sequence（与生产不一致，序号撞车）",
     "gate = createVoiceGate(); pump = createChunkPump();\n    state.mic = 'on';",
     "gate = createVoiceGate(); pump = createChunkPump(); sequence = 0;\n    state.mic = 'on';"),
    ("编译产物不再先删旧文件",
     '            out.unlink()',
     '            pass'),
    ("esbuild 空产物也算成功",
     'if not out.is_file() or out.stat().st_size == 0:',
     'if not out.is_file():'),
    ("click 标签写成元组（真机现场才会暴露的那种）",
     'return ("连接", "开麦", "AI 开始说话") if barge_in else ("连接", "开麦")',
     'return (("连接",), ("开麦",), ("AI 开始说话",)) if barge_in else (("连接",), ("开麦",))'),
]
