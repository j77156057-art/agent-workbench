# -*- coding: utf-8 -*-
"""网络搜索 / 抓取 API Key 必须进加密密钥库，不得明文落 STATE_FILE。

2026-10-29 收口：旧版 `set_web_search_api_key` 直接 `save_state` 明文写盘。
这里锁住三件事：写入进库、读取能回退到库、启动时把旧明文迁移并抹掉。
"""
import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import secrets_store  # noqa: E402

_SLOTS = (
    ("web_search_api_key", "web_search"),
    ("web_fetch_api_key", "web_fetch"),
)


def _state() -> dict:
    try:
        return json.loads(pathlib.Path(config.STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class WebKeySecretStorageTests(unittest.TestCase):
    def setUp(self):
        self._saved = {key: config.get_runtime(key) for key, _ in _SLOTS}

    def tearDown(self):
        for key, _slot in _SLOTS:
            config._RUNTIME.pop(key, None)
            if self._saved.get(key) is not None:
                config.set_runtime(key, self._saved[key])
            config.save_state(key, "")

    def test_search_key_is_stored_encrypted_not_in_state_file(self):
        config.set_web_search_api_key("sk-search-test")
        self.assertEqual(secrets_store.load(None, "web_search"), "sk-search-test")
        self.assertIn(_state().get("web_search_api_key"), ("", None),
                      "API Key 不得再以明文留在 STATE_FILE")
        self.assertEqual(config.get_web_search_api_key(), "sk-search-test")

    def test_getter_falls_back_to_the_store_after_restart(self):
        config.set_web_search_api_key("sk-search-restart")
        config._RUNTIME.pop("web_search_api_key", None)  # 模拟新进程只有磁盘状态
        self.assertEqual(config.get_web_search_api_key(), "sk-search-restart")

    def test_empty_key_removes_the_stored_secret(self):
        config.set_web_search_api_key("sk-search-drop")
        config.set_web_search_api_key("")
        self.assertEqual(secrets_store.load(None, "web_search"), "")
        self.assertEqual(config.get_web_search_api_key(), "")

    def test_startup_migrates_the_legacy_plaintext_key(self):
        config.save_state("web_search_api_key", "sk-legacy-plain")
        config._RUNTIME.pop("web_search_api_key", None)
        config._apply_web_search_state({"web_search_api_key": "sk-legacy-plain"})
        self.assertEqual(secrets_store.load(None, "web_search"), "sk-legacy-plain")
        self.assertIn(_state().get("web_search_api_key"), ("", None),
                      "迁移后必须抹掉旧明文")
        self.assertEqual(config.get_web_search_api_key(), "sk-legacy-plain")

    def test_runtime_override_still_wins(self):
        config.set_web_search_api_key("sk-from-store")
        config.set_runtime("web_search_api_key", "sk-runtime")
        self.assertEqual(config.get_web_search_api_key(), "sk-runtime")

    def test_fetch_key_follows_the_same_path(self):
        config.set_web_fetch_api_key("sk-fetch-test")
        self.assertEqual(secrets_store.load(None, "web_fetch"), "sk-fetch-test")
        self.assertIn(_state().get("web_fetch_api_key"), ("", None))
        config._RUNTIME.pop("web_fetch_api_key", None)
        self.assertEqual(config.get_web_fetch_api_key(), "sk-fetch-test")


if __name__ == "__main__":
    unittest.main()
