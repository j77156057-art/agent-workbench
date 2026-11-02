"""语义业务标签（阶段 1）：给代码文件打中文业务标签，支撑「大白话定位代码」。

为什么需要它：
- 向量检索能按语义找代码片段，但普通游戏开发者问的是「角色数值」「敌人 AI」
  这种业务概念，需要一份人能看懂、可人工修正的标签层。
- 标签缓存在 ``<root>/.docmind/semantic_tags.json``，按 mtime+size 增量更新；
  LLM 不可用时降级为关键词规则标签，绝不让功能整体不可用。
- 纯逻辑函数全部显式接收 ``root``，便于在临时目录里做单测。

记录结构（files[rel]）：
    {mtime, size, tags: [...2-4 个中文短标签], summary: 一句话,
     symbols: [顶层符号名...], origin: llm|rules|manual, stale, updated_at}
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime

from ingest import _CODE_EXT, _SKIP_DIRS, _MAX_CODE_FILE
import project_state

STORE_DIR = ".docmind"
STORE_NAME = "semantic_tags.json"
STORE_VERSION = 1

MAX_FILES = 1000          # 与 workbench_fs.SYMBOL_MAP_MAX_FILES 对齐
# 根目录分区契约文件不参与业务打标/定位（它们是治理元数据，不是游戏代码）
CONTRACT_ROOT_FILES = {
    "regions.json", "DOCMIND_RULES.md", "DEV_INDEX.md", "dev_changesets.jsonl",
}
EXCERPT_CHARS = 1500      # 喂给 LLM 的单文件正文上限
TOP_SYMBOLS = 12          # 喂给 LLM / 存盘的顶层符号上限
LLM_BATCH = 6             # 一次 LLM 调用标注的文件数
DEFAULT_LIMIT = 60        # 一次刷新最多标注的文件数（剩余再点一次「继续」）
MAX_TAGS = 4
SCAN_TTL = 5.0            # 代码文件清单缓存秒数

_scan_cache: dict = {}

# ---------------------------------------------------------------------------
# 规则降级标签：中英双语关键词（路径 + 正文头部命中即计分）
# 顺序即优先级展示顺序；命中数相同的标签按这里的先后排。
# ---------------------------------------------------------------------------
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("玩家角色", ("player", "avatar", "hero", "protagonist", "玩家", "主角")),
    ("角色属性", ("stat", "attribute", "attributes", "数值", "属性")),
    ("生命与战斗数值", ("hp", "health", "mp", "mana", "stamina", "damage", "attack",
                  "defense", "defence", "crit", "exp", "level", "speed",
                  "生命", "血量", "法力", "耐力", "伤害", "攻击", "防御",
                  "暴击", "命中", "经验", "等级", "攻速", "移速")),
    ("敌人AI", ("enemy", "monster", "mob", "boss", "ai", "behavior", "behaviour",
                "state_machine", "statemachine", "patrol", "chase",
                "敌人", "怪物", "小怪", "巡逻", "追击", "行为树", "状态机")),
    ("战斗系统", ("combat", "battle", "fight", "skill", "buff", "debuff",
                  "hitbox", "hurtbox", "projectile", "weapon", "bullet",
                  "战斗", "技能", "武器", "子弹", "受击")),
    ("背包道具", ("inventory", "item", "pickup", "loot", "drop", "equip",
                  "背包", "物品", "道具", "拾取", "掉落", "装备")),
    ("界面UI", ("ui", "hud", "menu", "button", "dialog", "panel", "widget",
                "tooltip", "toast", "界面", "菜单", "按钮", "弹窗", "血条",
                "头像", "面板")),
    ("对话剧情", ("dialogue", "story", "narrative", "cutscene", "剧情", "对话", "过场")),
    ("任务系统", ("quest", "mission", "objective", "任务")),
    ("商店经济", ("shop", "store", "currency", "coin", "gold", "economy",
                  "商店", "货币", "金币", "经济")),
    ("成就系统", ("achievement", "成就")),
    ("存档", ("save", "persist", "serialize", "存档", "读档", "序列化")),
    ("场景关卡", ("scene", "level", "room", "dungeon", "world",
                  "场景", "关卡", "地图", "房间")),
    ("游戏流程", ("game_manager", "gamemanager", "main", "bootstrap", "lifecycle",
                  "game_state", "gamestate", "app", "流程", "主循环")),
    ("音频", ("audio", "sound", "music", "sfx", "音频", "音效", "音乐")),
    ("视觉特效", ("vfx", "particle", "shader", "effect", "特效", "粒子", "着色器")),
    ("动画", ("anim", "animation", "tween", "动画")),
    ("输入控制", ("input", "controller", "keybind", "touch", "joystick",
                  "输入", "按键", "手柄", "摇杆")),
    ("物理碰撞", ("physics", "collision", "collider", "rigidbody", "raycast",
                  "物理", "碰撞", "刚体", "射线")),
    ("相机镜头", ("camera", "相机", "镜头")),
    ("网络同步", ("network", "multiplayer", "online", "socket", "rpc", "sync",
                  "网络", "联机", "同步")),
    ("配置数据", ("config", "settings", "constant", "table", "csv",
                  "配置", "常量", "数据表")),
    ("资源加载", ("asset", "resource", "loader", "import", "资源", "加载", "导入")),
    ("数学工具", ("math", "util", "utils", "helper", "vector", "geometry",
                  "random", "工具", "数学", "随机")),
    ("引擎底层", ("engine", "core", "runtime", "driver", "platform",
                  "引擎", "底层", "运行时")),
    ("自动化测试", ("test", "spec", "mock", "fake", "测试")),
    ("构建发布", ("build", "deploy", "release", "pipeline", "构建", "发布", "打包")),
]


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
def store_path(root: str) -> str:
    return project_state.path(root, STORE_NAME, legacy=os.path.join(STORE_DIR, STORE_NAME))


def load_store(root: str) -> dict:
    p = store_path(root)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            data.setdefault("version", STORE_VERSION)
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"version": STORE_VERSION, "files": {}}


def save_store(root: str, data: dict) -> None:
    d = os.path.dirname(store_path(root))
    os.makedirs(d, exist_ok=True)
    p = store_path(root)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# 代码文件扫描（5 秒缓存，与 git 快照同款策略）
# ---------------------------------------------------------------------------
def scan_code_files(root: str) -> list[dict]:
    """返回 [{rel, abs, mtime, size}]，跳过 _SKIP_DIRS/点目录，上限 MAX_FILES。"""
    now = time.time()
    cached = _scan_cache.get(root)
    if cached and now - cached[0] < SCAN_TTL:
        return cached[1]

    out: list[dict] = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(fns):
            if os.path.splitext(fn)[1].lower() not in _CODE_EXT:
                continue
            if len(out) >= MAX_FILES:
                break
            rel0 = os.path.relpath(os.path.join(dp, fn), root).replace("\\", "/")
            if rel0 in CONTRACT_ROOT_FILES:
                continue
            fp = os.path.join(dp, fn)
            try:
                st = os.stat(fp)
                if st.st_size > _MAX_CODE_FILE:
                    continue
            except OSError:
                continue
            out.append({
                "rel": os.path.relpath(fp, root).replace("\\", "/"),
                "abs": fp,
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    out.sort(key=lambda x: x["rel"])
    _scan_cache[root] = (now, out)
    return out


def _is_fresh(rec: dict, mtime: float, size: int) -> bool:
    return (
        not rec.get("stale")
        and int(rec.get("mtime", -1)) == int(mtime)
        and int(rec.get("size", -1)) == int(size)
    )


# ---------------------------------------------------------------------------
# 文件信息提取（正文摘录 + 顶层符号名）
# ---------------------------------------------------------------------------
def _file_brief(item: dict) -> dict:
    """读取正文摘录与顶层符号名；符号解析失败不影响正文标签。"""
    abs_path = item["abs"]
    text = ""
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            text = f.read(EXCERPT_CHARS + 400)
    except OSError:
        pass
    symbols: list[str] = []
    try:
        import symbols as symlib
        env = symlib.file_symbols(abs_path) or {}
        for s in (env.get("symbols") or [])[:TOP_SYMBOLS]:
            name = s.get("name")
            if name and name not in symbols:
                symbols.append(name)
    except Exception:  # noqa: BLE001
        pass
    return {"text": text[:EXCERPT_CHARS], "symbols": symbols}


# ---------------------------------------------------------------------------
# 规则降级标签
# ---------------------------------------------------------------------------
def rule_tags(rel: str, text: str, symbols: list[str]) -> tuple[list[str], str]:
    hay = f"{rel}\n{text[:4000]}\n{' '.join(symbols)}".lower()
    hits: list[tuple[int, int, str]] = []  # (命中数, 规则顺序负号, 标签)
    for idx, (label, kws) in enumerate(_RULES):
        n = sum(1 for kw in kws if kw.lower() in hay)
        if n:
            hits.append((n, -idx, label))
    hits.sort(reverse=True)
    tags = [h[2] for h in hits[:3]]
    if not tags:
        top = rel.split("/", 1)[0].rsplit(".", 1)[0]
        tags = ["其他逻辑" if top in ("scripts", "src", "source", "assets") else f"{top}相关"]
    summary = f"与「{tags[0]}」相关的代码文件。"
    return tags, summary


# ---------------------------------------------------------------------------
# LLM 批量标注
# ---------------------------------------------------------------------------
_LLM_INSTR = """你是游戏代码库的分类助手。下面给出若干代码文件的路径、语言、顶层符号名与正文摘录。
请为每个文件给出 2-4 个简短中文业务标签（每个标签不超过 6 个汉字，必须是游戏开发业务概念，
例如：玩家角色、角色属性、伤害计算、敌人AI、战斗系统、背包道具、界面UI、存档、场景关卡、
音频、特效、输入控制、物理碰撞、相机、网络同步、配置数据、资源加载、数学工具、引擎底层），
并用一句不超过 24 个汉字的中文说明这个文件负责什么（不要出现文件名）。

严格只输出 JSON，不要输出任何解释或 markdown 代码块，格式：
{"results":[{"path":"与输入完全一致的路径","tags":["标签1","标签2"],"summary":"一句话说明"}]}
无法判断时 tags 至少给 1 个最接近的业务标签。"""


def _default_llm(prompt: str) -> str:
    from llm import LLMClient
    resp = LLMClient().chat(
        [{"role": "user", "content": prompt}],
        stream=False, temperature=0.2, timeout=90,
    )
    if isinstance(resp, dict):
        return str(resp.get("content") or "")
    return str(resp)


def _parse_llm_json(raw: str) -> dict:
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty llm output")
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", s, flags=re.S)
    if m:
        s = m.group(0)
    return json.loads(s)


def _sanitize_tags(tags) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = str(t).strip().strip("##，,。.")
        if not t or len(t) > 12 or t in out:
            continue
        out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def llm_tag_batch(items: list[dict], llm_call=None) -> dict[str, dict]:
    """一批文件一次 LLM 调用。返回 {rel: {tags, summary}}；整批失败由调用方降级。"""
    lines = []
    for it in items:
        brief = it.get("brief") or {}
        syms = "、".join(brief.get("symbols") or []) or "（无）"
        excerpt = (brief.get("text") or "").strip()
        lines.append(
            f"### 文件：{it['rel']}\n语言：{it.get('lang', '')}\n顶层符号：{syms}\n正文摘录：\n{excerpt}"
        )
    raw = (llm_call or _default_llm)(_LLM_INSTR + "\n\n" + "\n\n".join(lines))
    data = _parse_llm_json(raw)
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("llm output missing results")
    out: dict[str, dict] = {}
    for row in results:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").replace("\\", "/").strip()
        tags = _sanitize_tags(row.get("tags"))
        summary = str(row.get("summary") or "").strip()[:60]
        if path and tags:
            out[path] = {"tags": tags, "summary": summary}
    return out


# ---------------------------------------------------------------------------
# 增量标注
# ---------------------------------------------------------------------------
def _put_record(store: dict, item: dict, brief: dict, tags: list[str],
                summary: str, origin: str) -> dict:
    rec = {
        "mtime": item["mtime"],
        "size": item["size"],
        "tags": tags,
        "summary": summary,
        "symbols": (brief.get("symbols") or [])[:TOP_SYMBOLS],
        "origin": origin,
        "stale": False,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    store["files"][item["rel"]] = rec
    return rec


def ensure_tags(root: str, *, limit: int = DEFAULT_LIMIT, force: bool = False,
                llm_call=None, use_llm: bool = True) -> dict:
    """扫描代码库并给缺失/过期文件打标签（先 LLM 批量，失败文件降级规则）。

    - manual（人工改过）的记录永不覆盖，除非 force=True。
    - limit 控制本次最多标注数量，前端可重复点击直到 pending=0。
    """
    root = os.path.abspath(root)
    store = load_store(root)
    files = scan_code_files(root)
    pending = []
    for item in files:
        rec = store["files"].get(item["rel"])
        if rec is None:
            pending.append(item)
        elif rec.get("origin") == "manual" and not force:
            if not _is_fresh(rec, item["mtime"], item["size"]):
                rec["stale"] = True
            continue
        elif force or not _is_fresh(rec, item["mtime"], item["size"]):
            pending.append(item)

    chosen = pending[:max(0, limit)]
    briefs: dict[str, dict] = {}
    for item in chosen:
        briefs[item["rel"]] = _file_brief(item)

    n_llm = n_rules = 0
    if use_llm and chosen:
        # 分批调 LLM；任何一批失败，该批全部降级规则（不阻断整个刷新）
        for i in range(0, len(chosen), LLM_BATCH):
            batch = chosen[i:i + LLM_BATCH]
            got: dict[str, dict] = {}
            try:
                got = llm_tag_batch(
                    [{**it, "brief": briefs[it["rel"]]} for it in batch],
                    llm_call=llm_call,
                )
            except Exception:  # noqa: BLE001
                got = {}
            for it in batch:
                brief = briefs[it["rel"]]
                row = got.get(it["rel"])
                if row and row.get("tags"):
                    _put_record(store, it, brief, row["tags"],
                                row.get("summary") or "", "llm")
                    n_llm += 1
                else:
                    tags, summary = rule_tags(it["rel"], brief["text"], brief["symbols"])
                    _put_record(store, it, brief, tags, summary, "rules")
                    n_rules += 1
    else:
        for it in chosen:
            brief = briefs[it["rel"]]
            tags, summary = rule_tags(it["rel"], brief["text"], brief["symbols"])
            _put_record(store, it, brief, tags, summary, "rules")
            n_rules += 1

    # 清理磁盘上已不存在文件的记录
    live = {x["rel"] for x in files}
    dead = [p for p in store["files"] if p not in live]
    for p in dead:
        del store["files"][p]

    if chosen or dead:
        save_store(root, store)

    counts = {"llm": 0, "rules": 0, "manual": 0}
    stale = 0
    for rec in store["files"].values():
        counts[rec.get("origin", "rules")] = counts.get(rec.get("origin", "rules"), 0) + 1
        if rec.get("stale"):
            stale += 1
    return {
        "ok": True,
        "tagged_llm": n_llm,
        "tagged_rules": n_rules,
        "removed": len(dead),
        "total_files": len(files),
        "tagged_files": len(store["files"]),
        "pending": max(0, len(files) - len(store["files"])) + stale,
        "stale": stale,
        "counts": counts,
        "files": store["files"],
    }


def tag_status(root: str) -> dict:
    """只读状态：已有标签 + 待标注数量（不触发 LLM）。"""
    root = os.path.abspath(root)
    store = load_store(root)
    files = scan_code_files(root)
    live = {x["rel"] for x in files}
    counts = {"llm": 0, "rules": 0, "manual": 0}
    stale = 0
    fresh_tags: dict[str, dict] = {}
    for rel, rec in store["files"].items():
        if rel not in live:
            continue
        fresh_tags[rel] = rec
        counts[rec.get("origin", "rules")] = counts.get(rec.get("origin", "rules"), 0) + 1
        if rec.get("stale"):
            stale += 1
    missing = len(files) - len(fresh_tags) + stale
    return {
        "ok": True,
        "files": fresh_tags,
        "total_files": len(files),
        "tagged_files": len(fresh_tags),
        "pending": max(0, missing),
        "stale": stale,
        "counts": counts,
        "has_store": os.path.isfile(store_path(root)),
    }


def manual_update(root: str, rel: str, tags: list[str], summary: str = "") -> dict:
    """人工修正标签：origin=manual，后续自动刷新不覆盖。"""
    root = os.path.abspath(root)
    rel = rel.replace("\\", "/").strip().lstrip("/")
    tags = _sanitize_tags(tags)
    if not tags:
        raise ValueError("标签不能为空。")
    abs_path = os.path.join(root, *rel.split("/"))
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(rel)
    st = os.stat(abs_path)
    store = load_store(root)
    prev = store["files"].get(rel, {})
    brief_symbols = prev.get("symbols") or []
    if not brief_symbols:
        brief_symbols = _file_brief({"abs": abs_path}).get("symbols", [])
    store["files"][rel] = {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "tags": tags,
        "summary": summary.strip()[:60] or prev.get("summary", ""),
        "symbols": brief_symbols[:TOP_SYMBOLS],
        "origin": "manual",
        "stale": False,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_store(root, store)
    return {"ok": True, "path": rel, "record": store["files"][rel]}


# ---------------------------------------------------------------------------
# 写操作钩子（save/create/rename/delete 后调用，维护缓存新鲜度）
# ---------------------------------------------------------------------------
def invalidate_scan(root: str) -> None:
    _scan_cache.pop(os.path.abspath(root), None)


def on_saved(root: str, rel: str) -> None:
    """文件保存后：自动标签置为待刷新，人工标签保留并标 stale。无 store 时直接跳过。"""
    root = os.path.abspath(root)
    p = store_path(root)
    if not os.path.isfile(p):
        return
    store = load_store(root)
    rec = store["files"].get(rel)
    if not rec:
        return
    if rec.get("origin") == "manual":
        rec["stale"] = True
    else:
        del store["files"][rel]
    save_store(root, store)


def on_renamed(root: str, old_rel: str, new_rel: str) -> None:
    root = os.path.abspath(root)
    p = store_path(root)
    if not os.path.isfile(p):
        return
    store = load_store(root)
    rec = store["files"].pop(old_rel, None)
    if rec is not None:
        # 改名不改变内容：mtime 通常保留，记录原样跟随到新路径
        store["files"][new_rel] = rec
        save_store(root, store)


def on_deleted(root: str, rel: str) -> None:
    """文件或目录删除：清掉该路径及其所有子路径的标签记录。"""
    root = os.path.abspath(root)
    p = store_path(root)
    if not os.path.isfile(p):
        return
    store = load_store(root)
    prefix = rel.rstrip("/") + "/"
    dead = [k for k in store["files"] if k == rel or k.startswith(prefix)]
    if dead:
        for k in dead:
            del store["files"][k]
        save_store(root, store)


# ---------------------------------------------------------------------------
# 大白话定位：标签/路径本地匹配 + 向量检索混合
# ---------------------------------------------------------------------------
def _terms(q: str) -> list[str]:
    q = q.strip().lower()
    if not q:
        return []
    parts = [p for p in re.split(r"[\s,，、/]+", q) if p]
    # 中文常与英文连写（敌人AI / player_血量），把连续拉丁数字段也拆成独立词
    extra = re.findall(r"[a-z0-9_]{2,}", q)
    out = list(parts)
    for e in extra:
        if e not in out:
            out.append(e)
    return out or [q]


def _cjk_bigrams(s: str) -> set[str]:
    """连续中文段内的相邻二元字组：'玩家受伤' → {玩家,家受,受伤}。"""
    out: set[str] = set()
    for run in re.findall(r"[一-鿿]{2,}", s):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


def _fuzzy(term: str, field: str) -> int:
    """中文整句对短字段的模糊命中数：共享二元字数；单字查询按字包含计 1。"""
    if not term:
        return 0
    if term in field:
        return 99  # 整串直接包含，交给外层精确档
    qb, fb = _cjk_bigrams(term), _cjk_bigrams(field)
    shared = len(qb & fb)
    if shared:
        return shared
    # 单字中文查询（如「血」）
    cjk = "".join(re.findall(r"[一-鿿]", term))
    if len(cjk) == 1 and cjk in field:
        return 1
    return 0


def _local_score(terms: list[str], rel: str, rec: dict | None) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    name = rel.rsplit("/", 1)[-1].lower()
    rel_l = rel.lower()
    tags = [(t or "").lower() for t in (rec or {}).get("tags") or []]
    blob_tags = " ".join(tags)
    summary = ((rec or {}).get("summary", "") or "").lower()
    symbols = [s.lower() for s in ((rec or {}).get("symbols") or [])]

    def tag_hit(reason):
        nonlocal score
        score += 5
        if reason not in reasons:
            reasons.append(reason)

    for t in terms:
        # 精确/包含档
        if t in blob_tags:
            tag_hit("业务标签命中")
        else:
            # 中文 bigram 模糊档：命中 1 个共享二元字即给分，多个累加（上限 4.5）
            sh = min(4.5, sum(min(_fuzzy(t, tag), 2) for tag in tags) * 2.0)
            if sh > 0:
                score += sh
                if "业务标签命中" not in reasons:
                    reasons.append("业务标签命中")
        if t in summary:
            score += 3
            if "说明文字命中" not in reasons:
                reasons.append("说明文字命中")
        elif _fuzzy(t, summary) >= 2:
            score += 1.5
            if "说明文字命中" not in reasons:
                reasons.append("说明文字命中")
        if t in name:
            score += 4
            if "文件名命中" not in reasons:
                reasons.append("文件名命中")
        elif t in rel_l:
            score += 2
            if "路径命中" not in reasons:
                reasons.append("路径命中")
        sym_hits = sum(1 for s in symbols if t in s or _fuzzy(t, s) >= 1)
        if sym_hits:
            score += min(3.5, 1.5 * sym_hits + 1)
            if "函数名命中" not in reasons:
                reasons.append("函数名命中")
    return score, reasons


def locate(root: str, q: str, limit: int = 20, *, vector_search=None) -> dict:
    """混合定位：业务标签 + 文件名/符号 + 向量语义检索 + 分区名匹配。

    vector_search 可注入（测试用）：callable(query, k) -> chroma 风格结果 dict；
    默认懒加载 embeddings+vectorstore，不可用则静默降级为纯本地匹配。
    """
    root = os.path.abspath(root)
    q = (q or "").strip()
    terms = _terms(q)
    merged: dict[str, dict] = {}

    def bump(rel, add, reasons, line=None, symbol=None):
        row = merged.setdefault(rel, {
            "path": rel, "name": rel.rsplit("/", 1)[-1],
            "score": 0.0, "reasons": [], "line": None, "symbol": "",
        })
        row["score"] += add
        for r in reasons:
            if r not in row["reasons"]:
                row["reasons"].append(r)
        if line and (row["line"] is None or add > 4):
            row["line"] = line
        if symbol and not row["symbol"]:
            row["symbol"] = symbol

    # 1) 本地匹配（标签/摘要/文件名/路径/符号名）
    store = load_store(root)
    for item in scan_code_files(root):
        rel = item["rel"]
        rec = store["files"].get(rel)
        score, reasons = _local_score(terms, rel, rec)
        if score > 0:
            bump(rel, score, reasons)

    # 2) 向量语义检索（code chunks 集合）
    degraded = ""
    if terms:
        try:
            if vector_search is None:
                from embeddings import EmbeddingClient
                from vectorstore import query as vs_query
                from ingest import CODE_COLLECTION_NAME

                def vector_search(query, k):  # noqa: ANN202
                    emb = EmbeddingClient().embed([query])[0]
                    return vs_query(emb, k=k, collection=CODE_COLLECTION_NAME)

            res = vector_search(q, 25) or {}
            metas0 = (res.get("metadatas") or [[]])[0]
            seen_src: set = set()
            for idx, m in enumerate(metas0):
                src = str(m.get("source") or "").replace("\\", "/")
                if not src or src in seen_src:
                    continue
                seen_src.add(src)
                rank_score = max(0.5, 8.0 - idx * 0.45)
                bump(src, rank_score, ["语义向量检索"],
                     line=m.get("start_line"), symbol=m.get("symbol", ""))
        except Exception as e:  # noqa: BLE001
            degraded = f"向量检索不可用（{type(e).__name__}），已降级为标签/文件名匹配。"

    # 3) 合并标签元信息 + 分区归属
    # 向量集合可能残留上一个项目的 chunk（切换 code_root 后），只保留本项目
    # 扫描中真实存在的文件，杜绝跨项目串味/点不开的幻觉路径。
    live = {item["rel"] for item in scan_code_files(root)}
    from workbench_fs import _region_dirs, _region_of
    rdirs = _region_dirs(root)
    rows = []
    for rel, row in merged.items():
        if rel not in live:
            continue
        rec = store["files"].get(rel)
        rmeta = _region_of(rel, rdirs)
        row["region"] = rmeta["key"] if rmeta else ""
        row["region_name"] = rmeta["name"] if rmeta else ""
        row["tags"] = (rec or {}).get("tags") or []
        row["summary"] = (rec or {}).get("summary", "") or ""
        rows.append(row)
    rows.sort(key=lambda r: r["score"], reverse=True)
    rows = rows[:max(1, limit)]

    # 4) 分区本身命中（问「战斗分区在哪」也能直接给出分区文件夹）
    region_hits = []
    for d, meta in rdirs.items():
        blob = f"{meta.get('key','')} {meta.get('name','')} {meta.get('desc','')}".lower()
        if any(t and t in blob for t in terms):
            region_hits.append({
                "key": meta["key"], "name": meta["name"], "dir": meta["dir"],
                "desc": meta.get("desc", ""),
            })

    return {
        "ok": True,
        "query": q,
        "files": rows,
        "regions": region_hits,
        "total": len(rows),
        "degraded": degraded,
    }
