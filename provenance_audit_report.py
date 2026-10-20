# -*- coding: utf-8 -*-
"""B3 provenance 审计消费侧：读 `DOCMIND_PROVENANCE_AUDIT` 落盘的 JSONL，
出 per-number 未背书的「逐条清单 + 聚合看板」，并渲染单文件 HTML 报告。

**纯消费 / 只读**：不改 episode 的 `backed`、不碰生产 agent/tools 链路。审计记录
schema 由 `agent._provenance_audit_emit` 产出：

    {ts:int, key:str, backed_hit:bool, n_fnums:int, backed:list, unbacked:list}

分类口径（§5「episode 任一数命中即整条背书」高估问题的可计算化）：

    overestimated —— `backed_hit=True` **且** `unbacked` 非空：粗 bool 说「有背书」，
                     但答案里确有程序内自算、无强产出背书的数——这正是 bool 与
                     per-number 明细之间的缺口，也是本报告要暴露的东西。
    fully_backed  —— `backed_hit=True` 且 `unbacked` 为空。
    not_backed    —— `backed_hit=False`（bool 本就保守，谈不上高估）。

一切解析 fail-closed：坏行 / 缺键 / 坏类型 / 文件缺失一律跳过或回安全默认，绝不抛。
"""
from __future__ import annotations

import json
import os
from collections import Counter

_EMPTY_KEY = ""


def _as_int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _as_str_list(v):
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x) for x in v if x is not None]


def _norm_rec(raw):
    """把一条原始 dict 归一化成消费用记录；缺键/坏类型 fail-closed 回安全默认。纯函数。"""
    raw = raw if isinstance(raw, dict) else {}
    return {
        "ts": _as_int(raw.get("ts")),
        "key": str(raw.get("key", "") or ""),
        "backed_hit": bool(raw.get("backed_hit", False)),
        "n_fnums": _as_int(raw.get("n_fnums")),
        "backed": _as_str_list(raw.get("backed")),
        "unbacked": _as_str_list(raw.get("unbacked")),
    }


def classify(rec):
    """一条归一化记录 → overestimated / fully_backed / not_backed。"""
    if not rec.get("backed_hit"):
        return "not_backed"
    return "overestimated" if rec.get("unbacked") else "fully_backed"


def load_audit(path):
    """读 JSONL → 归一化记录列表（每条附 `cls`）。

    文件缺失 / 空 / 坏 JSON 行 / 非对象行一律跳过（fail-closed，绝不抛），
    对齐 `agent_trace.recent` 对损坏行的健壮处理。
    """
    out = []
    if not path or not os.path.isfile(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                rec = _norm_rec(obj)
                rec["cls"] = classify(rec)
                out.append(rec)
    except OSError:
        return out
    return out


def summarize(records):
    """逐条分类 + 聚合看板。纯函数。

    返回 {total, counts{overestimated/fully_backed/not_backed}, episodes_with_numbers,
          overestimate_rate, top_unbacked:[(num,count)...], records:[...]}。
    高估率分母 = 「final 含数字」的 episode 数（无数字者谈不上高估），非总条数。
    """
    records = list(records or [])
    cats = Counter(r.get("cls") or classify(r) for r in records)
    with_numbers = sum(
        1 for r in records
        if _as_int(r.get("n_fnums")) > 0 or r.get("backed") or r.get("unbacked")
    )
    unbacked_counter = Counter()
    for r in records:
        for u in r.get("unbacked") or []:
            unbacked_counter[str(u)] += 1
    over = cats.get("overestimated", 0)
    rate = (over / with_numbers) if with_numbers else 0.0
    return {
        "total": len(records),
        "counts": {
            "overestimated": over,
            "fully_backed": cats.get("fully_backed", 0),
            "not_backed": cats.get("not_backed", 0),
        },
        "episodes_with_numbers": with_numbers,
        "overestimate_rate": round(rate, 4),
        "top_unbacked": unbacked_counter.most_common(12),
        "records": records,
    }


def build_report(path):
    """一步到位：load_audit → summarize。返回聚合 dict（含逐条 records）。"""
    return summarize(load_audit(path))


import html as _html  # noqa: E402  （放这里：仅展示层用，核心解析不依赖）
import time as _time  # noqa: E402

_CAT_LABEL = {
    "overestimated": "高估 · 头条未背书",
    "fully_backed": "全背书",
    "not_backed": "未背书（bool 保守）",
}


def _fmt_ts(ts):
    """epoch → UTC 可读串；坏值回破折号。绝不用 now()（保证报告确定性）。"""
    try:
        if ts:
            return _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(int(ts)))
    except (TypeError, ValueError, OSError):
        pass
    return "—"


def render_text(data, source=""):
    """终端摘要：聚合看板 + 逐条未背书清单。纯函数，返回 str。"""
    c = data.get("counts", {})
    lines = []
    lines.append("== B3 provenance 审计报告 ==")
    if source:
        lines.append("数据源：%s" % source)
    lines.append("总条数：%d" % data.get("total", 0))
    lines.append("  高估(头条未背书)：%d" % c.get("overestimated", 0))
    lines.append("  全背书：%d" % c.get("fully_backed", 0))
    lines.append("  未背书(bool 保守)：%d" % c.get("not_backed", 0))
    rate = data.get("overestimate_rate", 0.0)
    lines.append("高估率（分母=含数字 episode %d）：%.1f%%"
                 % (data.get("episodes_with_numbers", 0), rate * 100))
    top = data.get("top_unbacked", [])
    if top:
        lines.append("最常未背书的数 Top：")
        for num, cnt in top:
            lines.append("  %-12s ×%d" % (num, cnt))
    lines.append("-- 逐条明细 --")
    for r in data.get("records", []):
        lines.append("[%s]「%s」 %s | backed=%s | unbacked=%s" % (
            _fmt_ts(r.get("ts")), (r.get("key") or "—"),
            _CAT_LABEL.get(r.get("cls"), r.get("cls")),
            ",".join(r.get("backed") or []) or "—",
            ",".join(r.get("unbacked") or []) or "—"))
    return "\n".join(lines)


_CSS = (
    "body{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;"
    "margin:0;background:#f5f6f8;color:#1c2430;}"
    ".wrap{max-width:960px;margin:0 auto;padding:28px;}"
    "h1{font-size:20px;margin:0 0 4px;}"
    ".sub{color:#6b7684;font-size:13px;margin-bottom:20px;}"
    ".cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:22px;}"
    ".card{flex:1 1 150px;background:#fff;border:1px solid #e3e7ec;border-radius:10px;"
    "padding:14px 16px;}"
    ".card .n{font-size:24px;font-weight:600;}"
    ".card .l{font-size:12px;color:#6b7684;margin-top:4px;}"
    ".over{color:#c0392b;}"
    ".ok{color:#1e8e5a;}"
    ".muted{color:#8a94a1;}"
    "table{width:100%;border-collapse:collapse;background:#fff;"
    "border:1px solid #e3e7ec;border-radius:10px;overflow:hidden;}"
    "th,td{text-align:left;padding:10px 12px;font-size:13px;border-bottom:1px solid #eef1f4;}"
    "th{background:#fafbfc;color:#5a6572;font-weight:600;}"
    "tr:last-child td{border-bottom:none;}"
    "tr.hi{background:#fdf3f2;}"
    ".tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:12px;"
    "border:1px solid transparent;}"
    ".tag.overestimated{background:#fbe6e4;color:#c0392b;border-color:#f3c3bd;}"
    ".tag.fully_backed{background:#e3f3ea;color:#1e8e5a;border-color:#bfe3cd;}"
    ".tag.not_backed{background:#eef0f3;color:#8a94a1;border-color:#e0e4e9;}"
    "code{background:#f0f2f5;padding:1px 6px;border-radius:5px;font-size:12px;}"
    ".empty{color:#8a94a1;padding:16px 0;}"
)


def render_html(data, source=""):
    """把 summarize() 结果渲染成**自包含单文件 HTML**（内联 flat CSS、无外链、可离线打开）。

    所有动态文本（key / 数字）经 `html.escape` 转义——审计里的 key 是用户问题片段，
    数字来自模型输出，绝不原样插进 HTML。纯函数，返回 str。
    """
    esc = _html.escape
    c = data.get("counts", {})
    total = data.get("total", 0)
    rate = data.get("overestimate_rate", 0.0)

    parts = []
    parts.append("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width,initial-scale=1'>")
    parts.append("<title>B3 provenance 审计报告</title>")
    parts.append("<style>%s</style></head><body><div class='wrap'>" % _CSS)
    parts.append("<h1>W-C per-number 背书审计</h1>")
    src = ("<div class='sub'>数据源：" + esc(str(source)) + "</div>") if source else ""
    parts.append("<div class='sub'>episode 粗 bool「任一命中即背书」vs per-number 明细之间的高估缺口</div>")
    if src:
        parts.append(src)

    # 聚合看板
    parts.append("<div class='cards'>")
    parts.append("<div class='card'><div class='n'>%d</div><div class='l'>总条数</div></div>" % total)
    parts.append("<div class='card'><div class='n over'>%d</div><div class='l'>高估 · 头条未背书</div></div>"
                 % c.get("overestimated", 0))
    parts.append("<div class='card'><div class='n ok'>%d</div><div class='l'>全背书</div></div>"
                 % c.get("fully_backed", 0))
    parts.append("<div class='card'><div class='n muted'>%d</div><div class='l'>未背书</div></div>"
                 % c.get("not_backed", 0))
    parts.append("<div class='card'><div class='n over'>%.1f%%</div><div class='l'>高估率（分母 含数字 %d）</div></div>"
                 % (rate * 100, data.get("episodes_with_numbers", 0)))
    parts.append("</div>")

    # 最常未背书的数
    top = data.get("top_unbacked", [])
    if top:
        chips = " ".join("<code>%s ×%d</code>" % (esc(str(n)), cnt) for n, cnt in top)
        parts.append("<div class='sub'>最常出现在答案里、却无强产出背书的数：%s</div>" % chips)

    # 逐条清单
    if total:
        parts.append("<table><thead><tr><th>时间(UTC)</th><th>问题片段</th>"
                     "<th>分类</th><th>backed 数</th><th>unbacked 数</th></tr></thead><tbody>")
        for r in data.get("records", []):
            cls = r.get("cls", "not_backed")
            hi = " class='hi'" if cls == "overestimated" else ""
            parts.append(
                "<tr%s><td>%s</td><td>%s</td>"
                "<td><span class='tag %s'>%s</span></td>"
                "<td>%s</td><td>%s</td></tr>" % (
                    hi,
                    esc(_fmt_ts(r.get("ts"))),
                    esc(r.get("key") or "—"),
                    esc(cls), esc(_CAT_LABEL.get(cls, cls)),
                    esc(",".join(r.get("backed") or [])) or "—",
                    esc(",".join(r.get("unbacked") or [])) or "—"))
        parts.append("</tbody></table>")
    else:
        parts.append("<div class='empty'>暂无审计记录（审计默认关，需设 DOCMIND_PROVENANCE_AUDIT 后产生）。</div>")

    parts.append("</div></body></html>")
    return "".join(parts)


def main(argv=None):
    """CLI：读审计 JSONL → 终端摘要；--out 再写一份单文件 HTML。返回进程码。"""
    import argparse
    ap = argparse.ArgumentParser(description="B3 provenance 审计报告生成器（消费 DOCMIND_PROVENANCE_AUDIT）")
    ap.add_argument("audit", nargs="?", default=os.getenv("DOCMIND_PROVENANCE_AUDIT", ""),
                    help="审计 JSONL 路径（缺省读环境变量 DOCMIND_PROVENANCE_AUDIT）")
    ap.add_argument("--out", default="", help="HTML 输出路径；不给则只打印文本摘要")
    args = ap.parse_args(argv)
    path = args.audit
    if not path or not os.path.isfile(path):
        print("审计文件不存在或未配置：%s" % (path or "（未设 DOCMIND_PROVENANCE_AUDIT）"))
        return 2
    data = build_report(path)
    print(render_text(data, source=path))
    if args.out:
        d = os.path.dirname(os.path.abspath(args.out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(render_html(data, source=path))
        print("HTML 已写出：%s" % args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
