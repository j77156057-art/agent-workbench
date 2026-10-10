"""pytest 插件：检测「测试运行期间被测文件被改写」。

## 为什么需要它

实测事故（2026-10-10，见 docs/measure-freeze-discipline.md）：

一轮全量报 27 failed，看着像新缺陷。实际上**一条真缺陷都没有** ——
是**我在那轮跑的过程中**改写了 `agent.py`。

机制：`inspect.getsource` 靠**导入时烧进字节码的 `co_firstlineno`** 定位。
文件之后再改，那个行号不会更新 -> 源码扫描类测试从错位处切出别人的函数体：

    E   assert 'format_stalls' in '                try:\\n'

## 本插件做什么

**运行开始**时记下每个已导入模块对应源文件的大小+mtime+行数；
**每条用例结束时**比对。任何一个变了 -> 立刻报错，并说清是哪个文件。

不靠「我保证没改」这种自觉 —— **把污染变成会自己叫的失败**。

## 为什么是插件而不是 conftest.py

`tests/conftest.py` 是别人在途的文件（realtime 隔离层）。插件走
`-p` 显式加载，不进版本库、零交集。

用法：

    $env:PYTHONPATH='<repo>\\tools'
    pytest ... -p pytest_freeze_guard

⚠ 必须走 `PYTHONPATH` + 裸模块名。`-p tools.pytest_freeze_guard` 导入不了
（`tools/` 故意不是包，没有 `__init__.py` —— 加了会改变那个目录的性质），
而 `-p .\\tools\\xxx.py` 会被 pytest 判成相对导入而报错。

## 实测

* 四关自验（`tools/verify_freeze_guard.py`）：正例抓到 `agent.py` 并定位到
  具体用例；无改动不误报；**删掉核心比对就抓不到** —— 证明真在干活。
* 真实场景（`tools/verify_freeze_real.py`）：与那 26 条源码扫描测试同跑时
  正确报出污染。
* 真实批量（2026-10-10）：30 个测试文件、**595 passed、10 分 19 秒**，
  冻结工作树全程未碰任何文件 -> **零误报**。

## 自验时的两个坑（都记在这，免得重犯）

1. **只在 `sessionstart` 登记 = 漏掉之后才 import 的模块。**
   `pytest_sessionstart` 早于收集，那一刻大部分被测模块还没加载，
   基线里根本没有 `agent.py`。改成每条用例结束时**懒补登记**
   （按当时指纹记，那一刻它确实没被改过，所以不算污染）。

2. **第一版验证脚本自己恒真。** 它只看 stdout 里有没有「测量被污染」——
   插件**压根没加载成功**时（`ModuleNotFoundError`），自然也没有那串字，
   于是 A 关判 False、B/C 关判 True，看起来「三关都过」，其实是
   「守卫根本没运行」。加**加载自检关**（stdout 必须出现「已盯 N 个」）
   才暴露。

同型事故见 `docs/transform-script-false-success.md`。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest

#: 报告落盘位置（测完直接看这个文件）
REPORT = pathlib.Path(os.environ.get(
    "DOCMIND_FREEZE_REPORT", r"D:\Temp\opencode\freeze_violations.json"))

#: 会话开始时记下的快照：路径 -> (大小, mtime_ns, 摘要)
_BASELINE: dict[str, tuple] = {}
_VIOLATIONS: list[dict] = []
_armed = False


def _fingerprint(path: pathlib.Path):
    """文件指纹：大小 + mtime_ns + 内容摘要。

    摘要用 sha1 前 8 位 —— 只为区分「改了」与「没改」，不是安全用途。
    """
    try:
        st = path.stat()
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    return (st.st_size, st.st_mtime_ns,
            hashlib.sha1(raw).hexdigest()[:8])


def _tracked_modules():
    """所有已导入模块的源文件（只算仓库内的 .py）。

    ⚠ 必须取**实际被 import 的**文件，不是扫目录 —— 目录里有大量
    没参与测试的文件，扫目录会把「我顺手改了个无关脚本」也报成污染。
    """
    import sys
    root = pathlib.Path(__file__).resolve().parents[1].resolve()
    out = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        p = pathlib.Path(f)
        try:
            p = p.resolve()
        except (OSError, ValueError):
            continue
        if p.suffix != ".py":
            continue
        # ⚠ 必须是「仓库根**本身**或其子目录」。`root in p.parents` 在
        # root 是相对路径、或仓库被软链时会失效，把 site-packages 也放进来
        # （实测 490 个里有大半是第三方包）。这里用 is_relative_to 严格判断。
        try:
            if not p.is_relative_to(root) or p == root:
                continue
        except (AttributeError, ValueError):
            continue
        out.append(p)
    return out


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    global _BASELINE, _armed
    _VIOLATIONS.clear()
    if REPORT.exists():
        REPORT.unlink()
    _BASELINE = {}
    for p in _tracked_modules():
        fp = _fingerprint(p)
        if fp:
            _BASELINE[str(p)] = fp
    # ⚠ 不要因为「启动时一条都没盯上」就自我关闭 —— 那正是实测踩到的：
    # sessionstart 早于收集，此刻常常一个被测模块都还没 import。
    # 真正的启用信号是「守卫代码在跑」，由 teardown 里的懒补登记兜底。
    _armed = True
    if os.environ.get("DOCMIND_FREEZE_VERBOSE"):
        print("\n[freeze-guard] 已盯 %d 个被测源文件" % len(_BASELINE))


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    """每条用例跑完就查 —— 越早发现，污染的用例越少。

    ⚠ 先补登记再比对。`pytest_sessionstart` 跑在**收集之前**，那一刻大部分
    被测模块还没被 import（实测：只 import 了 pathlib 时，基线里根本没有
    `agent.py`）。不补登记就永远看不见之后才加载的模块。

    补登记记的是**当时**的指纹 —— 那时它确实没被改过，所以不算污染。
    """
    if not _armed:
        return
    # ---- 补登记：之后才 import 的被测模块 ----
    for p in _tracked_modules():
        key = str(p)
        if key in _BASELINE:
            continue
        fp = _fingerprint(p)
        if fp:
            _BASELINE[key] = fp
    # ---- 比对 ----
    for path_s, before in _BASELINE.items():
        now = _fingerprint(pathlib.Path(path_s))
        if now is None:
            _VIOLATIONS.append({
                "file": path_s, "kind": "deleted_or_unreadable",
                "test": item.nodeid, "before": list(before)})
            continue
        if now != before:
            _VIOLATIONS.append({
                "file": path_s, "kind": "modified_during_run",
                "test": item.nodeid,
                "before": list(before), "after": list(now),
                "delta_lines": None})
            # 已被污染的模块移出基线，避免同一条反复报
            _BASELINE[path_s] = now


def pytest_sessionfinish(session, exitstatus):
    if not _VIOLATIONS:
        if os.environ.get("DOCMIND_FREEZE_VERBOSE"):
            print("\n[freeze-guard] 全程无污染 ✓")
        return

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(_VIOLATIONS, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    by_file: dict[str, int] = {}
    for v in _VIOLATIONS:
        by_file[v["file"]] = by_file.get(v["file"], 0) + 1

    print("\n" + "=" * 74)
    print("  ⚠⚠⚠ 测量被污染：测试运行期间被测文件被改写")
    print("=" * 74)
    for f, n in sorted(by_file.items(), key=lambda kv: -kv[1]):
        rel = os.path.relpath(f, os.getcwd())
        print("    %-44s 被发现改动 %d 次" % (rel, n))
    print()
    print("  这轮的结果**不可信** —— 源码扫描类测试（inspect.getsource）")
    print("  读到的是错位切出来的片段，报红是假象。")
    print("  完整记录: %s" % REPORT)
    print("  处置: 冻结工作树重跑（见 docs/measure-freeze-discipline.md）")
    print("=" * 74 + "\n")


def pytest_report_header(config):
    """会话头部就宣告在盯，让「我改代码了」这件事有心理成本。"""
    if os.environ.get("DOCMIND_FREEZE_VERBOSE"):
        return "[freeze-guard] 正在盯被测源文件的改动（DOCMIND_FREEZE_VERBOSE=0 可静音）"
    return None
