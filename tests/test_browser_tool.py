"""内置浏览器的护栏测试：地址策略、访问域隔离、密码框护栏。

这些是**安全边界**，所以测的不是"能打开某个网页"（那需要真联网、也证明不了什么），
而是"该被拒的**一定**被拒"。护栏的失效模式从来不是"拦错了"，而是"漏放了一个"，
所以每条规则都要有一条对应的反向用例。
"""
import pytest

from agent_runtime import browser_tool, page_action, url_policy


# --------------------------------------------------------------------------- 地址策略

@pytest.mark.parametrize("raw", [
    "http://127.0.0.1:8000/",
    "http://localhost:5173/",
    "http://[::1]:9000/",
    "http://10.1.2.3/",
    "http://192.168.1.1/",
    "http://172.16.0.5/",
    "http://169.254.169.254/latest/meta-data/",   # 云元数据
    "http://printer.local/",
    "http://wiki.internal/",
    "http://nas.lan/",
    "file:///C:/Windows/win.ini",
    "javascript:alert(1)",
    "data:text/html,<h1>x</h1>",
])
def test_public_url_rejects_non_public(raw):
    """内网 / 本机 / 非 http 协议一律拒——这是内置浏览器唯一的上游入口。"""
    _url, error = url_policy.public_url(raw)
    assert error, "应该拒绝：%s" % raw
    assert url_policy.public_url(raw)[1]


@pytest.mark.parametrize("raw", [
    "https://example.com/",
    "https://docs.python.org/3/library/ipaddress.html",
    "http://93.184.216.34/",
])
def test_public_url_accepts_public(raw):
    url, error = url_policy.public_url(raw)
    assert not error, error
    assert url.startswith("http")


def test_public_url_normalizes_missing_scheme():
    url, error = url_policy.public_url("example.com/docs")
    assert not error, error
    assert url == "http://example.com/docs"


def test_public_url_keeps_query_and_fragment():
    url, error = url_policy.public_url("https://example.com/a?b=1#c")
    assert not error, error
    assert "?b=1" in url and "#c" in url


def test_public_url_error_names_the_host_and_the_alternative():
    """错误文案要说清是哪个地址、为什么不行、可以改用什么——否则模型只会重试。"""
    _url, error = url_policy.public_url("http://192.168.1.10:3000/")
    assert "192.168.1.10" in error
    assert "dev_page_action" in error, "应当告诉模型本机地址走哪个工具"


def test_is_public_url_rejects_empty_and_non_http():
    ok, _why = url_policy.is_public_url("")
    assert not ok
    ok, _why = url_policy.is_public_url("ftp://example.com/")
    assert not ok


# --------------------------------------------------------------------------- 工具层

def test_unknown_action_is_refused_with_the_supported_list():
    result = browser_tool.run_browser_action(".", {"action": "explode"})
    assert not result["ok"]
    assert "open|click|type" in result["text"]


def test_open_refuses_internal_url_without_starting_a_browser():
    """地址不合法时**不许**起浏览器：起浏览器是重操作，拦在前面才省。"""
    result = browser_tool.run_browser_action(".", {"action": "open", "url": "http://127.0.0.1:8000/"})
    assert not result["ok"]
    assert "公网" in result["text"]


def test_list_only_shows_public_sessions():
    result = browser_tool.run_browser_action(".", {"action": "list"})
    assert result["ok"]
    assert result["result"]["live"] == []


def test_spec_keys_include_password_switch():
    """密码框授权开关必须在工具的键表里，否则调用方传了会被当成不认识的参数。"""
    assert "allow_password" in browser_tool.SPEC_KEYS


# --------------------------------------------------------------------------- 访问域隔离

class _FakeDevtools:
    """最小可用的 devtools 替身：只回固定答案，不碰真浏览器。"""

    def __init__(self, target=None):
        self.target = target
        self.error_events = 0
        self.failure_events = 0
        self.runtime_events = 0
        self.console = []
        self.failed_requests = []
        self.calls = []

    def evaluate(self, source, *_args):
        if "window.__docmindTarget = null" in source:      # RESOLVE_JS
            return {"count": 1, "nth": 0,
                    "matched": {"tag": "input", "text": "", "w": 10, "h": 10,
                                "x": 0, "y": 0, "visible": True, "disabled": False},
                    "candidates": [], "view": {"width": 800, "height": 600},
                    "href": "https://example.com/", "title": "t", "ready": "complete"}
        if "window.__docmindTarget" in source and "el.focus" in source:  # TARGET_JS
            return self.target or {"ok": True, "tag": "input", "focused": True, "type": "text",
                                   "value": "", "x": 1, "y": 1}
        if "out.full_length" in source:                    # INSPECT_JS
            return {"count": 1, "found": True, "visible": True, "tag": "html",
                    "title": "t", "href": "https://example.com/", "text": "hello",
                    "full_length": 5}
        if "window.__docmindPage" in source:               # PROBE_JS
            return {"mutations": 0, "observer": True}
        return {}

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return {}

    def drain(self, _seconds=0.0):
        return None


def _fake_session(domain):
    import contextlib
    from pathlib import Path
    devtools = _FakeDevtools()
    session = page_action._Session("sess1", contextlib.ExitStack(), devtools,
                                   Path(".").resolve(), "https://example.com/", {}, None, None,
                                   domain=domain)
    return session, devtools


def test_sessions_of_different_domains_are_not_interchangeable(monkeypatch):
    """公网工具不能操作回环会话——否则"只许本机"这条护栏就只是个建议。"""
    session, _devtools = _fake_session("loopback")
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    with pytest.raises(page_action.PageActionError) as excinfo:
        page_action.click(".", page="sess1", selector="button", domain="public")
    assert "互不相通" in str(excinfo.value)


def test_loopback_session_still_rejects_leaving_loopback(monkeypatch):
    """回环会话跑到公网也算越界：护栏不因为"现在这样也行"就放松。"""
    session, devtools = _fake_session("loopback")
    devtools.evaluate = lambda source, *a: (
        {"count": 1, "nth": 0,
         "matched": {"tag": "button", "text": "x", "w": 10, "h": 10, "x": 0, "y": 0,
                     "visible": True, "disabled": False},
         "candidates": [], "view": {"width": 800, "height": 600},
         "href": "https://example.com/", "title": "t", "ready": "complete"}
        if "window.__docmindTarget = null" in source else
        {"count": 1, "found": True, "visible": True, "tag": "html", "title": "t",
         "href": "https://example.com/", "text": "x", "full_length": 1}
        if "out.full_length" in source else
        {"mutations": 0, "observer": True})
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    result = page_action.click(".", page="sess1", selector="button", domain="loopback")
    assert result["left_scope"] is True
    assert result["ok"] is False


def test_public_session_leaving_the_public_scope_is_reported(monkeypatch):
    """公网会话被页面重定向到内网 → 判失败并明说，越界后不许继续在内网里点。

    这条护栏的价值全在"如实报出来"上：若只说"没变化"，模型会以为页面没反应，
    于是换个选择器继续点 —— 那就等于让它在内网里操作了。
    """
    session, devtools = _fake_session("public")

    def evaluate(source, *_args):
        if "window.__docmindTarget = null" in source:
            return {"count": 1, "nth": 0,
                    "matched": {"tag": "button", "text": "x", "w": 10, "h": 10,
                                "x": 0, "y": 0, "visible": True, "disabled": False},
                    "candidates": [], "view": {"width": 800, "height": 600},
                    "href": "http://10.1.2.3/", "title": "内网", "ready": "complete"}
        if "out.full_length" in source:
            return {"count": 1, "found": True, "visible": True, "tag": "html",
                    "title": "内网", "href": "http://10.1.2.3/", "text": "x", "full_length": 1}
        if "window.__docmindPage" in source:
            return {"mutations": 0, "observer": True}
        return {}

    devtools.evaluate = evaluate
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    result = page_action.click(".", page="sess1", selector="button", domain="public")
    assert result["left_scope"] is True
    assert result["ok"] is False
    rendered = page_action.render(result)
    assert "已离开允许的访问域" in rendered


# --------------------------------------------------------------------------- 密码框护栏

def test_typing_into_a_password_box_is_refused_without_explicit_opt_in(monkeypatch):
    """往密码框写字必须显式授权：页面内容可能诱导模型"把密码填上去"。"""
    session, devtools = _fake_session("public")
    devtools.target = {"ok": True, "tag": "input", "focused": True, "type": "password",
                       "value": "", "x": 1, "y": 1}
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    with pytest.raises(page_action.PageActionError) as excinfo:
        page_action.type_text(".", page="sess1", value="hunter2", selector="input",
                               domain="public")
    assert "密码框" in str(excinfo.value)
    assert not any(method == "Input.insertText" for method, _p in devtools.calls), \
        "被拒之后不许真的把文本插进去"


def test_typing_into_a_password_box_proceeds_with_explicit_opt_in(monkeypatch):
    session, devtools = _fake_session("public")
    devtools.target = {"ok": True, "tag": "input", "focused": True, "type": "password",
                       "value": "", "x": 1, "y": 1}
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    page_action.type_text(".", page="sess1", value="hunter2", selector="input",
                           allow_password=True, domain="public")
    assert any(method == "Input.insertText" for method, _p in devtools.calls)


def test_typing_into_an_ordinary_input_is_unaffected(monkeypatch):
    """普通输入框不能被护栏误伤——否则这条规则会逼着用户每次都加参数。"""
    session, devtools = _fake_session("public")
    devtools.target = {"ok": True, "tag": "input", "focused": True, "type": "text",
                       "value": "", "x": 1, "y": 1}
    monkeypatch.setattr(page_action, "_sessions", {"sess1": session})
    page_action.type_text(".", page="sess1", value="hello", selector="input", domain="public")
    assert any(method == "Input.insertText" for method, _p in devtools.calls)
