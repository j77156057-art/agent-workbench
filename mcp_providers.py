"""注册服务适配器及其确定性匹配规则。"""
from __future__ import annotations

import re
from typing import Any, Optional


def _domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url or "")
    return m.group(1).lower() if m else ""


# provider 适配器注册表（数据驱动）：新增 provider = 加一条数据，**不改状态机**。
# 字段含义：
#   tier              该 provider 的自动化档（L0 全自动 / L1 自动填表但停在提交或挑战前，
#                     由用户点后 resume / L2 仅给指引不自动）
#   aliases           匹配 provider 名 / MCP 服务 URL / 产品名的别名（配合域名兜底）
#   domains           导航可信域（注册页主机必须落在其中，防跨域填凭证）
#   register_url      注册 / 取凭证页
#   deep_link         官方预填深链（可选；覆盖 register_url）
#   fields            确定性填表动作序列 [{selector, action, value}]
#   submit            提交按钮选择器（空串 = 不需要提交）
#   auto_submit       是否允许自动点击 submit；默认 False。§5 denylist：创建长期令牌/PAT
#                     等动作绝不自动 → 保持 False，填到提交前停下走 L1；仅「揭示/复制已存在
#                     凭证」（如 Stripe 测试键）可设 True
#   token_selectors   凭证白名单 DOM 选择器集（只读，绝不 eval 页面脚本）
#   token_pattern     凭证正则（命中才捕获，避免把噪声当凭证）
#   secret_provider   secrets_store 落库用的 provider key
#   user_prompt       L1 停等待时给用户看的提示
DEFAULT_USER_PROMPT = "如页面出现登录 / 验证 / 授权，请在浏览器中完成后点“继续”。"

PROVIDER_ADAPTERS: dict[str, dict[str, Any]] = {
    "github": {
        "tier": "L1",  # §7：创建长期令牌不可自动 → 自动填表停在提交前，用户点后 resume
        "aliases": ("github", "github.com", "github pat", "personal access token"),
        "domains": ("github.com",),
        "register_url": "https://github.com/settings/tokens/new",
        "deep_link": ("https://github.com/settings/tokens/new"
                      "?description=Agent Workbench&scopes=repo%2Cread%3Auser"),
        "fields": [{"selector": "#oauth_access_description", "action": "fill", "value": "Agent Workbench"}],
        "submit": "button:has-text('Generate token')",
        "auto_submit": False,  # 创建长期令牌属 §5 denylist：绝不自动提交，留给用户点
        "token_selectors": ["#new-oauth-token", "code.js-token-value",
                            "input[readonly][type='text']", "code"],
        "token_pattern": r"ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}",
        "secret_provider": "github",
        "user_prompt": "GitHub 需登录与两步验证，请在浏览器中完成后点“继续”。",
    },
    "figma": {
        "tier": "L1",  # §7：创建长期令牌不可自动 → 自动填表停在提交前，用户点后 resume
        "aliases": ("figma", "figma.com"),
        "domains": ("figma.com", "www.figma.com"),
        "register_url": "https://www.figma.com/settings/",
        "deep_link": "https://www.figma.com/settings/",
        "fields": [
            {"selector": "a[href*='personal-access-tokens']", "action": "click"},
            {"selector": "button:has-text('Generate new token')", "action": "click"},
        ],
        "submit": "button:has-text('Generate token')",
        "auto_submit": False,  # 创建长期令牌属 §5 denylist：绝不自动提交，留给用户点
        "token_selectors": ["input[readonly][type='text']", "code"],
        "token_pattern": r"figd_[A-Za-z0-9_-]{20,}",
        "secret_provider": "figma",
        "user_prompt": "Figma 需登录，请在浏览器中完成后点“继续”。",
    },
    "stripe": {
        "tier": "L0",
        "aliases": ("stripe", "stripe.com", "stripe test", "stripe test key"),
        "domains": ("dashboard.stripe.com", "stripe.com"),
        "register_url": "https://dashboard.stripe.com/test/apikeys",
        "deep_link": "https://dashboard.stripe.com/test/apikeys",
        "fields": [],
        "submit": "",
        "auto_submit": True,  # 仅揭示/复制已存在测试键（无创建动作），可自动
        "token_selectors": ["input[readonly][type='text']", "code"],
        "token_pattern": r"sk_test_[A-Za-z0-9]{16,}|rk_test_[A-Za-z0-9]{16,}",
        "secret_provider": "stripe",
        "user_prompt": "Stripe 测试密钥页需登录，请在浏览器中完成后点“继续”。",
    },
    "notion": {
        "tier": "L1",
        "aliases": ("notion", "notion.so"),
        "domains": ("notion.so", "www.notion.so"),
        "register_url": "https://www.notion.so/my-integrations",
        "deep_link": "https://www.notion.so/my-integrations",
        "fields": [],
        "submit": "",
        "token_selectors": ["input[readonly]", "code"],
        "token_pattern": r"secret_[A-Za-z0-9]{20,}|ntn_[A-Za-z0-9]{20,}",
        "secret_provider": "notion",
        "user_prompt": "Notion 需登录并在页面内确认集成，请在浏览器中完成后点“继续”。",
    },
    "slack": {
        "tier": "L1",
        "aliases": ("slack", "slack.com", "slack api"),
        "domains": ("api.slack.com", "slack.com", "app.slack.com"),
        "register_url": "https://api.slack.com/apps",
        "deep_link": "https://api.slack.com/apps",
        "fields": [],
        "submit": "",
        "token_selectors": ["input[readonly]", "code"],
        "token_pattern": r"xoxb-[A-Za-z0-9-]{20,}|xapp-[A-Za-z0-9-]{20,}",
        "secret_provider": "slack",
        "user_prompt": "Slack 需登录并创建工作区应用，请在浏览器中完成后点“继续”。",
    },
    "brave": {
        "tier": "L2",
        "aliases": ("brave", "brave search", "brave-search"),
        "domains": ("api.search.brave.com", "brave.com"),
        "register_url": "https://api.search.brave.com/app/keys",
        "note": "Brave Search API 需绑定信用卡，首版不做自动注册，请手动创建后回填。",
    },
    "google_drive": {
        "tier": "L2",
        "aliases": ("google drive", "googledrive", "google_drive", "gdrive"),
        "domains": ("console.cloud.google.com", "console.developers.google.com"),
        "register_url": "https://console.cloud.google.com/apis/credentials",
        "note": "Google Drive 需 Cloud Console 多步配置 + OAuth，首版不做自动注册，请手动创建后回填。",
    },
    "smithery": {
        "tier": "L2",  # Smithery 用统一 API Key 代理各 server 的第三方凭证，首版手动回填
        "aliases": ("smithery", "smithery.ai", "smithery api"),
        "domains": ("smithery.ai", "www.smithery.ai", "server.smithery.ai"),
        "register_url": "https://smithery.ai/account/api-keys",
        "note": "Smithery 用统一 API Key 代理各 server 的第三方凭证。请先登录 smithery.ai"
                "（未登录点击会回到首页），再在「Account → API Keys」创建 Key 后回填。",
    },
}

# 挑战关键字（本地确定性匹配，只用于「停-继续」判定；页面正文绝不回传模型）。
CHALLENGE_KEYWORDS: tuple[str, ...] = (
    "captcha", "recaptcha", "hcaptcha", "cloudflare", "verify you are human",
    "two-factor", "2fa", "authentication code", "one-time code", "one time code",
    "verify your email", "confirm your email", "check your email",
    "enter the code", "enter your password", "confirm your password",
    "sign in to continue", "sign in to your account", "log in to continue",
    "add a payment method", "payment method required",
)

# URL 路径标记（登录/授权跳转即视为需人工的挑战）。
CHALLENGE_URL_MARKERS: tuple[str, ...] = (
    "/login", "/signin", "/sign_in", "/session", "/auth/", "/oauth",
)


def select_provider_adapter(provider: str, cand: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    """按 provider 名 / MCP server 自有 URL 解析适配器；未收录返回 None（→ L2）。

    注意：cand["provenance"] 是**来源代码仓库**（如 github.com/...），并非凭证签发方，
    因此**不参与** adapter 匹配——否则任何以 GitHub 为仓库的候选（mcp_server_index
    多数 curated 条目 provenance.domain == "github.com"）都会被误路由到 github adapter
    （其 aliases 含 "github.com" 且排在 PROVIDER_ADAPTERS 首位），导致「去官网创建凭证」
    错误地打开 GitHub PAT 页。匹配只基于：
      (1) 凭证 provider 名（如 "smithery_api_key" / "postgres" / "github"）
      (2) MCP server 自身的 url 域（host 兜底）
    纯数据查找，无副作用。新增 provider 只需往 PROVIDER_ADAPTERS 加一条数据。
    """
    parts = [str(provider or "")]
    if isinstance(cand, dict):
        parts += [str(cand.get("url", ""))]
    hay = " ".join(parts).lower()
    for name, adapter in PROVIDER_ADAPTERS.items():
        for alias in adapter.get("aliases", ()):
            if alias and alias.lower() in hay:
                return {"name": name, **adapter}
    for token in parts:  # 域名兜底
        found = _adapter_by_url(token)
        if found:
            return found
    return None


def _adapter_by_url(url: str) -> Optional[dict[str, Any]]:
    host = _domain_of(url)
    if not host:
        return None
    for name, adapter in PROVIDER_ADAPTERS.items():
        for dom in adapter.get("domains", ()):
            if host == dom or host.endswith("." + dom):
                return {"name": name, **adapter}
    return None


def _provider_url_trusted(url: str, adapter: dict[str, Any]) -> bool:
    """导航限定：注册页主机必须落在适配器可信域（防跨域重定向填凭证）。"""
    host = _domain_of(url)
    if not host:
        return False
    return any(host == dom or host.endswith("." + dom) for dom in adapter.get("domains", ()))


