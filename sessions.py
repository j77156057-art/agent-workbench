"""会话隔离 + 持久化 + 滚动摘要。

把 Agent 的对话记忆从"进程内单例的一个 list"提升为**按 session_id 落盘**的会话：
每个会话一个 JSON 文件，保存最近若干轮问答 + 一段"早期对话摘要"。

- 隔离：不同 session_id 互不可见，解决 api.py 复用同一 Agent 导致历史串台的问题。
- 持久化：重启服务后同一 session_id 仍能续聊。
- 摘要压缩：轮数/字符超阈值时，把最早的若干轮交给 LLM 压缩成一段摘要，
  只保留最近 KEEP 轮原文 —— 长会话不会把上下文预算吃光。

存储：一个 session_id 就是**一个会话**，全局唯一，落在
`<STATE_ROOT>/.docmind_sessions/<slug>.json`。

## 为什么从「按项目分桶」改成全局（2026-10-10，乙方案）

P2 起会话按 `<project_id>/<slug>.json` 分桶。实测（带新代码的隔离实例）：
同一个 session_id 在项目 A 下问一次、切到项目 B 再问一次，
两次分别绑定 A 和 B —— **原缺陷「切项目顶掉会话的库」换个形式重演**。

根因：`_agent_key` 用 `pid::sid` 把 **Agent 实例**按项目分开了，而
`session_id` 本身还是同一个字符串。「同一个会话跨项目」这件事
在「按项目分桶」的磁盘上**根本不存在** —— B 项目读不到 A 桶的文件，
于是当成新会话重新绑。

会话级代码库隔离的全部前提就是「**这个会话一直用它绑过的那个库**」。
分桶让这个前提不成立，所以分桶必须去掉。

**读穿透**：旧安装的分桶文件仍在 `<SESSIONS_DIR>/<pid>/<slug>.json`。
读取时并上这些历史文件，不搬文件、零数据风险；首次写盘即落到全局路径，
自然完成迁移。

同名冲突：两个项目桶里有同一 slug 时，`code_root` 取**最早绑定的那份**
（`bind_code_root` 起会写 `code_root_at` 时间戳；老记录没有该字段时
回退按 `updated_at` 最早）。turns / summary / snapshots 取 `updated_at`
最新的那份。合并只发生在迁移那一次，之后全局只有一份。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime

from config import STATE_ROOT, state_path
import projects

SESSIONS_DIR = state_path("DOCMIND_SESSIONS_DIR", os.path.join(STATE_ROOT, ".docmind_sessions"))
# 压缩以 token 占用为准（阈值由 Agent 按模型真实窗口换算后传入），轮数只留两道
# 硬保险：轮数上限防失控；保留轮数下限保证最近的上下文不被摘要掉。
MAX_TURNS = int(os.getenv("DOCMIND_SESSION_MAX_TURNS", "200"))       # 轮数硬上限（超过必压缩）
KEEP_TURNS_MIN = int(os.getenv("DOCMIND_SESSION_KEEP_TURNS_MIN", "2"))  # 压缩至少保留的原文轮数
# 直接调用 maybe_compact 且未传 token 阈值时的兜底（约等于旧 8000 字符的保守口径）
DEFAULT_TRIGGER_TOKENS = int(os.getenv("DOCMIND_COMPACT_TRIGGER_TOKENS", "7000"))
DEFAULT_KEEP_TOKENS = int(os.getenv("DOCMIND_COMPACT_KEEP_TOKENS", "3500"))

_lock = threading.Lock()

# 早期 API 版本曾把路由上下文直接拼在用户问题前，并写入会话历史。
# 读取时只清理这种“从开头开始的完整旧前缀”，不改动用户正文中的普通提及。
_LEGACY_PROMPT_PREFIX = re.compile(r"^\s*【系统提示】.*?用户问题：\s*", re.S)


def _clean_legacy_prompt(value) -> str:
    text = str(value or "")
    match = _LEGACY_PROMPT_PREFIX.match(text)
    return text[match.end():].lstrip() if match else text


def _slug(session_id) -> str:
    """把任意 session_id 归一成安全文件名（防目录穿越 / 非法字符）。"""
    s = re.sub(r"[^0-9A-Za-z_.-]", "_", str(session_id or "default"))
    s = s.strip("._") or "default"
    return s[:64]


def _bucket_dirs() -> list:
    """会话存储目录：**全局扁平目录**（唯一写入目标）。

    返回单元素列表，只为让「读穿透」的调用方保持统一形状。
    `project_id` 一律忽略 —— 一个 session_id 就是一个会话，与项目无关。
    """
    return [SESSIONS_DIR]


def _legacy_bucket_dirs() -> list:
    """旧的按项目分桶目录（P2 期间写入的 `<SESSIONS_DIR>/<pid>/`）。

    只用于**读穿透与清理**，不再写入。这些子目录里只会有 `.json`
    （分桶目录从不含子目录），所以只扫一层、且只收目录。
    """
    out = []
    try:
        names = sorted(os.listdir(SESSIONS_DIR))
    except OSError:
        return out
    for name in names:
        sub = os.path.join(SESSIONS_DIR, name)
        if os.path.isdir(sub):
            out.append(sub)
    return out


def _path(session_id, project_id=None) -> str:
    """会话文件的**唯一**路径（全局扁平；project_id 仅为签名兼容而保留）。"""
    return os.path.join(SESSIONS_DIR, _slug(session_id) + ".json")


def _legacy_dir() -> str:
    """旧的扁平会话目录（= 现在的全局目录；留名供历史调用方）。"""
    return SESSIONS_DIR


def _legacy_path(session_id) -> str:
    return os.path.join(_legacy_dir(), _slug(session_id) + ".json")


def _read_raw(path):
    """读一个会话 JSON 文件；缺失/损坏/非 dict 返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def _apply_raw(data: dict, raw) -> dict:
    """把磁盘记录填进会话结构（raw 为 None → 保持空结构）。"""
    if isinstance(raw, dict):
        turns = raw.get("turns")
        data["summary"] = raw.get("summary") or ""
        data["turns"] = [
            {
                **t,
                "user": _clean_legacy_prompt(t.get("user", "")),
                "assistant": _clean_legacy_prompt(t.get("assistant", "")),
            }
            for t in turns if isinstance(t, dict)
        ] if isinstance(turns, list) else []
        # archived：长会话压缩时被弹出工作窗口（turns）的更早轮次，完整逐字保留，
        # 供 UI 回灌完整历史。老会话没这个字段 -> 空列表（行为与改动前一致）。
        archived = raw.get("archived")
        data["archived"] = [
            {
                **t,
                "user": _clean_legacy_prompt(t.get("user", "")),
                "assistant": _clean_legacy_prompt(t.get("assistant", "")),
            }
            for t in archived if isinstance(t, dict)
        ] if isinstance(archived, list) else []
        data["updated_at"] = raw.get("updated_at") or ""
        # ⚠⚠ **会话要记住自己用的是哪个代码库**（2026-10-10 加）。
        #
        # 实测事故：mean 题那次的探针目录被设成**全局** code_root；四天后在同一项目
        # 里开新会话问「优化界面」，中间件某一环没设成上下文 -> 静默回落到那个全局值
        # -> research 在 `calc.py`（mean 题的代码）底下搜 mean -> 5 个子任务 0/5 全败。
        #
        # 会话隔离**做到了**（不同 session_id、不同文件）。没做到的是代码库隔离 ——
        # 代码库从来没「跟着会话走」这个概念，只挂在项目/全局上，于是四天后被继承。
        #
        # 这里记下会话建立时的代码库，之后该会话一直用它。老会话没这字段时为空串，
        # 调用方回落原有优先级 —— **行为与改动前完全一致**，不做静默改写。
        raw_root = raw.get("code_root")
        data["code_root"] = raw_root.strip() if isinstance(raw_root, str) else ""
        # 绑定时刻：合并多份历史记录时按它判「谁绑在前面」。老记录没有 -> 空串，
        # _merge_candidates 会回退按 updated_at。必须一起搬，否则永久性承诺
        # 在下次合并时失去判据。
        raw_at = raw.get("code_root_at")
        data["code_root_at"] = raw_at.strip() if isinstance(raw_at, str) else ""
        # 会话的**归属项目**（丙方案，2026-10-11）。
        #
        # 为什么需要：乙方案把会话存储改成全局一份，「同一个 session_id 跨项目
        # 仍是同一个会话」。但 `/api/sessions` 有产品语义 —— 用户在项目 B 里
        # 不该看到项目 A 的会话列表。以前这靠「按项目分桶」天然做到；
        # 分桶去掉后就得**显式记住归属**。
        #
        # 归属在会话首次建立时定下，之后不改（与 code_root 的绑定同一次）。
        # 老会话没有这个字段 -> 空串 -> 调用方按「无归属」处理。
        raw_owner = raw.get("owner_project_id")
        data["owner_project_id"] = raw_owner.strip() if isinstance(raw_owner, str) else ""
        # 快照必须一起搬进来。这里漏掉它时症状特别隐蔽：snapshot() 写入成功、
        # 没有任何报错，但下一次 load 读回来的 data 里没有 snapshots，
        # 于是 restore_snapshot 永远说「还没有任何快照」——写得进、读不回来。
        snaps = raw.get("snapshots")
        data["snapshots"] = [s for s in snaps if isinstance(s, dict)] \
            if isinstance(snaps, list) else []
    return data


def _default() -> dict:
    # code_root / code_root_at 见 _apply_raw 的说明：会话级代码库归属与绑定时刻，
    # 老会话为空串（行为与改动前完全一致，不做静默改写）。
    # owner_project_id 见 _apply_raw 的说明：会话的**归属项目**（丙方案）。
    return {"session_id": "", "updated_at": "", "summary": "",
            "turns": [], "archived": [], "code_root": "", "code_root_at": "",
            "owner_project_id": ""}


def _read_all_candidates(session_id) -> list:
    """同一个 session_id 在所有存储位置上的记录（全局扁平 + 旧项目分桶）。

    返回 `[(来源标签, raw_dict), ...]`；读取失败的静默跳过。
    顺序即优先级：**全局扁平在前**（迁移后的唯一真源），旧分桶在后。
    """
    slug = _slug(session_id) + ".json"
    out = []
    for d in _bucket_dirs() + _legacy_bucket_dirs():
        p = os.path.join(d, slug)
        raw = _read_raw(p)
        if isinstance(raw, dict):
            out.append((d, raw))
    return out


def _merge_candidates(records: list) -> dict:
    """把同一 session_id 的多份历史记录合并成一份（只在迁移时发生）。

    规则（写死在注释里，不许随手改）：

    * `code_root` —— **最早绑定的那份**。绑定是永久承诺（`bind_code_root`
      的语义），迁移时不许因为「哪份最近被写过」就改掉它。有 `code_root_at`
      按它取最早；老记录没有该字段时回退按 `updated_at` 最早。
    * `turns` / `summary` / `snapshots` / `updated_at` —— `updated_at` 最新的那份，
      **平手时取 records 里靠前的那份**（`_read_all_candidates` 的顺序即优先级：
      全局扁平在前，旧分桶在后）。

    ⚠ 不用 `sorted(...)[-1]`：时间戳相同时它靠排序稳定性碰运气，实测会把旧
    分桶盖到全局文件上。`records` 是 `[(来源目录, raw_dict), ...]`，顺序即优先级。
    """
    if not records:
        return {}
    if len(records) == 1:
        return records[0][1]

    def _root_at(raw):
        return str(raw.get("code_root_at") or raw.get("updated_at") or "")

    # turns/summary/snapshots：updated_at 最新；平手保留靠前的
    newest = records[0][1]
    for _src, raw in records[1:]:
        if str(raw.get("updated_at") or "") > str(newest.get("updated_at") or ""):
            newest = raw

    # code_root：最早绑定的一份；平手保留靠前的
    bound = [raw for _src, raw in records
             if str(raw.get("code_root") or "").strip()]
    if bound:
        best = bound[0]
        for raw in bound[1:]:
            if _root_at(raw) < _root_at(best):
                best = raw
    else:
        best = None

    merged = dict(newest)
    if best is not None:
        merged["code_root"] = str(best.get("code_root") or "").strip()
        if best.get("code_root_at"):
            merged["code_root_at"] = best["code_root_at"]
    # owner 同样取"最早绑定的那份"—— 绑定是永久承诺，和 code_root 同理。
    # 漏了它时的症状特别隐蔽：turns/summary 都正常，但会话在项目列表里
    # 悄悄消失（`list_sessions(project_id=pid)` 按 owner 过滤）。
    owners = [raw for _src, raw in records
              if str(raw.get("owner_project_id") or "").strip()]
    if owners:
        first_owner = min(owners, key=lambda r: str(
            r.get("code_root_at") or r.get("updated_at") or ""))
        merged["owner_project_id"] = str(
            first_owner.get("owner_project_id") or "").strip()
    return merged


def load(session_id, project_id=None) -> dict:
    """读取一个会话记录；缺失/损坏返回空结构（调用方无需处理异常）。

    **全局唯一**：一个 session_id 就是一个会话，与项目无关（乙方案）。
    读穿透 = 全局扁平目录 + 所有旧项目分桶目录的同名文件合并；
    合并规则见 `_merge_candidates` 的注释，迁移完成后只剩全局一份。
    """
    data = _default()
    data["session_id"] = str(session_id or "default")
    records = _read_all_candidates(session_id)
    raw = _merge_candidates(records) if records else None
    return _apply_raw(data, raw)


def _write(session_id, data, project_id=None) -> bool:
    """写全局唯一的那份会话文件（project_id 忽略，仅为签名兼容保留）。"""
    try:
        d = _bucket_dirs()[0]
        os.makedirs(d, exist_ok=True)
        data["updated_at"] = datetime.now().isoformat(timespec="seconds")
        p = os.path.join(d, _slug(session_id) + ".json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def history(session_id, project_id=None):
    """返回给 Agent 回放的 turns 列表（list[{user, assistant}]）。"""
    return [{"user": t.get("user", ""), "assistant": t.get("assistant", ""),
             **({"resume_context": str(t["resume_context"])[-6000:]} if "resume_context" in t else {}),
             **{key: str(t[key])[:1200] for key in ("checkpoint_id", "failure_reason") if t.get(key)}}
            for t in load(session_id, project_id).get("turns", []) if isinstance(t, dict)]


def archived_turns(session_id, project_id=None) -> list:
    """长会话压缩时被弹出工作窗口的更早轮次（完整逐字，list[dict]）。

    Agent 在初始化时读它进 self.archived，之后每次压缩把新弹出的前缀累进来再落盘——
    这样跨轮、跨进程都不会丢已归档的历史。老会话没这个字段时返回空列表。
    """
    return [t for t in load(session_id, project_id).get("archived", []) if isinstance(t, dict)]


def full_history(session_id, project_id=None) -> list:
    """给 UI 回灌用的**完整**逐轮记录 = 归档（更早轮次）+ 当前工作窗口。

    顺序即时间序：先 archived，再 turns。压缩只收缩喂给 LLM 的工作窗口（turns），
    完整对话始终在此可见——这正是修掉的「长会话会删掉前面的对话」问题。
    """
    return archived_turns(session_id, project_id) + history(session_id, project_id)


def summary_text(session_id, project_id=None) -> str:
    return load(session_id, project_id).get("summary", "") or ""


def bind_code_root(session_id, root, project_id=None) -> bool:
    """把该会话的代码库**固定**为 `root`；已绑定的会话**不改**。

    绑一次，终身沿用 —— 这是整个隔离的核心。若每轮跟着当前项目变，
    等于没隔离，那正是本缺陷本身。

    同时定下**归属项目**（丙方案）：会话首次建立时它属于哪个项目。
    之后不随请求项目变化 —— 否则用户在项目 B 的列表里会看到 A 的会话。

    ⚠ 现在 load 是**全局**的：同一个 session_id 在项目 A 绑过之后，
    切到项目 B 再问，这里读到的是 A 的那份绑定 -> 直接返回 False，
    会话继续用 A。这正是乙方案要的行为（实测曾反过来：分桶时 B 会重新绑）。
    """
    with _lock:
        data = load(session_id, project_id)
        if data.get("code_root"):
            return False            # 已绑定：不动
        now = datetime.now().isoformat(timespec="seconds")
        data["code_root"] = str(root or "").strip()
        # 绑定时刻。合并多份历史记录时按它判「谁绑在前面」（见 _merge_candidates）。
        data["code_root_at"] = now
        # 归属项目：只在还没有时写一次。调用方没给就用「当前项目」。
        if not data.get("owner_project_id"):
            owner = (project_id if project_id is not None
                     else projects.current_project_id()) or ""
            data["owner_project_id"] = str(owner).strip()
        return _write(session_id, data, project_id)


def owned_by(session_id, project_id=None) -> str:
    """这个会话的归属项目；老会话（没这个字段）返回空串。"""
    if not session_id:
        return ""
    try:
        return str(load(session_id, project_id).get("owner_project_id") or "").strip()
    except Exception:               # noqa: BLE001 - 读不到不是错误
        return ""


def backfill_owner(session_id, project_id) -> bool:
    """给**老会话**补归属字段（一次性迁移用）。

    只在还没有归属时写。新会话走 `bind_code_root` 自动带上，不需要这个。
    """
    with _lock:
        data = load(session_id, project_id)
        if data.get("owner_project_id"):
            return False
        data["owner_project_id"] = str(project_id or "").strip()
        return _write(session_id, data, project_id)


def current_code_root(session_id, project_id=None) -> str:
    """这个会话记住的代码库；没记住则空串（调用方回落项目/全局）。"""
    if not session_id:
        return ""
    try:
        return str(load(session_id, project_id).get("code_root") or "").strip()
    except Exception:               # noqa: BLE001 - 读不到不是错误，回落即可
        return ""


def save(session_id, turns, summary=None, project_id=None, archived=None) -> bool:
    """整体落盘一个会话（turns = [{user, assistant}]）。

    archived：长会话压缩弹出的更早轮次（完整逐字），仅在压缩点由调用方传入并累进；
    传 None 表示**保持磁盘上已有的 archived 不变**——绝大多数常规 save 不关心归档，
    绝不能因为它们没带 archived 就把已归档的完整历史抹掉。
    """
    with _lock:
        data = load(session_id, project_id)
        data["session_id"] = str(session_id or "default")
        # ⚠ 会话**首次**落盘时把当时的代码库记下来，之后不再变。
        # 这里刻意不读 get_runtime —— 那会形成循环（config 依赖 sessions），
        # 而且 sessions 是纯存储层，不该知道"当前项目是什么"。
        # 真正的绑定由 api 入口调用 bind_code_root() 完成。
        if summary is not None:
            data["summary"] = summary
        data["turns"] = [
            {"user": t.get("user", ""), "assistant": t.get("assistant", ""),
             "ts": t.get("ts") or datetime.now().isoformat(timespec="seconds"),
             **({"resume_context": str(t["resume_context"])[-6000:]} if "resume_context" in t else {}),
             **{key: str(t[key])[:1200] for key in ("checkpoint_id", "failure_reason") if t.get(key)}}
            for t in (turns or []) if isinstance(t, dict)
        ]
        if archived is not None:
            data["archived"] = [
                {"user": t.get("user", ""), "assistant": t.get("assistant", ""),
                 "ts": t.get("ts") or "",
                 **({"resume_context": str(t["resume_context"])[-6000:]} if "resume_context" in t else {}),
                 **{key: str(t[key])[:1200] for key in ("checkpoint_id", "failure_reason") if t.get(key)}}
                for t in (archived or []) if isinstance(t, dict)
            ]
        return _write(session_id, data, project_id)


def _est_tokens(text: str) -> int:
    """无 LLM 客户端时的保守 token 估算：CJK 约 1.1 token/字，其余约 3.2 字符/token。

    与 llm.LLMClient._heuristic_tokens 同口径，但不含每条 +40 的固定底数
    （这里是整段历史一次计数，加底数会严重高估短历史）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return int(cjk * 1.1 + other / 3.2) + 1


def _turns_tokens(turns, counter) -> int:
    text = "\n".join(
        (t.get("user", "") or "") + "\n" + (t.get("assistant", "") or "")
        + ("\n" + str(t["resume_context"]) if t.get("resume_context") else "")
        for t in (turns or []) if isinstance(t, dict)
    )
    return int(counter(text) or 0)


_SUMMARY_PROMPT = (
    "你是对话压缩器。把下面这段【早期对话】压缩成不超过 300 字的中文要点，"
    "务必保留：已确认的结论、涉及的文件路径与行号、用户的偏好与约束、尚未解决的问题。"
    "不要复述寒暄，不要编造未出现的信息。只输出要点本身。\n\n早期对话：\n"
)


def _clip_with_mark(text, limit):
    """裁到 limit，但绝不"静默腰斩"：原样更长时显式加"…（截断）"标记，
    避免把 ALPHA-7731-OMEGA 这类精确标识符截成"看着完整的错值"（LONGCTX-Compaction-FactMangle）。
    切点若落在一段较短（<32 字符）的字母数字/_/- token 末尾，则回退到其起点，
    尽量不腰斩 token；残 token 过长则不强退（交给标记兜底）。返回 (串, 是否被截断)。"""
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    m = re.search(r"[A-Za-z0-9_\-]+$", cut)
    if m and m.start() >= limit - 32 and m.start() > 0:
        cut = cut[:m.start()].rstrip(" ,，。;；:：")
    return cut + "…（截断）", True


def _naive_summary(turns) -> str:
    """LLM 不可用/失败/返回空时的确定性兜底：抽取每轮【问答双方】首段拼要点。
    截断一律显式标"…（截断）"，并把助手侧要点也纳入（此前完全丢弃 assistant 事实）；
    头注明"可能不完整/被截断，勿当已验证事实"——防下游把残缺/错值当完整事实用。"""
    bits = []
    for t in turns:
        uq, _ = _clip_with_mark(t.get("user") or "", 160)
        ans, _ = _clip_with_mark(t.get("assistant") or "", 120)
        seg = []
        if uq:
            seg.append("问：" + uq)
        if ans:
            seg.append("答：" + ans)
        if seg:
            bits.append("· " + " ／ ".join(seg))
    return ("（早期对话摘要·本地兜底，可能不完整/被截断，勿当已验证事实；"
            "需要精确标识符/数值请重读来源或追问）\n" + "\n".join(bits))


def _llm_summary(turns, llm) -> str:
    if llm is None:
        return _naive_summary(turns)
    # 与 _naive_summary 一致：切片一律走 _clip_with_mark，超长时显式带"…（截断）"，
    # 不再用裸 [:400] 把落在 400 字之后的精确标识符静默腰斩后喂给摘要器
    # （LONGCTX-LLMSlice-Leak：修复此前只装了兜底、主路径漏装）。
    convo = "\n".join(
        "用户：%s\n助手：%s" % (
            _clip_with_mark(t.get("user") or "", 400)[0],
            _clip_with_mark(t.get("assistant") or "", 400)[0],
        )
        for t in turns
    )
    try:
        out = llm.chat(
            [{"role": "user", "content": _SUMMARY_PROMPT + convo}],
            stream=False, temperature=0.2,
        )
        out = (out or "").strip()
        return out or _naive_summary(turns)
    except Exception:  # noqa: BLE001 —— 压缩失败绝不能打断正常问答
        return _naive_summary(turns)


def maybe_compact(session_id, turns, llm=None, trigger_tokens=None, keep_tokens=None,
                  project_id=None):
    """历史 token 占用超过模型预算阈值时，把最早的若干轮压缩进 summary。

    - trigger_tokens：历史总 token 达到该值即压缩（由 Agent 按模型真实窗口 ×
      COMPACT_TRIGGER_RATIO 换算；1M 窗口模型与 16k 本地模型用各自的阈值）；
    - keep_tokens：压缩后最近原文轮次的 token 目标体积（× COMPACT_KEEP_RATIO），
      至少保留 KEEP_TURNS_MIN 轮；
    - 轮数超过 MAX_TURNS 硬上限也必压缩（与 token 阈值是「或」关系）；
    - project_id：摘要读写的项目桶（P3）；缺省 None = 「当前项目」，行为与改动前一致。
    返回 (turns, summary)；未触发压缩时原样返回。
    """
    turns = list(turns or [])
    trigger = int(trigger_tokens or DEFAULT_TRIGGER_TOKENS)
    keep = int(keep_tokens or DEFAULT_KEEP_TOKENS)
    counter = getattr(llm, "count_tokens", None) or _est_tokens

    over_tokens = _turns_tokens(turns, counter) > trigger
    over_turns = len(turns) > MAX_TURNS
    if not over_tokens and not over_turns:
        return turns, summary_text(session_id, project_id)

    # 从最旧轮次开始弹出，直到最近轮次装进 keep_tokens（轮数硬上限同时收敛），
    # 但给最近 KEEP_TURNS_MIN 轮免死金牌——再超也由每轮 _fit_budget 裁剪兜底。
    kept = list(turns)
    while (
        len(kept) > KEEP_TURNS_MIN
        and (len(kept) > MAX_TURNS or _turns_tokens(kept, counter) > keep)
    ):
        kept.pop(0)
    if len(kept) >= len(turns):
        return turns, summary_text(session_id, project_id)

    old = turns[: len(turns) - len(kept)]
    prev = summary_text(session_id, project_id)
    new_sum = _llm_summary(old, llm)
    summary = (prev + "\n" + new_sum).strip() if prev else new_sum
    return kept, summary


# ---------------------------------------------------------------------------
# 轮次快照（rewind / fork 的地基）
# ---------------------------------------------------------------------------
# 为什么需要：会话历史是**一条不断被覆写**的链——每轮问答都 save 一次，
# 只留最新状态。于是"回到第 3 轮看看当时是什么样"这件事**根本做不到**，
# 因为那个状态已经被覆盖掉了。这跟竞品（Claude Code 的 checkpoints、
# Codex 的 worktree + resume）在能力上差一档。
#
# 快照记在**同一份会话文件**里而不是新开文件：rewind 必须和会话同生共死，
# 否则会出现"会话删了、快照还在"的孤儿状态。代价是会话文件变大，
# 所以 SNAPSHOT_LIMIT 保留最近若干条。
SNAPSHOT_LIMIT = 50


def snapshot(session_id, turns, *, label="", summary="", project_id=None) -> bool:
    """在当前轮次落一个快照，供之后 rewind 回来。

    快照存的是**当刻的 turns 副本**，不是引用——否则后续 save 会把它一起改掉，
    那就不是快照而是别名。
    """
    turns = list(turns or [])
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list):
        snaps = []
    snaps.append({
        "seq": len(snaps) + 1,
        "at": datetime.now().isoformat(timespec="seconds"),
        "label": str(label or "")[:200],
        "turn_count": len(turns),
        "turns": turns,
        "archived": [dict(t) for t in (data.get("archived") or []) if isinstance(t, dict)],
        "summary": str(summary or data.get("summary") or ""),
    })
    data["snapshots"] = snaps[-SNAPSHOT_LIMIT:]
    return _write(session_id, data, project_id)


def snapshots(session_id, project_id=None) -> list:
    """列出快照（元信息 + 该轮次的问答摘要，不带完整 turns 以免响应过大）。"""
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list):
        return []
    out = []
    for item in snaps:
        if not isinstance(item, dict):
            continue
        turns = item.get("turns") or []
        last_user = ""
        for turn in reversed(turns):
            if isinstance(turn, dict) and str(turn.get("user") or "").strip():
                last_user = str(turn.get("user") or "").strip()
                break
        out.append({
            "seq": item.get("seq"),
            "at": item.get("at") or "",
            "label": item.get("label") or "",
            "turn_count": item.get("turn_count", len(turns)),
            "question": last_user[:200],
        })
    return out


def restore_snapshot(session_id, seq, *, project_id=None) -> dict:
    """回到第 seq 个快照那一刻的状态（截断掉之后的轮次）。

    返回 `{"ok": bool, "turns": [...], "summary": str, "reason": str}`。
    找不到时不抛异常——调用方据此给用户一句能读懂的说明，而不是 500。
    """
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        return {"ok": False, "turns": [], "summary": "",
                "reason": "这个会话还没有任何快照，没法回退。"}
    target = None
    for item in snaps:
        if isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0):
            target = item
            break
    if target is None:
        return {"ok": False, "turns": [], "summary": "",
                "reason": "找不到 seq=%s 的快照（当前保留 %d 条）。" % (seq, len(snaps))}
    turns = [dict(t) for t in (target.get("turns") or []) if isinstance(t, dict)]
    summary = str(target.get("summary") or "")
    archived = [dict(t) for t in (target.get("archived") or []) if isinstance(t, dict)]
    data["turns"] = turns
    data["summary"] = summary
    data["archived"] = archived
    # 被回退掉的快照一并丢弃：它们的 seq 已经对应不上了，留着会误导。
    data["snapshots"] = [i for i in snaps
                         if isinstance(i, dict) and int(i.get("seq") or 0) <= int(seq)]
    _write(session_id, data, project_id)
    return {"ok": True, "turns": turns, "summary": summary, "archived": archived,
            "reason": "已回到第 %s 个快照（%s 轮）。" % (seq, len(turns))}


def fork_session(session_id, new_id, *, at_seq=None, project_id=None) -> dict:
    """从某个快照分叉出一条新会话；不动原会话。

    竞品对照：Codex 用 git worktree 隔离并行任务，Claude Code 用 checkpoints。
    这里走的是"新会话 id + 复制 turns"的路子——它不需要 git，因此也不受
    「当前不是 git 工作树」的限制（handoff 工具在那种场景下只能明说做不到）。
    """
    new_id = str(new_id or "").strip()
    if not new_id:
        return {"ok": False, "reason": "新会话 id 不能为空。"}
    data = load(session_id, project_id=project_id)
    if not data.get("turns") and not data.get("snapshots"):
        return {"ok": False, "reason": "原会话没有可分叉的历史。"}
    turns, summary = list(data.get("turns") or []), data.get("summary") or ""
    if at_seq is not None:
        got = restore_snapshot_snapshot_only(session_id, at_seq, project_id=project_id)
        if not got.get("ok"):
            return got
        turns, summary = got["turns"], got["summary"]
        # 按快照分叉：归档也要退回该快照那一刻，否则 fork 出的会话会带上
        # 原会话的完整归档，与快照 turns 拼出重复/错位的完整历史。
        data["archived"] = got.get("archived") or []
    data["session_id"] = new_id
    data["turns"] = turns
    data["summary"] = summary
    # 分叉出来的新会话**不继承快照序列**：它的快照语义与原会话不同，
    # 混用会让 restore 回到不属于这条链的状态。
    data["snapshots"] = []
    saved = _write(new_id, data, project_id)
    return {"ok": bool(saved), "reason": "已分叉为新会话 %s。" % new_id if saved
            else "分叉失败：新会话写盘未成功。", "session_id": new_id,
            "turns": turns}


def restore_snapshot_snapshot_only(session_id, seq, *, project_id=None) -> dict:
    """只算不写：给 fork 用，避免分叉顺带改掉原会话的快照链。"""
    data = load(session_id, project_id=project_id)
    snaps = data.get("snapshots")
    if not isinstance(snaps, list) or not snaps:
        return {"ok": False, "turns": [], "summary": "", "archived": [], "reason": "没有可用快照。"}
    for item in snaps:
        if isinstance(item, dict) and int(item.get("seq") or 0) == int(seq or 0):
            return {"ok": True,
                    "turns": [dict(t) for t in (item.get("turns") or [])
                              if isinstance(t, dict)],
                    "summary": str(item.get("summary") or ""),
                    "archived": [dict(t) for t in (item.get("archived") or [])
                                 if isinstance(t, dict)],
                    "reason": ""}
    return {"ok": False, "turns": [], "summary": "", "archived": [],
            "reason": "找不到 seq=%s 的快照。" % seq}


def list_sessions(limit=50, project_id=None):
    """列出会话摘要（用于 /api/sessions 概览）。

    **全局唯一**：一个 session_id 就是一个会话，读取范围 = 全局扁平目录 ∪
    所有旧项目分桶。同一 session_id 在多处出现时**全局扁平优先**（迁移后的
    唯一真源），稳定去重；再按 updated_at 倒序取前 limit。

    `project_id` 是**归属过滤**（丙方案，2026-10-11），不是存储路径：
    会话存储已全局化，但「用户在项目 B 里不该看到 A 的会话」这条产品语义还在。
    以前靠按项目分桶天然做到；现在改成读 `owner_project_id` 字段。

    * 传了 pid -> 只返回归属为 pid 的会话；
    * 不传 -> 全部返回。

    **归属从哪来（2026-10-11 收窄）**：先取文件里的 `owner_project_id`，
    缺失时回退到**旧分桶的目录名**（`prj-<hex>`）。此前是「无归属一律放行」，
    那条豁免把 308 个没有该字段的旧副本一起放行了 —— 实测只有 4 个会话的
    项目，列表里出现 336 条，过滤形同虚设。归属本来就在目录名里，读出来即可。
    真正无归属的（`golden-*` 基线夹具，住在扁平目录、无对应桶）按用户拍板
    **不再出现在项目列表里**，但不传 pid 时仍全都可见 —— 数据不会消失。

    ⚠⚠ 为什么无归属的老会话在显式过滤时**也要返回**：P2 的明确意图是
    「确保迁移后用户既有历史不消失（用户原始 Bug 不复发）」
    （见 `16b1010` 的提交说明与 `tests/test_projects_qa.py::B10`）。
    第一版丙方案在这里把无归属会话一刀切掉 —— 那条既有判据立刻红，
    证明那是个**真回归**：升级一次，历史会话全从列表里消失。

    代价是：项目 B 的列表里会混进无归属的老会话。这比"历史消失"好得多，
    而且是一次性的 —— 跑过 `backfill_owner` 之后就没有无归属会话了。
    结果行里带 `owner_project_id`，前端若想区分可以自己判空。
    """
    seen = {}
    want_pid = str(project_id or "").strip() or None
    for d in _bucket_dirs() + _legacy_bucket_dirs():
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        # 旧分桶目录名本身就是归属（`prj-<hex>`）。`bind_code_root` 是丙方案
        # 之后才写的，所以这些历史文件里**没有** `owner_project_id` 字段 ——
        # 但归属并没有丢，它就在目录名里。不读出来的话，缺字段的会话会被
        # 当成「无归属 → 放行」，把归属过滤彻底淹没（实测：只有 4 个会话的
        # 项目，列表里出现 336 条）。
        dir_owner = ""
        base = os.path.basename(os.path.normpath(d))
        if base.startswith("prj-"):
            dir_owner = base
        for name in names:
            if not name.endswith(".json"):
                continue
            raw = _read_raw(os.path.join(d, name))
            if not isinstance(raw, dict):
                continue
            sid = raw.get("session_id") or name[:-5]
            if sid in seen:
                continue  # 全局先遍历 → 全局条目优先
            # 归属：**文件里的字段优先**，缺失时回退到目录名。
            owner = str(raw.get("owner_project_id") or "").strip() or dir_owner
            # 归属过滤（丙）。**与我要的不一致就排除** —— 包括「无归属」。
            #
            # ⚠ 此前是「有归属且不是我要的才排除」，无归属一律放行。那条豁免
            # 是为保 B10 的用户历史不消失，但它把 308 个没有该字段的旧副本
            # 一起放行了，过滤形同虚设（实测：只有 4 个会话的项目，列表里
            # 出现 336 条）。
            # 现在归属从目录名读出来了，旧历史照样读得到（B10 仍成立）；
            # 而真正无归属的（`golden-*` 基线夹具，按用户拍板）不再出现。
            # ⚠ 两条判据盯着这次改动：`tests/test_owner_filter_narrow.py`
            #   与 `tests/test_projects_qa.py::B10`。前者是收窄，后者是读穿透。
            if want_pid and owner != want_pid:
                continue
            turns = raw.get("turns") or []
            archived = raw.get("archived") or []
            # 完整逐轮 = 归档 + 工作窗口：压缩只收缩喂 LLM 的 turns，列表里的轮数
            # 与标题（首条用户问题）都必须按完整历史算，否则长会话会「轮数变少、
            # 标题跳到中途那一问」。
            all_turns = [t for t in archived if isinstance(t, dict)] + \
                        [t for t in turns if isinstance(t, dict)]
            first_user = ""
            for turn in all_turns:
                if isinstance(turn, dict) and str(turn.get("user") or "").strip():
                    first_user = _clean_legacy_prompt(turn.get("user", "")).strip()
                    break
            preview = " ".join(first_user.split())
            seen[sid] = {
                "session_id": sid,
                "turns": len(all_turns),
                "has_summary": bool(raw.get("summary")),
                "updated_at": raw.get("updated_at") or "",
                # 归属项目（丙）。**回退到桶目录名** —— 见上面 dir_owner。
                # 读穿透拿到的旧记录因此也带着归属，不再是一串空串。
                "owner_project_id": owner,
                # 首条用户问题作为可读标题；不保存额外数据，兼容旧会话文件。
                "title": preview[:72] or "未命名对话",
                "preview": preview[:180],
            }
    rows = list(seen.values())
    rows.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return rows[:max(1, int(limit))]


def delete(session_id, project_id=None) -> bool:
    """删除会话：全局文件与所有旧项目分桶里的同名文件一并删除。

    一个 session_id 就是一个会话，所以删要删干净 —— 只删全局那份会留下
    分桶里的旧文件，下次 load 的读穿透又把它捞回来（用户看到"删了又出现"）。
    """
    ok = True
    targets = {_path(session_id, project_id), _legacy_path(session_id)}
    for d in _legacy_bucket_dirs():
        targets.add(os.path.join(d, _slug(session_id) + ".json"))
    for p in targets:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            ok = False
    return ok
