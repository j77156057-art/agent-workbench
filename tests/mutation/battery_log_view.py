"""`log_view`（重复折叠 + 级别筛选）的变异电池。

这块的牙不在"折得狠"，而在三件容易糊过去的事：签名归一化、级别继承（traceback 正文）、
以及计数自洽。所以变异也照这三类摆。工具胶水由 `battery_job_logs` 单独管。
"""

BATTERY_NAME = 'log_view'
MODULE = 'agent_runtime.log_view'
TARGET = 'agent_runtime/log_view.py'
CASE_ID = 'log-view-collapses-real-spam'
TEST_MODULE = 'tests.test_log_view'
BIND = 'lv'
UNIT_CLASSES = (
        'RealLogTests',
        'SignatureTests',
        'LevelTests',
        'HonestyTests',
        'RenderTests',)

MINIMUM_MUTATIONS = 17      # 删一条变异要显式改这里
LIVE = ()

#: ToolSurfaceTests 测的是 tools.py 的胶水，这块电池变异的是 log_view 本体，
#: 交给 battery_job_logs 判——列在这里只会让纯层多跑一遍不相关的用例。
LAYER_EXEMPT = {"ToolSurfaceTests": "由 battery_job_logs 判（那块才变异 tools.py）"}

MUTATIONS = [
    # ---- 签名归一化：整行去重在这份真日志上是 0% 增益，全靠它 ----
    ("数字不再归一（access 行的端口各不相同，一行都折不动）",
     '    (re.compile(r"\\d+"), "#"),\n',
     ''),
    ("签名只比前 10 个字符（200 OK 与 500 会被折成同一段）",
     '    return text[:200]',
     '    return text[:10]'),
    ("uuid 不再归一（同一件事因请求 id 不同而折不动）",
     '    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
     '[0-9a-fA-F]{12}"),\n     "#uuid"),\n',
     ''),
    ("长十六进制 id 不再归一",
     '    (re.compile(r"\\b[0-9a-fA-F]{16,}\\b"), "#hex"),\n',
     ''),
    # ---- 折叠门槛与「关掉折叠真的不折」 ----
    ("折叠门槛降到 1（两行也折，主视图开始吞信息）",
     'MAX_FOLD_RUN = 3',
     'MAX_FOLD_RUN = 1'),
    ("折叠门槛高到永远达不到（等于没折，却还报 folded_away）",
     'MAX_FOLD_RUN = 3',
     'MAX_FOLD_RUN = 9999'),
    ("fold=False 仍然折（关掉折叠变成谎报）",
     '        elif fold and row["times"] >= MAX_FOLD_RUN:',
     '        elif row["times"] >= MAX_FOLD_RUN:'),
    ("折掉的段不保留最后一行（看不出刷到什么时候停）",
     '            out_lines.append(row["last"])\n            folded_away += row["times"] - 2',
     '            folded_away += row["times"] - 1'),
    # ---- 级别：下限、继承、保守方向 ----
    ("级别筛选写反（变成只看低于所选级别的行）",
     '        if floor and grade in LEVELS and LEVELS.index(grade) < keep_from:',
     '        if floor and grade in LEVELS and LEVELS.index(grade) > keep_from:'),
    ("无级别行不再继承（traceback 正文与它的标题脱钩）",
     '            out.append((text, current, True))',
     '            out.append((text, "", True))'),
    ("未知级别不再拒绝（静默当成不过滤）",
     '    if floor and floor not in LEVELS:',
     '    if False:'),
    # ---- 计数与缺口：视图不许冒充全文 ----
    ("合成提示行不再记账（进出对不上也看不出来）",
     '            markers_added += 1',
     '            markers_added += 0'),
    ("缓冲丢掉的中段不再解析（视图开始冒充完整日志）",
     '    omitted = sum(int(m) for m in OMITTED_MARK.findall(raw))',
     '    omitted = 0'),
    ("被筛掉的 4xx/5xx 不再计数（warn 视图被读成没有错误）",
     '            if FAILURE_MARK.search(row["first"]) or FAILURE_MARK.search(row["last"]):\n'
     '                dropped_failures += 1',
     '            if False:\n                dropped_failures += 1'),
    ("行数上限失效（一次吞下整块大日志还不说）",
     '    overflow = max(0, len(lines) - MAX_LOG_LINES)',
     '    overflow = 0'),
    ("排行按次数升序（最该看见的重复排在最后）",
     '    rank_rows = sorted(rank.items(), key=lambda kv: (-kv[1]["times"], kv[0]))[:TOP_GROUPS]',
     '    rank_rows = sorted(rank.items(), key=lambda kv: (kv[1]["times"], kv[0]))[:TOP_GROUPS]'),
    ("行尾换行被当成一行空行（计数虚高）",
     '    if lines and lines[-1] == "" and raw.endswith("\\n"):\n        lines.pop()',
     '    if False:\n        lines.pop()'),
]
