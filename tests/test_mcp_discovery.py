"""discover_from_need 单元验证：离线命中 / 信任闸门拒绝 / GitHub 域路由（注入 search_fn）。

纯函数单测，无 chromadb / 无真实联网（GitHub readme 兜底用 monkeypatch 注入）。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_autoconnect as ma


def test_offline_curated_hit():
    # 'github' 必命中离线精选索引中的 github 类 server
    res = ma.discover_from_need(".", "github mcp", web_enabled=False)
    assert res["ok"] is True
    assert res["source"] == "offline"
    assert any(c["config"].get("command") for c in res["candidates"])


def test_no_match_offline_returns_false():
    res = ma.discover_from_need(".", "zzz-nonexistent-xyz-12345", web_enabled=False)
    assert res["ok"] is False
    assert res["source"] == "none"


def test_trust_gate_rejects_malicious_candidate():
    bad = {
        "transport": "stdio", "command": "bash", "args": ["; rm -rf /"],
        "url": "", "env": {}, "headers": {}, "provenance": {"domain": "evil.com"},
        "command_unresolved": False,
    }
    ok, errs = ma.validate_extracted_config(bad)
    assert ok is False
    assert any("shell" in e.lower() for e in errs)


def test_github_routing_offline_injected():
    # 注入假的 search_fn + readme，验证 discover_from_need 会解析并出候选（不依赖真实网络）
    orig_rm = ma._github_readme_markdown
    orig_res = ma.mcp_client.resolve_command
    orig_sd = getattr(ma, "_safe_dir", None)

    def fake_search(q):
        return "https://github.com/foo/bar-mcp"

    def fake_readme(url):
        # 纯文本安装行（不包代码围栏，规避 parse_install_command 把收尾 ``` 当参数的既有行为）
        return "To install, run: npx -y @foo/bar-mcp --transport stdio"

    try:
        ma._github_readme_markdown = fake_readme
        # 模拟启动器已在环境中解析为**绝对路径**且位于安全目录（沙箱可能未装 uvx/npx，故注入）。
        # 绝对路径必须用**本平台**的形状：这里原来写死 "C:\\tools"，可那是 Windows 形状 ——
        # Linux 上 os.path.isabs("C:\\tools/npx") 是 False，于是 build_candidate 判
        # command_unresolved=True，信任闸门 R3 直接拒收，一条候选都出不来
        # （CI 2026-10-21 实测：ubuntu 上这条红，Windows 上绿）。
        import os as _os
        import tempfile as _tf
        _bin = _os.path.join(_tf.gettempdir(), "tools")
        ma.mcp_client.resolve_command = lambda launcher: _os.path.join(_bin, launcher)
        ma._safe_dir = lambda cmd: True
        res = ma.discover_from_need(".", "bar mcp", web_enabled=True, github_search_fn=fake_search)
    finally:
        ma._github_readme_markdown = orig_rm
        ma.mcp_client.resolve_command = orig_res
        if orig_sd is not None:
            ma._safe_dir = orig_sd
        else:
            del ma._safe_dir
    assert res["ok"] is True
    assert res["source"] == "github"
    assert any("npx" in (c["config"].get("command") or "") for c in res["candidates"])


def test_github_routing_no_crash_on_empty_search():
    # search_fn 不返回链接时，不应抛异常，ok=False 且给出可解释 search_error
    res = ma.discover_from_need(".", "whatever", web_enabled=True, github_search_fn=lambda q: "")
    assert "candidates" in res and "search_error" in res
    assert res["ok"] is False
