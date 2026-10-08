"""按内容定位仓库根，并把它放进 `sys.path`。

## 为什么要有这个模块

**今晚我在这件事上栽了三次**：

1. `tools/mutation/probe_adapter_implementations.py` —— `parents[1]` 得到 `tools/`
2. `tools/e2e_guard_preflight.py` —— `parents[2]` 得到仓库的**父目录**
3. `tools/e2e_guard_effect.py` —— `parent` 得到 `tools/`

三次的**症状都一样**：`ModuleNotFoundError: No module named 'llm'`。

而更该警惕的是**第 3 次**：第 2 次刚修完、教训刚写进注释（"硬编码 `parents[N]` 取决于文件层级"），
**下一个文件又犯了**。**教训写进注释没有阻止复发 —— 说明缺的是机制，不是文字。**

## 机制

各脚本不再自己数层级，改为 `from _repo_root import REPO`（或调用 `find_repo_root()`），
而它**按内容找**：向上找同时含 `agent.py` 与 `tools.py` 的目录。
于是文件被移动、被放到不同深度都不影响。

守卫在 `tests/test_tools_repo_root.py` —— 它扫 `tools/**` 下所有脚本，
断言**没有一个**解析到错误的位置。**那条守卫才是真正起作用的东西。**

## 用法

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from _repo_root import REPO          # 仓库根
    sys.path.insert(0, str(REPO))        # 然后才能 import llm / agent / tools
"""

from __future__ import annotations

import pathlib

#: 判定「这是仓库根」的标志性文件 —— 两个都要，避免认错目录
MARKERS = ("agent.py", "tools.py")

#: 防止在极端情况下无限上溯
_MAX_UP = 6


def find_repo_root(start: pathlib.Path | None = None) -> pathlib.Path:
    """向上找同时含 `agent.py` 与 `tools.py` 的目录。

    **按内容找，不按层级数** —— 文件被移动、被放到别的深度都不影响。
    """
    here = (start or pathlib.Path(__file__)).resolve()
    if here.is_file():
        here = here.parent
    for _ in range(_MAX_UP):
        if all((here / m).is_file() for m in MARKERS):
            return here
        if here.parent == here:
            break
        here = here.parent
    raise RuntimeError(
        "找不到仓库根：向上 %d 层内没有同时含 %s 的目录"
        % (_MAX_UP, " 与 ".join(MARKERS)))


#: 仓库根（导入即用，不必再调函数）
REPO = find_repo_root()


def ensure_on_path() -> pathlib.Path:
    """把仓库根放进 `sys.path`（幂等）。返回仓库根。"""
    import sys
    text = str(REPO)
    if text not in sys.path:
        sys.path.insert(0, text)
    return REPO