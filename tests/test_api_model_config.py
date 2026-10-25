# -*- coding: utf-8 -*-
"""/api/config 模型切换路由测试：自定义 OpenAI 兼容端点校验 + 能力画像回传。

直接调用 async 处理器（不经 HTTP），失败路径在任何全局状态被修改之前返回，
成功路径用例在 finally 中恢复运行时覆盖与全局 agent.llm。
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402
import llm  # noqa: E402
import projects  # noqa: E402
import secrets_store  # noqa: E402


_RUNTIME_KEYS = ("llm_provider", "llm_model", "llm_base_url", "llm_api_key")


def _call(**kwargs):
    return asyncio.run(api.set_config(api.ConfigReq(**kwargs)))


class ConfigValidationTests(unittest.TestCase):
    def test_unknown_provider_rejected(self):
        resp = _call(provider="not-a-vendor", model="x")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未知 provider", resp.body.decode("utf-8"))

    def test_custom_requires_http_base_url(self):
        resp = _call(provider="custom", model="gpt-4o-mini", base_url="file:///etc/passwd")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("http(s)", resp.body.decode("utf-8"))

    def test_custom_requires_base_url_when_none_saved(self):
        old = config.get_runtime("llm_base_url")
        config._RUNTIME.pop("llm_base_url", None)
        try:
            resp = _call(provider="custom", model="gpt-4o-mini", base_url="")
            self.assertEqual(resp.status_code, 400)
            self.assertIn("接口地址", resp.body.decode("utf-8"))
        finally:
            if old is not None:
                config.set_runtime("llm_base_url", old)

    def test_custom_requires_model_name(self):
        resp = _call(provider="custom", model="  ", base_url="https://host.test/v1")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("模型名称", resp.body.decode("utf-8"))


class ConfigSwitchTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: config.get_runtime(k) for k in _RUNTIME_KEYS}
        self._old_llm = api.agent.llm

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                config._RUNTIME.pop(k, None)
            else:
                config.set_runtime(k, v)
        api.agent.llm = self._old_llm

    def test_custom_switch_returns_capability_and_echoes_base_url(self):
        resp = _call(provider="custom", model="qwen3-8b-local",
                     base_url="https://llm.internal.example.com/v1/")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["llm_provider"], "custom")
        # 尾部斜杠被规范化
        self.assertEqual(resp["base_url"], "https://llm.internal.example.com/v1")
        self.assertEqual(resp["capability"]["thinking"], "toggle")
        # 全局 agent 的 LLM 客户端已即时重建
        self.assertEqual(api.agent.llm.provider, "custom")
        self.assertEqual(api.agent.llm.model, "qwen3-8b-local")

    def test_mock_switch_needs_no_key(self):
        resp = _call(provider="mock", model="mock")
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["warnings"], [])
        self.assertFalse(resp["capability"]["cloud"])

    def test_ollama_switch_hidden_behind_probe(self):
        """切到 ollama 必须先过模型探活：探活失败时不得改动当前模型。"""
        before = config.get_runtime("llm_provider")
        with patch("api._check_ollama_model", return_value=(False, "模型不存在")):
            resp = _call(provider="ollama", model="not-exist:latest")
        self.assertFalse(resp["ok"])
        self.assertIn("模型不存在", resp.get("model_error", ""))
        self.assertEqual(config.get_runtime("llm_provider"), before,
                         "探活失败时不能切换 provider")


class GetConfigShapeTests(unittest.TestCase):
    def test_payload_carries_provider_meta_and_capability(self):
        async def go():
            with patch("api.check_ollama", return_value={"reachable": False}):
                return await api.get_config()
        resp = asyncio.run(go())
        for name in ("qwen", "deepseek", "kimi", "zhipu", "siliconflow",
                     "openai", "ollama", "llamacpp", "custom", "mock"):
            self.assertIn(name, resp["providers"])
            meta = resp["provider_meta"][name]
            self.assertTrue(meta["label"])
            self.assertIn("cloud", meta)
        self.assertIn("context_window", resp["capability"])
        self.assertIn("thinking", resp["capability"])
        self.assertIn("vision", resp["capability"])
        self.assertIn("video", resp["capability"])
        self.assertIn("custom_base_url", resp)
        # 窗口覆盖/来源字段存在
        self.assertIn("context_window_override", resp)
        self.assertIn("context_source", resp)
        # 密钥字段只回布尔，不回原文
        self.assertNotIn("api_key", resp)


class ThinkingLevelConfigTests(unittest.TestCase):
    """/api/config 全局默认思考等级：设置回显 + GET 一致性（mock，不触网）。"""

    def test_set_and_echo_thinking_level(self):
        prev = config.get_thinking_level()
        try:
            resp = _call(provider="mock", model="mock", thinking_level="high")
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["thinking_level_default"], "high")
            self.assertIn("thinking_levels", resp["capability"])
            self.assertIn("thinking_effort", resp["capability"])
            # GET 与刚设置的默认一致
            async def go():
                with patch("api.check_ollama", return_value={"reachable": False}):
                    return await api.get_config()
            got = asyncio.run(go())
            self.assertEqual(got["thinking_level_default"], "high")
        finally:
            config.set_thinking_level(prev or "")


class ContextCandidateParseTests(unittest.TestCase):
    """搜索结果文本中的窗口数值提取（千分位/k/m/万；无关数字不采信）。"""

    def tokens(self, text):
        return [c["tokens"] for c in api._extract_context_candidates(text)]

    def test_separated_comma_number(self):
        self.assertIn(131072, self.tokens(
            "该模型支持 context length of 131,072 tokens，适合长文档。"))

    def test_k_suffix(self):
        self.assertIn(131072, self.tokens("官方文档：context window 128k tokens。"))

    def test_m_suffix(self):
        self.assertIn(1048576, self.tokens("Gemini 级别：context length up to 1M tokens。"))

    def test_chinese_wan(self):
        self.assertIn(128000, self.tokens("该模型上下文窗口为 12.8万 tokens。"))

    def test_unrelated_bare_numbers_ignored(self):
        # 页面里的下载量/好评率等裸数字附近没有上下文关键词，一律不采信
        self.assertEqual(api._extract_context_candidates(
            "下载次数 50000 次，好评率 99%，发布于 2024 年。"), [])

    def test_keyword_proximity_required_and_evidence_returned(self):
        cs = api._extract_context_candidates(
            "The model supports a context length of 131,072 tokens.")
        self.assertTrue(cs)
        self.assertEqual(cs[0]["tokens"], 131072)
        self.assertIn("context", cs[0]["evidence"].lower())


class ContextWindowEndpointTests(unittest.TestCase):
    """实时探测 / 联网查询端点 + /api/config 窗口覆盖读写（全部打桩，不触网）。"""

    MODEL = "zz-unit-ctx"

    def setUp(self):
        self._saved = {k: config.get_runtime(k) for k in _RUNTIME_KEYS}
        self._old_llm = api.agent.llm

    def tearDown(self):
        config.clear_context_window_override("mock", self.MODEL)
        for k, v in self._saved.items():
            if v is None:
                config._RUNTIME.pop(k, None)
            else:
                config.set_runtime(k, v)
        api.agent.llm = self._old_llm

    def test_ollama_probe_endpoint(self):
        from starlette.testclient import TestClient
        with patch("api.probe_ollama_context", return_value=65536):
            with TestClient(api.app) as c:
                r = c.get("/api/ollama/probe", params={"model": "q:7b"})
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["context_window"], 65536)
        # 探测失败返回 ok=False（非 5xx），前端可降级手填
        with patch("api.probe_ollama_context", return_value=None):
            with TestClient(api.app) as c:
                r2 = c.get("/api/ollama/probe", params={"model": "missing:7b"})
        self.assertFalse(r2.json()["ok"])
        self.assertTrue(r2.json()["error"])

    def test_model_context_lookup_endpoint(self):
        from starlette.testclient import TestClient
        fake = ("搜索结果：该模型 context length 为 131,072 tokens。\n"
                "另一篇文章提到 context window 128k tokens。")
        with patch("api.web_search", return_value=fake):
            with TestClient(api.app) as c:
                r = c.post("/api/model_context_lookup",
                           json={"provider": "qwen", "model": "qwen-long"})
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertIn(131072, [x["tokens"] for x in data["candidates"]])
        self.assertEqual(data["best"], data["candidates"][0]["tokens"])
        # 识别不到数字时 ok=False，提示手动填写
        with patch("api.web_search", return_value="今天天气不错，没有任何模型参数。"):
            with TestClient(api.app) as c:
                r2 = c.post("/api/model_context_lookup",
                            json={"provider": "qwen", "model": "x"})
        self.assertFalse(r2.json()["ok"])

    def test_config_save_custom_window_and_clear(self):
        from starlette.testclient import TestClient
        with patch("api.check_ollama", return_value={"reachable": False}):
            with TestClient(api.app) as c:
                # 范围外的值被 400 拒绝
                bad = c.post("/api/config", json={"provider": "mock", "model": self.MODEL,
                                                  "context_window": 999})
                self.assertEqual(bad.status_code, 400)
                # 正常保存：画像窗口立即变成手填值，来源标记为 custom
                ok = c.post("/api/config", json={"provider": "mock", "model": self.MODEL,
                                                 "context_window": 40000}).json()
                self.assertTrue(ok["ok"])
                self.assertEqual(ok["context_window_override"], 40000)
                self.assertEqual(ok["context_source"], "custom")
                self.assertEqual(ok["capability"]["context_window"], 40000)
                # GET 同样带回覆盖值与来源
                got = c.get("/api/config").json()
                self.assertEqual(got["context_window_override"], 40000)
                self.assertEqual(got["context_source"], "custom")
                # 传 0 清除覆盖：回到 mock 厂商默认窗口（32768）
                cleared = c.post("/api/config", json={"provider": "mock", "model": self.MODEL,
                                                      "context_window": 0}).json()
                self.assertEqual(cleared["context_window_override"], 0)
                self.assertEqual(cleared["capability"]["context_window"], 32768)
        self.assertIsNone(config.get_context_window_override("mock", self.MODEL))


class ModelPersistenceTests(unittest.TestCase):
    """模型选择跨进程恢复：状态文件保存选择，密钥不落入状态 JSON。"""

    def setUp(self):
        self._old_state = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_llm = api.agent.llm
        fd, self._state = tempfile.mkstemp(prefix="docmind_model_state_", suffix=".json")
        os.close(fd)
        os.unlink(self._state)
        config.STATE_FILE = self._state

    def tearDown(self):
        config.STATE_FILE = self._old_state
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        api.agent.llm = self._old_llm
        try:
            os.unlink(self._state)
        except OSError:
            pass

    def test_model_selection_is_saved_and_restored(self):
        response = _call(provider="mock", model="mock-persisted")
        self.assertTrue(response["ok"])
        with open(self._state, encoding="utf-8") as handle:
            state = json.load(handle)
        self.assertEqual(state["llm_provider"], "mock")
        self.assertEqual(state["llm_model"], "mock-persisted")
        self.assertEqual(state["llm_base_url"], "")
        self.assertNotIn("llm_api_key", state)

        # 模拟进程退出后模块运行时状态被清空，再由启动恢复。
        for key in ("llm_provider", "llm_model", "llm_base_url", "embedding_provider"):
            config._RUNTIME.pop(key, None)
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("llm_provider"), "mock")
        self.assertEqual(config.get_runtime("llm_model"), "mock-persisted")

    def test_startup_rebuilds_client_from_restored_selection(self):
        config.save_state("llm_provider", "mock")
        config.save_state("llm_model", "mock-restarted")
        config._RUNTIME.clear()
        config._apply_persisted_state()
        asyncio.run(api._restore_persisted_llm())
        self.assertEqual(api.agent.llm.provider, "mock")
        self.assertEqual(api.agent.llm.model, "mock-restarted")

    def test_custom_endpoint_and_embedding_are_restored(self):
        response = _call(provider="custom", model="vision-local",
                         base_url="https://example.test/v1",
                         embedding_provider="ollama")
        self.assertTrue(response["ok"])
        presets = config.load_model_presets()
        self.assertEqual(len(presets), 1)
        self.assertEqual((presets[0]["provider"], presets[0]["model"], presets[0]["base_url"]),
                         ("custom", "vision-local", "https://example.test/v1"))
        # Re-saving the same model must not fill the quick switcher with duplicates.
        self.assertTrue(_call(provider="custom", model="vision-local",
                              base_url="https://example.test/v1")["ok"])
        self.assertEqual(len(config.load_model_presets()), 1)
        config._RUNTIME.clear()
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("llm_provider"), "custom")
        self.assertEqual(config.get_runtime("llm_model"), "vision-local")
        self.assertEqual(config.get_runtime("llm_base_url"), "https://example.test/v1")
        self.assertEqual(config.get_runtime("embedding_provider"), "ollama")


class ProjectKeyPersistenceTests(unittest.TestCase):
    """API Key follows the current project even if legacy code_root is stale."""

    def setUp(self):
        self._old_state_root = config.STATE_ROOT
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_llm = api.agent.llm
        self._old_deepseek_window = config.get_context_window_override(
            "deepseek", "deepseek-chat"
        )
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_project_key_")
        self._state_root = os.path.join(self._tmp.name, "state")
        self._game_root = os.path.join(self._tmp.name, "game")
        self._stale_root = os.path.join(self._tmp.name, "workbench")
        os.makedirs(self._state_root)
        os.makedirs(self._game_root)
        os.makedirs(self._stale_root)
        config.STATE_ROOT = self._state_root
        config.STATE_FILE = os.path.join(self._state_root, ".docmind_state.json")
        config._RUNTIME.clear()

    def tearDown(self):
        if self._old_deepseek_window:
            config.set_context_window_override(
                "deepseek", "deepseek-chat", self._old_deepseek_window
            )
        else:
            config.clear_context_window_override("deepseek", "deepseek-chat")
        config.STATE_ROOT = self._old_state_root
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        api.agent.llm = self._old_llm
        self._tmp.cleanup()

    def _persist_current_game(self):
        pid = projects.ensure_project(self._game_root)
        self.assertTrue(projects.set_current(pid))
        # This is the real failure shape: the new project registry points at
        # the game while the backward-compatible pointer still names the app.
        config.save_state("code_root", self._stale_root)
        config.save_state("llm_provider", "deepseek")
        config.save_state("llm_model", "deepseek-chat")
        return pid

    def test_restart_loads_key_from_current_project_not_stale_code_root(self):
        self._persist_current_game()
        self.assertTrue(secrets_store.save(
            self._game_root, "deepseek", "sk-restart-persisted"
        )["ok"])

        # Simulate a fresh process: only persisted state remains in memory.
        config._RUNTIME.clear()
        config._apply_persisted_state()
        self.assertEqual(config.get_runtime("code_root"), self._stale_root)

        asyncio.run(api._restore_persisted_llm())

        self.assertEqual(config.get_runtime("llm_api_key"), "sk-restart-persisted")
        self.assertEqual(api.agent.llm.api_key, "sk-restart-persisted")

    def test_blank_key_update_does_not_overwrite_saved_key(self):
        self._persist_current_game()
        config._apply_persisted_state()
        first = _call(provider="deepseek", model="deepseek-chat",
                      api_key="sk-keep-existing")
        self.assertTrue(first["ok"])
        second = _call(provider="deepseek", model="deepseek-chat",
                       api_key="", context_window=65536)
        self.assertTrue(second["ok"])
        self.assertEqual(
            secrets_store.load(self._game_root, "deepseek"),
            "sk-keep-existing",
        )


class NoProjectKeyPersistenceTests(unittest.TestCase):
    """首次启动还没登记项目时保存的 Key 也必须跨重启保留（Key 不依赖项目）。"""

    def setUp(self):
        self._old_state_root = config.STATE_ROOT
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_llm = api.agent.llm
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_noproject_key_")
        self._state_root = os.path.join(self._tmp.name, "state")
        os.makedirs(self._state_root)
        config.STATE_ROOT = self._state_root
        config.STATE_FILE = os.path.join(self._state_root, ".docmind_state.json")
        config._RUNTIME.clear()
        # 明确的「还没有任何项目、也没有旧 code_root」形状
        config.save_state("projects", {})
        config.save_state("current_project_id", "")
        config.save_state("code_root", "")

    def tearDown(self):
        config.STATE_ROOT = self._old_state_root
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        api.agent.llm = self._old_llm
        self._tmp.cleanup()

    @staticmethod
    def _blank_keys():
        return patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "", "DEEPSEEK_API_KEY": "", "LLM_API_KEY": "",
        })

    def test_key_saved_without_any_project_survives_restart(self):
        with self._blank_keys(), patch.object(api, "LLM_API_KEY", ""), \
                patch.object(api, "CODE_ROOT", ""):
            self.assertIsNone(api._project_root_or_error())
            resp = _call(provider="deepseek", model="deepseek-chat",
                         api_key="sk-no-project")
            self.assertTrue(resp["ok"])

            # 全局密钥库（没有项目也能读）与自动生成的预设槽位都已落盘
            self.assertEqual(secrets_store.load(None, "deepseek"), "sk-no-project")
            presets = config.load_model_presets()
            self.assertTrue(presets)
            self.assertEqual(
                secrets_store.load(None, api._preset_secret_slot(presets[0]["id"])),
                "sk-no-project",
            )

            # 模拟重启：清运行时 → 恢复选择 → 恢复密钥（没有任何项目可依赖）
            config._RUNTIME.clear()
            config._apply_persisted_state()
            self.assertIsNone(api._project_root_or_error())
            asyncio.run(api._restore_persisted_llm())

        self.assertEqual(config.get_runtime("llm_api_key"), "sk-no-project")
        self.assertEqual(api.agent.llm.provider, "deepseek")
        self.assertEqual(api.agent.llm.api_key, "sk-no-project")

    def test_key_saved_with_project_is_mirrored_to_global_store(self):
        project_root = os.path.join(self._tmp.name, "proj")
        os.makedirs(project_root)
        pid = projects.ensure_project(project_root)
        self.assertTrue(projects.set_current(pid))

        with self._blank_keys(), patch.object(api, "LLM_API_KEY", ""):
            resp = _call(provider="deepseek", model="deepseek-chat",
                         api_key="sk-mirrored")
        self.assertTrue(resp["ok"])
        self.assertEqual(secrets_store.load(project_root, "deepseek"), "sk-mirrored")
        self.assertEqual(secrets_store.load(None, "deepseek"), "sk-mirrored")


class MissingKeyPreflightTests(unittest.TestCase):
    """切云端模型前必须确认有 Key：阻断要发生在任何状态变更之前。"""

    def setUp(self):
        self._old_state_root = config.STATE_ROOT
        self._old_state_file = config.STATE_FILE
        self._old_runtime = dict(config._RUNTIME)
        self._old_llm = api.agent.llm
        self._old_sessions = [
            (key, ag, ag.llm, getattr(ag, "last_context", None))
            for key, ag in list(api._SESSION_AGENTS.items())
        ]
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_missing_key_")
        self._state_root = os.path.join(self._tmp.name, "state")
        self._project_root = os.path.join(self._tmp.name, "project")
        os.makedirs(self._state_root)
        os.makedirs(self._project_root)
        config.STATE_ROOT = self._state_root
        config.STATE_FILE = os.path.join(self._state_root, ".docmind_state.json")
        config._RUNTIME.clear()
        pid = projects.ensure_project(self._project_root)
        self.assertTrue(projects.set_current(pid))
        config.set_runtime("llm_provider", "mock")
        config.set_runtime("llm_model", "mock")

    def tearDown(self):
        for _key, ag, saved_llm, saved_context in self._old_sessions:
            ag.llm = saved_llm
            ag.last_context = saved_context
        api.agent.llm = self._old_llm
        config.STATE_ROOT = self._old_state_root
        config.STATE_FILE = self._old_state_file
        config._RUNTIME.clear()
        config._RUNTIME.update(self._old_runtime)
        self._tmp.cleanup()

    @staticmethod
    def _blank_keys():
        return patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "",
            "DEEPSEEK_API_KEY": "",
            "LLM_API_KEY": "",
        })

    def test_cloud_switch_without_any_key_is_blocked_before_mutation(self):
        with self._blank_keys(), patch.object(api, "LLM_API_KEY", ""):
            resp = _call(provider="deepseek", model="deepseek-chat")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("API Key", resp.body.decode("utf-8"))
        # 阻断发生在入口：provider/model/客户端/密钥/预设全部保持原样
        self.assertEqual(config.get_runtime("llm_provider"), "mock")
        self.assertEqual(config.get_runtime("llm_model"), "mock")
        self.assertIsNone(config.get_runtime("llm_api_key"))
        self.assertIs(api.agent.llm, self._old_llm)
        self.assertEqual(secrets_store.load(self._project_root, "deepseek"), "")
        self.assertEqual(config.load_model_presets(), [])

    def test_other_vendors_env_key_does_not_unlock_switch(self):
        env = {"DASHSCOPE_API_KEY": "sk-qwen-only",
               "DEEPSEEK_API_KEY": "", "LLM_API_KEY": ""}
        with patch.dict(os.environ, env), patch.object(api, "LLM_API_KEY", ""):
            resp = _call(provider="deepseek", model="deepseek-chat")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("API Key", resp.body.decode("utf-8"))

    def test_config_has_key_only_counts_selected_provider(self):
        async def snapshot(provider):
            config.set_runtime("llm_provider", provider)
            config.set_runtime("llm_model", "any-model")
            with patch("api.check_ollama", return_value={"reachable": False}):
                return await api.get_config()

        env = {"DASHSCOPE_API_KEY": "sk-qwen-only",
               "DEEPSEEK_API_KEY": "", "LLM_API_KEY": ""}
        with patch.dict(os.environ, env), patch.object(api, "LLM_API_KEY", ""):
            deep = asyncio.run(snapshot("deepseek"))
            qwen = asyncio.run(snapshot("qwen"))
        self.assertFalse(deep["has_key"])
        self.assertTrue(qwen["has_key"])

    def test_stored_project_key_unlocks_switch_without_retyping(self):
        self.assertTrue(secrets_store.save(
            self._project_root, "deepseek", "sk-project-stored")["ok"])
        with self._blank_keys(), patch.object(api, "LLM_API_KEY", ""):
            resp = _call(provider="deepseek", model="deepseek-chat")
        self.assertTrue(resp["ok"])
        self.assertEqual(config.get_runtime("llm_api_key"), "sk-project-stored")
        self.assertEqual(api.agent.llm.provider, "deepseek")
        self.assertEqual(api.agent.llm.api_key, "sk-project-stored")

    def test_vendor_env_key_unlocks_switch(self):
        env = {"DASHSCOPE_API_KEY": "", "DEEPSEEK_API_KEY": "sk-vendor-env",
               "LLM_API_KEY": ""}
        with patch.dict(os.environ, env), patch.object(api, "LLM_API_KEY", ""):
            resp = _call(provider="deepseek", model="deepseek-chat")
        self.assertTrue(resp["ok"])
        self.assertEqual(api.agent.llm.api_key, "sk-vendor-env")

    def test_chat_raises_missing_key_before_network(self):
        with self._blank_keys(), patch.object(llm, "LLM_API_KEY", ""):
            client = llm.LLMClient(provider="deepseek", model="deepseek-chat")
            with self.assertRaises(llm.MissingAPIKey) as caught:
                client.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(llm.is_retryable(caught.exception))


if __name__ == "__main__":
    unittest.main()
