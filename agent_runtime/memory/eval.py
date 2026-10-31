"""记忆专用评测：检索命中、去重、晋升门与项目隔离的可重复基准。

不依赖业务数据；每次在临时目录里种一组带标准答案的记忆，跑完自动清理。
语义模式需要可用的 embedding 后端（默认尝试本机 bge-m3），不可用时仍会给出
关键词基线的结果并明确标注 semantic_effective=false。
"""
from __future__ import annotations

import os
import shutil
import tempfile

from config import get_runtime, set_runtime

from . import index, promote

FACTS = [
    ("部署前先跑完整测试，能降低回归率", "部署 测试"),
    ("日志排查先看退出码，再决定是否重跑", "日志 退出码"),
    ("用户偏好：回答保持简洁，不要长篇大论", "回答 简洁"),
    ("Git 提交前必须核对暂存区范围", "提交 暂存区"),
    ("数据库迁移前先备份", "数据库 备份"),
    ("前端改动后跑一次视觉回归", "前端 视觉回归"),
]
DISTRACTORS = [
    "部署后无需测试，直接上线",
    "日志只记录成功请求",
    "回答越长越好",
    "提交可以跳过暂存区核对",
    "数据库迁移不需要备份",
    "前端改动无需回归",
]
QUERIES = [
    ("部署前要不要跑测试", "部署前先跑完整测试，能降低回归率"),
    ("日志排查先看什么", "日志排查先看退出码，再决定是否重跑"),
    ("回答风格有什么偏好", "用户偏好：回答保持简洁，不要长篇大论"),
    ("提交前核对什么", "Git 提交前必须核对暂存区范围"),
    ("数据库迁移前要做什么", "数据库迁移前先备份"),
    ("上线前是否要验证", "部署前先跑完整测试，能降低回归率"),
]


def _check(checks, name, condition):
    checks[name] = bool(condition)


def run(semantic=False):
    old_dir = os.environ.get("DOCMIND_MEMORY_DIR")
    runtime_keys = ("memory_semantic", "memory_embed",
                    "embedding_provider", "embedding_model")
    old_runtime = {key: get_runtime(key) for key in runtime_keys}
    tmp = tempfile.mkdtemp(prefix="dm_memory_eval_")
    os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(tmp, "memory")
    try:
        set_runtime("memory_semantic", bool(semantic))
        set_runtime("memory_embed", False)
        if semantic:
            set_runtime("embedding_provider", "ollama")
            set_runtime("embedding_model", "bge-m3")
        index._EMBED_CLIENT = None
        index._EMBED_FAILED = False

        seed_ids = {}
        for text, _keywords in FACTS:
            mid = promote.seed_shared("fact", text, importance=0.7, confidence=0.8,
                                      status="active")
            seed_ids[text] = mid
        for text in DISTRACTORS:
            promote.seed_shared("fact", text, importance=0.4, confidence=0.5,
                                status="active")
        index.rebuild(verify=True)

        hits = 0
        rr_sum = 0.0
        per_query = []
        semantic_seen = False
        for query, expected in QUERIES:
            rows = index.search(query, limit=3)
            ids = [row.get("id") for row in rows]
            expected_id = seed_ids.get(expected)
            if any(float(row.get("_semantic") or 0) > 0 for row in rows):
                semantic_seen = True
            if expected_id and expected_id in ids:
                hits += 1
                rr_sum += 1.0 / (ids.index(expected_id) + 1)
            per_query.append({"query": query, "expected": expected,
                              "hit": bool(expected_id and expected_id in ids),
                              "top": [row.get("title", "")[:30] for row in rows]})
        recall = hits / len(QUERIES)
        mrr = rr_sum / len(QUERIES)

        checks = {}
        candidate = promote.create_candidate(
            "prj-eval", "fact", "无回执的候选：先复现再修复，减少盲目改动", importance=0.8)
        _check(checks, "receipt_gate",
               not promote.promote("prj-eval", candidate["id"])["ok"])

        adopted = promote.create_candidate(
            "prj-eval", "fact", "用户采纳后应晋升：先写测试再合并", importance=0.8)
        _check(checks, "adopt_promotes",
               bool(promote.adopt("prj-eval", adopted["id"]).get("promoted")))

        promote.seed_project("prj-eval", "fact", "历史重复一：先跑测试再合并，降低回归",
                             importance=0.6)
        promote.seed_project("prj-eval", "fact", "历史重复二：先跑测试再合并，可降低回归",
                             importance=0.6)
        _check(checks, "dedupe_merge",
               promote.dedupe_candidates("prj-eval")["merged"] >= 1)

        same = "跨项目同文必须隔离：先复现再修复，减少盲目改动"
        a = promote.create_candidate("eval-a", "fact", same, importance=0.8)
        b = promote.create_candidate("eval-b", "fact", same, importance=0.8)
        index.rebuild(verify=True)
        rows = index.search("跨项目", project="eval-a")
        _check(checks, "scope_ids_distinct", a["id"] != b["id"])
        _check(checks, "scope_isolation", b["id"] not in [row.get("id") for row in rows])

        frozen = promote.create_candidate(
            "prj-eval", "fact", "低星冻结：先复现再修复，不直接改动", importance=0.8,
            task="eval-task")
        promote.apply_feedback("prj-eval", "eval-task", 2)
        frozen_row = next((row for row in promote.candidates("prj-eval")
                           if row.get("id") == frozen["id"]), {})
        _check(checks, "star_freeze", bool(frozen_row.get("promotion_frozen")))
        _check(checks, "star_freeze_blocks",
               promote.sweep("prj-eval")["promoted"] == 0)

        ok = recall >= 0.75 and mrr >= 0.6 and all(checks.values())
        return {
            "ok": bool(ok),
            "semantic": bool(semantic),
            "semantic_effective": bool(semantic_seen),
            "retrieval": {"queries": len(QUERIES), "recall_at_3": round(recall, 4),
                          "mrr": round(mrr, 4), "cases": per_query},
            "checks": checks,
        }
    finally:
        for key, value in old_runtime.items():
            set_runtime(key, value)
        if old_dir is None:
            os.environ.pop("DOCMIND_MEMORY_DIR", None)
        else:
            os.environ["DOCMIND_MEMORY_DIR"] = old_dir
        shutil.rmtree(tmp, ignore_errors=True)
