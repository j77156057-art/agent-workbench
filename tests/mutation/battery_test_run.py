"""`dev_testrun` 的变异电池。

关键一条：`tests_collected` 由三个信号合成（计数 / 退出码 5 / "no tests ran"），我最初的用例
把三个信号同时设成"坏"，于是删掉任何一个都照样全绿——改成**每个用例让另外两个信号说"绿"**，
两条变异当场被抓住。
"""

BATTERY_NAME = 'test_run'
MODULE = 'agent_runtime.test_run'
TARGET = 'agent_runtime/test_run.py'
CASE_ID = 'testrun-rerereads-failures-not-just-an-exit-code'
TEST_MODULE = 'tests.test_test_run'
BIND = 'tr'
UNIT_CLASSES = (
        'ParseTests',
    'CommandShapeTests',
    'NodeidGuardTests',
    'BookkeepingTests',
    'RunTests',
    'RelatedTests',
    'LocationTests',
    'RenderTests',)
LIVE = ()

#: 故意不作为电池的层：它们要么慢得离谱、要么必须 import 整个工具注册面，
#: 与「这条守卫会不会红」无关。写在这里必须说清原因（否则就该进 UNIT_CLASSES）。
LAYER_EXEMPT = {'RealPytestTests': '真起 pytest 子进程：慢，且判的是运行器不是本模块', 'ToolSurfaceTests': '要 import tools 全量注册面；与「这条守卫会不会红」无关'}

#: 本块电池的变异条数下限：删一条变异必须显式改这里，「守卫悄悄变少」不该是绿灯
MINIMUM_MUTATIONS = 18

MUTATIONS = [
    ("退出码 5 也算收集到（0 收集被读成通过）",
     '"tests_collected": bool(parsed["collected"]) and exit_code != 5',
     '"tests_collected": bool(parsed["collected"])'),
    ("no_tests_ran 不参与判定",
     'and not parsed["no_tests_ran"],',
     'and True,'),
    ("读不到摘要也算通过",
     '"summary_parsed": parsed["summary_parsed"],',
     '"summary_parsed": True,'),
    ("计数与退出码不必自洽",
     '"counts_agree_with_exit": (exit_code == 0) == (problems == 0) if exit_code is not None',
     '"counts_agree_with_exit": True if exit_code is not None'),
    ("未知退出码也放行",
     '"runner_ran": exit_code in KNOWN_EXIT_CODES,',
     '"runner_ran": True,'),
    ("超时不说自己没跑完",
     'if rep.get("timed_out"):',
     'if False:'),
    ("记账把 total 存成截断后的清单长度（整片红守卫失效）",
     '"total": len(nodeids), "truncated": len(nodeids) > MAX_NODEIDS,',
     '"total": min(len(nodeids), MAX_NODEIDS), "truncated": len(nodeids) > MAX_NODEIDS,'),
    ("非法 nodeid 也进记账",
     'nodeids = [row["nodeid"] for row in rows if valid_nodeid(row["nodeid"])]',
     'nodeids = [row["nodeid"] for row in rows]'),
    ("nodeid 不做形状校验",
     'return bool(clean) and not clean.startswith("-") and ".." not in clean \\',
     'return bool(clean) and not clean.startswith("-") and False \\'),
    ("不禁用 cacheprovider（每次都刷 Permission denied）",
     '"-p", "no:cacheprovider",',
     '"-p", "randomly",'),
    ("basetemp 不钉在项目内",
     '"--basetemp=%s" % basetemp]',
     '"--basetemp=/tmp/nope"]'),
    ("路径不做校验就起进程",
     'if item.startswith("-") or ".." in item or Path(item).is_absolute():',
     'if False:'),
    ("相干选择为空时退化成跑全量",
     'if not related_info["picked"]:',
     'if False:'),
    ("tb 行数与失败数不等时硬配对（把 A 的消息挂到 B 上）",
     'if trace and len(trace) == len(rows):',
     'if trace:'),
    ("标准库帧被当成现场",
     'if location and base and not location.startswith(base + "/"):',
     'if False:'),
    ("ERROR 行不列进失败清单",
     'hit = _NODEID_RE.match(line.strip())',
     'hit = re.match(r"^(FAILED)\\s+(\\S+)(?:\\s+-\\s+(.*))?$", line.strip())'),
    ("没记账也照样跑一次",
     'if not record["nodeids"]:',
     'if False:'),
    ("缺覆盖率依赖时不开口，静默当普通跑",
     'if coverage and not coverage_available():',
     'if False:'),
]
