"""离线精选 MCP server 索引（v1，零网络命中层）。

用途：auto_connect 在联网抓取前先查本索引。用户输入常见 server 名（github / fetch /
postgres / slack …）直接返回官方安装命令，彻底绕开"官方文档是 JS 渲染壳、urllib 抓不到
正文"的问题。命令均取自各 server 官方仓库 README 并经人工核对；新增 server 在此追加一条即可。

字段约定：
- name / aliases：匹配关键词（小写）；aliases 命中得分更高。
- transport：stdio（v1 仅收录命令型，http 型交由 live 路径发现）。
- command：裸启动器 token（npx/uvx/docker…），过 R3 白名单，不解析绝对路径。
- args / env / headers：env 值用 @secret:<provider> 路由 secrets_store，绝不留明文密钥。
- official：官方仓库/文档 URL，仅用于 provenance（domain 落在 TRUSTED_SOURCE_DOMAINS → trusted）。
- note：给用户的友好提示（需填目录/需存凭证等），前端可展示。
"""
from __future__ import annotations

import re
from typing import Any

_SERVER_INDEX: list[dict[str, Any]] = [
    {
        "name": "github",
        "aliases": ["github", "gh", "modelcontextprotocol/server-github", "@modelcontextprotocol/server-github"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "@secret:github"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 GITHUB_PERSONAL_ACCESS_TOKEN（在「凭证」存为 github 后自动填入）",
    },
    {
        "name": "git",
        "aliases": ["git", "mcp-server-git"],
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-git"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "fetch",
        "aliases": ["fetch", "web fetch", "web-fetch", "mcp-server-fetch"],
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "filesystem",
        "aliases": ["filesystem", "file system", "files", "@modelcontextprotocol/server-filesystem"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需补一个目录参数（如要暴露的项目根目录）",
    },
    {
        "name": "postgres",
        "aliases": ["postgres", "postgresql", "pg", "@modelcontextprotocol/server-postgres"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres"],
        "env": {"DATABASE_URL": "@secret:postgres"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 DATABASE_URL（在「凭证」存为 postgres 后自动填入）",
    },
    {
        "name": "sqlite",
        "aliases": ["sqlite", "mcp-server-sqlite"],
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-sqlite"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需补一个 db 文件路径参数",
    },
    {
        "name": "memory",
        "aliases": ["memory", "@modelcontextprotocol/server-memory"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "sequentialthinking",
        "aliases": ["sequentialthinking", "sequential thinking", "sequential-thinking", "@modelcontextprotocol/server-sequentialthinking"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequentialthinking"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "time",
        "aliases": ["time", "worldclock", "mcp-server-time"],
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "brave-search",
        "aliases": ["brave", "brave-search", "brave search", "@modelcontextprotocol/server-brave-search"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "env": {"BRAVE_API_KEY": "@secret:brave"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 BRAVE_API_KEY（在「凭证」存为 brave 后自动填入）",
    },
    {
        "name": "puppeteer",
        "aliases": ["puppeteer", "@modelcontextprotocol/server-puppeteer"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "",
    },
    {
        "name": "playwright",
        "aliases": ["playwright", "@playwright/mcp"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@playwright/mcp"],
        "env": {},
        "official": "https://github.com/microsoft/playwright-mcp",
        "note": "本机需已安装 playwright 浏览器",
    },
    {
        "name": "slack",
        "aliases": ["slack", "@modelcontextprotocol/server-slack"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "env": {"SLACK_BOT_TOKEN": "@secret:slack", "SLACK_TEAM_ID": "@secret:slack_team"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 SLACK_BOT_TOKEN 与 SLACK_TEAM_ID（分别存为 slack / slack_team）",
    },
    {
        "name": "google-drive",
        "aliases": ["gdrive", "google drive", "google-drive", "@modelcontextprotocol/server-gdrive"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gdrive"],
        "env": {"GOOGLE_CREDS_JSON": "@secret:gdrive"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 GOOGLE_CREDS_JSON（存为 gdrive）",
    },
    {
        "name": "notion",
        "aliases": ["notion", "@modelcontextprotocol/server-notion"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-notion"],
        "env": {"OPENAPI_MCP_HEADERS": "@secret:notion"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 OPENAPI_MCP_HEADERS（存为 notion，值为 {\"Authorization\":\"Bearer <token>\"} 的 JSON）",
    },
    {
        "name": "sentry",
        "aliases": ["sentry", "@modelcontextprotocol/server-sentry"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sentry"],
        "env": {"SENTRY_DSN": "@secret:sentry"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 SENTRY_DSN（存为 sentry）",
    },
    {
        "name": "google-maps",
        "aliases": ["google maps", "google-maps", "maps", "@modelcontextprotocol/server-google-maps"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-google-maps"],
        "env": {"GOOGLE_MAPS_API_KEY": "@secret:gmaps"},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 GOOGLE_MAPS_API_KEY（存为 gmaps）",
    },
    {
        "name": "everything",
        "aliases": ["everything", "@modelcontextprotocol/server-everything"],
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "env": {},
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "官方示例 server，用于联调试连",
    },
    {
        "name": "docker",
        "aliases": ["docker", "mcp/docker"],
        "transport": "stdio",
        "command": "docker",
        "args": ["run", "-i", "--rm", "mcp/docker"],
        "env": {},
        "official": "https://github.com/ckreiling/mcp-server-docker",
        "note": "需本机 docker 守护进程在运行",
    },
    {
        "name": "aws-kb",
        "aliases": ["aws", "knowledge base", "knowledge-base", "aws-kb", "@modelcontextprotocol/server-aws-kb-retrieval"],
        "transport": "stdio",
        "command": "uvx",
        "args": ["@modelcontextprotocol/server-aws-kb-retrieval"],
        "env": {
            "AWS_ACCESS_KEY_ID": "@secret:aws",
            "AWS_SECRET_ACCESS_KEY": "@secret:aws_secret",
            "AWS_REGION": "",
        },
        "official": "https://github.com/modelcontextprotocol/servers",
        "note": "需 AWS 密钥（存为 aws / aws_secret）与 AWS_REGION",
    },
]


def match_curated_server(query: str, limit: int = 3) -> list[dict[str, Any]]:
    """按查询匹配精选索引，返回打分降序的 entry 列表（最多 limit 条）。

    命中规则：name 完整出现 +5；alias 等于查询或等于某个分词 +4；alias 子串出现 +2。
    无任何命中返回 []。
    """
    q = (query or "").lower()
    if not q.strip():
        return []
    tokens = [t for t in re.split(r"[^a-z0-9+@._/-]+", q) if t]
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in _SERVER_INDEX:
        score = 0
        name = entry["name"].lower()
        if name in q:
            score += 5
        for alias in entry.get("aliases", []):
            al = alias.lower()
            if al == q or al in tokens:
                score += 4
            elif al in q:
                score += 2
        if score <= 0:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:limit]]


def curated_entry_to_config(entry: dict[str, Any]) -> dict[str, Any]:
    """把一条索引 entry 转成候选 config（含 provenance）。command 保持裸启动器 token。"""
    official = entry.get("official", "") or ""
    return {
        "transport": entry.get("transport", "stdio"),
        "command": entry.get("command", ""),
        "args": list(entry.get("args", []) or []),
        "url": entry.get("url", "") or "",
        "env": dict(entry.get("env", {}) or {}),
        "headers": dict(entry.get("headers", {}) or {}),
        "provenance": {
            "url": official,
            "domain": _domain_of(official),
            "server_name": entry.get("name", "") or "",
            "note": entry.get("note", "") or "",
            "curated": True,
        },
        "command_unresolved": False,
    }


def _domain_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)/?", url or "")
    return m.group(1).lower() if m else ""


def list_curated_names() -> list[str]:
    """返回全部收录的 server 名（调试/前端提示用）。"""
    return [e["name"] for e in _SERVER_INDEX]
