"""Agent 工作台 桌面端启动器（控制台模式，最稳）。

为什么是控制台而不是原生窗口：
  早期版本用 pywebview/WebView2 弹原生窗口，在缺少 WebView2 运行时的机器上会
  静默失败（不报错也不弹窗、进程还活着），表现为"双击没反应"。后又试过 tkinter
  控制窗，同样可能在某些机器上建不出窗口。因此改为最朴素的方案：exe 本身带一个
  可见的控制台窗口，启动后打印本地地址并自动尝试打开默认浏览器；窗口常驻直到用户
  按 Enter 退出。界面本身就是 Web 应用，浏览器即桌面端，跨机器稳定。

特性：
  - 单实例保护：若本实例的端口上已在提供 Agent 工作台 服务，直接打开浏览器并退出，避免堆积进程。
  - 启动全程写入 docmind_desktop.log（与 exe 同目录）便于排查。

环境变量：
  DOCMIND_PORT=8012        监听端口（默认 8000）。本机 8000 常被别的项目占着，
                           换端口即可共存；单实例探测、浏览器地址、/api 全都跟着它走。
  DOCMIND_SERVER_ONLY=1   仅启动后端服务模式（无界面 / 容器 / 自动化测试用）。
  DOCMIND_HOME=/workbench  桌面壳默认打开哪个页面（默认是 RAG 问答页）。
"""
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser

from api import app
from tools import dev_capture_bug

HOST = "127.0.0.1"
# 端口可用环境变量覆盖：本机 8000 常被另一个实例占着，起验证实例（或自动化测试）时需要换个口
PORT = int(os.getenv("DOCMIND_PORT", "8000"))
API_BASE = f"http://{HOST}:{PORT}"
URL = API_BASE + "/"

# 桌面壳打开哪个页面。默认是 **RAG 问答页**：它才是产品原点入口（"点开就能问"）；
# 开发工作台是第二个入口——问答页顶栏有「开发工作台 →」，工作台顶栏有「问答」回链。
# 想换默认入口不必改代码：DOCMIND_HOME=/workbench 即可（浏览器回退路径同理走 URL）。
HOME_PATH = os.getenv("DOCMIND_HOME", "/")

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "docmind_desktop.log")


def _ensure_bundled_git():
    """分发版随包携带 MinGit（<exe目录>/MinGit/cmd/git.exe）。

    前置进 PATH 后，所有裸 `git` 子进程（regions.workbench_fs 的版本状态、
    分区初始化/提交/回滚）都能命中，用户机器无需自行安装 Git。
    源码运行或包内缺目录时不做任何处理（回落到系统 PATH）。
    """
    if not getattr(sys, "frozen", False):
        return
    git_cmd_dir = os.path.join(BASE, "MinGit", "cmd")
    if os.path.isfile(os.path.join(git_cmd_dir, "git.exe")):
        os.environ["PATH"] = git_cmd_dir + os.pathsep + os.environ.get("PATH", "")
        _log("已启用随包 Git（MinGit）")
    else:
        _log("未找到随包 MinGit，分区 Git 功能需要系统已安装 Git")


def _log(msg: str):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass
    print(msg)


def _already_running() -> bool:
    # 只用纯存活探针：/api/config 内部会探测 Ollama（未运行时本机回环
    # 拒绝连接都可能耗时数秒），1s 超时下会被误判为「没有实例」。
    try:
        with urllib.request.urlopen(f"{URL}api/health", timeout=1) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _wait_for_server(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{URL}api/health", timeout=1):
                return True
        except (urllib.error.URLError, OSError, TimeoutError):
            time.sleep(0.3)
    return False


def _run_server():
    import uvicorn

    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except Exception as e:  # noqa: BLE001 线程异常默认只丢 stderr，必须落日志否则启动失败无线索
        import traceback

        _log("服务线程异常退出：%s: %s" % (type(e).__name__, e))
        _log(traceback.format_exc())
        raise


def _open_browser() -> bool:
    try:
        return bool(webbrowser.open(URL))
    except Exception:  # noqa: BLE001
        return False

def probe_multi_window_support():
    """静态探测 pywebview 是否具备"每项目一个宿主窗口"的能力。

    ⚠️ 只做**静态**能力探测（包版本 + API 存在性），返回值仅表示"允许尝试多窗口"，
    **不代表真机同时开两个 edgechromium 窗口并各自嵌入引擎已被验证**——本机无法证实
    多窗口同时嵌入（见 desktop_bridge 宿主表已就绪，默认仍走单窗口）。
    返回 ``(ok, reason)``。
    """
    try:
        import webview
        try:
            from importlib.metadata import version as _pkg_version
            ver = _pkg_version("pywebview")
        except Exception:  # noqa: BLE001
            ver = getattr(webview, "__version__", "") or ""
        major = 0
        head = str(ver).split(".")[0] if ver else ""
        major = int("".join(ch for ch in head if ch.isdigit()) or 0)
        multi_api = hasattr(webview, "windows") and hasattr(webview, "create_window")
        ok = bool(multi_api and major >= 4)
        return ok, f"pywebview {ver or '?'} (multi_api={multi_api}, major={major})"
    except Exception as e:  # noqa: BLE001
        return False, "pywebview 不可用：" + str(e)


def build_host_window(api_base: str = "", title: str = "Agent Workbench 开发工作台",
                      width: int = 1440, height: int = 920, project_id: str = ""):
    """创建 pywebview 原生宿主窗口并绑定事件，返回 window 对象（**不启动事件循环**）。

    抽成独立函数是为了让自动化测试能驱动同一套宿主逻辑（标题、最小尺寸、宿主 HWND 注册、
    resized/shown/closing 事件接线），而不是在测试里各写一份——两份实现必然漂移。

    ``project_id``：非空时把宿主登记到**该项目**（多窗口/多项目场景）；为空则登记到全局
    默认键，行为与改造前一致（生命线）。
    """
    import webview
    class _DesktopApi:
        def select_directory(self):
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            return result[0] if result else ''

    base = (api_base or API_BASE).rstrip('/')
    page = base + HOME_PATH
    # 带项目后缀的标题：多窗口时靠它把每个窗口区分开（find_host 按标题子串枚举）。
    # project_id 为空 → 标题不变，单窗口行为与改造前完全一致。
    win_title = title if not project_id else f"{title} · {project_id}"

    def _host_hwnd():
        """Prefer this window's native handle, including initially hidden windows."""
        from desktop_bridge import find_host, is_window
        try:
            hwnd = int(win.native.Handle.ToInt64())
            if hwnd and is_window(hwnd):
                return hwnd
        except (AttributeError, TypeError, ValueError):
            pass  # Non-WinForms backends / older pywebview use title fallback.
        host = find_host(win_title) or find_host("Agent Workbench")
        return host[0] if host else None

    def _register_host():
        hwnd = _host_hwnd()
        if not hwnd:
            _log("未找到 pywebview 宿主 HWND，Godot 保持独立窗口")
            return None
        _log("已找到 Agent 工作台 宿主 HWND: %s" % hwnd)
        import desktop_bridge
        desktop_bridge.set_host(hwnd, project_id)
        try:
            import json as _json
            payload = {"hwnd": hwnd}
            if project_id:
                payload["project_id"] = project_id
            body = _json.dumps(payload).encode()
            req = urllib.request.Request(base + "/api/desktop/host", data=body,
                                        headers={'Content-Type': 'application/json'},
                                        method='POST')
            urllib.request.urlopen(req, timeout=2).read()
        except Exception as e:
            _log("宿主 HWND 注册后端失败：" + str(e))
        return hwnd

    def _loaded():
        try:
            _register_host()
        except Exception as e:
            _log("宿主 HWND 检测失败：" + str(e))

    def _refill(reason):
        """宿主尺寸/可见性变化时，把"铺满模式"的嵌入窗口按客户区重排。

        注意用的是**客户区**而不是 win.width/win.height：外框包含标题栏与边框，
        直接拿外框尺寸会把引擎画面裁掉一截（150% 缩放下裁得更多）。
        rect 模式的引擎视窗由前端自己重新 place，这里不插手。
        """
        try:
            from desktop_bridge import fill_all
            # project_id 为空 → fill_all(project_id=None) 行为与改造前完全一致。
            results = fill_all(project_id=project_id or None)
            if results:
                _log("%s：已同步 %d 个嵌入窗口" % (reason, len(results)))
        except Exception as e:
            _log("嵌入窗口尺寸同步失败：" + str(e))

    def _resized(*_args):
        _refill("宿主 resize")

    def _shown(*_args):
        _refill("宿主显示")

    def _closing(*_args):
        """关窗即收尾：先解除嵌入再结束引擎进程，避免留下孤儿窗口/进程。

        只在 finally 里清是不够的——pywebview 的主循环一退出，宿主 HWND 就没了，
        此时子窗口还挂在它下面，引擎进程会继续跑，用户看到的是"关不掉的后台游戏"。
        """
        try:
            from game_workbench import engine_stop_all
            stopped = engine_stop_all()
            if stopped:
                _log("关窗前已停止引擎：" + str(sorted(stopped)))
        except Exception as e:
            _log("关窗前停止引擎失败：" + str(e))

    win = webview.create_window(win_title, page, width=width, height=height,
                                min_size=(1024, 680), text_select=True, js_api=_DesktopApi())
    try:
        # pywebview 事件属于具体窗口对象；绑定全局 webview.events 在部分版本不会触发。
        # resized/shown/closed 在不同版本上名字与签名都不一样，逐个 hasattr 探测。
        win.events.loaded += _loaded
        for name, handler in (('resized', _resized), ('shown', _shown),
                              ('closing', _closing), ('closed', _closing)):
            event = getattr(win.events, name, None)
            if event is None:
                continue
            try:
                event += handler
            except Exception:  # noqa: BLE001  个别版本的事件对象不支持 +=
                pass
    except Exception as e:  # noqa: BLE001
        _log("绑定宿主事件失败（降级为不自动同步）：" + str(e))
    return win


def _open_native_window() -> bool:
    """Prefer a native pywebview window; return False when its runtime is unavailable."""
    try:
        import webview
        _log("检测到 pywebview %s，尝试创建 Edge 原生窗口" % getattr(webview, '__version__', 'unknown'))

        win = build_host_window(API_BASE)
        _log("开始运行 pywebview 事件循环")
        webview.start(gui="edgechromium", debug=False)
        _log("pywebview 事件循环已退出")
        # 事件循环退出后再兜一次：closing 事件在个别 pywebview 版本上不触发，
        # 漏掉就会留下一个关不掉的后台引擎进程
        try:
            from game_workbench import engine_stop_all
            stopped = engine_stop_all()
            if stopped:
                _log("事件循环退出后清理引擎：" + str(sorted(stopped)))
        except Exception as e:  # noqa: BLE001
            _log("退出后清理引擎失败：" + str(e))
        _ = win
        return True
    except Exception as e:
        _log("原生桌面窗口不可用，回退浏览器：" + str(e))
        return False


def _keep_alive():
    """常驻直到用户按 Enter；无法读取输入时退化为保活睡眠。"""
    try:
        input("\n服务运行中。按 Enter 退出 Agent 工作台（关闭浏览器标签不会停止服务）。\n")
    except EOFError:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


def _emit_ollama_guidance():
    """若本机 Ollama 不可用，打印清晰引导（而非让用户对着报错发懵）。"""
    try:
        from api import check_ollama
        st = check_ollama()
        if st.get("guidance"):
            _log("Ollama 未就绪或缺少模型")
            print("\n" + "=" * 48)
            print("  WARNING  Ollama 不可用 / 缺少所需模型")
            print("=" * 48)
            for line in (st.get("guidance") or "").splitlines():
                print("  " + line)
            print()
    except Exception as e:  # noqa: BLE001
        _log("Ollama 检查失败：" + str(e))


def main():
    _ensure_bundled_git()
    # 必须在建宿主窗口之前把进程提到 per-monitor v2：否则在 125%/150% 缩放下
    # 本进程拿到的是虚拟化坐标，而 Godot 是 DPI 感知的，父/子窗口坐标系不一致 → 嵌入错位。
    try:
        from desktop_bridge import dpi_awareness, ensure_dpi_awareness
        ok, mode = ensure_dpi_awareness()
        _log("进程 DPI 感知：%s（设置%s）" % (mode, '成功' if ok else '失败，可能错位'))
    except Exception as e:  # noqa: BLE001
        _log("设置 DPI 感知失败：" + str(e))
    if os.getenv("DOCMIND_SERVER_ONLY"):
        _log("服务模式启动")
        _emit_ollama_guidance()
        _run_server()
        return

    _log("桌面模式启动")
    print("=" * 48)
    print("  Agent 工作台 · 本地 RAG 代码问答")
    print("=" * 48)

    if _already_running():
        _log("检测到已有实例在运行，直接打开浏览器")
        print(f"[Agent 工作台] 已在运行，正在打开浏览器：{URL}")
        ok = _open_browser()
        if not ok:
            print(f"[Agent 工作台] 请手动在浏览器打开：{URL}")
        input("按 Enter 退出本启动器。\n")
        return

    server = threading.Thread(target=_run_server, daemon=True)
    server.start()

    if not _wait_for_server():
        _log("服务启动失败")
        print("[Agent 工作台] 服务在 20 秒内未就绪：可能是 8000 端口被占用，或启动过程被卡住。")
        print(f"          详情见日志：{LOG}")
        input("按 Enter 退出。\n")
        return

    _log("服务就绪")
    print(f"[Agent 工作台] 服务已就绪：{URL}")
    _emit_ollama_guidance()
    ok = _open_native_window()
    _log("原生窗口启动结果 -> %s" % ok)
    if not ok:
        ok = _open_browser()
    _log("浏览器打开尝试 -> %s" % ok)
    if ok:
        print(f"[Agent 工作台] 已尝试用默认浏览器打开页面。")
    else:
        print(f"[Agent 工作台] 未能自动打开浏览器，请手动访问：{URL}")

    _keep_alive()
    _log("退出")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        _log("未预期异常：\n" + tb)
        try:
            # 若已配置代码库，异常会同时归档到 bugs 分区，便于按提交追踪。
            dev_capture_bug(f"title: Agent 工作台 启动异常\nerror: {tb.splitlines()[-1] if tb else 'unknown'}\ntraceback: {tb}")
        except Exception as capture_error:
            _log("Bug 归档失败：" + str(capture_error))
        print("启动过程出现未预期错误：\n" + tb)
        try:
            input("按 Enter 退出。\n")
        except Exception:
            pass
