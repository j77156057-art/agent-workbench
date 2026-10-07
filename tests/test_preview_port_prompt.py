"""教模型的文案里不许再出现**写死的预览端口**。

## 为什么这是守卫而不是文案改动

`dev_server.py:32-36` 的注释把漂移写成了契约：vite 撞了 5173 会自动挪到 5174，
于是日志里有**多个**端口行、第一个是死的，`parse_port_hint` 才故意取最后一个。

而工具清单里教模型用的是**写死的** `http://127.0.0.1:5173/`。那个字面量在
无碰撞时对，**一碰撞就错**，而且错的两种结局都不好：

* 打到一个已经死掉的地址 -> 探测超时，报告却指向一个错地址
* 打到**别的**服务上     -> 拍到别人的画面**并报成功**（假绿灯，实测踩过：
  5173 上站着另一个 worktree 项目的 vite）

`dev_serve` 的返回里**已经带了真实 url**（`render()` 明说「接着用这个 url」），
所以正确做法不是给一个更准的写死端口，而是**别写死**。

## 这条不许被「统一文案」推翻

下一个人做一致性整理时，很容易把这些字面量「统一」回去。那样做是撕开契约。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 教模型怎么传 url 的那几处文案
_PROMPT_FILES = ("agent.py", "tools.py")

#: 匹配「http(s)://回环:五位端口」这种**完整字面量**
_LITERAL = re.compile(r"https?://(?:127\.0\.0\.1|localhost|\[::1\]):(\d{2,5})")

#: 这些端口在这两文件里作为**示例字面量**出现就是错的（5173/5174 是漂移族）
_DRIFT_PORTS = {"5173", "5174"}


def test_the_prompt_text_never_hardcodes_a_drift_prone_port():
    """反向守卫：教模型的文案里不许有 `http://127.0.0.1:5173/` 这种字面量。"""
    hits = []
    for name in _PROMPT_FILES:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in _LITERAL.finditer(text):
            if match.group(1) in _DRIFT_PORTS:
                line = text[:match.start()].count("\n") + 1
                hits.append("%s:%d  %s" % (name, line, match.group(0)))
    assert not hits, (
        "教模型的文案里又出现了写死的预览端口：\n  " + "\n  ".join(hits) +
        "\n端口会被占后挪（5173→5174）。改成 `<实际端口>` 并提示用 dev_serve 返回的地址。"
    )


def test_the_drift_contract_itself_is_still_pinned():
    """反向守卫：这条不许顺手动漂移契约。

    `parse_port_hint` 取**最后一个**匹配是另一族问题（「vite 会漂移」），
    有用例（`test_it_takes_the_last_one_when_the_port_moved`）与变异电池钉着。
    本次改动只是不教模型写死端口，与它无关。
    """
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from agent_runtime import dev_server as D

    assert D.parse_port_hint(
        "  ➜  Local:   http://127.0.0.1:5173/\n"
        "  ➜  Network: http://127.0.0.1:5174/\n") == 5174


def test_the_prompt_still_points_at_the_address_dev_serve_returns():
    """**正向守卫**：改完之后要真的指了路，而不是只把字面量删掉。

    光删字面量等于让模型自己猜端口 —— 那比写死更糟。这里钉住「用 dev_serve
    返回的地址」这个说法还在。
    """
    agent_text = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert "dev_serve 返回" in agent_text, (
        "preview_project 的说明应当指向 dev_serve 返回的实际地址"
    )
    tools_text = (ROOT / "tools.py").read_text(encoding="utf-8")
    assert "实际端口" in tools_text, "tools.py 的 url 说明应当说明端口取实际值"