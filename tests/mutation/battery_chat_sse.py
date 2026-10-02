"""`agent_runtime/chat_sse.py`（`/api/ask`→`/api/chat` 真问答链路判据）的变异电池。

这块判据的存在理由是：`agent.py` 里有**六条不调模型也能产出一个 `final`** 的早退路径
（容量兜底 / 歧义追问 / 强制收尾 / 格式失守 / 截断 / 任何异常），所以"收到了 final"
不等于"跑通了一轮问答"。少了这块判据，`/api/chat` 的既有 6 条用例又全都打在
`Agent.run` 那一层——ReAct 主循环与工具执行经由这个端点一条都没真跑过。

**为什么这块电池几乎全在纯层**：`tests/test_chat_sse.py` 的 `VerdictTableTests` /
`ParsingTests` 把每一条判据的"假象形状"都喂了一遍，所以纯层就能把绝大多数变异抓住——
这意味着它在 **ubuntu 的 CI 上也跑得到**（Wave23 刚为 `windows_sandbox` 踩过
"本机全绿而 CI 红"那条，见 docs §9）。

**现场层只留三条**：它们要真事件流才现形（判据读的是真观察、真分块数、真工具名）。
"""

BATTERY_NAME = 'chat_sse'
MODULE = 'agent_runtime.chat_sse'
TARGET = 'agent_runtime/chat_sse.py'
CASE_ID = 'chat-sse-refuses-a-final-that-never-ran-a-tool'
TEST_MODULE = 'tests.test_chat_sse'
#: **必须**是测试模块里那个模块级名字。写成别的（第一版写了 "cs"）时，引擎
#: `setattr(测试模块, "cs", 变异副本)` 只是新建一个没人用的属性，纯层从头到尾看的是**真模块**
#: ——11 条变异于是全部"活下来"，看起来像守卫没牙，其实是电池接错了线（§9：活下来先怀疑锚点，
#: 再怀疑接线）。
BIND = 'chat_sse'
UNIT_CLASSES = (
    'VerdictTableTests',
    'ParsingTests',)
LIVE = ()

#: 现场档**不作为电池的层**，理由与 `bat_guard` 那条同类：它验的是"真链路满足判据"，
#: 不是"这条判据会不会红"——后者由纯层那些假象形状回答。而它并没有因此少跑：
#: `tests/test_chat_sse.py::RealTurnTests` 在常规 pytest 运行里就会真打一次 `/api/chat`
#: （CI 也跑，`unittest discover` 收得到），所以"真链路"这一半仍然有自动覆盖。
LAYER_EXEMPT = {'RealTurnTests': '真打 /api/chat 的现场档：验的是"真链路满足判据"，'
                                 '不是"判据会不会红"；它本身在常规 pytest 运行里跑到'}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 18

_ACTION_NAMES = ('    for key in ("tool", "name", "action"):',
                 '    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\\s*\\(", text)')

MUTATIONS = [
    # ---- 工具名解析：第一版读 payload["tool"]，于是永远 None，联网判据恒真 ----
    # 注意这三条都必须是**真能区分**的：只删掉结构化字段那个循环而留着正则，是等价变异
    #（正则本来就能从真实载荷的 `text` 里解析出来），第一版就写了这种自欺的变异。
    ("工具名一个都不解析：联网判据与命名判据双双恒真",
     '    for key in ("tool", "name", "action"):',
     '    for key in ():\n        return ""'),
    ("工具名正则写坏，退化成取首词",
     _ACTION_NAMES[1],
     '    match = re.match(r"^(\\S+)", text)'),
    ("工具名要求括号前有空格：真实载荷 `name(args)` 没有空格，于是读不出来",
     _ACTION_NAMES[1],
     '    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\\s+\\(", text)'),

    # ---- final 的四条早退：删掉任一条，那条早退就被当成正常回答 ----
    ("容量兜底那条 final（offline）被当成正常回答",
     '"final_is_not_an_offline_capacity_reply": (not final) or not final.get("offline"),',
     '"final_is_not_an_offline_capacity_reply": True,'),
    ("强制收尾拼的 final（degraded）被当成正常回答",
     '"final_is_not_degraded": (not final) or not final.get("degraded"),',
     '"final_is_not_degraded": True,'),
    ("歧义追问那条 final 被当成回答",
     '"final_is_not_a_clarification_request": (not final) \\\n            or not final.get("clarification_required"),',
     '"final_is_not_a_clarification_request": True,'),
    ("报错的 final（status=error）被当成回答——HTTP 200 + 有 final 就够绿",
     '"final_is_not_an_error": (not final) or (str(final.get("status") or "") != "error"\n                                                 and not final.get("error_kind")),',
     '"final_is_not_an_error": True,'),

    # ---- 凭据：final 必须引述工具观察得到的东西 ----
    ("引述判据恒真：final 凭空写一句也过",
     '    body = str(final_text or "")',
     '    return True\n    body = str(final_text or "")'),
    ("窗口长度缩到 1：任意一个字符撞上就算引述",
     'QUOTE_WINDOW = 12',
     'QUOTE_WINDOW = 1'),
    ("只扫观察的第一行，后面几行的证据一律不算",
     '        for line in _observation_text({"text": text}).splitlines():',
     '        for line in _observation_text({"text": text}).splitlines()[:1]:'),

    # ---- 零网络：不联网与"检索没命中"两条 ----
    ("联网工具清单被清空：调了 web_search 也算离线",
     'NETWORK_TOOLS = ("web_search", "http_fetch", "browse")',
     'NETWORK_TOOLS = ()'),
    ("「未找到」不再算失守：检索没命中、下一轮改调联网也看不出来",
     '"no_miss_marker_in_observations": not [text for text in obs_texts\n                                               if MISS_MARKER in text],',
     '"no_miss_marker_in_observations": True,'),

    # ---- ReAct 往返：N 个 action 要对应 N 条 observation ----
    ("observation 只需要一条：action 有两次也照样算配平",
     '"every_action_has_an_observation": len(observations) >= len(actions),',
     '"every_action_has_an_observation": len(observations) >= 1,'),
    ("action 数量不再要求：一个都没有也算跑过工具",
     '"at_least_one_tool_action": len(actions) >= 1,',
     '"at_least_one_tool_action": True,'),

    # ---- 协议形状：流式与收尾 ----
    ("一个 data 块也算流式（缓冲后一次性吐与真流式长得一样）",
     '"stream_had_more_than_one_chunk": (chunk_count is None or chunk_count >= 2),',
     '"stream_had_more_than_one_chunk": (chunk_count is None or chunk_count >= 1),'),
    ("没有 done 也算收尾完整",
     '"done_terminates_the_stream": bool(types) and types[-1] == "done",',
     '"done_terminates_the_stream": bool(types),'),
    ("token 事件不再要求",
     '"tokens_were_streamed": len(tokens) >= 1,',
     '"tokens_were_streamed": True,'),

    # ---- 解析：心跳与半截 JSON ----
    # 第一版这条写的是 `if not line.startswith("data:")` → `if False:`，那是**等价变异**：
    # 真实 SSE 里非 data 行去掉前缀后都不是合法 JSON（`event: {...}` 会剩下 `: {...}`），
    # 于是"只认 data:"这道门在协议形状下**根本区分不出来**。换成真正能区分的写法：
    # 连切片一起去掉，于是任何裸 JSON 行都会被当成事件——而裸 JSON 行是现实存在的
    # （代理重写、手写夹具、别的 SSE 实现）。
    ("不认 `data:` 前缀也不切片：裸 JSON 行被当成事件",
     '        if not line.startswith("data:"):\n'
     '            continue                     # `:` 心跳、空行、以及任何非 data 行\n'
     '        chunk = line[len("data:"):].strip()',
     '        chunk = line.strip()'),
    ("半截 JSON 也算一个事件：流被截断时反而多出内容",
     '        except ValueError:\n            continue                     # 半截 JSON 不算事件，也不该把整轮带崩',
     '        except ValueError:\n            events.append({"type": "truncated"})'),
    ("分块计数把心跳与空行也算进去：流式判据被注水",
     '    return sum(1 for line in text.splitlines() if line.strip().startswith("data:"))',
     '    return sum(1 for line in text.splitlines() if line.strip())'),
]
