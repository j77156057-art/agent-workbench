# -*- coding: utf-8 -*-
"""HTTP 头名必须是纯 ASCII，且测试里用的头名必须在生产代码里真的被读过。

## 这条判据从哪来（2026-10-21 的真实代价）

品牌改名那次把 `DocMind` 批量替换成 `Agent 工作台`，替换是无差别的 ——
它把 HTTP **头名**也一起换了：

    headers={"X-DocMind-User": "api-user"}          # 改前
    headers={"X-Agent 工作台-User": "api-user"}      # 改后

两处后果，都是真 bug，不是风格问题：

1. **中文进了头名** —— httpx 按 latin-1 编头，`UnicodeEncodeError: 'ascii' codec
   can't encode characters in position 8-10` 直接把请求打回去。CI 上那条失败长这样：
   `AttributeError` 之外单开一条 `UnicodeEncodeError`，位置 8-10 正好 3 个字 ——「工作台」。
2. **名字换错了** —— 服务端读的是 `request.headers.get("x-docmind-user")`，
   测试发的是 `X-Agent Workbench-User`，头被静默忽略 → 服务端退回 `local-user`
   → 测试拿到的是别人的 profile。断言如果只查 `ok` 就发现不了。

## 为什么头名属于「标识符层」

本仓的改名约定是：**标识符层一律不动**（环境变量前缀、前端全局 API、HTTP 头名、
状态目录名、向量集合名、包名、DB 名）。头名是部署方与本仓之间的公开契约 ——
已经接入的部署在鉴权层发的就是 `x-docmind-user`，改名等于破坏性变更。

## 判据范围

- **头名里的非 ASCII 字符**：一律不允许（RFC 7230 的 token 字符集不含非 ASCII）。
  这条**不依赖**任何具体头名，改名换了别处也照样兜住。
- **测试里用到的 `X-DocMind-*` / `X-Agent*` 头名**：必须在生产源码里出现过同样的
  字符串字面量 —— 否则服务端根本不读它，这个头就是摆设。

## ⚠ 沙箱/副作用

本文件只读源码，不写任何文件。
"""

import io
import os
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: 引用起来的长得像头名的字符串字面量（只认引号紧贴的那一层，避免误伤行文）。
HEADER_LITERAL = re.compile(r"""['"]([Xx]-[A-Za-z0-9_\- ]*)['"]""")

#: 生产侧要覆盖到的源码位置。头名只在这些地方被读或被发。
PROD_DIRS = ["api_routes", "agent_runtime", "frontend/src"]
PROD_FILES = ["api.py", "tools.py", "cli.py", "web_export.py"]

#: 测试侧要扫描的目录。
TEST_DIRS = ["tests"]

#: 扫描时要跳过的目录（产物、依赖、状态目录、并发写者的在制品快照）。
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "__pycache__", ".pytest_cache",
    "web", "dist", "build", ".tmp", ".idea", ".docmind", ".docmind_sessions",
    ".workbuddy", ".mavis",
}

SOURCE_EXT = (".py", ".ts", ".vue", ".js", ".mjs")


def _iter_source_files(root: pathlib.Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(SOURCE_EXT):
                yield pathlib.Path(dirpath) / name


#: 本文件自己跳过 —— 判据里写着 `"x-docmind-"` / `"x-agent"` 这两个前缀字面量，
#: 不排掉就会**自己抓自己**（第一版就是这么红的：扫到本文件第 139 行）。
_SELF = pathlib.Path(__file__).name


def _literals(path: pathlib.Path):
    if path.name == _SELF:
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for match in HEADER_LITERAL.finditer(text):
        line = text[:match.start()].count("\n") + 1
        yield match.group(1), line


def _prod_header_names():
    """生产源码里出现过的头名字面量（小写归一，HTTP 头名大小写不敏感）。"""
    names = set()
    files = [REPO / f for f in PROD_FILES]
    for d in PROD_DIRS:
        files.extend(_iter_source_files(REPO / d))
    for path in files:
        if not path.is_file():
            continue
        for name, _ in _literals(path):
            names.add(name.lower())
    return names


class HeaderNamesAreAscii(unittest.TestCase):
    """头名里不许有非 ASCII —— httpx 会直接 UnicodeEncodeError。"""

    def test_no_non_ascii_header_names(self):
        offenders = []
        for d in TEST_DIRS + PROD_DIRS:
            for path in _iter_source_files(REPO / d):
                for name, line in _literals(path):
                    try:
                        name.encode("ascii")
                    except UnicodeEncodeError:
                        rel = path.relative_to(REPO)
                        offenders.append("%s:%d  %r" % (rel, line, name))
        for name in PROD_FILES:
            path = REPO / name
            if not path.is_file():
                continue
            for header, line in _literals(path):
                try:
                    header.encode("ascii")
                except UnicodeEncodeError:
                    offenders.append("%s:%d  %r" % (name, line, header))
        self.assertEqual(
            offenders, [],
            "HTTP 头名里出现非 ASCII 字符：\n  %s\n"
            "httpx 按 latin-1 编头，这会直接变成 UnicodeEncodeError；\n"
            "RFC 7230 的 token 字符集也不含非 ASCII。\n"
            "改名时头名属于标识符层 —— 必须原样保留。" % "\n  ".join(offenders))


class TestHeadersAreActuallyRead(unittest.TestCase):
    """测试里用的项目/用户/令牌头名，生产代码必须真的读它。"""

    def test_contract_headers_exist_in_production(self):
        known = _prod_header_names()
        offenders = []
        for d in TEST_DIRS:
            for path in _iter_source_files(REPO / d):
                for name, line in _literals(path):
                    low = name.lower()
                    if not (low.startswith("x-docmind-") or low.startswith("x-agent")):
                        continue
                    if low not in known:
                        rel = path.relative_to(REPO)
                        offenders.append("%s:%d  %r" % (rel, line, name))
        self.assertEqual(
            offenders, [],
            "测试里用了生产代码根本不读的头名：\n  %s\n"
            "服务端会静默忽略这个头（不是报错，是当成没传），\n"
            "于是测试可能拿到错误的身份却不自知 —— 改名时头名属于标识符层，必须原样保留。"
            % "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
