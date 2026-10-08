"""核实：前端那笔提交（`f0922a9`，41 个 `frontend/src/**` 文件、305 处 font-size）
有没有让「扫前端源码」的后端守卫**静默失效**。

## 为什么单独查这件事

前端 agent 改了 41 个源文件。而我们这边**至少 6 个测试直接读前端源码并断言
具体字符串** —— 那类守卫的失效方式最阴险：前端改掉那个字符串，守卫不会报错，
它只是**不再检查任何东西**（或反过来，红在一个与本次改动无关的原因上）。

按纪律，这里**只做只读的判读**，不改任何测试。

## 判据不是「测试绿不绿」

全量正在跑，而**绿灯可能正是问题本身**（断言因守卫对象消失而恒真）。
所以这里查的是**守卫的前提还在不在**：

* 守卫断言的那个字符串/文件**现在还在吗**
* 守卫读的文件**这笔提交动过吗**

两个都答，才能判断「这次全量的绿，有多少是可信的」。
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]

# 后端测试里「读前端源码 + 断言具体内容」的守卫。
# 每项: (测试文件, 守卫意图)
GUARDS = {
    "test_frontend_bundle_fresh.py": "bundle 新鲜度 + 若干前端字符串（pricing_state 等）",
    "test_cache_display.py": "SettingsView / harness.ts 里的缓存展示字符串",
    "test_pricing_state.py": "costLabel.ts / SettingsView.vue / harness.ts 的定价状态",
    "test_failed_request_forensics.py": "fonts.ts 注释里的四个字族依赖说法",
    "test_live_frame_worker.py": "两个 liveFrame*.ts 存在且可被 esbuild 处理",
    "test_vue_component.py": "WorkflowMembersDock.vue / ContextMenu.vue 可挂载",
    "test_realtime_protocol_contract.py": "realtimeProtocol.ts 与 Python 侧契约一致",
}

# 这笔前端提交动过的前端文件
CHANGED = """
frontend/src/ask/AskApp.vue
frontend/src/ask/ask-typo.css
frontend/src/ask/ask.css
frontend/src/ask/components/AskHistoryPopover.vue
frontend/src/ask/components/AskMessage.vue
frontend/src/ask/components/AskSidebar.vue
frontend/src/ask/components/FileViewer.vue
frontend/src/workbench/App.vue
frontend/src/workbench/components/AssetCenterView.vue
frontend/src/workbench/components/AssetGeneratePanel.vue
frontend/src/workbench/components/AutonomousCockpit.vue
frontend/src/workbench/components/ChatComposer.vue
frontend/src/workbench/components/ChatDock.vue
frontend/src/workbench/components/EngineConnectPopover.vue
frontend/src/workbench/components/FlowCanvas.vue
frontend/src/workbench/components/GitHistoryDialog.vue
frontend/src/workbench/components/HarnessPanel.vue
frontend/src/workbench/components/ModelSwitcherPopover.vue
frontend/src/workbench/components/OverviewView.vue
frontend/src/workbench/components/RegionHomeView.vue
frontend/src/workbench/components/RegionMapDialog.vue
frontend/src/workbench/components/RelationGraph.vue
frontend/src/workbench/components/RewriteDiffDialog.vue
frontend/src/workbench/components/RuntimeTimeline.vue
frontend/src/workbench/components/SceneCanvas.vue
frontend/src/workbench/components/SceneRuntimePanel.vue
frontend/src/workbench/components/SelectionAiPanel.vue
frontend/src/workbench/components/SelectionToolbar.vue
frontend/src/workbench/components/SemanticLocateBar.vue
frontend/src/workbench/components/SessionHistoryPopover.vue
frontend/src/workbench/components/SymbolMap.vue
frontend/src/workbench/components/SymbolOutline.vue
frontend/src/workbench/components/UnityGraph.vue
frontend/src/workbench/components/WorkflowCard.vue
frontend/src/workbench/components/WorkflowGateDialog.vue
frontend/src/workbench/components/WorkflowHistoryPopover.vue
frontend/src/workbench/components/WorkflowMembersDock.vue
frontend/src/workbench/style.css
frontend/src/workbench/workbench-typo.css
""".split()

# 这些测试读取的前端路径。**两种写法都要认**：
#   `frontend/src/...`  —— 多数测试用这个
#   `src/workbench/...` —— `test_vue_component.py` 用 `TARGET = "src/..."`
# 我第一版只认前一种，于是报「test_vue_component 引用 0 个前端文件」，
# **漏掉了唯一一处真交集**（WorkflowMembersDock.vue 被本次提交动过）。
FRONTEND_REF = re.compile(
    r"(?:frontend/)?src/[A-Za-z0-9_./-]+\.(?:vue|ts|css)")


def normalise(rel: str) -> str:
    """`src/...` -> `frontend/src/...`，让两种写法可比。"""
    return rel if rel.startswith("frontend/") else "frontend/" + rel


def main() -> None:
    changed = {c for c in CHANGED}
    print("=" * 78)
    print("  1. 守卫读了哪些前端文件，其中哪些被这笔提交动过")
    print("=" * 78)

    touched_refs: list[tuple[str, str]] = []
    for test_name, why in GUARDS.items():
        path = ROOT / "tests" / test_name
        if not path.exists():
            print("  %-38s *** 测试文件不存在 ***" % test_name)
            continue
        text = path.read_text(encoding="utf-8")
        refs = sorted({normalise(r) for r in FRONTEND_REF.findall(text)})
        hits = [r for r in refs if r in changed]
        mark = "  <== 这笔提交动过" if hits else ""
        print("  %-38s 引用 %d 个前端文件%s" % (test_name, len(refs), mark))
        print("      意图: %s" % why)
        for h in hits:
            print("      动过: %s" % h)
            touched_refs.append((test_name, h))

    print()
    print("  === 判据：守卫的断言对象是否被本次改动碰到 ===")
    if touched_refs:
        print("  %d 处交集 —— 这些守卫的判据被前端那笔提交直接影响：" % len(touched_refs))
        for t, h in touched_refs:
            print("    %-38s %s" % (t, h))
        print()
        print("  ⚠ 交集本身不等于坏。要看的是**断言的那个字符串还在不在**。")
    else:
        print("  无交集。")

    print()
    print("=" * 78)
    print("  2. 逐个守卫：它断言的字面量现在还在吗")
    print("=" * 78)

    # 抽取形如 self._read("frontend/...") 后面 assertIn(...) 的字面量，做粗定位。
    # 这是**辅助判读**，不是完整分析 —— 完整分析要逐个读测试。
    for test_name in GUARDS:
        path = ROOT / "tests" / test_name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        reads = re.findall(r'_read\(\s*["\'](frontend/src/[^"\']+)["\']', text)
        if not reads:
            continue
        print()
        print("  %s" % test_name)
        for rel in sorted(set(reads)):
            f = ROOT / rel
            if not f.exists():
                print("    [缺文件] %s  *** 守卫会读不到 ***" % rel)
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            print("    %-56s %5d 行%s"
                  % (rel, len(src.splitlines()),
                     "   <== 本次动过" if rel in changed else ""))

    print()
    print("=" * 78)
    print("  3. bundle 新鲜度（前端 agent 提交后是否重建过）")
    print("=" * 78)
    web = ROOT / "web"
    src = ROOT / "frontend/src"
    bw = max((p.stat().st_mtime for p in web.rglob("*.js") if p.is_file()), default=0)
    sw = max((p.stat().st_mtime for p in src.rglob("*")
              if p.is_file() and p.suffix in (".vue", ".ts", ".tsx")), default=0)
    print("  bundle 比 src 新: %s" % (bw >= sw))
    print()
    print("  ⚠ 本探针**不能**回答「全量的绿可不可信」—— 那要等全量跑完，")
    print("    且必须先确认工作树在跑的过程中没被改动。")
    print("    本探针只答：守卫的前提还在不在。")
    print()
    print("  === 我自己踩过的坑（照抄判据时会重犯）===")
    print("  1. 第一版正则只认 `frontend/src/...`，漏掉 `test_vue_component.py`")
    print("     用的 `src/workbench/...` 写法 —— 于是它报「引用 0 个前端文件」，")
    print("     **正好漏掉了唯一一处真交集**（WorkflowMembersDock.vue）。")
    print("     报「0 个引用」时先怀疑正则，不要当成「这个测试不读前端」。")
    print("  2. 「引用数 0」与「读了 11 个」都可能是探针错 —— 两个方向都会骗人。")
    print("  3. 交集 ≠ 坏。要看的是**断言的那个字符串/那个行为还在不在**；")
    print("     纯排版改动（font-size）与逻辑守卫不相交，但那是本次的运气，")
    print("     不是结构保证。")


if __name__ == "__main__":
    main()