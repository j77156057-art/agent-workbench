"""工具两级分类：大类（domain）→ 小类（facet）。

## 为什么不用现有的 `group`

`ToolSpec.group` 由 `_infer_group(name)` **按名字猜**得出（`agent_runtime/tools.py:291`），
7 个取值里 `developer` 占 **74 个 / 23574 字符（全部 description 的 82%）**。
一个装了 74 个成员的「其余」不是分类 —— 它等于没分。

而 `agent.py:1611-1619` 已经记过一次教训：**名单副本会与运行期脱节，
断言副本的测试给出假保障**（`dev_apply_edits` 一例）。所以这里**不新增名单副本**，
改为在 `ToolSpec` 上加一个**声明式**字段，由注册处逐个写明，运行期只读声明。

## 为什么不按 `capability` 分

`capability` 是**权限维度**，不是**功能维度**。实测 `READ_LOCAL` 那 50 个里同时有
`dev_vue`（把 .vue 组件挂进真浏览器验接线）和 `dev_probe`（探活）——
完全不同的域。而**权限判定必须继续走 `capability`**，那是 `Agent._is_write_tool`
的依据，不能为了分类好用而改动它。

两个维度正交，各自管一件事：

* `capability` —— 能不能做（护栏查这个）
* `domain`/`facet` —— 做什么、怎么找（模型查这个）

## 数字依据（实测，非估计）

`python -c` 读 `tools.TOOLS`：111 个工具的 description 共 **28773 字符
≈ 7193 tokens**（chars/4 启发式，与 opencode `codemode` 同一算法），
每次会话都要付。而最短的 description 只有 25 字符，最长的 `dev_vue` 1167 字符 ——
**极不均匀**。

两级目录只让模型先看到「有哪些大类、每类大概管什么」，需要时再下钻到具体工具。
"""
from __future__ import annotations

from typing import Mapping

#: 大类 code -> (中文标签, 该类的定位说明)
#:
#: 定位说明是给模型看的**一��话**，要能回答「什么时候该往这类里找」，而不是罗列工具名。
DOMAINS: Mapping[str, tuple[str, str]] = {
    "files": ("文件与代码检索", "读写项目内文件、按内容或符号检索代码"),
    "verify": ("验证与对账", "跑测试、核对契约与依赖、重建索引、回滚变更集"),
    "frontend": ("前端与浏览器验收", "起 dev server、真实浏览器里点/读/截屏、视觉验收"),
    "preview_adapter": ("预览适配器", "为新文件类型或运行环境设计并激活声明式预览适配器"),
    "mcp": ("MCP 与连接器", "连接、探活、发现能力、路由工具；安装与审批受用户控制"),
    "plan": ("规划与区域", "制定计划、提出区域变更、路由任务流、交接与定价"),
    "task": ("任务与资产登记", "登记缺陷/任务、创建与审批 skill、管理资产"),
    "git": ("版本与提交", "看 diff 与 log、做提交"),
    "data": ("数据与产物", "识别/生成数据文件、制作 DOCX/PDF/PPTX/XLSX、算数值"),
    "jobs": ("进程与后台任务", "跑命令与 Python、查看与取消后台任务"),
    "web": ("外网检索与浏览", "搜索、抓取页面、在真实浏览器里打开并交互网页、字幕、翻译、天气"),
    "knowledge": ("知识与记忆", "检索已上传资料、保存与检索记忆、召回经验"),
    "game": ("游戏开发", "启停与热重载引擎、引擎截图、数值模拟、配置校验、发布前检查"),
    "eda": ("芯片与 EDA 验证", "真跑 Vivado/KLayout/仿真、解析报告为结构化事实、给三态验收结论"),
    "net": ("网络诊断", "发 HTTP 请求观察真实响应"),
    "catalog": ("工具目录", "按大类查工具清单与用法；不确定某个工具怎么调时先查它"),
}

#: 工具名 -> (大类, 小类)
#:
#: **逐个写明，不按名字推断。** 每一条都由 `tests/test_tool_domains.py` 的
#: 完整性守卫覆盖：新增工具若不声明 domain 会红（而不是静默落进「其余」）。
TOOL_DOMAINS: Mapping[str, tuple[str, str]] = {}


def _reg(domain: str, facet: str, *names: str) -> None:
    for name in names:
        if name in TOOL_DOMAINS:
            raise ValueError("工具 %r 被登记了两次" % name)
        TOOL_DOMAINS[name] = (domain, facet)


_reg("files", "读写", "create_file", "apply_edit", "dev_apply_edits",
     "create_external_file", "edit_external_file", "delete_external_file")
_reg("files", "读取", "read_file", "read_external_file", "list_dir")
_reg("files", "检索", "grep", "search_code", "dev_glob", "dev_find_references")
_reg("files", "移动", "dev_move")

_reg("verify", "测试", "dev_testrun", "dev_ci_status", "dev_list_changesets")
_reg("verify", "写后自验证", "self_verify")
_reg("verify", "契约与对账", "dev_verify_contracts", "dev_diagnostics",
     "dev_headcheck", "dev_patch", "dev_agent_log")
_reg("verify", "依赖与索引", "dev_deps", "dev_rebuild_index", "dev_probe")
_reg("verify", "回滚", "dev_rollback_changeset")

_reg("frontend", "真实浏览器", "preview_project", "dev_page_action", "dev_vue",
     "dev_apptest", "dev_media")
_reg("frontend", "开发服务器", "dev_serve")
# 视觉定位点击：DOM 锚点定位够不到的那一半（canvas/图表/位图按钮）。
# 定位与执行分成两个工具，是因为执行需要用户在审核队列点头 —— 合成一个工具，
# 模型就会以为"定位成功=可以点"。
_reg("frontend", "桌面", "dev_desktop_capture", "dev_desktop_action",
     "vision_locate_click", "vision_click")

_reg("preview_adapter", "全流程",
     "dev_preview_adapter_create", "dev_preview_adapter_approve",
     "dev_preview_adapter_refresh", "dev_preview_adapter_rollback",
     "dev_preview_adapter_test")

_reg("mcp", "连接与调用", "dev_mcp_add", "dev_mcp_call", "dev_mcp_search",
     "dev_mcp_probe")
_reg("mcp", "发现与路由", "dev_mcp_discover", "dev_mcp_discover_from_need",
     "dev_mcp_decide", "dev_route_connector", "dev_list_connectors",
     "dev_list_connector_tools")
_reg("mcp", "安装与审批", "dev_mcp_remove", "dev_install_tool", "dev_tool_install_audit")

_reg("plan", "计划与区域", "dev_plan", "dev_propose", "dev_propose_regions",
     "dev_apply_regions",
     "init_regions", "dev_add_region", "dev_list_regions", "dev_region_edit",
     "dev_region_read", "dev_region_verify")
_reg("plan", "流程与交接", "start_workflow", "dev_lanes", "dev_handoff",
     "dev_pricing", "dev_refactor")
_reg("plan", "定时与等待", "timer_set", "timer_list", "timer_cancel")
_reg("plan", "计划与决策", "plan_decision")

_reg("task", "缺陷与任务", "dev_list_bugs", "dev_update_bug", "dev_capture_bug")
_reg("task", "技能", "dev_skill_create", "dev_skill_approve", "dev_skill_reject",
     "dev_use_skill")
_reg("task", "资产", "dev_asset_get", "dev_asset_register", "search_assets")

_reg("git", "查看", "dev_git_diff", "dev_git_log")
_reg("git", "提交", "dev_commit", "dev_commit_all")

_reg("data", "文件", "inspect_data_file", "generate_data_file")
_reg("data", "产物", "create_artifact", "gen_video_prompt")
_reg("data", "计算", "calculate")

_reg("jobs", "执行", "run_command", "python_exec")
_reg("jobs", "后台任务", "dev_job_logs", "dev_job_cancel")

_reg("web", "检索与抓取", "web_search", "web_search_batch", "web_fetch",
     "web_research", "web_subtitles")
_reg("web", "其他", "web_transport", "web_weather")
# 内置浏览器：web_fetch 只给纯文本，页面上"要交互才拿得到"的内容拿不到，
# 模型于是开始猜页面写了什么 —— 那是这类任务里最贵的一种错。这个工具让模型
# 能在**公网**网页上真的点/填/等/读。⚠️ 与 dev_page_action 分工明确：那个只许
# 本机回环（点自己的开发服务），这个只许公网，两个会话域互不相通。
_reg("web", "网页交互", "browser_action")

# EDA 自成一类：这两个工具都是"拿 EDA 报告说话"的，只是一个解析已有正文、
# 一个真跑工具。原先报告门挂在"外网检索"下（因为它会抓 url），模型要在
# 几十个工具里翻才想得到它 —— 归到自己的域才找得到。
# ⚠️ 与联网开关无关：开关判的是 ToolSpec.group（eda_report_gate 仍是 "web"），
# 这里改的是给模型看的分类维度。
_reg("eda", "报告门", "eda_report_gate")
_reg("eda", "真跑工具", "eda_run_script")

_reg("knowledge", "资料检索", "search_knowledge")
_reg("knowledge", "记忆与经验", "memory_save", "memory_search", "memory_forget",
     "recall_experience")

_reg("game", "验证", "game_release_check", "game_validate_data", "game_playtest")
_reg("game", "观察与模拟", "game_screenshot", "game_simulate", "game_impact",
     "engine_logs")
_reg("game", "任务", "game_upsert_task")
# 引擎控制：此前只有 HTTP 端点（人点 UI 用的），模型只能等用户手动点完，
# 再用 game_screenshot 看一眼**已经**在跑的引擎 —— 于是"改完代码 → 画面变了
# 没有"这条闭环对模型是断的。engine_* 与 HTTP 端点走同一份底层实现。
_reg("game", "引擎控制", "engine_start", "engine_stop", "engine_reload")

_reg("net", "探测", "dev_http_request")

_reg("verify", "审批状态", "dev_approval_status")
_reg("mcp", "审批", "dev_approve")

_reg("catalog", "查目录", "tool_catalog", "tool_search")
_reg("plan", "多代理编排", "delegate", "orchestrate")


def facets_of(domain: str) -> list[tuple[str, list[str]]]:
    """某个大类下的小类清单，返回 [(小类, [工具名...])]，按小类名排序。"""
    out: dict[str, list[str]] = {}
    for name, (dom, facet) in TOOL_DOMAINS.items():
        if dom != domain:
            continue
        out.setdefault(facet, []).append(name)
    return [(facet, sorted(out[facet])) for facet in sorted(out)]


def domains_with_facets() -> list[tuple[str, list[tuple[str, list[str]]]]]:
    """全部大类与其小类，按 `DOMAINS` 的声明顺序。"""
    return [(dom, facets_of(dom)) for dom in DOMAINS if facets_of(dom)]


def tools_of(domain: str, facet: str) -> list[str]:
    """某个小类下的工具名（已排序）。"""
    return [n for n, (d, f) in sorted(TOOL_DOMAINS.items())
            if d == domain and f == facet]