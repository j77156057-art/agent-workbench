# -*- coding: utf-8 -*-
"""守卫：工具的「没选项目」提示必须指向**产品里真实存在**的路径。

背景（2026-10-20）：**34 个**工具在没选项目时统一回

    尚未配置代码库根目录，请先用 /api/ingest_code 指定代码目录。

但那个端点前端**根本不调** —— 项目制（顶栏项目菜单 →「打开项目」→ 登记代码目录）
已经取代了它（`ChatDock.vue:25-26` 写明："不用 /api/config 的 code_root —— 那个字段是
遗留指针，切项目后可能滞后，标错库比不标更糟"）。于是：

1. 用户在界面上**找不到这个入口**（提示指向一个产品不暴露的端点）；
2. 更糟的是那个端点会**全局切项目**（`ingest_code_directory` 是「切换项目」语义），
   照着提示调等于让 Agent 静默改掉用户正在用的项目。

所以提示必须指向**当前产品路径**：顶栏的项目菜单里「打开项目」并选择代码目录，
并明说"Agent 无法自行切换项目"（没有哪个 agent 工具能绑项目 —— 核过：TOOLS 里没有）。

本守卫钉住三件事：
- 没有任何工具/提示再让用户去调那个旧端点；
- 系统提示里连提都不该提它；
- 新文案必须存在（防"悄悄改回去"）。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent  # noqa: E402
import tools  # noqa: E402

LEGACY = "/api/ingest_code"
PRODUCT_PATH = "顶栏的项目菜单里「打开项目」"
TOOLS_PY = os.path.join(ROOT, "tools.py")


def _tools_source():
    with io.open(TOOLS_PY, encoding="utf-8") as fh:
        return fh.read()


def test_no_tool_tells_the_user_to_use_the_legacy_endpoint():
    """没有任何一处让用户去调那个已被项目制取代的端点。"""
    offenders = [ln.strip() for ln in _tools_source().splitlines()
                 if LEGACY in ln and "请先用" in ln]
    assert not offenders, "仍在让用户调旧端点：\n" + "\n".join(offenders)


def test_system_prompt_never_mentions_the_legacy_endpoint():
    """系统提示是模型的第一现场：连提都不该提那个端点。"""
    assert LEGACY not in agent.SYSTEM_PROMPT, (
        "系统提示里出现了旧端点 %s" % LEGACY)


def test_product_path_wording_is_present():
    """新文案必须真的在（防"悄悄改回去"）。"""
    src = _tools_source()
    assert src.count(PRODUCT_PATH) >= 30, (
        "产品路径文案只剩 %d 处，预期 ≥30" % src.count(PRODUCT_PATH))
    assert PRODUCT_PATH in agent.SYSTEM_PROMPT, "系统提示里没有产品路径文案"


def test_the_hint_says_the_agent_cannot_switch_projects():
    """提示必须说清"Agent 无法自行切换项目"——否则模型会反复尝试自我修复。"""
    src = _tools_source()
    assert "本工具无法替你切换项目" in src, "缺少「无法替你切换项目」这句"
    assert "Agent 无法自行切换项目" in agent.SYSTEM_PROMPT, "系统提示里缺少这句"


def test_no_frontend_pointer_to_the_legacy_endpoint():
    """前端也不能再引用那个端点（它不在产品路径上）。"""
    fe = os.path.join(ROOT, "frontend", "src")
    hits = []
    for dirpath, _dirs, files in os.walk(fe):
        for name in files:
            if not name.endswith((".ts", ".vue")):
                continue
            path = os.path.join(dirpath, name)
            with io.open(path, encoding="utf-8") as fh:
                if LEGACY in fh.read():
                    hits.append(os.path.relpath(path, ROOT))
    assert not hits, "前端仍引用旧端点：%s" % hits
