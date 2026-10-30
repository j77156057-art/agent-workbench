# -*- coding: utf-8 -*-
"""跨源闸门：浏览器里的任意网站不能驱动本地 Agent 的读写能力。

2026-10-29 实测：CORS 默认 allow_origins="*"，而写端点里只有少数做了来源校验。
从 http://127.0.0.1:9999 的页面可以读取 /api/config、/api/projects，并成功
POST /api/web-settings。本文件锁住统一闸门：同源 / 无 Origin（脚本、curl）/
显式 allowlist 放行；`*` 与未知来源一律 403。

2026-10-30 补：同源判定不能只比 Origin 与 Host 字符串——DNS rebinding 会把
攻击者域名解析到 127.0.0.1，两者天然相等（实测旧实现 Host/Origin 同为
evil.example:8000 时 /api/health 返回 200）。只有回环名/IP 字面量的 Host
才按同源放行，域名来源必须显式进 allowlist。
"""
import os
import sys
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import config  # noqa: E402


class LocalOriginGateTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app)

    def test_cross_site_origin_is_rejected(self):
        resp = self.client.get("/api/health", headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("来源", resp.json().get("error", ""))

    def test_same_origin_is_allowed(self):
        resp = self.client.get("/api/health", headers={
            "Origin": "http://127.0.0.1:8000", "Host": "127.0.0.1:8000"})
        self.assertEqual(resp.status_code, 200)

    def test_localhost_same_origin_is_allowed(self):
        resp = self.client.get("/api/health", headers={
            "Origin": "http://localhost:5173", "Host": "localhost:5173"})
        self.assertEqual(resp.status_code, 200)

    def test_ip_literal_same_origin_is_allowed(self):
        """局域网 IP 直连不属于 rebinding，仍按同源放行。"""
        resp = self.client.get("/api/health", headers={
            "Origin": "http://192.168.1.5:8000", "Host": "192.168.1.5:8000"})
        self.assertEqual(resp.status_code, 200)

    def test_dns_rebinding_origin_is_rejected(self):
        """攻击者域名解析到 127.0.0.1 时 Origin 与 Host 相同，不能被当成同源。

        实测绕过：Host/Origin 同为 evil.example:8000 时旧实现返回 200。
        域名来源只能走 DOCMIND_CORS_ORIGINS 显式放行。
        """
        resp = self.client.get("/api/health", headers={
            "Origin": "http://evil.example:8000", "Host": "evil.example:8000"})
        self.assertEqual(resp.status_code, 403)

    def test_dns_rebinding_write_has_no_side_effect(self):
        before = config.get_runtime("web_search_provider")
        resp = self.client.post("/api/web-settings", headers={
            "Origin": "http://evil.example:8000", "Host": "evil.example:8000"},
            json={"web_search_provider": "tavily"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(config.get_runtime("web_search_provider"), before)

    def test_missing_origin_is_allowed_for_scripts(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_explicit_allowlist_is_allowed(self):
        with patch.object(api, "DOCMIND_CORS_ORIGINS", ["http://app.example"]):
            resp = self.client.get("/api/health",
                                   headers={"Origin": "http://app.example"})
        self.assertEqual(resp.status_code, 200)

    def test_wildcard_does_not_grant_access(self):
        """`*` 只决定响应可读，不代表任何网站都能驱动本机能力。"""
        with patch.object(api, "DOCMIND_CORS_ORIGINS", ["*"]):
            resp = self.client.get("/api/health",
                                   headers={"Origin": "http://evil.example"})
        self.assertEqual(resp.status_code, 403)

    def test_null_origin_is_rejected(self):
        resp = self.client.get("/api/health", headers={"Origin": "null"})
        self.assertEqual(resp.status_code, 403)

    def test_cross_site_write_has_no_side_effect(self):
        before = config.get_runtime("web_search_provider")
        resp = self.client.post("/api/web-settings",
                                headers={"Origin": "http://evil.example"},
                                json={"web_search_provider": "tavily"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(config.get_runtime("web_search_provider"), before)

    def test_static_pages_are_not_gated(self):
        resp = self.client.get("/workbench", headers={"Origin": "http://evil.example"})
        self.assertNotEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
