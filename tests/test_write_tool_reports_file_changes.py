"""写工具自报「改了哪些文件」——`create_file` 与 `_run_child` 那一环的守卫。

## 为什么由工具自报，而不是从 action 文本反推

实测（真跑，不是推断）：

- **解析 `path:` 会把没做的记成做了**：`apply_edit` 被「安全拦截：未授权写操作」拦下、
  `create_external_file` 被安全模式拒绝——它们的 action 文本里**同样带着 `path:`**。
- **解析不到会漏**：`python_exec` / `run_command` 真落盘了文件，路径却藏在
  `code:` / 命令行里，正则取不到。

两个方向同时成立 = 从文本推导不可行。所以只有一条路：**工具在成功那一刻自报**。

## 为什么收在 `_run_child` 的 observation 分支

`_run` 里有**两条**工具执行路径：

- `_run_batch`（原生 FC 并行批次）—— 子代理实际走这条
- 文本协议顺序路径 —— `execute_tool` 在那里被调用

我第一版把收集挂在顺序路径的 `execute_tool` 之后，**子代理永远走不到**
（插桩确认那个分支一次都没进）。而 observation 事件是**两条路径的共同出口**——
`artifacts` 早就靠它回到子代理结果里（`workflow_reflection` 能显示文件名
就是这条路）。所以收在这里。

## 守卫的分工

- **工具层**：成功时必须带 artifact，失败路径一律不带
- **接线层**：结果里必须有 `file_changes`，且它只能来自 observation 事件
- **反向守卫**：不许退回「从 action 文本解析」；不许让拦截路径也带上 artifact
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS_PY = ROOT / "tools.py"
AGENT_PY = ROOT / "agent.py"
CORE_PY = ROOT / "agent_runtime" / "preview_adapters" / "core.py"


# --------------------------------------------------------------------------
# 工具层：create_file 成功时自报
# --------------------------------------------------------------------------


def _create_file_fn() -> ast.FunctionDef:
    tree = ast.parse(TOOLS_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "create_file":
            return node
    pytest.fail("找不到 create_file")


def test_create_file_success_reports_the_path():
    """核心守卫：真创建了就把路径带出来。"""
    fn = _create_file_fn()
    returns = [n for n in ast.walk(fn)
               if isinstance(n, ast.Return) and n.value is not None]
    segs = [ast.unparse(n.value) for n in returns]
    tool_results = [s for s in segs if s.startswith("ToolResult(")]
    assert tool_results, "create_file 的成功返回没有用 ToolResult"
    joined = "\n".join(tool_results)
    assert "artifacts=" in joined or "artifacts" in joined, (
        "成功返回没有带 artifacts——下游拿不到改了哪个文件"
    )
    assert "path" in joined, "artifact 里必须有 path"


def test_the_reported_path_is_the_created_one_not_a_literal():
    """反向守卫：不能写死一个路径进去。

    写 `path="x.txt"` 也能过 `assertIn("path")`——所以要查它**取自变量**。
    """
    fn = _create_file_fn()
    joined = "\n".join(
        ast.unparse(n.value) for n in ast.walk(fn)
        if isinstance(n, ast.Return) and n.value is not None
    )
    # 成功分支里引用的应是 `rel`（相对路径，由 relpath 算出来）而不是字面量
    assert '"path": rel' in joined or "'path': rel" in joined, (
        f"artifact 的 path 取的不是实际创建出来的路径：\n{joined[:400]}"
    )


def test_failure_paths_do_not_report_a_file():
    """**最要紧的反向守卫**：失败路径一律不许带 artifact。

    被安全拦截 / 语法校验失败 / 越界写 —— 这些都**没有真的改文件**。
    一旦它们也报 artifact，`file_changes` 里就会出现「改了却没改」——
    这正是本仓库反复出现的「把没做的记成做了」。

    判据用 `ok=True` / `ok=False`，**不靠文案**：我第一版用「成功文案里有
    『已创建』」当条件，变异把文案换掉就溜过去了（实测 12 条守卫全绿）。
    文案会变，`ok` 才是这份返回值自己的声明。
    """
    fn = _create_file_fn()
    bad = []
    total = 0
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        value = node.value
        if not (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "ToolResult"):
            continue
        total += 1
        ok_value = None
        for kw in value.keywords:
            if kw.arg == "ok":
                ok_value = ast.unparse(kw.value)
        seg = ast.unparse(value)
        reports = "artifacts" in seg
        if ok_value == "False":
            if reports:
                bad.append("ok=False 却报了 artifacts：" + seg[:160])
        elif ok_value == "True" and not reports:
            bad.append("ok=True 却没有 artifacts：" + seg[:160])
    assert total == 1, (
        f"应当只有一个 ToolResult 返回（成功那一个），实际 {total} 个——"
        "失败路径若也改用 ToolResult 返回，这条要重新评估"
    )
    assert not bad, "有失败路径报了 artifacts，或成功路径没报：\n" + "\n".join(bad)


def test_the_failure_branches_are_still_plain_string_returns():
    """失败路径仍应是普通字符串（靠 `_is_failure` 识别），别被顺手改成 ToolResult。

    改成都合法，但那时必须显式 `ok=False`（上面那条正是这么钉的）。这条只为
    盯住「有人把某个失败分支单独改成 ToolResult(False, …) 却忘了带 ok」。
    """
    fn = _create_file_fn()
    plain_failures = 0
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and node.value is not None):
            continue
        seg = ast.unparse(node.value)
        if seg.startswith("ToolResult("):
            continue
        if any(w in seg for w in ("拒绝", "失败", "已存在", "缺失", "冲突", "异常")):
            plain_failures += 1
    assert plain_failures >= 5, f"失败分支只剩 {plain_failures} 个了——被大改过？"


# --------------------------------------------------------------------------
# 接线层：结果里的 file_changes 只能来自 observation 事件
# --------------------------------------------------------------------------


def _run_child_fn() -> ast.FunctionDef:
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_child":
            return node
    pytest.fail("找不到 _run_child")


def test_file_changes_is_built_from_the_observation_event():
    """反向守卫：**不许**退回「从 action 文本解析」。

    那条路实测会把被拦截的写入算进去（它们的 action 文本同样带 `path:`），
    也会漏掉 `python_exec` / `run_command` 真落盘的文件。
    """
    fn = _run_child_fn()
    body = ast.unparse(fn)
    # 收集必须发生在 observation 分支里
    assert "artifacts" in body, "_run_child 里连 artifacts 都不收了"
    # 不许出现按 action 文本解析路径的写法
    for pattern in (r"_RE_FILE_PATH", r"re\.(?:search|findall)\([^)]*path",
                    r"action\.split\(", r"_FILE_REF"):
        assert not re.search(pattern, body), (
            f"发现按 action 文本解析路径的写法（{pattern}）——"
            "那条路实测既会误报被拦截的写入，也会漏掉命令/代码里落盘的文件"
        )


def test_file_changes_only_reads_observation_artifacts():
    """收集的输入必须是 `artifact`（observation 事件带来的），不是别的。

    这里用 **AST** 定位而不是正则：正则 `elif et == "observation":(.*?)elif`
    在 `ast.unparse` 出来的文本上匹配不到（实测红过一次）——同一个形态匹配的
    老坑。AST 找比较节点，位置不会飘。
    """
    fn = _run_child_fn()
    body = ""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "observation" not in test or "==" not in test:
            continue
        seg = ast.unparse(node)
        if len(seg) > len(body):
            body = seg
    assert body, "找不到 observation 分支"
    assert re.search(r'artifact\.get\(["\']path["\']', body), (
        "observation 分支没有从 artifact 取 path"
    )
    # 必须在写集合的同一分支里，而不是在别处
    assert "_file_changes.append" in body, (
        "收集动作不在 observation 分支内——那条路径子代理走不到"
    )


def test_file_changes_lands_in_the_result_dict():
    """结果字典里必须有 `file_changes`。"""
    fn = _run_child_fn()
    keys = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
    assert "file_changes" in keys, "子代理结果里没有 file_changes 字段"


def test_it_caps_the_list():
    """上限要留着——否则一个乱写的模型能报出成千上万个文件。"""
    fn = _run_child_fn()
    body = ast.unparse(fn)
    assert re.search(r"_file_changes\)\s*<\s*\d+", body), (
        "file_changes 没有上限"
    )


# --------------------------------------------------------------------------
# 展示层：artifacts 与 file_changes 指向同一事实时要按 path 去重
# --------------------------------------------------------------------------


def test_preview_bundle_deduplicates_by_path():
    """反向守卫：两条数据源必然重叠，不去重就在界面上出现两次。

    实测：`artifacts` 与 `file_changes` 各含同一条，最终 artifacts 里
    同一个文件出现 2 次。
    """
    text = CORE_PY.read_text(encoding="utf-8")
    i = text.find("def _iter_result_artifacts")
    assert i > 0
    body = text[i:i + 2600]
    assert "_already" in body or "_seen" in body, (
        "_iter_result_artifacts 没有按 path 去重——同一个文件会出现两次"
    )
    # 去重必须在 file_changes 那个循环里
    j = body.find("file_changes")
    assert j > 0, "找不到消费 file_changes 的那段"
    assert re.search(r"if\s+not\s+_path\s+or\s+_path\s+in\s+_already", body), (
        "file_changes 那段没有跳过与 artifacts 同 path 的条目"
    )


def test_file_changes_rows_carry_no_illegal_kind():
    """反向守卫：不许填 `kind`（或填非法值）。

    `preview_adapters._adapter_inputs` 拿 `kind` 当**适配器路由键**
    （`adapter or domain or workflow["kind"]`），路由不到就整条被丢；
    而协议认可的取值只有 code/text/diff/image/… —— 我先填过 `"file"`，
    实测产物清单直接变 0 条。
    """
    fn = _run_child_fn()
    branch = ast.unparse(fn)
    m = re.search(r'_file_changes\.append\(\{(.*?)\}\)', branch, re.S)
    assert m, "找不到 _file_changes.append 的内容"
    row = m.group(1)
    assert not re.search(r'["\']kind["\']\s*:', row), (
        f"file_changes 的行里不该有 kind（它会被当适配器路由键）：\n{row[:200]}"
    )


# --------------------------------------------------------------------------
# 行为级：真跑一次
# --------------------------------------------------------------------------


def _run_real_child(tmp_path):
    """真跑一个用 create_file 的子代理。"""
    sys.path.insert(0, str(ROOT))
    import agent as agent_mod
    import tools as tools_mod
    from config import set_runtime

    set_runtime("code_root", str(tmp_path))

    class _LLM:
        provider = "deepseek"
        model = "stub"
        base_url = ""

        def __init__(self):
            self.n = 0

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            self.n += 1
            text = ("Thought: t\nAction: create_file\n"
                    "Action Input: path: guard_probe.md\nnew_text: X"
                    if self.n == 1 else "Final Answer: 完成")
            return iter([text]) if stream else text

        def count_tokens(self, text):
            return 1

    parent = agent_mod.Agent(llm=_LLM(), tool_registry=tools_mod.TOOLS)
    return parent._run_child(
        "文档撰写专员", "把说明写进 guard_probe.md",
        tool_allowlist=["create_file"], reflect=False,
    ), tmp_path / "guard_probe.md"


def test_a_real_write_is_reported(tmp_path):
    """**行为级**守卫：真写了文件，结果里就有它。

    只查源码形状不够——我第一版就挂在死代码路径上、源码看着全对，而真跑时
    `file_changes` 恒为空。这条真跑一次。
    """
    out, target = _run_real_child(tmp_path)
    assert target.is_file(), "探针自己没写成文件，后面的断言没有意义"
    changes = out.get("file_changes")
    assert isinstance(changes, list) and changes, (
        f"真写了文件却没有 file_changes：{changes!r}"
    )
    assert any(row.get("path") == "guard_probe.md" for row in changes), (
        f"file_changes 里没有那个文件：{changes!r}"
    )


def test_a_blocked_write_is_not_reported(tmp_path):
    """反向守卫：**没写成就不许报**。

    实测 `create_external_file` 在安全模式下被拒（文件没写），
    结果里 `file_changes` 为空——那正是要的。
    """
    sys.path.insert(0, str(ROOT))
    import agent as agent_mod
    import tools as tools_mod
    from config import set_runtime

    set_runtime("code_root", str(tmp_path))

    class _LLM:
        provider = "deepseek"
        model = "stub"
        base_url = ""

        def __init__(self):
            self.n = 0

        def clone(self):
            return self

        def chat(self, messages, stream=True, **kw):
            self.n += 1
            text = ("Thought: t\nAction: create_external_file\n"
                    "Action Input: path: D:/Temp/opencode/guard_ext.md\nnew_text: X"
                    if self.n == 1 else "Final Answer: 完成")
            return iter([text]) if stream else text

        def count_tokens(self, text):
            return 1

    parent = agent_mod.Agent(llm=_LLM(), tool_registry=tools_mod.TOOLS)
    out = parent._run_child(
        "文档撰写专员", "在外部写个文件",
        tool_allowlist=["create_external_file"], reflect=False,
    )
    assert not Path("D:/Temp/opencode/guard_ext.md").is_file(), (
        "探针前提不成立：外部文件真被写了，这条断言就没有意义"
    )
    assert not out.get("file_changes"), (
        f"没写成却报进了 file_changes：{out.get('file_changes')!r}"
    )


def test_the_preview_bundle_lists_the_file_exactly_once(tmp_path):
    """真跑 + 真走展示层：同一个文件在产物清单里只出现一次。"""
    sys.path.insert(0, str(ROOT))
    from agent_runtime.preview_adapters import core as preview_core

    out, target = _run_real_child(tmp_path)
    assert target.is_file()
    wf = {"workflow_id": "wf-guard", "status": "completed", "kind": "generic",
          "results": {"results": {"t1": out}}}
    bundle = preview_core.build_preview_bundle(wf)
    paths = [a.get("path") for a in (bundle.get("artifacts") or [])]
    assert paths.count("guard_probe.md") == 1, (
        f"同一个文件在产物清单里出现 {paths.count('guard_probe.md')} 次：{paths}"
    )
