"""`agent_runtime/bat_guard.py`（批处理引号静态门）的变异电池。

这道门的特殊性：**它几乎全是判据表**，没有浏览器、没有子进程、没有网络，所以纯层与门禁
两层就能把每条决定都钉住——也正因为它快，它没有理由不钉全。

`RealRepoLauncherTests` 刻意不进任何层（见 `LAYER_EXEMPT`）：那两条测的是**本仓真启动器
现在干不干净**（工作树里那两个 `--root "%~dp0"` 就是这条门第一天抓到的），而电池要问的是
「我的规则有没有牙」。把别人的文件状态当成对照组的一部分，环境一脏整块电池就作废。

`arguments_of` 那组结构断言是这支电池的主要弹药：光看「报没报错」看不出实参认错了没有，
而认错误把 `cd /d "%~dp0"` 这种正确写法也判红，门就等着被人关掉。
"""

BATTERY_NAME = 'bat_guard'
MODULE = 'agent_runtime.bat_guard'
TARGET = 'agent_runtime/bat_guard.py'
CASE_ID = 'bat-guard-refuses-an-escaped-closing-quote'
TEST_MODULE = 'tests.test_bat_guard'
BIND = 'bg'
UNIT_CLASSES = (
    'QuotedArgumentTests',
    'UnquotedArgumentTests',
    'ArgumentResolutionTests',
    'DirectoryVariableTests',
    'LogicalLineTests',
    'CorpusFileTests',
    'RenderTests',
    'ModuleHonestyTests',)
#: 这块电池没有现场层，也没有任何一条变异需要它：整道门是纯文本判据，没有浏览器、没有子进程。
LIVE = ()

#: 故意不作为电池的层：见文件开头。
LAYER_EXEMPT = {'RealRepoLauncherTests': '扫的是本仓真启动器（别人的文件状态），'
                                         '不是这条守卫的判据；变异体在它那里红也不能算抓住'}

#: 本块电池的变异条数下限：删一条变异必须显式改这里，「守卫悄悄变少」不该是绿灯
MINIMUM_MUTATIONS = 35

MUTATIONS = [
    ("整条门变成装饰：什么都不报",
     '    dir_vars = _directory_vars(text)\n    findings: list[dict[str, Any]] = []',
     '    dir_vars = _directory_vars(text)\n    return []\n'
     '    findings: list[dict[str, Any]] = []'),
    ("尾反斜杠只看有没有、不看奇偶",
     r'    if (len(content) - len(content.rstrip("\\"))) % 2 == 1:',
     r'    if content.endswith("\\"):'),
    ("字面尾反斜杠这一类干脆不看",
     r'    if (len(content) - len(content.rstrip("\\"))) % 2 == 1:',
     '    if False:'),
    ("目录展开不要求停在实参末尾（`\"%~dp0a.py\"` 也被判红）",
     r'_DIR_EXPANSION_END_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)$")',
     r'_DIR_EXPANSION_END_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)")'),
    ("目录展开把 `%1` 也认进去（引号包住参数占位是合法写法）",
     r'_DIR_EXPANSION_END_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)$")',
     r'_DIR_EXPANSION_END_RE = re.compile(r"%[a-z]{0,4}(?:\$\w+:\d|\d)$")'),
    ("`%NAME%` 结尾不再对照一跳表（任何变量都算目录）",
     '    if tail and tail.group(1).lower() in set(dir_vars):',
     '    if tail:'),
    ("一跳追踪整个关掉（误修成变量就查不到）",
     '        if _DIR_EXPANSION_END_RE.search(value):\n            known.add(name)',
     '        if False:\n            known.add(name)'),
    ("每个 set 出来的变量都被当成目录（正确写法也被判红）",
     '        name, value = match.group(1).lower(), match.group(2).strip().strip(\'"\')',
     '        name, value = match.group(1).lower(), "%~dp0"'),
    ("cmd 内置词表失效（`cd /d \"%~dp0\"` 被判红）",
     '    if word in CMD_BUILTINS or word in ("(", ")"):',
     '    if word in ("(", ")"):'),
    ("call 调批处理也被当成外部程序（参数其实交给 cmd）",
     '        if _bare(rest[0]).lower().endswith(BATCH_CALL_SUFFIXES):\n            return []',
     '        if False:\n            return []'),
    ("call 整个豁免（`call python x.py --root \"%~dp0\"` 溜过去）",
     '    if word == "call":\n        rest = tokens[1:]',
     '    if word == "call":\n        return []\n        rest = tokens[1:]'),
    ("if 这一行整个放过（内联命令不再查）",
     '    if word == "if":\n        return arguments_of(" ".join(_after_if(tokens[1:])))',
     '    if word == "if":\n        return []'),
    ("if 的条件部分也被当成实参",
     '    if head in _IF_KEYWORDS:\n        return rest[2:]',
     '    if head in _IF_KEYWORDS:\n        return rest'),
    ("嵌在一个 token 里的 `==` 不认（`if \"%X%\"==\"0\" py ...` 漏检）",
     '    for index in range(len(rest)):',
     '    for index in range(1, len(rest)):'),
    ("for 的 in (...) 部分也被当成实参",
     '    if word == "for":\n        return arguments_of(" ".join(_after_for(tokens[1:])))',
     '    if word == "for":\n        return arguments_of(" ".join(tokens[1:]))'),
    ("一行只认第一条命令（`& python x \"%~dp0\"` 溜过去）",
     '    return [row.strip() for row in _SEGMENT_SPLIT_RE.split(line) if row.strip()]',
     '    return [line.strip()]'),
    ("`2>&1` 里的 `&` 也被当命令分隔符",
     r'_SEGMENT_SPLIT_RE = re.compile(r"(?<![<>])(?:&&|[&|])")',
     r'_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|[&|])")'),
    ("重定向被当成实参",
     '    return [token for token in args if not _REDIRECT_RE.match(token)]',
     '    return list(args)'),
    ("命令词自己也进实参清单",
     '    args = tokens[1:]',
     '    args = tokens'),
    ("一行开头的括号块不脱括号（`(py c d)` 判成 `d)`）",
     '    if body.startswith("(") and body.endswith(")"):',
     '    if False:'),
    ("R2 整个关掉（不加引号反而安全了）",
     '                if not rule and \'"\' not in token and (',
     '                if False and not rule and \'"\' not in token and ('),
    ("R2 对加了引号的实参也报（等于建议「别加引号」）",
     '                if not rule and \'"\' not in token and (',
     '                if not rule and ('),
    ("R2 把 `%1` 这类参数占位也报出来",
     r'_DIR_EXPANSION_ANY_RE = re.compile(r"%~[a-z]{0,4}(?:\$\w+:\d|\d)")',
     r'_DIR_EXPANSION_ANY_RE = re.compile(r"%[a-z]{0,4}(?:\$\w+:\d|\d)")'),
    ("注释与标签行不再跳过",
     '        if stripped.startswith(("rem ", "rem\\t", "::", ":", "@rem")) '
     'or stripped == "rem":',
     '        if False:'),
    ("`^` 续行不拼接（参数断在续行处就查不到）",
     '        if line.endswith("^"):\n            pending += line[:-1].rstrip() + " "\n'
     '            continue',
     '        if False:\n            pending += line[:-1].rstrip() + " "\n            continue'),
    ("分词时不管引号（`\"a b\"` 被切成两个实参）",
     '        elif not quoted and ch in " \\t":',
     '        elif ch in " \\t":'),
    ("按 utf-8 解码启动器（cmd 用的是 cp936）",
     '            return body.decode(codec), clipped',
     '            return body.decode("utf-8", errors="replace"), clipped'),
    ("超过体积上界却不吭声（看起来像「读完了且干净」）",
     '        if clipped:',
     '        if False:'),
    ("体积上界形同虚设：不截断也不标记",
     '    clipped = len(raw) > MAX_SOURCE_BYTES',
     '    clipped = False'),
    ("一个文件都没扫也算通过",
     '    return {"ok": bool(paths) and not findings, "error": "",',
     '    return {"ok": not findings, "error": "",'),
    ("扫语料时不剪枝（.venv 里成千上万个 .bat 一起进来）",
     '        dirnames[:] = sorted(name for name in dirnames if name not in PRUNED_DIRS)',
     '        dirnames[:] = sorted(dirnames)'),
    ("结论里丢掉文件与行号（红了也不知道改哪）",
     '                    findings.append({"path": source, "line": lineno, "rule": rule,',
     '                    findings.append({"path": "", "line": 0, "rule": rule,'),
    ("「没扫」的文案写成「通过」",
     '        return "批处理引号门：一个 .bat/.cmd 都没读到——这不是「干净」，是没扫。"',
     '        return "批处理引号门通过：没有发现问题。"'),
    ("改法提示被抹成空话",
     '        lines.append("    怎么改：%s" % row["fix"])',
     '        lines.append("    怎么改：（略）")'),
    ("`_bare` 不脱引号（后缀/命令词判断全错位）",
     '    return text[1:-1] if len(text) >= 2 and text[0] == \'"\' and text[-1] == \'"\' '
     'else text',
     '    return text'),
]
