"""本地图检索：在现有 edges 表上做 Personalized PageRank（无外部图数据库）。

边的方向在“关联/取代”语义上是有向的，但检索激活按无向传播——用户查到一个
节点时，它的前驱与后继都可能是相关证据。PPR 只用于**扩展**基础检索结果，
不改变关键词/语义的原始档序。
"""
from __future__ import annotations


def personalized_pagerank(edges, seeds, *, damping=0.85, iterations=20):
    """返回 {item_id: score}；seeds 是 {id: weight}，权重会被归一化。"""
    seed_total = sum(float(value) for value in (seeds or {}).values() if float(value) > 0)
    if not seed_total:
        return {}
    base = {str(node): float(weight) / seed_total
            for node, weight in seeds.items() if float(weight) > 0}
    adjacency = {}
    for src, dst in edges or []:
        src, dst = str(src), str(dst)
        if not src or not dst or src == dst:
            continue
        adjacency.setdefault(src, set()).add(dst)
        adjacency.setdefault(dst, set()).add(src)
        base.setdefault(src, 0.0)
        base.setdefault(dst, 0.0)
    nodes = set(base) | set(adjacency)
    if not nodes:
        return {}
    scores = {node: base.get(node, 0.0) for node in nodes}
    for _ in range(max(1, int(iterations))):
        updated = {node: (1.0 - damping) * base.get(node, 0.0) for node in nodes}
        for node in nodes:
            neighbours = adjacency.get(node)
            if not neighbours:
                continue
            share = damping * scores.get(node, 0.0) / len(neighbours)
            for neighbour in neighbours:
                updated[neighbour] += share
        scores = updated
    total = sum(scores.values())
    if total:
        scores = {node: value / total for node, value in scores.items()}
    return scores
