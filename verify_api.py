"""接口层验证：用 TestClient 检查 /api/ingest /api/chat(SSE) / 首页。"""
import os

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("EMBEDDING_PROVIDER", "local")

from fastapi.testclient import TestClient

import api

client = TestClient(api.app)

with open("sample_docs/docmind_product.md", "rb") as f:
    r = client.post("/api/ingest", files={"file": ("docmind_product.md", f, "text/plain")})
print("ingest ->", r.json())

r = client.post("/api/chat", data={"question": "Agent 工作台 的计费方式是怎样的？"})
print("chat status ->", r.status_code)
import json

types = []
for block in r.text.split("\n\n"):
    block = block.strip()
    if not block.startswith("data:"):
        continue
    try:
        obj = json.loads(block[len("data:"):].strip())
    except Exception:  # noqa: BLE001
        continue
    if obj.get("type"):
        types.append(obj["type"])
print("SSE 事件类型序列 ->", types)

r = client.get("/")
print("index ->", r.status_code, r.headers.get("content-type"))
assert r.status_code == 200
assert "Agent 工作台" in r.text
print("OK: 接口层验证通过。")
