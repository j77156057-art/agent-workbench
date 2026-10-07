"""预览地址：**能应答不等于是我的项目**，所以不许静默换一个端口。

## 为什么要做这一条

`agent.py` 的工具清单教模型传**写死的** `http://127.0.0.1:5173/`，
而 vite 撞了端口会自动往后挪（5173 -> 5174）。于是模型照着那个地址访问时，
两种结局都不对：

* 打到一个已经死掉的地址 -> 探测超时，而报告说「没认到端口」，指向一个错地址
* 打到**别的**服务上     -> 拍到别人的画面**并报成功**（假绿灯，实测踩过：
  5173 上站着另一个 worktree 项目的 vite）

## 我第一版写错了

第一版是「给定地址不通就按候选端口逐个探，**取第一个能应答的**」。

**那是把同一个假绿灯换了个入口。** 能应答不代表那上面跑的是本项目 ——
静默选一个候选并截图报成功，比「打错端口超时」更难发现，因为它看起来是成功的。

所以正确行为是：**只沿用给定地址自己应答的情况**；不通就把候选端口**列出来**
让人认领，不替它选。

## 归属判断在另一处

`dev_server.owns_port()` 判「这个端口上是不是我的项目」（要项目指纹）。
这里只判「通不通」，**不做归属猜测**。两条是不同的问题。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_runtime import visual_acceptance as V  # noqa: E402
from agent_runtime.visual_acceptance import VisualAcceptanceError  # noqa: E402


def _probe_on(alive=(), dead=()):
    alive = {a.rstrip("/") for a in alive}
    dead = {d.rstrip("/") for d in dead}

    def probe(url: str):
        u = url.rstrip("/")
        if u in alive:
            return True, "HTTP 200"
        if u in dead:
            return False, "端口还没有在听"
        return False, "端口还没有在听"

    return probe


# --------------------------------------------------------------------------
# 核心：不通时不静默换端口
# --------------------------------------------------------------------------

def test_it_refuses_to_silently_switch_to_another_live_port():
    """**反向守卫，本文件存在的理由**：别的端口在应答时也不许顶替。

    第一版就是在这里错的：它「取第一个能应答的候选」，于是
    `http://127.0.0.1:5173/` 不通时会去打 5174 —— 而 5174 上可能是**别人的**服务。
    """
    probe = _probe_on(alive=["http://127.0.0.1:5174/"])
    url, note = V.resolve_live_url("http://127.0.0.1:5173/", probe=probe)
    assert url == "", (
        "给定地址不通却返回了另一个地址 %r —— 这就是那个假绿灯："
        "在应答不等于那上面跑的是本项目" % url
    )


def test_the_message_names_the_live_candidates_and_asks_for_confirmation():
    """不许只说「没找到」：得把在应答的候选列出来，并说明不能默认就是它。"""
    probe = _probe_on(alive=["http://127.0.0.1:5174/", "http://127.0.0.1:3000/"])
    _, note = V.resolve_live_url("http://127.0.0.1:5173/", probe=probe)
    assert "5174" in note and "3000" in note, note
    assert "请确认" in note, note
    assert "不等于是" in note or "不代表" in note, (
        "说明里必须写清「在应答不代表是本项目」，否则用户会默认第一个就是自己的"
    )


def test_the_message_does_not_claim_nothing_is_running_when_something_is():
    """**反向守卫**：说「没找到在跑的服务」的同时又列出在应答的端口 = 自相矛盾。"""
    probe = _probe_on(alive=["http://127.0.0.1:5174/"])
    _, note = V.resolve_live_url("http://127.0.0.1:5173/", probe=probe)
    assert "没找到在跑的本机预览服务" not in note, (
        "有端口在应答却断言什么都没在跑 —— 这条消息会让人以为 vite 没起来：\n" + note
    )


def test_the_given_address_is_not_probed_twice():
    """给定地址不通时，候选循环要跳过它 —— 否则说明里同一句出现两次。"""
    probe = _probe_on()
    _, note = V.resolve_live_url("http://127.0.0.1:5173/", probe=probe)
    assert note.count("http://127.0.0.1:5173/") == 1, note


# --------------------------------------------------------------------------
# 沿用与拒绝
# --------------------------------------------------------------------------

def test_it_keeps_the_given_address_when_it_answers():
    probe = _probe_on(alive=["http://127.0.0.1:5173/"])
    url, note = V.resolve_live_url("http://127.0.0.1:5173/", probe=probe)
    assert url == "http://127.0.0.1:5173/", note
    assert "沿用" in note


def test_without_a_url_it_says_it_cannot_tell():
    """没给 url 就明确说不知道，**不**去挨个端口挨个打（那会打到别人的服务）。"""
    url, note = V.resolve_live_url("", probe=_probe_on(alive=["http://127.0.0.1:5174/"]))
    assert url == "", "没给 url 却自己挑了一个端口"
    assert "url" in note


def test_a_non_loopback_url_is_rejected_by_the_existing_guard():
    """不因为这次改动放松了回环校验 —— 那条护栏在 `loopback_url` 里。"""
    url, note = V.resolve_live_url("http://10.0.0.5:5173/", probe=_probe_on())
    assert url == "", note
    assert "回环" in note


# --------------------------------------------------------------------------
# 接线：capture_project_preview 必须真的用上它
# --------------------------------------------------------------------------

def test_capture_fails_fast_when_the_given_url_is_dead():
    """行为级接线守卫：给定地址不通时，**不能**照样去截一个别的端口。

    用桩替掉 `resolve_live_url` 来证明它被调到了；真跑浏览器太贵，而这里要验的
    是「有没有在截图之前就失败」，不是浏览器本身。
    """
    called = {}

    def _fake(url="", **kw):
        called["url"] = url
        return "", "给定地址不通；另有在应答的候选端口：http://127.0.0.1:5174/"

    original = V.resolve_live_url
    V.resolve_live_url = _fake
    try:
        V.capture_project_preview(ROOT, url="http://127.0.0.1:5173/")
    except VisualAcceptanceError as exc:
        assert "候选" in str(exc), str(exc)
    else:
        raise AssertionError("给定地址不通却没失败 —— 它去截了某个别的端口")
    finally:
        V.resolve_live_url = original
    assert called.get("url") == "http://127.0.0.1:5173/", (
        "capture_project_preview 没有把 url 交给 resolve_live_url：%r" % called
    )


def test_the_no_url_static_path_is_untouched(tmp_path):
    """反向守卫：没给 url 时仍走原来的静态站路径，别被这次改动带跑偏。

    用 `tmp_path` 造项目根而不是仓库根 —— 仓库根有没有 index.html 是布局问题，
    与本条要验的东西无关（我第一版就栽在这儿：守卫挂在环境上，不是挂在机制上）。
    """
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    target, entry = V.preview_target(tmp_path, "index.html", "")
    assert target == "", target
    assert entry is not None


def test_the_drift_contract_is_still_untouched():
    """漂移契约（另一族问题）不许被这次改动碰到。"""
    from agent_runtime import dev_server as D

    assert D.parse_port_hint(
        "  ➜  Local:   http://127.0.0.1:5173/\n"
        "  ➜  Network: http://127.0.0.1:5174/\n") == 5174