# -*- coding: utf-8 -*-
"""网络搜索 / 抓取 API Key：进加密密钥库、按厂商分槽、不落明文。

2026-10-29 收口：
- 旧版把 key 明文写进 .docmind_state.json；
- 早先的实现只有一个全局槽位，切换搜索厂商会串用/覆盖上一家的 key。

本文件锁住：写入进库、按厂商隔离、读取回退、空值删除、两类旧数据迁移。
**自建临时 STATE_ROOT**：即使 `DOCMIND_NO_TEST_ISOLATION=1` 跑测试，也不会
碰到真实的密钥库（本文件含删除槽位的用例）。
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import secrets_store  # noqa: E402


def _state() -> dict:
    try:
        return json.loads(pathlib.Path(config.STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class WebKeySecretStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="docmind_webkey_")
        self._old_root = config.STATE_ROOT
        self._old_file = config.STATE_FILE
        config.STATE_ROOT = self._tmp.name
        config.STATE_FILE = os.path.join(self._tmp.name, ".docmind_state.json")
        self._saved = {key: config.get_runtime(key) for key in (
            "web_search_api_key", "web_search_api_key_provider",
            "web_fetch_api_key", "web_fetch_api_key_provider",
            "web_search_provider", "web_fetch_provider")}
        config.set_runtime("web_search_provider", "bocha")
        config.set_runtime("web_fetch_provider", "jina")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                config._RUNTIME.pop(key, None)
            else:
                config.set_runtime(key, value)
        config.STATE_ROOT = self._old_root
        config.STATE_FILE = self._old_file
        self._tmp.cleanup()

    def test_search_key_is_stored_per_provider_not_in_state_file(self):
        config.set_web_search_api_key("sk-search-test")
        self.assertEqual(secrets_store.load(None, "web:search:bocha"), "sk-search-test")
        self.assertIn(_state().get("web_search_api_key"), ("", None),
                      "API Key 不得再以明文留在 STATE_FILE")
        self.assertEqual(config.get_web_search_api_key(), "sk-search-test")

    def test_getter_falls_back_to_the_store_after_restart(self):
        config.set_web_search_api_key("sk-search-restart")
        config._RUNTIME.pop("web_search_api_key", None)  # 模拟新进程只有磁盘状态
        self.assertEqual(config.get_web_search_api_key(), "sk-search-restart")

    def test_empty_key_removes_only_the_current_provider_secret(self):
        config.set_web_search_api_key("sk-bocha")
        config.set_runtime("web_search_provider", "tavily")
        config.set_web_search_api_key("sk-tavily")
        config.set_web_search_api_key("")  # 清掉 tavily 的
        self.assertEqual(secrets_store.load(None, "web:search:tavily"), "")
        self.assertEqual(secrets_store.load(None, "web:search:bocha"), "sk-bocha")

    def test_keys_are_isolated_per_provider(self):
        config.set_web_search_api_key("sk-bocha")
        config.set_runtime("web_search_provider", "tavily")
        self.assertEqual(config.get_web_search_api_key(), "",
                         "切换厂商后不能串用上一家的 Key")
        config.set_web_search_api_key("sk-tavily")
        self.assertEqual(config.get_web_search_api_key(), "sk-tavily")
        config.set_runtime("web_search_provider", "bocha")
        self.assertEqual(config.get_web_search_api_key(), "sk-bocha")

    def test_startup_migrates_the_legacy_plaintext_key(self):
        config.save_state("web_search_api_key", "sk-legacy-plain")
        config._RUNTIME.pop("web_search_api_key", None)
        config._apply_web_search_state({"web_search_api_key": "sk-legacy-plain"})
        self.assertEqual(secrets_store.load(None, "web:search:bocha"), "sk-legacy-plain")
        self.assertIn(_state().get("web_search_api_key"), ("", None),
                      "迁移后必须抹掉旧明文")
        self.assertEqual(config.get_web_search_api_key(), "sk-legacy-plain")

    def test_startup_migrates_the_legacy_single_slot_to_the_current_provider(self):
        secrets_store.save(None, "web_search", "sk-today")
        config._RUNTIME.pop("web_search_api_key", None)
        config._apply_web_search_state({})
        self.assertEqual(secrets_store.load(None, "web:search:bocha"), "sk-today")
        self.assertEqual(secrets_store.load(None, "web_search"), "",
                         "迁移后旧单一槽位应删除，避免跨厂商复用")
        self.assertEqual(config.get_web_search_api_key(), "sk-today")

    def test_runtime_override_still_wins(self):
        config.set_web_search_api_key("sk-from-store")
        config.set_runtime("web_search_api_key", "sk-runtime")
        self.assertEqual(config.get_web_search_api_key(), "sk-runtime")

    def test_fetch_key_follows_the_same_path(self):
        config.set_web_fetch_api_key("sk-fetch-test")
        self.assertEqual(secrets_store.load(None, "web:fetch:jina"), "sk-fetch-test")
        self.assertIn(_state().get("web_fetch_api_key"), ("", None))
        config._RUNTIME.pop("web_fetch_api_key", None)
        self.assertEqual(config.get_web_fetch_api_key(), "sk-fetch-test")


if __name__ == "__main__":
    unittest.main()
