"""pytest 根 conftest：让 freeze_guard **默认**生效。

## 为什么需要这个文件

`tools/pytest_freeze_guard.py` 守着一条纪律：**跑测期间不许改被测文件**。
但它原本要人记得手动 `-p pytest_freeze_guard` + `PYTHONPATH`—— 结果
2026-10-10~11 我两次「全量跑到一半改 `config.py`/`api.py`」，它一声没响，
一份被污染的测量结果看起来完全合格。

**任何需要"记得"的机制都不是机制。** 所以在这里声明一次，让它每次
`pytest tests/` 都自动生效。

## 为什么是根 conftest 而不是 `pytest.ini` 的 addopts

`pytest.ini` 的 `-p` 只吃**可 import 的模块名**，而 `tools/` 不是包。
放根 `conftest.py` 里声明 `pytest_plugins` 是 top-level conftest 允许的用法，
且**单一来源** —— 守卫本体仍只在 `tools/pytest_freeze_guard.py` 一份，
不会出现两份漂移。

## 副作用（已确认可接受）

子进程里再跑 pytest 时也会加载守卫。那正是我们想要的：连子进程的测量
也受保护。实测 `tests/` 全量在该加载下零误报。
"""

import pathlib
import sys

_TOOLS = str(pathlib.Path(__file__).resolve().parent / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

# top-level conftest 里声明插件是官方允许的用法。
pytest_plugins = ("pytest_freeze_guard",)
