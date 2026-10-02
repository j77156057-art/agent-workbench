"""日志的**视图**层：重复折叠 + 级别筛选。纯文本进、纯文本出，不改任何捕获逻辑。

为什么需要这一层，是量出来的不是想出来的：拿真 uvicorn（`--log-level info`）打 24 次同一个
接口，回来的 33 行日志**逐行去重后还是 33 行**——每条 access 行都带着临时客户端端口
（`127.0.0.1:56617`），文本上全是"唯一行"。所以按整行折叠对真实日志等于没折，必须先归一
成【签名】：数字 / 十六进制 / uuid / 引号里的内容都换掉，再比。

三条不可退让的规矩：

1. **只折连续的同类块，且首尾原样保留**。日志噪音是"一段一段"重复的，全局分组会把时间线
   打散；而保留首尾让"从什么时候开始刷、刷到什么时候停"还能看出来。
2. **按块筛级别，不按行**。`ERROR` 后面那段 traceback 没有级别词，逐行筛级别会正好把最该看
   的正文筛掉。规则是：带级别词的行开一个块，后续不带级别的行（缩进正文）归属它。
3. **折叠不能假装看见过全文**。上游 `_CappedBuffer` 是"头 4000 + 尾 4000，丢中段"，
   所以视图必须把"被丢的中段"如实报出去（`middle_omitted_chars`），并且在它存在时禁止
   说"日志里没有错误"——那是本仓反复付过学费的假干净。
"""
from __future__ import annotations

import re
from typing import Any

#: 一次最多折多少行：折叠是给人看趋势的，不是用来把 50k 行压成 5 行的黑箱
MAX_LOG_LINES = 20_000
MAX_FOLD_RUN = 3                      # 连续多少行同签名才值得折
TOP_GROUPS = 6                        # "重复签名排行"列几组
#: 被级别筛掉的行里，仍带这些字样的要单独计数：uvicorn 把 404/500 记成 INFO，
#: `level: warn` 会合法地把它们筛掉——视图必须能说出"筛掉的里面有 N 行看着像失败"，
#: 否则就是拿一个视图冒充"没有错误"。
FAILURE_MARK = re.compile(r"\b[45]\d\d \w|\b(404|500|502|503)\b|Not Found|Internal Server Error",
                         re.I)

LEVELS = ("debug", "info", "warn", "error")
#: 级别词的识别表。刻意保守：认不准就归进"当前块的正文"，绝不猜成 info。
LEVEL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("error", re.compile(r"\b(ERROR|FATAL|CRITICAL|Traceback \(most recent call last\))\b|"
                         r"\berror\b|失败|报错")),
    ("warn", re.compile(r"\b(WARNING|WARN)\b|警告")),
    ("info", re.compile(r"\b(INFO|NOTICE)\b")),
    ("debug", re.compile(r"\b(DEBUG|VERBOSE)\b")),
)

#: 归一化：这些片段在"同一件事重复刷屏"里是唯一的差别，留着就折不动。
#: 刻意【不】归一引号里的内容与 IP：access 行的引号里是请求路径，把 `"/api/settings"` 与
#: `"/api/models"` 折成同一个签名就把不同的事件合并了——那是比"折不动"更糟的错。
#: IP 也不需要单独规则：`\d+` 已经把它折成 `#.#.#.#`。空白同理——真实日志的同类行不会因为
#: 对齐空格不同而需要归一，写一条没人验证的规则等于写一条猜测。
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
     "#uuid"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "#hex"),
    (re.compile(r"\d+"), "#"),
)

OMITTED_MARK = re.compile(r"\[省略 (\d+) 字符\]")


def signature(line: str) -> str:
    """一行的"重复身份"：只保留结构，丢掉每次都不同的数字与请求 id。"""
    text = str(line or "").strip()
    for pattern, word in _NORMALIZERS:
        text = pattern.sub(word, text)
    return text[:200]


def level_of(line: str) -> str:
    """返回这一行【开】的级别；认不出就回 ""（它属于当前块的正文）。"""
    for name, pattern in LEVEL_PATTERNS:
        if pattern.search(str(line or "")):
            return name
    return ""


def _levels(lines: list[str]) -> list[tuple[str, str, bool]]:
    """每行标级别。认不出级别词的行**继承**上一条认得出的级别。

    这条不是便利，是正确性要求：`Traceback` 下面的正文行没有任何级别词，
    按"逐行筛级别"会把最该看的正文筛掉，留下一个孤零零的 ERROR 标题。
    """
    out: list[tuple[str, str, bool]] = []
    current = ""
    for text in lines:
        found = level_of(text)
        if found:
            current = found
            out.append((text, found, False))
        else:
            out.append((text, current, True))
    return out


def _runs(rows: list[tuple[str, str, bool]]) -> list[dict[str, Any]]:
    """连续同【签名】的行合成一段。首尾各留一行原文，中间只报次数。"""
    runs: list[dict[str, Any]] = []
    for text, level, inherited in rows:
        key = signature(text)
        if runs and runs[-1]["key"] == key:
            last = runs[-1]
            last["times"] += 1
            last["last"] = text
            last["last_level"] = level
        else:
            runs.append({"key": key, "times": 1, "first": text, "last": text,
                         "level": level, "last_level": level})
    return runs


def summarize(text: str, *, level: str = "", fold: bool = True) -> dict[str, Any]:
    """日志 → 可核查的视图。`level` 是【下限】：warn 表示只看 warn 及以上。"""
    floor = str(level or "").strip().lower()
    if floor and floor not in LEVELS:
        return {"ok": False, "error": "level 只支持 %s，收到 %r。" % ("|".join(LEVELS), level)}
    raw = str(text or "").replace("\r\n", "\n")
    omitted = sum(int(m) for m in OMITTED_MARK.findall(raw))
    lines = raw.split("\n")
    if lines and lines[-1] == "" and raw.endswith("\n"):
        lines.pop()                     # 行尾换行不是一行空行，别让它进计数
    overflow = max(0, len(lines) - MAX_LOG_LINES)
    if overflow:
        lines = lines[:MAX_LOG_LINES]
    rows = _levels(lines)

    rank: dict[str, dict[str, Any]] = {}
    for text, _level, _inh in rows:
        key = signature(text)
        if not key:
            continue
        row = rank.setdefault(key, {"times": 0, "first": text})
        row["times"] += 1
    rank_rows = sorted(rank.items(), key=lambda kv: (-kv[1]["times"], kv[0]))[:TOP_GROUPS]

    keep_from = LEVELS.index(floor) if floor else 0
    units: list[dict[str, Any]] = _runs(rows)
    out_lines: list[str] = []
    dropped_runs = dropped_lines = folded_away = markers_added = dropped_failures = 0
    for row in units:
        grade = row["level"]
        if floor and grade in LEVELS and LEVELS.index(grade) < keep_from:
            dropped_runs += 1
            dropped_lines += row["times"]
            if FAILURE_MARK.search(row["first"]) or FAILURE_MARK.search(row["last"]):
                dropped_failures += 1
            continue
        if row["times"] == 1:
            out_lines.append(row["first"])
        elif fold and row["times"] >= MAX_FOLD_RUN:
            out_lines.append(row["first"])
            out_lines.append("… ↑ 同一件事重复 %d 次（差别只在数字/请求 id）" % row["times"])
            out_lines.append(row["last"])
            folded_away += row["times"] - 2      # 真的少显示了几行原文
            markers_added += 1                   # 折出来的那行提示是【合成行】，不记进去对不上账
        else:                                # 次数不到折叠门槛：原样留着，不做主视图的压缩
            out_lines.append(row["first"])
            out_lines.extend(row["last"] for _ in range(row["times"] - 1))
    return {"ok": True, "text": "\n".join(out_lines), "lines_in": len(lines),
            "lines_out": len(out_lines), "folded_away": folded_away,
            "markers_added": markers_added,
            "dropped_runs": dropped_runs, "dropped_lines": dropped_lines,
            "dropped_failure_marks": dropped_failures,
            "middle_omitted_chars": omitted, "line_cap_hit": overflow,
            "levels_seen": sorted({level for _t, level, _i in rows if level}),
            "runs": len(units),
            "top_repeats": [{"times": v["times"], "line": v["first"][:160]}
                            for _k, v in rank_rows]}


def render(summary: dict[str, Any], *, title: str = "日志") -> str:
    """人读版本。计数与缺口先说，正文其次——先看可信度再看内容。"""
    if not summary.get("ok"):
        return "%s读取失败：%s" % (title, summary.get("error") or "未知原因")
    head = ("%s：%s 行 → 折成 %s 行（折掉 %s 行重复%s）" % (
        title, summary["lines_in"], summary["lines_out"], summary["folded_away"],
        "，按级别筛掉 %d 段/%d 行" % (summary["dropped_runs"], summary["dropped_lines"])
        if summary["dropped_runs"] else ""))
    lines = [head]
    if summary["middle_omitted_chars"]:
        lines.append("注意：上游缓冲已经丢掉中段 %s 字符，所以这里**不能说「日志里没有 X」**——"
                     "只看得见头尾。" % summary["middle_omitted_chars"])
    if summary.get("dropped_failure_marks"):
        lines.append("注意：被级别筛掉的 %d 段里有 %d 段带着 4xx/5xx/失败 字样——"
                     "这份视图不证明没有错误。" % (summary["dropped_runs"],
                                                summary["dropped_failure_marks"]))
    if summary["line_cap_hit"]:
        lines.append("注意：只处理了前 %s 行（再多就截断），后面 %s 行没看。"
                     % (MAX_LOG_LINES, summary["line_cap_hit"]))
    for row in summary.get("top_repeats") or []:
        if row["times"] >= MAX_FOLD_RUN:
            lines.append("重复 %3d×：%s" % (row["times"], row["line"][:150]))
    lines.append("看到的级别：%s" % ("、".join(summary["levels_seen"]) or "无（纯文本输出）"))
    body = str(summary.get("text") or "").strip()
    if body:
        lines.append(body)
    return "\n".join(lines)


__all__ = ["LEVELS", "MAX_FOLD_RUN", "level_of", "render", "signature", "summarize"]
