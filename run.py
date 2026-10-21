"""一键启动：先摄取示例文档，再启动 FastAPI 服务。"""
import os

from config import PROJECT_DIR
from ingest import ingest_directory

if __name__ == "__main__":
    sample_dir = os.path.join(PROJECT_DIR, "sample_docs")
    if os.path.isdir(sample_dir):
        n = ingest_directory(sample_dir)
        print(f"[ Agent 工作台 ] 已摄取示例文档，共 {n} 个切片")

    import uvicorn

    print("[ Agent 工作台 ] 启动服务 -> http://localhost:8000")
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=False)
