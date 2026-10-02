"""`page_action` 读取与页面内搜索（`mode: contains`）的变异电池（三层对照组）。

立命之处有三条，每条都对应一类曾经真的糊过去的错：
① `truncated` 是个**结构上永远为假**的判据（JS 先 `slice(0,4000)`，Python 再判
`len(text) > 4000`）——现在由页面上报 `full_length`，判据才活得下来；
② 可见性：`innerText` 对不在渲染树里的元素会退回 `textContent`（真浏览器实测），
所以「只搜可见文字」必须靠显式判定并把不可见命中单独报；
③ 上限单一下发：`limit` / `samples` 由 Python 给，JS 不再自带一套默认值。
"""

BATTERY_NAME = 'page_action'
MODULE = 'agent_runtime.page_action'
TARGET = 'agent_runtime/page_action.py'
CASE_ID = 'page-action-refuses-and-never-claims-success'
TEST_MODULE = 'tests.test_page_action'
BIND = 'pa'
UNIT_CLASSES = (
        'SpecTests',
        'EvalInjectionTests',
        'VerdictTests',
        'ActionDispatchTests',
        'SessionRegistryTests',
        'RenderTests',
        'ToolSurfaceTests',)
LIVE = (
        'BrowserActionTests',
        (
                'test_contains_searches_visible_text_only',
                'test_a_long_page_is_searchable_even_though_the_read_is_capped',
        ),)

MINIMUM_MUTATIONS = 18      # 删一条变异要显式改这里

MUTATIONS = [
    # ---- 截断：那个曾经永远为假的判据 ----
    ("截断标记退回永远为假的判据（len 已被 JS 砍平）",
     '    truncated = (int(full) > len(text)) if isinstance(full, (int, float)) \\\n'
     '        else len(text) > MAX_READ_CHARS',
     '    truncated = len(text) > MAX_READ_CHARS'),
    ("全文长度不再回传给调用方（读了一半看起来像读完）",
     '            "full_length": int(full) if isinstance(full, (int, float)) else len(text),\n',
     ''),
    # ---- contains 的判定与围栏 ----
    ("found 改由容器是否存在决定（没命中也算找到）",
     '            "found": hits > 0, "hits": hits, "elements": elements,',
     '            "found": elements > 0, "hits": hits, "elements": elements,'),
    ("命中数被压成「有没有」",
     '            "found": hits > 0, "hits": hits, "elements": elements,',
     '            "found": hits > 0, "hits": min(hits, 1), "elements": elements,'),
    ("空 needle 不再拒绝",
     '    if not needle.strip():\n'
     '        raise PageActionError("mode: contains 需要 needle: <要找的词>。")',
     '    if False:\n'
     '        raise PageActionError("mode: contains 需要 needle: <要找的词>。")'),
    ("needle 长度上限失效",
     '    if len(needle) > MAX_NEEDLE_CHARS:',
     '    if False:'),
    ("搜索容器不过选择器围栏",
     '    _target_spec(css, "", None, "")            # 容器仍走同一道选择器围栏',
     '    pass'),
    ("扫描上限不由 Python 下发（两处各一套数字）",
     '                    {"selector": css, "needle": needle, "limit": MAX_SCAN_CHARS,',
     '                    {"selector": css, "needle": needle,'),
    ("样本上限不下发给页面（JS 拿到 undefined，一段都不回）",
     '                     "samples": MAX_SAMPLES}) or {}',
     '                    }) or {}'),
    ("没扫完不再算截断",
     '            "text": "", "truncated": not scanned_all, "error": str(out.get("error") or "")}',
     '            "text": "", "truncated": False, "error": str(out.get("error") or "")}'),
    ("不可见命中不进结果（静默丢掉，「页面上没有」变成假话）",
     '            "hidden_hits": int(out.get("hidden_hits") or 0),\n'
     '            "hidden_elements": int(out.get("hidden_elements") or 0),\n',
     ''),
    # ---- 如实回报的那三行提醒 ----
    ("容器没匹配到时不再提醒（最坏的一种没找到被藏起来）",
     '        if not result.get("elements"):',
     '        if False:'),
    ("没扫完时照样不吭声",
     '        elif not result.get("scanned_all"):',
     '        elif False:'),
    ("渲染里去掉「另有不可见命中」那一行",
     '        elif int(result.get("hidden_hits") or 0):',
     '        elif False:'),
    # ---- 页面脚本：只有真浏览器才走到 ----
    ("可见性判定被去掉（DOM 里有就算页面上有）",
     "    const hidden = el.getClientRects().length === 0;",
     "    const hidden = false;", ("live",)),
    ("不可见命中不再单独计，混进 hits 与样本",
     "    if (hidden) { out.hidden_hits += seen.n; out.hidden_elements += 1; }\n"
     "    else {",
     "    if (false) { }\n    else {", ("live",)),
    ("页面侧上报的是截断后的长度（等于永远没说被砍过）",
     "  out.full_length = String(out.text).length;",
     "  out.full_length = String(out.text).slice(0, 4000).length;", ("live",)),
    ("读取不再截断（上限只是装饰）",
     "  out.text = String(out.text).slice(0, 4000);",
     "  out.text = String(out.text);", ("live",)),
]
