"""Structured final-output audit for workflow and Subagent results."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|password|passwd|secret|private[_-]?key)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
_SENSITIVE_TERMS = re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)\b")
#: 判定"这是不是一个代码定位问题"。
#:
#: 英文项**必须加词边界** `\b`：裸 `line`/`where` 会命中 `inline`、`pipeline`、
#: `outline`、`deadline`、`online`、`elsewhere`、`whereas` 这类含子串的常见词，
#: 于是把与"定位代码"毫无关系的问题（工具观察、EDA 门禁、纯计算、知识库问答…）
#: 误判成定位问题，套上"必须给 `文件:行号`"的证据门——正文本就无引用时三项齐翻
#: False，把一条如实的 fail-closed 结论倒盖成 `（核验：FAIL）`，还 nudge 模型去补
#: 不存在的行号（可能诱导编造）。加 `\b` 后只认独立的 line/where/which file。
#: 中文项**不加**：CJK 无词边界，且 `在哪/哪个文件/逻辑/行号/定位/实现/路径` 都是
#: 多字聚合词、误伤面小，现有测试也都靠中文触发。
_LOCATION_QUERY = re.compile(
    r"(?i)(?:在哪|哪个文件|文件位置|路径|行号|定位|逻辑|实现|定义在哪"
    r"|\bwhere\b|\bwhich file\b|\bline\b)"
)
#: 从结论正文里切「文件引用」。
#:
#: 前置断言用**显式的 ASCII 路径字符集**，而不是 `\w`——
#: Python 3 的 `\w` 在 str 模式下**包含 CJK**，于是「数值在behaviors/enemy.gd:7」
#: 这种紧贴中文的写法会因为前面的「在」被判成词字符而拒绝匹配，引擎一路挪到
#: `enemy.gd` 前（前面是 `/`，不算 `\w`）才咬住，于是把**正确**的引用
#: `behaviors/enemy.gd` 切成了 `enemy.gd` -> 报「路径打不开」。
#:
#: 实测（godot_sample 定位扣血逻辑）第一轮就是这样被判 references_exist 失败的：
#: 模型写对了，审计切错了，于是白白多花一轮工具调用去"修路径"。
#: 中文是本项目的主要语言，这个断言几乎必然踩中。
#:
#: 改成 ASCII 路径字符集后仍能挡住「路径后缀」误匹配：
#: `behaviors/enemy.gd` 里的 `enemy.gd` 前面是 `/`，在集合内 -> 被拒。
#:
#: ## 扩展名表为什么按「长的在前」排
#:
#: 实测（`wf-2a548e060abf` 的 `design_retry`）：模型交出 3769 字符的设计说明，
#: 引用了 18 处**完全正确**的路径，却整份被 `review_output` 拒掉——
#: `evidence_verified` 判红的真正原因是
#:
#:     "path": "behaviors/player.ts",  "exists": false
#:
#: **`.tscn` 被咬成 `.ts`**。原表里有 `ts` 却没有 `tscn`（Godot 场景文件，
#: 本项目最核心的一类），正则匹配到 `ts` 就结束，剩下的 `cn` 被丢掉。
#: 于是审计拿着一条**模型没写过的路径**说「路径打不开」。
#:
#: 同一张表里 `ts|tsx` 的顺序还有同一个毛病：`.tsx` 也会被咬成 `.ts`。
#: 所以两处一起修：表按长度降序排，且尾部加 `(?![A-Za-z0-9_])` 兜底——
#: 就算将来又有人往表里插一个前缀相同的扩展名，也不会再截断。
#:
#: 这已经是同一族里的第三次了（上一轮：`behaviors/enemy.gd` 被切成 `enemy.gd`）。
#: **教训**：凡是「正则从正文里切路径」的地方，扩展名表就是**判据的一部分**，
#: 补一条扩展名不是加个词，是让一类引用从「误判不存在」变成「真的去核验」。
_FILE_REF = re.compile(
    r"(?<![A-Za-z0-9_./\\-])([A-Za-z0-9_./\\-]+\.(?:tscn|tsx|import|gdshader|gdignore"
    r"|tres|godot|json|jsonc|yaml|yml|toml|html|css|scss|vue|jsx|js|ts|py|pyi|md"
    r"|txt|cfg|xml|sh|bat|ps1|sql|csv|cpp|cc|cxx|hpp|h|java|kt|go|rs|swift|cs|gd"
    r"|svg|png|jpe?g|gif|webp|ico|mp3|wav|ogg|ttf|woff2?))"
    r"(?![A-Za-z0-9_])"
    r"(?:\s*:\s*L?(\d+)(?:\s*[-~]\s*L?(\d+))?)?"
)

#: 否定语境：这条路径**不是引用**，而是「我试错了 / 它不存在」的说明。
#:
#: ⚠⚠ **为什么必须有这个（实测 `wf` 任务连续 3 轮 research 全部失败的直接原因）**
#:
#: `_FILE_REF.finditer(text)` 扫的是**结论文本**，它分不清两种句子：
#:
#:     实现见 addons/godot_ai/utils/port_resolver.gd:303   <- 引用，该核验
#:     路径更正：addons/godot_ai/port_resolver.gd 不存在   <- **不是引用**
#:
#: 审计把第二句里的路径也当成引用，去代码根找，找不到 ->
#: 「路径打不开」-> 核验 FAIL。
#:
#: **方向完全反了：模型越如实报告自己的试错，越过不了门禁。**
#: 实测那一轮 research 的结论已经把路径全部改对（44 处引用可核验），
#: 却因为正文里如实点名了两个错路径而被拒；重规划两次仍然是同一个死法，
#: 上游 research failed 连累 implement/verify 全部 blocked，最终 0/5。
#:
#: 判据覆盖两个方向（真实文本里两种都出现过）：
#:     前置否定：`不存在 xxx.gd` / `xxx.gd 不存在` / `xxx.gd 是错的`
_NEGATED = re.compile(
    r"(?:不存在|没有|读不到|读不出|打不开|找不到|是错的|写错了|已更正|"
    r"真实路径|实际路径|应该是|更正为|不是真实路径|路径陷阱|路径陷阱是)"
)

#: 子句分隔符。**判否定必须按子句切，不能按固定字符窗口。**
#:
#: 实测踩过：用 24 字窗口时，下面这句会把**正面引用**一起误伤
#: （否定词与下一条路径只隔 14 字符，正在窗口内）：
#:
#:     不存在 `a/does_not_exist.gd` 这个文件；真实实现见 `b/port_resolver.gd`。
#:                                              ^^^^^^^^ 被当成否定
#:
#: 按「；」切成两个子句后，前者在子句 0（否定的），后者在子句 1（干净的）。
_CLAUSE_SPLIT = re.compile(r"[。；！？\n]")

#: 可证伪的**证据来源断言**：模型白纸黑字说这段正文是它一手读来的、不是猜的。
#:
#: 「来源可溯」核验（候选修法③）**只在结论主动作出这类断言时才触发**——
#: 因为普通答案即便引用行号也不声称"我逐字读过"，那是如实表达、不该被门禁误伤
#: （本文件反复强调：越诚实越 FAIL 是设计事故）。而一旦模型写下"来自真实读取/
#: 不是推断"，这句话就成了一条**可证伪的事实主张**：若那段正文本轮根本没送达，
#: 它就是在捏造来源。实测根因①那轮——模型凭空写出第 757-768 行函数体，还专门加
#: 一句"上面的行号与函数体来自那次真实读取，不是推断"，恰好骗过了只验"行是否存在"
#: 的一致性度量。这条正则就是为了把这类话揪出来送去核验。
_PROVENANCE_CLAIM = re.compile(
    r"(?:来自真实读取|来自那次真实读取|真实读取|实际读取|逐行读|逐字读"
    r"|并非推断|不是推断|不是推测|不是猜测|非推测|非推断"
    r"|工具确认|确已读|本轮已读|已在本次对话中读|原文核验|一手读取)")


def _ranges_cover(ranges, start, end):
    """``[start, end]`` 是否被 ``ranges`` 里某条送达区间完全覆盖。

    与 tools 的 `_range_covered` 同义，但这里**刻意复制一份**：`output_audit`
    不得反向 import tools（tools 才是调用方，见 tools.py 里那条说明），
    否则成环。逻辑只有两行，复制成本远低于引入一个跨模块依赖。
    """
    return any(lo <= start and end <= hi for lo, hi in (ranges or []))


#: 声称**总数**的一行：`总数: 194` / `合计 194 个` / `准确总数 194` / `共计：194`。
_COUNT_TOTAL_LINE = re.compile(
    r"(?:准确|精确|确切|实际|最终)?\s*"
    r"(?:总数|总量|总个数|总计|合计|共计|一共|总共|全部|共)\s*"
    r"(?:为|是|[:：=])?\s*(\d+)")

#: **整行**只是「名称: 数字」的分项（行内没有其它句读，数字收尾）——
#: 这是分项清单在纯文本里最稳的形状；散文几乎不会以 `xxx：123` 独占一行。
_COUNT_ITEM_LINE = re.compile(
    r"^\s*(?P<label>[^\n:：]{1,48}?)\s*[:：]\s*(?P<num>\d{1,7})\s*$")

#: 分项 label 若本身就是"总数"类词，它是合计行、不是分项。
_COUNT_TOTAL_LABEL = re.compile(
    r"(?:总数|总量|总个数|总计|合计|共计|一共|总共|全部|平均|均)")

#: 分项 label 若像**文件路径 / 带代码扩展名**，多半是"逐文件属性表"（每行是某文件的
#: 行数、大小等），彼此不求和、与总数不同口径——拿它去核对总数是跨口径误伤。
_COUNT_PATHISH_LABEL = re.compile(
    r"(?:[/\\~]|\.(?:gd|py|js|ts|jsx|tsx|cs|java|go|rs|c|h|cpp|hpp|lua|rb|php|swift|"
    r"kt|vue|svelte|md|markdown|json|jsonl|ya?ml|toml|tscn|tres|glsl|hlsl|sh|proto|sql)$)")


def _count_breakdown_conflict(text: str):
    """「声称总数 vs 自己列出的分项加总」是否内部矛盾（候选修法④之②·窄触发）。

    这是纯文本里**可稳定判定**的一类过度声称：模型给出一张 `名称: 数字` 的分项表，
    又写了一个总数，可两者对不上——无需任何外部真值即可证伪，属硬错误。

    为守住本文件「越诚实越 FAIL 是事故」的信条，**判不了就返回 None（不判定）**，只在
    结构足够无歧义时才下结论：
      * 必须恰好出现**一个**不同的总数值（出现两个不同总数 → 无法确定谁是总计，跳过）；
      * 必须有 **≥3** 个独立成行的 `名称: 数字` 分项（少于 3 条不构成"一张表"，跳过）；
      * 分项 label 去重（同 label 重复 → 结构含糊，跳过）；分项 label 不得是总数词本身。

    注意**覆盖边界**：若模型的分项本身就加总等于它写的总数（现场"准确总数 194"那类），
    内部并无矛盾，本检查判 `consistent=True` 并放行——因为"每个分项各自漏计、总数却
    自洽"是**外部真值**问题，审计侧拿不到全仓、不可判定，硬判 FAIL 就是误伤。那类
    只能靠工具层如实交代截断（grep/ingest 已做）或乙方案（截断证据对撞）兜。

    返回 None 表示不可判定；否则返回 dict(present=True, consistent=bool,
    total=int, sum=int, items=[(label, num), ...])。
    """
    if not text:
        return None
    lines = text.splitlines()

    totals = set()
    for ln in lines:
        m = _COUNT_TOTAL_LINE.search(ln)
        if m:
            try:
                totals.add(int(m.group(1)))
            except (TypeError, ValueError):
                continue
    # 无总数，或出现多个不同总数（分不清哪个是总计）-> 不可判定。
    if len(totals) != 1:
        return None
    total = totals.pop()

    items: list[tuple[str, int]] = []
    seen_labels = set()
    for ln in lines:
        # 先排除"总数行"本身：它匹配 item 形状（label 含总数词 + 数字），但不是分项。
        im = _COUNT_ITEM_LINE.match(ln)
        if not im:
            continue
        label = im.group("label").strip()
        if not label or _COUNT_TOTAL_LABEL.search(label):
            continue
        try:
            num = int(im.group("num"))
        except (TypeError, ValueError):
            return None  # 分项里冒出解析不了的数 -> 结构不可信，整体跳过
        key = label.lower()
        if key in seen_labels:
            return None  # 同 label 重复，分不清是分项还是修订，跳过
        seen_labels.add(key)
        items.append((label, num))

    # 防守：任一 label 像文件路径 / 带代码扩展名 -> 这是「逐文件属性表」（每行是某个
    # 文件的行数/大小等属性），彼此**不求和**、与总数不同口径。拿它去对总数就是跨口径
    # 误伤（例：`总数: 194` 配的是 `enemy.gd: 30` 这类行数清单）。整体不判定。
    if any(_COUNT_PATHISH_LABEL.search(label) for label, _n in items):
        return None

    if len(items) < 3:
        return None  # 少于 3 个分项不构成"一张可核对的表"，太弱，不判定

    s = sum(n for _l, n in items)
    return {"present": True, "consistent": s == total,
            "total": total, "sum": s, "items": items}


#: 乙方案·触发一：答案用**确定性口吻**声称一个数字。要求出现
#: 准确/精确/确切/实际/最终 之类的词，并在同一短句内（不跨句读/换行）紧跟一个数字。
#: 光有"总数: 194"而**无**确定性口吻词的，不在此列——那是②甲（分项加总核对）的活。
#: 与触发二（证据截断）**同时**成立才判红，缺一不碰，误伤面极小。
_ACCURACY_TOTAL_CLAIM = re.compile(
    r"(?:准确|精确|确切|实际|最终)(?:的)?(?:总数|总量|总个数|个数|数量|合计|共计)?"
    r"[^。\n]{0,8}?(\d{1,7})")

#: 乙方案·触发二：本轮**代码检索工具**的原始返回里自报了"会少计"的截断。
#: 只认这几处措辞（都是 grep/search_code/dev_glob 触顶时**如实追加**、且已被各自
#: 测试锁定的字样）。**不含** grep 的"已跳过 Agent 工作台 状态目录"——那是影子副本去重、
#: 不是少计，把它当截断会误伤。
_CODE_UNDERCOUNT_NOTE = re.compile(
    r"条上限并"            # grep：已达 40 条上限并**截断**
    r"|2MB 的文件未搜索"    # grep：>2MB 文件跳过（可能少计）
    r"|已取满"            # search_code：top-K 配额取满
    r"|结果可能不完整"      # dev_glob：目录条目超扫描上限
    r"|只显示前"           # dev_glob：命中过多只显示前 N
    r"|或加 scope: 收窄")   # dev_glob：命中被截断的收窄提示

#: 只有这三类"跨文件计数最常靠它、且会静默少计"的工具，其截断才算数。
#: read_file 单文件截断、web 正文截断与"全仓计数"不同口径，不计（枚举一个函数
#: 不需要读文件尾段——把它们算进来会大面积误伤）。
_CODE_COUNT_TOOLS = frozenset({"grep", "search_code", "dev_glob"})


def obs_reports_code_undercount(action: str, obs: str) -> bool:
    """本轮某次代码检索工具的**原始返回**里，是否自报了"会少计"型截断。

    只对 grep / search_code / dev_glob 生效（见 `_CODE_COUNT_TOOLS` 的口径说明）。
    **必须在工具执行时拿原始 obs 判定**：trail 里的观测正文会被 `_clip` 裁掉尾部，
    而截断提示恰恰在末尾——事后扫 trail 会漏（假阴），这里从源头打标、只认原始返回。

    审计（`audit_evidence`）拿这个信号去和答案的"确定性计数口吻"对撞，补②甲够不着的
    那类"分项各自漏计、加总却自洽"的内部一致错报。
    """
    if _tool_name(str(action or "")) not in _CODE_COUNT_TOOLS:
        return False
    return bool(_CODE_UNDERCOUNT_NOTE.search(str(obs or "")))


def _ref_is_negated(text: str, start: int, end: int) -> bool:
    """这条「引用」在**它自己那个子句**里被否定吗（是的话它不是引用）。

    只看同一子句：向前找最近的子句分隔符，向后找最近的，
    判定词必须落在这两个分隔符**之间**。
    """
    prev = 0
    for m in _CLAUSE_SPLIT.finditer(text, 0, start):
        prev = m.end()
    nxt = len(text)
    m = _CLAUSE_SPLIT.search(text, end)
    if m:
        nxt = m.start()
    return bool(_NEGATED.search(text[prev:nxt]))


def _tool_name(action: str) -> str:
    """从动作文本里取出工具名：`read_file(path: x)` -> `read_file`。

    工作流的 `_child_trace` 存的是带参数的整句（`traj` 里的
    `{"action": "read_file(path: project.godot)"}`），不是裸工具名。此前这里
    写的是 `action == "read_file"` 精确匹配，于是**工作流路径下 `read_steps`
    恒为空**，`read_file_present` 恒为 False——而 `evidence_verified` 用的正是
    含这一项的聚合。结果是每个工作流子代理的结论都被 Harness 复核拒掉，
    无论内容多好（实测 `wf-87c0df3224aa` 的 `design_probe`：4028 字符、每条都
    带 `文件:行号`，照样被拒）。
    """
    text = str(action or "").strip()
    head = text.split("(", 1)[0].strip()
    return head or text


def _strip_uri_scheme(path: str) -> str:
    """去掉 `res://` / `user://` 这类 URI 前缀，只留**相对代码根**的部分。

    实测（`wf-87c0df3224aa` 的 `design_probe`）：模型交出 4028 字符、每条都带
    `文件:行号` 的证据清单（`project.godot:6 config/name="StarVoyager"`…），
    又被整份拒掉，理由是 6 条引用「路径打不开」：

        //addons/docmind_bridge/docmind_bridge.gd
        //behaviors/enemy.tscn
        //scenes/Main.tscn
        …

    **模型写的是 Godot 原生的 `res://behaviors/player.gd`。** 正则的路径字符集
    含 `/`，于是从 `res:` 后面那两个斜杠开始咬，把 **scheme 吃进了路径**。
    结果是：模型**每写一个 Godot 原生路径，这条判据就误报一次**。

    这个项目是 Godot 优先的，`res://` 是它的母语——所以这不是边角情况，
    是主干路径。

    顺带把 Windows 的反斜杠与前导 `./` 也归一掉：同一个目录的两种写法不该
    被判成两个不同的引用。
    """
    text = str(path or "").replace("\\", "/").strip()
    # `res://x` / `user://x` / `file://x` -> `x`；只去 scheme，不动后面的内容。
    for scheme in ("res://", "user://", "file://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _trace_steps(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = (result.get("trace") or {}).get("steps")
    if raw is None:
        raw = result.get("steps") or []
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, (list, tuple)) else []


def _read_paths_from_actions(actions: Any) -> list[str]:
    """从动作文本里取出**本轮 read_file/grep/search_code 真正读过的路径**。

    真实记录里动作是带参数的整句，而形状不止一种（实测三种都在用）：

        read_file(path: behaviors/player.gd, start: 26, end: 49)
        read_file(behaviors/player.gd)
        grep(pattern: GRAVITY|detect path: behaviors/enemy)

    第三种里 `path` 前面是空格、值后面跟着收尾括号；第一种里键名紧跟左括号；
    第二种**根本没有键名**（原生 FC 通道的独立 `path` 参数拼回来的形状）。
    所以两条正则都要：先找带键名的，找不到再退回「第一个像路径的实参」。

    切不干净不会造成误命中：多出来的字符只会让 basename 带分隔符而匹配不上，
    而**匹配不上等于退回原判定**，不会误判为通过。
    """
    found: list[str] = []
    keyed = re.compile(r"(?:path|file|filename)\s*[:：]\s*([^\s,;()]+)", re.I)
    bare = re.compile(r"^\s*([^\s,;()]+(?:\.[A-Za-z0-9_]+)+)\s*$")
    for action in (actions or []):
        text = str(action or "")
        if not re.match(r"\s*(?:read_file|grep|search_code)\s*\(", text, re.I):
            continue
        for line in text.splitlines():
            hit = keyed.search(line)
            if hit:
                found.append(_strip_uri_scheme(hit.group(1)))
                continue
            # 无键名：取括号内的第一个实参。`read_file(behaviors/player.gd)` 与
            # `grep(pattern: X)path: y` 都靠这条（后者的 path: 已被 keyed 命中）。
            inside = re.search(r"\(([^)]*)\)", line)
            if not inside:
                continue
            for token in re.split(r"[,;]", inside.group(1)):
                token = bare.match(token.strip())
                if token:
                    found.append(_strip_uri_scheme(token.group(1)))
    return found


def _basename_index(paths: Any) -> dict[str, list[str]]:
    """本轮**实际读过**的路径，按裸文件名建索引。

    ## 为什么需要它

    实测（`wf-54651f11c4a0`）：子代理用 `read_file(path: behaviors/enemy.gd)` 成功
    读到全文，结论里写的是裸文件名 `enemy.gd`——复核判 `exists=false`，于是
    `references_exist: false` 把**整份结论**否掉，而同一份结论里的
    `behaviors/player.gd:8`、`project.godot` 全部 `exists=true line_ok=true`。

    ## 这不是放水

    打不开的理由是「根下没有 `enemy.gd`」——但**子代理刚刚读过它**，观察里有
    全文（`traj` 里那条 obs 就是证据），而 harness 自己记着这件事却不用。
    拿「根下查不到」去否一条「我们自己看见它被读过」的引用，是判据在打自己的脸。

    ## 与既有那条规矩的关系

    本文件此前写过一条「裸文件名**照常判定**：它在根下确实不存在」——**那条注释的
    推理方向错了**。它论证的是「该检查跑得了，所以必须判」，可它漏了自己手上
    就有更好的证据来源：不是文件系统，而是**本轮真实执行记录**。

    两者不冲突：文件系统查得到就直接判；查不到时**先问「这轮读过吗」**，读过就认。
    只有两者都否才判打不开——那才是真的打不开。

    ## 为什么不能用「宽松匹配」糊过去

    只在**本轮 traj 里出现过**的路径上生效。凭空写一个 `foo.gd` 而这轮没读过，
    依然判打不开。这里索引的是执行记录，不是全库文件名。

    ## 索引的是**读过的完整路径的裸文件名部分**

    输入是 `behaviors/enemy.gd` 这类**读过的完整路径**，而查询方拿着的是
    `enemy.gd`。所以索引键取**最后一段**——之前写成「跳过带目录的路径」把索引
    建空了，等于整条兜底从未生效（实测：路径提取全对，兜底仍不触发）。
    """
    index: dict[str, list[str]] = {}
    for raw in (paths or []):
        norm = _strip_uri_scheme(str(raw or "")).strip()
        if not norm:
            continue
        base = norm.rsplit("/", 1)[-1]
        if base and base not in (".", ".."):
            index.setdefault(base.lower(), []).append(norm)
    return index


def audit_evidence(result: Mapping[str, Any] | None, *, question: str = "",
                   code_root: str = "", require_read: bool | None = None,
                   authority_tools: Sequence[str] | None = None,
                   served_ranges: Mapping[str, Sequence[Sequence[int]]] | None = None) -> dict[str, Any]:
    """Check that a location answer is backed by a readable source file.

    This is intentionally a narrow, deterministic gate.  It does not claim to
    understand the semantics of a function; it verifies the minimum evidence
    contract first: location questions need a file reference, a successful
    ``read_file`` step, and line numbers that exist in the current code root.
    Non-location answers are left untouched unless ``require_read`` is true.
    """
    result = dict(result or {})
    text = str(result.get("conclusion") or result.get("text") or result.get("merged") or "")
    location_question = bool(_LOCATION_QUERY.search(str(question or "")))
    required = location_question if require_read is None else bool(require_read)
    steps = _trace_steps(result)
    actions = [str(item.get("action") or item.get("tool") or "").strip() for item in steps]
    # 动作文本在工作流路径里是 **带参数的整句**（`_child_trace` 存的是
    # `traj` 里的 `{"action": "read_file(path: project.godot)"}`），而不是裸工具名。
    # 此前写的是 `action == "read_file"` 精确匹配，于是工作流里 `read_steps`
    # **恒为空**、`read_file_present` 恒为 False，而 `evidence_verified` 用的
    # 正是含这一项的聚合 —— 结果是**每个工作流子代理的结论都被 Harness 复核
    # 拒掉**，无论内容多好。实测 `wf-87c0df3224aa` 的 `design_probe`：
    # 4028 字符、每条都带 `文件:行号`，照样被拒。
    read_steps = [item for item, action in zip(steps, actions)
                  if _tool_name(action) == "read_file"]
    refs = []
    skipped_negated = 0
    for match in _FILE_REF.finditer(text):
        # ⚠ 否定语境里的路径**不是引用**。模型如实写「xxx.gd 不存在，真实路径是
        # yyy.gd」是在**帮你**找出问题；把它当引用会判成「路径打不开」，
        # 于是越诚实越 FAIL（实测 wf 任务连续 3 轮 research 死在这里）。
        if _ref_is_negated(text, match.start(1), match.end(1)):
            skipped_negated += 1
            continue
        # 存**归一后**的路径：判定（`exists` / `line_ok`）是拿它去做的，
        # 记录里若还留着 `res://` 的残渣，读到这条记录的人重新核验会得到
        # 不同答案——那就成了「报告与事实不一致」。
        refs.append({"path": _strip_uri_scheme(match.group(1)),
                     "start_line": int(match.group(2)) if match.group(2) else None,
                     "end_line": int(match.group(3) or match.group(2)) if match.group(2) else None})
    file_checks = []
    root = Path(code_root).resolve() if code_root else None
    # 本轮真实读过的路径（从 action 文本里取），供裸文件名回退判定用。
    read_basenames = _basename_index(_read_paths_from_actions(actions))
    for ref in refs[:50]:
        rel = ref["path"]
        path = Path(rel)
        if root is not None and not path.is_absolute():
            path = (root / path).resolve()
        exists = False
        in_root = True
        # 行号检查只在**能打开文件**时才有意义。打不开时 line_count 从未数过，
        # 此时判 line_ok=False 会让 `reference_lines_valid` 与 `references_exist`
        # 一起失败，于是纠偏文案会说「行号超出了文件实际行数」——**而我们根本没数过
        # 行数**。那条建议模型无法执行，也是不该给出的。
        # （实测：模型写裸文件名 enemy.gd 时，两条失败同时出现。）
        # 所以：打不开 -> 行号检查标记为「不适用」，只报路径问题。
        line_ok = True
        try:
            exists = path.is_file()
            if root is not None:
                in_root = path == root or root in path.parents
            if exists and ref["start_line"] is not None:
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    line_count = sum(1 for _ in handle)
                line_ok = 1 <= ref["start_line"] <= line_count and ref["end_line"] <= line_count
        except (OSError, ValueError):
            exists = False
            line_ok = True  # 同上：打不开 -> 行号问题不适用，不是「越界」
        # 裸文件名 + 文件系统查不到 -> 再问一次「这轮读过吗」。
        # 见 `_basename_index` 的说明：子代理确实读过它，观察里有全文，
        # 而 harness 自己记着这件事却不用，拿根下查不到去否它是在打自己的脸。
        # `read_verified` 单独记一份，避免把「文件系统有」与「这轮读过」混成一件事。
        read_verified = False
        if not exists and read_basenames:
            hits = read_basenames.get(rel.lower())
            if hits:
                exists = True
                read_verified = True
                if root is not None and not path.is_absolute():
                    in_root = True  # 已确认存在，无需再判越界
        file_checks.append(dict(ref, exists=exists, in_root=in_root,
                                line_ok=line_ok, read_verified=read_verified))
    # A trace can be supplied by a workflow runner or by a direct Agent.  A
    # successful flag is the only portable signal; failure observations remain
    # evidence of an attempted read, not proof of verification.
    successful_read = any(item.get("ok", True) is not False for item in read_steps)
    valid_refs = [item for item in file_checks if item["exists"] and item["in_root"] and item["line_ok"]]
    # 裸文件名（`player.tscn`，不带目录）**照常判定**：它在代码根下确实不
    # 存在，那就是一个打不开的引用。本文件曾一度把它们从判定里摘出去，理由
    # 是「我们没去找它在哪一份」——**那是把一条关于行号的规矩挪用到路径存在性
    # 上了**。既有测试已经把裸文件名钉成「打不开且必须拦住门禁」
    # （`test_a_bare_basename_is_caught_as_an_unopenable_reference`），其原则
    # 出自 Claude Code：「If you can run the check, you must decide PASS or FAIL」——
    # 这条检查跑得了，所以必须判。
    # ---- ② 工具观察豁免（authority-backed evidence 顶替 文件:行号）--------------
    # 本轮若有一次**成功返回、且自带结构化判定**的权威工具观察（`authority_tools` 里的
    # 工具，如 eda_report_gate 直接算出 met/unmet/rollback，来自真取像/报告解析），那
    # 终答的证据就是这条观察本身，而不是 repo 里的「文件:行号」。此时正文合理地不含
    # 任何引用（`refs` 为空），不该再按定位门凭空要求引用、把如实的结论倒盖成 FAIL。
    # **只在无引用时豁免**：正文一旦写了引用，照旧逐一核验——幻觉路径 / 越界行号仍 FAIL。
    # 这保住 `test_no_reference_at_all_fails_the_gate` 等锁测：它们的 trace 里没有这种
    # 权威工具观察（只有 read_file），豁免分支根本不触发。
    authority_names = {_tool_name(n) for n in (authority_tools or []) if n}
    authority_exempt = False
    if authority_names and required and not refs:
        for _item, _act in zip(steps, actions):
            if _item.get("ok") is True and _tool_name(_act) in authority_names:
                required = False
                authority_exempt = True
                break
    # ---- ③ 来源可溯（候选修法③，**窄触发**）--------------------------------
    # 只有当结论**主动**写下「来自真实读取 / 不是推断」这类可证伪的来源断言时，
    # 才去核那段正文本轮**是否真送达**。不做来源断言的如实答案一律不碰（零冲击面，
    # 也守住本文件「越诚实越 FAIL 是事故」的信条）。
    # 数据源是 `tools.read_served_ranges()`——①修好后它如实记录**送达**区间，
    # 截断读的尾段不再被谎记成已覆盖。
    # 关键分寸：**只对「有该文件送达记录、却没覆盖被引区间」的情形判红**。
    # 查不到该文件的覆盖（路径写法/symlink 归一对不齐、或本轮没读过它）一律
    # 视为**不可判定**、跳过——绝不凭"查不到"就断言失败（对齐本文件 `no_root`：
    # 没检查过就别硬判 FAIL）。这样这道门只可能因"确有反证"而红，不会误伤。
    provenance_claim = bool(_PROVENANCE_CLAIM.search(text))
    unverified_provenance: list[dict[str, Any]] = []
    provance_judged = False
    if provenance_claim and served_ranges is not None and root is not None:
        provance_judged = True
        for item in file_checks:
            if item.get("start_line") is None:
                continue  # 没有行号的引用不纳入来源核验
            try:
                _p = Path(item["path"])
                if not _p.is_absolute():
                    _p = (root / _p).resolve()
                _key = os.path.normcase(str(_p))
            except (OSError, ValueError):
                continue
            cov = served_ranges.get(_key)
            if not cov:
                continue  # 无送达记录 -> 不可判定，跳过（不制造 FAIL）
            lo = item["start_line"]
            hi = item["end_line"] if item.get("end_line") is not None else lo
            if not _ranges_cover(cov, lo, hi):
                unverified_provenance.append({
                    "path": item["path"], "start_line": lo, "end_line": hi,
                    "served": [list(r) for r in cov]})
    # ---- ④之② 确定性计数自检（候选修法甲：内部矛盾，**窄触发**）--------------
    # 纯文本里能**无外部真值**判定的一类过度声称：模型给一张 `名称: 数字` 分项表 +
    # 一个总数，两者加总对不上。抽取器判不了就返回 None（不判定），只在结构无歧义时
    # 才下结论——守住「越诚实越 FAIL 是事故」。取数异常绝不让门禁本身崩（对齐 ③）。
    try:
        count_conflict = _count_breakdown_conflict(text)
    except Exception:  # noqa: BLE001 —— 核验数据解析不得拖垮门禁
        count_conflict = None
    count_judged = bool(count_conflict and count_conflict.get("present"))
    # ---- ④之②·乙 截断证据 × 确定性计数口吻 对撞（**双条件，窄触发**）-------------
    # 补②甲够不着的那类错报：模型的分项加总恰好等于它写的总数（内部自洽），但那个总数
    # 是拿**被截断的观察**拼出来的——"准确总数 194"（真值 196）正是这种。判据两条**同时**
    # 成立才可能红：(1) 答案用确定性口吻声称一个数字；(2) 本轮产出该数字的代码检索证据
    # 自报过"会少计"型截断（step.truncated，由调用方在工具执行时拿原始 obs 打标）。
    # 只给总数、无确定性口吻 -> 交回②甲；证据没截断 -> 不碰（judged 仍可能 True 但判通过）。
    accuracy_claim = _ACCURACY_TOTAL_CLAIM.search(text)
    evidence_truncated = any(bool(item.get("truncated")) for item in steps)
    count_evidence_judged = bool(accuracy_claim)
    checks = {
        "location_question": location_question,
        "reference_present": bool(refs) if required else True,
        "read_file_present": successful_read if required else True,
        "references_exist": all(item["exists"] and item["in_root"] for item in file_checks) if refs else not required,
        "reference_lines_valid": all(item["line_ok"] for item in file_checks) if refs else not required,
    }
    # 只有真正进入来源核验（有断言 + 有覆盖数据 + 有代码根）时，才把这一项**加进**
    # checks——否则既有测试与不做断言的普通答案完全不受影响（零回归面）。
    if provance_judged:
        checks["provenance_traceable"] = not unverified_provenance
    # 同理：只有抽取器**判得出来**时才加这一项；判不了就不碰门禁（零回归面）。
    if count_judged:
        checks["count_consistent"] = bool(count_conflict["consistent"])
    # 乙：只有答案**确实用了确定性口吻**（judged）才加这一项。加了之后：证据未被截断
    # -> True（如实放行）；证据自报少计型截断 -> False（声称准确却没看全，判红）。
    # 不做确定性声称的普通答案完全不碰这一项（零回归面，守住"越诚实越 FAIL 是事故"）。
    if count_evidence_judged:
        checks["count_evidence_complete"] = not evidence_truncated
    # ---- 门禁项 vs 参考项 -------------------------------------------------
    #
    # ``read_file_present``（「这一轮有没有调 read_file」）是**过程**指标，不是**事实**
    # 指标，而且已被证明不可靠：本次会话连续四次误报——并行读取看不见、完整读取看不见、
    # 连上了却被路径切分错误连累。它衡量的是 harness 自己的记录能力，不是答案质量。
    #
    # Claude Code 的验证 prompt 把这一点写成了明文：
    #     "The code looks correct based on my reading" — reading is not verification. Run it.
    # 即「读过」根本不被当作证据。拿它当闸门不只是多余，是**方向反了**。
    #
    # 而 ``references_exist`` / ``reference_lines_valid`` 是纯文件系统判定：路径能不能
    # 打开、行号是否越界。它们回答的是「你的引用能不能被核验」，属于**可核验性**，
    # 拦的是真错误（幻觉路径、越界行号），且无观察到的误报。
    #
    # 所以：门禁只看事实项；过程项降级为参考，不再驱动闸门与用户可见警告。
    gate_checks = {k: v for k, v in checks.items() if k != "read_file_present"}
    gate_ok = bool(all(v for k, v in gate_checks.items() if k != "location_question"))
    # PARTIAL 只留给**环境限制**（无法核验），不给「我不确定」当逃逸口——
    # 这是 Claude Code 明确设计过的约束（"PARTIAL is for environmental limitations
    # only ... not for 'I'm unsure whether this is a bug'"）。
    #
    # 两种 PARTIAL 的**含义完全不同**，措辞必须分开：
    #   no_root       —— 没有代码根，**根本无法检查**，一个字都不能说；
    #   no_read_record—— 引用已逐一核实通过，只是本轮没有读取记录作为旁证。
    # 早先两者共用「环境限制，无法核验」一句话，于是第二种明明核实过了却被说成
    # "无法核验"；更糟的是第一种会把每一条引用都列成「未通过（路径打不开）」——
    # 我们压根没检查过，凭什么断言路径有问题。**在说一件它不知道的事。**
    verdict = "PASS"
    verdict_reason = ""
    if not gate_ok:
        verdict = "FAIL"
    elif required and not successful_read:
        verdict = "PARTIAL"
        verdict_reason = "no_read_record"
    # Without a project root, do not manufacture a filesystem failure; the
    # read_file trace is still useful and the caller can surface this gap.
    if required and refs and not root:
        checks["references_exist"] = successful_read
        checks["reference_lines_valid"] = successful_read
        checks["read_file_present"] = True
        # 没有代码根 = **环境限制**，不是答案有问题。按三值判定的定义这才是 PARTIAL
        # 的正当用途；此时不能声称 FAIL（那是把环境问题说成内容问题）。
        gate_checks["references_exist"] = True
        gate_checks["reference_lines_valid"] = True
        verdict = "PARTIAL" if successful_read else "FAIL"
        verdict_reason = "no_root"
    # 一条"总数 ≠ 分项加总"的内部矛盾是**内容错误**，绝不该被"无代码根/环境限制"
    # 降级成 PARTIAL——那会把明明自相矛盾的结论原样放出去。有反证就必须红。
    if count_judged and not count_conflict["consistent"]:
        verdict = "FAIL"
        verdict_reason = ""
    # 乙：确定性口吻的计数 + 本轮代码检索证据自报少计型截断 = 声称"准确"却明知道没看全，
    # 是过度声称（内容/口吻问题），不该被"无代码根/环境限制"降级成 PARTIAL 原样放出去。
    if count_evidence_judged and evidence_truncated:
        verdict = "FAIL"
        verdict_reason = ""
    # ---- 外显判定：答案**自招**的过度声称，与问题是否定位无关，必须外显 ----------
    # 这三项只会在结论**自己**写下"准确总数 / 来自真实读取 / 分项加总"时才被激活
    # （窄触发），因此它们的 FAIL 是**内容/口吻**问题——纯计数题谎报"准确总数"
    # 与定位题引用打不开一样是真错误。它们不该被 `required`（定位类）这道**消费者门**
    # 吞掉：此前消费者 `if required and not gate_ok` 让非定位题即便 verdict=FAIL 也
    # 一个 reflection 都不发。调用方据此（`content_fail`）把外显通道对这类过度声称打开。
    _OVERCLAIM_KEYS = ("count_consistent", "count_evidence_complete", "provenance_traceable")
    content_fail = any(checks.get(k) is False for k in _OVERCLAIM_KEYS)
    return {"ok": bool(all(value for key, value in checks.items()
                             if key != "location_question")),
            # 只看事实项的判定结果。调用方应当用它驱动闸门与用户可见结论，
            # ``ok`` 保留给需要"过程也达标"的旧调用方。
            "gate_ok": gate_ok,
            # 答案**自招**的过度声称（计数/来源）判红——与 `required` 无关，调用方据此
            # 对非定位题也外显核验结论。见上方 _OVERCLAIM_KEYS 注释。
            "content_fail": content_fail,
            "gate_checks": gate_checks,
            "verdict": verdict,
            "verdict_reason": verdict_reason,
            "required": required, "checks": checks,
            "authority_exempt": authority_exempt,
            "references": file_checks, "verified_references": valid_refs,
            # 否定语境里被跳过的路径数（诊断用）。**不是**「有几处引用失败」——
            # 这些恰恰是模型**正确**指出的问题（试错路径 / 不存在的路径）。
            "skipped_negated_refs": skipped_negated,
            # ③ 来源可溯：本轮结论是否作了可证伪的来源断言，以及哪些被引区间
            # **有送达记录却没覆盖**（"声称读过、实际从未送达"）。调用方据此点名。
            "provenance_claim": provenance_claim,
            "unverified_provenance": unverified_provenance,
            # ④之② 确定性计数自检：`名称: 数字` 分项表与声称总数的加总矛盾详情
            # （判不了则为 None）。调用方据此点名 total/sum，给模型可执行的纠偏。
            "count_conflict": count_conflict,
            # ④之②·乙 截断证据对撞：答案声称的确定性数字、本轮是否代码检索证据被截断、
            # 以及**具体是哪几个**工具自报了少计（调用方点名用，说到工具名才可能被纠正）。
            "accuracy_count": (int(accuracy_claim.group(1)) if accuracy_claim else None),
            "evidence_truncated": evidence_truncated,
            "truncated_evidence_tools": sorted({_tool_name(a) for item, a in zip(steps, actions)
                                                 if item.get("truncated") and _tool_name(a)}),
            "read_steps": len(read_steps)}


def redact(text: Any) -> str:
    value = str(text or "").replace("\x00", "").strip()
    for pattern in _CREDENTIAL_PATTERNS[:1]:
        value = pattern.sub(lambda match: "%s=[REDACTED]" % match.group(1), value)
    value = _CREDENTIAL_PATTERNS[1].sub("[REDACTED]", value)
    value = _CREDENTIAL_PATTERNS[2].sub("[PRIVATE_KEY_REDACTED]", value)
    return value[:8000]


def audit_final_output(result: Mapping[str, Any] | None, *, max_tool_failures: int = 3,
                       question: str = "", code_root: str = "",
                       require_evidence: bool | None = None) -> dict[str, Any]:
    result = dict(result or {})
    raw_text = str(result.get("conclusion") or result.get("text") or result.get("merged") or "")
    credential_detected = any(pattern.search(raw_text) for pattern in _CREDENTIAL_PATTERNS)
    text = redact(raw_text)
    raw_steps = (result.get("trace") or {}).get("steps")
    if raw_steps is None:
        raw_steps = result.get("steps") or []
    steps = list(raw_steps) if isinstance(raw_steps, (list, tuple)) else []
    tool_failures = [step for step in steps
                     if isinstance(step, Mapping) and step.get("ok") is False]
    file_changes = result.get("file_changes") or result.get("files") or []
    tests = result.get("tests") or result.get("test_results") or []
    if isinstance(file_changes, Mapping):
        file_changes = [file_changes]
    if isinstance(tests, Mapping):
        tests = [tests]
    failed_tests = [item for item in tests if isinstance(item, Mapping) and item.get("ok") is False]
    evidence = audit_evidence(result, question=question, code_root=code_root,
                              require_read=require_evidence)
    checks = {
        "has_result": bool(text),
        "no_replacement_chars": "\ufffd" not in text,
        "credentials_absent": not credential_detected,
        "sensitive_terms_without_value": not bool(_SENSITIVE_TERMS.search(text)) or "[REDACTED]" in text,
        "tool_failures_below_limit": len(tool_failures) < max(1, int(max_tool_failures)),
        "status_ok": result.get("status", "ok") == "ok",
        "file_evidence_bounded": len(file_changes) <= 100,
        "test_evidence_bounded": len(tests) <= 100,
        "tests_ok": not failed_tests,
        # 这里用 `ok`（含 `read_file_present`）是**刻意的**，不是漏改。
        #
        # `audit_evidence` 里另有 `gate_ok`（只看事实项）并注释说「调用方应当
        # 用它驱动闸门」，`test_verification_gate_reshape.py` 也是按那条原则
        # 写的；而 `test_output_audit.py::test_location_answer_requires_read_and_
        # valid_line` 要求「定位类问题必须真读过文件」。两个契约互相矛盾。
        #
        # 本轮实测站在**更严的一边**：`read_file_present` 之所以在工作流路径下
        # 恒为 False，是 `action == "read_file"` 精确匹配认不出
        # `read_file(path: x)` 这个真实形状——那是**bug**，修掉之后这一项
        # 本身就判得准了。既然 bug 已修，就没必要再把口径放宽：否则等于用
        # 「过程项不可靠」当理由，放过一个「模型没读过也能报出 文件:行号」的
        # 真实漏洞。
        "evidence_verified": evidence["ok"],
    }
    failures = []
    if not checks["credentials_absent"]:
        failures.append({"kind": "credential", "message": "输出疑似包含凭据，已阻断", "recovery": "删除凭据后重新生成并轮换已暴露密钥"})
    if not checks["no_replacement_chars"]:
        failures.append({"kind": "encoding", "message": "输出包含乱码替换字符", "recovery": "重新读取原文并使用 UTF-8 输出"})
    if tool_failures:
        failures.append({"kind": "tool", "message": "%s 个工具步骤失败" % len(tool_failures),
                         "recovery": "检查失败工具证据，切换替代工具或重新规划"})
    if failed_tests:
        failures.append({"kind": "test", "message": "%s 个测试失败" % len(failed_tests),
                         "recovery": "先修复失败测试，再重新执行验证任务"})
    if not evidence["ok"]:
        failures.append({"kind": "evidence", "message": "定位回答缺少可核验的文件原文或有效行号",
                         "recovery": "先用 search_code 定位，再用 read_file 读取原文；无法确认时明确说明"})
    return {"ok": bool(all(checks.values())), "checks": checks, "text": text,
            "failures": failures, "tool_failure_count": len(tool_failures),
            "failed_test_count": len(failed_tests),
            "evidence_audit": evidence,
            "evidence": {"files": list(file_changes)[:100], "tests": list(tests)[:100],
                         "tools": steps[-100:]},
            "sensitive_redacted": "[REDACTED]" in text}


__all__ = ["audit_evidence", "audit_final_output", "redact"]
