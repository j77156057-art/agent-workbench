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
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
_RUNTIME_KEYS = (
    "llm_provider", "llm_model",
    "web_search_provider", "web_search_api_key", "web_search_api_url",
    "web_search_prefer_builtin",
    "web_fetch_provider", "web_fetch_api_key", "web_fetch_api_url",
)


class WebSettingsSaveTests(unittest.TestCase):
    def setUp(self):
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


if __name__ == "__main__":
    unittest.main()
