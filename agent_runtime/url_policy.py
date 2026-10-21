"""URL 策略：区分「本机回环」与「公网」，并把公网那一侧的口子堵住。

这个仓库里已经有两套浏览器语义，**必须**分清：

* `dev_page_action` —— **本机回环**。只允许 `127.0.0.1` / `localhost` / `::1`，
  用来点自己的开发服务。它的护栏是刻意的：页面上的按钮可能真的会改你的项目，
  所以绝不放它去访问别的机器（见 `dev_server.loopback_url`）。
* `browser_action`（内置浏览器）—— **公网**。模型要能真的上网看文档、查资料、
  读一个公开的 API。

两者的边界必须互补而不是重叠：内置浏览器**只走公网**，回环地址一律拒绝。
理由不是洁癖，而是这两条路各有一类事故：

* 只放回环 → 模型看不了外网，于是「查个官方文档」只能靠 `web_fetch` 的纯文本，
  页面上的交互（点标签页、展开折叠、填搜索框）全做不到。
* 只放公网 → 模型可以用「访问内网」这条后门去打本机服务、云元数据接口、
  数据库管理台。所以公网这一侧必须**显式拒绝**内网，而不是「默认都能访问、
  靠约定别去访问内网」。

## 四道闸

1. **协议**：只允许 `http` / `https`。`file:` / `data:` / `javascript:` 直接拒。
2. **主机名**：显式拒掉内网后缀（`.local` / `.internal` / `.lan` / `.home.arpa` …）
   与主机名 `localhost`。
3. **IP 字面量**：必须是公网地址。私有段 / 回环 / 链路本地 / CGNAT /
   文档保留段 / 组播一律拒——写 `http://192.168.1.1/` 这种不该被"公网浏览器"放行。
4. **解析后复核**：域名在 DNS 里可能指向内网（`evil.test` → `127.0.0.1`）。
   所以开页面前把域名解析一次，**只要有一个 A/AAAA 落在非公网段就拒**。
   这挡不住 DNS rebinding（检查与实际连接之间存在时间差），所以还有第五道：
   **导航后复核**——每次动作后重新判断当前地址，页面把自己重定向到内网时
   立刻标记为越界（见 `page_action._off_scope`）。

## 为什么不做「允许所有 https 就够了」

因为本工具是**模型可调用**的：模型可能被页面内容里的文字引导去访问某个内网地址
（页面提示注入是真实存在的攻击面）。护栏必须假设**页面内容是敌意输入**。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse

#: 合法 scheme 的形状（RFC 3986）：字母开头，后面是字母数字 / + - . 。
#: 用它把 "javascript:alert(1)"（是 scheme）和 "example.com:8080/x"（是 host:port）
#: 区分开——两者的冒号长得一样，含义完全不同。
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*$")

#: 主机名里出现这些后缀，就是内网命名而不是公网域名。
#: `.local` 是 mDNS、`.internal` / `.lan` / `.home.arpa` 是常见内网域、
#: `.localdomain` 在部分发行版上指向本机。
INTERNAL_SUFFIXES = (
    ".local", ".localdomain", ".internal", ".lan", ".home.arpa", ".intranet",
)

#: 这些主机名本身就是本机，别名一个都不能放行。
BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

#: 云厂商的元数据地址：拿到它等于拿到宿主机的临时凭据。
#: 169.254.169.254 属于链路本地段，已经被 IP 规则挡住；这里额外列出来是因为
#: 它值得在错误文案里被点名，而不是只给一句"内网地址被拒"。
METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal", "metadata.goog"}


def _is_public_ip(value: str) -> tuple[bool, str]:
    """判断一个 IP 字面量是不是公网地址。返回 (是否公网, 原因)。"""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False, "不是合法的 IP 地址。"
    if address.is_loopback:
        return False, "回环地址属于本机。"
    if address.is_private:
        # 注意：Python 的 is_private 对 100.64.0.0/10（CGNAT）、192.0.0.0/24 等
        # 也返回 True，这正是我们要拒的。
        return False, "私有地址段（内网）。"
    if address.is_link_local:
        return False, "链路本地地址。"
    if address.is_multicast:
        return False, "组播地址。"
    if address.is_reserved:
        return False, "保留地址段。"
    if address.is_unspecified:
        return False, "未指定地址。"
    return True, ""


def _host_is_public(host: str) -> tuple[bool, str]:
    """判断主机名（可能是域名，也可能是 IP 字面量）是否属于公网。"""
    name = host.strip().strip("[]").lower()
    if not name:
        return False, "缺少主机名。"
    if name in BLOCKED_HOSTNAMES:
        return False, "localhost 属于本机。"
    if name in METADATA_HOSTS:
        return False, "云元数据地址（拿到它等于拿到宿主凭据）。"
    for suffix in INTERNAL_SUFFIXES:
        if name.endswith(suffix):
            return False, "%s 是内网域名后缀。" % suffix
    # IP 字面量：直接按地址段判。
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return True, ""
    return _is_public_ip(name)


def resolves_to_internal(host: str) -> tuple[bool, str]:
    """把域名解析一次，看是不是有地址落在非公网段。

    解析失败**不**当成拒绝：DNS 不可用时浏览器自己也会失败，那是网络问题，
    不是策略问题，把两件事混在一起会让「临时断网」看起来像「被策略拦了」。
    返回 (是否解析到内网, 原因)；解析不出来时返回 (False, "")。
    """
    name = host.strip().strip("[]").lower()
    if not name:
        return False, ""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        # 已经是 IP 字面量，交给调用方的字面量规则判。
        return False, ""
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False, ""
    for info in infos:
        address = str(info[4][0])
        ok, why = _is_public_ip(address)
        if not ok:
            return True, "%s 解析到 %s（%s）" % (name, address, why)
    return False, ""


def is_public_url(href: str) -> tuple[bool, str]:
    """判断一个地址是不是「公网地址」。返回 (是否公网, 原因)。

    这是**纯语法 + 字面量**判定，不做 DNS 解析——它要在每次动作后被反复调用，
    不能每次都去问 DNS（那会让每个动作多出几百毫秒，还可能把动作卡在超时上）。
    DNS 那一层在 `resolves_to_internal`，只在开页面前跑一次。
    """
    text = str(href or "").strip()
    if not text:
        return False, "地址为空。"
    parsed = urllib.parse.urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, "只支持 http/https，收到 %r。" % (scheme or "（无协议）")
    host = parsed.hostname or ""
    ok, why = _host_is_public(host)
    if not ok:
        return False, why
    try:
        port = parsed.port
    except ValueError:
        return False, "端口不合法。"
    if port is not None and not 1 <= port <= 65535:
        return False, "端口超出范围。"
    return True, ""


def public_url(raw: str) -> tuple[str, str]:
    """校验并归一化成一个公网地址，返回 (url, 错误信息)。

    错误信息一律**中文且可执行**：要说清是哪个地址、为什么不行、可以怎么办。
    """
    text = str(raw or "").strip().strip("'\"")
    if not text:
        return "", "缺少 url。"
    # ⚠ 协议判断必须发生在「补 http:// 前缀」**之前**。
    # 早先的写法是"没有 :// 就补前缀"，于是 `javascript:alert(1)` 被补成
    # `http://javascript:alert(1)` —— scheme 变成 http，**协议检查被整个绕过**，
    # 而 urlsplit 之后 netloc 变成 "javascript:alert(1)"，连端口解析都会炸。
    # 所以这里先看冒号前面那段是不是一个合法 scheme：是就按 scheme 判（不是
    # http/https 就拒），不是才当成 "host[:port]/path" 补前缀。
    head = text.split("/", 1)[0]
    if ":" in head:
        candidate = head.split(":", 1)[0]
        if _SCHEME_RE.match(candidate):
            scheme = candidate.lower()
            if scheme not in ("http", "https"):
                return "", ("只支持 http/https，收到 %r。file:/data:/javascript: 这类地址"
                            "不会发请求，给了也没有意义。" % scheme)
        # 不是合法 scheme —— 按 "host:port" 处理（"example.com:8080/docs" 这种）。
    elif "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return "", ("只支持 http/https，收到 %r。file:/data:/javascript: 这类地址"
                    "不会发请求，给了也没有意义。" % (scheme or "（无协议）"))
    host = (parsed.hostname or "").strip("[]")
    ok, why = _host_is_public(host)
    if not ok:
        return "", ("%s 不是公网地址（%s）。内置浏览器只访问公网；"
                    "要看本机跑着的东西，请用 dev_page_action（它走的是回环地址）。"
                    % (host, why))
    internal, detail = resolves_to_internal(host)
    if internal:
        return "", ("%s 解析到了内网地址（%s）。已拒绝——页面可以把域名指到本机或内网，"
                    "内置浏览器不替它开门。" % (host, detail))
    try:
        port = parsed.port
    except ValueError:
        return "", "端口不合法。"
    url = "%s://%s" % (scheme, host)
    if port:
        url += ":%d" % port
    url += parsed.path or "/"
    if parsed.query:
        url += "?" + parsed.query
    if parsed.fragment:
        url += "#" + parsed.fragment
    return url, ""


__all__ = ["BLOCKED_HOSTNAMES", "INTERNAL_SUFFIXES", "METADATA_HOSTS",
           "is_public_url", "public_url", "resolves_to_internal"]
