"""`degraded` 的任务不算「已完成」——判据与四个调用点的守卫。

## 这条判据在防什么

`agent.py` 在三处给结论打 `degraded`（安全网兜底的 final、连续空响应、
清洗残句），标的是「这段结论**不是模型自己的答案**」。但 `status` 仍是
`"ok"`。于是只看 `status` 的判据会把这种任务算成成功完成——时间线是绿的、
`n_ok` 是过的、报告写着「全部完成」，而实际上一个子问题都没答。

## 守卫的分工

- 判据本身：真 ok 必须仍然算 ok（防「一律不算」）
- 判据本身：**不许顺手改 `status`**（防「不算的同时改成 failed」让 `n_failed` 撒谎）
- 四个调用点：用 `ast` 断言源码里**没有**裸的 `status == "ok"` 判定
  （防「只改一处、另三处照旧」——这类漏改最阴，因为单测照样全绿）
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_runtime.game_workflow import counts_as_ok

SRC = Path(__file__).resolve().parents[1] / "agent_runtime" / "game_workflow.py"


# --------------------------------------------------------------------------
# 判据本身
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item, expected, why",
    [
        ({"status": "ok"}, True, "正常的 ok 当然算完成"),
        ({"status": "ok", "degraded": False}, True, "degraded 显式 False 不算降级"),
        ({"status": "ok", "degraded": True}, False, "核心：降级的结论不算完成"),
        ({"status": "ok", "degraded": "yes"}, False, "真值型都算降级，不能只认 True"),
        ({"status": "ok", "degraded": 1}, False, "非零数值同样算降级"),
        ({"status": "failed"}, False, "失败当然不算完成"),
        ({"status": "failed", "degraded": True}, False, "失败且降级"),
        ({}, False, "空结果不算完成"),
        ({"status": None}, False, "status 缺失不算完成"),
        (None, False, "None 不算完成"),
        ("not-a-dict", False, "非字典不能冒充结果"),
        ([], False, "空列表不能冒充结果"),
    ],
)
def test_degraded_is_never_counted_as_complete(item, expected, why):
    assert counts_as_ok(item) is expected, why


def test_it_does_not_rewrite_status_to_failed():
    """反向守卫：判据只能「回答」，不能「改写」。

    偷懒修法会把 `degraded` 的结果顺手改成 `status="failed"`——那样
    `n_failed` 就开始撒谎：一个没答出来的子问题被记成一次失败。这里的
    `counts_as_ok` 是纯函数，它没有、也不该有能力改调用方的字典。
    """
    item = {"status": "ok", "degraded": True}
    before = dict(item)
    assert counts_as_ok(item) is False
    assert item == before, "判据修改了传入的字典——它必须只读"


def test_a_real_ok_task_is_still_counted():
    """反向守卫：防「一律不算」。

    把 `degraded` 判据写成 `return False`（或者把 status 也一起毙掉），
    上面那条「正常 ok」和这条会一起红。这条独立再钉一遍，因为它是业务底线：
    正常完成的任务绝不能被误伤。
    """
    assert counts_as_ok({"status": "ok"}) is True
    assert counts_as_ok({"status": "ok", "degraded": False, "steps": 12}) is True


# --------------------------------------------------------------------------
# 四个调用点：源码层不许再有裸判定
# --------------------------------------------------------------------------


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _judgement_calls(func: ast.AST) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "counts_as_ok"
    ]


def _own_source() -> str:
    return SRC.read_text(encoding="utf-8")


def _site_texts() -> list[str]:
    """按判据所在的具体结构取文本，而不是按函数圈定。

    圈函数会误伤：`_run_task_dag_langgraph` 里还有依赖门（`3473`）和
    调度器门（`3570`），`_execute_unleased` 里还有整体成败的
    `{"ok","deduped"}` + optional + remediated 判定——那些是别的族
    （判「这次运行整体成功吗」，要算补救豁免和可选项）。混进来会顺手
    改掉它们的行为。
    """
    tree = ast.parse(_own_source())
    texts: list[str] = []
    for node in ast.walk(tree):
        # dict 字面量里的 "n_ok"
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "n_ok":
                    texts.append(ast.unparse(value))
        # 赋给局部名 ok 的 all(...) 汇总
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "ok" in targets:
                texts.append(ast.unparse(node.value))
        # emit("wave_complete", {...}) 里的 status
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id != "emit" or not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and first.value == "wave_complete"):
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Dict):
                continue
            for key, value in zip(node.args[1].keys, node.args[1].values):
                if isinstance(key, ast.Constant) and key.value == "status":
                    texts.append(ast.unparse(value))
    return texts


def test_each_single_task_judgement_site_calls_the_single_judgement():
    """**最关键的一条**：四处「单任务算不算完成」的判定都必须走判据。

    只改一处调用点是最阴的失败模式：单测照样全绿（因为单测直接调判据），
    但线上另几处仍在把降级任务算成完成。这条按结构定位（`n_ok` / 赋给 `ok`
    的汇总 / `wave_complete` 的 `status`），既能全覆盖这四处，又不会把
    别的族的判定误抓进来。
    """
    sites = _site_texts()
    assert len(sites) == 4, (
        "期望 4 个判定点（wave_complete / 整体 ok / n_ok / 图报告 n_ok），"
        f"实际找到 {len(sites)} 个：\n" + "\n".join(sites)
    )
    for text in sites:
        assert "counts_as_ok" in text, f"这个判定点没走判据：\n  {text}"
        assert not ('"ok"' in text or "'ok'" in text) or "counts_as_ok" in text, (
            f"这个判定点仍在裸比 status：\n  {text}"
        )


def test_no_n_ok_counts_status_without_the_degraded_guard():
    """`n_ok` 是给用户看的「完成了几个」——它必须排除降级。

    反向守卫：如果有人只改 `wave_complete` 而漏了 `n_ok`，界面上的
    「n/m 完成」仍会把降级任务算进去。这条独立钉住每个 `n_ok`。
    """
    tree = ast.parse(_own_source())
    n_ok_values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "n_ok":
                    n_ok_values.append(ast.unparse(value))
    assert n_ok_values, "找不到任何 n_ok 计算——守卫空转了"
    for text in n_ok_values:
        assert "counts_as_ok" in text, f"n_ok 仍在裸判 status==ok：\n  {text}"


def test_the_whole_module_has_exactly_four_call_sites():
    """全模块恰好四处调用——新增或漏改调用点都要同步更新判据与守卫。"""
    tree = ast.parse(_own_source())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "counts_as_ok"
    ]
    assert len(calls) == 4, (
        "期望 4 个调用点，实际 "
        f"{len(calls)} 个——新增或漏改调用点都要同步更新判据与守卫"
    )


def test_other_families_are_left_alone():
    """反向守卫：这一族之外的口径**不许**被顺手改掉。

    整体成败的判定口径是 `{"ok", "deduped"}` + optional + remediated
    （补救豁免、可选项）。它和「单任务算不算完成」不是一回事：那个要算
    豁免。如果有人把 `counts_as_ok` 硬塞进去，会连带改掉补救豁免的行为。
    这条把「别人的口径」钉住，改动越界时立刻响。
    """
    source = _own_source()
    assert '"status") in {"ok", "deduped"}' in source or (
        "status\") in {\"ok\", \"deduped\"}" in source
    ), "整体成败的 {'ok','deduped'} 口径不见了——它不属于本次改动，请单独决定"


def test_judgement_lives_in_the_runtime_not_in_a_test_double():
    """判据必须在生产代码里，不能是测试里现拼的替身。"""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "counts_as_ok" in names
