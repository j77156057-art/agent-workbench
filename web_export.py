"""Godot Web 导出与 iframe 试玩支持（纯标准库）。

职责：
- 幂等注入 DocmindBridge（addons 自动加载：Web 走 postMessage，桌面走 DOCMIND_EVENT JSON）；
- 生成/合并 export_presets.cfg 的 Web preset，命令行执行 --export-release；
- Web 导出模板检测与按需安装（GitHub tpz，仅提取 web_* 三个文件，省 ~600MB 磁盘）；
- 导出产物 index.html 后处理（注入父页面消息转发脚本）。

产物固定在 <root>/.docmind/web/（.gdignore 阻止 Godot 扫描）。
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

WEB_DIR_NAME = ".docmind/web"
BRIDGE_ADDON_DIR = "addons/docmind_bridge"
BRIDGE_SCRIPT_REL = BRIDGE_ADDON_DIR + "/docmind_bridge.gd"
AUTOLOAD_NAME = "DocmindBridge"

_MISSING_RX = re.compile(r"export templates? .*(missing|not found|没有|未安装)|can't open file|could not open", re.I)

# ---------------------------------------------------------------------------
# DocmindBridge 自动加载脚本
# ---------------------------------------------------------------------------
BRIDGE_GD = '''extends Node
## Agent 工作台 运行时桥（工作台自动注入，请勿手改）
## Web：与父页面 window.postMessage 双向通信；桌面：打印 DOCMIND_EVENT + JSON 供时间线抓取。

const MARKER := "DOCMIND_EVENT "
var _jb: Object
var _win: Object
var _cb: Object  # create_callback 返回的 JS 对象必须由 GDScript 成员持有，否则 Callable 会被回收
var _seq := 0

func _ready() -> void:
\t# 平台判据用官方推荐的 OS.has_feature("web")（桌面版同名空壳无 eval 等方法，
\t# GDScript 短路保证桌面构建永不触碰其方法）。
\t# 注意 Godot 4.7 已移除 get_eval_window()，取 window 用 get_interface("window")。
\tif OS.has_feature("web") and JavaScriptBridge != null \\
\t\t\tand JavaScriptBridge.has_method("get_interface") \\
\t\t\tand JavaScriptBridge.has_method("create_callback") \\
\t\t\tand JavaScriptBridge.has_method("eval"):
\t\t_jb = JavaScriptBridge
\t\t_win = _jb.get_interface("window")
\t\t_cb = _jb.create_callback(_on_js_message)
\t\t_win.__docmind_recv = _cb
\t\t_jb.eval("window.addEventListener('message', function(e) { var d = e.data; if (d && d.source === 'docmind-cmd' && window.__docmind_recv) { window.__docmind_recv(JSON.stringify(d)); } });")
\t\t_post_to_parent({"source": "docmind-runtime", "eid": _next_eid(), "type": "__bridge_ready__", "data": {}})

## 游戏代码调用：DocmindBridge.emit("player_damaged", {"hp": 90})
func emit(type: String, data: Dictionary = {}) -> void:
\tvar eid := _next_eid()
\tif _jb != null:
\t\t_post_to_parent({"source": "docmind-runtime", "eid": eid, "type": type, "data": data})
\telse:
\t\tprint(MARKER + JSON.stringify({"eid": eid, "type": type, "data": data}))

func _next_eid() -> String:
\t_seq += 1
\treturn "%d-%d" % [Time.get_ticks_msec(), _seq]

func _post_to_parent(payload: Dictionary) -> void:
\tif _jb == null:
\t\treturn
\t_jb.eval("window.parent.postMessage(%s, '*')" % JSON.stringify(payload))

func _on_js_message(args: Array) -> void:
\tif args.is_empty():
\t\treturn
\tvar parsed = JSON.parse_string(str(args[0]))
\tif typeof(parsed) != TYPE_DICTIONARY:
\t\treturn
\tvar cmd: String = parsed.get("cmd", "")
\tmatch cmd:
\t\t"ping":
\t\t\t_post_to_parent({"source": "docmind-runtime", "eid": _next_eid(), "type": "__pong__", "data": {}})
\t\t"reload":
\t\t\tget_tree().reload_current_scene()
\t\t"quit":
\t\t\tget_tree().quit()
'''

# 导出页面注入：父页面 → 游戏 的命令通道（游戏→父页面由 GDScript 直接 postMessage）。
_HTML_SNIPPET = '''<script>
(function () {
  if (window.parent === window) return;
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (d && d.source === 'docmind-cmd' && typeof window.__docmind_recv === 'function') {
      window.__docmind_recv(JSON.stringify(d));
    }
  });
})();
</script>
</body>'''

_PRESET_HEADER = '''[preset.{n}]

name="Web"
platform="Web"
runnable=true
dedicated_server=false
custom_features=""
export_filter="all_resources"
include_filter=""
exclude_filter=""
export_path="{export_path}"
encryption_include_filters=""
encryption_exclude_filters=""
encrypt_pck=false
encrypt_directory=false
script_export_mode=2

[preset.{n}.options]

custom_template/debug=""
custom_template/release=""
variant/extensions_support=false
vram_texture_compression/for_desktop=true
vram_texture_compression/for_mobile=false
html/export_icon=true
html/custom_html_shell=""
html/head_include=""
html/canvas_resize_policy=2
html/focus_canvas_on_start=true
html/experimental_virtual_keyboard=false
progressive_web_app/enabled=false
progressive_web_app/offline_page=""
progressive_web_app/display=1
progressive_web_app/orientation=0
progressive_web_app/icon_144x144=""
progressive_web_app/icon_180x180=""
progressive_web_app/icon_512x512=""
progressive_web_app/background_color=Color(0, 0, 0, 1)
'''


# ---------------------------------------------------------------------------
# 路径/标识
# ---------------------------------------------------------------------------
def play_token(root):
    return hashlib.sha1(os.path.normcase(os.path.abspath(root)).encode("utf-8")).hexdigest()[:16]


# token -> root，供 /play/<token> 反查（浏览器 GET 不带 X-DocMind-Project 头，
# 不能依赖当前项目上下文）。导出时写入；若服务重启，会从项目注册表 + code_root 回扫兜底。
_PLAY_TOKEN_ROOTS = {}


def root_for_token(token):
    """按 token 反查工程根目录。找不到返回 None。"""
    if not token:
        return None
    root = _PLAY_TOKEN_ROOTS.get(token)
    if root and os.path.isdir(root):
        return root
    # Fallback：扫描注册项目与全局 code_root，匹配 token。
    candidates = []
    try:
        import projects as _projects
        candidates.extend([p['root'] for p in _projects.list_projects() if p.get('root')])
    except Exception:  # noqa: BLE001
        pass
    try:
        import config as _config
        code_root = _config.get_runtime('code_root') or _config.CODE_ROOT
        if code_root and code_root not in candidates:
            candidates.append(code_root)
    except Exception:  # noqa: BLE001
        pass
    for cand in candidates:
        if cand and os.path.isdir(cand) and play_token(cand) == token:
            _PLAY_TOKEN_ROOTS[token] = cand
            return cand
    return None


def web_dir(root):
    return os.path.join(os.path.abspath(root), WEB_DIR_NAME)


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Bridge / preset 注入
# ---------------------------------------------------------------------------
def ensure_bridge(root):
    """幂等注入 bridge 脚本、autoload、.gdignore。返回变更清单。"""
    root = os.path.abspath(root)
    changed = []
    script_path = os.path.join(root, *BRIDGE_SCRIPT_REL.split("/"))
    if not os.path.exists(script_path) or _read(script_path) != BRIDGE_GD:
        _write(script_path, BRIDGE_GD)
        changed.append(BRIDGE_SCRIPT_REL)

    proj = os.path.join(root, "project.godot")
    text = _read(proj) if os.path.exists(proj) else ""
    autoload_line = f'{AUTOLOAD_NAME}="*res://{BRIDGE_SCRIPT_REL}"'
    if autoload_line not in text:
        if "[autoload]" in text:
            text = re.sub(r"(\[autoload\][^\n]*\n)", r"\1" + autoload_line + "\n", text, count=1)
        else:
            text = text.rstrip("\n") + "\n\n[autoload]\n\n" + autoload_line + "\n"
        _write(proj, text)
        changed.append("project.godot:[autoload]")

    gdignore = os.path.join(root, ".docmind", ".gdignore")
    if not os.path.exists(gdignore):
        _write(gdignore, "")
        changed.append(".docmind/.gdignore")
    return changed


def ensure_web_preset(root):
    """确保 export_presets.cfg 含一个 Web preset；已有则原样保留。返回 (changed:bool, preset_index:int)。"""
    root = os.path.abspath(root)
    cfg_path = os.path.join(root, "export_presets.cfg")
    text = _read(cfg_path) if os.path.exists(cfg_path) else ""
    for m in re.finditer(r"^\[preset\.(\d+)\][^\n]*\n(?P<body>.*?)(?=^\[preset\.\d+\]|\Z)",
                         text, re.M | re.S):
        if re.search(r'^\s*platform\s*=\s*"Web"\s*$', m.group("body"), re.M):
            return False, int(m.group(1))
    indices = [int(x) for x in re.findall(r"^\[preset\.(\d+)\]", text, re.M)]
    n = (max(indices) + 1) if indices else 0
    block = _PRESET_HEADER.format(n=n, export_path=WEB_DIR_NAME + "/index.html")
    text = (text.rstrip("\n") + "\n\n" + block) if text.strip() else block
    _write(cfg_path, text)
    return True, n


# ---------------------------------------------------------------------------
# 引擎版本与模板
# ---------------------------------------------------------------------------
def godot_version(executable, timeout=20):
    try:
        p = subprocess.run([executable, "--version"], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
    except Exception:
        return None
    line = (p.stdout or p.stderr or "").strip().splitlines()
    return line[0].strip() if line else None


def _template_dir(version):
    # 4.7.2.stable.official.ed1daf0bf -> 4.7.2.stable
    parts = (version or "").split(".")
    if len(parts) < 4:
        return None
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Godot", "export_templates", ".".join(parts[:4]))


# Web 模板核心变体（threads + 非 threads，缺任一则常见 preset 导出失败）
_WEB_CORE = ("web_debug.zip", "web_release.zip",
             "web_nothreads_debug.zip", "web_nothreads_release.zip")


def templates_status(executable):
    version = godot_version(executable)
    tdir = _template_dir(version)
    info = {"version": version, "template_dir": tdir, "installed": False,
            "web_debug": False, "web_release": False, "variants": [],
            "install": install_state().copy()}
    if tdir and os.path.isdir(tdir):
        info["web_debug"] = os.path.exists(os.path.join(tdir, "web_debug.zip"))
        info["web_release"] = os.path.exists(os.path.join(tdir, "web_release.zip"))
        info["variants"] = [n for n in _WEB_CORE if os.path.exists(os.path.join(tdir, n))]
        info["installed"] = all(os.path.exists(os.path.join(tdir, n)) for n in _WEB_CORE)
    return info


def _tpz_url(version):
    parts = (version or "").split(".")
    if len(parts) < 4:
        raise ValueError(f"无法解析引擎版本：{version!r}")
    tag = f"{parts[0]}.{parts[1]}.{parts[2]}-{parts[3]}"
    if parts[3] != "stable":
        raise ValueError("自动安装仅支持 stable 正式版模板，beta/dev 请在编辑器中手动安装。")
    return (f"https://github.com/godotengine/godot/releases/download/{tag}/"
            f"Godot_v{tag}_export_templates.tpz", tag)


def _tpz_urls(version):
    """返回 (官方 URL + 镜像前缀拼接 URL) 候选列表，安装时按顺序尝试。"""
    url, tag = _tpz_url(version)
    mirrors = [x.strip() for x in os.getenv(
        "DOCMIND_TPZ_MIRRORS",
        "https://bin.m.daocloud.io/,https://gh-proxy.com/,https://ghfast.top/").split(",") if x.strip()]
    candidates = [m.rstrip("/") + "/" + url for m in mirrors]
    candidates.append(url)  # GitHub 直连兜底
    return candidates, tag


# 安装状态（进程内单例；后台线程写）
_install_lock = threading.Lock()
_install_state = {"state": "idle", "downloaded": 0, "total": 0, "error": "",
                  "started_at": 0, "mirror": ""}


def install_state():
    with _install_lock:
        return dict(_install_state)


def _set_install(**kw):
    with _install_lock:
        _install_state.update(kw)


def install_templates_async(executable):
    """启动后台下载安装；已在进行中则返回 False。"""
    with _install_lock:
        if _install_state["state"] in ("downloading", "extracting"):
            return False
        _install_state.update({"state": "starting", "downloaded": 0, "total": 0,
                               "error": "", "started_at": time.time(), "mirror": ""})
    threading.Thread(target=_install_worker, args=(executable,), daemon=True).start()
    return True


def _install_worker(executable):
    try:
        version = godot_version(executable)
        tdir = _template_dir(version)
        if not tdir:
            raise RuntimeError(f"无法解析引擎版本：{version!r}")
        urls, tag = _tpz_urls(version)
        tmp = os.path.join(os.environ.get("TEMP", tdir), f"docmind_templates_{tag}.tpz")
        last_err = None
        downloaded_ok = False
        for url in urls:
            mirror = "github" if "github.com" in url and "/" + "github.com/" not in url else url.split("/https://")[0]
            try:
                _set_install(state="starting", mirror=mirror, error="")
                req = urllib.request.Request(url, headers={"User-Agent": "Agent Workbench/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}")
                    total = int(resp.headers.get("Content-Length", 0))
                    _set_install(state="downloading", total=total, downloaded=0)
                    with open(tmp, "wb") as f:
                        while True:
                            chunk = resp.read(1 << 20)
                            if not chunk:
                                break
                            f.write(chunk)
                            with _install_lock:
                                _install_state["downloaded"] += len(chunk)
                                if total:
                                    _install_state["downloaded"] = min(total, _install_state["downloaded"])
                got = os.path.getsize(tmp)
                if total and got != total:
                    raise RuntimeError(f"下载不完整：{got}/{total}")
                downloaded_ok = True
                break
            except Exception as e:  # noqa: BLE001  切换下一镜像
                last_err = e
                with _install_lock:
                    _install_state["downloaded"] = 0
                    _install_state["total"] = 0
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        if not downloaded_ok:
            raise RuntimeError(f"所有下载源均失败（最后错误：{last_err}）")
        _set_install(state="extracting")
        os.makedirs(tdir, exist_ok=True)
        with zipfile.ZipFile(tmp) as zf:
            names = set(zf.namelist())
            # 提取 version.txt + tpz 内全部 Web 变体（threads/nothreads/dlink，
            # 各 Godot 版本变体集合不同，动态识别；整包 1GB+ 但 Web 部分仅约 90MB）
            wanted = {"templates/version.txt"}
            wanted |= {n for n in names
                       if n.startswith("templates/web") and n.endswith(".zip")}
            missing = {"templates/version.txt",
                       "templates/web_debug.zip",
                       "templates/web_nothreads_debug.zip"} - names
            if missing:
                raise RuntimeError("tpz 内缺少：" + ", ".join(sorted(missing)))
            for name in sorted(wanted):
                target = os.path.join(tdir, os.path.basename(name))
                with zf.open(name) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        try:
            os.remove(tmp)
        except OSError:
            pass
        _set_install(state="done", error="")
    except Exception as e:  # noqa: BLE001
        _set_install(state="error", error=str(e))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export_web(root, executable, timeout=300):
    root = os.path.abspath(root)
    changed_bridge = ensure_bridge(root)
    preset_added, _ = ensure_web_preset(root)
    out_dir = web_dir(root)
    os.makedirs(out_dir, exist_ok=True)

    tpl = templates_status(executable)
    if not tpl["installed"]:
        return {"ok": False, "error": "missing_templates",
                "message": "未检测到 Godot Web 导出模板（web_debug.zip / web_release.zip）。",
                "templates": tpl}

    cmd = [executable, "--headless", "--path", root,
           "--export-release", "Web", os.path.join(out_dir, "index.html")]
    started = time.time()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace", creationflags=creationflags)
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    index = os.path.join(out_dir, "index.html")
    missing = bool(_MISSING_RX.search(out)) or not os.path.exists(index)
    if p.returncode != 0 or missing:
        return {"ok": False, "error": "export_failed", "returncode": p.returncode,
                "output": out[-4000:], "templates": tpl,
                "bridge_changed": changed_bridge, "preset_added": preset_added}

    injected = _inject_html_bridge(index)
    files = {}
    for name in sorted(os.listdir(out_dir)):
        fp = os.path.join(out_dir, name)
        if os.path.isfile(fp):
            files[name] = os.path.getsize(fp)
    token = play_token(root)
    _PLAY_TOKEN_ROOTS[token] = root
    return {"ok": True, "token": token, "url": f"/play/{token}/index.html",
            "elapsed": round(time.time() - started, 1), "files": files,
            "html_injected": injected, "bridge_changed": changed_bridge,
            "preset_added": preset_added, "output": out[-2000:]}


def _inject_html_bridge(index_path):
    text = _read(index_path)
    if "__docmind_recv" in text:
        return False
    if "</body>" in text:
        text = text.replace("</body>", _HTML_SNIPPET, 1)
    elif "</html>" in text:
        text = text.replace("</html>", _HTML_SNIPPET + "\n</html>", 1)
    else:
        text = text + _HTML_SNIPPET
    _write(index_path, text)
    return True


# ---------------------------------------------------------------------------
# 静态服务辅助
# ---------------------------------------------------------------------------
PLAY_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".wasm": "application/wasm",
    ".pck": "application/octet-stream",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".css": "text/css; charset=utf-8",
    ".worker.js": "application/javascript; charset=utf-8",
}


def resolve_play_file(root, rel):
    """把 /play/<token>/<rel> 的 rel 解析到产物文件，越界/不存在返回 None。"""
    base = os.path.abspath(web_dir(root))
    target = os.path.abspath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        return None
    if not os.path.isfile(target):
        return None
    return target
