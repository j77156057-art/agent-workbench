"""端到端验证：mock 模式无需任何 key，跑通 摄取 -> 检索 -> Agent 推理。"""
import os

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "local")

from ingest import ingest_file
from agent import Agent

n = ingest_file("sample_docs/docmind_product.md")
print(f"[verify] 摄取切片数 = {n}")

ag = Agent()
q = "Agent 工作台 支持哪些文件格式？答案准确率怎么保证？"
print(f"[verify] 提问: {q}\n--- Agent 推理过程 ---")
final = None
for ev in ag.run(q, stream=False):
    t = ev["type"]
    txt = ev["text"].replace("\n", " ")
    print(f"  [{t}] {txt[:160]}")
    if t == "final":
        final = txt
print("--- 验证结论 ---")
assert final, "未产生最终答案"
assert ag.history, "多轮记忆未写入"
print("OK: 摄取/检索/ReAct 循环/记忆 全链路跑通。")
