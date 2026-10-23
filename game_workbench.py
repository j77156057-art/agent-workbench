"""Game-development helpers built on top of the region workspace."""
import json, os, re, subprocess, math, time, mimetypes, sys, ast as _ast, urllib.request, urllib.parse, urllib.error, shutil, zipfile, tempfile, uuid, hashlib, threading
from collections import deque
import mcp_client
import gpu_coordinator as _gpu
import project_state as _project_state
from gpu_coordinator import process_environment as _gpu_process_environment
from datetime import datetime

# ComfyUI 作业租约 TTL：提交后到生成完成之间即使 Agent 工作台 崩了/不再轮询，
# 租约也会在该秒数后自动回收，不会把 GPU 锁死（DOCMIND_COMFY_JOB_TTL 可调）。
COMFY_JOB_TTL = float(os.getenv("DOCMIND_COMFY_JOB_TTL", "600") or 600)
# 提交 ComfyUI 作业要求的显存余量（MB）：余量不足时先触发 Ollama 卸载钩子，
# 腾不出来就直接拒绝（不排队）。0=不检查（DOCMIND_GPU_MIN_FREE_MB 同名语义）。
COMFY_MIN_FREE_MB = float(os.getenv("DOCMIND_COMFY_MIN_FREE_MB", "1024") or 0)

_ENGINE_PROCS = {}
_ENGINE_LOGS = {}
# 保留最后一次启动参数，供 Godot 热重载在重启进程后恢复原来的嵌入方式。
_ENGINE_LAUNCH = {}
# root_abs -> 文件监视基线。只记录可影响运行结果的 Godot 项目文件，
# 不把 .godot、构建产物或依赖目录的内部变化误报给用户。
_ENGINE_SNAPSHOT = {}
# prompt_id -> 后台 watch 作业状态（轮询线程维护，不再单独持有 GPU 租约：
# 租约由 comfy_queue 提交后 reown 给 comfyui:{prompt_id}，终态时由 history 释放）
# Legacy/no-project bucket is kept for direct callers and old tests.  HTTP
# requests always pass their explicit project root and use a separate bucket.
_COMFY_JOBS = {}
_COMFY_PROJECT_JOBS = {}
_COMFY_LOADED = set()
_COMFY_JOBS_LOCK = threading.Lock()
# Kept as a compatibility constant for integrations that imported it; project
# requests resolve their file through _comfy_history_file(root) below.
_COMFY_HISTORY_FILE = os.getenv('DOCMIND_COMFY_HISTORY_FILE', os.path.join('.docmind','comfy_history.json'))
_COMFY_SERVICE = None
def _comfy_key(root=None):
    return os.path.abspath(root) if root else ''

def _comfy_jobs(root=None):
    key = _comfy_key(root)
    bucket = _COMFY_JOBS if not key else _COMFY_PROJECT_JOBS.setdefault(key, {})
    if key not in _COMFY_LOADED:
        _load_comfy_history(root)
    return bucket

def _comfy_history_file(root=None):
    if root:
        return _project_state.path(root, 'comfy_history.json', legacy=os.path.join('.docmind', 'comfy_history.json'))
    return _COMFY_HISTORY_FILE

def _save_comfy_history(root=None):
    try:
        path = _comfy_history_file(root)
        jobs = _COMFY_JOBS if not root else _comfy_jobs(root)
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f: json.dump(jobs, f, ensure_ascii=False)
    except Exception: pass
def _load_comfy_history(root=None):
    key = _comfy_key(root)
    if key in _COMFY_LOADED:
        return
    jobs = _COMFY_JOBS if not root else _COMFY_PROJECT_JOBS.setdefault(key, {})
    _COMFY_LOADED.add(key)
    try:
        with open(_comfy_history_file(root), encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            # A daemon restart cannot safely resume a remote ComfyUI watcher.
            # Mark unfinished records explicitly so the UI never presents a
            # stale running job as live; completed/failed history is retained.
            for key, value in data.items():
                if not isinstance(value, dict):
                    continue
                row = dict(value)
                if row.get('running') or row.get('status') in ('queued', 'running'):
                    row.update({'running': False, 'status': 'recovered',
                                'recovered_at': datetime.now().isoformat(timespec='seconds'),
                                'recovery_note': '服务重启后未自动恢复远端任务，请确认 ComfyUI 状态后重新提交'})
                jobs[str(key)] = row
    except Exception: pass
# root_abs -> 嵌入状态 {child_hwnd, host_hwnd, offset_y, title, dpi, size}；
# 保存它是为了"停止/解除嵌入"时能把引擎窗口原样还原，而不是留下一个失效的子窗口。
_EMBED_STATE = {}
# 嵌入时在宿主顶部留出的像素高度：保住工作台顶栏（试玩器 / 停止按钮都在那儿），
# 否则引擎窗口铺满整个宿主，用户连"停止引擎"都点不到。
EMBED_TOP_STRIP = 46
ENGINE_CATALOG = [
 {'id':'godot','name':'Godot 4','executable':'godot','download':'https://godotengine.org/download/windows/','project_file':'project.godot'},
 {'id':'unity','name':'Unity','executable':'Unity.exe','download':'https://unity.com/download','project_file':'ProjectSettings/ProjectVersion.txt'},
 {'id':'unreal','name':'Unreal Engine','executable':'UnrealEditor.exe','download':'https://www.unrealengine.com/download','project_file':'*.uproject'},
]
def engine_catalog(): return {'ok':True,'engines':ENGINE_CATALOG}

def engine_scan(root):
    """识别并摘要 Godot/Unity/Unreal 项目文件，供适配层和 AI 定位入口。"""
    base = _root(root); found=[]
    for dp, _, files in os.walk(base):
        if any(x in dp.split(os.sep) for x in ('.git','node_modules','.venv','Library','Intermediate','DerivedDataCache')): continue
        for fn in files:
            rel=os.path.relpath(os.path.join(dp,fn),base).replace('\\','/')
            if fn=='project.godot': found.append({'engine':'godot','path':rel})
            elif fn=='ProjectVersion.txt': found.append({'engine':'unity','path':rel})
            elif fn.endswith('.uproject'): found.append({'engine':'unreal','path':rel})
    return {'ok':True,'projects':found}

def engine_inspect(root, engine=''):
    """深度读取 Unity/Unreal 项目文本资产，建立可供 AI 定位的轻量索引。"""
    base=_root(root); result={'ok':True,'engine':engine,'manifests':[],'scenes':[],'prefabs':[],'assets':[],'symbols':[],'blueprints':[],'levels':[]}
    projects=engine_scan(base).get('projects',[])
    if not engine and projects: engine=projects[0]['engine']; result['engine']=engine
    if engine=='unity':
        ver=os.path.join(base,'ProjectSettings','ProjectVersion.txt')
        if os.path.isfile(ver):
            result['manifests'].append({'path':'ProjectSettings/ProjectVersion.txt','version':next((x.split(':',1)[1].strip() for x in open(ver,encoding='utf-8',errors='replace') if x.startswith('m_EditorVersion:' )), '')})
        for dp,_,files in os.walk(base):
            if any(x in dp.split(os.sep) for x in ('.git','Library','Temp','Logs','obj')): continue
            for fn in files:
                rel=os.path.relpath(os.path.join(dp,fn),base).replace('\\','/')
                if fn.endswith('.unity'): result['scenes'].append({'path':rel})
                elif fn.endswith('.prefab'): result['prefabs'].append({'path':rel})
                elif fn.endswith('.meta'):
                    try:
                        text=open(os.path.join(dp,fn),encoding='utf-8',errors='replace').read(4000)
                        m=re.search(r'^guid:\s*([0-9a-fA-F]+)',text,re.M)
                        if m: result['assets'].append({'path':rel[:-5],'meta':rel,'guid':m.group(1)})
                    except OSError: pass
    elif engine=='unreal':
        for p in projects:
            if p['engine']!='unreal': continue
            path=os.path.join(base,p['path'])
            try:
                with open(path,encoding='utf-8',errors='replace') as f: data=json.load(f)
                result['manifests'].append({'path':p['path'],'file_version':data.get('FileVersion'),
                    'modules':[x.get('Name') for x in data.get('Modules',[]) if isinstance(x,dict)],
                    'plugins':[x.get('Name') for x in data.get('Plugins',[]) if isinstance(x,dict)],
                    'targets':data.get('TargetPlatforms',[])})
            except Exception: result['manifests'].append({'path':p['path'],'error':'invalid json'})
        for dp,_,files in os.walk(os.path.join(base,'Source')) if os.path.isdir(os.path.join(base,'Source')) else []:
            for fn in files:
                if fn.endswith(('.h','.cpp','.cs','.Build.cs')):
                    rel=os.path.relpath(os.path.join(dp,fn),base).replace('\\','/')
                    item={'path':rel,'kind':'build' if fn.endswith('.Build.cs') else 'source'}
                    if fn.endswith('.Build.cs'):
                        try:
                            text=open(os.path.join(dp,fn),encoding='utf-8',errors='replace').read(12000)
                            item['dependencies']=re.findall(r'"([A-Za-z0-9_]+)"', text)
                        except OSError: pass
                    result['symbols'].append(item)
        for dp,_,files in os.walk(base):
            if any(x in dp.split(os.sep) for x in ('.git','Intermediate','DerivedDataCache','Saved')): continue
            for fn in files:
                rel=os.path.relpath(os.path.join(dp,fn),base).replace('\\','/')
                if fn.endswith('.uplugin'): result['assets'].append({'path':rel,'kind':'plugin'})
                elif fn.endswith('.umap'):
                    result['levels'].append({'path':rel,'kind':'level'})
                elif fn.endswith('.uasset'):
                    low=fn.lower()
                    kind='blueprint' if ('blueprint' in low or low.startswith('bp_') or low.endswith('_bp.uasset')) else 'asset'
                    result['blueprints' if kind=='blueprint' else 'assets'].append({'path':rel,'kind':kind})
    return result

def install_unreal_bridge(root, force=False):
    """安装 Unreal Editor Python 桥接脚本；不修改 .uasset。"""
    base = _root(root)
    if not any(x.get('engine') == 'unreal' for x in engine_scan(base).get('projects', [])):
        return {'ok': False, 'error': '未找到 Unreal .uproject。'}
    rel = 'Content/Python/docmind_bridge.py'; path = _file(base, rel)
    if os.path.exists(path) and not force: return {'ok': False, 'error': '桥接脚本已存在，请使用 force 覆盖。', 'path': rel}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = '''"""Agent Workbench Unreal Editor Python HTTP bridge."""
import json, unreal
from http.server import BaseHTTPRequestHandler, HTTPServer
def list_assets(asset_class="Blueprint"):
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    return [str(x.object_path) for x in ar.get_assets_by_class(asset_class)]
def list_level_actors():
    return [{"name": a.get_name(), "class": a.get_class().get_name(), "path": a.get_path_name()} for a in unreal.EditorLevelLibrary.get_all_level_actors()]
class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = {"ok": True, "service": "docmind-unreal"}
        if self.path.startswith("/health"): data["available"] = True
        elif self.path.startswith("/assets"): data["assets"] = list_assets()
        elif self.path.startswith("/actors"): data["actors"] = list_level_actors()
        elif self.path.startswith("/blueprint/"): data.update({"blueprint": self.path.split('/blueprint/',1)[1], "nodes": [], "variables": [], "links": []})
        elif self.path.startswith("/actor/"): data.update({"actor": self.path.split('/actor/',1)[1], "components": [], "properties": []})
        body=json.dumps(data).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *_): pass
def run_server(port=8765):
    HTTPServer(("127.0.0.1", int(port)), _Handler).serve_forever()
'''
    with open(path, 'w', encoding='utf-8', newline='\n') as f: f.write(source)
    return {'ok': True, 'path': rel, 'created': True, 'note': '需启用 Unreal Editor Python Script Plugin 后执行。'}

def engine_prepare(root, engine='godot', executable=''):
    """为 AI 提供幂等的引擎准备动作：探测可执行文件并写入项目配置。"""
    if engine not in {x['id'] for x in ENGINE_CATALOG}: return {'ok':False,'error':'不支持的游戏引擎。'}
    path=_resolve_engine_executable(engine, executable or next(x['executable'] for x in ENGINE_CATALOG if x['id']==engine))
    if not path or not os.path.isfile(path):
        return {'ok':False,'ready':False,'engine':engine,'download':next(x['download'] for x in ENGINE_CATALOG if x['id']==engine),'error':'未找到引擎可执行文件。'}
    cfg=engine_config(root, engine, path)
    return {'ok':True,'ready':True,'config':cfg,'projects':engine_scan(root).get('projects',[])}
def engine_config(root, engine=None, executable=None):
 path=_project_state.path(root, 'engine.json', legacy='.docmind_engine.json')
 if engine is not None:
  item=next((x for x in ENGINE_CATALOG if x['id']==engine),None)
  if not item: return {'ok':False,'error':'不支持的游戏引擎。'}
  with open(path,'w',encoding='utf-8') as f: json.dump({'engine':engine,'executable':executable or item['executable']},f,ensure_ascii=False,indent=2)
 try:
  with open(path,encoding='utf-8') as f: data=json.load(f)
 except Exception: data={'engine':'godot','executable':'godot'}
 return {'ok':True,**data}

def _scan_godot_dirs():
    """在常见安装目录浅层搜索 Godot exe，优先控制台版（headless 才能捕获 stdout）。"""
    dirs = [
        os.environ.get('LOCALAPPDATA', '') and os.path.join(os.environ['LOCALAPPDATA'], 'Godot'),
        os.environ.get('PROGRAMFILES', '') and os.path.join(os.environ['PROGRAMFILES'], 'Godot'),
        os.environ.get('PROGRAMFILES(X86)', '') and os.path.join(os.environ['PROGRAMFILES(X86)'], 'Godot'),
        r'D:\Tools\Godot', r'C:\Tools\Godot', r'D:\Godot', r'C:\Godot',
        os.path.expandvars(r'%USERPROFILE%\scoop\apps\godot\current'),
    ]
    hits = []
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for dp, _dirs, files in os.walk(d):
            depth = os.path.relpath(dp, d).count(os.sep)
            if depth > 2:
                _dirs[:] = []
                continue
            for fn in files:
                if re.fullmatch(r'Godot_[\w.-]*win64_console\.exe', fn) or fn in ('godot.exe', 'godot_console.exe'):
                    hits.append(os.path.join(dp, fn))
    return hits


def _resolve_engine_executable(engine, executable):
    if executable and os.path.isfile(executable): return executable
    hit = shutil.which(executable or '')
    if hit: return hit
    if engine == 'godot':
        # 用户显式配置/旧固定候选
        candidates = [
            os.path.expandvars(r'%LOCALAPPDATA%\Godot\godot.exe'),
            os.path.expandvars(r'%PROGRAMFILES%\Godot\godot.exe'),
            os.path.expandvars(r'%PROGRAMFILES%\Godot\Godot_v4.3-stable_win64.exe'),
        ]
        found = next((p for p in candidates if os.path.isfile(p)), '')
        if found:
            return found
        scans = _scan_godot_dirs()
        if scans:
            # 控制台版优先（同目录/同版本），其余按版本字符串排序取最高
            scans.sort(key=lambda p: ('_console' not in os.path.basename(p), p), reverse=False)
            return scans[0]
        return executable or ''
    elif engine == 'unity': candidates = [os.path.expandvars(r'%PROGRAMFILES%\Unity Hub\Editor\Unity.exe')]
    else: candidates = [os.path.expandvars(r'%PROGRAMFILES%\Epic Games\UE_5.4\Engine\Binaries\Win64\UnrealEditor.exe')]
    return next((p for p in candidates if os.path.isfile(p)), executable or '')

# ---------------------------------------------------------------- Godot 静态诊断
# Unreal/MSVC 编译诊断：D:\p\Foo.cpp(12,3): error C2065: msg
_UNREAL_DIAG_RX = re.compile(r'^\s*(?P<path>(?:[A-Za-z]:[\\/])?[^():\r\n]+\.(?:cpp|h|inl|cs|Build\.cs))\((?P<line>\d+)(?:,\d+)?\)\s*:\s*(?P<kind>error|warning)\s*(?P<msg>.*)$', re.I)


def parse_unreal_diagnostics(text):
    """解析 Unreal/MSVC 编译输出，返回与 Godot 统一的诊断结构。"""
    out, seen = [], set()
    for raw in (text or '').splitlines():
        m = _UNREAL_DIAG_RX.match(raw)
        if not m:
            continue
        path = m.group('path').replace('\\', '/').strip()
        msg = ' '.join(m.group('msg').split())
        item = (path, int(m.group('line')), m.group('kind').lower(), msg)
        if item in seen:
            continue
        seen.add(item)
        out.append({'path': path, 'line': int(m.group('line')), 'severity': item[2], 'message': msg})
    return out

# 格式 1（同行）：res://x.gd:22 - Parse Error: msg ／ x.gd:10: ERROR: msg
_GODOT_DIAG_RX = re.compile(
    r'(?:res://)?(?P<path>[A-Za-z0-9_./\\-]+\.gd):(?P<line>\d+)\s*[-:]\s*'
    r'(?P<kind>Parse Error|SCRIPT ERROR|ERROR|WARNING|Error|error)\s*:?\s*(?P<msg>.*)'
)
# 格式 2（分两行，Godot 4.x check-only 实际输出）：
#   SCRIPT ERROR: Parse Error: <msg>
#      at: GDScript::reload (res://x.gd:4)
_GODOT_SCRIPT_ERR_RX = re.compile(r'^\s*SCRIPT ERROR:\s*(?:Parse Error:\s*)?(?P<msg>.*\S)\s*$')
_GODOT_AT_RX = re.compile(r'\(res://(?P<path>[A-Za-z0-9_./\\-]+\.gd):(?P<line>\d+)\)')
# 旧版/其它路径同行情境：SCRIPT ERROR: ... (at res://x.gd:88)
_GODOT_DIAG_TAIL_RX = re.compile(
    r'SCRIPT ERROR:\s*(?:Parse Error:\s*)?(?P<msg>.*?)\s*\(at\s+res://(?P<path>[A-Za-z0-9_./\\-]+\.gd):(?P<line>\d+)\)', re.S
)


def parse_godot_diagnostics(text):
    """把 Godot headless/check-only 输出解析为 [{path,line,severity,message}]，按出现顺序去重。"""
    out, seen = [], set()

    def add(path, line, kind, msg):
        path = str(path).replace('\\', '/').removeprefix('res://').strip()
        msg = ' '.join(str(msg or '').split())
        severity = 'warning' if str(kind).upper() == 'WARNING' else 'error'
        sig = (path, int(line or 0), severity, msg)
        if msg and sig not in seen:
            seen.add(sig)
            out.append({'path': path, 'line': int(line or 0), 'severity': severity, 'message': msg})

    # 先处理同一行的 tail 格式（旧版兼容）
    for m in _GODOT_DIAG_TAIL_RX.finditer(text or ''):
        add(m.group('path'), m.group('line'), 'SCRIPT ERROR', m.group('msg'))

    pending = None  # 上一条 SCRIPT ERROR 的消息，等待下一行 (at res://x.gd:N)
    for line in (text or '').splitlines():
        inline = _GODOT_DIAG_RX.search(line)
        if inline:
            add(inline.group('path'), inline.group('line'), inline.group('kind'), inline.group('msg'))
            pending = None
            continue
        se = _GODOT_SCRIPT_ERR_RX.match(line)
        if se:
            pending = se.group('msg').strip()
            continue
        at = _GODOT_AT_RX.search(line)
        if at:
            if pending:
                add(at.group('path'), at.group('line'), 'SCRIPT ERROR', pending)
            pending = None
            continue
        # at 行允许是空行或以 at: 开头；其它实质行打断挂起消息
        if pending and line.strip() and not line.strip().startswith('at:'):
            pending = None
    return out


def _resolve_project_godot(root, executable=''):
    """读取项目引擎配置并解析出 Godot 可执行文件；非 Godot 项目/找不到时返回错误串。"""
    cfg = engine_config(root)
    selected = cfg.get('engine', 'godot')
    if selected != 'godot':
        return None, f'当前配置的引擎是 {selected}，Godot 校验仅适用于 Godot 项目。'
    exe = executable if executable and executable != 'godot' else cfg.get('executable', 'godot')
    exe = _resolve_engine_executable('godot', exe)
    if not exe or not os.path.isfile(exe):
        return None, '未找到 Godot 可执行文件，请在引擎配置中填写 Godot exe 绝对路径（建议用 _console 版）。'
    return exe, ''


def _project_autoloads(root_abs):
    """读取 project.godot [autoload] 节的单例名集合（--check-only 不注册 autoload 全局，需据此过滤误报）。"""
    proj = os.path.join(root_abs, 'project.godot')
    try:
        with open(proj, encoding='utf-8-sig') as f:
            text = f.read()
    except OSError:
        return set()
    sec = re.search(r'^\[autoload\](?P<body>.*?)(?=^\[|\Z)', text, re.S | re.M)
    if not sec:
        return set()
    return set(re.findall(r'^\s*([A-Za-z_]\w*)\s*=', sec.group('body'), re.M))


_AUTOLOAD_MISS_RX = re.compile(r'Identifier (?:not found: ?|")([A-Za-z_]\w*)')


def godot_check_script(root, rel, executable='', timeout=120):
    """单文件 GDScript 校验：godot --headless --check-only --script。

    返回该文件诊断（同时附带全工程诊断供参考）。--check-only 不打开编辑器，
    速度快，适合保存后即时反馈。
    """
    root_abs = _root(root)
    rel = str(rel or '').replace('\\', '/').lstrip('/')
    if not rel.endswith('.gd'):
        return {'ok': False, 'error': '仅支持 .gd 文件的单文件校验。'}
    try:
        abs_path = _file(root_abs, rel)
    except ValueError:
        return {'ok': False, 'error': '路径超出代码库范围。'}
    if not os.path.isfile(abs_path):
        return {'ok': False, 'error': f'文件不存在：{rel}'}
    exe, err = _resolve_project_godot(root_abs, executable)
    if err:
        return {'ok': False, 'error': err}
    cmd = [exe, '--head', '--path', root_abs, '--check-only', '--script', abs_path]
    try:
        p = subprocess.run(cmd, cwd=root_abs, capture_output=True, text=True,
                           timeout=max(5, min(int(timeout), 300)),
                           encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return {'ok': False, 'error': f'无法启动 Godot：{exe}'}
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'Godot 单文件校验超时。'}
    output = (p.stdout or '') + '\n' + (p.stderr or '')
    autoloads = _project_autoloads(root_abs)

    def _is_autoload_false_positive(d):
        if d['severity'] != 'error' or 'Identifier' not in d['message']:
            return False
        m = _AUTOLOAD_MISS_RX.search(d['message'])
        return bool(m and m.group(1) in autoloads)

    diags = [d for d in parse_godot_diagnostics(output) if not _is_autoload_false_positive(d)]
    target = [d for d in diags if d['path'] == rel]
    errors = [d for d in diags if d['severity'] == 'error']
    # returncode 在校验到编译错误时恒为 1；误报过滤后以「是否仍有错误诊断」为准。
    return {'ok': not errors, 'returncode': p.returncode,
            'path': rel, 'diagnostics': target, 'all_diagnostics': diags,
            'output': output[-4000:]}


# ---------------------------------------------------------------- godot-ai 插件安装
GODOT_AI_REPO_API = 'https://api.github.com/repos/hi-godot/godot-ai/releases/latest'
GODOT_AI_MIN_VERSION = (4, 0, 0)


def _parse_plugin_version(text):
    m = re.search(r'^\s*version\s*=\s*"([^"]+)"', text or '', re.M)
    return m.group(1) if m else ''


def _plugin_enabled(project_text):
    sec = re.search(r'^\[editor_plugins\](?P<body>.*?)(?=^\[|\Z)', project_text or '', re.S | re.M)
    if not sec:
        return False
    m = re.search(r'^\s*enabled\s*=\s*PackedStringArray\((?P<items>[^)]*)\)', sec.group('body'), re.M)
    if not m:
        return False
    return 'godot_ai' in re.findall(r'"([^"]+)"', m.group('items'))


def _enable_plugin_in_project_text(text):
    """在 project.godot 的 [editor_plugins] 中幂等启用 godot_ai。"""
    text = text if text.endswith('\n') else text + '\n'
    sec = re.search(r'^(\[editor_plugins\]\n)(?P<body>.*?)(?=^\[|\Z)', text, re.S | re.M)
    if not sec:
        return text + '\n[editor_plugins]\nenabled=PackedStringArray("godot_ai")\n'
    body = sec.group('body')
    m = re.search(r'^(\s*enabled\s*=\s*PackedStringArray\()([^)]*)(\))', body, re.M)
    if m:
        items = re.findall(r'"([^"]+)"', m.group(2))
        if 'godot_ai' in items:
            return text
        items.append('godot_ai')
        new_line = m.group(1) + ', '.join(f'"{x}"' for x in items) + m.group(3)
        return text[:sec.start('body')] + body.replace(m.group(0), new_line, 1) + text[sec.end('body'):]
    new_body = body + 'enabled=PackedStringArray("godot_ai")\n'
    return text[:sec.start('body')] + new_body + text[sec.end('body'):]


def godot_addon_status(root):
    """godot-ai 插件与接入前置条件摘要（供前端安装引导）。"""
    root_abs = _root(root)
    project_file = os.path.join(root_abs, 'project.godot')
    plugin_cfg = os.path.join(root_abs, 'addons', 'godot_ai', 'plugin.cfg')
    result = {'ok': True, 'is_godot_project': os.path.isfile(project_file),
              'installed': os.path.isfile(plugin_cfg), 'version': '', 'enabled': False,
              'uvx': mcp_client.resolve_command('uvx'),
              'uvx_available': os.path.isfile(mcp_client.resolve_command('uvx')),
              'godot': _resolve_engine_executable('godot', engine_config(root_abs).get('executable', 'godot')),
              'min_version': '.'.join(map(str, GODOT_AI_MIN_VERSION))}
    if result['installed']:
        try:
            with open(plugin_cfg, encoding='utf-8') as f:
                result['version'] = _parse_plugin_version(f.read())
        except OSError:
            pass
    if result['is_godot_project']:
        try:
            with open(project_file, encoding='utf-8') as f:
                result['enabled'] = _plugin_enabled(f.read())
        except OSError:
            pass
    result['godot_available'] = bool(result['godot'] and os.path.isfile(result['godot']))
    return result


def _pick_addon_asset(assets):
    zips = [a for a in assets if str(a.get('name', '')).lower().endswith('.zip')]
    if not zips:
        return None
    for a in zips:
        name = str(a.get('name', '')).lower()
        if 'plugin' in name or 'addon' in name or 'asset' in name:
            return a
    return zips[0]


def install_godot_addon(root, force=False):
    """从 GitHub Releases 下载安装 godot-ai 插件并在 project.godot 启用。

    必须由上层 API 在用户明确确认后调用（会改写用户项目目录与 project.godot）。
    """
    root_abs = _root(root)
    if not os.path.isfile(os.path.join(root_abs, 'project.godot')):
        return {'ok': False, 'error': '当前代码库不是 Godot 项目（缺少 project.godot）。'}
    status = godot_addon_status(root_abs)
    if status['installed'] and not force:
        return {'ok': False, 'error': 'godot-ai 插件已安装，如需重装请先确认覆盖。', 'status': status}
    if not status['uvx_available']:
        return {'ok': False, 'error': '未找到 uvx，请先安装 uv（https://docs.astral.sh/uv/getting-started/installation/）。'}
    try:
        req = urllib.request.Request(GODOT_AI_REPO_API, headers={'User-Agent': 'docmind-workbench', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            release = json.loads(r.read().decode())
    except Exception as e:
        return {'ok': False, 'error': f'获取 godot-ai 最新版本失败：{e}'}
    asset = _pick_addon_asset(release.get('assets') or [])
    if not asset:
        return {'ok': False, 'error': f'发布版本 {release.get("tag_name", "?")} 中未找到插件 zip 包，请按 Release 说明手动安装。'}
    try:
        with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
            tmp_path = tmp.name
            req = urllib.request.Request(asset['browser_download_url'], headers={'User-Agent': 'docmind-workbench'})
            with urllib.request.urlopen(req, timeout=120) as r:
                shutil.copyfileobj(r, tmp)
        extracted = []
        with zipfile.ZipFile(tmp_path) as zf:
            members = zf.namelist()
            marker = next((n for n in members if n.endswith('addons/godot_ai/plugin.cfg')), None)
            if not marker:
                return {'ok': False, 'error': f"压缩包 {asset['name']} 中未找到 addons/godot_ai/plugin.cfg，请确认包格式。"}
            prefix = marker[: marker.index('addons/')]
            for n in members:
                if n.endswith('/') or not n.startswith(prefix + 'addons/'):
                    continue
                rel_part = n[len(prefix):].replace('\\', '/')
                target = os.path.join(root_abs, *rel_part.split('/'))
                if not os.path.abspath(target).startswith(root_abs + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(n) as src, open(target, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(rel_part)
    except Exception as e:
        return {'ok': False, 'error': f'下载/解压插件失败：{e}'}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # 启用插件
    project_path = os.path.join(root_abs, 'project.godot')
    try:
        with open(project_path, encoding='utf-8') as f:
            project_text = f.read()
        with open(project_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(_enable_plugin_in_project_text(project_text))
    except OSError as e:
        return {'ok': False, 'error': f'插件文件已解压，但写入 project.godot 失败：{e}'}

    status = godot_addon_status(root_abs)
    version = status.get('version') or str(release.get('tag_name', '')).lstrip('v')
    try:
        if version:
            mcp_client.pin_godot_server(root_abs, version)
    except Exception:
        pass
    return {'ok': True, 'installed': True, 'version': version, 'enabled': True,
            'release_tag': release.get('tag_name'), 'files': len(extracted),
            'next': '重启（或打开）Godot 编辑器使插件生效，然后在 AI 对话台连接 godot-ai。'}

COMFY_MAX_DOWNLOAD = 25 * 1024 * 1024

def _safe_comfy_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or p.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("ComfyUI 地址仅允许本机 localhost/127.0.0.1。")
    return url.rstrip('/')

def engine_status(root):
    p = _ENGINE_PROCS.get(_root(root)); running = bool(p and p.poll() is None)
    embed = _EMBED_STATE.get(_root(root)) or {}
    out = {"ok": True, "running": running, "pid": p.pid if running else None,
           "embedded": bool(embed) and running}
    if embed:
        out.update({"child_hwnd": embed.get('child_hwnd'), "host_hwnd": embed.get('host_hwnd'),
                    "embed_title": embed.get('title'), "embed_offset_y": embed.get('offset_y'),
                    "embed_dpi": embed.get('dpi'), "host_dpi": embed.get('host_dpi'),
                    "embed_size": embed.get('size'), "embed_mode": embed.get('mode')})
    return out


def _engine_snapshot(root):
    """Capture lightweight mtimes/sizes for files that can affect a Godot run."""
    root_abs = _root(root)
    extensions = {'.gd', '.tscn', '.tres', '.shader', '.gdshader', '.gdextension',
                  '.godot', '.cfg', '.json'}
    # `.docmind` / `.docmind_*` 是 DocMind 自己的运行期状态（工作流快照、
    # 项目画像、引擎配置），不是用户源码。它们会在 engine_start 之后继续
    # 写入；若算进快照，engine.log 就永远"早于最后一次改动"，runtime_evidence
    # 会把一轮干净的真跑误判成没跑过（实测 project-profile.json 比日志晚 93s）。
    ignored = {'.git', '.godot', '.venv', 'node_modules', 'build', 'dist',
               '__pycache__', '.docmind'}
    rows = {}
    if not os.path.isdir(root_abs):
        return rows
    for base, dirs, files in os.walk(root_abs):
        dirs[:] = [d for d in dirs if d not in ignored]
        for name in files:
            if name.startswith('.docmind_'):
                continue
            if name == 'project.godot' or os.path.splitext(name)[1].lower() in extensions:
                path = os.path.join(base, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rel = os.path.relpath(path, root_abs).replace('\\', '/')
                rows[rel] = {'mtime_ns': st.st_mtime_ns, 'size': st.st_size}
    return rows


def engine_changes(root):
    """Report project files changed since the current engine instance started."""
    root_abs = _root(root)
    baseline = _ENGINE_SNAPSHOT.get(root_abs)
    if baseline is None:
        return {'ok': True, 'running': bool(engine_status(root_abs).get('running')),
                'changed': [], 'added': [], 'deleted': [], 'count': 0}
    current = _engine_snapshot(root_abs)
    changed = sorted([
        path for path, meta in current.items()
        if path in baseline and meta != baseline[path]
    ])
    added = sorted([path for path in current if path not in baseline])
    deleted = sorted([path for path in baseline if path not in current])
    files = sorted(set(changed + added + deleted))
    return {'ok': True, 'running': bool(engine_status(root_abs).get('running')),
            'changed': changed, 'added': added, 'deleted': deleted,
            'files': files, 'count': len(files)}


def engine_embed(root, host_hwnd, width=None, height=None, title_hint='',
                 offset_y=EMBED_TOP_STRIP, rect=None, fill=False):
    """把已运行的引擎窗口嵌进宿主窗口。

    `rect` = 前端口算出的"引擎视窗"（宿主客户区坐标，物理像素）；给了它引擎就只占那一块，
    工作台界面照常可用。`rect` 缺失时默认**有界框降级**（在宿主客户区里嵌一块居中留边的小窗），
    绝不悄悄铺满全屏——`fill=True` 才显式请求铺满（会盖住工作台界面，仅限明确意图）。
    """
    root_abs = _root(root)
    st = engine_status(root_abs)
    if not st.get('running'):
        return {'ok': False, 'error': '引擎尚未运行。'}
    if not host_hwnd:
        return {'ok': False, 'error': '没有桌面宿主窗口（浏览器模式下无法嵌入，请用桌面端启动）。'}
    try:
        from desktop_bridge import embed, ensure_dpi_awareness, find_window, is_window
        ensure_dpi_awareness()   # 父/子窗口必须在同一套坐标空间，否则 150% 缩放下会错位
        # 已经嵌进去过就直接复用旧句柄：嵌入之后窗口变成了宿主的子窗口，而 EnumWindows
        # 只枚举顶层窗口——再走一遍 find_window 必然找不到，会误报"引擎窗口没出现"。
        prev = _EMBED_STATE.get(root_abs) or {}
        hwnd_prev = int(prev.get('child_hwnd') or 0)
        if hwnd_prev and is_window(hwnd_prev):
            child = (hwnd_prev, prev.get('title') or '')
        else:
            child = find_window(st['pid'], title_hint)
        if not child:
            return {'ok': False, 'error': '尚未找到引擎窗口，请稍后重试。'}
        r = embed(child[0], int(host_hwnd), width, height, offset_y, title=child[1], rect=rect, fill=fill)
        if not r.get('ok'):
            return r
        _EMBED_STATE[root_abs] = {'child_hwnd': r['hwnd'], 'host_hwnd': int(host_hwnd),
                                  'offset_y': int(offset_y), 'title': child[1],
                                  'dpi': r.get('dpi'), 'host_dpi': r.get('host_dpi'),
                                  'mode': r.get('mode'),
                                  'rect': dict(rect) if isinstance(rect, dict) else None,
                                  'size': {'width': r.get('width'), 'height': r.get('height')}}
        return r
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def engine_place(root, x, y, width, height):
    """引擎视窗（rect 模式）随前端布局变化同步位置与尺寸。"""
    root_abs = _root(root)
    state = _EMBED_STATE.get(root_abs)
    if not state:
        return {'ok': False, 'error': '引擎未嵌入。'}
    try:
        from desktop_bridge import place as bridge_place
        r = bridge_place(state['child_hwnd'], x, y, width, height)
        if r.get('ok'):
            state['mode'] = 'rect'
            state['rect'] = {'x': int(x), 'y': int(y), 'width': int(width), 'height': int(height)}
            state['size'] = {'width': r.get('width'), 'height': r.get('height')}
        return r
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def engine_stop_all():
    """停止全部引擎（桌面壳关闭时调用，避免留下孤儿进程与窗口）。"""
    roots = sorted(set(_ENGINE_PROCS) | set(_EMBED_STATE))
    return {r: engine_stop(r) for r in roots}


def engine_detach(root):
    """解除嵌入，把引擎窗口还原成独立顶层窗口（引擎进程保持运行）。"""
    root_abs = _root(root)
    state = _EMBED_STATE.pop(root_abs, None)
    if not state:
        return {'ok': True, 'was_embedded': False}
    try:
        from desktop_bridge import detach
        r = detach(state['child_hwnd'])
        r['was_embedded'] = True
        return r
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'was_embedded': True, 'error': str(e)}


def engine_focus(root, keep_attached=False):
    """把键盘焦点交给嵌入的引擎窗口（用户点工作台后要把焦点还给游戏）。"""
    root_abs = _root(root)
    state = _EMBED_STATE.get(root_abs)
    if not state:
        return {'ok': False, 'error': '引擎未嵌入，无需聚焦。'}
    try:
        from desktop_bridge import focus
        return focus(state['child_hwnd'], state.get('host_hwnd'), keep_attached=keep_attached)
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def engine_window_target(root, title_hint=''):
    """定位本项目**自己启动**的引擎独立顶层窗口，返回 (hwnd, title) 或 None（**不抓像素**）。

    这是"引擎操控"的**共用找窗生命线**：
      engine_status → pid → desktop_bridge.find_window(pid) → restore_window → 短暂 settle。
    只认引擎进程表里**本项目**正在跑的那个引擎，**不接受任何外部句柄**——既不是"截任意
    窗口"的后门，也不是"点任意窗口"的后门。

    刻意不抓像素：点击投递（desktop_actions）只要句柄 + 客户区，不需要图。让"看"
    （engine_window_frame 复用它再 capture_window）和"点"（desktop_actions 复用它拿句柄）
    共用**这一个**找窗入口，避免两处各写一套脆弱的"找窗→还原"序列而漂移。

    找窗前先还原：Godot 游戏窗口会在启动几秒后**自己最小化**（客户区塌成 0x0），此时既
    抓不到像素、也读不到有效客户区尺寸，restore_window 把它还原+置前后一切才落到同一个真实句柄。
    """
    root_abs = _root(root)
    try:
        # 用模块全局名调用（不是 from-import 绑死），好让测试能 patch game_workbench.engine_status
        st = engine_status(root_abs) or {}
        if not st.get('running'):
            return None
        pid = int(st.get('pid') or 0)
        if not pid:
            return None
        import desktop_bridge
        found = desktop_bridge.find_window(pid, title_hint)
        if not found:
            return None
        hwnd, title = int(found[0]), str(found[1] or '')
        # 引擎窗口可能已自最小化：还原+置前（窗口重建客户区/像素需要一点时间）。
        desktop_bridge.restore_window(hwnd)
        time.sleep(0.35)
        return hwnd, title
    except Exception:  # noqa: BLE001
        return None


def engine_window_frame(root, title_hint=''):
    """抓本项目自己启动的引擎独立窗口画面，返回 ((raw, w, h, hwnd), title) 或 None。

    是 agent 的 game_screenshot、桌面视觉端点 (/api/vision/desktop-frame) 与视觉点击
    (visual_click) 的**共用生命线**——找窗/还原逻辑全在 engine_window_target，这里只负责
    在拿到句柄后 capture_window。多处共用一处找窗，杜绝"某出口能看见、另一处看不见"的漂移。
    """
    target = engine_window_target(root, title_hint)
    if not target:
        return None
    hwnd, title = target
    try:
        import screen_capture
        grabbed = screen_capture.capture_window(hwnd)
        if not grabbed:
            return None
        raw, width, height = grabbed
        return (raw, width, height, hwnd), title
    except Exception:  # noqa: BLE001
        return None


def engine_resize(root, offset_y=None):
    """按宿主当前客户区重排嵌入窗口（宿主 resize 后调用）。"""
    root_abs = _root(root)
    state = _EMBED_STATE.get(root_abs)
    if not state:
        return {'ok': False, 'error': '引擎未嵌入。'}
    try:
        from desktop_bridge import fill_host
        r = fill_host(state['child_hwnd'], state['offset_y'] if offset_y is None else offset_y)
        if r.get('ok'):
            state['size'] = {'width': r.get('width'), 'height': r.get('height')}
        return r
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)}


def engine_reap_dead():
    """清理"引擎异常退出 / 崩溃"（没走 /api/engine/stop）的残留状态。

    * _EMBED_STATE 里指向已死进程 root 的条目：弹出并在 desktop_bridge 注销失效 hwnd，
      否则 engine_status 会误报嵌入、embedded_children 会列幽灵窗口。
    * _ENGINE_PROCS 里已退出的进程记录：弹出（实时状态以 poll() 为准，这里只是卫生）。
    * GPU 租约（engine:<root>）由 gpu_coordinator 的进程死亡看门狗统一 force_release，
      此处不重复释放，避免双释放。
    """
    for root in list(_EMBED_STATE):
        st = _EMBED_STATE.get(root) or {}
        p = _ENGINE_PROCS.get(root)
        if p is not None and p.poll() is None:
            continue  # 进程还活着
        hwnd = st.get('child_hwnd')
        if hwnd:
            try:
                from desktop_bridge import forget
                forget(hwnd)
            except Exception:  # noqa: BLE001
                pass
        _EMBED_STATE.pop(root, None)
    for root in list(_ENGINE_PROCS):
        p = _ENGINE_PROCS.get(root)
        if p is None or p.poll() is not None:
            _ENGINE_PROCS.pop(root, None)
    return True


def engine_running_roots():
    """返回当前仍在运行的引擎 root 列表（已死进程顺带清理）。

    先调用 engine_reap_dead() 清掉崩溃残留（它会把 poll() 非 None 的记录弹出），
    再以 _ENGINE_PROCS 里 poll() is None 的 root 视为"正在运行"。返回按字典序排序，
    供"启动新引擎 / 切换项目前停掉其它项目引擎"（单实例策略）使用。
    """
    try:
        engine_reap_dead()
    except Exception:  # noqa: BLE001  清理失败不应阻断调用方
        pass
    alive = []
    for root, p in list(_ENGINE_PROCS.items()):
        if p is not None and p.poll() is None:
            alive.append(root)
    return sorted(alive)


_WATCHDOG_THREAD = None
def start_engine_watchdog(interval=3.0):
    """起一个 daemon 线程周期调用 engine_reap_dead（与 gpu_coordinator 后台线程同构）。"""
    global _WATCHDOG_THREAD
    if _WATCHDOG_THREAD is not None and _WATCHDOG_THREAD.is_alive():
        return
    def _loop():
        while True:
            try:
                engine_reap_dead()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval)
    t = threading.Thread(target=_loop, name="docmind-engine-watchdog", daemon=True)
    t.start()
    _WATCHDOG_THREAD = t


def engine_start(root, executable="godot", scene="", host_hwnd=None, embed=False, rect=None, fill=False):
    root_abs = _root(root)
    if engine_status(root_abs)["running"]: return engine_status(root_abs)
    cfg=engine_config(root); selected=cfg.get('engine','godot'); executable=cfg.get('executable','godot') if not executable or (executable == 'godot' and selected != 'godot') else executable; executable=_resolve_engine_executable(selected, executable)
    # Godot 的 *_console.exe 只是转发器（0.2MB），窗口模式会派生 GUI 进程且不继承重定向的
    # stdout（时间线收不到 DOCMIND_EVENT）。窗口/编辑器运行改用同目录真引擎 GUI 可执行文件。
    if selected == 'godot' and isinstance(executable, str) and executable.lower().endswith('_console.exe'):
        gui_exe = executable[:-len('_console.exe')] + '.exe'
        if os.path.isfile(gui_exe):
            executable = gui_exe
    if selected == 'unity': args=[executable, '-projectPath', root_abs]
    elif selected == 'unreal': args=[executable, os.path.join(root_abs, scene)] if scene else [executable, root_abs]
    else: args=[executable, '--path', root_abs]
    if scene and selected == 'godot': args += ['--editor']
    # 引擎是长生命周期 GPU 占用方：启动前先拿**软租约**（exclusive=False）——
    # 引擎需要多开（每个项目一个引擎），硬租约会让第二个引擎被"GPU 正忙"挡住。
    # 软租约仍登记（供 gpu_status 展示、进程死亡回收），但不参与互斥；ttl=0 不限期，
    # 由 engine_stop / 进程死亡看门狗 force_release 释放。Popen 前把租约卡号注入
    # CUDA_VISIBLE_DEVICES——这是物理设备隔离的接线点。
    lease_owner = 'engine:' + root_abs
    lease = _gpu.acquire_lease(lease_owner, timeout=2, purpose=selected, ttl=0, exclusive=False)
    if not lease.get("ok"):
        return {'ok': False, 'error': 'GPU 资源正忙，无法启动引擎。', 'reason': lease.get('reason')}
    child_env = os.environ.copy()
    child_env.update(_gpu_process_environment(lease.get("gpu")))
    try:
        log_path = _project_state.path(root_abs, "engine.log", legacy=".docmind_engine.log")
        log = open(log_path, "a", encoding="utf-8")
        p = subprocess.Popen(args, cwd=root_abs, stdout=log, stderr=subprocess.STDOUT, text=True, env=child_env)
        _ENGINE_PROCS[root_abs] = p
        _ENGINE_SNAPSHOT[root_abs] = _engine_snapshot(root_abs)
        _ENGINE_LAUNCH[root_abs] = {'executable': executable, 'scene': scene or '',
                                    'embed': bool(embed), 'host_hwnd': int(host_hwnd or 0),
                                    'rect': dict(rect) if isinstance(rect, dict) else None,
                                    'fill': bool(fill), 'engine': selected}
        _gpu.register_process(p.pid, lease_owner, lease.get('gpu'), selected)
        _ENGINE_LOGS[root_abs] = log
        result = {"ok": True, "running": True, "pid": p.pid, "gpu": lease.get("gpu")}
        # 软租约下显存门槛只出警告、不阻断（单卡让用户自行判断）；透传给前端展示。
        if lease.get("warning"):
            result["notice"] = lease["warning"]
            result["gpu_warning"] = lease["warning"]
        if embed and host_hwnd:
            # 引擎建窗口是异步的：轮询直到找到窗口并嵌入成功，或超时。
            # rect 给了就嵌到前端口算的"引擎视窗"；都不给也不许铺满——退化为安全有界框
            #（fill=True 才显式铺满）。引擎仍会正常启动，只是嵌入方式不同。
            last = ''
            for _ in range(30):
                time.sleep(0.2)
                er = engine_embed(root_abs, host_hwnd, rect=rect, fill=fill)
                if er.get('ok'):
                    result['embedded'] = True
                    result['hwnd'] = er.get('hwnd')
                    result['embed'] = {k: er.get(k) for k in
                                       ('width', 'height', 'offset_y', 'dpi', 'host_dpi', 'title')}
                    break
                last = er.get('error', '')
            else:
                result['embedded'] = False
                result['embed_error'] = last or '等待引擎窗口超时。'
        elif embed:
            result['embedded'] = False
            result['embed_error'] = '没有桌面宿主窗口（请用桌面端启动工作台）。'
        return result
    except FileNotFoundError:
        _gpu.release(lease_owner)
        return {"ok": False, "error": f"找不到 {selected} 可执行文件。请安装引擎，或在 .docmind_engine.json 中配置 executable 的绝对路径。下载地址：{next((x['download'] for x in ENGINE_CATALOG if x['id']==selected), '')}"}
    except Exception as e:
        _gpu.release(lease_owner)
        return {"ok": False, "error": f"无法启动 {selected}：{e}"}

def engine_stop(root):
    """停止引擎。**先解除嵌入再结束进程**——顺序反了会留下失效的子窗口。

    另外要杀掉整棵进程树：Godot 的 `*_console.exe` 是转发器，只 terminate 直接子进程
    会留下真正的 GUI 进程和它的窗口，也就是"孤儿窗口"。
    """
    root_abs = _root(root)
    p = _ENGINE_PROCS.get(root_abs)
    detached = engine_detach(root_abs)
    if not p or p.poll() is not None:
        if p: _gpu.unregister_process(p.pid, "exited")
        _ENGINE_PROCS.pop(root_abs, None)
        _ENGINE_LAUNCH.pop(root_abs, None)
        _ENGINE_SNAPSHOT.pop(root_abs, None)
        _gpu.release('engine:' + root_abs)
        return {"ok": True, "stopped": False, "detached": detached.get('was_embedded', False)}
    pid = p.pid
    killed = []
    try:
        from desktop_bridge import terminate_tree
        killed = terminate_tree(pid, timeout=5)
    except Exception:  # noqa: BLE001  桥接层不可用时退回 Popen 终止
        killed = []
    if not killed:
        p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
    log = _ENGINE_LOGS.pop(root_abs, None)
    if log:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass
    _ENGINE_PROCS.pop(root_abs, None)
    _ENGINE_LAUNCH.pop(root_abs, None)
    _ENGINE_SNAPSHOT.pop(root_abs, None)
    _gpu.unregister_process(pid, "stopped")
    _gpu.release('engine:' + root_abs)
    return {"ok": True, "stopped": True, "pid": pid,
            "detached": detached.get('was_embedded', False), "killed": killed}


def engine_reload(root):
    """快速重载当前 Godot 运行实例。

    原生 Godot 游戏没有一个跨版本、无需插件的进程内 reload RPC。这里采用可审计的
    快速重启：保存启动参数和嵌入矩形，停止旧进程，再用同一参数启动；项目文件、脚本
    和场景会重新导入，GPU 租约与窗口嵌入状态也随生命周期正确释放/重建。Web 试玩
    仍使用 docmind_bridge 的真正 ``reload_current_scene`` 路径。
    """
    root_abs = _root(root)
    selected = str(engine_config(root_abs).get('engine', 'godot')).lower()
    if selected != 'godot':
        return {'ok': False, 'error': '当前热重载仅支持 Godot。Unity/Unreal 请使用各自编辑器的脚本重载能力。'}
    st = engine_status(root_abs)
    if not st.get('running'):
        return {'ok': False, 'error': '引擎当前未运行，无法热重载。'}
    launch = dict(_ENGINE_LAUNCH.get(root_abs) or {})
    embed = dict(_EMBED_STATE.get(root_abs) or {})
    if not launch:
        # 兼容旧进程记录：至少按 Godot 项目重新启动，不伪造原嵌入状态。
        launch = {'executable': 'godot', 'scene': '', 'embed': False, 'host_hwnd': 0,
                  'rect': None, 'fill': False, 'engine': 'godot'}
    was_embedded = bool(st.get('embedded')) and bool(launch.get('host_hwnd') or embed.get('host_hwnd'))
    host = int(launch.get('host_hwnd') or embed.get('host_hwnd') or 0)
    rect = launch.get('rect') or embed.get('rect')
    fill = bool(launch.get('fill')) or (embed.get('mode') == 'fill')
    launch['embed'] = was_embedded
    launch['host_hwnd'] = host
    launch['rect'] = rect if isinstance(rect, dict) else None
    launch['fill'] = fill
    stopped = engine_stop(root_abs)
    if not stopped.get('ok'):
        return {'ok': False, 'error': '停止旧引擎失败，未执行热重载。', 'stop': stopped}
    result = engine_start(root_abs, launch.get('executable') or 'godot', launch.get('scene') or '',
                          host_hwnd=host or None, embed=was_embedded,
                          rect=launch.get('rect'), fill=fill)
    result['reloaded'] = bool(result.get('running'))
    result['reload_mode'] = 'process_restart'
    result['previous_pid'] = st.get('pid')
    if not result.get('running'):
        result.setdefault('error', '热重载启动新引擎失败。')
    return result

_ENGINE_INLINE_ERROR_RX = re.compile(
    r"(?P<path>(?:res://)?[A-Za-z0-9_./\\-]+\.(?:gd|tscn|cs|py))(?::(?P<line>\d+))?.{0,80}(?:error|Error|ERROR)")


def _engine_log_errors(lines):
    """解析引擎日志错误，返回 ``(去重后的错误, 原始错误行数)``。

    Godot 4.x 运行时把一条脚本错误拆成两行：

        SCRIPT ERROR: Invalid call. Nonexistent function 'take_damage' ...
           at: player.take_damage (res://behaviors/enemy.gd:44)

    旧实现只对**单行**跑正则，于是这类错误全部漏掉，``errors=[]`` 让
    runtime_evidence 把明显报错的运行判成通过（假绿）。这里优先复用
    ``parse_godot_diagnostics``（它本来就处理两行式），再保留旧的单行匹配
    作为兼容兜底。

    去重按 ``(path, line, message)``：同一个错误每个物理帧刷几千行时，
    展示列表只留一条，但 ``error_count`` 仍保留原始行数，供运行时证据写出
    「多少条错误行」而不是被去重后的 1 条掩盖。
    """
    text = "\n".join(lines)
    try:
        structured = parse_godot_diagnostics(text)
    except Exception:  # noqa: BLE001 - 诊断解析失败不能反过来拖垮只读日志
        structured = []
    errors, seen = [], set()
    for item in structured:
        if not isinstance(item, dict) or item.get("severity") != "error":
            continue
        path = str(item.get("path") or "").replace("\\", "/").removeprefix("res://")
        line_no = int(item.get("line") or 0)
        message = " ".join(str(item.get("message") or "").split())
        key = (path, line_no, message)
        if not message or key in seen:
            continue
        seen.add(key)
        errors.append({"path": path, "line": line_no, "message": message})
    # 兼容旧格式/其它引擎：res://x.gd:12 - Parse Error: ... 这类同一行的引用。
    # 已经由结构化解析命中的 path+line 不再重复记账。
    structured_spots = {(item["path"], item["line"]) for item in errors}
    raw_error_lines = set()
    for index, raw in enumerate(lines):
        if _GODOT_SCRIPT_ERR_RX.match(raw):
            raw_error_lines.add(index)
        m = _ENGINE_INLINE_ERROR_RX.search(raw)
        if not m:
            continue
        raw_error_lines.add(index)
        path = m.group("path").replace("\\", "/").removeprefix("res://")
        line_no = int(m.group("line") or 0)
        if (path, line_no) in structured_spots:
            continue
        message = raw.strip()
        key = (path, line_no, message)
        if key in seen:
            continue
        seen.add(key)
        structured_spots.add((path, line_no))
        errors.append({"path": path, "line": line_no, "message": message})
    return errors, len(raw_error_lines)


def _engine_log_matches(path, needle, *, keep=50, max_bytes=16 * 1024 * 1024):
    """在整份日志里找包含 ``needle`` 的行（大小写不敏感）。

    日志被重复错误刷屏时，固定回看 tail 200 行可能完全看不到用户真正要
    找的标记（实测 CLICK_PROBE 就这样被 14k 条 SCRIPT ERROR 淹没）。
    这里按字节回看最多 ``max_bytes``，用 deque 只保留最近 ``keep`` 条命中。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0, [], False
    offset = max(0, size - max(1, int(max_bytes)))
    needle_lower = str(needle or "").lower()
    count, recent = 0, deque(maxlen=max(1, int(keep)))
    try:
        with open(path, "rb") as handle:
            if offset:
                handle.seek(offset)
                handle.readline()  # 丢掉从中间截断的半行
            for raw_bytes in handle:
                raw = raw_bytes.decode("utf-8", errors="replace")
                if needle_lower in raw.lower():
                    count += 1
                    recent.append(raw.rstrip("\n"))
    except OSError:
        return 0, [], bool(offset)
    return count, list(recent), bool(offset)


#: 日志里的「大写标记」：CLICK_PROBE / CLICKED / HP_CHANGED 这类探针输出。
#: 只用于在 tail 被报错刷屏时给模型一点「日志别处还有东西」的线索，
#: 不是错误判据（错误仍由 _engine_log_errors 负责）。
_ENGINE_MARKER_RX = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")


def _engine_diag_line(raw):
    """这一行是不是诊断/堆栈的一部分（标记采样要把它们排除掉）。"""
    return bool(_GODOT_SCRIPT_ERR_RX.match(raw)
                or _ENGINE_INLINE_ERROR_RX.search(raw)
                or re.search(r"(?i)(?:GDScript backtrace|\bat:\s+\S|^\s*\[[0-9]+\]\s)", raw))


def _engine_log_markers(path, *, keep=12, max_bytes=16 * 1024 * 1024, text_limit=300):
    """统计日志总行数，并采回少量「非诊断的大写标记行」。

    为什么要有这个：实测日志被同一条 SCRIPT ERROR 刷了 1.4 万行，
    ``engine_logs`` 默认只看 tail 200，模型于是断言「日志里没有 CLICK_PROBE」，
    而探针行其实就在更早的位置。**tail 里没有 != 全日志没有**，这里主动把
    别处的标记行采回来，让模型自己看见它出现过。

    返回 ``(total_lines, marker_samples, scan_truncated, marker_count)``：
    ``marker_samples`` 是 ``[{"line": N, "text": "..."}]``（最近的若干条，
    按行内标记词集合去重，避免唯一心跳行把一次性标记挤掉）；
    ``scan_truncated`` 表示日志超过 ``max_bytes``、只扫了尾段。
    只读且不抛异常——日志扫描失败不能反过来拖垮只读诊断。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0, [], False, 0
    max_bytes = max(1, int(max_bytes))
    offset = max(0, size - max_bytes)

    # 第一遍只数行（二进制分块，不解码），顺便记住 offset 之前有多少个换行。
    newlines_before = 0
    total_newlines = 0
    last_byte = b""
    try:
        with open(path, "rb") as handle:
            read = 0
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                if offset and read < offset < read + len(chunk):
                    newlines_before = total_newlines + chunk[:offset - read].count(b"\n")
                total_newlines += chunk.count(b"\n")
                read += len(chunk)
                last_byte = chunk[-1:]
    except OSError:
        return 0, [], False, 0
    total_lines = total_newlines + (1 if size and last_byte != b"\n" else 0)

    # 第二遍只扫尾段（最多 max_bytes），给标记行配上**真实行号**。
    line_no = newlines_before
    if offset:
        try:
            with open(path, "rb") as handle:
                handle.seek(offset - 1)
                if handle.read(1) != b"\n":
                    line_no = newlines_before + 1  # 被丢掉的半行
        except OSError:
            pass
    count = 0
    samples = deque()
    seen = set()
    try:
        with open(path, "rb") as handle:
            if offset:
                handle.seek(offset)
                handle.readline()  # 丢掉从中间截断的半行
            for raw_bytes in handle:
                line_no += 1
                raw = raw_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                if not _ENGINE_MARKER_RX.search(raw) or _engine_diag_line(raw):
                    continue
                count += 1
                text = raw.strip()[:text_limit]
                # 按**标记词集合**去重，而不是整行文本：心跳行可能每行数字都不同，
                # 按整行去重会把 1500 条心跳全留下、把早先的 CLICK_PROBE 挤掉。
                key = tuple(sorted(set(_ENGINE_MARKER_RX.findall(raw))))
                if not key or key in seen:
                    continue
                seen.add(key)
                samples.append((key, {"line": line_no, "text": text}))
                while len(samples) > max(1, int(keep)):
                    old_key, _old = samples.popleft()
                    seen.discard(old_key)
    except OSError:
        return total_lines, [], bool(offset), count
    return total_lines, [item for _key, item in samples], bool(offset), count


def engine_logs(root, limit=200, contains=""):
    path = _project_state.path(root, "engine.log", legacy=".docmind_engine.log")
    try:
        total_lines, markers, scan_truncated, marker_count = _engine_log_markers(path)
    except Exception:  # noqa: BLE001 - 附加线索失败不能拖垮基础日志读取
        total_lines, markers, scan_truncated, marker_count = 0, [], False, 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = list(deque(f, maxlen=max(1, min(int(limit), 2000))))
    except OSError:
        lines = []
    errors, error_count = _engine_log_errors(lines)
    # 只有「日志比 tail 长」才需要提醒。此时模型看到的 lines 不是全貌，
    # 「没看到 X」不能推出「X 没出现」——这正是上一轮幻觉的起点。
    tail_truncated = total_lines > len(lines)
    # 顺序即协议：**摘要字段必须排在 `lines` 前面**。
    # 反思/观察截断只保留工具结果的前 1600 字；`lines` 若排在最前，一次
    # 300 行的心跳洪泛就会把 total_lines / marker_samples / matches 全挤出窗口，
    # 判据于是把「材料被切掉」误判成「结论无证据」。实测 `wf-4dd2abca90c9` 的
    # verify 首轮：结论里的 CLICK_PROBE / match_count 全对，却被反思否掉，
    # 只因观察里只剩 HEARTBEAT_FRAME。字段值与行为不变，只改排列。
    data = {"ok": True,
            "total_lines": total_lines,
            "tail_start_line": max(1, total_lines - len(lines) + 1) if lines else 0,
            "tail_truncated": tail_truncated,
            "marker_samples": markers,
            "marker_count": marker_count,
            "marker_scan_truncated": scan_truncated,
            "errors": errors[:50], "error_count": error_count,
            "running": engine_status(root)["running"]}
    if tail_truncated:
        data["note"] = (
            "lines 只是日志尾部 %d 行（全文共 %d 行），不能据 tail 里没有某个标记"
            "就断言它没出现。marker_samples 是从全日志采回的大写标记行（含行号）；"
            "要找特定文本请用 contains 参数搜索整份日志。" % (len(lines), total_lines)
        )
    needle = str(contains or "").strip()[:200]
    if needle:
        match_count, matches, truncated = _engine_log_matches(path, needle)
        data.update({"contains": needle, "matches": matches,
                     "match_count": match_count, "search_truncated": truncated})
    data["lines"] = [x.rstrip("\n") for x in lines]
    return data


def runtime_evidence(root, limit=300):
    """Review 阶段可机器判定的"真跑过"证据（只读，不启停引擎）。

    三条同时满足才算 ok：
      1. engine.log 里确实有运行输出（不是空文件）；
      2. 解析出的 error 行为 0（有错误行就不算通过）；
      3. 日志时间 **不早于** 最后一次源码/配置改动（否则上一轮的干净日志
         会把这一轮的新 bug 洗白 —— 那正是"假绿"）。

    失败时给出中文 reason，供 review 的 unmet 明细直接展示；不抛异常。
    """
    root_abs = _root(root)
    log_path = _project_state.path(root_abs, "engine.log", legacy=".docmind_engine.log")
    try:
        log_mtime_ns = os.stat(log_path).st_mtime_ns
    except OSError:
        log_mtime_ns = 0
    latest_source_ns = max(
        (int((meta or {}).get("mtime_ns") or 0)
         for meta in _engine_snapshot(root_abs).values()),
        default=0,
    )
    data = engine_logs(root_abs, limit=limit)
    lines = data.get("lines") or []
    errors = data.get("errors") or []
    try:
        error_count = int(data.get("error_count") or 0)
    except (TypeError, ValueError):
        error_count = 0
    error_count = max(error_count, len(errors))
    fresh = bool(log_mtime_ns) and log_mtime_ns >= latest_source_ns
    reason = ""
    if not lines:
        reason = "引擎日志为空：本轮没有真实运行记录。"
    elif error_count:
        reason = ("引擎日志有 %d 条错误行（%d 类）。" % (error_count, len(errors))
                  if len(errors) < error_count else
                  "引擎日志有 %d 条错误行。" % error_count)
    elif not fresh:
        reason = "引擎日志早于最后一次代码/配置改动：改动后没有重新运行。"
    return {"ok": bool(lines) and not errors and fresh,
            "fresh": fresh,
            "running": bool(data.get("running")),
            "log_lines": len(lines),
            "log_mtime_ns": log_mtime_ns,
            "latest_source_mtime_ns": latest_source_ns,
            "error_count": error_count,
            "error_classes": len(errors),
            "errors": errors[:20],
            "reason": reason}

def engine_verify(root, executable="godot", timeout=30):
    """Run a bounded headless editor import/parse check when the executable is available."""
    root_abs = _root(root)
    cfg=engine_config(root); selected=cfg.get('engine','godot'); executable=cfg.get('executable','godot') if not executable or (executable == 'godot' and selected != 'godot') else executable
    executable = _resolve_engine_executable(selected, executable)
    if selected == 'unity': cmd=[executable,'-batchmode','-nographics','-quit','-projectPath',root_abs]
    elif selected == 'unreal':
        projects=[x['path'] for x in engine_scan(root_abs).get('projects',[]) if x.get('engine')=='unreal']
        project_path=os.path.join(root_abs, projects[0]) if projects else root_abs
        cmd=[executable, project_path, '-Unattended','-NullRHI','-ProjectOnly']
    else: cmd=[executable,'--headless','--path',root_abs,'--editor','--quit']
    # 校验是短时 GPU 占用：拿租约（失败直接报错，不排队 30 秒）+ 注入卡号环境
    lease_owner = 'verify:' + root_abs
    lease = _gpu.acquire_lease(lease_owner, timeout=2, purpose='engine-verify', ttl=max(30, int(timeout) + 30))
    if not lease.get("ok"):
        return {'ok': False, 'error': 'GPU 资源正忙，无法执行引擎校验。', 'reason': lease.get('reason')}
    verify_env = os.environ.copy()
    verify_env.update(_gpu_process_environment(lease.get("gpu")))
    try:
        p = subprocess.run(cmd, cwd=root_abs, capture_output=True, text=True, timeout=max(3, min(int(timeout), 180)),
                           encoding='utf-8', errors='replace', env=verify_env)
        out = ((p.stdout or '') + '\n' + (p.stderr or ''))[-6000:]
        diagnostics = parse_godot_diagnostics(out) if selected == 'godot' else (parse_unreal_diagnostics(out) if selected == 'unreal' else [])
        # 子进程直接捕获的诊断优先；日志文件解析作为历史兜底
        log_errors = engine_logs(root_abs).get("errors", [])
        return {"ok": p.returncode == 0 and not any(d['severity'] == 'error' for d in diagnostics),
                "returncode": p.returncode, "output": out, "gpu": lease.get("gpu"),
                "diagnostics": diagnostics, "errors": diagnostics or log_errors}
    except FileNotFoundError:
        return {"ok": False, "error": f"未找到 {selected} 可执行文件，请配置路径或将其加入 PATH。"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{selected} headless 校验超时。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"引擎校验失败：{e}"}
    finally:
        _gpu.release(lease_owner)

def install_runtime_probe(root, dest='addons/docmind_runtime/probe.gd'):
    path = _file(root, dest)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = 'extends Node\nclass_name DocMindRuntimeProbe\n\nfunc emit_event(event_type: String, name: String, data: Dictionary = {}) -> void:\n\tvar event = {"type": event_type, "name": name, "data": data}\n\tprint("DOCMIND_EVENT " + JSON.stringify(event))\n'
    with open(path, 'w', encoding='utf-8', newline='\n') as f: f.write(source)
    return {'ok': True, 'path': dest.replace('\\','/'), 'created': True}

def comfy_status(url="http://127.0.0.1:8188"):
    try: url = _safe_comfy_url(url)
    except ValueError as e: return {"ok": False, "available": False, "error": str(e)}
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/system_stats', timeout=3) as r: data = json.loads(r.read().decode())
        pid = None
        try:
            port = urllib.parse.urlparse(url).port or 8188
            out = subprocess.check_output(['netstat','-ano'], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if f':{port} ' in line and 'LISTENING' in line:
                    pid = int(line.split()[-1]); break
        except Exception: pass
        if pid:
            _gpu.register_process(pid, 'comfyui:service', None, 'comfyui')
        return {"ok": True, "available": True, "url": url, "system": data, "pid": pid}
    except Exception as e:
        return {"ok": True, "available": False, "url": url, "error": str(e)}

def comfy_start(root=None, port=8188):
    """Start a local portable ComfyUI instance and register its real PID."""
    global _COMFY_SERVICE
    if _COMFY_SERVICE is not None and _COMFY_SERVICE.poll() is None:
        return {'ok': True, 'running': True, 'pid': _COMFY_SERVICE.pid, 'reused': True}
    root = root or os.getenv('DOCMIND_COMFY_ROOT', r'D:\ComfyUI')
    root = os.path.abspath(root)
    py = os.path.join(root, 'python_embeded', 'python.exe')
    main = os.path.join(root, 'ComfyUI', 'main.py')
    if not os.path.isfile(py): return {'ok': False, 'error': f'未找到 ComfyUI Python：{py}'}
    if not os.path.isfile(main): return {'ok': False, 'error': f'未找到 ComfyUI main.py：{main}'}
    env = os.environ.copy(); env.update(_gpu_process_environment(None))
    try:
        _COMFY_SERVICE = subprocess.Popen([py, main, '--listen', '127.0.0.1', '--port', str(int(port))], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _gpu.register_process(_COMFY_SERVICE.pid, 'comfyui:service', None, 'comfyui')
        return {'ok': True, 'running': True, 'pid': _COMFY_SERVICE.pid, 'root': root, 'port': int(port)}
    except Exception as e: return {'ok': False, 'error': f'启动 ComfyUI 失败：{e}'}

def comfy_stop():
    global _COMFY_SERVICE
    p = _COMFY_SERVICE
    if p is None or p.poll() is not None: return {'ok': True, 'running': False, 'stopped': False}
    pid = p.pid
    try: p.terminate(); p.wait(timeout=8)
    except Exception:
        try: p.kill()
        except Exception: pass
    _gpu.unregister_process(pid, 'stopped'); _COMFY_SERVICE = None
    return {'ok': True, 'running': False, 'stopped': True, 'pid': pid}

def _comfy_job_owner(prompt_id, root=None):
    if root:
        return f"comfyui:{_project_state.projects.project_id(root)}:{prompt_id}"
    return f"comfyui:{prompt_id}"


def gpu_free_mb():
    """当前 GPU 空闲显存（MB）；探测不到返回 None。单卡/聚合取总和。"""
    mem = _gpu.memory_info()
    return None if not mem else int(mem.get('free_mb') or 0)


def comfy_queue_running(url="http://127.0.0.1:8188"):
    """ComfyUI 当前是否有正在执行/排队的提示（腾显存前必须确认没有在跑的任务）。"""
    try:
        url = _safe_comfy_url(url)
        with urllib.request.urlopen(url.rstrip('/') + '/queue', timeout=5) as r:
            q = json.loads(r.read().decode())
    except Exception:
        return False
    running = q.get('queue_running') or []
    pending = q.get('queue_pending') or []
    return bool(running or pending)


def comfy_free_models(url="http://127.0.0.1:8188", *, wait=True, timeout=20):
    """让 ComfyUI 卸载已加载模型并释放缓存显存（POST /free）。

    用于跨链路切换：生图前卸掉视频大模型、生视频前卸掉生图模型。
    有任务在跑时不执行（卸载会打断执行）；5xx 时重试一次（实测 ComfyUI
    在异常状态后第一次 /free 偶发 500）。返回 {'ok', 'freed_mb', 'error'}。
    """
    try:
        url = _safe_comfy_url(url)
    except ValueError as e:
        return {'ok': False, 'freed_mb': 0, 'error': str(e)}
    if comfy_queue_running(url):
        return {'ok': False, 'freed_mb': 0, 'error': 'ComfyUI 有任务执行中，跳过显存回收'}
    before = gpu_free_mb() or 0
    payload = json.dumps({'unload_models': True, 'free_memory': True}).encode()
    last_err = ''
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                url.rstrip('/') + '/free', data=payload,
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            last_err = ''
            break
        except Exception as e:  # noqa: BLE001 - 卸载失败要返回可展示原因而非抛出
            last_err = str(e)
            time.sleep(2)
    if last_err:
        return {'ok': False, 'freed_mb': 0, 'error': f'ComfyUI 卸载模型失败：{last_err}'}
    freed = 0
    if wait:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            now = gpu_free_mb()
            if now is None:
                break
            freed = max(freed, now - before)
            if now - before >= 256:
                break
    return {'ok': True, 'freed_mb': freed, 'error': ''}


def comfy_release_job(prompt_id, root=None):
    """作业终态后释放 GPU 租约（asset_gen 自轮询路径不走 comfy_history，
    需要显式调用，否则租约要等 600s TTL 才回收，挡住下一次生成）。"""
    pid = str(prompt_id or '').strip()
    if not pid:
        return False
    if root is None:
        with _COMFY_JOBS_LOCK:
            for candidate, jobs in _COMFY_PROJECT_JOBS.items():
                if pid in jobs:
                    root = candidate
                    break
    return _gpu.force_release(_comfy_job_owner(pid, root)) is not None


def _gpu_busy_error(res):
    if res.get("reason") == "insufficient_memory":
        msg = "显存不足，低于 DOCMIND_GPU_MIN_FREE_MB 门槛，已拒绝（未排队）。"
    else:
        msg = "GPU 正忙：其他任务占用中，请稍后重试或在 GPU 面板取消排队。"
    if res.get("evicted"):
        msg += "（已尝试卸载 Ollama 驻留模型后重试）"
    return msg

# ---------------------------------------------------------------- ComfyUI 模板
def comfy_templates(root=None):
    # Resolve the portable installation root without baking a developer
    # machine into the runtime.  Explicit project/env configuration wins;
    # common Windows locations remain a backwards-compatible fallback.
    return {'ok': True, 'templates': [
        {'id':'z-image-turbo','name':'Z-Image Turbo 图片','model':'z_image_turbo-Q8_0.gguf','models':['z_image_turbo-Q8_0.gguf','Qwen3-4B-Q8_0.gguf','ae.safetensors'],'kind':'image','author':'Tongyi-MAI','source_url':'https://github.com/Tongyi-MAI/Z-Image','license':'Apache-2.0','schema':{'prompt':'string','negative_prompt':'string','width':'integer','height':'integer','steps':'integer','seed':'integer','filename_prefix':'string'}},
        {'id':'minimax-h3-i2v','name':'MiniMax H3 参考图视频','model':'minimax_h3_fl2va_pruned_int8_convrot.safetensors','kind':'video','author':'MiniMax','source_url':'https://github.com/MiniMax-AI','license':'check-model-card','workflow':_comfy_workflow_path(root),'schema':{'prompt':'string','width':'integer','height':'integer','frames':'integer','steps':'integer','seed':'integer','filename_prefix':'string'}},
        {'id':'flux1-dev-fp8','name':'FLUX.1-dev 文生图（fp8）','model':'flux1-dev-fp8.safetensors','kind':'image','models':['flux1-dev-fp8.safetensors','t5xxl_fp8_e4m3fn.safetensors','clip_l.safetensors','ae.safetensors'],'pending':True},
        {'id':'flux1-dev-gguf-q4','name':'FLUX.1-dev 文生图（GGUF Q4 低显存）','model':'flux1-dev-Q4_K_S.gguf','kind':'image','models':['flux1-dev-Q4_K_S.gguf','t5-v1_1-xxl-encoder-Q4_K_S.gguf','clip_l.safetensors','ae.safetensors'],'pending':True},
        {'id':'flux1-canny','name':'FLUX Canny 线稿控图','model':'flux1-canny-dev.safetensors','kind':'image','models':['flux1-dev-fp8.safetensors','flux1-canny-dev.safetensors','t5xxl_fp8_e4m3fn.safetensors','clip_l.safetensors','ae.safetensors'],'pending':True},
        {'id':'flux1-depth','name':'FLUX Depth 深度控图','model':'flux1-depth-dev.safetensors','kind':'image','models':['flux1-dev-fp8.safetensors','flux1-depth-dev.safetensors','t5xxl_fp8_e4m3fn.safetensors','clip_l.safetensors','ae.safetensors'],'pending':True},
        {'id':'uso-subject','name':'USO 主体一致（角色立绘）','model':'uso-subject.safetensors','kind':'image','models':['flux1-dev-fp8.safetensors','uso-subject.safetensors','t5xxl_fp8_e4m3fn.safetensors','clip_l.safetensors','ae.safetensors'],'pending':True},
        {'id':'uso-style','name':'USO 风格一致（同款画风）','model':'uso-style.safetensors','kind':'image','models':['flux1-dev-fp8.safetensors','uso-style.safetensors','t5xxl_fp8_e4m3fn.safetensors','clip_l.safetensors','ae.safetensors'],'pending':True}
    ]}

def _comfy_workflow_path(root=None):
    project = _root(root) if root else os.getenv('DOCMIND_PROJECT_ROOT', os.getcwd())
    configured = ''
    try:
        with open(os.path.join(project, '.docmind_comfy.json'), encoding='utf-8') as f:
            configured = str((json.load(f) or {}).get('h3_workflow') or '')
        if configured and not os.path.isabs(configured): configured = os.path.join(project, configured)
    except Exception: pass
    roots = [str(os.getenv('DOCMIND_COMFY_ROOT','')).strip(), r'D:\ComfyUI', r'C:\ComfyUI']
    candidates = [configured, os.getenv('DOCMIND_COMFY_WORKFLOW_H3','')]
    candidates.extend(os.path.join(root, 'ComfyUI', 'user', 'default', 'workflows', 'minimax_h3_t2v.json')
                      for root in roots if root)
    for p in candidates:
        if p and os.path.isfile(p): return p
    return next((p for p in candidates if p), '')

def comfy_template_workflow(template_id, root=None):
    meta = next((t for t in comfy_templates(root).get('templates', []) if t.get('id') == template_id), None)
    if meta and meta.get('pending'):
        return {'ok': False, 'error': '该模板待接入（需先安装依赖模型/自定义节点）', 'pending': True}
    # H3 路径由 _comfy_workflow_path 按项目配置、环境变量和常见目录解析。
    if template_id == 'minimax-h3-i2v':
        path = _comfy_workflow_path(root)
        try:
            with open(path, encoding='utf-8') as f: return {'ok': True, 'id': template_id, 'workflow': json.load(f), 'format': 'ui'}
        except Exception as e: return {'ok': False, 'error': f'无法读取 H3 workflow：{e}'}
    if template_id == 'z-image-turbo':
        return {'ok': True, 'id': template_id, 'format': 'api', 'workflow': {
            '1': {'class_type':'UnetLoaderGGUF','inputs':{'unet_name':'z_image_turbo-Q8_0.gguf'}},
            '2': {'class_type':'CLIPLoaderGGUF','inputs':{'clip_name':'Qwen3-4B-Q8_0.gguf','type':'lumina2'}},
            '3': {'class_type':'TextEncodeZImageOmni','inputs':{'clip':['2',0],'prompt':'a cinematic game character concept','auto_resize_images':True}},
            '4': {'class_type':'TextEncodeZImageOmni','inputs':{'clip':['2',0],'prompt':'blurry, low quality','auto_resize_images':True}},
            '5': {'class_type':'EmptyLatentImage','inputs':{'width':512,'height':512,'batch_size':1}},
            '6': {'class_type':'KSampler','inputs':{'model':['1',0],'seed':42,'steps':8,'cfg':1.0,'sampler_name':'euler','scheduler':'simple','positive':['3',0],'negative':['4',0],'latent_image':['5',0],'denoise':1.0}},
            '7': {'class_type':'VAELoader','inputs':{'vae_name':'ae.safetensors'}}, '8': {'class_type':'VAEDecode','inputs':{'samples':['6',0],'vae':['7',0]}}, '9': {'class_type':'SaveImage','inputs':{'images':['8',0],'filename_prefix':'docmind_zimage'}}}}
    return {'ok': False, 'error': '未知模板。'}

# 只读的 ComfyUI 模型依赖检查。保留在工作台服务层，API 仅做代理，避免把
# 文件系统扫描逻辑重新塞回巨型入口；模板声明的 models 列表支持待接入模板。
_COMFY_MODEL_SUBDIRS = ('unet', 'checkpoints', 'clip', 'vae', 'controlnet',
                        'diffusion_models', 'text_encoders', 'clip_vision',
                        'loras', 'style_models')

def _comfy_root(root=None):
    project = _root(root) if root else os.getenv('DOCMIND_PROJECT_ROOT', os.getcwd())
    try:
        with open(os.path.join(project, '.docmind_comfy.json'), encoding='utf-8') as f:
            configured = str((json.load(f) or {}).get('comfy_root') or '').strip()
        if configured:
            return configured if os.path.isabs(configured) else os.path.join(project, configured)
    except Exception:
        pass
    env_root = str(os.getenv('DOCMIND_COMFY_ROOT', '')).strip()
    if env_root:
        return env_root
    return next((p for p in (r'D:\ComfyUI', r'C:\ComfyUI') if os.path.isdir(p)), '')

def comfy_model_check(root=None):
    comfy_root = _comfy_root(root)
    if not comfy_root:
        return {'ok': False, 'error': '未找到 ComfyUI 安装目录，请设置 DOCMIND_COMFY_ROOT 或配置 comfy_root。'}
    if not os.path.isdir(comfy_root):
        return {'ok': False, 'error': f'ComfyUI 根目录不存在：{comfy_root}'}
    models_dir = os.path.join(comfy_root, 'models')
    if not os.path.isdir(models_dir):
        return {'ok': False, 'error': f'ComfyUI 根目录下没有 models 目录：{models_dir}'}
    found = {}
    for sub in _COMFY_MODEL_SUBDIRS:
        sub_dir = os.path.join(models_dir, sub)
        if not os.path.isdir(sub_dir):
            continue
        for name in os.listdir(sub_dir):
            if os.path.isfile(os.path.join(sub_dir, name)):
                found.setdefault(name.lower(), name)
    rows = []
    for template in comfy_templates(root).get('templates', []):
        declared = [str(x) for x in (template.get('models') or ([template.get('model')] if template.get('model') else []))]
        missing = [x for x in declared if x.lower() not in found]
        rows.append({'id': template.get('id', ''), 'name': template.get('name', ''),
                     'present': not missing, 'missing': missing,
                     'pending': bool(template.get('pending'))})
    return {'ok': True, 'root': os.path.abspath(comfy_root),
            'found': sorted(found.values()), 'templates': rows}

# --------------------------------------------------------------------- ComfyUI UI→API
# H3（及多数官方）工作流是 **子图（subgraph）UI 格式**：真正的生成链藏在
# definitions.subgraphs[].nodes 里，顶层 node 只是子图实例（type == 子图 id）。
# /prompt 既不吃原始 UI 格式（500），也不认把子图实例当普通节点提交（400），
# 因此这里先把子图 **拍平** 成普通节点，再用 ComfyUI 的 /object_info 按
# INPUT_TYPES 顺序把 widgets_values 正确映射到 widget 输入（连接型输入不占位）。
_COMFY_OBJINFO = {}
_COMFY_WIDGET_TYPES = {'STRING', 'INT', 'FLOAT', 'COMBO', 'BOOLEAN', 'COMFY_DYNAMICCOMBO_V3'}
_COMFY_CONN_TYPES = {'IMAGE', 'MODEL', 'CLIP', 'VAE', 'LATENT', 'CONDITIONING', 'MASK',
                     'AUDIO', 'VIDEO', 'SIGMAS', 'SAMPLER', 'GUIDER', 'NOISE', 'CONTROL_NET',
                     'STYLE_MODEL', 'CLIP_VISION', 'UPLOAD_IMAGE', 'COMFY_AUTOGROW_V3'}
# 早期 H3 编辑器模板用这个 UUID 当 OSS 加速器（TE-Speed）的节点类型；已安装的
# 插件暴露稳定名 TESpeedMiniMaxH3。子图 id 同名时不走这里（走子图拍平）。
_COMFY_TYPE_ALIASES = {'4c314f31-ecda-4b08-ae98-faaba1bf613f': 'TESpeedMiniMaxH3'}

def _comfy_object_info(node_type, url):
    node_type = str(node_type)
    if node_type in _COMFY_OBJINFO:
        return _COMFY_OBJINFO[node_type]
    info = None
    for cand in (node_type, _COMFY_TYPE_ALIASES.get(node_type)):
        if not cand:
            continue
        try:
            u = _safe_comfy_url(url).rstrip('/') + '/object_info/' + cand
            req = urllib.request.Request(u, headers={'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode('utf-8'))
            info = (data.get(cand) if isinstance(data, dict) else None) or {}
            if info:
                break
        except Exception:
            info = None
    _COMFY_OBJINFO[node_type] = info
    return info

def _comfy_input_type(spec):
    if isinstance(spec, str):
        return spec
    if isinstance(spec, list) and spec:
        first = spec[0]
        if isinstance(first, str) and (first in _COMFY_CONN_TYPES or first in _COMFY_WIDGET_TYPES):
            return first
        return 'COMBO'  # 选项列表 -> 动态下拉，属 widget
    return None

def _comfy_is_widget(spec):
    t = _comfy_input_type(spec)
    return t in _COMFY_WIDGET_TYPES

def _comfy_norm_widget(key, value):
    if isinstance(value, str) and ('\\' in value or '/' in value) and key.endswith(('_name', '_name_1', 'clip_name', 'unet_name')):
        return value.replace('\\', '/').rsplit('/', 1)[-1]
    return value

def _comfy_links_by_name(node, links_by_id):
    out = {}
    for inp in node.get('inputs') or []:
        if isinstance(inp, dict) and inp.get('name') and inp.get('link') is not None:
            lid = str(inp['link'])
            if lid in links_by_id:
                v = links_by_id[lid]
                out[inp['name']] = (str(v[0]), int(v[1]))  # (origin_id, slot)
    return out

def _comfy_skip_node(node):
    typ = str(node.get('type') or '')
    if not typ or typ.lower().startswith(('note', 'markdownnote', 'label')) or typ.startswith('TE_image_pro_save_video'):
        return True
    if int(node.get('mode', 0) or 0) == 4:
        return True
    return False

def _comfy_build_inputs(node, links_by_id, url, resolve_origin, interface_by_slot=None):
    """Build one node's API `inputs`.

    resolve_origin(origin_id, slot) -> [node_id, slot] | None (connection target).
    interface_by_slot: optional {slot: ('link', origin, slot) | ('value', literal)}
    for links whose origin is the subgraph input interface (-10).
    Widget values are consumed positionally over widget-type INPUT_TYPES inputs;
    a linked widget input yields to the link, but its widget slot is still consumed.
    """
    widgets = list(node.get('widgets_values') or [])
    wi = 0
    info = _comfy_object_info(node.get('type'), url)
    node_links = _comfy_links_by_name(node, links_by_id)
    inputs = {}
    if info:
        req = (info.get('input', {}) or {}).get('required', {}) or {}
        opt = (info.get('input', {}) or {}).get('optional', {}) or {}
        ordered = list(req.items()) + list(opt.items())
    else:
        # Fallback (ComfyUI 不可达): 退化为旧的"按 inputs 顺序消费 widget"逻辑。
        # 有 link 的输入视为连接型（omit 或连接），无 link 的输入视为 widget，
        # 这样未知节点的 widget 值仍能正确映射（见 test_comfy_templates
        # test_ui_workflow_conversion_maps_links_and_widgets）。
        ordered = []
        for inp in (node.get('inputs') or []):
            if not isinstance(inp, dict) or not inp.get('name'):
                continue
            if inp.get('link') is not None:
                ordered.append((inp['name'], None))
            else:
                ordered.append((inp['name'], ['__WIDGET__']))
    for name, spec in ordered:
        if name is None:
            continue
        t = _comfy_input_type(spec)
        is_widget = t in _COMFY_WIDGET_TYPES
        if is_widget:
            wval = widgets[wi] if wi < len(widgets) else None
            wi += 1
            link = node_links.get(name)
            if link is not None:
                origin, slot = link
                if origin in ('-10', '-20'):
                    iv = (interface_by_slot or {}).get(int(slot))
                    if iv and iv[0] == 'link':
                        inputs[name] = [iv[1], int(iv[2])]
                    elif iv and iv[0] == 'value':
                        inputs[name] = _comfy_norm_widget(name, iv[1])
                    elif wval is not None:
                        inputs[name] = _comfy_norm_widget(name, wval)
                else:
                    res = resolve_origin(origin, slot)
                    if res is not None:
                        inputs[name] = [res[0], int(res[1])]
            elif wval is not None:
                inputs[name] = _comfy_norm_widget(name, wval)
        elif 'AUTOGROW' in (t or ''):
            # COMFY_AUTOGROW_V3 在 UI 里拆成 name.a / name.b 子输入；API 端也按
            # 子名提交（例如 ComfyMathExpression 的 values.a）。逐子输入处理，
            # 只提交有链接的（未连接的子输入视为可选）。
            for kn, kv in node_links.items():
                if not kn.startswith(name + '.'):
                    continue
                origin, slot = kv
                if origin in ('-10', '-20'):
                    iv = (interface_by_slot or {}).get(int(slot))
                    if iv and iv[0] == 'link':
                        inputs[kn] = [iv[1], int(iv[2])]
                else:
                    res = resolve_origin(origin, slot)
                    if res is not None:
                        inputs[kn] = [res[0], int(res[1])]
        else:
            link = node_links.get(name)
            if link is not None:
                origin, slot = link
                if origin in ('-10', '-20'):
                    iv = (interface_by_slot or {}).get(int(slot))
                    if iv and iv[0] == 'link':
                        inputs[name] = [iv[1], int(iv[2])]
                    # unlinked connection input fed by interface -> omit
                else:
                    res = resolve_origin(origin, slot)
                    if res is not None:
                        inputs[name] = [res[0], int(res[1])]
            # unlinked connection input -> omit (optional/default)
    return inputs

def comfy_ui_to_api_workflow(ui_workflow, url="http://127.0.0.1:8188"):
    """Convert ComfyUI editor JSON (nodes/links, possibly with subgraphs) to the
    /prompt API JSON. Subgraphs are flattened; widget values are mapped using each
    node's INPUT_TYPES so nodes that keep widgets only in `widgets_values` work."""
    if not isinstance(ui_workflow, dict) or not isinstance(ui_workflow.get('nodes'), list):
        return {'ok': True, 'workflow': ui_workflow, 'format': 'api'}
    nodes = {str(n.get('id')): n for n in ui_workflow['nodes'] if isinstance(n, dict) and n.get('id') is not None}
    # normalize links (top-level array form or subgraph dict form)
    links_by_id = {}
    for link in ui_workflow.get('links') or []:
        if isinstance(link, list) and len(link) >= 5:
            links_by_id[str(link[0])] = (str(link[1]), int(link[2]))
        elif isinstance(link, dict) and 'id' in link:
            links_by_id[str(link['id'])] = (str(link.get('origin_id')), int(link.get('origin_slot') or 0))
    subgraphs_by_id = {str(s.get('id')): s for s in (ui_workflow.get('definitions', {}).get('subgraphs') or [])
                       if s.get('id')}
    comfy_root = os.getenv('DOCMIND_COMFY_ROOT', r'D:\ComfyUI')
    invalid_image_nodes = set()
    for nid, node in nodes.items():
        if str(node.get('type')) == 'LoadImage':
            vals = node.get('widgets_values') or []
            image_name = str(vals[0]) if vals else ''
            if image_name and not os.path.isfile(os.path.join(comfy_root, 'ComfyUI', 'input', image_name)) \
                    and not os.path.isfile(os.path.join(comfy_root, 'input', image_name)):
                invalid_image_nodes.add(nid)
    max_id = max((int(k) for k in nodes), default=0)
    counter = {'n': max_id + 1}
    out = {}
    instance_ids = set()
    expanded_outputs = {}  # (instance_node_id, slot) -> (new_id, slot)

    def expand(instance_node, links_map):
        """Flatten one subgraph instance. Returns {slot_index: (new_id, slot)}."""
        sg_def = subgraphs_by_id.get(str(instance_node.get('type')))
        if sg_def is None:
            return {}
        inst_links_by_name = {}
        for inp in instance_node.get('inputs') or []:
            if isinstance(inp, dict) and inp.get('name') and inp.get('link') is not None:
                lid = str(inp['link'])
                if lid in links_map:
                    inst_links_by_name[inp['name']] = links_map[lid]
        wv = list(instance_node.get('widgets_values') or [])
        k = 0
        widget_by_name = {}
        for inp in sg_def.get('inputs') or []:
            t = str(inp.get('type') or '').upper()
            if t in _COMFY_WIDGET_TYPES:
                if k < len(wv):
                    widget_by_name[inp.get('name')] = wv[k]
                k += 1
        inner_links = {}
        for lk in sg_def.get('links') or []:
            if isinstance(lk, dict) and 'id' in lk:
                inner_links[str(lk['id'])] = (str(lk.get('origin_id')), int(lk.get('origin_slot') or 0),
                                              str(lk.get('target_id')), int(lk.get('target_slot') or 0))
        interface_by_slot = {}
        for i, inp in enumerate(sg_def.get('inputs') or []):
            name = (inp or {}).get('name')
            if name in inst_links_by_name:
                interface_by_slot[i] = ('link',) + inst_links_by_name[name]
            elif name in widget_by_name:
                interface_by_slot[i] = ('value', widget_by_name[name])
        inner_nodes = sg_def.get('nodes') or []
        remap = {}
        for nd in inner_nodes:
            remap[str(nd.get('id'))] = str(counter['n']); counter['n'] += 1
        nested_ids = set()
        def resolve_origin(o, s):
            if o in nested_ids:
                return expanded_outputs.get((o, int(s)))
            if o in remap:
                return [remap[o], int(s)]
            return [o, int(s)]  # 顶层真实节点（如 ResolutionSelector）保持原 id
        for nd in inner_nodes:
            ntyp = str(nd.get('type') or '')
            if ntyp in subgraphs_by_id:  # 嵌套子图
                nested_ids.add(str(nd.get('id')))
                sub = expand(nd, inner_links)
                for slot_idx, tgt in sub.items():
                    if isinstance(slot_idx, int):
                        expanded_outputs[(str(nd.get('id')), slot_idx)] = tgt
        for nd in inner_nodes:
            nid = str(nd.get('id'))
            if nid in nested_ids:
                continue
            new_id = remap[nid]
            eff_type = _COMFY_TYPE_ALIASES.get(str(nd.get('type'))) or str(nd.get('type'))
            inputs = _comfy_build_inputs(nd, inner_links, url, resolve_origin, interface_by_slot)
            out[new_id] = {'class_type': eff_type, 'inputs': inputs}
        out_slots = {}
        for oi, outp in enumerate(sg_def.get('outputs') or []):
            for lid in ((outp or {}).get('linkIds') or []):
                if str(lid) in inner_links:
                    o, os_, t, ts = inner_links[str(lid)]
                    if t in ('-20',):
                        out_slots[oi] = (remap.get(o, o), int(os_))
        return out_slots

    # 1) expand subgraph instances
    for nid, node in list(nodes.items()):
        typ = str(node.get('type') or '')
        if typ in subgraphs_by_id:
            if nid in invalid_image_nodes:
                continue
            instance_ids.add(nid)
            for slot_idx, tgt in expand(node, links_by_id).items():
                expanded_outputs[(nid, slot_idx)] = tgt
    # 2) convert top-level real nodes (those referenced by instances too)
    for nid, node in nodes.items():
        if nid in instance_ids or nid in invalid_image_nodes or _comfy_skip_node(node):
            continue
        typ = str(node.get('type') or '')
        eff_type = _COMFY_TYPE_ALIASES.get(typ) or typ
        inputs = _comfy_build_inputs(node, links_by_id, url,
                                     resolve_origin=lambda o, s: expanded_outputs.get((o, int(s)))
                                     if o in instance_ids else [o, int(s)])
        out[nid] = {'class_type': eff_type, 'inputs': inputs}
    # Remove dangling references and cascade
    changed = True
    while changed:
        changed = False
        for nid, item in list(out.items()):
            if any(isinstance(v, list) and v and str(v[0]) not in out for v in (item.get('inputs') or {}).values()):
                del out[nid]; changed = True; break
    if not out:
        return {'ok': False, 'error': 'UI workflow 没有可提交节点'}
    return {'ok': True, 'workflow': out, 'format': 'api'}

def comfy_apply_parameters(workflow, params):
    """按通用参数 schema 修改 API workflow，并严格校验目标节点字段。"""
    wf = json.loads(json.dumps(workflow or {})); p = params or {}
    targets = {'prompt': [('3','prompt')], 'negative_prompt': [('4','prompt')], 'width':[('5','width')], 'height':[('5','height')],
               'steps':[('6','steps')], 'seed':[('6','seed')], 'filename_prefix':[('9','filename_prefix')],
               'frames':[('5','frames'),('5','frame_count')]}
    for key, value in p.items():
        if key not in targets: continue
        applied=False
        for node, field in targets[key]:
            item=wf.get(str(node))
            if isinstance(item, dict) and isinstance(item.get('inputs'), dict) and field in item['inputs']:
                item['inputs'][field] = value; applied=True
        if not applied:
            # UI workflow 节点编号不稳定：仅在输入字段名称唯一且明确时回退匹配。
            matches=[]
            for item in wf.values() if isinstance(wf, dict) else []:
                if isinstance(item, dict) and isinstance(item.get('inputs'), dict) and key in item['inputs']:
                    matches.append(item['inputs'])
            if len(matches) == 1: matches[0][key] = value
            else: return {'ok':False,'error':f'模板缺少参数节点或字段：{key}'}
    return {'ok':True,'workflow':wf}


def comfy_queue(workflow, url="http://127.0.0.1:8188", min_free_mb=None, root=None):
    # ComfyUI exposes two JSON formats: the editor's UI graph (`nodes`/`links`)
    # and the API prompt graph (`{id: {class_type, inputs}}`).  The latter is
    # required by /prompt; fail early with an actionable message instead of
    # forwarding a UI graph and surfacing an opaque HTTP 500.
    if isinstance(workflow, dict) and isinstance(workflow.get('nodes'), list):
        converted = comfy_ui_to_api_workflow(workflow, url)
        if not converted.get('ok'): return converted
        workflow = converted['workflow']
    try: url = _safe_comfy_url(url)
    except ValueError as e: return {"ok": False, "error": str(e)}
    # 租约必须覆盖"提交 → ComfyUI 异步生成 → history 轮询到完成"整个周期，
    # 不能像旧版只在 POST /prompt 期间持有（请求返回时代码还在 GPU 上跑）。
    #
    # 【CUDA 隔离接线点（待实现，勿提前宣称）】lease 里的 gpu 只是协调层卡号，
    # 对外部常驻 ComfyUI 服务不产生隔离——它不会继承 Agent 工作台 进程的环境。
    # 未来若改为由 Agent 工作台 直接 Popen ComfyUI/worker，必须在启动前往子进程
    # 环境写入 CUDA_VISIBLE_DEVICES=<lease["gpu"]>（进程初始化后改无效）。
    # 验收口径见 HANDOFF.md 第 5 节 P2-1 待验收项 B。
    submit_owner = f"comfyui:submit:{uuid.uuid4().hex[:12]}"
    # min_free_mb=None：沿用 DOCMIND_COMFY_MIN_FREE_MB（默认 1024MB）；
    # 视频大模型靠逐层 offload 运行，空闲几百 MB 也能跑，调用方可显式放宽。
    threshold = COMFY_MIN_FREE_MB if min_free_mb is None else float(min_free_mb)
    lease = _gpu.acquire_lease(submit_owner, timeout=2, purpose="comfyui",
                               ttl=COMFY_JOB_TTL,
                               min_free_mb=threshold or None,
                               evict=("ollama", "comfyui"))
    if not lease.get("ok"):
        return {"ok": False, "error": _gpu_busy_error(lease)}
    payload = json.dumps({"prompt": workflow}).encode()
    req = urllib.request.Request(url.rstrip('/') + '/prompt', data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
    except Exception as e:
        _gpu.release(submit_owner)
        return {"ok": False, "error": f"ComfyUI 请求失败：{e}"}
    # 提交成功：把租约改名成作业 owner，TTL 从这一刻重新起算；
    # 之后由 comfy_history 见终态释放、comfy_cancel 中断释放，或 TTL 兜底回收。
    prompt_id = str(resp.get("prompt_id") or "")
    if prompt_id:
        _gpu.reown(submit_owner, _comfy_job_owner(prompt_id, root), purpose="comfyui", ttl=COMFY_JOB_TTL)
    else:
        _gpu.release(submit_owner)
    canonical = json.dumps(workflow, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    result = {"ok": True, "response": resp, "workflow_sha256": hashlib.sha256(canonical).hexdigest(),
              "lease": {"owner": _comfy_job_owner(prompt_id, root) if prompt_id else "",
                        "ttl": COMFY_JOB_TTL, "gpu": lease.get("gpu"),
                        "evicted": bool(lease.get("evicted"))}}
    if prompt_id:
        # 后台 watch 只负责轮询状态，不再单独持租约（租约已 reown 给本作业 owner）
        result["watch"] = comfy_watch(prompt_id, url, root=root)
        jobs = _comfy_jobs(root)
        with _COMFY_JOBS_LOCK:
            jobs.setdefault(prompt_id, {}).update({'workflow': workflow, 'workflow_sha256': result['workflow_sha256'], 'gpu': lease.get('gpu'), 'status': 'queued', 'retry_count': 0, 'project_root': _comfy_key(root)})
            _save_comfy_history(root)
    return result

def comfy_history(prompt_id, url="http://127.0.0.1:8188", _release=True, root=None):
    try: url = _safe_comfy_url(url)
    except ValueError as e: return {"ok": False, "error": str(e)}
    pid = str(prompt_id or "").strip()
    if not pid or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pid):
        return {"ok": False, "error": "无效的 ComfyUI prompt_id。"}
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/history/' + pid, timeout=5) as r:
            data = json.loads(r.read().decode())
        item = data.get(pid, data)
        outputs = []
        for node in (item.get("outputs") or {}).values():
            for img in (node.get("images") or []):
                if isinstance(img, dict):
                    x = dict(img)
                    name = str(x.get('filename') or 'output.bin')
                    sub = str(x.get('subfolder') or '')
                    x['preview_url'] = url + '/view?' + urllib.parse.urlencode(
                        {'filename': name, 'subfolder': sub, 'type': x.get('type') or 'output'})
                    x['mime'] = mimetypes.guess_type(name)[0] or 'application/octet-stream'
                    x['asset_kind'] = '3d' if os.path.splitext(name)[1].lower() in {'.glb','.gltf','.fbx','.obj','.stl','.ply','.usd','.usdz'} else ('media' if x['mime'].split('/')[0] in {'image','audio','video'} else 'unknown')
                    # 3D assets are metadata/import-only until an engine-backed
                    # renderer is available; never imply that a binary MIME
                    # type is itself preview support.
                    x['preview_supported'] = x['asset_kind'] != '3d'
                    outputs.append(x)
        status = item.get("status", {}) or {}
        messages = status.get('messages') or []
        executed = sum(1 for m in messages if isinstance(m, list) and m and m[0] in ('execution_cached', 'executed'))
        total = len(item.get('prompt', {}) or {})
        progress = {"executed_nodes": executed, "total_nodes": total,
                    "percent": round(executed * 100 / total, 1) if total else (100.0 if item.get('outputs') else 0.0)}
        status_str = str(status.get("status_str") or "")
        finished = bool(item.get("outputs")) or bool(status.get("completed")) or status_str in ("success", "error", "failed")
        failed = status_str in ("error", "failed")
        result = {"ok": True, "prompt_id": pid, "status": status, "outputs": outputs,
                  "done": bool(item.get("outputs")), "finished": finished, "failed": failed,
                  "progress": progress}
        # Keep the durable local history aligned with the authoritative ComfyUI
        # response.  This is intentionally best effort: a history read must
        # still succeed when the state file cannot be written.
        jobs = _comfy_jobs(root)
        with _COMFY_JOBS_LOCK:
            job = jobs.get(pid)
            if job is not None:
                status_value = "failed" if failed else ("completed" if finished else "running")
                job.update({"status": status_value, "progress": progress,
                            "outputs": outputs, "result": result})
                if finished:
                    job.update({"running": False, "done": True,
                                "finished_at": job.get("finished_at") or datetime.now().isoformat(timespec="seconds")})
                    if job.get("cancel_requested"):
                        job["cancel_state"] = "terminated"
                _save_comfy_history(root)
        if finished and _release:
            # 生成结束（成功/失败都算）：释放作业租约，让排队的 Ollama/下一作业上卡。
            # 后台 watch 轮询用 _release=False 只更新状态，避免与用户轮询
            # （API GET /api/comfy/history，前端 TaskEnginePanel 每 4s 轮询）抢释放，
            # 导致 lease_released 上报不确定；真正的释放由用户侧 history 轮询负责。
            result["lease_released"] = _gpu.force_release(_comfy_job_owner(pid, root)) is not None
        return result
    except Exception as e:
        return {"ok": False, "error": f"ComfyUI 状态查询失败：{e}"}

def comfy_cancel(prompt_id, url="http://127.0.0.1:8188", root=None):
    """按 prompt_id 定向取消 ComfyUI 作业并释放 GPU 租约（用户取消 / 排队取消的落地动作）。

    ComfyUI 正确取消口径（P2-2 收尾修正）：
    - 唯一能按 prompt_id 精确取消的接口是 `POST /queue` 带 `{"delete":[prompt_id]}`。
      ComfyUI 的 `delete_prompt` 会：① 从队列移除未执行的任务；② 若该任务正在执行，
      自动调用 `interrupt()` 中断它——因此**不会误伤其他正在跑的任务**。
    - `POST /interrupt`（全局、不带 prompt_id）只中断“当前正在执行”的任务，无法定向取消
      某条队列任务；旧实现误带 `prompt_id` 体发 /interrupt，真实 ComfyUI 忽略该体，
      等于取消不掉指定任务。此 bug 已修复。
    """
    try: url = _safe_comfy_url(url)
    except ValueError as e: return {"ok": False, "error": str(e)}
    pid = str(prompt_id or "").strip()
    if not pid or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", pid):
        return {"ok": False, "error": "无效的 ComfyUI prompt_id。"}
    deleted = False
    err = ""
    try:
        req = urllib.request.Request(url.rstrip('/') + '/queue',
                                     data=json.dumps({'delete': [pid]}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            deleted = 200 <= r.status < 300
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    jobs = _comfy_jobs(root)
    with _COMFY_JOBS_LOCK:
        job = jobs.get(pid)
        if job:
            job.update({'cancel_requested': True,
                        'cancel_state': 'requesting',
                        'cancel_requested_at': datetime.now().isoformat(timespec='seconds')})
            _save_comfy_history(root)
    # watcher 存在时等待 ComfyUI history 报告终态再释放租约；没有 watcher
    # 的兼容调用才允许立即释放，避免取消请求尚未生效时发生 GPU 抢占。
    with _COMFY_JOBS_LOCK:
        has_watcher = pid in jobs
    released = (not has_watcher) and (_gpu.force_release(_comfy_job_owner(pid, root)) is not None)
    # ComfyUI 成功从队列删除即视为取消已生效
    if deleted and has_watcher:
        with _COMFY_JOBS_LOCK:
            if pid in jobs:
                jobs[pid]['cancel_state'] = 'requested'
                _save_comfy_history(root)
    return {"ok": released or deleted, "prompt_id": pid,
            "deleted": deleted, "interrupted": deleted, "lease_released": released,
            "cancel_state": 'requested' if deleted else 'failed',
            "error": err if (err and not (released or deleted)) else ""}

def comfy_wait(prompt_id, url="http://127.0.0.1:8188", timeout=120, interval=1.0, root=None):
    deadline=time.time()+max(1,min(int(timeout),600))
    while time.time()<deadline:
        result=comfy_history(prompt_id,url,_release=False,root=root)
        if not result.get("ok"): return result
        status=result.get("status") or {}
        if result.get("finished"):
            return result
        time.sleep(max(.1,min(float(interval),10)))
    # 轮询超时不释放租约：ComfyUI 侧可能仍在生成，交给作业 TTL 兜底回收
    return {"ok":False,"prompt_id":str(prompt_id),"timeout":True,"error":"ComfyUI 生成轮询超时。"}

def comfy_watch(prompt_id, url="http://127.0.0.1:8188", timeout=900, interval=1.0, root=None):
    """启动后台 ComfyUI history 轮询；返回可查询的 job 状态，不阻塞 API 请求。

    注意：watch 线程自身**不持有 GPU 租约**。整作业周期的租约由 comfy_queue
    提交成功后 reown 为 comfyui:{prompt_id}，终态由 history/cancel 释放——
    watch 再申请同名/异名租约都会造成重复占卡或自锁。
    """
    key = str(prompt_id)
    jobs = _comfy_jobs(root)
    with _COMFY_JOBS_LOCK:
        existing = jobs.get(key)
        if existing and existing.get('running'):
            return {'ok': True, 'job': dict(existing)}
    with _COMFY_JOBS_LOCK:
        job = {'prompt_id': key, 'running': True, 'done': False, 'result': None,
               'started_at': datetime.now().isoformat(timespec='seconds')}
        jobs[key] = job
    def worker():
        result = comfy_wait(key, url, timeout, interval, root=root)
        with _COMFY_JOBS_LOCK:
            job.update({'running': False, 'done': bool(result.get('finished') or result.get('done')),
                        'result': result,
                        'status': ('failed' if result.get('failed') else ('completed' if result.get('finished') else 'timeout')),
                        'outputs': result.get('outputs') or [],
                        'progress': result.get('progress') or {},
                        'finished_at': datetime.now().isoformat(timespec='seconds')})
            if job.get('cancel_requested') and result.get('finished'):
                job['cancel_state'] = 'terminated'
            _save_comfy_history(root)
    threading.Thread(target=worker, name='comfy-watch', daemon=True).start()
    return {'ok': True, 'job': dict(job)}

def comfy_watch_status(prompt_id, root=None):
    jobs = _comfy_jobs(root)
    with _COMFY_JOBS_LOCK:
        job = jobs.get(str(prompt_id))
        return {'ok': bool(job), 'job': dict(job) if job else None}

def comfy_history_list(page=1, page_size=20, root=None):
    """返回本地持久化 ComfyUI 作业历史分页。"""
    try:
        page=max(1,int(page)); page_size=max(1,min(int(page_size),100))
    except Exception: page,page_size=1,20
    jobs = _comfy_jobs(root)
    with _COMFY_JOBS_LOCK:
        rows=[dict(v) for v in jobs.values()]
    rows.sort(key=lambda x: x.get('finished_at') or x.get('started_at') or '', reverse=True)
    start=(page-1)*page_size
    return {'ok':True,'page':page,'page_size':page_size,'total':len(rows),'items':rows[start:start+page_size]}

def comfy_retry(prompt_id, url='http://127.0.0.1:8188', root=None):
    jobs = _comfy_jobs(root)
    with _COMFY_JOBS_LOCK:
        job = jobs.get(str(prompt_id))
        if not job: return {'ok':False,'error':'找不到作业历史'}
        if job.get('status') not in ('failed','error'): return {'ok':False,'error':'仅失败作业可重试'}
        if int(job.get('retry_count',0)) >= 2: return {'ok':False,'error':'已达到最多 2 次重试'}
        workflow = job.get('workflow'); count = int(job.get('retry_count',0))+1
    if not workflow: return {'ok':False,'error':'历史中缺少 workflow，无法重试'}
    result = comfy_queue(workflow, url, root=root)
    if result.get('ok'):
        with _COMFY_JOBS_LOCK:
            new_id = str((result.get('response') or {}).get('prompt_id') or '')
            if new_id in jobs: jobs[new_id]['retry_count'] = count
            _save_comfy_history(root)
        result['retry_of'] = str(prompt_id); result['retry_count'] = count
    return result

def comfy_import(root, prompt_id, image, url="http://127.0.0.1:8188", dest_dir="assets/generated"):
    """Download one ComfyUI output into a project asset directory with metadata."""
    try: url = _safe_comfy_url(url)
    except ValueError as e: return {"ok": False, "error": str(e)}
    name = os.path.basename(str(image.get("filename") or "output.bin"))
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", name): return {"ok": False, "error": "无效的资源文件名。"}
    sub = str(image.get("subfolder") or "").replace("\\", "/").strip("/")
    if sub and (".." in sub.split("/") or not re.fullmatch(r"[A-Za-z0-9._/-]{1,300}", sub)): return {"ok": False, "error": "无效的资源子目录。"}
    rel = "/".join(x for x in [dest_dir.strip("/"), name] if x)
    target = _file(root, rel); os.makedirs(os.path.dirname(target), exist_ok=True)
    query = urllib.parse.urlencode({"filename": name, "subfolder": sub, "type": image.get("type") or "output"})
    try:
        with urllib.request.urlopen(url + '/view?' + query, timeout=30) as r:
            data = r.read(COMFY_MAX_DOWNLOAD + 1)
        if len(data) > COMFY_MAX_DOWNLOAD: return {"ok": False, "error": "资源超过 25MB 下载上限。"}
        with open(target, "wb") as f: f.write(data)
        meta_path = _file(root, rel + ".json")
        ext = os.path.splitext(name)[1].lower()
        kind = '3d' if ext in {'.glb','.gltf','.fbx','.obj','.stl','.ply','.usd','.usdz'} else ('image' if ext in {'.png','.jpg','.jpeg','.webp'} else ('video' if ext in {'.mp4','.webm','.mov'} else 'other'))
        meta = {"source": "comfyui", "url": url, "prompt_id": str(prompt_id), "filename": name, "subfolder": sub, "mime": mimetypes.guess_type(name)[0] or 'application/octet-stream', "asset_kind": kind, "preview_supported": kind in {'image','video'}, "imported_at": datetime.now().isoformat(timespec="seconds"), "size": len(data)}
        for key in ('license', 'source_url', 'author', 'workflow_sha256'):
            if image.get(key): meta[key] = str(image[key])[:1000]
        with open(meta_path, "w", encoding="utf-8") as f: json.dump(meta, f, ensure_ascii=False, indent=2)
        return {"ok": True, "path": rel.replace("\\", "/"), "metadata": meta}
    except Exception as e: return {"ok": False, "error": f"资源下载失败：{e}"}

def comfy_import_all(root, prompt_id, images, url="http://127.0.0.1:8188", dest_dir="assets/generated"):
    results = [comfy_import(root, prompt_id, image, url, dest_dir) for image in (images or [])[:32]]
    return {"ok": all(x.get("ok") for x in results), "results": results, "imported": sum(1 for x in results if x.get("ok"))}

def comfy_validate_provenance(meta):
    """校验资源来源元数据；不替代人工许可证审阅。"""
    m=meta or {}; errors=[]
    for key in ('author','license'):
        if m.get(key) is not None and (not isinstance(m[key], str) or len(m[key])>1000): errors.append(f'{key} 格式无效')
    if m.get('source_url'):
        u=str(m['source_url'])
        if not re.match(r'^https?://[^\s]{1,1000}$',u): errors.append('source_url 必须是 http(s) URL')
    return {'ok': not errors, 'errors': errors, 'review_required': not bool(m.get('license'))}

def comfy_resource_duplicates(root, directory="assets/generated"):
    """按 SHA-256 查找 ComfyUI 导入目录中的重复资源，只读。"""
    base = _file(root, directory)
    groups = {}
    if not os.path.isdir(base):
        return {'ok': True, 'directory': directory, 'groups': [], 'files': 0}
    count = 0
    for dp, _, files in os.walk(base):
        for fn in files:
            if fn.endswith('.json'):
                continue
            path = os.path.join(dp, fn)
            try:
                h = hashlib.sha256()
                with open(path, 'rb') as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b''): h.update(chunk)
                rel = os.path.relpath(path, _root(root)).replace('\\', '/')
                groups.setdefault(h.hexdigest(), []).append(rel); count += 1
            except OSError:
                continue
    dup = [{'sha256': h, 'paths': paths, 'duplicate_count': len(paths)-1} for h, paths in groups.items() if len(paths) > 1]
    return {'ok': True, 'directory': directory, 'groups': dup, 'duplicate_files': sum(x['duplicate_count'] for x in dup), 'files': count}

def comfy_unused_resources(root, directory="assets/generated"):
    """查找生成目录中未被项目文本文件引用的资源（仅提供疑似列表）。"""
    base_root = _root(root); asset_root = _file(root, directory)
    if not os.path.isdir(asset_root): return {'ok': True, 'directory': directory, 'unused': [], 'files': 0}
    haystack = []
    for dp, _, files in os.walk(base_root):
        if any(x in dp.split(os.sep) for x in ('.git','node_modules','.venv','Library','Intermediate','DerivedDataCache')): continue
        for fn in files:
            if fn.endswith(('.json','.meta','.import')) or fn.endswith(('.png','.jpg','.jpeg','.webp','.wav','.mp3','.ogg','.mp4','.webm')): continue
            try:
                with open(os.path.join(dp,fn), encoding='utf-8', errors='ignore') as f: haystack.append(f.read())
            except OSError: pass
    text='\n'.join(haystack); unused=[]; count=0
    for dp, _, files in os.walk(asset_root):
        for fn in files:
            if fn.endswith('.json'): continue
            count += 1; rel=os.path.relpath(os.path.join(dp,fn),base_root).replace('\\','/')
            if fn not in text and rel not in text and ('/' + rel) not in text: unused.append(rel)
    return {'ok': True, 'directory': directory, 'unused': unused, 'files': count, 'unused_count': len(unused)}

# Dedicated module keeps scene inspection and runtime capture independently testable.
from scene_runtime import scene_tree, runtime_events, set_scene_property

def task_revert(root, task):
    import workbench_fs
    results=[]
    snap=(task.get('snapshot') or {}).get('files') or {}
    for rel in task.get('files') or []:
        try:
            saved=snap.get(str(rel).replace('\\','/'))
            if saved and 'content' in saved:
                target,_=workbench_fs._resolve(root, rel, must_exist=False, for_write=True)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target,'w',encoding='utf-8',newline='') as f: f.write(saved['content'])
                results.append({'ok':True,'path':rel,'restored':'snapshot'})
            elif saved is None and os.path.lexists(_file(root, rel)):
                target,_=workbench_fs._resolve(root, rel, must_exist=True, for_write=True)
                os.remove(target)
                results.append({'ok':True,'path':rel,'restored':'deleted-created-file'})
            else: results.append(workbench_fs.revert_file(root, rel))
        except Exception as e: results.append({'ok':False,'path':rel,'error':str(e)})
    return {'ok': all(x.get('ok') for x in results), 'results': results}

def task_branch(root, task):
    import workbench_fs
    branch = re.sub(r'[^A-Za-z0-9._/-]+', '-', str(task.get('id') or task.get('title') or 'task')).strip('-/')[:60] or 'task'
    full_branch = 'docmind/' + branch
    repo = workbench_fs._find_repo_bounded(_root(root), _root(root))
    if not repo: return {'ok': False, 'error': '项目未初始化 Git。'}
    _, current = workbench_fs._git(['branch', '--show-current'], cwd=repo)
    current = current.strip()
    exists, _ = workbench_fs._git(['show-ref', '--verify', '--quiet', 'refs/heads/' + full_branch], cwd=repo)
    if exists:
        if current != full_branch:
            # 已有分支只在工作区干净时切换，避免覆盖用户未提交修改。
            _, porcelain = workbench_fs._git(['status', '--porcelain'], cwd=repo)
            if porcelain.strip():
                return {'ok': False, 'error': '当前工作区有未提交修改，无法安全切换到已有任务分支。', 'branch': full_branch}
            ok, out = workbench_fs._git(['switch', full_branch], cwd=repo)
            if not ok: return {'ok': False, 'error': out[:300]}
        created = False
    else:
        ok, out = workbench_fs._git(['switch','-c',full_branch], cwd=repo)
        if not ok: return {'ok': False, 'error': out[:300]}
        created = True
    # 将分支写回任务记录，后续回滚/审计可追踪实际工作分支。
    if task.get('id'):
        rows = _jsonl(_project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl'))
        for row in rows:
            if str(row.get('id')) == str(task.get('id')):
                row['branch'] = full_branch
                row['updated_at'] = datetime.now().isoformat(timespec='seconds')
        with open(_project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl'), 'w', encoding='utf-8') as f:
            for row in rows: f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return {'ok': True, 'branch': full_branch, 'created': created, 'current': full_branch}

def _root(root):
    return os.path.abspath(root) if root else ""

def _file(root, name):
    base = _root(root); p = os.path.abspath(os.path.join(base, name))
    if not (p == base or p.startswith(base + os.sep)): raise ValueError("path outside project")
    return p

def _jsonl(path):
    if not os.path.isfile(path): return []
    out=[]
    with open(path, encoding="utf-8") as f:
        for line in f:
            try: out.append(json.loads(line))
            except ValueError: pass
    return out

def list_tasks(root, status=""):
    rows = _jsonl(_project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl'))
    return [x for x in rows if not status or x.get("status") == status]

def upsert_task(root, task):
    path = _project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl'); rows = _jsonl(path)
    tid = (task.get("id") or "TASK-" + datetime.now().strftime("%Y%m%d-%H%M%S")).strip()
    item = {"id": tid, "title": task.get("title", ""), "description": task.get("description", ""), "region": task.get("region", ""), "priority": task.get("priority", "normal"), "status": task.get("status", "open"), "owner": task.get("owner", ""), "files": task.get("files", []), "allowed_paths": task.get("allowed_paths", []), "symbols": task.get("symbols", []), "verification": task.get("verification", []), "impact_files": task.get("impact_files", []), "snapshot": task.get("snapshot", {}), "updated_at": datetime.now().isoformat(timespec="seconds")}
    rows = [x for x in rows if x.get("id") != tid] + [item]
    with open(path, "w", encoding="utf-8") as f:
        for x in rows: f.write(json.dumps(x, ensure_ascii=False) + "\n")
    return item

def validate_task_scope(root, task):
    """Validate that task files stay inside the declared region and project root."""
    root = _root(root); region = (task.get("region") or "").strip().replace("\\", "/")
    allowed = [str(x).replace("\\", "/").strip("/") for x in (task.get("allowed_paths") or [])]
    errors = []
    for rel in task.get("files") or []:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            errors.append({"path": rel, "error": "路径包含 .."}); continue
        if region and not (rel == region or rel.startswith(region + "/")):
            errors.append({"path": rel, "error": "文件不在任务分区内"}); continue
        if allowed and not any(rel == p or rel.startswith(p.rstrip("/") + "/") for p in allowed):
            errors.append({"path": rel, "error": "文件不在 allowed_paths 范围内"})
    return {"ok": not errors, "errors": errors, "region": region, "allowed_paths": allowed}

def task_impact(root, task):
    """Resolve semantic files plus relation-graph neighbors for an editable task."""
    query = " ".join([str(task.get("title", "")), str(task.get("description", "")), *[str(x) for x in task.get("symbols", [])]])
    direct = [str(x).replace("\\", "/") for x in (task.get("files") or [])]
    related = impact_analysis(root, query, k=12) if query.strip() else []
    graph_files = []
    try:
        import workbench_fs
        g = workbench_fs.build_relation_graph(root)
        ids = {"gd:" + p for p in direct} | {"py:" + p for p in direct}
        for e in g.get("edges", []):
            if e.get("source") in ids or e.get("target") in ids:
                for nid in (e.get("source"), e.get("target")):
                    n = next((x for x in g.get("nodes", []) if x.get("id") == nid), None)
                    if n and n.get("rel"): graph_files.append(n["rel"])
    except Exception:
        pass
    files = list(dict.fromkeys(direct + related + graph_files))
    return {"ok": True, "direct_files": direct, "semantic_files": related, "related_files": graph_files, "files": files}

def task_snapshot(root, task):
    """Capture lightweight pre-edit mtimes and sizes for task auditing."""
    rows = {}
    for rel in task.get("files") or []:
        try:
            st = os.stat(_file(root, str(rel)))
            key = str(rel).replace("\\", "/")
            item = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
            if st.st_size <= 512 * 1024:
                with open(_file(root, str(rel)), encoding='utf-8', errors='replace') as f: item['content'] = f.read()
            rows[key] = item
        except OSError:
            rows[str(rel).replace("\\", "/")] = None
    return {"created_at": datetime.now().isoformat(timespec="seconds"), "files": rows}

def verify_task(root, task):
    """Run declared checks (or safe defaults) and return one auditable report."""
    checks = task.get("verification") or []
    if not checks: checks = ["python -m unittest discover -s tests"] if os.path.isdir(_file(root, "tests")) else []
    if not checks:
        return {"ok": False, "skipped": True, "reason": "no_checks", "checks": [], "completed_at": datetime.now().isoformat(timespec="seconds")}
    results = []
    for cmd in checks[:8]:
        try:
            from tools import _cmd_is_blocked
            blocked, why = _cmd_is_blocked(str(cmd))
        except Exception: blocked, why = False, ""
        if blocked: results.append({"command": cmd, "ok": False, "error": f"命令被安全策略拦截：{why}"}); continue
        try:
            p = subprocess.run(str(cmd), shell=True, cwd=_root(root), capture_output=True, text=True, timeout=120)
            results.append({"command": cmd, "ok": p.returncode == 0, "output": (p.stdout + p.stderr)[-3000:]})
        except Exception as e: results.append({"command": cmd, "ok": False, "error": str(e)})
    report = {"ok": all(x["ok"] for x in results), "checks": results, "completed_at": datetime.now().isoformat(timespec="seconds")}
    if task.get("id"):
        rows = _jsonl(_project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl')); tid = str(task["id"])
        for row in rows:
            if str(row.get("id")) == tid:
                row["status"] = "verified" if report["ok"] else "failed"
                row["verification_result"] = report
                row["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(_project_state.path(root, 'tasks.jsonl', legacy='.docmind_tasks.jsonl'), "w", encoding="utf-8") as f:
            for row in rows: f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return report

def validate_data(root):
    errors=[]; checked=0
    for base, dirs, files in os.walk(_root(root)):
        dirs[:] = [d for d in dirs if d not in {".git",".venv","__pycache__","build","dist","node_modules",".chroma"}]
        for name in files:
            if os.path.splitext(name)[1].lower() not in {".json",".yaml",".yml",".csv",".toml"}: continue
            path=os.path.join(base,name); checked+=1
            if name.endswith(".json"):
                try:
                    with open(path, encoding="utf-8") as f: json.load(f)
                except Exception as e: errors.append({"path":os.path.relpath(path,root),"error":str(e)})
    return {"ok": not errors, "checked": checked, "errors": errors}

def localization_check(root):
    keys={}; missing=[]
    for base, dirs, files in os.walk(_root(root)):
        dirs[:] = [d for d in dirs if d not in {".git",".venv","__pycache__","build","dist","node_modules"}]
        for name in files:
            if not re.search(r"(locale|localization|i18n|translation)", name, re.I): continue
            try:
                with open(os.path.join(base,name), encoding="utf-8") as f: data=json.load(f)
                if isinstance(data,dict): keys[name]=set(data)
            except Exception: pass
    if len(keys)>1:
        ref=set.intersection(*keys.values())
        allk=set.union(*keys.values())
        missing=[{"file":n,"keys":sorted(allk-v)} for n,v in keys.items() if v != allk]
    return {"ok": not missing, "files": list(keys), "missing": missing}

def release_check(root):
    data=validate_data(root); loc=localization_check(root); warnings=[]
    if os.path.isfile(_file(root,".env")): warnings.append("项目包含 .env，请确认发布包未携带密钥")
    return {"ok": data["ok"] and loc["ok"] and not warnings, "data": data, "localization": loc, "warnings": warnings}

def project_memory(root, content=None):
    path=_file(root,"DOCMIND_MEMORY.md")
    if content is not None:
        with open(path,"w",encoding="utf-8") as f: f.write(content)
    return open(path,encoding="utf-8").read() if os.path.isfile(path) else ""

def simulate_growth(levels=50, base=100, growth=1.08, model="geometric", k=None):
    """模拟数值成长曲线，返回逐等级数值 + 摘要统计。

    用于游戏数值/经济平衡（等级经验、货币积累、资源产出等）的离线推演。
    支持多种成长模型（model）：
      - geometric   复合成长：v_i = base * growth**(i-1)        （默认，指数型经验曲线）
      - linear      线性成长：v_i = base + step*(i-1)，step = base*(growth-1)
      - logistic    S 形（带承载上限 K）：v_i = K / (1 + (K/base-1)·e^{-r·(i-1)})，
                    K = k 或 base*20；r 由 growth 映射（growth>1 才有意义）
      - diminishing 边际递减（凹函数，前期陡峭后期平缓的 XP）：v_i = base * i**p，
                    p = clamp((growth-1)*2, 0.15, 0.95)
    未知 model 退化为 geometric，保证任何入参都出数。

    返回 {ok, model, values:[{level,value}], summary:{count,start,end,total,
    peak_increment,peak_increment_level,doubling_level?,inflection_level?}}。
    """
    levels = max(1, min(int(levels), 1000))
    try:
        base = float(base)
    except (TypeError, ValueError):
        base = 100.0
    if base <= 0:
        base = 1.0
    try:
        growth = float(growth)
    except (TypeError, ValueError):
        growth = 1.08
    model = (model or "geometric").lower()
    if k in (None, "", 0):
        kcap = None
    else:
        try:
            kcap = float(k)
        except (TypeError, ValueError):
            kcap = None

    values = []
    if model == "linear":
        step = base * (growth - 1) if growth > 1 else 0.0
        for i in range(1, levels + 1):
            values.append(round(base + step * (i - 1), 4))
    elif model == "logistic":
        K = kcap if (kcap and kcap > base) else base * 20.0
        r = max(0.01, (growth - 1) * 2.0)
        for i in range(1, levels + 1):
            v = K / (1.0 + (K / base - 1.0) * math.exp(-r * (i - 1)))
            values.append(round(v, 4))
    elif model == "diminishing":
        p = max(0.15, min(0.95, (growth - 1) * 2.0))
        for i in range(1, levels + 1):
            values.append(round(base * (i ** p), 4))
    else:  # geometric（默认 / 未知 model 兜底）
        for i in range(1, levels + 1):
            values.append(round(base * (growth ** (i - 1)), 4))

    series = [{"level": i, "value": values[i - 1]} for i in range(1, levels + 1)]

    increments = [values[i] - values[i - 1] for i in range(1, len(values))]
    peak_inc = max(increments) if increments else 0.0
    peak_inc_level = increments.index(peak_inc) + 2 if increments else 1
    doubling_level = None
    for i, v in enumerate(values):
        if v >= base * 2:
            doubling_level = i + 1
            break
    summary = {
        "count": len(values),
        "start": values[0],
        "end": values[-1],
        "total": round(sum(values), 4),
        "peak_increment": round(peak_inc, 4),
        "peak_increment_level": peak_inc_level,
    }
    if doubling_level is not None:
        summary["doubling_level"] = doubling_level
    if model == "logistic":
        try:
            inf = 1 + math.log(K / base - 1.0) / r
            summary["inflection_level"] = round(max(1.0, inf), 1)
        except (ValueError, ZeroDivisionError, OverflowError):
            pass

    return {"ok": True, "model": model, "values": series, "summary": summary}

def asset_dependencies(root):
    refs=[]
    for base,dirs,files in os.walk(_root(root)):
        dirs[:]=[d for d in dirs if d not in {".git",".venv","build","dist",".chroma","__pycache__"}]
        for name in files:
            if os.path.splitext(name)[1].lower() not in {".py",".js",".ts",".cs",".json",".yaml",".yml"}: continue
            p=os.path.join(base,name)
            try: text=open(p,encoding="utf-8",errors="ignore").read()
            except OSError: continue
            for m in re.finditer(r"(?:asset|sprite|texture|sound|audio|resource)[_:/\"']+([A-Za-z0-9_./-]+)",text,re.I):
                refs.append({"source":os.path.relpath(p,root),"asset":m.group(1)})
    return refs

def preview_resource(root, rel):
    p=_file(root,rel); st=os.stat(p); mime=mimetypes.guess_type(p)[0] or "application/octet-stream"
    return {"path":rel.replace("\\","/"),"mime":mime,"size":st.st_size,"extension":os.path.splitext(p)[1].lower()}

def create_placeholder(root, rel, kind="text"):
    p=_file(root,rel); os.makedirs(os.path.dirname(p),exist_ok=True)
    if os.path.exists(p): raise ValueError("file already exists")
    if kind == "json": content="{}\n"
    elif kind == "svg": content='<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"><rect width="128" height="128" fill="#777"/><text x="10" y="68" fill="white">PLACEHOLDER</text></svg>\n'
    else: content="# PLACEHOLDER\n"
    open(p,"w",encoding="utf-8").write(content); return preview_resource(root,rel)

def _rel(root_abs, src):
    """把检索到的 source 路径规整为相对 code_root 的路径（已是相对则原样返回）。"""
    try:
        if os.path.isabs(src) and src.startswith(root_abs):
            return os.path.relpath(src, root_abs)
    except Exception:  # noqa: BLE001
        pass
    return src


def impact_analysis(root, query, k=8):
    """返回受改动影响最大的源码文件（相对路径，去重排序）。

    优先用语义检索（需代码库已索引进 Chroma 的 code 集合，bge-m3 向量）；
    不可用时退回大小写不敏感子串 grep，保证任何环境都能用。
    """
    root_abs = _root(root)
    # 1) 语义检索
    try:
        from vectorstore import query as vs_query, CODE_COLLECTION_NAME, count as vs_count
        if vs_count(CODE_COLLECTION_NAME) > 0:
            from embeddings import EmbeddingClient
            vec = EmbeddingClient().embed([query])[0]
            res = vs_query(vec, k=k, collection=CODE_COLLECTION_NAME)
            metas = (res.get("metadatas") or [[]])[0]
            files = []
            for m in metas:
                src = (m or {}).get("source") if isinstance(m, dict) else None
                if src:
                    files.append(_rel(root_abs, src))
            if files:
                return sorted(set(files))
    except Exception:  # noqa: BLE001
        pass
    # 2) 兜底：子串 grep
    hits = []
    for base, dirs, files in os.walk(root_abs):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "build", "dist", ".chroma", "__pycache__"}]
        for name in files:
            if os.path.splitext(name)[1].lower() not in {".py", ".js", ".ts", ".cs", ".json", ".yaml", ".yml"}:
                continue
            p = os.path.join(base, name)
            try:
                if query.lower() in open(p, encoding="utf-8", errors="ignore").read().lower():
                    hits.append(os.path.relpath(p, root))
            except OSError:
                pass
    return sorted(hits)

_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs",
              ".cpp", ".c", ".h", ".hpp", ".lua", ".gd", ".kt", ".swift", ".rb"}


def _extract_symbols(path):
    """从源码文件提取可测符号（函数/类/方法名）。Python 走 ast，其它语言走启发式正则。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        text = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    if ext == ".py":
        try:
            tree = _ast.parse(text)
        except SyntaxError:
            return []
        syms = []
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                syms.append(("function", node.name))
            elif isinstance(node, _ast.ClassDef):
                syms.append(("class", node.name))
        return syms
    found = []
    for m in re.finditer(
        r"\b(?:function|func|def|class|public|private|protected|static|interface|struct|enum)\s+([A-Za-z_]\w*)",
        text,
    ):
        found.append(("symbol", m.group(1)))
    seen, out = set(), []
    for kind, name in found:
        if name not in seen:
            seen.add(name)
            out.append((kind, name))
    return out


def _read_contract_fields(region_abs):
    """读取分区内契约/模式文件（*.schema.json / manifest.json / contract.json），提取对外接口字段。"""
    fields = []
    names = os.listdir(region_abs) if os.path.isdir(region_abs) else []
    for name in names:
        low = name.lower()
        if not (low.endswith(".schema.json") or low in ("manifest.json", "contract.json")):
            continue
        p = os.path.join(region_abs, name)
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            props = data.get("properties") or data.get("fields") or {}
            if isinstance(props, dict):
                fields.extend(sorted(props.keys()))
    return sorted(set(fields))


def generate_test_scene(root, name, region="behaviors"):
    """基于分区真实代码与契约生成测试场景：扫描公共符号 + 契约字段，并产出可运行测试骨架。

    产出（返回相对路径列表）：
      <region>/tests/<name>.json      场景描述（步骤引用真实符号/契约字段，而非死模板）
      <region>/tests/test_<name>.py   可运行测试骨架（仅当分区含 .py 时生成，可被 playtest 的 unittest 发现）
    """
    region_abs = os.path.join(_root(root), region)
    tests_dir = os.path.join(region_abs, "tests")
    os.makedirs(tests_dir, exist_ok=True)
    json_rel = os.path.join(region, "tests", (name if name.endswith(".json") else name + ".json"))
    json_path = _file(root, json_rel)
    if os.path.exists(json_path):
        raise ValueError("test scene already exists")

    # 1) 收集分区内的代码文件与符号（跳过 tests/ 自身）
    code_files, targets, has_py = [], [], False
    for base, dirs, files in os.walk(region_abs):
        if os.path.basename(base) == "tests":
            continue
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _CODE_EXTS:
                continue
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            code_files.append(rel)
            if ext == ".py":
                has_py = True
            for kind, sym in _extract_symbols(p):
                targets.append({"file": rel, "symbol": sym, "kind": kind})

    # 2) 读取契约字段
    contract_fields = _read_contract_fields(region_abs)

    # 3) 写场景 JSON（内容引用真实符号/字段）
    scene = {
        "name": name,
        "region": region,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "language_hint": "python" if has_py else "mixed",
        "code_files": code_files,
        "targets": targets,
        "contract_fields": contract_fields,
        "steps": (
            [{"action": "load_module", "module": t["file"]} for t in targets[:5]]
            + [{"action": "assert_symbol_exists", "target": t["symbol"], "file": t["file"]} for t in targets[:8]]
            + ([{"action": "assert_contract_fields", "fields": contract_fields}] if contract_fields else [])
        ),
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(scene, f, ensure_ascii=False, indent=2)

    created = [json_rel.replace("\\", "/")]

    # 4) 仅当含 .py 时，额外生成可运行测试骨架（与已接上的 playtest / unittest 衔接）
    if has_py:
        py_name = "test_" + (name[:-5] if name.endswith(".json") else name) + ".py"
        py_rel = os.path.join(region, "tests", py_name)
        py_path = _file(root, py_rel)
        if not os.path.exists(py_path):
            files_syms = {}
            for t in targets:
                files_syms.setdefault(t["file"], []).append(t["symbol"])
            file_entries = ",\n".join(
                f"        ({r!r}, {syms!r})" for r, syms in files_syms.items()
            ) or "        # 未检出可测符号"
            py_src = (
                "# 由 Agent 工作台 generate_test_scene 自动生成（基于真实代码符号）\n"
                "import importlib.util\nimport os\nimport sys\nimport unittest\n\n"
                "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
                "# (相对 code_root 的文件路径, 该文件内期望存在的符号列表)\n"
                "FILES = [\n" + file_entries + "\n]\n\n\n"
                "def _load(rel_path):\n"
                "    abs_path = os.path.join(ROOT, rel_path)\n"
                "    d = os.path.dirname(abs_path)\n"
                "    if d not in sys.path:\n"
                "        sys.path.insert(0, d)\n"
                "    if ROOT not in sys.path:\n"
                "        sys.path.insert(0, ROOT)\n"
                "    spec = importlib.util.spec_from_file_location(\n"
                "        \"dm_gen_test_\" + rel_path.replace(os.sep, \"_\").replace(\".\", \"_\"),\n"
                "        abs_path,\n"
                "    )\n"
                "    mod = importlib.util.module_from_spec(spec)\n"
                "    spec.loader.exec_module(mod)\n"
                "    return mod\n\n\n"
                "class TestGenerated(unittest.TestCase):\n"
                "    def test_symbols_present(self):\n"
                "        \"\"\"断言各待测文件确实导出了期望的符号（基于 AST 扫描结果）。\"\"\"\n"
                "        for rel_path, symbols in FILES:\n"
                "            with self.subTest(file=rel_path):\n"
                "                try:\n"
                "                    mod = _load(rel_path)\n"
                "                except Exception as e:  # noqa: BLE001\n"
                "                    self.fail(f\"加载模块失败 {rel_path}: {e}\")\n"
                "                for sym in symbols:\n"
                "                    self.assertTrue(\n"
                "                        hasattr(mod, sym), f\"{rel_path} 缺少符号 {sym}\"\n"
                "                    )\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    unittest.main()\n"
            )
            with open(py_path, "w", encoding="utf-8") as f:
                f.write(py_src)
            created.append(py_rel.replace("\\", "/"))

    return created

_AUTO_TOKENS = {"", "auto", "test", "tests", "pytest", "unittest"}


def _detect_test_runner(root_abs):
    """返回可用的测试运行命令（优先 pytest，否则 unittest discover）。无则 None。

    打包版（frozen）内没有独立 Python 解释器（sys.executable 是 DocMind.exe，
    onedir 不含 python.exe），`-m pytest/unittest` 只会再启动一次本程序而不是跑测试，
    因此 frozen 下直接返回 None，由调用方给出明确提示，避免挂起/弹浏览器/假绿灯。
    """
    if getattr(sys, "frozen", False):
        return None
    try:
        probe = subprocess.run([sys.executable, "-m", "pytest", "--version"],
                               cwd=root_abs, capture_output=True, text=True, timeout=20)
        if probe.returncode == 0:
            return f'"{sys.executable}" -m pytest -q'
    except Exception:  # noqa: BLE001
        pass
    # unittest 兜底：tests/ 子包优先，否则从根发现
    if os.path.isdir(os.path.join(root_abs, "tests")):
        return f'"{sys.executable}" -m unittest discover -s tests -t . -p "test_*.py"'
    return f'"{sys.executable}" -m unittest discover -s . -p "test_*.py"'


def _parse_test_summary(out):
    """从 pytest/unittest 输出里抓取通过/失败计数，便于工作台展示。"""
    import re as _re
    s = {}
    m = _re.search(r"(\d+)\s+passed", out)
    if m:
        s["passed"] = int(m.group(1))
    m = _re.search(r"(\d+)\s+failed", out)
    if m:
        s["failed"] = int(m.group(1))
    m = _re.search(r"Ran\s+(\d+)\s+tests?", out)
    if m:
        s["ran"] = int(m.group(1))
    return s


_PLAYTEST_ENGINE_ERROR_MARKERS = (
    "SCRIPT ERROR",
    "Parse Error",
    "Failed to load script",
)


def _playtest_engine_error(output: str, command: str) -> str:
    """Detect engine-level failures that can still exit with code 0.

    Godot's headless process commonly returns zero after printing a script parse
    error.  Treating only the process code as truth would let a broken project
    pass the Harness review gate, so only apply these markers to an engine-like
    command and return a bounded diagnostic for the caller.
    """
    command_lower = (command or "").lower()
    if not any(token in command_lower for token in ("godot", "project.godot")):
        return ""
    lines = [line.strip() for line in (output or "").splitlines()
             if any(marker.lower() in line.lower() for marker in _PLAYTEST_ENGINE_ERROR_MARKERS)]
    return "\n".join(lines[-8:])


def playtest(root, command, timeout=30):
    root_abs = _root(root)
    cmd = (command or "").strip()
    auto = cmd.lower() in _AUTO_TOKENS
    if auto:
        runner = _detect_test_runner(root_abs)
        if not runner:
            if getattr(sys, "frozen", False):
                return {"ok": False, "error": "分发版（DocMind.exe）内未内置 Python 解释器，无法在应用内运行 pytest/unittest；请在源码环境（.venv）中执行测试。"}
            return {"ok": False, "error": "未检测到测试框架（需 pytest 或 unittest 测试文件）。"}
        cmd = runner
    else:
        # 自定义命令复用 run_command 的结构化黑名单（首词集合 + 危险模式），
        # 避免本模块旧的几条子串规则被双空格/变形轻易绕过。
        try:
            from tools import _cmd_is_blocked
            blocked, why = _cmd_is_blocked(cmd)
        except Exception:  # noqa: BLE001
            blocked, why = False, ""
        if blocked:
            return {"ok": False, "error": f"命令被 Playtest 安全策略拦截（命中「{why}」）"}
    started = time.time()
    try:
        # Godot emits UTF-8 even when the Windows process locale is GBK.  An
        # implicit ``text=True`` decode therefore raises in subprocess's
        # reader thread and turns a valid playtest into a blank failure.
        p = subprocess.run(cmd, shell=True, cwd=root_abs, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=min(int(timeout), 120))
        out = ((p.stdout or "") + (p.stderr or ""))[-4000:]
        engine_error = _playtest_engine_error(out, cmd)
        return {"ok": p.returncode == 0 and not engine_error, "code": p.returncode,
                "duration": round(time.time() - started, 2), "output": out, **_parse_test_summary(out)}
    except subprocess.TimeoutExpired as e:
        return {"ok": False, "error": "timeout", "output": str(e)}


def performance_sample(root, command):
    root_abs = _root(root)
    cmd = (command or "").strip()
    # 对 python 脚本做真实剖析（cProfile），其余仅测墙钟。
    # frozen 下 sys.executable 是 DocMind.exe 而非 python，无法 cProfile，
    # 直接返回明确错误，避免再启动一个应用实例（弹浏览器/挂起 120s）。
    is_python_cmd = cmd and (cmd.lower().endswith(".py") or cmd.lower().startswith("python "))
    if is_python_cmd and getattr(sys, "frozen", False):
        return {"ok": False, "metric": "cProfile(top15 by cumulative)",
                "error": "分发版（DocMind.exe）未内置 Python 解释器，cProfile 剖析仅源码环境可用；可改为在源码环境运行。"}
    if is_python_cmd:
        prof = f'"{sys.executable}" -m cProfile -s cumulative {cmd}'
        started = time.time()
        try:
            p = subprocess.run(prof, shell=True, cwd=root_abs, capture_output=True, text=True, timeout=120)
            out = (p.stdout + p.stderr)
            top = "\n".join(out.splitlines()[-15:])
            return {"ok": p.returncode == 0, "metric": "cProfile(top15 by cumulative)",
                    "duration": round(time.time() - started, 2), "output": top[-4000:]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
    r = playtest(root, cmd, 30)
    r["metric"] = "wall_time_seconds"
    return r

# ---------------------------------------------------------------------------
# 审批门禁（approval gate）
# 把「审批」从“只记一条日志”升级为真正的服务端门禁：敏感操作（提交/回滚/应用分区方案）
# 执行前必须存在一条「在有效期内、action+target 匹配」的 approved 记录，否则被拦截。
# 这样既约束 API 端点，也约束 Agent 的 dev_* 工具（它们最终都走 regions.py 底层函数）。
# ---------------------------------------------------------------------------
APPROVAL_TTL_SECONDS = 1800  # 审批有效期 30 分钟


def approval_ledger_path(root):
    return _project_state.path(root, 'approvals.jsonl', legacy='.docmind_approvals.jsonl')


def list_approval_records(root):
    """读取项目审批台账，供开发舱展示已处理与待处理请求。"""
    path = approval_ledger_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            return [row for line in handle if line.strip()
                    if isinstance((row := json.loads(line)), dict)]
    except (OSError, ValueError):
        return []


def approval(root, action, user, approved=False, target=""):
    """记录一条审批（追加到 .docmind_approvals.jsonl）。target 用于把审批绑定到具体对象。"""
    path = approval_ledger_path(root)
    row = {
        "action": action,
        "target": (target or "").strip(),
        "user": user,
        "approved": bool(approved),
        "time": datetime.now().isoformat(timespec="seconds"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def is_approved(root, action, target, ttl=APPROVAL_TTL_SECONDS):
    """是否存在一条对 (action, target) 有效的 approved 记录（默认 30 分钟内）。"""
    path = approval_ledger_path(root)
    if not os.path.isfile(path):
        return False
    target = (target or "").strip()
    now = time.time()
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except Exception:  # noqa: BLE001
        return False
    for r in rows:
        if not r.get("approved"):
            continue
        if r.get("action") != action:
            continue
        if (r.get("target") or "").strip() != target:
            continue
        ts = r.get("time")
        if not ts:
            return True  # 无时间戳的旧记录视为永久有效（兼容历史数据）
        try:
            then = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            return True  # 时间解析失败也放行，避免历史记录把操作永久卡死
        if now - then <= ttl:
            return True
    return False


def approval_status(root, action, target):
    """查询某操作当前是否已通过审批（供前端/Agent 判断是否需先审批）。"""
    return {
        "action": action,
        "target": (target or "").strip(),
        "approved": is_approved(root, action, target),
        "ttl_seconds": APPROVAL_TTL_SECONDS,
    }


def require_approval(root, action, target):
    """门禁检查：已审批返回 None；未审批返回阻断结构，由敏感操作原样向上返回。

    阻断结构含 blocked / approval_required / action / target / message，
    让 API 端点与 Agent 都能明确识别「需要先审批」。
    """
    if is_approved(root, action, target):
        return None
    try:
        from agent_runtime.cockpit_policy import record_gate_request
        record_gate_request(root, action, target)
    except Exception:
        # 审核队列只是可视化；记录失败也绝不能让审批门放行。
        pass
    return {
        "blocked": True,
        "approval_required": True,
        "action": action,
        "target": (target or "").strip(),
        "message": (
            f"操作 {action}(target={target or '*'}) 需要先审批。"
            f"请先调用审批（action={action}, target={target or '*'}, approved=true），"
            f"审批通过后 {APPROVAL_TTL_SECONDS // 60} 分钟内该操作放行。"
        ),
    }


# ---------------------------------------------------------------------------
# MCP 待装配提案暂存（pending connector proposal store）
#
# 死循环的根因：Agent 走 dev_mcp_add 触发 mcp_server 审批门时，队列里的 mcp_server
# 项被 cockpit_policy 归为 SETTINGS_ONLY——它的「批准」按钮打到 approval-requests/
# decide 会被硬拒（"只能在设置页确认"），而唯一能写 hash 匹配审批的 POST /api/mcp/
# servers 又需要用户手动复现逐字相同的 command/args。提案内容此前没被任何地方留住
# （record_gate_request 刻意不落参数），于是用户没有一键确认的入口，dev_mcp_add
# 只能永远 blocked。
#
# 这里把「用户要批准的那份连接配置」按 target 暂存一份，供连接器设置页渲染成一张
# 「待装配连接器」确认卡片、用完全相同的参数调 POST /api/mcp/servers 落盘。
#
# 安全边界：这不是审批本身（放行仍**只**认 approvals.jsonl 里 is_approved 的 approved
# 记录，本 store 不授予任何权限）。它与 mcp_client 落盘 servers 配置同处项目状态目录、
# 同一台机器同一用户的信任边界（servers 配置本就持久化了 command/args/url），因此不
# 新增泄密面。随审批 TTL 过期，落盘成功后由消费方移除。
# ---------------------------------------------------------------------------
def mcp_proposal_path(root):
    return _project_state.path(root, 'mcp_proposals.json',
                               legacy='.docmind_mcp_proposals.json')


def record_mcp_proposal(root, target, key, cfg):
    """按审批 target 暂存一份待装配连接配置（{key, config}）。幂等覆盖。"""
    target = str(target or "").strip()
    if not target:
        return
    path = mcp_proposal_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            store = json.load(handle)
        if not isinstance(store, dict):
            store = {}
    except (OSError, ValueError):
        store = {}
    store[target] = {
        "key": str(key or "").strip(),
        "config": dict(cfg or {}),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "created_at_epoch": time.time(),
    }
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def list_mcp_proposals(root, ttl=APPROVAL_TTL_SECONDS):
    """返回未过期、且尚未被审批放行的待装配提案（供设置页展示确认卡片）。"""
    path = mcp_proposal_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            store = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(store, dict):
        return []
    now = time.time()
    out = []
    for target, row in list(store.items()):
        if not isinstance(row, dict):
            continue
        try:
            age = now - float(row.get("created_at_epoch") or 0)
        except (TypeError, ValueError):
            age = ttl + 1
        if age > ttl:
            continue
        # 已有匹配 approved 记录 -> 不必再展示（放行了）
        if is_approved(root, "mcp_server", target, ttl=ttl):
            continue
        out.append({
            "target": target,
            "key": row.get("key"),
            "config": row.get("config") or {},
            "created_at": row.get("created_at"),
        })
    return out


def remove_mcp_proposal(root, target):
    """落盘/放行后移除提案；任何异常都不抛（清理失败不影响主流程）。"""
    target = str(target or "").strip()
    path = mcp_proposal_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            store = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(store, dict) or target not in store:
        return
    store.pop(target, None)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 自检沙箱租约（playtest sandbox lease）
#
# 问题：自检循环里每点一下游戏画面都要人去审核队列批一次，人成了瓶颈，
# 而"批准"本身也被稀释成机械点击。这里把授权从"每个动作批一次"改成
# "进沙箱前批一次边界"：
#
#   * 只覆盖**服务端自己解析出来的本项目窗口**（embedded / engine）——
#     target=foreground（任意前台窗口）永远不在边界内；
#   * 只覆盖鼠标/键盘输入，不授予任何工具能力，尤其不授予终端/EXEC；
#   * Win/Meta 等系统键照旧禁止（即便将来 _VK 表扩容也要在这里拦）；
#   * 有 TTL，过期即失效；工作流结束/中断时主动收回；
#   * 租约绑定项目根目录与边界摘要：换项目、改边界、文件被篡改一律 fail-closed。
#
# 它**不替代**审批台账：单次动作审批仍然有效，沙箱只是其中一类动作的
# 前置批量授权。两层都查，任一通过即可（沙箱被收回后立刻退回逐次审批）。
# ---------------------------------------------------------------------------
PLAYTEST_SANDBOX_TTL_SECONDS = 1800  # 30 分钟，与审批台账同量级
PLAYTEST_SANDBOX_SCOPE_VERSION = 1
_PLAYTEST_SANDBOX_ACTIONS = ("click", "type", "drag", "key", "save")
_PLAYTEST_SANDBOX_TARGETS = ("embedded", "engine")
#: 这些键名一旦出现在组合键里就整条拒绝。宁可让模型换一种输入方式，
#: 也不让沙箱变成"能按 Win+R"的后门。
_PLAYTEST_SANDBOX_DENIED_KEY_PARTS = frozenset({
    "win", "windows", "lwin", "rwin", "meta", "super", "cmd", "command", "os",
})


def _sandbox_root_key(root) -> str:
    return os.path.normcase(os.path.abspath(str(root or "")))


def playtest_sandbox_scope(root) -> dict:
    """当前项目允许自检沙箱覆盖的边界（可安全展示给用户/前端）。"""
    return {
        "version": PLAYTEST_SANDBOX_SCOPE_VERSION,
        "root": _sandbox_root_key(root),
        "actions": list(_PLAYTEST_SANDBOX_ACTIONS),
        "targets": list(_PLAYTEST_SANDBOX_TARGETS),
        "denied_targets": ["foreground"],
        "denied_keys": sorted(_PLAYTEST_SANDBOX_DENIED_KEY_PARTS),
        "capabilities": ["read_local", "write_local"],
        "denied_capabilities": ["exec", "admin", "network"],
        "note": "仅本项目引擎/嵌入窗口的鼠标键盘输入；终端、任意前台窗口与 Win 键仍禁用。",
    }


def _playtest_sandbox_digest(scope: dict) -> str:
    raw = json.dumps(scope, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def playtest_sandbox_path(root) -> str:
    return _project_state.path(root, "playtest_sandbox.json",
                               legacy=".docmind_playtest_sandbox.json")


def _read_playtest_sandbox(root) -> dict:
    """读取租约；任何异常都当成"没有租约"（fail-closed）。"""
    try:
        with open(playtest_sandbox_path(root), encoding="utf-8") as handle:
            row = json.load(handle)
        return row if isinstance(row, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_playtest_sandbox(root, lease: dict) -> None:
    path = playtest_sandbox_path(root)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(lease, handle, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def playtest_sandbox_status(root, now: float | None = None) -> dict:
    """租约是否有效。返回 active/reason/lease/scope，供动作门与前端查询。"""
    scope = playtest_sandbox_scope(root)
    lease = _read_playtest_sandbox(root)
    moment = float(now if now is not None else time.time())
    result = {"active": False, "reason": "", "lease": lease, "scope": scope,
              "ttl_seconds": PLAYTEST_SANDBOX_TTL_SECONDS}
    if not lease:
        result["reason"] = "尚未开启自检沙箱。"
        return result
    if str(lease.get("status") or "") != "active":
        result["reason"] = "自检沙箱租约已收回。"
        return result
    if _sandbox_root_key(lease.get("root")) != scope["root"]:
        result["reason"] = "自检沙箱租约属于另一个项目，已忽略。"
        return result
    if str(lease.get("scope_digest") or "") != _playtest_sandbox_digest(scope):
        result["reason"] = "自检沙箱边界已变化，旧租约失效。"
        return result
    try:
        expires = float(lease.get("expires_at_epoch") or 0)
    except (TypeError, ValueError):
        expires = 0.0
    if expires <= moment:
        result["reason"] = "自检沙箱租约已过期（%s），需重新审批。" % (
            lease.get("expires_at") or "未知时间")
        return result
    result["active"] = True
    result["remaining_seconds"] = int(max(0, expires - moment))
    return result


def grant_playtest_sandbox(root, user: str, *, ttl_seconds=None,
                           workflow_id: str = "") -> dict:
    """在工作流计划获批时开启一次自检沙箱（同一边界、同一项目）。"""
    if not root:
        raise ValueError("缺少项目根目录，无法开启自检沙箱。")
    scope = playtest_sandbox_scope(root)
    try:
        ttl = float(ttl_seconds if ttl_seconds is not None
                    else PLAYTEST_SANDBOX_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = float(PLAYTEST_SANDBOX_TTL_SECONDS)
    ttl = max(60.0, min(float(PLAYTEST_SANDBOX_TTL_SECONDS), ttl))
    now = time.time()
    lease = {
        "id": "sandbox-" + uuid.uuid4().hex[:12],
        "status": "active",
        "root": scope["root"],
        "scope": scope,
        "scope_digest": _playtest_sandbox_digest(scope),
        "granted_by": str(user or "")[:120],
        "workflow_id": str(workflow_id or "")[:120],
        "granted_at": datetime.now().isoformat(timespec="seconds"),
        "granted_at_epoch": now,
        "expires_at": datetime.fromtimestamp(now + ttl).isoformat(timespec="seconds"),
        "expires_at_epoch": now + ttl,
        "ttl_seconds": ttl,
        "released_at": "",
        "release_reason": "",
    }
    _write_playtest_sandbox(root, lease)
    return lease


def revoke_playtest_sandbox(root, reason: str = "") -> dict:
    """主动收回租约（幂等：已收回/不存在时原样返回）。"""
    lease = _read_playtest_sandbox(root)
    if not lease or str(lease.get("status") or "") != "active":
        return lease
    lease["status"] = "released"
    lease["released_at"] = datetime.now().isoformat(timespec="seconds")
    lease["release_reason"] = str(reason or "")[:200]
    _write_playtest_sandbox(root, lease)
    return lease


def playtest_sandbox_desktop_allowed(root, *, action: str, target: str,
                                     key: str = "") -> tuple[bool, str, dict]:
    """沙箱是否覆盖这次桌面动作 -> (allowed, reason, lease)。

    只判"沙箱边界"，不判断窗口是否存在；窗口仍由 desktop_actions 按
    root/pid 在服务端解析，调用方没有任何机会指定 HWND。
    """
    status = playtest_sandbox_status(root)
    if not status.get("active"):
        return False, str(status.get("reason") or "自检沙箱未开启。"), {}
    lease = dict(status.get("lease") or {})
    action = str(action or "").strip().lower()
    target = str(target or "").strip().lower()
    if action not in _PLAYTEST_SANDBOX_ACTIONS:
        return False, "自检沙箱不覆盖动作 %s。" % (action or "空"), lease
    if target not in _PLAYTEST_SANDBOX_TARGETS:
        return (False,
                "自检沙箱只覆盖本项目自己的窗口（embedded/engine），"
                "不覆盖 %s。" % (target or "空"), lease)
    parts = [part.strip().lower() for part in str(key or "").split("+") if part.strip()]
    denied = [part for part in parts if part in _PLAYTEST_SANDBOX_DENIED_KEY_PARTS]
    if denied:
        return (False, "自检沙箱不允许系统键：%s。" % "、".join(sorted(denied)),
                lease)
    return True, "", lease

