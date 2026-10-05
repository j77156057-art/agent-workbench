"""Chroma 向量库封装：建库 / 入库 / 检索。"""
import chromadb
import re

from config import CHROMA_DIR, COLLECTION_NAME, CODE_COLLECTION_NAME, ensure_dirs

# 去掉上传时为防重名加的 32 位 hex uuid 前缀，让 UI 展示干净的原始文件名
_UUID_PREFIX = re.compile(r"^[0-9a-f]{32}_")


def pretty_source(s):
    """把 chroma 里保存的 source（可能是 './uploads/<uuid>_xxx.md' 或 'xxx.md'）
    剥成干净的文档名，供 UI 状态栏与检索观察区展示。

    非字符串（脏 metadata 里的 None / 数字 / 列表）一律当空处理：这是**展示层**
    助手，会被任意检索结果调用，一次 `s.replace` 就能把整个检索打崩——
    实测 `source=123` 时抛 `AttributeError: 'int' object has no attribute 'replace'`。
    """
    if not s or not isinstance(s, str):
        return ""
    s = s.replace("\\", "/").split("/")[-1]  # basename
    return _UUID_PREFIX.sub("", s)


_client = None


def _get_client():
    global _client
    if _client is None:
        # 惰性建目录：脱离 api.py lifespan 的用法（脚本 / 测试）也能正常建库。
        ensure_dirs()
        _client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _client


def get_collection(name=COLLECTION_NAME):
    return _get_client().get_or_create_collection(
        name=name, metadata={"hnsw:space": "cosine"}
    )


def add_documents(chunks, embeddings, metadatas, ids, collection=COLLECTION_NAME):
    col = get_collection(collection)
    try:
        col.add(ids=ids, documents=chunks, embeddings=embeddings, metadatas=metadatas)
        _invalidate_lexical(collection)
    except Exception as exc:  # noqa: BLE001
        # Chroma fixes a collection's dimension on first insert.  Make a
        # stale-provider mismatch actionable instead of leaking its opaque
        # InvalidArgumentError; never delete the existing index implicitly.
        msg = str(exc)
        if "dimension" in msg.lower():
            raise ValueError(
                f"向量维度与集合不匹配（当前输入 {len(embeddings[0]) if embeddings else 0} 维）。"
                "请切换到创建该集合时的 embedding 配置，或显式执行 reset_collection() 重建索引。"
            ) from exc
        raise


def query(text_embedding, k=4, collection=COLLECTION_NAME, where=None):
    col = get_collection(collection)
    try:
        kwargs = {"query_embeddings": [text_embedding], "n_results": k}
        if where:
            kwargs["where"] = where
        return col.query(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if "dimension" in str(exc).lower():
            raise ValueError(
                f"查询向量维度与集合不匹配（当前输入 {len(text_embedding)} 维）。"
                "请切换 embedding 配置，或显式 reset_collection() 重建索引。"
            ) from exc
        raise


def reset_collection(name=COLLECTION_NAME):
    """删除并重建集合（切换 embedding 维度时调用，避免新旧向量维度冲突）。"""
    client = _get_client()
    try:
        client.delete_collection(name)
    except Exception:  # noqa: BLE001
        pass
    _invalidate_lexical(name)
    return get_collection(name)


def delete_by_source(source, collection=COLLECTION_NAME):
    """删除某来源文件的全部切片（元数据 source 精确匹配）。

    用于工作台手动保存/改名/删除后的增量索引维护；返回是否无异常。
    """
    try:
        col = get_collection(collection)
        col.delete(where={"source": source})
        _invalidate_lexical(collection)
        return True
    except Exception:  # noqa: BLE001
        return False


def _invalidate_lexical(collection):
    """Notify the optional lexical cache without creating an import cycle."""
    try:
        from agent_runtime.retrieval import invalidate_lexical_index
        invalidate_lexical_index(collection)
    except Exception:
        pass


def list_sources(collection=COLLECTION_NAME):
    """返回集合中已入库文档的来源（去重、排序，剥 uuid 前缀），供 UI 展示与上下文感知。"""
    try:
        col = get_collection(collection)
        res = col.get(include=["metadatas"])
        sources = {m.get("source", "") for m in (res.get("metadatas") or []) if m and m.get("source")}
        return sorted({pretty_source(s) for s in sources if s})
    except Exception:  # noqa: BLE001
        return []


def count(collection=COLLECTION_NAME):
    """返回集合中已入库条数，供状态展示。"""
    try:
        return get_collection(collection).count()
    except Exception:  # noqa: BLE001
        return 0
