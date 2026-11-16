# -*- coding: utf-8 -*-
"""外部资源族能力探针（2026-11-17）：在场真跑机制证据，缺资源可见 skip。

设计纪律（与 real-network lane 同源）：
* 资源在场 → 正面证据（进程真起/日志真回/文件真在/HTTP 真应答）；
* 资源缺席 → `skipTest` 带原因，CI summary 的 skipped 计数可见，**不静默绿**；
* 真桌面点击（vision_*）在共享/CI 环境不安全 → 恒可见 skip，仅人工现场跑；
* 不造假证据：不用 stub 引擎/假截图冒充真跑。

本文件被 unittest discover 收进 sqlite lane；浏览器相关探针另受
browser_available() 守卫（CI runner 的 Edge 起身即退时可见 skip）。
"""
import json
import os
import re
import shutil
import unittest
import urllib.request

import config
import tools

BOX = None


def setUpModule():
    global BOX
    import tempfile
    BOX = tempfile.mkdtemp(prefix="dm_ext_probe_")
    config.set_context_code_root(BOX)
    with open(os.path.join(BOX, "main.py"), "w", encoding="utf-8") as fh:
        fh.write("def hero_damage():\n    return 10\n")


def tearDownModule():
    config.set_context_code_root("")
    if BOX:
        shutil.rmtree(BOX, ignore_errors=True)


def call(nm, *a, **kw):
    v = tools.TOOLS[nm].func(*a, **kw)
    return getattr(v, "text", None) or str(v)


def _godot_exe():
    return (os.environ.get("DOCMIND_GODOT_EXECUTABLE")
            or config.get_runtime("godot_executable")
            or shutil.which("godot") or shutil.which("godot4") or "")


class ExternalResourceProbes(unittest.TestCase):
    def test_engine_chain_real_or_visible_skip(self):
        exe = _godot_exe()
        if not exe or not os.path.exists(exe):
            self.skipTest("Godot 可执行文件缺席（设 DOCMIND_GODOT_EXECUTABLE 可真跑）")
        out = call("engine_start", "")
        self.assertNotIn("失败", out, "engine_start 失败：%s" % out[:160])
        logs = call("engine_logs", "limit: 20\n")
        self.assertIn('"ok": true', logs, "engine_logs 没回真实日志：%s" % logs[:160])
        stopped = call("engine_stop", "")
        self.assertTrue('"ok": true' in stopped or "stopped" in stopped,
                        "engine_stop 没停掉：%s" % stopped[:160])

    def test_eda_script_real_or_visible_skip(self):
        exe = next((shutil.which(t) for t in ("vivado", "klayout", "verdi")
                    if shutil.which(t)), "")
        if not exe:
            self.skipTest("EDA 工具（vivado/klayout/verdi）缺席，脚本探针可见 skip")
        out = call("eda_run_script", json.dumps({
            "tool": "klayout", "kind": "drc", "script": "puts probe\n"}))
        self.assertIn('"ok"', out, "eda_run_script 没回结构化结果：%s" % out[:160])

    def test_vision_desktop_click_always_visible_skip(self):
        self.skipTest("vision_* 真桌面点击在共享/CI 环境不安全：仅人工现场跑，恒可见 skip")

    def test_media_build_produces_real_wav(self):
        out = call("dev_media", "action: build\nscript: sil:0.3 talk:0.7 sil:1.3\n"
                   "out: media-probe\n")
        m = re.search(r"[\w/\\.-]+\.wav", out)
        self.assertIsNotNone(m, "dev_media build 没给 wav 路径：%s" % out[:160])
        wav = m.group(0)
        path = wav if os.path.isabs(wav) else os.path.join(BOX, wav)
        self.assertTrue(os.path.exists(path), "夹具 wav 不在：%s" % path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(4), b"RIFF", "wav 不是真 RIFF 容器")

    def test_serve_start_stop_real_http(self):
        out = call("dev_serve",
                   "action: start\ncmd: python -m http.server 8795\nport: 8795\nwait: 20\n")
        m = re.search(r"job_id=([A-Za-z0-9]+)", out)
        self.assertIsNotNone(m, "dev_serve 没给 job id：%s" % out[:160])
        try:
            with urllib.request.urlopen("http://127.0.0.1:8795/main.py", timeout=5) as r:
                self.assertEqual(r.status, 200, "dev_serve 起了但不应答")
                self.assertIn(b"hero_damage", r.read())
        finally:
            call("dev_serve", "action: stop\njob_id: %s\n" % m.group(1))

    def test_preview_project_browser_guarded(self):
        from agent_runtime import visual_acceptance as visual
        if not visual.browser_available():
            self.skipTest("浏览器起不来，预览截图探针可见 skip：%s"
                          % (visual.browser_probe_reason() or "未知原因"))
        started = call("dev_serve",
                       "action: start\ncmd: python -m http.server 8796\nport: 8796\nwait: 20\n")
        m = re.search(r"job_id=([A-Za-z0-9]+)", started)
        self.assertIsNotNone(m, "preview 前置服务没起：%s" % started[:160])
        try:
            out = call("preview_project", "url: http://127.0.0.1:8796/\n")
            self.assertIn('"ok": true', out,
                          "preview_project 没回真截图证据：%s" % out[:160])
        finally:
            call("dev_serve", "action: stop\njob_id: %s\n" % m.group(1))

    def test_game_impact_fallback_grep_locates_file(self):
        # 语义路径走全局 Chroma（沙箱无法隔离），这里钉文档承诺的兜底机制：
        # 索引不可用时退回大小写不敏感子串 grep，任何环境都能用。
        import vectorstore
        from unittest.mock import patch
        config.set_context_code_root(BOX)
        with patch.object(vectorstore, "count", return_value=0):
            out = call("game_impact", "query: hero_damage\n")
        self.assertIn("main.py", out,
                      "game_impact 兜底 grep 没定位真实文件：%s" % out[:160])


if __name__ == "__main__":
    unittest.main(verbosity=2)