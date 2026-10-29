"""本地 API 密钥加密存储：优先 Windows DPAPI，其他平台使用受保护密钥文件。

密钥属于「这台机器上的这个 Windows 用户」，不属于某个项目：首次启动、还没
添加任何项目时也必须能保存并在重启后恢复。因此 ``root`` 为空时读写
``STATE_ROOT/.docmind/secrets.json`` 这个全局密钥库；指定项目时仍按项目保存
（兼容既有安装与多项目各自的覆盖），调用方可在项目库没有时回落到全局库。
"""
import base64, json, os, tempfile, threading
import ctypes
from pathlib import Path
import config
import project_state
from file_locks import cross_process_lock

# 读-改-写必须串行：同一项目的多把 Key（LLM / 语音 / 搜索 / 预设）会从不同
# 请求线程写入，交错时后写的那次会把先写的键整块抹掉。
_WRITE_LOCK = threading.RLock()


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                     dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
try:
    from cryptography.fernet import Fernet
except Exception: Fernet = None

def _global_dir(): return Path(config.STATE_ROOT) / '.docmind'
def _secrets_path(root):
    if root: return Path(project_state.path(root, 'secrets.json', legacy='.docmind_secrets.json'))
    return _global_dir() / 'secrets.json'
def _key_path(root):
    if root: return Path(project_state.path(root, 'secret.key', legacy='.docmind_secret.key'))
    return _global_dir() / 'global_secret.key'
def _box(root):
    p=_key_path(root); p.parent.mkdir(parents=True,exist_ok=True)
    if not p.exists(): p.write_bytes(Fernet.generate_key() if Fernet else os.urandom(32));
    return Fernet(p.read_bytes()) if Fernet else None

def _dpapi_encrypt(value):
    if os.name != 'nt': return None
    try:
        class B(ctypes.Structure): _fields_=[('cbData',ctypes.c_uint32),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
        raw=value.encode(); src=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw); inp=B(len(raw),src); out=B()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inp),None,None,None,None,0,ctypes.byref(out)): return None
        data=ctypes.string_at(out.pbData,out.cbData); ctypes.windll.kernel32.LocalFree(out.pbData)
        return base64.b64encode(data).decode()
    except Exception: return None

def _dpapi_decrypt(token):
    if os.name != 'nt': return None
    try:
        class B(ctypes.Structure): _fields_=[('cbData',ctypes.c_uint32),('pbData',ctypes.POINTER(ctypes.c_ubyte))]
        raw=base64.b64decode(token); src=(ctypes.c_ubyte*len(raw)).from_buffer_copy(raw); inp=B(len(raw),src); out=B()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp),None,None,None,None,0,ctypes.byref(out)): return None
        data=ctypes.string_at(out.pbData,out.cbData); ctypes.windll.kernel32.LocalFree(out.pbData); return data.decode()
    except Exception: return None
def save(root, provider, value):
    if not value: return {'ok':False,'error':'空密钥不保存'}
    with _WRITE_LOCK:
        p=_secrets_path(root)
        # 两个服务实例共享同一密钥库时，进程内锁管不到对方；用 OS 文件锁串行化。
        with cross_process_lock(p):
            data={}
            if p.exists():
                try: data=json.loads(p.read_text(encoding='utf-8'))
                except Exception: data={}
            token=_dpapi_encrypt(value)
            if token: scheme='dpapi'
            else:
                box=_box(root); token=box.encrypt(value.encode()).decode() if box else base64.b64encode(value.encode()).decode(); scheme='fernet' if box else 'base64'
            data[str(provider)]={'ciphertext':token,'scheme':scheme}
            _atomic_write(p, json.dumps(data,ensure_ascii=False,indent=2))
            return {'ok':True,'provider':provider,'stored':True}
def load(root, provider):
    """Load a provider key, retaining compatibility with pre-project storage.

    A migrated ``secrets.json`` can outlive the legacy file.  If its DPAPI
    record cannot be opened (for example after switching from an old packaged
    launcher), try the original project-local file before giving up.  This is
    deliberately read-only: an explicit save is still required to rewrite a
    key with the current user's protection context.

    ``root`` 为空时只读全局密钥库（``STATE_ROOT/.docmind/secrets.json``）；
    没有项目不等于没有密钥。
    """
    candidates = []
    if root:
        try:
            candidates.append(_secrets_path(root))
        except Exception:
            pass
        legacy = Path(os.path.abspath(root)) / '.docmind_secrets.json'
        if legacy not in candidates:
            candidates.append(legacy)
    else:
        candidates.append(_secrets_path(None))

    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            item = data.get(str(provider), {})
            token = item.get('ciphertext', '')
            if not token:
                continue
            if item.get('scheme') == 'dpapi':
                value = _dpapi_decrypt(token) or ''
            else:
                box = _box(root)
                value = box.decrypt(token.encode()).decode() if box else base64.b64decode(token).decode()
            if value:
                return value
        except Exception:
            continue
    return ''

def providers(root):
    try: return sorted(json.loads(_secrets_path(root).read_text(encoding='utf-8')).keys())
    except Exception: return []

def remove(root, provider):
    with _WRITE_LOCK:
        p=_secrets_path(root)
        with cross_process_lock(p):
            try: data=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
            except Exception: data={}
            existed=str(provider) in data; data.pop(str(provider),None)
            _atomic_write(p, json.dumps(data,ensure_ascii=False,indent=2))
            return {'ok':True,'removed':existed,'provider':str(provider)}
