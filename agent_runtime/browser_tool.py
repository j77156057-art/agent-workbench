"""内置浏览器：让模型能真的**访问公网网页并与之交互**。

## 为什么要有它

仓库里早就有 `dev_page_action`（点/填/按键/读/等），但它**只允许本机回环地址**
——这不是偷懒，是刻意的护栏：页面上的按钮可能真的会改你的项目、发请求到你的
开发服务，所以绝不放它去访问别的机器。

代价是模型**看不了外网**：查个官方文档只能靠 `web_fetch` 拿纯文本，
页面上那些需要交互才能拿到的内容（点开折叠区、翻到第二页标签、填搜索框后看结果、
展开一张图表）全都做不到。模型于是开始"猜"页面上写了什么——那是这类任务里最贵的一种错。

所以这里补上另一半：**同一个浏览器内核、同一套动作原语、同一套证据判定**，
只把**地址域**从"本机回环"换成"公网"。两者的边界互补且不重叠：

| | `dev_page_action` | `browser_action`（本模块） |
| --- | --- | --- |
| 地址 | 只许 `127.0.0.1` / `localhost` / `::1` | 只许**公网** |
| 内网地址 | 允许（它就是干这个的） | **拒绝**（见 `url_policy`） |
| 会话域 | `loopback` | `public` |
| 用途 | 点自己的开发服务、调前端 bug | 看文档、查资料、读公开 API |

两个域的会话**互不相通**：公网工具操作不了回环会话（否则"只许本机"就只是个建议），
回环工具也操作不了公网会话（它会把外站当成本机服务去点）。

## 护栏清单

1. **地址**：`url_policy.public_url` 把关 —— 协议、主机名后缀、IP 段、DNS 解析结果，
   四道闸全过才放行（详见该模块 docstring）。
2. **访问域复核**：页面可能把自己重定向到内网。每次动作后重新判断当前地址，
   越界就把 `ok` 判 false 并要求 close（`page_action._off_domain`）。
3. **不暴露任意 JS**：页面脚本全是 `page_action` 里的固定常量，参数以 JSON 字面量传入。
4. **凭据**：往 `type=password` 的输入框写字必须显式 `allow_password: true`。
5. **会话上限**：与回环会话共用名额（同时最多 3 个），空闲 10 分钟回收，
   进程退出时兜底关闭——浏览器不会漏在机器上。

## 外部副作用

点外站的按钮**真的会生效**（登录、提交表单、下单）。这与 `run_command` 能删文件
是同一量级：DocMind 的既有立场是"agent 有这类权限，用户自己把关"。
本模块不假装能区分"安全的点击"和"危险的点击"——那种基于按钮文字的猜测既漏又烦；
能确定拦住的是**内网**与**凭据**，这两条已经拦住了。
"""
from __future__ import annotations

from typing import Any

from agent_runtime import page_action
from agent_runtime import url_policy

#: 这个模块只操作公网会话。写死在这里而不是当参数传，是为了杜绝"调用方传错了
#: 于是拿公网浏览器去点本机服务"这种事——那种错误一旦发生，护栏就形同虚设。
DOMAIN = "public"

#: 工具接受的键。与 `dev_page_action` 一致，外加两个本模块特有的：
#: `allow_password`（往密码框写字的显式授权）与 `replace`（填入前先清空）。
SPEC_KEYS = (
    "action", "page", "url", "selector", "text", "nth", "scope", "value",
    "key", "state", "mode", "attr", "needle", "timeout", "settle", "screenshot",
    "replace", "allow_password",
)

_BOOL_WORDS = {"1", "true", "yes", "on", "y"}


def _one(fields: dict[str, Any], key: str, default: Any = "") -> Any:
    value = fields.get(key)
    if value is None:
        return default
    text = str(value)
    return text.splitlines()[0].strip() if text.strip() and "\n" not in text.strip() else text.strip()


def _flag(fields: dict[str, Any], key: str, default: bool) -> bool:
    text = _one(fields, key, "").strip().lower()
    if not text:
        return default
    return text in _BOOL_WORDS


def run_browser_action(root: str, spec: dict[str, Any]) -> dict[str, Any]:
    """执行一次内置浏览器动作，返回 `{"ok", "text", ...}`。

    `text` 是给模型看的渲染结果（与 `dev_page_action` 同一套 `page_action.render`），
    `screenshot` 是截图相对路径（由调用方转成图片回传）。
    """
    fields = dict(spec or {})
    action = str(_one(fields, "action") or "open").strip().lower()
    page = _one(fields, "page")
    selector = _one(fields, "selector")
    text = _one(fields, "text")
    scope = _one(fields, "scope")
    nth = _one(fields, "nth")
    settle = _one(fields, "settle", 600)
    timeout = _one(fields, "timeout", 4)

    try:
        if action == "open":
            result = page_action.open_external_page(root, url=_one(fields, "url"),
                                                     domain=DOMAIN)
        elif action == "click":
            result = page_action.click(root, page=page, selector=selector, text=text,
                                       nth=nth, scope=scope, settle=settle,
                                       screenshot=_flag(fields, "screenshot", True),
                                       domain=DOMAIN)
        elif action == "type":
            result = page_action.type_text(root, page=page, value=fields.get("value") or "",
                                           selector=selector, text=text, nth=nth, scope=scope,
                                           replace=_flag(fields, "replace", True),
                                           settle=settle,
                                           screenshot=_flag(fields, "screenshot", False),
                                           allow_password=_flag(fields, "allow_password", False),
                                           domain=DOMAIN)
        elif action == "press":
            result = page_action.press_key(root, page=page, key=_one(fields, "key"),
                                           selector=selector, settle=settle,
                                           screenshot=_flag(fields, "screenshot", False),
                                           domain=DOMAIN)
        elif action == "wait":
            result = page_action.wait_for(root, page=page, selector=selector,
                                          state=_one(fields, "state", "present"),
                                          value=fields.get("value") or "",
                                          timeout=timeout, domain=DOMAIN)
        elif action == "read":
            result = page_action.read_page(root, page=page, selector=selector,
                                           mode=_one(fields, "mode", "text"),
                                           attr=_one(fields, "attr"), nth=nth,
                                           needle=_one(fields, "needle"), domain=DOMAIN)
        elif action == "locate":
            result = page_action.locate(root, page=page, selector=selector, text=text,
                                        nth=nth, scope=scope, domain=DOMAIN)
        elif action in ("list", "status"):
            result = {"action": "list",
                      "live": [row for row in page_action.live_pages()
                               if row.get("domain") == DOMAIN]}
        elif action == "close":
            result = page_action.close_page(page, domain=DOMAIN)
        else:
            return {"ok": False,
                    "text": ("browser_action 不支持 action: %s"
                             "（可用：open|click|type|press|wait|read|locate|list|close）。" % action)}
    except page_action.PageActionError as exc:
        return {"ok": False, "text": str(exc), "action": action, "page": page}
    except Exception as exc:  # noqa: BLE001 - agent 工具层不该把异常甩给模型
        name = type(exc).__name__
        if name == "VisualAcceptanceError":
            return {"ok": False, "text": "浏览器动作无法执行：%s" % str(exc)[:300],
                    "action": action, "page": page}
        return {"ok": False, "text": "browser_action 失败：%s: %s" % (name, str(exc)[:240]),
                "action": action, "page": page}

    rendered = page_action.render(result)
    # 越界（被重定向到内网）时必须让"ok"为假：这次动作的结论不能算数。
    ok = bool(result.get("ok", True)) and not result.get("off_scope") and not result.get("left_scope")
    return {"ok": ok, "text": rendered, "action": result.get("action") or action,
            "page": result.get("page") or page,
            "screenshot": str(result.get("screenshot") or ""),
            "result": result}


def check_url(url: str) -> tuple[bool, str]:
    """只做地址校验，不开浏览器。给上层做预检/解释用。"""
    _target, error = url_policy.public_url(url)
    return (not error), error


__all__ = ["DOMAIN", "SPEC_KEYS", "check_url", "run_browser_action"]
