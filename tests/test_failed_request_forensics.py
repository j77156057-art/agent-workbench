"""`Network.loadingFailed` 的取证信息必须够定位到「哪一跳」。

## 为什么改这个

`test_app_test.py::LivePageTests` 那条红一直**说不清是哪个请求**：

```
"failed": ["console_clean", "no_failed_requests"]
"console": [{"level":"error","text":"network: Failed to load resource: net::ERR_CONNECTION_RESET"}]
```

消息里**只有 `ERR_CONNECTION_RESET`，没有 URL**。读的人无法判断它是偶发网络抖动
还是某个稳定资源坏了 —— 这正是「失败时没有声音」。

## 两个具体缺口（都已补）

**缺口一：重定向被丢掉。**

CDP 在重定向时会**复用同一个 `requestId`** 再发一次 `requestWillBeSent`，
`request.url` 变成新地址，而**跳转前的地址在 `redirectResponse.url`**
（CDP 规范：`redirectResponse` is `Network.Response` — Redirect response data）。

旧代码只覆盖记最新 URL，所以重定向链上的失败只能指向最后一个地址 ——
而字体 CDN（`fonts.googleapis.com` → `fonts.gstatic.com`）正是重灾区。

现在记**整条链**，报告里写「经 N 跳重定向：A -> B」。

**缺口二：反查不到 URL 时静默。**

`loadingFailed` 的参数里**没有 URL**，只能靠 `requestId` 反查那张表；
查不到就成了空字符串，而代码当时什么都不说。
现在带上 `requestId`、`type`，并明说「该请求的 `requestWillBeSent` 未被记录」。

## 与「两条随机红」的关系

定性结论：那条红**不是测试的随机故障，是前端缺陷的真实暴露** ——
`frontend/src/fonts.ts`（前端侧未提交的改动）的注释写着原先四个字族依赖
`fonts.googleapis.com`、**实测本机访问会间歇性 `ERR_CONNECTION_RESET`**。
症状字面相同。

所以本篇是**取证能力**的修复，不是把红变绿 —— 红该红。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime.visual_acceptance import _DevTools  # noqa: E402


def _devtools() -> _DevTools:
    return _DevTools(connection=None)


def _fire(d: _DevTools, method: str, params: dict) -> None:
    d._handle_event({"method": method, "params": params})


# --------------------------------------------------------------------------
# 缺口一：重定向链
# --------------------------------------------------------------------------

def test_a_redirect_chain_is_reported_in_full():
    """**反向守卫**：失败发生在重定向目标上时，报告必须说清整条链。

    字体 CDN 的典型形态：`fonts.googleapis.com` → `fonts.gstatic.com`。
    CDP 为**同一跳**发两次 `requestWillBeSent`（第二次带 `redirectResponse`），
    所以这是**一跳**、两次事件 —— 我第一版误当成「两跳」，
    是先跑探针看到链尾重复才发现的。
    """
    d = _devtools()
    _fire(d, "Network.requestWillBeSent", {
        "requestId": "2", "request": {"url": "https://fonts.googleapis.com/css2?family=X"},
        "redirectResponse": {"url": "https://fonts.googleapis.com/css2?family=X"}})
    _fire(d, "Network.requestWillBeSent", {
        "requestId": "2", "request": {"url": "https://fonts.gstatic.com/s/x.woff2"},
        "redirectResponse": {"url": "https://fonts.gstatic.com/s/x.woff2"}})
    _fire(d, "Network.loadingFailed", {
        "requestId": "2", "errorText": "net::ERR_CONNECTION_RESET", "type": "Font"})

    assert d.failed_requests, "失败请求没被记下来"
    entry = d.failed_requests[0]
    assert entry["url"] == "https://fonts.gstatic.com/s/x.woff2", entry
    detail = entry["detail"]
    assert "重定向" in detail, (
        "没报重定向 —— 只给最后一个地址，读者会去查没坏的那一跳：%s" % detail)
    assert "fonts.googleapis.com" in detail, (
        "重定向链里丢了源头地址：%s" % detail)


def test_a_two_hop_redirect_chain_is_fully_recorded():
    """**正向守卫**：真的两跳时链条要有两段且不重复。

    CDP 连续重定向会发三次 `requestWillBeSent`。实现上一版误把
    `redirectResponse.url` 当来源，会报成「A -> A -> B」这种零信息量的链。
    """
    d = _devtools()
    for url in ("https://a.example/1", "https://b.example/2", "https://c.example/3"):
        _fire(d, "Network.requestWillBeSent", {
            "requestId": "8", "request": {"url": url},
            "redirectResponse": {"url": url}})
    _fire(d, "Network.loadingFailed", {
        "requestId": "8", "errorText": "net::ERR_CONNECTION_RESET", "type": "Font"})
    assert d.failed_requests[0]["url"] == "https://c.example/3"
    detail = d.failed_requests[0]["detail"]
    assert "2 跳重定向" in detail, detail
    assert "https://a.example/1 -> https://b.example/2 -> https://c.example/3" in detail, (
        "链条不完整或有重复：%s" % detail)


def test_the_redirect_source_is_the_previous_request_url():
    """**正向守卫**：来源取自**上一次记的** `request.url`。

    `redirectResponse.url` 在 CDP 里就是**本次**这条地址（已等于 `request.url`），
    拿它当来源会报出「A -> A」这种零信息量的链 —— 我第一版就是这么错的，
    被这条守卫抓住。
    """
    d = _devtools()
    _fire(d, "Network.requestWillBeSent", {
        "requestId": "7", "request": {"url": "https://a.example/one"},
        "redirectResponse": {"url": "https://a.example/one"}})
    _fire(d, "Network.requestWillBeSent", {
        "requestId": "7", "request": {"url": "https://b.example/two"},
        "redirectResponse": {"url": "https://b.example/two"}})
    _fire(d, "Network.loadingFailed", {
        "requestId": "7", "errorText": "net::ERR_CONNECTION_RESET", "type": "Font"})
    detail = d.failed_requests[0]["detail"]
    assert "https://a.example/one -> https://b.example/two" in detail, (
        "重定向链的起点不是上一次记的 URL：%s" % detail)
    assert "one -> https://a.example" not in detail, "链条里出现自我重复：%s" % detail


def test_a_plain_failure_is_not_dressed_up_as_a_redirect():
    """反向守卫：没重定向过就不许出现「跳」字样 —— 那会误导排查方向。"""
    d = _devtools()
    _fire(d, "Network.requestWillBeSent", {
        "requestId": "1", "request": {"url": "http://127.0.0.1:9/a.js"}})
    _fire(d, "Network.loadingFailed", {
        "requestId": "1", "errorText": "net::ERR_CONNECTION_RESET", "type": "Network"})
    entry = d.failed_requests[0]
    assert entry["url"] == "http://127.0.0.1:9/a.js"
    assert "重定向" not in entry["detail"], entry["detail"]


# --------------------------------------------------------------------------
# 缺口二：反查不到 URL 时不许静默
# --------------------------------------------------------------------------

def test_an_unresolvable_request_id_still_speaks_up():
    """**反向守卫**：查不到 URL 时必须说出 requestId 与 type。

    这是本篇存在的理由 —— 旧代码给一条空 URL + 一个 `ERR_CONNECTION_RESET`，
    读的人完全无从下手。
    """
    d = _devtools()
    _fire(d, "Network.loadingFailed", {
        "requestId": "99", "errorText": "net::ERR_ABORTED", "type": "Other"})
    assert d.failed_requests, "失败请求被静默丢弃了"
    detail = d.failed_requests[0]["detail"]
    assert "requestId=99" in detail, (
        "反查不到 URL 时没带 requestId —— 无从定位：%s" % detail)
    assert "type=Other" in detail, (
        "反查不到 URL 时没带 CDP 的资源类型：%s" % detail)
    assert "未捕获到 URL" in detail, (
        "没明说「这条的 requestWillBeSent 未被记录」—— "
        "读者会以为是网络问题，其实是我们的采集缺口：%s" % detail)


def test_the_error_text_itself_is_never_dropped():
    """反向守卫：CDP 给的 `errorText` 必须在，任何情况下都不许丢。"""
    d = _devtools()
    _fire(d, "Network.loadingFailed", {"requestId": "5", "errorText": "net::ERR_NAME_NOT_RESOLVED"})
    assert "ERR_NAME_NOT_RESOLVED" in d.failed_requests[0]["detail"]
    d2 = _devtools()
    _fire(d2, "Network.requestWillBeSent", {"requestId": "6", "request": {"url": "http://x/y"}})
    _fire(d2, "Network.loadingFailed", {"requestId": "6", "errorText": "net::ERR_TIMED_OUT"})
    assert "ERR_TIMED_OUT" in d2.failed_requests[0]["detail"]


def test_console_errors_still_have_their_own_path():
    """反向守卫：`Log.entryAdded` 那条路**不受**本篇影响。

    这次的红是 `console` 与 `no_failed_requests` 两条同时成立；
    若只修好 `failed_requests` 而 console 那条的信息量没变，
    排查时仍会看到一条没头没尾的 console 错误。
    """
    d = _devtools()
    _fire(d, "Log.entryAdded", {
        "entry": {"level": "error", "source": "network",
                  "text": "Failed to load resource: net::ERR_CONNECTION_RESET"}})
    assert d.console, "console 事件没被记下来"
    assert "ERR_CONNECTION_RESET" in d.console[0]["text"]


# --------------------------------------------------------------------------
# 不许靠删断言变绿
# --------------------------------------------------------------------------

def test_no_failed_requests_check_still_exists():
    """**反向守卫**：`no_failed_requests` 这条判定必须还在。

    把它去掉就能「修好」那条红 —— 但那是把证据藏起来，不是修问题。
    本篇只增强取证，不动判定。
    """
    import agent_runtime.app_test as at

    src = inspect_source(at)
    for key in ("ask_no_failed_requests", "no_failed_requests",
                "deeplink_no_failed_requests"):
        assert key in src, (
            "判定 %r 不见了 —— 确认不是为了让那条红变绿而删掉的" % key)


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)