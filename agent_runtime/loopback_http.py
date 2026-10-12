"""回环 HTTP 请求：**显式绕开环境代理**。

## 为什么需要这个模块

`urllib.request.urlopen()` 默认读 `HTTP_PROXY` / `http_proxy` 等环境变量
（内部走 `getproxies()`）。本仓所有打回环的 HTTP —— 服务就绪探针、
Chrome DevTools 协议、Ollama `/api/ps`、组件守卫的静态服务、`http_probe` ——
目标地址都由各自的回环校验保证是 `127.0.0.1` / `localhost` / `::1`。
**对回环地址走代理是纯粹的错**，而且它坏得很安静：

* 目标**活着**时代理成功转发，看起来完全正常（最危险的情况）；
* 目标**没起来**时代理替它应答（实测拿到 502），于是「连不上」被读成
  「服务端应答了」—— **fail-closed 语义被静默反转**。

2026-10-12 实测（沙箱注入 `HTTP_PROXY=http://127.0.0.1:49884`）：
`http_probe('http://127.0.0.1:1/')` 返回 `status=502` / `server_answered=True`
/ `error=''`；禁用代理后是 `URLError WinError 10061` / `error='连不上…'`。
同一环境里另起一个本地服务器，`urlopen` 也能拿到正常响应 —— 说明请求**确实过了代理**，
只是代理转发成功了。所以"能通"不能证明"没走代理"。

## 用法

    from . import loopback_http
    with loopback_http.urlopen(request, timeout=5) as response:
        ...

异常类型与 `urllib.request.urlopen` 完全一致（`HTTPError` / `URLError`），
调用方现有的 `except` 分支无需修改；返回的响应对象同样是上下文管理器。
"""
from __future__ import annotations

import urllib.request

__all__ = ["loopback_opener", "urlopen"]


def loopback_opener() -> urllib.request.OpenerDirector:
    """直连回环的 opener：显式禁用环境代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def urlopen(request, *, timeout: int = 15):
    """对回环地址发一次请求，**绝不走环境代理**。

    入参兼容 `urllib.request.urlopen(request, timeout=...)`：
    `request` 可以是 `urllib.request.Request`，也可以是 URL 字符串。
    """
    return loopback_opener().open(request, timeout=timeout)
