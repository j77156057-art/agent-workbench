"""交互式子代理派发：风险分档审批、独立代理索引与可追溯任务树。

同会话、同项目、depth 0→1、未扩权且未超限的直接子代理登记后立即执行；
跨层、跨会话/项目、扩权、超限或任何无法证明安全的派发都进入审批。
节点、ticket、TTL、批准人与时间按会话持久化；执行回调与 Agent 实例仅在内存中保存，
重启后由同会话 Agent 按持久化执行规格重建。普通工具权限与审批边界不受分档影响。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from file_locks import cross_process_lock

_LOCK = threading.RLock()
_ROOTS = {}
_DIR = Path(".docmind") / "delegations"

# 只有仍在占用算力的节点计入额度（总量 / 单父）。completed、failed、expired、
# rejected 以及重启后被标记为 unknown 的节点都不再占额度，否则长会话里每个跑完
# 的子代理都会永久挤占配额，最终把正常的直接派发全部推入审批。
_QUOTA_HOLDING_STATUSES = frozenset({"pending", "running", "waiting_child"})
#: 心跳字段都平铺在节点上，便于直接用 `.docmind/delegations/<sha>.json` 审计。
_HB_FIELDS = ("last_seen", "last_seen_epoch", "heartbeat_interval", "beats",
              "progress", "last_event", "recent_beats")
_RECENT_BEATS = 8


def _session_path(session):
    digest = hashlib.sha256(str(session or "default").encode("utf-8")).hexdigest()[:20]
    return _DIR / (digest + ".json")


def _iso(epoch=None):
    value = time.time() if epoch is None else epoch
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")


def _read_payload(path):
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _atomic_write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n"
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_nodes(session, nodes, alerts=None):
    """原子落盘；并从磁盘合并更新的心跳字段，避免并发 beat 被旧内存快照覆盖。"""
    session = str(session or "default")
    path = _session_path(session)
    with cross_process_lock(path, timeout=10):
        old = _read_payload(path) or {}
        old_nodes = {str(row.get("id")): row for row in old.get("nodes") or [] if row.get("id")}
        merged = []
        for node in nodes:
            row = dict(node)
            previous = old_nodes.get(str(row.get("id")))
            if previous and float(previous.get("last_seen_epoch") or 0) > float(
                    row.get("last_seen_epoch") or 0):
                for key in _HB_FIELDS:
                    if key in previous:
                        row[key] = previous[key]
            merged.append(row)
        old_alerts = list(old.get("alerts") or [])
        current_alerts = list(alerts or [])
        seen = {(item.get("agent_id"), item.get("last_seen")) for item in current_alerts}
        for item in old_alerts:
            key = (item.get("agent_id"), item.get("last_seen"))
            if key not in seen:
                current_alerts.append(item)
                seen.add(key)
        _atomic_write(path, {"schema": 1, "session_id": session,
                             "nodes": merged, "alerts": current_alerts[-200:]})


def _persist(session):
    root = _ROOTS.get(str(session or "default"))
    if root is not None:
        _write_nodes(session, list(root["nodes"].values()), root.get("alerts") or [])


def _load(session):
    path = _session_path(session)
    with cross_process_lock(path, timeout=10):
        if not path.is_file():
            return {"nodes": {}, "callbacks": {}, "agents": {}, "alerts": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"nodes": {}, "callbacks": {}, "agents": {}, "alerts": []}
    nodes = {str(row["id"]): row for row in payload.get("nodes") or [] if row.get("id")}
    interrupted = False
    for node in nodes.values():
        if node.get("status") == "running":
            if node.get("last_seen_epoch"):
                # 有真实心跳：状态保留 running，由心跳判定 alive/stale/dead。
                # 这样跨进程/重启后的读路径也能看到失联迁移，而不是一律抹成 unknown。
                node["resume_note"] = "进程重载：以心跳判定存活；结果未知，未自动重跑"
            else:
                node["status"] = "unknown"
                node["error"] = "进程在派发执行期间退出；结果未知，未自动重跑"
                interrupted = True
    root = {"nodes": nodes, "callbacks": {}, "agents": {},
            "alerts": list(payload.get("alerts") or [])}
    if interrupted:
        _ROOTS[str(session or "default")] = root
        _persist(session)
    return root


def _int_env(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def limits():
    return {
        "max_depth": _int_env("DOCMIND_DELEGATION_MAX_DEPTH", 4),
        "max_branches": _int_env("DOCMIND_DELEGATION_MAX_BRANCHES", 12),
        "max_children": _int_env("DOCMIND_DELEGATION_MAX_CHILDREN", 4),
    }


def approval_ttl():
    """strict/model 的 ticket 一次性消费；user 档复用 30 分钟有效期。"""
    raw = os.getenv("DOCMIND_DELEGATION_TTL_SECONDS")
    if raw is not None:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0
    try:
        from config import DOCMIND_DESKTOP_APPROVAL
        return 1800 if DOCMIND_DESKTOP_APPROVAL == "user" else 0
    except Exception:  # noqa: BLE001 - 配置不可读时按一次性 strict 处理
        return 0


def _float_env(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def heartbeat_config():
    """三档失联阈值：一律按 heartbeat_interval 的倍数判定，不用绝对毫秒。

    默认值来源与校准状态必须显式暴露给视图，避免把未校准的拍脑袋阈值
    伪装成经过真实任务验证的参数。
    """
    interval = _float_env("DOCMIND_HEARTBEAT_INTERVAL_S", 15.0, 1.0)
    stale_factor = _float_env("DOCMIND_HEARTBEAT_STALE_FACTOR", 3.0, 1.0)
    dead_factor = max(stale_factor + 0.1,
                      _float_env("DOCMIND_HEARTBEAT_DEAD_FACTOR", 6.0, 1.0))
    progress_factor = _float_env("DOCMIND_HEARTBEAT_PROGRESS_FREEZE_FACTOR", 3.0, 1.0)
    return {
        "interval_seconds": interval,
        "stale_factor": stale_factor,
        "dead_factor": dead_factor,
        "progress_freeze_factor": progress_factor,
        "stale_after_seconds": round(interval * stale_factor, 3),
        "dead_after_seconds": round(interval * dead_factor, 3),
        "calibrated": False,
        "calibration_note": "默认阈值未经真实任务校准：15s 间隔 / 3×stale / 6×dead 仅按本机演示任务观察取值。",
        "source": "agent_runtime/delegation.py 默认值；DOCMIND_HEARTBEAT_INTERVAL_S / "
                  "DOCMIND_HEARTBEAT_STALE_FACTOR / DOCMIND_HEARTBEAT_DEAD_FACTOR / "
                  "DOCMIND_HEARTBEAT_PROGRESS_FREEZE_FACTOR 可覆盖",
    }


def _seed_beat(row, now, *, phase="dispatched", event="dispatch", steps=0):
    row["last_seen"] = _iso(now)
    row["last_seen_epoch"] = now
    row["heartbeat_interval"] = heartbeat_config()["interval_seconds"]
    row["beats"] = 1
    signature = json.dumps({"steps": int(steps), "phase": str(phase)}, sort_keys=True)
    row["progress"] = {"steps": int(steps), "phase": str(phase), "signature": signature,
                       "changed_at": _iso(now), "changed_at_epoch": now}
    row["last_event"] = str(event or "")[:120]
    row["recent_beats"] = [{"at": _iso(now), "epoch": now, "steps": int(steps),
                            "phase": str(phase), "event": str(event or "")[:80]}]


def _apply_beat(row, now, *, progress=None, phase="", event=""):
    """只由真实执行事件调用：派发、工具返回、状态迁移、回合收尾。"""
    interval = heartbeat_config()["interval_seconds"]
    row["last_seen"] = _iso(now)
    row["last_seen_epoch"] = now
    row["heartbeat_interval"] = interval
    try:
        row["beats"] = int(row.get("beats") or 0) + 1
    except (TypeError, ValueError):
        row["beats"] = 1
    previous = row.get("progress") if isinstance(row.get("progress"), dict) else {}
    steps = previous.get("steps", 0)
    if isinstance(progress, dict) and progress.get("steps") is not None:
        try:
            steps = int(progress.get("steps"))
        except (TypeError, ValueError):
            pass
    next_phase = str(phase or (progress or {}).get("phase") or previous.get("phase") or "")
    signature = json.dumps({"steps": steps, "phase": next_phase}, sort_keys=True)
    if signature != previous.get("signature"):
        row["progress"] = {"steps": steps, "phase": next_phase, "signature": signature,
                           "changed_at": _iso(now), "changed_at_epoch": now}
    else:
        kept = dict(previous)
        kept.update({"steps": steps, "phase": next_phase, "signature": signature})
        row["progress"] = kept
    row["last_event"] = str(event or "")[:120]
    log = list(row.get("recent_beats") or [])
    log.append({"at": _iso(now), "epoch": now, "steps": steps,
                "phase": next_phase, "event": str(event or "")[:80]})
    row["recent_beats"] = log[-_RECENT_BEATS:]


def heartbeat_view(row, now=None, config=None):
    """从真实时间戳派生状态；没有 last_seen 时如实返回 none，不补 0 秒。"""
    cfg = config or heartbeat_config()
    now = time.time() if now is None else now
    try:
        last = float(row.get("last_seen_epoch") or 0)
    except (TypeError, ValueError):
        last = 0.0
    progress = row.get("progress") if isinstance(row.get("progress"), dict) else {}
    base = {
        "last_seen": row.get("last_seen") or None,
        "beats": int(row.get("beats") or 0),
        "progress": progress or None,
        "recent_beats": list(row.get("recent_beats") or []),
        "last_event": row.get("last_event") or "",
    }
    if last <= 0:
        return {**base, "state": "none", "silence_seconds": None,
                "progress_age_seconds": None, "progress_frozen": False,
                "reason": "无心跳记录"}
    # 阈值一律用当前生效配置，历史节点的 heartbeat_interval 只是当时的记录值；
    # 否则改环境变量后旧节点仍按旧间隔判定，"阈值可配"就是假的。
    interval = float(cfg["interval_seconds"])
    silence = max(0.0, now - last)
    state = "alive"
    threshold = ""
    if silence > interval * cfg["dead_factor"]:
        state = "dead"
        threshold = f"{silence:.1f}s > {interval:g}s×{cfg['dead_factor']:g}"
    elif silence > interval * cfg["stale_factor"]:
        state = "stale"
        threshold = f"{silence:.1f}s > {interval:g}s×{cfg['stale_factor']:g}"
    else:
        threshold = f"{silence:.1f}s ≤ {interval:g}s×{cfg['stale_factor']:g}"
    status = str(row.get("status") or "")
    if status in {"completed", "failed", "rejected", "expired", "unknown"}:
        state = "closed"
    elif status == "pending":
        state = "waiting"
    try:
        changed = float(progress.get("changed_at_epoch") or 0)
    except (TypeError, ValueError):
        changed = 0.0
    progress_age = max(0.0, now - changed) if changed > 0 else None
    progress_frozen = bool(progress_age is not None
                           and progress_age >= interval * cfg["progress_freeze_factor"])
    if state in {"closed", "waiting", "none"}:
        progress_frozen = False
    return {
        **base,
        "state": state,
        "silence_seconds": round(silence, 3),
        "interval_seconds": interval,
        "progress_age_seconds": None if progress_age is None else round(progress_age, 3),
        "progress_frozen": progress_frozen,
        "reason": ("已结束，不再期待心跳" if state == "closed"
                   else "等待审批，尚未开始执行" if state == "waiting"
                   else ("时钟在走但进度不动：" if progress_frozen else "") + threshold),
    }


def _update_payload(session, mutate, retries=4):
    """跨进程锁内读-改-写；撞锁时退避重试，绝不丢已落盘的更新。"""
    path = _session_path(session)
    last_error = None
    for attempt in range(retries):
        try:
            with cross_process_lock(path, timeout=10):
                payload = _read_payload(path)
                if payload is None:
                    payload = {"schema": 1, "session_id": str(session or "default"),
                               "nodes": [], "alerts": []}
                if mutate(payload) is False:
                    return False
                _atomic_write(path, payload)
            return True
        except TimeoutError as exc:  # 撞锁：退避后重试，不静默丢更新
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error
    return False


def beat(session, node_id, *, progress=None, phase="", event="", create=False):
    """执行侧打点。node_id 不存在时只有显式 create（main 根）才创建。"""
    session = str(session or "default")
    node_id = str(node_id or "")
    if not node_id:
        return False
    now = time.time()
    saved = []

    def mutate(payload):
        rows = payload.setdefault("nodes", [])
        row = next((item for item in rows if str(item.get("id")) == node_id), None)
        if row is None:
            if not create and node_id != "main":
                return False
            row = {
                "id": node_id, "ticket_id": "", "session_id": session, "parent_id": "",
                "project_id": "", "role": "main", "plan_id": "", "task_id": "",
                "task": "平台助手（主 Agent）", "depth": 0, "status": "active",
                "created_at": _iso(now), "created_at_epoch": now,
                "approval_required": False, "result": None, "error": "",
            }
            rows.append(row)
        _apply_beat(row, now, progress=progress, phase=phase, event=event)
        saved.append(dict(row))
        return True

    if not _update_payload(session, mutate):
        return False
    if saved:
        with _LOCK:
            root = _ROOTS.get(session)
            if root is None:
                root = _load(session)
                _ROOTS[session] = root
            target = root["nodes"].get(node_id)
            if target is None:
                root["nodes"][node_id] = dict(saved[0])
            else:
                # 原位更新，不换 dict：执行侧可能在回调前就持有该节点引用。
                target.update(saved[0])
    return True


def _append_alerts(session, new_alerts):
    """失联接收端：写入 delegation 快照 alerts[]，供平台助手读路径展示。"""
    if not new_alerts:
        return []
    added = []

    def mutate(payload):
        alerts = payload.setdefault("alerts", [])
        keys = {(item.get("agent_id"), item.get("last_seen")) for item in alerts}
        for item in new_alerts:
            key = (item.get("agent_id"), item.get("last_seen"))
            if key not in keys:
                alerts.append(item)
                added.append(item)
                keys.add(key)
        alerts[:] = alerts[-200:]
        return True

    _update_payload(session, mutate)
    if added:
        with _LOCK:
            root = _ROOTS.get(session)
            if root is not None:
                existing = root.setdefault("alerts", [])
                seen = {(item.get("agent_id"), item.get("last_seen")) for item in existing}
                for item in added:
                    if (item.get("agent_id"), item.get("last_seen")) not in seen:
                        existing.append(item)
    return added


def _root(session):
    session = str(session or "default")
    with _LOCK:
        if session not in _ROOTS:
            _ROOTS[session] = _load(session)
        return _ROOTS[session]


def _refresh(session):
    """读路径合并磁盘快照：执行侧（可能是另一个进程）写入的心跳必须对 UI 可见。

    `_ROOTS` 只在首次读时从磁盘加载一次；若不合并，一次早于节点创建的空快照会
    永久遮蔽之后另一个进程写入的 beats/last_seen（实测截图脚本在独立进程里跑子
    代理，API 进程的树里始终没有子节点）。这里按 last_seen_epoch 取较新者，
    只读路径调用，不碰 callbacks/agents。
    """
    session = str(session or "default")
    with _LOCK:
        root = _ROOTS.get(session)
        if root is None:
            root = _load(session)
            _ROOTS[session] = root
            return root
        payload = _read_payload(_session_path(session))
        if not payload:
            return root
        for row in payload.get("nodes") or []:
            node_id = str(row.get("id") or "")
            if not node_id:
                continue
            current = root["nodes"].get(node_id)
            if current is None:
                root["nodes"][node_id] = dict(row)
                continue
            try:
                disk_seen = float(row.get("last_seen_epoch") or 0)
                mem_seen = float(current.get("last_seen_epoch") or 0)
            except (TypeError, ValueError):
                disk_seen = mem_seen = 0.0
            if disk_seen >= mem_seen:
                current.update(row)
        alerts = list(root.get("alerts") or [])
        seen = {(item.get("agent_id"), item.get("last_seen")) for item in alerts}
        for item in payload.get("alerts") or []:
            key = (item.get("agent_id"), item.get("last_seen"))
            if key not in seen:
                alerts.append(item)
                seen.add(key)
        root["alerts"] = alerts[-200:]
        return root


def _public(node):
    return {key: value for key, value in node.items() if not key.startswith("_")}


def resolve_tools(parent, *, default_tools, tool_allowlist,
                  mcp_policy, read_only, interactive_session):
    """返回实际生效的工具与 MCP 策略，风险分类和执行共用同一份结果。"""
    from .tools import tool_mutates

    requested = tool_allowlist
    provided = requested is not None
    if isinstance(requested, str):
        requested = [item.strip() for item in requested.split(",") if item.strip()]
    elif provided and not isinstance(requested, (list, tuple, set)):
        requested = []
    if isinstance(requested, (list, tuple, set)) and requested:
        allowed = [str(name) for name in requested if str(name) in parent.tools]
        if read_only:
            allowed = [name for name in allowed
                       if not tool_mutates(parent._tool_spec(name))]
    elif not provided:
        allowed = list(default_tools)
    else:
        allowed = []
    policy = str(mcp_policy or "auto").strip().lower()
    if read_only:
        policy = "deny"
    mcp_tools = [name for name in (
        "dev_route_connector", "dev_list_connector_tools", "dev_mcp_call")
        if name in parent.tools]
    if policy == "deny":
        allowed = [name for name in allowed if name not in mcp_tools]
    elif policy == "allow" and not provided:
        allowed = list(dict.fromkeys(allowed + mcp_tools))
    allowed = [name for name in allowed if name in parent.tools]
    if interactive_session:
        allowed = list(dict.fromkeys(allowed + [
            name for name in ("delegate", "orchestrate") if name in parent.tools]))
    return {"requested_tools": requested, "allowlist_provided": provided,
            "allowed": allowed, "policy": policy}


def classify(parent, *, session_id, project_id, depth, effective_tools,
             total, siblings):
    """集中判定派发风险；任何无法证明安全的条件都返回需要审批。"""
    reasons = []
    parent_session = str(getattr(parent, "memory_session_id", None)
                         or getattr(parent, "session_id", "") or "default")
    if not session_id or str(session_id) != parent_session:
        reasons.append("跨会话或会话归属无法证明")
    parent_project = str(getattr(parent, "project_id", "") or "")
    if str(project_id or "") != parent_project:
        reasons.append("跨项目派发")
    if depth != 1:
        reasons.append("跨层递归派发")

    caps = limits()
    limit_reasons = []
    if depth > caps["max_depth"]:
        limit_reasons.append(f"深度 {depth} 超过默认上限 {caps['max_depth']}")
    if total >= caps["max_branches"]:
        limit_reasons.append(f"总分支 {total + 1} 超过默认上限 {caps['max_branches']}")
    if siblings >= caps["max_children"]:
        limit_reasons.append(f"单父分支 {siblings + 1} 超过默认上限 {caps['max_children']}")
    reasons.extend(limit_reasons)

    lease = getattr(parent, "capability_lease", None)
    if lease:
        if not isinstance(lease, dict):
            reasons.append("现有能力租约格式无效")
        else:
            try:
                expires = float(lease.get("expires_at_epoch") or 0)
            except (TypeError, ValueError):
                expires = 0
            if lease.get("status", "active") != "active" or expires <= time.time():
                reasons.append("现有能力租约已失效")
            else:
                allowed_capabilities = {str(item) for item in (lease.get("capabilities") or [])}
                for name in effective_tools:
                    try:
                        spec = parent._tool_spec(name)
                        if spec is None:
                            reasons.append(f"工具 {name} 的能力无法证明")
                            continue
                        capability = getattr(spec.capability, "value", str(spec.capability))
                        if capability not in allowed_capabilities:
                            reasons.append(f"工具 {name} 需要未租用的 {capability} 能力")
                    except Exception:  # noqa: BLE001 - 分类失败必须 fail-closed
                        reasons.append(f"工具 {name} 的能力无法证明")
    return {
        "approval_required": bool(reasons),
        "approval_reasons": list(dict.fromkeys(reasons)),
        "requires_limit_confirmation": bool(limit_reasons),
        "limit_reasons": limit_reasons,
    }


def request(parent, *, role, task, tool_allowlist=None, run,
            session_id="", project_id="", depth=None, effective_tools=None,
            plan_id="", task_id="", execution=None):
    """登记派发并占用额度；安全直接节点从 running 开始，其余从 pending 开始。"""
    session = str(session_id or getattr(parent, "memory_session_id", None)
                  or getattr(parent, "session_id", "") or "default")
    parent_id = str(getattr(parent, "agent_id", "main") or "main")
    if depth is None:
        depth = int(getattr(parent, "depth", 0)) + 1
    effective = [str(name) for name in (effective_tools if effective_tools is not None
                                        else tool_allowlist or []) if str(name)]
    tools = effective[:20]
    node_id = "ag-" + uuid.uuid4().hex[:12]
    with _LOCK:
        root = _root(session)
        siblings = len([node for node in root["nodes"].values()
                        if node["parent_id"] == parent_id
                        and node["status"] in _QUOTA_HOLDING_STATUSES])
        total = len([node for node in root["nodes"].values()
                     if node["status"] in _QUOTA_HOLDING_STATUSES])
        risk = classify(parent, session_id=session, project_id=project_id,
                        depth=depth, effective_tools=effective, total=total, siblings=siblings)
        now_epoch = time.time()
        ttl = approval_ttl()
        node = {
            "id": node_id, "ticket_id": node_id, "session_id": session, "parent_id": parent_id,
            "project_id": str(project_id or ""), "role": str(role or ""),
            "plan_id": str(plan_id or ""), "task_id": str(task_id or ""),
            "task": str(task or ""), "depth": depth,
            "status": "pending" if risk["approval_required"] else "running",
            "created_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "created_at_epoch": now_epoch, "ttl_seconds": ttl,
            "expires_at_epoch": (now_epoch + ttl) if ttl > 0 else 0,
            "strict_one_shot": ttl == 0, "approved_by": "", "decided_at": "", "decision": "",
            **risk,
            "execution": dict(execution or {}),
            "impact": {"tools": tools, "independent_context": True,
                       "estimated": "将启动 1 个独立上下文子代理；其再次派发仍需单独审批"},
            "result": None, "error": "",
        }
        _seed_beat(node, now_epoch, phase="dispatched", event="delegation.request", steps=0)
        root["nodes"][node_id] = node
        root["callbacks"][node_id] = run
        _persist(session)
    return _public(node)


def register_agent(node_id, agent):
    with _LOCK:
        for root in _ROOTS.values():
            if node_id in root["nodes"]:
                root["agents"][node_id] = agent
                return


def agent_for(session, node_id):
    with _LOCK:
        return _root(session).get("agents", {}).get(str(node_id or ""))


def get(session, node_id):
    with _LOCK:
        node = _root(session).get("nodes", {}).get(str(node_id or ""))
        return _public(node) if node else None


def latest_pending(session, parent_id):
    with _LOCK:
        rows = [node for node in _root(session)["nodes"].values()
                if node["parent_id"] == str(parent_id or "") and node["status"] == "pending"]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return _public(rows[0]) if rows else None


def all_nodes():
    """跨会话读取所有持久化的委派节点（只读，供 Agent 卡片墙使用）。

    不依赖内存中的 root/callbacks：后端重启后卡片仍然能列出历史 agent。
    """
    rows = []
    for path in sorted(_DIR.glob("*.json")):
        payload = _read_payload(path)
        if not payload:
            continue
        session = str(payload.get("session_id") or "")
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict) or str(node.get("id") or "") in {"", "main"}:
                continue
            item = _public(node)
            item["session_id"] = str(item.get("session_id") or session)
            rows.append(item)
    rows.sort(key=lambda row: str(row.get("last_seen") or row.get("created_at") or ""),
              reverse=True)
    return rows


def tree(session):
    session = str(session or "default")
    config = heartbeat_config()
    now = time.time()
    with _LOCK:
        root = _refresh(session)
        stored_main = root["nodes"].get("main")
        # 根节点 "main" 只以心跳记录的形式落盘；合成树时并回合成根，避免重复节点。
        nodes = [_public(node) for key, node in root["nodes"].items() if key != "main"]
        alerts = list(root.get("alerts") or [])
    main = {"id": "main", "session_id": session, "parent_id": "", "role": "main",
            "task": "主 Agent", "depth": 0, "status": "active", "children": []}
    if stored_main:
        for key in _HB_FIELDS:
            if key in stored_main:
                main[key] = stored_main[key]
        main["status"] = str(stored_main.get("status") or "active")
        main["heartbeat"] = heartbeat_view(stored_main, now, config)
    for node in nodes:
        node["heartbeat"] = heartbeat_view(node, now, config)
    # 失联接收端：读路径扫描 running/waiting 节点，dead 时写入结构化告警。
    existing = {(item.get("agent_id"), item.get("last_seen")) for item in alerts}
    new_alerts = []
    for node in nodes:
        if node.get("status") not in {"running", "active", "waiting_child"}:
            continue
        heartbeat = node.get("heartbeat") or {}
        if heartbeat.get("state") != "dead":
            continue
        key = (node.get("id"), heartbeat.get("last_seen"))
        if key in existing:
            continue
        new_alerts.append({
            "agent_id": node.get("id"),
            "role": node.get("role"),
            "last_seen": heartbeat.get("last_seen"),
            "silence_seconds": heartbeat.get("silence_seconds"),
            "progress_frozen": bool(heartbeat.get("progress_frozen")),
            "progress": heartbeat.get("progress"),
            "detected_at": _iso(now),
            "received_by": "platform_assistant",
            "receiver": "GET /api/agent/delegations → delegation.tree() 读路径",
            "delivery": ".docmind/delegations/<session>.json → alerts[]",
        })
    if new_alerts:
        _append_alerts(session, new_alerts)
        alerts.extend(new_alerts)
    alerts.sort(key=lambda item: str(item.get("detected_at") or ""), reverse=True)
    by_id = {node["id"]: {**node, "children": []} for node in nodes}
    by_id["main"] = main
    roots = []
    for node in by_id.values():
        if node["id"] == "main":
            continue
        parent = by_id.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    main["children"].sort(key=lambda row: row["created_at"])
    for node in by_id.values():
        node["children"].sort(key=lambda row: row.get("created_at", ""))
    return {"ok": True, "session_id": session, "root": main,
            "nodes": [main] + nodes, "pending": [node for node in nodes if node["status"] == "pending"],
            "alerts": alerts,
            "heartbeat_config": config,
            "limits": limits(),
            "config": {"DOCMIND_DELEGATION_MAX_DEPTH": limits()["max_depth"],
                       "DOCMIND_DELEGATION_MAX_BRANCHES": limits()["max_branches"],
                       "DOCMIND_DELEGATION_MAX_CHILDREN": limits()["max_children"]}}


def _execute(session, node_id):
    """执行 running 节点并回写结果，供直接派发与审批派发共用。"""
    session = str(session or "default")
    infrastructure_error = ""
    with _LOCK:
        root = _root(session)
        node = root["nodes"].get(str(node_id or ""))
        if not node:
            return {"status": "failed", "conclusion": "", "error": "未找到该会话的派发节点"}, "未找到该会话的派发节点"
        if node["status"] != "running":
            error = f"当前状态 {node['status']} 不可执行"
            return {"status": "failed", "conclusion": "", "error": error}, error
        callback = root["callbacks"].get(node_id)
    if callback is None:
        result = {"status": "failed", "conclusion": "", "error": "派发回调已失效；未执行子代理",
                  "completed": [], "resume_point": "可换策略或拆小后重新派发"}
        infrastructure_error = result["error"]
    else:
        try:
            result = callback() or {}
            if not isinstance(result, dict):
                raise TypeError("子代理结果必须是 dict")
        except Exception as exc:  # noqa: BLE001 —— agent 自身异常也必须进入任务树
            infrastructure_error = f"{type(exc).__name__}: {exc}"
            result = {"status": "failed", "conclusion": "", "error": infrastructure_error,
                      "completed": [], "resume_point": "可换策略或拆小后重新派发"}
    need = result.get("needs_confirmation") or {}
    status = ("waiting_child" if need.get("kind") == "delegation"
              else "completed" if result.get("status") == "ok" else "failed")
    with _LOCK:
        node["status"] = status
        node["result"] = result
        node["error"] = str(result.get("error") or "")
        _apply_beat(node, time.time(), phase=status, event="status:" + status,
                    progress={"steps": result.get("steps")})
        _persist(session)
    return result, infrastructure_error


def execute(session, node_id):
    """直接执行一个已登记且无需审批的 running 节点。"""
    result, _error = _execute(session, node_id)
    return result


def _restore_callback(session, node_id, resume_agent):
    """重启后按持久化规格重建执行回调；不依赖旧进程内存闭包。"""
    if resume_agent is None:
        return
    root = _root(session)
    node = root["nodes"].get(str(node_id or ""))
    if not node or root["callbacks"].get(node_id) is not None:
        return
    spec = node.get("execution") or {}
    tools = list((node.get("impact") or {}).get("tools") or []) or None
    root["callbacks"][node_id] = lambda: resume_agent._run_child(
        node.get("role"), node.get("task"), context=spec.get("context") or {},
        persona=spec.get("persona", ""), tool_allowlist=tools,
        mcp_policy=spec.get("mcp_policy", "auto"), reflect=bool(spec.get("reflect", True)),
        max_steps=spec.get("max_steps"), _delegation_ticket=node_id,
        _plan_id=node.get("plan_id", ""), _task_id=node.get("task_id", ""))


def _resume_plan(node, resume_agent):
    plan_id = str(node.get("plan_id") or "")
    if not plan_id:
        return None
    from . import orchestration
    return orchestration.resume(plan_id, node, agent=resume_agent)


def decide(session, node_id, approved, resume_agent=None, actor=""):
    """批准/拒绝后恢复原 DAG；过期、重复决定和重启回调都显式处理。"""
    session = str(session or "default")
    actor = str(actor or "local-user")
    with _LOCK:
        root = _root(session)
        node = root["nodes"].get(str(node_id or ""))
        if not node:
            return {"ok": False, "error": "未找到该会话的派发审批"}
        if node["status"] in {"unknown", "expired"}:
            return {"ok": False, "error": str(node.get("error") or "审批 ticket 不可执行"),
                    "expired": node["status"] == "expired", "item": _public(node)}
        expires = float(node.get("expires_at_epoch") or 0)
        if node["status"] == "pending" and expires > 0 and expires <= time.time():
            node["status"] = "expired"
            node["decision"] = "expired"
            node["decided_at"] = datetime.now(timezone.utc).isoformat()
            node["approved_by"] = actor
            node["error"] = "审批 ticket 已过期；未执行子代理"
            node["result"] = {"status": "expired", "conclusion": "", "error": node["error"]}
            _apply_beat(node, time.time(), phase="expired", event="status:expired")
            _persist(session)
            return {"ok": False, "expired": True, "error": node["error"], "item": _public(node)}
        if node["status"] != "pending":
            was_rejected = node["status"] == "rejected"
            if bool(approved) == (not was_rejected):
                response = {"ok": True, "approved": bool(approved), "idempotent": True,
                            "item": _public(node)}
                if node.get("plan_id"):
                    from . import orchestration
                    response["plan"] = orchestration.get(node["plan_id"])
                return response
            return {"ok": False, "error": f"当前状态 {node['status']} 与本次决定冲突",
                    "item": _public(node)}
        node["approved_by"] = actor
        node["decided_at"] = datetime.now(timezone.utc).isoformat()
        node["decision"] = "approved" if approved else "rejected"
        if not approved:
            node["status"] = "rejected"
            node["error"] = "用户拒绝派发；该分支未执行"
            node["result"] = {"status": "rejected", "conclusion": "",
                              "error": node["error"], "completed": [], "resume_point": "可重新发起派发"}
            _apply_beat(node, time.time(), phase="rejected", event="status:rejected")
        else:
            # strict TTL=0：ticket 在执行前先被消费；回调完成后也不恢复成可审批状态。
            node["status"] = "running"
            _apply_beat(node, time.time(), phase="approved", event="status:approved")
            _restore_callback(session, node_id, resume_agent)
        _persist(session)
    if not approved:
        response = {"ok": True, "approved": False, "item": _public(node)}
        resumed = _resume_plan(node, resume_agent)
        if resumed:
            response.update(resumed)
        return response
    result, infrastructure_error = _execute(session, node_id)
    response = {"ok": not infrastructure_error, "approved": True,
                "error": infrastructure_error, "item": _public(node)}
    if node.get("plan_id"):
        resumed = _resume_plan(node, resume_agent)
        if resumed:
            response.update(resumed)
    return response


def clear(session=None):
    """测试隔离用；生产代码不调用。同步清理对应持久化记录。"""
    with _LOCK:
        if session is None:
            _ROOTS.clear()
            if _DIR.is_dir():
                for path in _DIR.glob("*.json"):
                    path.unlink(missing_ok=True)
        else:
            _ROOTS.pop(str(session), None)
            _session_path(session).unlink(missing_ok=True)
