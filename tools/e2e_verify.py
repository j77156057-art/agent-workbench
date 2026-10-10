"""③ 端到端复跑：A + B + C 一次跑完，用真工具 + 真参数串 + 真项目目录。

## 为什么这样才算端到端

单元测试里调的是 `code_intel.glob_files` / `_parse_keyed`；这里调
`tools.TOOLS["dev_glob"]["func"]` / `["dev_page_action"]["func"]` /
`["dev_serve"]["func"]` —— Agent 真正走的入口，参数也是 Agent 真正会写的
那串多行文本，项目目录用实录里那个 `D:\\WorkBuddy\\godot_sample`。

## ⚠ 脚本自己踩过的两个坑（记在这，免得重犯）

1. `dev_page_action` 返回的是 `ToolResult`，`str()` 拿到的是 **repr**
   （`ToolResult(ok=True, text="press：生效…")`）。直接搜关键词会漏。
   —— 必须 `getattr(v, "text", None) or str(v)`。
2. `pixels` 是**内部 effect 名**，中文渲染里叫「画面变了，DOM 没变」。
   按内部名去搜中文文本，永远搜不到。判定要按**渲染后的措辞**来。

## 会真的起 Edge 与静态站，用完必须回收。
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config                                            # noqa: E402
import tools                                             # noqa: E402

PROJECT = pathlib.Path(r"D:\WorkBuddy\godot_sample")
if not PROJECT.is_dir():
    raise SystemExit("  项目目录不在: %s" % PROJECT)
config.set_runtime("code_root", str(PROJECT))

dev_glob = tools.TOOLS["dev_glob"]["func"]
dev_serve = tools.TOOLS["dev_serve"]["func"]
dev_page = tools.TOOLS["dev_page_action"]["func"]

RESULTS = {}


def _text(v):
    """`dev_page_action` 返回的可能是 `ToolResult` —— 取 `.text`，别拿 repr。"""
    return getattr(v, "text", None) or str(v)


def _show(title, s):
    print()
    print("=" * 78)
    print("  " + title)
    print("=" * 78)
    for ln in s.splitlines():
        print("   ", ln[:108])


# ---------------------------------------------------------------- A
out = str(dev_glob("pattern: **/*.html"))
_show("A. dev_glob 能否看见 .docmind/web/index.html", out)
RESULTS["A"] = {
    "命中导出产物": ".docmind/web/index.html" in out,
    "不再是假否定措辞": "没有命中文件" not in out,
}

# ---------------------------------------------------------------- B
INC = ("action: open\nurl: http://127.0.0.1:8099/\n"
       "width: 1280\nheight: 720\n")
out2 = _text(dev_page(INC))
_show("B. dev_page_action 喂进 width/height（事故原样输入）", out2)
RESULTS["B"] = {
    "被显式拒绝": "不接受" in out2 and "preview_project" in out2,
    "没有打开 404": "Error response" not in out2,
}

out3 = _text(dev_page("action: open\nurl: http://127.0.0.1:1/\nfoo: 1\n"))
_show("B2. 纯未知键 foo", out3)
RESULTS["B2"] = {
    "被拒并列出可用参数": "不认识" in out3 and "selector" in out3,
}

# ---------------------------------------------------------------- C
_show("C. dev_serve 起静态站", "")
out4 = str(dev_serve("action: start\ncmd: python -m http.server 8123\n"
                     "cwd: .docmind/web\nport: 8123\nwait: 25"))
for ln in out4.splitlines():
    print("   ", ln[:100])
m = re.search(r"(http://127\.0\.0\.1:\d+/)", out4)
if not m:
    raise SystemExit("  *** 没拿到地址，停下（不猜）***")
base = m.group(1)
job = re.search(r"job_id[=:]\s*([0-9a-f]+)", out4)
job_id = job.group(1) if job else ""

try:
    p = _text(dev_page("action: open\nurl: %s" % base))
    _show("C. open 页面会话（只给地址）", p)
    pm = re.search(r"page=(\S+)", p)
    if not pm:
        raise SystemExit("  *** 没拿到 page id，停下 ***")
    pid = pm.group(1)

    r1 = _text(dev_page("action: press\npage: %s\nkey: ArrowRight\n"
                        "screenshot: true\nsettle: 900" % pid))
    _show("C. 第一次按键 —— 无上一帧，必须说不构成证据", r1)
    RESULTS["C-第一次"] = {"明确说不构成证据": "不构成证据" in r1}

    r2 = _text(dev_page("action: press\npage: %s\nkey: ArrowUp\n"
                        "screenshot: true\nsettle: 900" % pid))
    _show("C. 第二次按键 —— 有上一帧，画面在动", r2)
    RESULTS["C-第二次"] = {
        "报出画面变了": "画面变了" in r2 and "DOM 没变" in r2,
        "给出指导": "要以这一条为准" in r2,
    }
    print()
    print("  回收:", _text(dev_page("action: close\npage: %s" % pid))[:120])
finally:
    if job_id:
        print("  停服:", str(dev_serve("action: stop\njob_id: %s" % job_id))[:120])

# ---------------------------------------------------------------- 汇总
print()
print("=" * 78)
print("  端到端结论")
print("=" * 78)
allok = True
for group, checks in RESULTS.items():
    for name, ok in checks.items():
        print("  %-10s %-22s %s" % (group, name, "✅" if ok else "❌"))
        allok = allok and ok
print()
print("  ", "全部通过 —— 那段录职业已跑得通" if allok else "*** 有未通过项 ***")
