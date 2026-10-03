"""Embedding 客户端：云端(通义千问 text-embedding-v3) + 本地(Ollama nomic-embed-text) + 本地轻量兜底。

说明：
- qwen:   通义千问 text-embedding-v3 (1024 维), 需 DASHSCOPE_API_KEY
- ollama: 本机 Ollama 的 OpenAI 兼容 /v1/embeddings, 默认 nomic-embed-text (768 维), 零 Key
- local:  零依赖离线演示, 字符 n-gram 哈希成固定维向量(非语义, 仅保证链路可跑)

维度在初始化时自动探测(对一条测试文本求嵌入), 避免硬编码与 Chroma 集合维度冲突。
切到 ollama 后若本机 Ollama/模型不可用, 自动降级为 local 并打警告, 保证服务不崩(只是检索变非语义)。
"""
import hashlib
import math
import os
import re
import warnings

from openai import OpenAI

from config import (
    EMBEDDING_PROVIDER,
    EMBEDDING_MODEL,
    LOCAL_EMBED_DIM,
    PROVIDERS,
    get_runtime,
)
from gpu_coordinator import note_activity as _gpu_note_activity


class EmbeddingClient:
    def __init__(self):
        self.provider = get_runtime("embedding_provider") or EMBEDDING_PROVIDER
        self.client = None
        self.model = None
        self.dim = LOCAL_EMBED_DIM  # 兜底默认维度
        #: 降级可见性：True = **本该用远程/本机模型却连不上**，于是退回了本地哈希词袋。
        #:
        #: 这与"用户主动把 embedding_provider 选成 local"是**两件不同的事**：
        #: 前者是故障（维度可能与既有集合不符，且向量无意义），后者是决定。
        #: 入库侧据此拒绝前者、放过后者——否则一次 Ollama 掉线就会把集合维度
        #: 钉死在 256 维的无意义向量上，且**不报任何错**。
        self.degraded = False
        self.degraded_reason = ""

        if self.provider == "qwen":
            # 云端语义向量(需 key)
            cfg = PROVIDERS["qwen"]
            self.client = OpenAI(
                base_url=cfg["base_url"],
                api_key=get_runtime("llm_api_key") or os.getenv("DASHSCOPE_API_KEY", ""),
            )
            self.model = EMBEDDING_MODEL
            self.dim = 1024
        elif self.provider == "ollama":
            # 本机 Ollama OpenAI 兼容端点, 零 Key
            cfg = PROVIDERS["ollama"]
            self.client = OpenAI(base_url=cfg["base_url"], api_key="ollama-local")
            self.model = EMBEDDING_MODEL or "bge-m3"
            # 维度自动探测；探测失败则降级 local
            try:
                self.dim = self._probe_dim()
            except Exception as e:  # noqa: BLE001
                warnings.warn(
                    f"[embeddings] ollama 嵌入不可用({e}), 降级为 local 哈希向量(非语义)。"
                    f"请确认 Ollama 已启动且已拉取 {self.model}。"
                )
                self.client = None
                self.dim = LOCAL_EMBED_DIM
                self.degraded = True
                self.degraded_reason = (
                    "本机 Ollama 嵌入不可用（%s: %s），已降级为 %d 维哈希词袋（非语义）。"
                    "若既有索引是用语义模型建的，这次查询会因维度不符而失败；"
                    "若此刻上传文档，还会把集合维度钉死在哈希向量上。"
                    % (type(e).__name__, e, LOCAL_EMBED_DIM)
                )

    def _probe_dim(self):
        return len(self._remote_embed(["__dim_probe__"])[0])

    def embed(self, texts):
        """texts: list[str] -> list[list[float]]"""
        if self.client is not None and self.provider in ("qwen", "ollama"):
            return self._remote_embed(texts)
        return [self._local_embed(t) for t in texts]

    def _remote_embed(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        # 本地 Ollama 嵌入模型同样驻留显存：打点让 GPU 空闲卸载计时器知道它刚被用过
        if self.provider == "ollama":
            try:
                _gpu_note_activity("ollama")
            except Exception:  # noqa: BLE001
                pass
        resp = self.client.embeddings.create(model=self.model, input=texts)
        return [d.embedding for d in resp.data]

    def _local_embed(self, text):
        vec = [0.0] * self.dim
        tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
        for tok in tokens:
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
