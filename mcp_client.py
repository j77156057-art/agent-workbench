"""通用 MCP（Model Context Protocol）客户端。

Agent 工作台 不内置任何引擎专用协议，而是通过 MCP 对接外部游戏引擎桥：
- godot-ai v4：stdio 传输（`uvx godot-ai attach`），靠同用户 capability 目录认证；
- unity-mcp / UnrealMCP：Streamable HTTP JSON-RPC（无状态）。

仅使用标准库：stdio 走 newline-delimited JSON-RPC 2.0（子进程），HTTP 走
urllib（兼容 application/json 与 text/event-stream 两种响应）。

服务器注册表持久化在代码库根目录 .docmind_mcp.json，结构：
{"servers": {"godot": {"transport":"stdio","command":"uvx","args":[...]}, ...}}
默认提供 godot/unity/unreal 三个预设，项目内配置覆盖预设（可禁用/改地址/加自定义）。
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from queue import Queue, Empty
import project_state

CONFIG_FILENAME = ".docmind_mcp.json"
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "docmind-workbench", "version": "1.0"}

# uvx 冷启动（首次构建约 67 个包）可能超过常规 MCP 客户端 30s 超时
INIT_TIMEOUT = 180
CALL_TIMEOUT = 120
HTTP_TIMEOUT = 30

# HTTP 传输 UA：远端常按 UA/指纹拦截程序化请求（Cloudflare 等），默认 Python-urllib UA 会被
# 403；用浏览器 UA 与抽取层一致。与 mcp_autoconnect._BROWSER_UA 同源，单测 test_user_agent_parity 防漂移。
HTTP_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 预设：godot-ai 必须 stdio attach（裸 HTTP 无法通过 capability 轮换认证）
DEFAULT_SERVERS = {
    "godot": {
        "label": "Godot 4（godot-ai）",
        "engine": "godot",
        "transport": "stdio",
        "command": "uvx",
        "args": ["godot-ai", "attach"],
        "env": {},
        "enabled": True,
        "help": "需先在 Godot 项目中安装并启用 godot-ai 插件，且 Godot 编辑器处于打开状态。",
    },
    "unity": {
        "label": "Unity（unity-mcp）",
        "engine": "unity",
        "transport": "http",
        "url": "http://127.0.0.1:8080/mcp",
        "enabled": False,
        "help": "需在 Unity 中运行 CoplayDev unity-mcp 插件（Unity 2021.3+）。",
    },
    "unreal": {
        "label": "Unreal Engine（UnrealMCP）",
        "engine": "unreal",
        "transport": "http",
        "url": "http://127.0.0.1:3000/mcp",
        "enabled": False,
        "help": "需在 UE5 中启用 ChiR24/Unreal_mcp 插件（默认 :3000）。",
    },
}


class MCPError(Exception):
    """MCP 调用层面的可展示错误（进程/协议/远端 JSON-RPC error）。"""


# ---------------------------------------------------------------- 纯函数（便于单测）

def resolve_command(command):
    """解析可执行文件：PATH 优先；Windows 下 uv/uvx 常装在 ~/.local/bin 且未入 PATH。"""
    if not command:
        return ""
    if os.path.isabs(command) and os.path.isfile(command):
        return command
    hit = shutil.which(command)
    if hit:
        return hit
    base = os.path.basename(command)
    local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
    for cand in (os.path.join(local_bin, base),
                 os.path.join(local_bin, base + (".exe" if os.name == "nt" else ""))):
        if os.path.isfile(cand):
            return cand
    return command  # 保留原值，交由 Popen 报 FileNotFoundError


def parse_sse_frames(data):
    """解析 Streamable HTTP 的 text/event-stream 响应，返回 JSON 消息列表。"""
    msgs = []
    for raw_frame in data.replace("\r\n", "\n").split("\n\n"):
        payload_lines = []
        for line in raw_frame.split("\n"):
            if line.startswith("data:"):
                payload_lines.append(line[5:].strip())
        if not payload_lines:
            continue
        payload = "\n".join(payload_lines)
        if payload and payload != "[DONE]":
            try:
                msgs.append(json.loads(payload))
            except ValueError:
                pass
    return msgs


def extract_text(result):
    """从 tools/call 结果里抽取文本、结构化内容与图片块。

    返回 ``(text, structured, blocks, images)``：blocks 记录非文本块的类型名
    （含 "image"），images 是图片块的结构化副本 ``[{"data": base64, "mime_type": str}]``，
    供截图类连接器把画面送入统一视觉观察通道；非图片块不在这里展开。
    """
    if not isinstance(result, dict):
        return "", None, [], []
    texts, blocks, images = [], [], []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        ctype = item.get("type")
        if ctype == "text":
            texts.append(item.get("text") or "")
        else:
            blocks.append(ctype or "unknown")
            if ctype == "image" and item.get("data"):
                images.append({
                    "data": str(item.get("data")),
                    "mime_type": str(item.get("mimeType")
                                     or item.get("mime_type") or "image/png"),
                })
    return ("\n".join(t for t in texts if t).strip(),
            result.get("structuredContent"), blocks, images)


def normalize_server_config(cfg):
    """校验/归一化单个服务器配置，非法时抛 MCPError。"""
    if not isinstance(cfg, dict):
        raise MCPError("服务器配置必须是对象。")
    transport = cfg.get("transport")
    if transport not in ("stdio", "http"):
        raise MCPError("transport 仅支持 stdio 或 http。")
    out = dict(cfg)
    if transport == "stdio":
        if not cfg.get("command"):
            raise MCPError("stdio 服务器缺少 command。")
        out["args"] = list(cfg.get("args") or [])
        out["env"] = dict(cfg.get("env") or {})
    else:
        url = str(cfg.get("url") or "").strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            raise MCPError("http 服务器缺少合法 url。")
        out["url"] = url
    out["enabled"] = bool(cfg.get("enabled", True))
    return out


# ---------------------------------------------------------------- 配置持久化

def _config_path(root):
    return project_state.path(root, CONFIG_FILENAME, legacy=CONFIG_FILENAME)


def load_user_servers(root):
    """读取项目内覆盖配置（文件缺失/损坏时返回空 dict，不阻塞默认预设）。"""
    path = _config_path(root)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("servers") if isinstance(data, dict) else None
        return servers if isinstance(servers, dict) else {}
    except (OSError, ValueError):
        return {}


def server_configs(root):
    """默认预设 + 项目覆盖（深合并），返回列表。"""
    user = load_user_servers(root)
    keys = list(dict.fromkeys(list(DEFAULT_SERVERS.keys()) + list(user.keys())))
    out = []
    for key in keys:
        cfg = dict(DEFAULT_SERVERS.get(key, {}))
        cfg.update({k: v for k, v in (user.get(key) or {}).items() if v is not None})
        if key not in DEFAULT_SERVERS:
            cfg.setdefault("label", key)
            cfg.setdefault("enabled", True)
        try:
            cfg = normalize_server_config(cfg)
        except MCPError:
            cfg["enabled"] = False
            cfg["config_error"] = "配置无效"
        out.append({"key": key, **cfg})
    return out


def get_server_config(root, key):
    for item in server_configs(root):
        if item["key"] == key:
            return item
    raise MCPError(f"未找到 MCP 服务器：{key}")


# ---------------------------------------------------------------- 连接器能力模型 / 路由策略
# Agent 自主切换连接器的策略层：把"连接器能干什么"结构化出来，并按任务语义打分排序，
# 让 ReAct 不必靠硬编码的引擎名、也不必先开会话就能挑/切连接器。全部纯配置读取，可离线单测。

# 预设引擎默认携带的通用能力（用户配置里的 capabilities 字段可追加/覆盖语义）
_ENGINE_CAPS = {
    "godot": ["scene", "editor", "run", "build", "asset", "script", "export", "debug"],
    "unity": ["scene", "editor", "run", "build", "asset", "script", "export", "debug"],
    "unreal": ["scene", "editor", "run", "build", "asset", "script", "export", "debug"],
}

# 各引擎的"适用说明"，注入 Agent 目录便于按自然语言挑连接器
_BEST_FOR = {
    "godot": "Godot 编辑器内的场景/脚本/资源/运行/构建操作（godot-ai 插件）",
    "unity": "Unity 编辑器内的场景/资源/Console/PlayMode 操作（unity-mcp 插件）",
    "unreal": "Unreal 编辑器内的 Actor/Blueprint/构建操作（UnrealMCP 插件）",
}

# 任务描述里的关键词 → 能力同义词，提升召回（只映射到具体能力串，避免误命中）
_KEYWORD_CAPS = {
    "场景": "scene", "scene": "scene", "关卡": "scene", "level": "scene", "地图": "scene",
    "运行": "run", "play": "run", "启动": "run", "run": "run", "试玩": "run",
    "编辑器": "editor", "editor": "editor", "编辑": "editor",
    "构建": "build", "build": "build", "编译": "build", "打包": "build", "出包": "build",
    "资源": "asset", "asset": "asset", "素材": "asset", "导入": "asset",
    "脚本": "script", "script": "script", "代码": "script",
    "导出": "export", "export": "export",
    "调试": "debug", "debug": "debug", "断点": "debug",
    "蓝图": "blueprint", "blueprint": "blueprint",
    "godot": "engine:godot", "unity": "engine:unity", "unreal": "engine:unreal",
    "游戏引擎": "game_engine", "引擎": "game_engine",
}


def capabilities_of(cfg):
    """从配置推导连接器的能力标签集合（排序返回）。"""
    caps = set()
    engine = (cfg.get("engine") or "").lower()
    if engine:
        caps.add("game_engine")
        caps.add(f"engine:{engine}")
        for c in _ENGINE_CAPS.get(engine, []):
            caps.add(c)
    for c in (cfg.get("capabilities") or []):
        if isinstance(c, str) and c.strip():
            caps.add(c.strip().lower())
    return sorted(caps)


def best_for_of(cfg):
    explicit = cfg.get("best_for")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    engine = (cfg.get("engine") or "").lower()
    return _BEST_FOR.get(engine, "")


def connector_directory(root):
    """Agent 面向的连接器目录：启用的连接器 + 能力标签 + 适用说明（不打开会话）。

    与前端 `/api/agent/connectors` 同源，但额外带 capabilities / best_for，供 Agent 自主挑选。
    """
    rows = []
    try:
        import mcp_capabilities
    except Exception:  # 可选能力清单不可用时保持旧行为
        mcp_capabilities = None
    for item in server_configs(root):
        learned = mcp_capabilities.active_for(root, item.get("key")) if mcp_capabilities else {}
        caps = set(capabilities_of(item))
        caps.update(c for c in (learned.get("capabilities") or []) if isinstance(c, str))
        rows.append({
            "key": item.get("key"),
            "label": item.get("label"),
            "engine": item.get("engine"),
            "transport": item.get("transport"),
            "enabled": bool(item.get("enabled")),
            "capabilities": sorted(caps),
            "best_for": learned.get("best_for") or best_for_of(item),
            "domain": learned.get("domain") or (item.get("engine") or "general"),
            "keywords": learned.get("keywords") or [],
            "tool_mappings": learned.get("tool_mappings") or [],
            "capability_source": "discovered" if learned else "configured",
            "help": item.get("help") or "",
            "config_error": item.get("config_error"),
        })
    return rows


def select_connector(root, task_hint, only_enabled=True):
    """按任务语义给连接器打分排序，返回 [{key, score, reason}]（高分在前）。

    打分：能力同义词命中 +2、引擎名直接出现 +3、label/help/best_for 含任务词 +1。
    仅对已启用且无配置错误的连接器排序（only_enabled）；无匹配返回空列表。

    两个语义约束（避免误路由）：
      - 关键词用「子串扫描」匹配，兼容中文无空格分词（"生成游戏场景"能拆出"场景"）。
      - 若任务描述点名某引擎（godot/unity/unreal），只在该引擎的连接器里选，
        绝不把 Unity 任务路由到 Godot 等其它引擎。
    """
    hint = (task_hint or "").lower()
    # 子串扫描：hint 含某关键词即视为命中其能力同义词（中文无需分词）
    caps_in_hint = {cap for kw, cap in _KEYWORD_CAPS.items() if kw and kw in hint}
    # 引擎专指：点名某引擎则只在它之内选
    preferred = next((e for e in ("godot", "unity", "unreal") if e in hint), None)
    results = []
    for item in connector_directory(root):
        if only_enabled and not item["enabled"]:
            continue
        if item.get("config_error"):
            continue
        engine = (item.get("engine") or "").lower()
        if preferred and engine != preferred:
            continue
        score = 0
        matched = []
        for cap in item["capabilities"]:
            for h in caps_in_hint:
                if cap == h or (h and (h in cap or cap in h)):
                    score += 2
                    matched.append(cap)
                    break
        # 已批准能力清单的领域关键词参与路由，但不会绕过 enabled/config_error。
        for keyword in item.get("keywords") or []:
            word = str(keyword).lower().strip()
            if len(word) >= 2 and word in hint:
                score += 2
                matched.append(word)
        if engine and engine in hint:
            score += 3
            matched.append(f"engine:{engine}")
        blob = f"{item.get('label','')} {item.get('help','')} {item.get('best_for','')}".lower()
        for kw in _KEYWORD_CAPS:
            if len(kw) >= 2 and kw in hint and kw in blob:
                score += 1
                matched.append(kw)
        if score > 0:
            reasons = []
            if engine and engine in hint:
                reasons.append(f"引擎名命中 {engine}")
            if matched:
                reasons.append("能力匹配：" + ", ".join(sorted(set(matched))))
            results.append({"key": item["key"], "score": score,
                             "reason": "；".join(reasons) if reasons else "能力相关"})
    results.sort(key=lambda x: (-x["score"], x["key"]))
    return results


def save_server(root, key, cfg):
    """新增/更新一个服务器（白名单字段，拒绝畸形 key）。"""
    if not key or not all(ch.isalnum() or ch in "_-" for ch in key) or len(key) > 40:
        raise MCPError("服务器 key 仅允许字母数字、下划线、连字符（≤40）。")
    norm = normalize_server_config(cfg)
    user = load_user_servers(root)
    user[key] = {k: v for k, v in norm.items() if k not in ("label", "help", "config_error")}
    path = _config_path(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"servers": user}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "servers": server_configs(root)}


def remove_server(root, key):
    """删除项目级自定义服务器；预设 key 仅写入 enabled:false（不删默认项）。"""
    user = load_user_servers(root)
    if key in DEFAULT_SERVERS:
        user[key] = {**_drop_label(DEFAULT_SERVERS[key]), "enabled": False}
    elif key in user:
        del user[key]
    else:
        raise MCPError(f"未找到 MCP 服务器：{key}")
    path = _config_path(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"servers": user}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"ok": True, "servers": server_configs(root)}


def _drop_label(cfg):
    return {k: v for k, v in cfg.items() if k not in ("label", "help")}


def pin_godot_server(root, version):
    """安装 godot-ai 插件后，把 stdio 参数 pin 到与插件相同的发布版本。"""
    cfg = get_server_config(root, "godot")
    if cfg.get("transport") != "stdio":
        return
    args = list(cfg.get("args") or [])
    spec = f"godot-ai=={version}"
    if args and str(args[0]).startswith("godot-ai"):
        args[0] = spec
    else:
        args = [spec] + args
    save_server(root, "godot", {
        "transport": "stdio", "command": cfg.get("command") or "uvx",
        "args": args, "env": cfg.get("env") or {}, "enabled": True,
    })


# ---------------------------------------------------------------- stdio 传输

class _StdioSession:
    """长驻 stdio MCP 子进程：reader 线程收 JSON 行，请求按 id 匹配响应。"""

    def __init__(self, cfg, cwd=None):
        self.cfg = cfg
        # 解析 @secret:<provider> 哨兵：写入 .docmind_mcp.json 时原样保存，
        # 运行时在此替换为本机 secrets_store 明文（不落盘明文）。无 @secret 时原样返回。
        from mcp_autoconnect import resolve_secret_refs
        try:
            cfg = resolve_secret_refs(cfg, cwd or os.getcwd())
        except Exception as exc:
            raise MCPError(f"凭证解析失败（@secret 引用无法解析）：{exc}")
        self.cfg = cfg
        command = resolve_command(cfg["command"])
        env = os.environ.copy()
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        kwargs = dict(args=[command, *[str(a) for a in cfg.get("args") or []]],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, cwd=cwd, env=env,
                      text=True, encoding="utf-8", errors="replace", bufsize=1)
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(**kwargs)
        except FileNotFoundError:
            raise MCPError(f"找不到可执行文件：{cfg['command']}（请确认已安装并在 PATH 或 ~/.local/bin 中）")
        self._inbox = Queue()
        self._stderr_tail = deque(maxlen=200)
        self._lock = threading.Lock()
        self._next_id = 0
        self._dead_reason = ""
        self._stop = threading.Event()
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # MCP 要求 stdout 只有 JSON-RPC；杂散行（启动横幅等）忽略
                if isinstance(msg, dict):
                    self._inbox.put(msg)
        except (ValueError, OSError):
            pass

    def _pump_stderr(self):
        try:
            for line in self.proc.stderr:
                s = line.rstrip()
                if s:
                    self._stderr_tail.append(s)
        except (ValueError, OSError):
            pass

    def alive(self):
        return self.proc.poll() is None

    def request(self, method, params=None, timeout=INIT_TIMEOUT, want_response=True):
        if not self.alive():
            rc = self.proc.returncode
            tail = "\n".join(self._stderr_tail)[-800:]
            raise MCPError(f"MCP 进程已退出（code={rc}）。{tail}")
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            body = json.dumps({"jsonrpc": "2.0", "id": msg_id,
                               "method": method, "params": params or {}},
                              ensure_ascii=False)
            try:
                self.proc.stdin.write(body + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise MCPError(f"MCP 进程写入失败：{e}")
            if not want_response:
                return None
            deadline = time.monotonic() + timeout
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    tail = "\n".join(self._stderr_tail)[-400:]
                    raise MCPError(f"MCP {method} 超时（{timeout}s）。{tail}")
                try:
                    msg = self._inbox.get(timeout=min(1.0, remain))
                except Empty:
                    if not self.alive():
                        tail = "\n".join(self._stderr_tail)[-800:]
                        raise MCPError(f"MCP 进程意外退出（code={self.proc.returncode}）。{tail}")
                    continue
                if msg.get("id") != msg_id:
                    continue  # 服务端通知等，忽略
                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"{err.get('code')}: {err.get('message')}")
                return msg.get("result")

    def notify(self, method, params=None):
        try:
            self.request(method, params, timeout=5, want_response=False)
        except MCPError:
            pass

    def initialize(self, timeout=INIT_TIMEOUT):
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=timeout)
        self.notify("notifications/initialized")
        return result or {}

    def close(self):
        self._stop.set()
        try:
            # Windows 上 npx.CMD 会再启动 node。仅终止 cmd 会留下持有 stdout 管道的
            # 子进程，随后 close 管道会一直等 reader 线程退出。
            if os.name == "nt" and self.proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        for pipe in (self.proc.stdin,):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        for thread, pipe in ((self._stdout_thread, self.proc.stdout),
                             (self._stderr_thread, self.proc.stderr)):
            thread.join(timeout=1)
            if not thread.is_alive() and pipe:
                try:
                    pipe.close()
                except Exception:
                    pass


_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _session_for(root, item):
    key = (os.path.abspath(root), item["key"])
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(key)
        if sess is not None and not sess.alive():
            sess.close()
            del _SESSIONS[key]
            sess = None
        if sess is None:
            sess = _StdioSession(item, cwd=os.path.abspath(root))
            sess.initialize()
            _SESSIONS[key] = sess
        return sess


def close_server(root, key):
    with _SESSIONS_LOCK:
        sess = _SESSIONS.pop((os.path.abspath(root), key), None)
    if sess:
        sess.close()
    return {"ok": True, "closed": bool(sess)}


def close_all():
    with _SESSIONS_LOCK:
        items = list(_SESSIONS.items())
        _SESSIONS.clear()
    for _, sess in items:
        try:
            sess.close()
        except Exception:
            pass


def active_servers(root):
    """返回当前在 root 下有活跃（已连接并保持）stdio 会话的服务器 key 列表。

    HTTP 传输为无状态，不持有长驻会话，因此不会出现在返回值中。
    """
    target = os.path.abspath(root)
    with _SESSIONS_LOCK:
        return [k[1] for k in _SESSIONS.keys() if os.path.abspath(k[0]) == target]


# ---------------------------------------------------------------- HTTP 传输（无状态）

def _http_status_message(code):
    """把 HTTP 状态码清洗为可读信息；**绝不回传远端原始 body**（HTML/长 JSON 会上屏变乱码）。"""
    hints = {400: "请求被拒绝", 401: "需要鉴权", 403: "可能为反爬或鉴权拦截",
             404: "端点不存在（服务可能已下线或地址已变更）", 405: "方法不被允许",
             429: "请求过于频繁"}
    hint = hints.get(code, "远端服务错误" if code and code >= 500 else "请求被拒绝")
    return f"远端拒绝程序化试连（HTTP {code}，{hint}）"


def _http_post(url, body, timeout=HTTP_TIMEOUT, *, headers=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": HTTP_USER_AGENT}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("content-type", "")
            raw = resp.read().decode("utf-8", errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        # 只保留状态码语义，丢弃原始 body（可能含反爬拦截页/长 JSON）。
        raise MCPError(_http_status_message(e.code))
    except (urllib.error.URLError, OSError) as e:
        raise MCPError(f"无法连接 {url}：{e}")
    if status == 202 or not raw.strip():
        return None  # 无状态服务器接受通知，无响应体
    if "text/event-stream" in ctype:
        msgs = parse_sse_frames(raw)
        if not msgs:
            return None
        target = body.get("id")
        for m in msgs:
            if m.get("id") == target:
                if "error" in m:
                    err = m["error"]
                    raise MCPError(f"{err.get('code')}: {err.get('message')}")
                return m.get("result")
        return msgs[-1].get("result")
    try:
        msg = json.loads(raw)
    except ValueError:
        # 非 JSON（多为反爬拦截页 / 错误端点）：只报状态，不回传原始正文。
        raise MCPError(f"无法解析 MCP HTTP 响应（HTTP {status}，远端返回非 JSON，"
                       "可能为反爬拦截页或端点错误）")
    if "error" in msg:
        err = msg["error"]
        raise MCPError(f"{err.get('code')}: {err.get('message')}")
    return msg.get("result")


def _http_headers(item, root):
    """解析 http 传输可发送的自定义头（含 @secret），并施加受信域闸。

    可信度判断在 mcp_autoconnect 侧完成（惰性 import，避免循环依赖）：
    - 非 https / 非受信域 → 返回 {}（不发任何自定义头，尤其是密钥）；
    - @secret 未存入 → **抛 MCPError（可读，不静默）**。
    """
    try:
        from mcp_autoconnect import http_headers_for
    except Exception:
        return {}
    try:
        return dict(http_headers_for(item, root) or {})
    except MCPError:
        raise
    except Exception as exc:
        raise MCPError(f"凭证解析失败（@secret 引用无法解析）：{exc}")


def _http_initialize(item, *, headers=None):
    """无状态模式下 initialize 与后续请求相互独立；失败仅影响探测，不阻断 tools/call。"""
    try:
        return _http_post(item["url"], {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": CLIENT_INFO},
        }, headers=headers) or {}
    except MCPError:
        return {}


# ---------------------------------------------------------------- 高层 API

def _require_enabled(item):
    if item.get("config_error"):
        raise MCPError(f"服务器配置无效：{item['config_error']}")
    if not item.get("enabled", True):
        raise MCPError("该 MCP 服务器已禁用，可在配置中启用后重试。")
    if item["transport"] == "stdio" and item.get("command"):
        item = dict(item)
        item["command"] = resolve_command(item["command"])
    return item


def probe_server(root, key, timeout=INIT_TIMEOUT):
    """探测一个服务器：initialize + tools/list，返回协议版本与工具数量。"""
    item = _require_enabled(get_server_config(root, key))
    started = time.monotonic()
    if item["transport"] == "stdio":
        sess = None
        try:
            sess = _session_for(root, item)
            tools = sess.request("tools/list", {}, timeout=min(timeout, 30))
            names = [t.get("name") for t in (tools or {}).get("tools", [])]
            return {"ok": True, "server": key, "tool_count": len(names),
                    "tools": names, "transport": "stdio",
                    "elapsed_ms": int((time.monotonic() - started) * 1000)}
        finally:
            if sess is not None:
                pass  # 保留长驻会话供后续调用
    else:
        headers = _http_headers(item, root)
        _http_initialize(item, headers=headers)
        result = _http_post(item["url"], {"jsonrpc": "2.0", "id": 2,
                                          "method": "tools/list", "params": {}},
                            headers=headers) or {}
        names = [t.get("name") for t in result.get("tools", [])]
        return {"ok": True, "server": key, "tool_count": len(names),
                "tools": names, "transport": "http",
                "elapsed_ms": int((time.monotonic() - started) * 1000)}


def list_tools(root, key):
    """列出某服务器的工具（含输入 schema），供前端调用面板渲染。"""
    item = _require_enabled(get_server_config(root, key))
    if item["transport"] == "stdio":
        sess = _session_for(root, item)
        result = sess.request("tools/list", {}, timeout=30) or {}
    else:
        headers = _http_headers(item, root)
        _http_initialize(item, headers=headers)
        result = _http_post(item["url"], {"jsonrpc": "2.0", "id": 2,
                                          "method": "tools/list", "params": {}},
                            headers=headers) or {}
    tools = []
    for t in result.get("tools", []):
        if not isinstance(t, dict) or not t.get("name"):
            continue
        tools.append({"name": t["name"], "description": t.get("description") or "",
                      "input_schema": t.get("inputSchema") or {"type": "object"}})
    return {"ok": True, "server": key, "tools": tools, "count": len(tools)}


def call_tool(root, key, name, arguments=None, timeout=CALL_TIMEOUT):
    """调用 MCP 工具，统一抽取文本/结构化结果。"""
    item = _require_enabled(get_server_config(root, key))
    params = {"name": name, "arguments": arguments or {}}
    hook_payload = {
        "connector": str(key)[:120],
        "tool": str(name)[:160],
        "transport": str(item.get("transport") or "")[:40],
        "timeout_s": max(0.0, float(timeout)),
        "argument_chars": len(json.dumps(arguments or {}, ensure_ascii=False, default=str)),
    }
    try:
        import hooks as _workflow_hooks
    except Exception:  # pragma: no cover - hooks are optional at runtime
        _workflow_hooks = None
    if _workflow_hooks is not None:
        before = _workflow_hooks.run_workflow("before_mcp", hook_payload)
        if before.get("blocked"):
            reason = str(before.get("reason") or "MCP 生命周期钩子拦截")[:300]
            raise MCPError("MCP 调用已被拦截：%s" % reason)
    started = time.monotonic()
    result = None
    error_text = ""
    try:
        if item["transport"] == "stdio":
            sess = _session_for(root, item)
            result = sess.request("tools/call", params, timeout=timeout) or {}
        else:
            headers = _http_headers(item, root)
            _http_initialize(item, headers=headers)
            result = _http_post(item["url"], {"jsonrpc": "2.0", "id": 3,
                                              "method": "tools/call", "params": params},
                                timeout=timeout, headers=headers) or {}
    except Exception as exc:
        error_text = "%s: %s" % (type(exc).__name__, str(exc)[:240])
        raise
    finally:
        if _workflow_hooks is not None:
            after = _workflow_hooks.run_workflow("after_mcp", {
                **hook_payload,
                "ok": bool(isinstance(result, dict) and not result.get("isError", False)
                           and not error_text),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error": error_text,
            })
            if after.get("blocked") and not error_text:
                # A post-MCP block is a failed call from the caller's point of
                # view, even though the remote server may have completed it.
                raise MCPError("MCP 结果未放行：%s" %
                               str(after.get("reason") or "需要人工审核")[:300])
    text, structured, blocks, images = extract_text(result)
    return {"ok": not result.get("isError", False), "server": key, "name": name,
            "is_error": bool(result.get("isError", False)),
            "text": text, "structured": structured, "other_blocks": blocks,
            "images": images}


def call_tool_with_fallback(root, key, name, arguments=None, *, fallback_keys=None,
                            task_hint="", timeout=CALL_TIMEOUT, max_attempts=3,
                            side_effect=False, allow_side_effect_fallback=False):
    """Call an MCP tool and try bounded connector alternatives after failure.

    Fallback is explicit and bounded because a second connector may have side
    effects or expose a different tool set.  Callers can provide ordered
    ``fallback_keys``; when omitted, the semantic router supplies candidates
    for ``task_hint``.  A successful response returns the same shape as
    :func:`call_tool` plus an ``attempts`` audit trail.  If every attempt
    fails, the original :class:`MCPError` is raised with the diagnostics
    attached to ``attempts`` on the exception for the caller to report.
    """
    primary = str(key or "").strip()
    if not primary:
        raise MCPError("MCP 调用失败：缺少连接器 key")
    ordered = []
    for candidate in (fallback_keys or []):
        candidate = str(candidate or "").strip()
        if candidate and candidate not in ordered and candidate != primary:
            ordered.append(candidate)
    if not ordered and task_hint:
        for row in select_connector(root, task_hint):
            candidate = str(row.get("key") or "").strip()
            if candidate and candidate != primary and candidate not in ordered:
                ordered.append(candidate)
    # A lost response can mean a mutating tool already ran.  Never repeat a
    # side effect across connectors unless the caller explicitly accepts that
    # risk (for example, an idempotent create with a durable operation key).
    fallback_allowed = not side_effect or bool(allow_side_effect_fallback)
    limit = max(1, min(8, int(max_attempts))) if fallback_allowed else 1
    candidates = [primary, *ordered[: max(0, limit - 1)]]
    attempts = []
    last_error = None
    for candidate in candidates:
        started = time.monotonic()
        retry_hook = None
        try:
            import hooks as _workflow_hooks
            retry_hook = _workflow_hooks.run_workflow("mcp_retry", {
                "connector": candidate, "attempt": len(attempts) + 1,
                "side_effect": bool(side_effect),
            })
        except Exception:
            # Hook import/runtime errors are observational only.
            _workflow_hooks = None
            retry_hook = None
        if retry_hook and retry_hook.get("blocked"):
            attempts.append({"key": candidate, "ok": False,
                             "blocked": True,
                             "error": str(retry_hook.get("reason") or "MCP 重试被拦截")[:240],
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})
            blocked = MCPError("MCP 重试已被拦截：%s" %
                               str(retry_hook.get("reason") or "需要人工审核")[:300])
            blocked.attempts = attempts
            blocked.fallback_allowed = fallback_allowed
            raise blocked
        try:
            response = call_tool(root, candidate, name, arguments, timeout=timeout)
            attempts.append({"key": candidate, "ok": bool(response.get("ok")),
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})
            if response.get("ok"):
                response["attempts"] = attempts
                response["fallback_allowed"] = fallback_allowed
                return response
            last_error = MCPError("MCP 工具返回 isError")
        except MCPError as exc:
            last_error = exc
            attempts.append({"key": candidate, "ok": False,
                             "error": str(exc)[:240],
                             "elapsed_ms": int((time.monotonic() - started) * 1000)})
    if last_error is None:
        last_error = MCPError("没有可用的 MCP 连接器")
    last_error.attempts = attempts
    last_error.fallback_allowed = fallback_allowed
    raise last_error
