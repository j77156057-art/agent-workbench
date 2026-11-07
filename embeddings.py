"""Embedding 客户端：云端(通义千问 text-embedding-v3) + 本地(Ollama bge-m3) + 本地轻量兜底。

说明：
- qwen:   通义千问 text-embedding-v3 (1024 维), 需 DASHSCOPE_API_KEY
- ollama: 本机 Ollama 的 OpenAI 兼容 /v1/embeddings, 默认 bge-m3, 零 Key
- local:  零依赖离线演示, 字符 n-gram 哈希成固定维向量(非语义, 仅保证链路可跑)

维度在初始化时自动探测(对一条测试文本求嵌入), 避免硬编码与 Chroma 集合维度冲突。
切到 ollama 后若本机 Ollama/模型不可用, 自动降级为 local 并打警告, 保证服务不崩(只是检索变非语义)。

两条不变式（api.py 的保存入口与 /api/config 依赖它们）：
1. 合法 provider 只有 ``SUPPORTED_PROVIDERS`` 这三个；非法值由 api.py 在写状态之前就拒绝。
   即使状态文件里残留了非法值，这里也只降级（degraded=True + 原因），不抛异常。
2. 任何后端初始化失败（缺 Key、缺依赖、服务不可达）都走同一条降级通道：
   ``degraded=True`` + ``degraded_reason``，绝不把 ImportError / 连接错误抛给调用方。
   ``ingest.py`` 据此拒绝用降级向量入库，``/api/config`` 据此回传可照做的指引。
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

#: 合法 embedding provider。顺序与前端下拉、/api/config 的 embedding_options 一致。
#: 注意：三条路径都不需要 torch —— 本地语义向量由 Ollama 进程算（HTTP），
#: 进程内 torch 只在可选的 cross_encoder 重排里出现（requirements-ml.txt）。
SUPPORTED_PROVIDERS = ("local", "ollama", "qwen")

#: 缺 Key 时的可照做提示。放在模块级是为了让 api.py 的 /api/config 能原样回传。
QWEN_KEY_GUIDANCE = (
    "云端嵌入 qwen 需要 API Key。任选一种：\n"
    "  1) 在「⚙ 模型设置」的对话模型里填写 DashScope Key（会复用给嵌入）；\n"
    "  2) 设置环境变量 DASHSCOPE_API_KEY 后重启；\n"
    "  3) 改用 embedding_provider=ollama（本机 Ollama，零 Key）或 local（哈希兜底）。"
)


def guidance_for(provider):
    """给某个 provider 的「当前不可用」提示（不做探测，纯静态）。

    只用于本地就是缺 Key 这种能静态判定的情况；Ollama 的可达性由 api.py 的
    ``check_ollama`` 在线程池里探测并回传，避免这里再打一次网络。
    """
    if provider == "qwen":
        return QWEN_KEY_GUIDANCE
    if provider == "ollama":
        return (
            "本机 Ollama 嵌入不可用。请确认 Ollama 已启动，并执行：\n"
            "  ollama pull %s\n"
            "或在「⚙ 模型设置」把 embedding_provider 换成 qwen / local。"
            % (EMBEDDING_MODEL or "bge-m3")
        )
    return ""


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
        try:
            self._configure()
        except ImportError as e:
            # 可选后端的依赖没装：这是「降级」而不是「崩溃」。调用方（api/ingest）
            # 读 degraded 决定要不要拒绝入库，而不是收到 ModuleNotFoundError。
            self._degrade("缺少依赖（%s: %s）" % (type(e).__name__, e))
        except Exception as e:  # noqa: BLE001 - 后端构造失败一律降级，服务不崩
            self._degrade("嵌入后端初始化失败（%s: %s）" % (type(e).__name__, e))

    def _degrade(self, reason):
        warnings.warn(f"[embeddings] {reason}；已降级为 local 哈希向量(非语义)。")
        self.client = None
        self.model = None
        self.dim = LOCAL_EMBED_DIM
        self.degraded = True
        self.degraded_reason = reason

    def _configure(self):
        if self.provider == "qwen":
            # 云端语义向量(需 key)。缺 Key 时不要把空串塞给 OpenAI 让它抛，
            # 而是落进同一条降级通道，由 /api/config 回传可照做的指引。
            key = get_runtime("llm_api_key") or os.getenv("DASHSCOPE_API_KEY", "")
            if not key:
                self._degrade(QWEN_KEY_GUIDANCE)
                return
            cfg = PROVIDERS["qwen"]
            self.client = OpenAI(base_url=cfg["base_url"], api_key=key)
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
                self._degrade(
                    "本机 Ollama 嵌入不可用（%s: %s）。" % (type(e).__name__, e)
                    + guidance_for("ollama")
                )
        elif self.provider == "local":
            # 用户主动选择：这是决定，不是故障，degraded 保持 False。
            self.dim = LOCAL_EMBED_DIM
        else:
            # 状态文件里残留了非法值（正常路径已被 api.py 白名单拦下）。
            self._degrade(
                "未知 embedding_provider=%r，合法值：%s。" % (
                    self.provider, " / ".join(SUPPORTED_PROVIDERS))
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
