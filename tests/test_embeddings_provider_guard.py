"""embedding provider 的降级契约：任何后端不可用都只降级，不抛异常。

为什么单独钉这一组：
- `api.py` 的保存入口按白名单拒绝非法值，但状态文件可能被手改/旧版本残留，
  所以 `EmbeddingClient` 自己也得兜住 —— 它是被 api / ingest / tools 共用的边界。
- 缺 Key、缺依赖（ImportError）、服务不可达，三种情况必须走**同一条**通道：
  `degraded=True` + `degraded_reason`（+ `/api/config` 的 guidance），
  否则用户看到的是 500 / ModuleNotFoundError，而不是「该装什么、该填什么」。
"""

import os
import unittest
from unittest.mock import patch

import embeddings
from config import LOCAL_EMBED_DIM


def _client(provider, key="", openai_client=None):
    """按指定 provider 构造一个 EmbeddingClient，隔离掉真实的运行时配置。"""
    runtime = {"embedding_provider": provider, "llm_api_key": key}

    def fake_get_runtime(name, default=None):
        return runtime.get(name, default)

    env = {"DASHSCOPE_API_KEY": key}
    with patch.object(embeddings, "get_runtime", side_effect=fake_get_runtime), \
            patch.dict(os.environ, env):
        if openai_client is not None:
            with patch.object(embeddings, "OpenAI", openai_client):
                return embeddings.EmbeddingClient()
        return embeddings.EmbeddingClient()


class MissingKeyTests(unittest.TestCase):
    def test_qwen_without_key_degrades_instead_of_raising(self):
        client = _client("qwen", key="")
        self.assertTrue(client.degraded, "缺 Key 却没标记降级：界面会以为云端嵌入可用")
        self.assertIn("Key", client.degraded_reason)
        self.assertEqual(client.dim, LOCAL_EMBED_DIM)
        vectors = client.embed(["测试"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), LOCAL_EMBED_DIM,
                         "降级后维度必须回落到本地兜底，否则与既有集合对不上")

    def test_qwen_with_key_is_not_degraded(self):
        client = _client("qwen", key="sk-test")
        self.assertFalse(client.degraded)
        self.assertEqual(client.dim, 1024)


class MissingDependencyTests(unittest.TestCase):
    def test_import_error_is_degraded_not_raised(self):
        """后端依赖没装（ImportError）时，构造不能把异常抛给调用方。"""
        def boom(*args, **kwargs):
            raise ImportError("No module named 'some_backend'")

        client = _client("qwen", key="sk-test", openai_client=boom)
        self.assertTrue(client.degraded, "ImportError 没被降级通道接住")
        self.assertIn("缺少依赖", client.degraded_reason)
        self.assertIsNone(client.client)
        self.assertEqual(len(client.embed(["x"])[0]), LOCAL_EMBED_DIM)

    def test_unknown_provider_is_degraded_and_lists_legal_values(self):
        client = _client("openai-embedding", key="")
        self.assertTrue(client.degraded, "非法 provider 被静默当成 local 了")
        for legal in embeddings.SUPPORTED_PROVIDERS:
            self.assertIn(legal, client.degraded_reason,
                          "降级原因没列出合法值 %r，用户不知道改成什么" % legal)


class LocalProviderTests(unittest.TestCase):
    def test_local_is_a_choice_not_a_failure(self):
        client = _client("local")
        self.assertFalse(client.degraded,
                         "用户主动选 local 被标成降级：ingest 会误拒入库")
        self.assertEqual(client.dim, LOCAL_EMBED_DIM)


class GuidanceTests(unittest.TestCase):
    def test_qwen_guidance_is_actionable(self):
        text = embeddings.guidance_for("qwen")
        self.assertIn("DASHSCOPE_API_KEY", text)
        self.assertIn("ollama", text)

    def test_ollama_guidance_names_the_pull_command(self):
        self.assertIn("ollama pull", embeddings.guidance_for("ollama"))


if __name__ == "__main__":
    unittest.main()
