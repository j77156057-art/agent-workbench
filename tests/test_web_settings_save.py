# -*- coding: utf-8 -*-
"""设置页局部保存的回归：保存网络搜索不能动 LLM provider。

2026-10-29 实测：设置页保存网络搜索只提交 web_* 字段，此前复用 /api/config，
而 ConfigReq.provider 的默认值是 "mock" —— 一次保存就把真实模型切到离线 mock、
重建客户端并写盘（重启仍是 mock），界面却显示「已保存」。
现在保存走专用 /api/web-settings；本文件锁住「只碰 web_*」这一契约。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUNTIME_KEYS = (
    "llm_provider", "llm_model",
    "web_search_provider", "web_search_api_key", "web_search_api_key_provider",
    "web_search_api_url", "web_search_prefer_builtin",
    "web_fetch_provider", "web_fetch_api_key", "web_fetch_api_key_provider",
    "web_fetch_api_url",
)


class WebSettingsSaveTests(unittest.TestCase):
    def setUp(self):
        # 自建临时状态根：这组用例会写 state / 密钥库，关闭测试隔离时也不能碰真的。
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_websettings_")
        self._old_root = config.STATE_ROOT
        self._old_file = config.STATE_FILE
        config.STATE_ROOT = self._tmp.name
        config.STATE_FILE = os.path.join(self._tmp.name, ".docmind_state.json")
        self._saved = {k: config.get_runtime(k) for k in _RUNTIME_KEYS}
        self._old_llm = api.agent.llm
        config.set_runtime("llm_provider", "deepseek")
        config.set_runtime("llm_model", "deepseek-flash")
        config.save_state("llm_provider", "deepseek")
        config.save_state("llm_model", "deepseek-flash")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                config._RUNTIME.pop(key, None)
            else:
                config.set_runtime(key, value)
        api.agent.llm = self._old_llm
        config.STATE_ROOT = self._old_root
        config.STATE_FILE = self._old_file
        self._tmp.cleanup()

    def test_saving_search_settings_keeps_model_and_client(self):
        resp = asyncio.run(api.set_web_settings(api.WebSettingsReq(
            web_search_provider="tavily",
            web_search_api_url="https://api.tavily.com/",
            web_fetch_provider="jina",
        )))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["web_search_provider"], "tavily")
        self.assertEqual(resp["web_search_api_url"], "https://api.tavily.com")
        self.assertEqual(resp["web_fetch_provider"], "jina")
        # 模型选择与运行中的客户端都没被触碰
        self.assertEqual(config.get_runtime("llm_provider"), "deepseek")
        self.assertEqual(config.get_runtime("llm_model"), "deepseek-flash")
        self.assertIs(api.agent.llm, self._old_llm)
        state = json.loads(pathlib.Path(config.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(state.get("llm_provider"), "deepseek")
        self.assertEqual(state.get("llm_model"), "deepseek-flash")

    def test_empty_payload_is_a_noop(self):
        before = (config.get_runtime("web_search_provider"),
                  config.get_runtime("llm_provider"))
        resp = asyncio.run(api.set_web_settings(api.WebSettingsReq()))
        self.assertTrue(resp["ok"])
        self.assertEqual((config.get_runtime("web_search_provider"),
                          config.get_runtime("llm_provider")), before)

    def test_partial_payload_without_provider_is_rejected(self):
        """provider 默认值是 "mock"：局部请求不能靠默认值把真实模型打回 mock。"""
        resp = asyncio.run(api.set_config(api.ConfigReq(web_search_provider="tavily")))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("provider", resp.body.decode("utf-8"))
        self.assertEqual(config.get_runtime("llm_provider"), "deepseek")

    def test_frontend_save_uses_the_dedicated_endpoint(self):
        source = (ROOT / "frontend/src/workbench/api/settings.ts").read_text(encoding="utf-8")
        start = source.index("save(req: SaveSettingsReq)")
        block = source[start:start + 600]
        self.assertIn("/api/web-settings", block,
                      "设置页保存必须走专用端点，不能复用 /api/config")
        self.assertNotIn("/api/config", block,
                         "局部保存打 /api/config 会把 LLM provider 覆盖成 mock")

    def test_blank_key_input_is_not_sent_as_empty_string(self):
        """占位符写着「留空表示不修改」：前端不能把空串提交上去（会删掉已存 Key）。"""
        source = (ROOT / "frontend/src/workbench/components/SettingsView.vue").read_text(
            encoding="utf-8")
        block = source[source.index("async function saveSearch"):]
        block = block[:block.index("} finally") if "} finally" in block else 2000]
        for field in ("web_search_api_key", "web_fetch_api_key"):
            self.assertIn("%s: %s.value.trim() || undefined" % (
                field, "wsApiKey" if field.startswith("web_search") else "wfApiKey"), block,
                "%s 留空时必须以 undefined 省略，不能发空串" % field)

    def test_api_urls_must_be_http(self):
        """搜索/抓取的 API 地址只允许 http(s)：file:// 与裸主机名都必须拒绝。"""
        for payload in (
            api.WebSettingsReq(web_search_provider="searxng",
                               web_search_api_url="file:///etc/passwd"),
            api.WebSettingsReq(web_search_provider="searxng",
                               web_search_api_url="api.example.com"),
            api.WebSettingsReq(web_fetch_provider="firecrawl",
                               web_fetch_api_url="ftp://example.com"),
        ):
            resp = asyncio.run(api.set_web_settings(payload))
            self.assertEqual(resp.status_code, 400, payload)
            self.assertIn("http", resp.body.decode("utf-8"))

    def test_apply_config_rejects_bad_web_url_before_any_mutation(self):
        before = config.get_runtime("llm_provider")
        resp = asyncio.run(api.set_config(api.ConfigReq(
            provider="mock", web_search_api_url="file:///etc/passwd")))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(config.get_runtime("llm_provider"), before,
                         "URL 校验必须发生在任何模型状态变更之前")

    def test_saved_key_hint_is_scoped_to_the_selected_provider(self):
        """Key 按厂商分槽后，"已保存" 只能对当前选中的厂商成立。"""
        source = (ROOT / "frontend/src/workbench/components/SettingsView.vue").read_text(
            encoding="utf-8")
        self.assertIn("wsProvider.value === cfg.value?.web_search_provider", source)
        self.assertIn("wfProvider.value === cfg.value?.web_fetch_provider", source)
        self.assertIn("wsHasSavedKey ? '已保存，留空表示不修改'", source)
        self.assertIn("wfHasSavedKey ? '已保存，留空表示不修改'", source)

    def test_omitting_the_key_field_preserves_the_stored_key(self):
        """API 层契约：不提交 key 字段 = 不改动已存 Key。"""
        config.set_runtime("web_search_provider", "bocha")
        config.set_web_search_api_key("sk-keep-me")
        resp = asyncio.run(api.set_web_settings(api.WebSettingsReq(
            web_search_provider="bocha")))
        self.assertTrue(resp["ok"])
        self.assertEqual(config.get_web_search_api_key(), "sk-keep-me")
        self.assertTrue(resp["web_search_has_key"])


if __name__ == "__main__":
    unittest.main()
