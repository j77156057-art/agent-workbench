"""把「进行中的任务」外置成项目状态目录里的一个文件——让压缩对话变得**无损**。

## 为什么是文件，而不是更多压缩

上下文压缩是**有损**的：摘要有取舍，摘得越多、退化越明显（这是社区反复验证的
经验，不是我的猜测）。所以"让长对话不退化"的正解不是压得更狠，而是
**把任务状态搬出上下文**：

    · 计划清单在磁盘上（这里）；
    · 每轮都把它注入系统消息；
    · 于是"上下文被摘掉一大半"不再等于"任务目标丢了"——
      对话可以随便压，目标不会丢。

这直接对应社区里跑长时自治的那批实践：plan 文档 + 勾选状态 + 跨上下文窗口接力。
本仓的子代理已经会开独立会话（`agent._run_child` 用 `session_id=None`），
所以"换窗口"这件事的**机制**是有的，缺的正是"换窗口时读什么"——就是这份清单。

## 为什么落在项目状态目录而不是工作树

工作树是别人的工作面（多人同仓时把临时文件写进去等于给别人埋雷）。
本仓 `dev_propose` 已经确立了这个约定：产物写状态目录，`git status` 不因此变脏。

## 格式：JSON 是事实来源，markdown 只是渲染

勾选/取消勾选必须**可靠**且幂等，所以不用正则去解析 markdown 往回写；
但模型和人要读的是一份可勾选的清单，所以对外渲染成 `- [ ] / - [x]`。
两者不互相解析，因此没有"模型手改文件把状态改坏"的风险。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

import project_state

_LOCK = threading.RLock()

#: 状态文件名（相对项目状态目录）。刻意带版本号：将来换结构时老文件自然作废，
#: 而不是被新代码误读成别的东西。
PLAN_FILE = "plan-v1.json"

MAX_ITEMS = 80
MAX_TEXT = 400

#: 条目 key 由标题派生，因此同样的标题重复添加会**合并**而不是堆出重复项。
#: 这很重要：模型重述一遍计划不该产生三份一样的条目。
_KEY_STRIP = re.compile(r"[^0-9a-z一-鿿]+")


def _key_of(text: str) -> str:
    slug = _KEY_STRIP.sub("-", str(text or "").strip().lower()).strip("-")
    return (slug[:48] or "item-%d" % int(time.time() * 1000))


def _plan_path(root: str) -> str:
    return project_state.path(root, PLAN_FILE)


def _read(root: str) -> dict:
    path = _plan_path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"title": "", "items": []}
    except (OSError, ValueError):
        # 状态文件损坏不是致命错误：报成"没有计划"，而不是让整个问答崩掉。
        # fail-closed 的方向是"少给信息"，不是"拒绝服务"。
        return {"title": "", "items": [], "corrupt": True}
    if not isinstance(data, dict):
        return {"title": "", "items": [], "corrupt": True}
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    return {"title": str(data.get("title") or ""), "items": items[:MAX_ITEMS]}


def _write(root: str, plan: dict) -> None:
    path = _plan_path(root)
    payload = {"version": 1, "title": plan.get("title") or "",
               "updated_at": time.time(), "items": plan["items"][:MAX_ITEMS]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _clean(items) -> list:
    out = []
    for raw in items or []:
        if isinstance(raw, dict):
            text = str(raw.get("text") or "").strip()
            key = str(raw.get("key") or "").strip() or None
            done = bool(raw.get("done"))
        else:
            text, key, done = str(raw or "").strip(), None, False
        if not text:
            continue
        text = text[:MAX_TEXT]
        out.append({"key": key or _key_of(text), "text": text, "done": done})
    return out


def render(plan: dict) -> str:
    """渲染成可勾选清单。模型读的是这个，不读 JSON。"""
    items = plan.get("items") or []
    if not items:
        return "（当前没有任务清单）"
    lines = []
    if plan.get("title"):
        lines.append("# %s" % plan["title"])
        lines.append("")
    done = sum(1 for i in items if i.get("done"))
    lines.append("进度：%d/%d 已完成。下一件事：%s"
                 % (done, len(items),
                    next((i["text"] for i in items if not i.get("done")), "（全部完成）")))
    lines.append("")
    for item in items:
        lines.append("- [%s] %s  <!-- %s -->"
                     % ("x" if item.get("done") else " ", item["text"], item["key"]))
    return "\n".join(lines)


def summary_for_prompt(root: str) -> str:
    """注入系统消息用的紧凑版。空计划返回空串——不往提示里塞无用的东西。"""
    plan = _read(root)
    if not plan.get("items"):
        return ""
    pending = [i for i in plan["items"] if not i.get("done")]
    if not pending:
        done = len(plan["items"])
        return ("任务清单（%s）已全部完成（%d 项）。"
                "如果用户提出新任务，用 dev_plan 重开清单。" % (plan.get("title") or "未命名", done))
    head = ["任务清单（%s），未完成项如下——继续做**下一件未完成的事**，不要重做已完成的："
            % (plan.get("title") or "未命名")]
    for item in pending[:8]:
        head.append("  - [ ] %s" % item["text"])
    if len(pending) > 8:
        head.append("  - …… 还有 %d 项，用 dev_plan(action: read) 看全量。" % (len(pending) - 8))
    head.append("清单变了用 dev_plan(action: check/append) 更新，别只留在对话里。")
    return "\n".join(head)


def open_plan(root: str, title: str, items) -> dict:
    """开一份新清单（清掉旧的）。同名标题的条目会被合并，不会重复堆叠。"""
    with _LOCK:
        plan = {"title": str(title or "").strip()[:MAX_TEXT],
                "items": _clean(items)}
        seen = set()
        deduped = []
        for item in plan["items"]:
            if item["key"] in seen:
                continue
            seen.add(item["key"])
            deduped.append(item)
        plan["items"] = deduped[:MAX_ITEMS]
        _write(root, plan)
        return plan


def append_items(root: str, items) -> dict:
    with _LOCK:
        plan = _read(root)
        existing = {i["key"] for i in plan["items"]}
        added, skipped = 0, 0
        for item in _clean(items):
            if item["key"] in existing:
                skipped += 1
                continue
            existing.add(item["key"])
            plan["items"].append(item)
            added += 1
        _write(root, plan)
        plan["_added"] = added
        plan["_skipped"] = skipped
        return plan


def set_done(root: str, key: str, done: bool) -> dict:
    with _LOCK:
        plan = _read(root)
        target = str(key or "").strip()
        hit = None
        for item in plan["items"]:
            if item["key"] == target or item["text"].startswith(target):
                item["done"] = bool(done)
                hit = item
                break
        _write(root, plan)
        plan["_hit"] = hit
        return plan


def clear(root: str) -> dict:
    with _LOCK:
        _write(root, {"title": "", "items": []})
        return {"title": "", "items": []}


def read(root: str) -> dict:
    with _LOCK:
        return _read(root)