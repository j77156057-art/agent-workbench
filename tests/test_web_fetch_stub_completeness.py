"""搜索测试的桩完整性：`builtin_auto` 必须打桩**全部**后端。

## 这一族

`tools.py` 的 `_builtin_search`（约 1844 行）：

    backends = {"ddg": _ddg_search, "bing": _bing_search, "baidu": _baidu_search}
    order = [provider] if provider in backends else ["ddg", "baidu", "bing"]

provider 不是三选一时（`builtin_auto`）会**扇出到三个后端**。测试若只桩其中
一两个，剩下的就**走真实网络**，于是断言结果取决于当天网络状况。

实测：`test_ddg_success_can_merge_parallel_backends` 漏桩 `_baidu_search`，
于是

    baidu 通   -> 聚合结果多一个来源 -> 断言红
    baidu 不通 -> 只剩 ddg -> 绿

**此前「4 轮全绿」不是稳定，是那4 次 baidu 都不通。** 后来一次全量回归里它红了，
报「来源：ddg、baidu」。

## 为什么这条守卫是**结构**而不是逐句

逐条断言「这条测试补丁了哪些后端」得把断言写在测试里，改一条就得同步改断言 ——
而漏掉的恰恰是没写断言的那些条。所以这里反过来：**扫整个测试文件**，找
「用了 `builtin_auto` 却没桩满三个后端」的测试。

`test_forced_ddg_does_not_fail_over` 用 `provider="ddg"`（单后端），当前安全 ——
守卫按「是否用 builtin_auto」判定，不误伤它。但它哪天把 provider 改成
`builtin_auto` 就会变成第二个雷，守卫会立刻抓到。
"""
from __future__ import annotations

import re
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "test_web_fetch.py"
FANOUT_BACKENDS = ("_ddg_search", "_baidu_search", "_bing_search")


def _tests_needing_all_backends() -> list[tuple[str, set[str]]]:
    """扫出所有用 `builtin_auto` 的测试，及它们桩了哪些后端。"""
    source = TARGET.read_text(encoding="utf-8")
    out = []
    for block in re.split(r"\n    def ", source)[1:]:
        name = block.split("(")[0]
        if "builtin_auto" not in block:
            continue
        stubbed = {b for b in FANOUT_BACKENDS if f"tools.{b}" in block}
        out.append((name, stubbed))
    return out


def test_the_scan_actually_found_the_auto_mode_tests():
    """前置：扫描本身要有效 —— 若一条都没扫出来，上面那条守卫就是**恒真**的。

    恒真的守卫比没有守卫更危险：它让人以为机制被覆盖了。
    """
    found = _tests_needing_all_backends()
    assert len(found) >= 3, (
        f"只扫到 {len(found)} 条 builtin_auto 测试（期望 >=3）—— "
        "扫描逻辑失效，下面的守卫会变成恒真"
    )
    names = [n for n, _ in found]
    assert "test_ddg_success_can_merge_parallel_backends" in names, names


def test_every_auto_mode_test_stubs_all_three_backends():
    """核心：`builtin_auto` 的测试必须桩满三个后端。

    漏一个，那一个就走真实网络，断言结果取决于当天网络状况。
    """
    missing = {name: sorted(set(FANOUT_BACKENDS) - stubbed)
               for name, stubbed in _tests_needing_all_backends()
               if set(FANOUT_BACKENDS) - stubbed}
    assert not missing, (
        f"这些测试用了 builtin_auto 却没有桩满三个后端，漏掉的后端会走真实网络："
        f"{missing}"
    )


def test_the_fanout_order_is_still_the_one_we_assume():
    """反向守卫：本守卫**依赖**扇出顺序这个事实，它变了守卫就该更新。

    `_builtin_search` 里 provider 不是三选一时扇出 `["ddg","baidu","bing"]`。
    哪天它改成别的顺序或加了后端，这条会红 —— 那时该重新评估哪些测试易受影响。
    """
    import inspect

    import tools as T

    src = inspect.getsource(T._builtin_search)
    assert '"ddg", "baidu", "bing"' in src, (
        "扇出顺序变了 —— 本文件里按「三个后端」写的桩可能不再够用"
    )