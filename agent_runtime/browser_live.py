"""浏览器实况：给无头会话开一条只读的 CDP 推流连接。

## 为什么单开一条连接

`page_action` 的主 CDP 连接是同步 request/response 模型：一次 `call()` 写一条命令、
读到对应 id 的回复就返回；没有常驻读线程。`Page.startScreencast` 是**事件流**，
帧会不受邀请地持续到来，塞进主连接要么把下一次 `call()` 的回复读串，要么让帧在
socket 缓冲区里堆到 ack 超时。所以这里另连一条 WebSocket 到同一个 page target：
CDP 允许同一定位目标有多个客户端，主连接的动作语义一个字都不用改。

## 只做观察，不做动作

本模块只发 `Page.enable` / `Page.startScreencast` / `Page.screencastFrameAck` /
`Runtime.evaluate`（只读 location/title）。任何点击、输入、导航都不从这里走——
否则"实况"就成了第二条能改页面的路径，护栏会出现两套。

## 断线策略

无头浏览器被杀、标签崩溃、页面导航都可能让连接断开。线程按 0.5s/1s/2s 退避重连，
最多 4 次；仍失败就把 `state` 置为 `error` 并保留最后一条错误。`snapshot()` 永远
不会抛异常——实况面板拿不到画面只能显示"连接中断"，不能反过来影响 Agent 的工具调用。
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

try:  # websocket-client 已经是浏览器控制的既有依赖；缺失时只降级实况，不影响动作。
    import websocket
except Exception:  # pragma: no cover - bundle 缺依赖时走这条
    websocket = None


#: everyNthFrame 必须为 1：静态页面只在开局（和每次画面变化时）产生一两帧，
#: 按 8 帧抽会把这几帧全部丢掉，实况面板永远空白（实测 10s 零帧）。
#: 视频页几十帧/秒的问题改由 **ack 节流** 解决：Chrome 收到 ack 才发下一帧，
#: 客户端按 _ACK_INTERVAL 回 ack，上行自然压到 ~6 fps。
_EVERY_NTH_FRAME = 1
_ACK_INTERVAL = 0.15
_MAX_WIDTH = 960
_MAX_HEIGHT = 720
_JPEG_QUALITY = 60
_EVAL_INTERVAL = 1.5
_RECV_TIMEOUT = 0.5
_ACK_POLL_TIMEOUT = 0.05
_MAX_RETRIES = 4


class LiveScreencast:
    """一条页面会话对应的只读实况流。线程安全。"""

    def __init__(self, ws_url: str, page_id: str) -> None:
        self.ws_url = str(ws_url or "")
        self.page_id = str(page_id or "")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: Any = None
        self._seq = 0
        self._frame = b""
        self._version = 0
        self._captured_at = 0.0
        self._meta: dict[str, Any] = {}
        self._url = ""
        self._title = ""
        self._state = "starting" if self.ws_url else "unavailable"
        self._error = ""
        self._connected_at = 0.0

    # ---- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if not self.ws_url or websocket is None:
            with self._lock:
                self._state = "unavailable"
                self._error = ("websocket-client 未安装" if websocket is None
                               else "会话没有暴露 CDP 地址")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="docmind-live-%s" % self.page_id, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            ws = self._ws
            self._state = "stopped"
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # ---- 对外快照 ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            frame = base64.b64encode(self._frame).decode("ascii") if self._frame else ""
            return {
                "available": bool(self._frame),
                "state": self._state,
                "error": self._error,
                "frame": frame,
                "frame_version": self._version,
                "captured_at": self._captured_at,
                "width": int(self._meta.get("deviceWidth") or 0),
                "height": int(self._meta.get("deviceHeight") or 0),
                "url": self._url,
                "title": self._title,
                "connected_at": self._connected_at,
            }

    def save_latest(self, path: Path, max_age: float = 0) -> bool:
        """把当前帧落盘；返回是否真的写到了文件。

        `max_age` 秒内的帧才算数：动作前截图要的是"现在这一页"，
        导航之后如果实况还没收到新帧，旧帧会把上一页拍进 before 证据。
        """
        with self._lock:
            raw = bytes(self._frame)
            captured = float(self._captured_at or 0)
        if not raw:
            return False
        if max_age and captured and (time.time() - captured) > float(max_age):
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def digest(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""

    # ---- 线程主体 ---------------------------------------------------------

    def _set_state(self, state: str, error: str = "") -> None:
        with self._lock:
            self._state = state
            self._error = error

    def _send(self, ws: Any, method: str, params: dict[str, Any] | None = None) -> int:
        with self._lock:
            self._seq += 1
            seq = self._seq
        ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
        return seq

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set() and attempt < _MAX_RETRIES:
            ws = None
            try:
                ws = websocket.create_connection(
                    self.ws_url, timeout=5.0, suppress_origin=True)
                with self._lock:
                    self._ws = ws
                    self._connected_at = time.time()
                self._set_state("live")
                self._send(ws, "Page.enable")
                self._send(ws, "Runtime.enable")
                self._send(ws, "Page.startScreencast", {
                    "format": "jpeg",
                    "quality": _JPEG_QUALITY,
                    "maxWidth": _MAX_WIDTH,
                    "maxHeight": _MAX_HEIGHT,
                    "everyNthFrame": _EVERY_NTH_FRAME,
                })
                ws.settimeout(_RECV_TIMEOUT)
                next_eval = time.monotonic() + 0.2
                pending_ack: Any = None
                next_ack = 0.0
                while not self._stop.is_set():
                    now = time.monotonic()
                    if pending_ack is not None and now >= next_ack:
                        try:
                            self._send(ws, "Page.screencastFrameAck", {"sessionId": pending_ack})
                        except Exception:
                            break
                        pending_ack = None
                        next_ack = now + _ACK_INTERVAL
                    if now >= next_eval:
                        try:
                            self._send(ws, "Runtime.evaluate", {
                                "expression": "({url: location.href, title: document.title})",
                                "returnByValue": True,
                            })
                        except Exception:
                            break
                        next_eval = now + _EVAL_INTERVAL
                    ws.settimeout(_ACK_POLL_TIMEOUT if pending_ack is not None else _RECV_TIMEOUT)
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionError("CDP 连接被对端关闭")
                    session_id = self._consume(raw)
                    if session_id is not None:
                        pending_ack = session_id
                attempt = 0
            except Exception as exc:  # noqa: BLE001 - 实况断了不能影响动作
                if self._stop.is_set():
                    break
                attempt += 1
                self._set_state(
                    "reconnecting" if attempt < _MAX_RETRIES else "error",
                    "%s: %s" % (type(exc).__name__, str(exc)[:160]))
                self._stop.wait(min(2.0, 0.5 * attempt))
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
                with self._lock:
                    if self._ws is ws:
                        self._ws = None
        if not self._stop.is_set():
            with self._lock:
                if self._state not in ("error", "unavailable"):
                    self._state = "stopped"

    def _consume(self, raw: str | bytes) -> Any:
        """消化一条 CDP 消息；收到帧时返回 sessionId，交给主循环节流 ack。"""
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return None
        method = msg.get("method")
        if method == "Page.screencastFrame":
            params = msg.get("params") or {}
            data = params.get("data") or ""
            if data:
                try:
                    image = base64.b64decode(data)
                except Exception:
                    image = b""
                if image:
                    with self._lock:
                        self._frame = image
                        self._version += 1
                        self._captured_at = time.time()
                        self._meta = params.get("metadata") or {}
            return params.get("sessionId")
        if method == "Page.screencastVisibilityChanged":
            return None
        if "id" in msg and isinstance(msg.get("result"), dict):
            result = msg["result"]
            value = (result.get("result") or {}).get("value")
            if isinstance(value, dict):
                url = str(value.get("url") or "")
                title = str(value.get("title") or "")
                if url or title:
                    with self._lock:
                        self._url = url
                        self._title = title
        return None
