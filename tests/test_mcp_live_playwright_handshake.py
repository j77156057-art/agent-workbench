"""真·Playwright MCP 集成握手：证明连接器能被 rag-agent 内联投影为一等工具。

这一条不属于「FakeMCP 逻辑层」那 40 个用例（`test_mcp_inline_tools.py` 已把 discover
/命名/cap/spec/attach 全覆盖）。这里锁的是真传输：真起 stdio 子进程、真跑一次
`tools/list` JSON-RPC 握手、真把返回的浏览器工具变成 `mcp__<server>__<tool>` 投进
实例工具表；顺带在真握手上把「联网关时对模型隐藏」那条 060b7ea 的语义再钉一遍。

跑不了就 skip，不 fail。条件：
- `node` 在 PATH 上；
- 能找到 `@playwright/mcp` 的 `cli.js`（默认走 `npm root -g`，可用环境变量
  `RAG_TEST_PLAYWRIGHT_MCP_CLI` 覆盖到绝对路径）；
- 首包 `tools/list` 能在 30s 内返回非空条目（网络/进程起不来也算 skip 条件）。

只跑握手 + 投影形状，绝不调 `browser_navigate`——那会真启 Edge、真上网，不适合 CI。
浏览器抓取本身的端到端证据在会话回复里以探针形式给出（2026-10-15），不落仓。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config  # noqa: E402
from agent_runtime import mcp_bridge  # noqa: E402


def _find_pw_cli():
    """定位 @playwright/mcp 的 cli.js；找不到返回 None（触发 skip）。"""
    env = (os.environ.get("RAG_TEST_PLAYWRIGHT_MCP_CLI") or "").strip()
    if env and os.path.isfile(env):
        return env
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        # Windows 上 subprocess 不会从 PATH 自动补 .cmd/.bat 扩展，
        # 必须把 shutil.which 拿到的绝对路径原样喂给 Popen。
        out = subprocess.run([npm, "root", "-g"], capture_output=True, text=True,
                             timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    base = (out.stdout or "").strip().strip('"')
    if not base:
        return None
    cli = os.path.join(base, "@playwright", "mcp", "cli.js")
    return cli if os.path.isfile(cli) else None


class LivePlaywrightHandshake(unittest.TestCase):
    """一次真 stdio 握手：discover → build_specs → attach，形状与门一致。"""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")
        cls.pw_cli = _find_pw_cli()
        if not cls.node or not cls.pw_cli:
            raise unittest.SkipTest(
                "Playwright MCP 未就位：需要 node 在 PATH 且能定位 @playwright/mcp "
                "的 cli.js（可 export RAG_TEST_PLAYWRIGHT_MCP_CLI 覆盖）")

    def setUp(self):
        self._prev_root = config.get_runtime("code_root")
        self._prev_inline = os.environ.get("DOCMIND_MCP_INLINE")
        os.environ["DOCMIND_MCP_INLINE"] = "1"
        self.tmp = tempfile.mkdtemp(prefix="docmind_pw_live_")
        # Agent 构造时按 config.get_runtime("code_root") 走 mcp_bridge.attach
        # （agent.py:3456），所以这里必须把运行时 code_root 也指到临时根，
        # 让实例读到的连接器配置 == 我们写的这份。
        config.set_runtime("code_root", self.tmp)
        cfg = {"servers": {"playwright": {
            "label": "Playwright MCP (headless, live handshake test)",
            "transport": "stdio",
            "command": self.node,
            # 用 `node cli.js --browser msedge --headless --isolated` 避免下载 chromium
            # 与系统里已有 build 号错配。msedge channel 在其它机器上没有时，整条
            # 握手会连不上，走 skipTest 分支。
            "args": [self.pw_cli, "--browser", "msedge", "--headless", "--isolated"],
            "env": {},
            "enabled": True, "inline_tools": True,
        }}}
        with open(os.path.join(self.tmp, ".docmind_mcp.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        mcp_bridge.reset_cache()

    def tearDown(self):
        # 关掉真子进程：mcp_client._SESSIONS 持长驻 stdio，别让它漏到下一测试
        try:
            from mcp_client import _SESSIONS
            for key in list(_SESSIONS.keys()):
                sess = _SESSIONS.pop(key, None)
                if sess is None:
                    continue
                proc = getattr(sess, "proc", None)
                if proc is not None and proc.poll() is None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
        except Exception:
            pass
        config.set_runtime("code_root", self._prev_root)
        mcp_bridge.reset_cache()
        if self._prev_inline is None:
            os.environ.pop("DOCMIND_MCP_INLINE", None)
        else:
            os.environ["DOCMIND_MCP_INLINE"] = self._prev_inline
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_real_handshake_projects_browser_tools_and_respects_web_gate(self):
        import agent as agent_mod
        import tools as tools_mod

        # 1) 真握手：discover 会 spawn 子进程、跑一次 tools/list JSON-RPC
        out = mcp_bridge.discover(self.tmp, refresh=True)
        if not out["entries"]:
            reason = "; ".join(str(x.get("reason", "")) for x in out["skipped"])
            self.skipTest("Playwright MCP 握手没条目，可能 Edge/进程起不来：%s" % reason)

        names = [e["name"] for e in out["entries"]]
        self.assertTrue(all(n.startswith("mcp__playwright__browser_") for n in names),
                        "投影出的名字应全部是 mcp__playwright__browser_*：%s" % names[:5])
        # browser_evaluate 稳在前 12（PER_SERVER 默认）；navigate 若被截也没关系
        self.assertIn("mcp__playwright__browser_evaluate", names)

        # 2) build_specs 的保守能力标记（真握手数据上再钉一遍）
        specs = mcp_bridge.build_specs(out["entries"])
        spec = specs["mcp__playwright__browser_evaluate"]
        from agent_runtime.tools import Capability, SideEffect
        self.assertIs(spec.capability, Capability.NETWORK)
        self.assertEqual(spec.group, "mcp")  # stdio 传输走 mcp group，不是 web
        self.assertIs(spec.side_effect, SideEffect.MUTATING)  # 没打 inline_read_only
        self.assertFalse(spec.parallel_safe)
        self.assertIn("developer", spec.applications)

        # 3) attach 只进实例、不进全局
        dict_view = {"grep": "builtin"}
        rep = mcp_bridge.attach(dict_view, self.tmp)
        self.assertIn("mcp__playwright__browser_evaluate", rep["added"])
        self.assertIn("mcp__playwright__browser_evaluate", dict_view)
        for name in rep["added"]:
            self.assertNotIn(name, tools_mod.TOOLS, "注入不能撑大全局注册表")

        # 4) 联网门（060b7ea）在真握手数据上同样生效
        class _NullLLM:
            provider = "mock"

            def chat(self, messages, stream=True, **kwargs):
                raise AssertionError("不该在这里调用模型")

        instance = agent_mod.Agent(llm=_NullLLM())
        self.assertIn("mcp__playwright__browser_evaluate", instance.tools)
        self.assertFalse(instance.web_enabled)
        self.assertNotIn("mcp__playwright__browser_evaluate",
                         instance._effective_tool_names())
        instance.web_enabled = True
        self.assertIn("mcp__playwright__browser_evaluate",
                      instance._effective_tool_names())


if __name__ == "__main__":
    unittest.main()
