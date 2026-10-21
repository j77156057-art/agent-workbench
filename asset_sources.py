# -*- coding: utf-8 -*-
"""素材中心后端（阶段 2）。

两类真实来源（素材均为 CC0、免登录、免 Key）：
- polyhaven：Poly Haven 实时 API，单件 3D 模型 / PBR 贴图 / HDRI；
- kenney：Kenney 精选游戏素材包，抓官网资产页解析 ZIP 直链，下载解压后按目录组挑选入库。

所有出站请求统一带 User-Agent、超时与大小上限；入库素材登记到
项目台账 `.docmind/assets.json`（含来源/许可/作者/sha256）。
ComfyUI 生成物（assets/generated/*.json sidecar）在素材库中自动聚合展示。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import ntpath
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime

UA = 'DocMindAssetCenter/1.0 (+https://github.com/j77156057-art/agent-workbench)'

POLY_API = 'https://api.polyhaven.com'
KENNEY_BASE = 'https://kenney.nl'

ITEM_MAX_BYTES = 200 * 1024 * 1024      # 单件素材下载上限
PACK_MAX_BYTES = 500 * 1024 * 1024      # Kenney 整包下载上限
PROXY_MAX_BYTES = 25 * 1024 * 1024      # 缩略图/预览代理上限
EXTRACT_TOTAL_MAX = 2 * 1024 * 1024 * 1024
EXTRACT_FILES_MAX = 20000
PACK_CACHE_KEEP = 3                     # LRU 保留最近解压的包数

LEDGER_REL = os.path.join('.docmind', 'assets.json')
GENERATED_DIR = os.path.join('assets', 'generated')
# 帧动画清单（阶段 5）：与散帧/图集同目录，素材库据此把一个动画聚合成单个条目。
ANIM_SUFFIX = '.anim.json'

# 出站白名单（host 后缀匹配）
HOST_ALLOWLIST = ('api.polyhaven.com', 'cdn.polyhaven.com', 'dl.polyhaven.org', 'kenney.nl')

# 允许入库 / 预览 / 列出的扩展名（全小写，含点）
MEDIA_EXTS = {
    '.glb', '.gltf', '.fbx', '.obj',                       # 3D
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.tga', '.bmp',  # 图片/2D
    '.hdr', '.exr',                                        # HDRI / PBR
    '.wav', '.ogg', '.mp3',                                # 音频
    '.bin',                                                # glTF 同目录依赖
}
TEXT_EXTS = {'.txt', '.md'}
LIST_EXTS = MEDIA_EXTS | TEXT_EXTS

_KIND_BY_EXT = {
    **{e: 'model' for e in ('.glb', '.gltf', '.fbx', '.obj')},
    **{e: 'image' for e in ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.svg', '.tga', '.bmp')},
    **{e: 'hdri' for e in ('.hdr', '.exr')},
    **{e: 'audio' for e in ('.wav', '.ogg', '.mp3')},
}

# Kenney 精选包（slug 即 kenney.nl/assets/<slug>，均已核实官网页面含 ZIP 直链，2026-09）。
KENNEY_PACKS = [
    {'slug': 'prototype-kit', 'name': '原型套件', 'kinds': ['model'],
     'summary': '灰盒原型用几何体模块，搭关卡最快的一套'},
    {'slug': 'city-kit-suburban', 'name': '城市套件 · 郊区', 'kinds': ['model'],
     'summary': '郊区住宅、街道与城市道具模块'},
    {'slug': 'city-kit-industrial', 'name': '城市套件 · 工业区', 'kinds': ['model'],
     'summary': '厂房、储罐、管道与工业设施模块'},
    {'slug': 'nature-kit', 'name': '自然套件', 'kinds': ['model'],
     'summary': '树木、岩石、灌木等自然道具'},
    {'slug': 'car-kit', 'name': '载具套件', 'kinds': ['model'],
     'summary': '轿车、卡车等低多边形载具'},
    {'slug': 'blaster-kit', 'name': '爆能枪套件', 'kinds': ['model'],
     'summary': '科幻枪械、能量武器低多边形模型'},
    {'slug': 'platformer-kit', 'name': '平台跳跃套件', 'kinds': ['model'],
     'summary': '横版平台游戏用角色、地块与道具'},
    {'slug': 'modular-space-kit', 'name': '模块化太空套件', 'kinds': ['model'],
     'summary': '太空舱内壁、管道、面板等可拼模块'},
    {'slug': 'mini-dungeon', 'name': '迷你地下城', 'kinds': ['model'],
     'summary': '地牢墙地砖、道具、门与宝箱模块'},
    {'slug': 'prototype-textures', 'name': '原型纹理集', 'kinds': ['texture'],
     'summary': '网格/棋盘格原型贴图，快速拼出灰盒关卡'},
    {'slug': 'road-textures', 'name': '道路纹理集', 'kinds': ['texture'],
     'summary': '道路、路面标线等 PBR 风格贴图'},
    {'slug': 'skyboxes', 'name': '天空盒', 'kinds': ['texture'],
     'summary': '全景天空盒贴图，快速搭建场景氛围'},
    {'slug': 'ui-pack', 'name': 'UI 套件', 'kinds': ['2d'],
     'summary': '按钮、面板、图标等通用界面素材'},
    {'slug': 'ui-pack-pixel-adventure', 'name': '像素冒险 UI 套件', 'kinds': ['2d'],
     'summary': '像素风 RPG/冒险游戏界面元素'},
    {'slug': 'shape-characters', 'name': '形状小人', 'kinds': ['2d'],
     'summary': '可拼脸的几何小人 2D 精灵'},
    {'slug': 'interface-sounds', 'name': '界面音效', 'kinds': ['audio'],
     'summary': '点击、切换、提示等界面短音效'},
]

EXTERNAL_SOURCES = [
    {'key': 'quaternius', 'name': 'Quaternius（官网）', 'url': 'https://quaternius.com/'},
    {'key': 'kaykit', 'name': 'KayKit（itch.io）', 'url': 'https://kaylousberg.itch.io/'},
    {'key': 'poly-pizza', 'name': 'Poly Pizza（需免费 Key）', 'url': 'https://poly.pizza/'},
]


class AssetError(Exception):
    """可直接展示给用户的素材操作错误（消息为中文）。"""


# ================================================================ 路径安全

def root_path(root: str) -> str:
    base = os.path.abspath(root or '')
    if not base:
        raise AssetError('未配置项目目录。')
    return base


def safe_join(root: str, rel: str) -> str:
    """把项目内相对路径解析为绝对路径，拒绝越出项目根。"""
    base = root_path(root)
    raw_rel = (rel or '').replace('\\', '/')
    # 无论当前 runner 是 Windows 还是 POSIX，都拒绝 Windows 驱动器/UNC 绝对路径。
    # 否则 `C:/Windows/x` 在 Linux 会被当作普通相对目录，跨平台安全测试失真。
    if ntpath.isabs(raw_rel) or re.match(r'^[A-Za-z]:', raw_rel):
        raise AssetError('目标路径越出了项目目录。')
    rel = raw_rel.strip('/')
    p = os.path.abspath(os.path.join(base, rel))
    if not (p == base or p.startswith(base + os.sep)):
        raise AssetError('目标路径越出了项目目录。')
    return p


_SAFE_NAME = re.compile(r'^[A-Za-z0-9._\-（）()一-鿿 ]+$')


def safe_name(name: str, default: str = 'asset') -> str:
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    if not name or name in ('.', '..'):
        return default
    # 只保留白名单字符，其余替换为下划线，防止任何控制/路径字符
    name = re.sub(r'[^A-Za-z0-9._\-（）()一-鿿 ]', '_', name)
    name = re.sub(r'_+', '_', name).strip('._ ') or default
    return name[:120]


def kind_of(filename: str) -> str:
    return _KIND_BY_EXT.get(os.path.splitext(filename)[1].lower(), 'other')


_MIME_OVERRIDES = {
    '.glb': 'model/gltf-binary', '.gltf': 'model/gltf+json',
    '.hdr': 'application/octet-stream', '.webp': 'image/webp',
    '.ogg': 'audio/ogg', '.wav': 'audio/wav',
}


def guess_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return mimetypes.guess_type(path)[0] or 'application/octet-stream'


# ================================================================ HTTP 工具

def _request(url: str, accept: str = '*/*') -> urllib.request.Request:
    return urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': accept})


def http_json(url: str, timeout: float = 15.0):
    try:
        with urllib.request.urlopen(_request(url, 'application/json'), timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except AssetError:
        raise
    except Exception as e:
        raise AssetError(f'素材来源请求失败：{_friendly_net_error(e)}')


def http_text(url: str, timeout: float = 15.0) -> str:
    try:
        with urllib.request.urlopen(_request(url, 'text/html,*/*'), timeout=timeout) as r:
            charset = r.headers.get_content_charset() or 'utf-8'
            return r.read().decode(charset, errors='replace')
    except AssetError:
        raise
    except Exception as e:
        raise AssetError(f'素材来源页面请求失败：{_friendly_net_error(e)}')


def _friendly_net_error(e: Exception) -> str:
    msg = str(e)
    low = msg.lower()
    if 'timed out' in low or 'timeout' in low:
        return '连接超时（可能是网络无法访问该素材站，可稍后重试）'
    if '404' in msg:
        return '素材站返回 404（链接可能已变更）'
    if '403' in msg or '401' in msg:
        return '素材站拒绝访问（403/401）'
    if 'getaddrinfo' in low or 'name or service' in low:
        return '域名解析失败（网络可能不可达）'
    return msg or e.__class__.__name__


def _assert_allowed_host(url: str):
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    if not any(host == h or host.endswith('.' + h) for h in HOST_ALLOWLIST):
        raise AssetError('该下载地址不在受信任的素材站域名白名单内。')


def download_to_file(url: str, dest: str, limit: int, timeout: float = 600.0) -> int:
    """流式下载到 dest，超 limit 立即中止并删除残文件；返回字节数。"""
    _assert_allowed_host(url)
    total = 0
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as r, open(dest, 'wb') as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise AssetError(f'素材超过 {limit // (1024 * 1024)}MB 上限，已取消下载。')
                f.write(chunk)
    except AssetError:
        _quiet_remove(dest)
        raise
    except Exception as e:
        _quiet_remove(dest)
        raise AssetError(f'素材下载失败：{_friendly_net_error(e)}')
    return total


def proxy_fetch(url: str, limit: int = PROXY_MAX_BYTES, timeout: float = 30.0):
    """出站白名单代理（缩略图/glb 预览）：返回 (content_bytes, content_type)。"""
    _assert_allowed_host(url)
    try:
        with urllib.request.urlopen(_request(url), timeout=timeout) as r:
            data = r.read(limit + 1)
            ctype = r.headers.get('Content-Type') or 'application/octet-stream'
    except Exception as e:
        raise AssetError(_friendly_net_error(e))
    if len(data) > limit:
        raise AssetError('预览文件超过 25MB 代理上限。')
    return data, ctype.split(';')[0].strip() or 'application/octet-stream'


def _quiet_remove(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


# ================================================================ Poly Haven

POLY_KIND_TO_TYPE = {'model': 'models', 'texture': 'textures', 'hdri': 'hdris'}
# type 字段：0=HDRI 1=Texture 2=Model
POLY_TYPE_CODE = {'hdri': 0, 'texture': 1, 'model': 2}
_SEARCH_PAGE_SIZE = 24


def sources_list():
    return {
        'ok': True,
        'sources': [
            {'key': 'polyhaven', 'name': 'Poly Haven', 'mode': 'remote',
             'kinds': ['model', 'texture', 'hdri'], 'license': 'CC0',
             'home': 'https://polyhaven.com/'},
            {'key': 'kenney', 'name': 'Kenney 素材包', 'mode': 'pack',
             'kinds': ['model', 'texture', '2d', 'audio'], 'license': 'CC0',
             'home': 'https://kenney.nl/assets'},
        ],
        'external': EXTERNAL_SOURCES,
    }


def _normalize_poly(item_id: str, d: dict, kind_hint: str) -> dict:
    code = d.get('type')
    kind = next((k for k, c in POLY_TYPE_CODE.items() if c == code), kind_hint)
    authors = d.get('authors') or {}
    return {
        'id': item_id,
        'source': 'polyhaven',
        'kind': kind,
        'name': d.get('name') or item_id,
        'author': ', '.join(authors.keys()) if isinstance(authors, dict) else '',
        'license': 'CC0',
        'page_url': f'https://polyhaven.com/a/{item_id}',
        'thumb_url': d.get('thumbnail_url') or '',
        'tags': [str(t) for t in (d.get('tags') or [])[:8]],
        'summary': (d.get('description') or '')[:300],
    }


def _match_item(item: dict, q: str) -> bool:
    if not q:
        return True
    ql = q.lower()
    hay = ' '.join([
        item.get('name', ''), item.get('summary', ''), item.get('author', ''),
        ' '.join(item.get('tags') or []), item.get('id', ''),
    ]).lower()
    # CJK 不拆词；英文按所有 token 都命中（AND）
    tokens = [t for t in re.split(r'\s+', ql) if t]
    return all(t in hay for t in tokens)


def poly_search(q: str, kind: str, page: int = 1) -> dict:
    type_ = POLY_KIND_TO_TYPE.get(kind)
    if not type_:
        raise AssetError('Poly Haven 支持的素材类型：3D 模型 / 贴图 / HDRI。')
    url = f'{POLY_API}/assets?type={type_}'
    if q:
        url += '&search=' + urllib.parse.quote(q)
    data = http_json(url)
    if not isinstance(data, dict):
        raise AssetError('素材来源返回格式异常。')
    items = [_normalize_poly(iid, d, kind) for iid, d in data.items() if isinstance(d, dict)]
    # API 的 search 已过滤；本地再做一次宽松匹配（CJK/多词）
    items = [it for it in items if _match_item(it, q)]
    items.sort(key=lambda it: it['name'].lower())
    page = max(1, int(page or 1))
    start = (page - 1) * _SEARCH_PAGE_SIZE
    return {'ok': True, 'source': 'polyhaven', 'page': page,
            'has_more': start + _SEARCH_PAGE_SIZE < len(items),
            'items': items[start:start + _SEARCH_PAGE_SIZE]}


def _flatten_options(node, path_parts: list[str], out: list[dict]):
    """递归把 Poly Haven /files 响应展平为下载候选（叶子节点含 url+size）。"""
    if isinstance(node, dict) and 'url' in node and isinstance(node['url'], str):
        ext = os.path.splitext(urllib.parse.urlparse(node['url']).path)[1].lower()
        out.append({
            'label': ' · '.join(p.replace('_', ' ') for p in path_parts) or ext,
            'ext': ext,
            'size': int(node.get('size') or 0),
            'url': node['url'],
            'md5': node.get('md5') or '',
        })
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten_options(v, path_parts + [str(k)], out)


def _res_score(label: str) -> int:
    lab = label.lower()
    if '1k' in lab:
        return 0
    if '2k' in lab:
        return 1
    if '4k' in lab:
        return 2
    return 3


def _option_rank(o: dict, kind: str) -> tuple:
    """推荐排序：模型 glb（自包含）优先、贴图取 Diffuse 2K jpg、HDRI 取 2K hdr。"""
    label = o['label'].lower()
    ext = o['ext']
    if kind == 'model':
        base = {'.glb': 0, '.fbx': 2, '.obj': 5, '.gltf': 8}.get(ext, 9)
        return base + _res_score(label), o['size']
    if kind == 'texture':
        channel = 0
        if any(w in label for w in ('diffuse', 'color', 'albedo')):
            channel = -10
        elif 'normal' in label:
            channel = -6
        elif any(w in label for w in ('rough', 'ao', 'arm', 'metal')):
            channel = -4
        fmt = 0 if ext in ('.jpg', '.jpeg') else 2
        # 2K 最实用（默认），1K 省空间次之，4K/8K 置后
        lab = label
        res = 0 if '2k' in lab else (1 if '1k' in lab else (2 if '4k' in lab else 3))
        return channel + fmt + res, o['size']
    # hdri：2K hdr 默认
    base = 0 if ext == '.hdr' else 3
    res = 0 if '2k' in label.lower() else (1 if '1k' in label.lower() else (2 if '4k' in label.lower() else 3))
    return base + res, o['size']


def poly_resolve(item_id: str, kind: str) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_\-]{1,120}', item_id or ''):
        raise AssetError('素材 ID 不合法。')
    data = http_json(f'{POLY_API}/files/{urllib.parse.quote(item_id)}')
    options: list[dict] = []
    _flatten_options(data, [], options)
    # 按素材类型过滤：模型只给主网格文件（自带贴图不单独导入），贴图只给位图通道
    allowed = {
        'model': {'.glb', '.fbx', '.obj', '.gltf'},
        'texture': {'.png', '.jpg', '.jpeg', '.webp', '.tga'},
        'hdri': {'.hdr', '.exr'},
    }.get(kind, set())
    options = [o for o in options if o['ext'] in allowed]
    if not options:
        raise AssetError('该素材没有可直接导入的文件格式。')
    options.sort(key=lambda o: _option_rank(o, kind))
    return {'ok': True, 'source': 'polyhaven', 'id': item_id, 'kind': kind, 'options': options}


# ================================================================ Kenney 素材包

def kenney_list(q: str = '', kinds: str = '') -> dict:
    packs = [dict(p, source='kenney', license='CC0',
                  page_url=f'{KENNEY_BASE}/assets/{p["slug"]}',
                  thumb_url='') for p in KENNEY_PACKS]
    ql = (q or '').strip().lower()
    kind_set = {k.strip() for k in (kinds or '').split(',') if k.strip()}
    if ql:
        packs = [p for p in packs
                 if ql in (p['name'] + p['summary'] + p['slug']).lower()]
    if kind_set:
        packs = [p for p in packs if kind_set.intersection(p.get('kinds') or [])]
    return {'ok': True, 'source': 'kenney', 'items': packs}


_ZIP_HREF_RE = re.compile(r'''href=(?:"|')(https?://kenney\.nl/media/[^"']+?\.zip(?:\?[^"']*)?)(?:"|')''', re.I)
_OG_IMAGE_RE = re.compile(
    r'''<meta[^>]+property=(?:"|')og:image(?:"|')[^>]+content=(?:"|')([^"']+)(?:"|')''', re.I)


def _parse_pack_page(html: str) -> tuple[str, str]:
    m = _ZIP_HREF_RE.search(html)
    if not m:
        raise AssetError('没有在素材包页面解析到下载链接（官网可能已改版），可到官网手动下载。')
    zip_url = m.group(1).replace('&amp;', '&')
    img = ''
    mi = _OG_IMAGE_RE.search(html)
    if mi:
        img = mi.group(1)
        if img.startswith('/'):
            img = KENNEY_BASE + img
    return zip_url, img


def _cache_root() -> str:
    d = os.path.join(tempfile.gettempdir(), 'docmind-assets')
    os.makedirs(d, exist_ok=True)
    return d


def _pack_dir(token: str) -> str:
    if not re.fullmatch(r'[0-9a-f]{8,64}', token or ''):
        raise AssetError('素材包缓存标识不合法。')
    return safe_join(_cache_root(), token)  # temp 目录充当 root 做穿越校验


def _read_pack_meta(d: str) -> dict | None:
    mp = os.path.join(d, '_pack.json')
    try:
        with open(mp, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _evict_pack_cache():
    root = _cache_root()
    entries = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        if not os.path.isdir(d):
            continue
        meta = _read_pack_meta(d)
        ts = (meta or {}).get('ts') or os.path.getmtime(d)
        entries.append((float(ts), d))
    entries.sort(reverse=True)
    for _, d in entries[PACK_CACHE_KEEP:]:
        shutil.rmtree(d, ignore_errors=True)


def _common_top_level(files: list[str]) -> str:
    """所有条目共享同一顶层目录时返回该前缀（带尾斜杠），否则空串。"""
    tops = {f.replace('\\', '/').split('/', 1)[0] for f in files if '/' in f.replace('\\', '/')}
    if len(tops) == 1:
        return next(iter(tops)) + '/'
    return ''


def _extract_zip(zip_path: str, dest_dir: str) -> dict:
    """安全解压：zip-slip/符号链接/总量与数量防护。返回文件清单（相对路径）。"""
    files: list[str] = []
    total = 0
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise AssetError('下载的素材包不是有效的 ZIP 文件。')
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > EXTRACT_FILES_MAX:
            raise AssetError('素材包内文件数量超过 2 万上限，已取消解压。')
        base = os.path.abspath(dest_dir)
        for info in infos:
            # 跳过类 Unix 符号链接条目（高 16 位文件类型 = 0120000）
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue
            rel = info.filename.replace('\\', '/')
            if not rel or rel.startswith('/') or re.match(r'^[A-Za-z]:', rel) or '..' in rel.split('/'):
                continue
            target = os.path.abspath(os.path.join(base, rel))
            if not (target == base or target.startswith(base + os.sep)):
                continue
            total += info.file_size
            if total > EXTRACT_TOTAL_MAX:
                raise AssetError('素材包解压后超过 2GB 上限，已取消。')
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, length=256 * 1024)
            files.append(rel)
    return {'files': files, 'prefix': _common_top_level(files)}


def _pack_file_list(dest_dir: str, prefix: str) -> list[dict]:
    out = []
    base = os.path.abspath(dest_dir)
    for dp, _, names in os.walk(base):
        for fn in sorted(names):
            if fn == '_pack.json':
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in LIST_EXTS:
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, base).replace('\\', '/')
            shown = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
            out.append({'path': rel, 'show_path': shown, 'ext': ext,
                        'kind': kind_of(fn), 'size': os.path.getsize(full),
                        'dep': ext == '.bin'})
    out.sort(key=lambda x: x['show_path'].lower())
    return out


def kenney_peek(slug: str) -> dict:
    pack = next((p for p in KENNEY_PACKS if p['slug'] == slug), None)
    if not pack:
        raise AssetError('未知的素材包。')
    page_url = f'{KENNEY_BASE}/assets/{slug}'
    html = http_text(page_url)
    zip_url, thumb = _parse_pack_page(html)
    token = hashlib.sha1(zip_url.encode('utf-8')).hexdigest()[:16]
    dest = _pack_dir(token)

    meta = _read_pack_meta(dest)
    if meta and os.path.isdir(dest):
        files = _pack_file_list(dest, meta.get('prefix') or '')
        meta['ts'] = datetime.now().timestamp()
        with open(os.path.join(dest, '_pack.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        return {'ok': True, 'token': token, 'name': pack['name'], 'thumb': meta.get('thumb') or thumb,
                'page_url': page_url, 'files': files, 'prefix': meta.get('prefix') or '',
                'cached': True}

    os.makedirs(dest, exist_ok=True)
    tmp_zip = os.path.join(_cache_root(), token + '.zip.part')
    size = download_to_file(zip_url, tmp_zip, PACK_MAX_BYTES)
    try:
        extracted = _extract_zip(tmp_zip, dest)
    except AssetError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        _quiet_remove(tmp_zip)
    meta = {'slug': slug, 'name': pack['name'], 'zip_url': zip_url, 'thumb': thumb,
            'page_url': page_url, 'size': size, 'prefix': extracted['prefix'],
            'ts': datetime.now().timestamp()}
    with open(os.path.join(dest, '_pack.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    _evict_pack_cache()
    return {'ok': True, 'token': token, 'name': pack['name'], 'thumb': thumb,
            'page_url': page_url, 'files': _pack_file_list(dest, extracted['prefix']),
            'prefix': extracted['prefix'], 'cached': False}


def kenney_preview_file(token: str, file: str):
    dest = _pack_dir(token)
    if not os.path.isdir(dest):
        raise AssetError('素材包缓存已过期，请重新打开素材包。')
    ext = os.path.splitext(file)[1].lower()
    if ext not in MEDIA_EXTS:
        raise AssetError('该文件类型不支持预览。')
    p = safe_join(dest, file)
    if not os.path.isfile(p):
        raise AssetError('包内文件不存在。')
    return p, guess_mime(p)


def _expand_gltf_deps(files: list[dict], selected: set[str]) -> set[str]:
    """选中 .gltf 时连带同目录子树下的全部媒体/依赖文件。"""
    by_path = {f['path']: f for f in files}
    extra: set[str] = set()
    for sel in list(selected):
        if by_path.get(sel, {}).get('ext') == '.gltf':
            prefix = sel.rsplit('/', 1)[0] + '/'
            for f in files:
                if f['path'].startswith(prefix) and f['ext'] in MEDIA_EXTS:
                    extra.add(f['path'])
    return selected | extra


def kenney_import(root: str, token: str, selected: list[str], dest_root: str) -> dict:
    dest = _pack_dir(token)
    meta = _read_pack_meta(dest)
    if not meta:
        raise AssetError('素材包缓存已过期，请重新打开素材包。')
    files = _pack_file_list(dest, meta.get('prefix') or '')
    chosen = {s for s in (selected or []) if isinstance(s, str)}
    chosen = _expand_gltf_deps(files, chosen)
    chosen = {p for p in chosen if p in {f['path'] for f in files}}
    if not chosen:
        raise AssetError('没有选择要导入的文件。')

    pack_dir_name = safe_name(meta.get('name') or meta.get('slug') or 'kenney-pack')
    imported, skipped = [], []
    ledger = _Ledger.load(root)
    for f in files:
        if f['path'] not in chosen:
            continue
        shown = f['show_path']
        rel_dir = os.path.dirname(shown)
        target_rel = '/'.join(x for x in [
            (dest_root or 'assets').strip('/'), pack_dir_name, rel_dir, safe_name(os.path.basename(shown))
        ] if x)
        src = safe_join(dest, f['path'])
        r = ledger.import_bytes(root, src, target_rel, kind_of(shown), source='kenney',
                                source_id=meta.get('slug') or '', source_url=meta.get('page_url') or '',
                                author='Kenney', license_='CC0', move=False)
        (imported if r.get('ok') else skipped).append(r)
    ledger.save(root)
    ok = len(skipped) == 0
    return {'ok': ok, 'imported': imported, 'skipped': skipped,
            'imported_count': len(imported), 'skipped_count': len(skipped)}


# ================================================================ 台账与入库

class _Ledger:
    def __init__(self, data: dict):
        self.data = data

    @classmethod
    def load(cls, root: str) -> '_Ledger':
        p = safe_join(root, LEDGER_REL)
        try:
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                raise ValueError
        except (OSError, ValueError):
            data = {'version': 1, 'items': []}
        return cls(data)

    def save(self, root: str):
        p = safe_join(root, LEDGER_REL)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    def find_sha(self, sha: str) -> str | None:
        for it in self.data['items']:
            if it.get('sha256') == sha:
                return it.get('path')
        return None

    def import_bytes(self, root: str, src_path: str, target_rel: str, kind: str, *,
                     source: str, source_id: str, source_url: str,
                     author: str, license_: str, move: bool = False) -> dict:
        """登记并落盘一个已在本地（src_path）的素材文件。"""
        target_rel = target_rel.replace('\\', '/').lstrip('./')
        ext = os.path.splitext(target_rel)[1].lower()
        if ext not in MEDIA_EXTS:
            return {'ok': False, 'path': target_rel, 'error': f'不支持导入 {ext} 类型文件。'}
        target = safe_join(root, target_rel)
        # sha + 台账去重
        sha = sha256_file(src_path)
        existing = self.find_sha(sha)
        if existing:
            return {'ok': False, 'path': existing, 'error': f'相同内容已在素材库：{existing}'}
        # 同名避让
        target = _unique_path(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if move:
            shutil.move(src_path, target)
        else:
            shutil.copy2(src_path, target)
        size = os.path.getsize(target)
        rel = os.path.relpath(target, root_path(root)).replace('\\', '/')
        rec = {
            'path': rel, 'kind': kind, 'source': source, 'source_id': str(source_id)[:160],
            'source_url': str(source_url)[:1000], 'author': str(author)[:200],
            'license': license_ or 'CC0', 'sha256': sha, 'size': size,
            'imported_at': datetime.now().isoformat(timespec='seconds'),
        }
        self.data['items'].append(rec)
        return {'ok': True, 'path': rel, 'kind': kind, 'size': size, 'sha256': sha,
                'license': rec['license'], 'author': rec['author']}


def _unique_path(p: str) -> str:
    if not os.path.exists(p):
        return p
    stem, ext = os.path.splitext(p)
    i = 1
    while os.path.exists(f'{stem}-{i}{ext}'):
        i += 1
    return f'{stem}-{i}{ext}'


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def import_item(root: str, *, source: str, item_id: str, option: dict, kind: str,
                dest_dir: str, author: str = '', source_url: str = '',
                license_: str = 'CC0') -> dict:
    """单件素材入库（Poly Haven 等远程单文件）。"""
    if source != 'polyhaven':
        raise AssetError('当前仅支持 Poly Haven 的单件导入。')
    url = (option or {}).get('url') or ''
    _assert_allowed_host(url)
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in MEDIA_EXTS:
        raise AssetError(f'不支持导入 {ext} 类型文件。')
    name = safe_name(f'{item_id}{ext}', default='asset')
    target_rel = '/'.join(x for x in [(dest_dir or 'assets').strip('/'), name] if x)
    target = safe_join(root, target_rel)
    target = _unique_path(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + '.download'
    download_to_file(url, tmp, ITEM_MAX_BYTES)
    ledger = _Ledger.load(root)
    try:
        r = ledger.import_bytes(root, tmp, os.path.relpath(target, root_path(root)).replace('\\', '/'),
                                kind, source=source, source_id=item_id,
                                source_url=source_url or url, author=author or 'Poly Haven',
                                license_=license_, move=True)
    except AssetError:
        _quiet_remove(tmp)
        raise
    if not r.get('ok'):
        _quiet_remove(tmp)
        return r
    ledger.save(root)
    return r


# ================================================================ 素材库浏览 / 原始流

def _read_sidecar(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _collect_generated(base: str, gen_root: str) -> list[dict]:
    """扫描 assets/generated：动画清单聚合成单个 animation 条目。

    一个 ``<名称>.anim.json`` 代表一个帧动画：素材库只展示一个卡片
    （缩略图用 SpriteSheet），其散帧目录与源视频被屏蔽，不单独刷屏。
    其余媒体文件沿用“同名 .json sidecar”规则。
    """
    media: list[tuple[str, str, str]] = []
    anim_manifests: list[tuple[str, str]] = []
    for dp, _, names in os.walk(gen_root):
        for fn in names:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, base).replace('\\', '/')
            if fn.endswith(ANIM_SUFFIX):
                anim_manifests.append((rel, full))
            elif os.path.splitext(fn)[1].lower() in MEDIA_EXTS:
                media.append((rel, full, fn))

    items: list[dict] = []
    shield_files: set[str] = set()
    shield_dirs: set[str] = set()
    for rel, full in anim_manifests:
        meta = _read_sidecar(full) or {}
        if not isinstance(meta, dict):
            continue
        dir_rel = os.path.dirname(rel)
        sheet = str(meta.get('sheet') or '')
        if not sheet:
            continue
        sheet_rel = f'{dir_rel}/{sheet}' if dir_rel else sheet
        sheet_full = os.path.join(base, *sheet_rel.split('/'))
        if not os.path.isfile(sheet_full):
            continue  # 清单存在但图集丢失，按损坏处理，不展示半截动画
        frames_dir = str(meta.get('frames_dir') or 'frames')
        fd_rel = f'{dir_rel}/{frames_dir}'.strip('/') if dir_rel else frames_dir
        shield_dirs.add(fd_rel)
        shield_files.add(sheet_rel)
        video = str(meta.get('video') or '')
        if video:
            shield_files.add(f'{dir_rel}/{video}' if dir_rel else video)
        prefix = str(meta.get('frame_prefix') or 'frame_')
        ext = str(meta.get('frame_ext') or '.png')
        first = f'{fd_rel}/{prefix}0001{ext}'
        items.append({
            'path': sheet_rel,
            'name': str(meta.get('name') or os.path.basename(dir_rel)),
            'kind': 'animation',
            'size': os.path.getsize(sheet_full),
            'mtime': int(os.path.getmtime(sheet_full)),
            'source': meta.get('source') or 'comfyui-h3',
            'author': meta.get('author') or '',
            'license': meta.get('license') or '',
            'imported_at': meta.get('imported_at') or meta.get('created_at') or '',
            # 前端播放动画所需的元数据
            'fps': meta.get('fps'), 'frame_count': meta.get('frame_count'),
            'cols': meta.get('cols'), 'rows': meta.get('rows'),
            'frame_width': meta.get('frame_width'), 'frame_height': meta.get('frame_height'),
            'frames_dir': fd_rel, 'first_frame': first,
            'prompt': meta.get('prompt') or '', 'manifest': os.path.basename(rel),
        })

    for rel, full, fn in media:
        if rel in shield_files:
            continue
        if any(rel == d or rel.startswith(d + '/') for d in shield_dirs):
            continue
        meta = _read_sidecar(full + '.json') or {}
        items.append({
            'path': rel, 'name': fn, 'kind': meta.get('asset_kind') or kind_of(fn),
            'size': os.path.getsize(full), 'mtime': int(os.path.getmtime(full)),
            'source': meta.get('source') or 'comfyui',
            'author': meta.get('author') or '', 'license': meta.get('license') or '',
            'imported_at': (meta.get('imported_at') or '') if isinstance(meta, dict) else '',
        })
    return items


def library(root: str) -> dict:
    base = root_path(root)
    ledger = _Ledger.load(root)
    items: list[dict] = []
    seen_paths: set[str] = set()

    # 1) 台账（以磁盘存在为准）
    for rec in ledger.data['items']:
        rel = rec.get('path')
        if not rel or rel in seen_paths:
            continue
        full = safe_join(base, rel)
        if not os.path.isfile(full):
            continue
        seen_paths.add(rel)
        items.append({
            'path': rel, 'name': os.path.basename(rel), 'kind': rec.get('kind') or kind_of(rel),
            'size': rec.get('size') or os.path.getsize(full),
            'mtime': int(os.path.getmtime(full)),
            'source': rec.get('source') or 'unknown',
            'author': rec.get('author') or '', 'license': rec.get('license') or '',
            'imported_at': rec.get('imported_at') or '',
        })

    # 2) assets/generated 下的 ComfyUI 产物（动画清单聚合；其余按 sidecar 元数据）
    gen_root = os.path.join(base, *GENERATED_DIR.split('/'))
    if os.path.isdir(gen_root):
        for it in _collect_generated(base, gen_root):
            if it['path'] in seen_paths:
                continue
            seen_paths.add(it['path'])
            items.append(it)

    # 3) 重复内容分组（素材库通常文件不多；大文件跳过，避免卡顿）
    sha_map: dict[str, list[str]] = {}
    for it in items:
        full = os.path.join(base, *it['path'].split('/'))
        try:
            if it['size'] <= 300 * 1024 * 1024:
                sha_map.setdefault(sha256_file(full), []).append(it['path'])
        except OSError:
            continue
    dup_paths = {p for paths in sha_map.values() if len(paths) > 1 for p in paths}
    for it in items:
        it['duplicate'] = it['path'] in dup_paths

    items.sort(key=lambda x: x['path'].lower())
    dirs: dict[str, list[dict]] = {}
    for it in items:
        d = os.path.dirname(it['path']) or '/'
        dirs.setdefault(d, []).append(it)
    return {'ok': True, 'total': len(items),
            'dirs': [{'dir': d, 'items': v} for d, v in sorted(dirs.items())]}


def raw_file(root: str, path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext not in MEDIA_EXTS:
        raise AssetError('该文件类型不允许通过素材接口读取。')
    p = safe_join(root, path)
    if not os.path.isfile(p):
        raise AssetError('素材文件不存在。')
    return p, guess_mime(p)
