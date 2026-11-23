# -*- coding: utf-8 -*-
"""云端 AI 素材生成：外部联网生图 / 图生帧动画（用户自带 API Key）。

与本地 ComfyUI 链路（asset_gen.py）并列：
- 密钥不出本机：统一走 secrets_store（Windows DPAPI 加密落项目 .docmind 目录）；
- 服务商清单预置 base_url / 模型 / 申请密钥与文档链接，用户只做「申请 Key → 粘贴」；
- 生图结果落 assets/generated/images，图生视频结果下载后复用 asset_gen 的
  抽帧 / SpriteSheet / anim.json 管线落 assets/generated/animations，
  云端产物与本地产物在「我的素材库」里完全同构。

协议适配只实现有公开文档保证的形态（2026-09 核实）：
- siliconflow 生图：POST /v1/images/generations，返回 {"images":[{"url": ...}]}；
  视频：POST /v1/video/submit 得 requestId，POST /v1/video/status 轮询，
  Succeed 后 results.videos[0].url 下载（图生视频首帧走 image 字段）；
- openai 形态（智谱 bigmodel / OpenAI / 自定义兼容网关）：
  POST {base}/images/generations，返回 {"data":[{"url"|"b64_json"}]}。
"""
from __future__ import annotations

import base64
import binascii
import os
import threading
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import asset_gen
import asset_sources as assets
import secrets_store


if TYPE_CHECKING:  # pragma: no cover - 只给静态检查看名字
    import httpx


def __getattr__(name: str):
    """惰性暴露 `httpx`（PEP 562）—— 理由见 asset_gen.__getattr__。

    `httpx` 会连带拖起 click 与 rich（实测 327 毫秒），启动期用不上；
    真正要联网时第一次访问才真导入。测试用
    `mock.patch.object(cloud_gen.httpx, "post", ...)` 打桩，
    所以这个模块级名字必须仍然指向**真正的** httpx 模块。
    """
    if name == "httpx":
        import httpx as _httpx
        globals()["httpx"] = _httpx
        return _httpx
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


IMAGE_SUBDIR = asset_gen.IMAGE_SUBDIR
ANIM_SUBDIR = asset_gen.ANIM_SUBDIR
KEY_PREFIX = 'cloudgen:'
HTTP_TIMEOUT_IMAGE = 180.0
HTTP_TIMEOUT_VIDEO = 900.0
POLL_INTERVAL = 3.0
MAX_RESULT_BYTES = 600 * 1024 * 1024


class CloudError(Exception):
    """云端链路可展示给用户的错误（中文）。"""


# ================================================================ 服务商清单
# 每条模型：id（接口 model 字段）/ label / i2v（视频模型是否需要首帧）
PROVIDERS: list[dict] = [
    {
        'id': 'siliconflow',
        'name': '硅基流动 SiliconFlow（国内直连）',
        'base_url': 'https://api.siliconflow.cn/v1',
        'adapter': 'siliconflow',
        'key_url': 'https://cloud.siliconflow.cn/account/ak',
        'doc_url': 'https://docs.siliconflow.cn/cn/userguide/capabilities/images',
        'note': '注册即送额度；FLUX.1-schnell 免费出图；Wan 图生视频约 ¥1–2/条。',
        'image_models': [
            {'id': 'black-forest-labs/FLUX.1-schnell', 'label': 'FLUX.1 Schnell（免费 · 4 步快出图）'},
            {'id': 'black-forest-labs/FLUX.1-dev', 'label': 'FLUX.1 Dev（质量更好）'},
            {'id': 'Qwen/Qwen-Image', 'label': '通义 Qwen-Image（中文语义强）'},
            {'id': 'Kwai-Kolors/Kolors', 'label': '可图 Kolors'},
        ],
        'video_models': [
            {'id': 'Wan-AI/Wan2.2-I2V-A14B', 'label': 'Wan 2.2 图生视频（最新 · 稳）', 'i2v': True},
            {'id': 'Wan-AI/Wan2.1-I2V-14B-720P-Turbo', 'label': 'Wan 2.1 图生视频 Turbo（快 · 便宜）', 'i2v': True},
            {'id': 'Wan-AI/Wan2.1-I2V-14B-720P', 'label': 'Wan 2.1 图生视频 720P', 'i2v': True},
            {'id': 'Wan-AI/Wan2.2-T2V-A14B', 'label': 'Wan 2.2 文生视频（无需首帧）', 'i2v': False},
        ],
    },
    {
        'id': 'bigmodel',
        'name': '智谱 AI（CogView / GLM-Image）',
        'base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'adapter': 'openai',
        'key_url': 'https://bigmodel.cn/usercenter/proj-mgmt/apikeys',
        'doc_url': 'https://docs.bigmodel.cn/cn/guide/models/image-generation/cogview-4',
        'note': 'CogView-3-Flash 免费；CogView-4 ¥0.06/张，擅长中文与画面文字。',
        'image_models': [
            {'id': 'cogview-3-flash', 'label': 'CogView-3-Flash（免费）'},
            {'id': 'cogview-4', 'label': 'CogView-4（中文文字强）'},
            {'id': 'glm-image', 'label': 'GLM-Image（高清精细）'},
        ],
        'video_models': [],
    },
    {
        'id': 'openai',
        'name': 'OpenAI（gpt-image-1 / DALL·E 3，需可访问 OpenAI 的网络）',
        'base_url': 'https://api.openai.com/v1',
        'adapter': 'openai',
        'key_url': 'https://platform.openai.com/api-keys',
        'doc_url': 'https://platform.openai.com/docs/guides/image-generation',
        'note': '走官方接口；若网络无法直连，请改用下面的「OpenAI 兼容」并填中转地址。',
        'image_models': [
            {'id': 'gpt-image-1', 'label': 'gpt-image-1（指令遵循最强）'},
            {'id': 'dall-e-3', 'label': 'DALL·E 3'},
        ],
        'video_models': [],
    },
    {
        'id': 'custom',
        'name': 'OpenAI 兼容网关（自定义 Base URL / 模型）',
        'base_url': '',
        'adapter': 'openai',
        'key_url': '',
        'doc_url': '',
        'note': '任何兼容 POST {base}/images/generations 的服务商或中转站均可；模型名手填。',
        'image_models': [],
        'video_models': [],
    },
]


def public_providers() -> list[dict]:
    """返回前端可展示的服务商清单（不含任何密钥）。"""
    out = []
    for p in PROVIDERS:
        out.append({k: v for k, v in p.items() if k in (
            'id', 'name', 'base_url', 'adapter', 'key_url', 'doc_url', 'note',
            'image_models', 'video_models')})
    return out


def get_provider(pid: str) -> dict:
    for p in PROVIDERS:
        if p['id'] == pid:
            return p
    raise CloudError(f'未知服务商：{pid}')


# ================================================================ 密钥
def _secret_key(pid: str) -> str:
    return KEY_PREFIX + pid


def key_status(root: str) -> dict:
    """各服务商是否已存 Key（返回掩码预览，绝不回传明文）。"""
    saved = set(secrets_store.providers(root))
    out = {}
    for p in PROVIDERS:
        sk = _secret_key(p['id'])
        if sk in saved:
            plain = secrets_store.load(root, sk) or ''
            tail = plain[-4:] if len(plain) >= 4 else ''
            out[p['id']] = {'saved': True, 'mask': ('****' + tail) if tail else '已保存'}
        else:
            out[p['id']] = {'saved': False, 'mask': ''}
    return out


def save_key(root: str, pid: str, key: str) -> dict:
    get_provider(pid)
    key = (key or '').strip()
    if not key:
        raise CloudError('请粘贴 API Key 后再保存。')
    secrets_store.save(root, _secret_key(pid), key)
    return {'ok': True, 'provider': pid}


def delete_key(root: str, pid: str) -> dict:
    get_provider(pid)
    secrets_store.remove(root, _secret_key(pid))
    return {'ok': True, 'provider': pid}


def _resolve_key(root: str, pid: str, inline_key: str) -> str:
    k = (inline_key or '').strip() or secrets_store.load(root, _secret_key(pid))
    if not k:
        p = get_provider(pid)
        raise CloudError(f'还没有 {p["name"]} 的 API Key：先在下面填写并保存（点开「申请 Key」可注册）。')
    return k


def _resolve_base_url(p: dict, base_url: str) -> str:
    base = (base_url or '').strip() or p.get('base_url') or ''
    if not base:
        raise CloudError('请填写服务商的 Base URL（例如 https://api.example.com/v1）。')
    return base.rstrip('/')


# ================================================================ HTTP 原语
def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        raise CloudError(f'连不上服务商接口（网络/代理问题）：{e}') from e
    if r.status_code in (401, 403):
        raise CloudError('API Key 无效或无权限（401/403）：请检查 Key 是否正确、是否已开通该模型。')
    if r.status_code == 429:
        raise CloudError('服务商限流（429）：额度用尽或请求太频繁，请稍后再试或更换账号。')
    if r.status_code >= 400:
        raise CloudError(f'服务商返回错误 {r.status_code}：{_short(r.text)}')
    try:
        return r.json()
    except ValueError as e:
        raise CloudError(f'服务商返回了无法解析的内容：{_short(r.text)}') from e


def _short(text: str, limit: int = 300) -> str:
    text = (text or '').replace('\n', ' ').strip()
    return text[:limit] + ('…' if len(text) > limit else '')


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    # data:image/png;base64,xxxx
    try:
        _, _, rest = uri.partition(',')
        meta = uri.split(',', 1)[0]
        ext = '.png'
        if 'image/jpeg' in meta or 'image/jpg' in meta:
            ext = '.jpg'
        elif 'image/webp' in meta:
            ext = '.webp'
        return base64.b64decode(rest), ext
    except (ValueError, binascii.Error) as e:
        raise CloudError('无法解析服务商返回的 base64 图片。') from e


def _download(url: str) -> bytes:
    try:
        with httpx.stream('GET', url, timeout=180, follow_redirects=True) as r:
            if r.status_code >= 400:
                raise CloudError(f'下载生成产物失败 {r.status_code}。')
            buf = bytearray()
            for chunk in r.iter_bytes(1024 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_RESULT_BYTES:
                    raise CloudError('生成产物超过 600MB 上限。')
            return bytes(buf)
    except httpx.HTTPError as e:
        raise CloudError(f'下载生成产物失败：{e}') from e


def _guess_ext(url: str, default: str = '.png') -> str:
    path = urlparse(url).path.lower()
    for ext in ('.png', '.jpg', '.jpeg', '.webp'):
        if path.endswith(ext):
            return '.jpeg' if ext == '.jpeg' else ext
    return default


def _extract_images(data: dict) -> list[tuple[bytes, str]]:
    """从生图响应中取出全部图片（兼容 SF images[] 与 OpenAI data[]）。"""
    items: list[dict] = []
    if isinstance(data.get('images'), list):
        items = [x for x in data['images'] if isinstance(x, dict)]
    elif isinstance(data.get('data'), list):
        items = [x for x in data['data'] if isinstance(x, dict)]
    out: list[tuple[bytes, str]] = []
    for it in items:
        b64 = it.get('b64_json') or it.get('image')
        u = it.get('url') or ''
        if b64 and isinstance(b64, str):
            if b64.startswith('data:'):
                out.append(_decode_data_uri(b64))
            else:
                try:
                    out.append((base64.b64decode(b64), '.png'))
                except binascii.Error:
                    continue
        elif u.startswith('data:'):
            out.append(_decode_data_uri(u))
        elif u:
            out.append((_download(u), _guess_ext(u)))
    if not out:
        raise CloudError(f'服务商未返回图片，响应：{_short(str(data))}')
    return out


# ================================================================ 生图
def generate_image(*, root: str, pid: str, model: str, prompt: str,
                   width: int = 1024, height: int = 1024, negative_prompt: str = '',
                   seed: int | None = None, batch: int = 1,
                   inline_key: str = '', base_url: str = '') -> list[tuple[bytes, str]]:
    p = get_provider(pid)
    model = (model or '').strip()
    if not model:
        raise CloudError('请选择或填写云端模型。')
    key = _resolve_key(root, pid, inline_key)
    base = _resolve_base_url(p, base_url)
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    size = f'{int(width)}x{int(height)}'

    if p['adapter'] == 'siliconflow':
        payload = {
            'model': model, 'prompt': prompt, 'image_size': size,
            'batch_size': max(1, min(int(batch), 4)),
        }
        if negative_prompt.strip():
            payload['negative_prompt'] = negative_prompt.strip()
        if seed is not None:
            payload['seed'] = int(seed)
    else:
        # OpenAI 形态：gpt-image / dall-e / cogview 共用；各家忽略不认识的字段会报错，
        # 故只发通用字段。dall-e-3 支持 n，gpt-image-1 固定 1 张。
        payload = {'model': model, 'prompt': prompt, 'size': size}
        if model.startswith('dall-e'):
            payload['n'] = 1
            payload['quality'] = 'standard'
    data = _post_json(base + '/images/generations', headers, payload, HTTP_TIMEOUT_IMAGE)
    return _extract_images(data)


# ================================================================ 图生视频（硅基流动）
def _first_frame_data_uri(frame_bytes: bytes | None) -> str:
    if not frame_bytes:
        return ''
    return 'data:image/png;base64,' + base64.b64encode(frame_bytes).decode('ascii')


def generate_video(*, root: str, pid: str, model: str, prompt: str,
                   first_frame: bytes | None = None, duration: float = 5.0,
                   seed: int | None = None, inline_key: str = '', base_url: str = '',
                   is_canceled=None) -> tuple[bytes, str]:
    """提交云端视频并轮询到出片，返回 (mp4_bytes, ext)。仅 siliconflow 适配器。"""
    p = get_provider(pid)
    if p['adapter'] != 'siliconflow':
        raise CloudError('云端帧动画目前支持硅基流动 Wan 系列；该服务商暂无已适配的视频接口。')
    model = (model or '').strip()
    if not model:
        raise CloudError('请选择云端视频模型。')
    video_models = {m['id']: m for m in p.get('video_models', [])}
    m = video_models.get(model)
    if m and m.get('i2v') and not first_frame:
        raise CloudError('该模型是图生视频：请先选择首帧图片（素材库 / 上传 / 云端生图结果）。')
    key = _resolve_key(root, pid, inline_key)
    base = _resolve_base_url(p, base_url)
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}

    payload: dict = {'model': model, 'prompt': prompt}
    if first_frame:
        payload['image'] = _first_frame_data_uri(first_frame)
        payload['duration'] = max(1.0, min(float(duration), 10.0))
        payload['resolution'] = '720p'
    else:
        # 文生视频需要 image_size
        payload['image_size'] = '1280x720'
        payload['duration'] = max(1.0, min(float(duration), 10.0))
    if seed is not None:
        payload['seed'] = int(seed)

    sub = _post_json(base + '/video/submit', headers, payload, 60.0)
    rid = str(sub.get('requestId') or sub.get('request_id') or '')
    if not rid:
        raise CloudError(f'视频提交未返回 requestId：{_short(str(sub))}')

    deadline = time.time() + HTTP_TIMEOUT_VIDEO
    while time.time() < deadline:
        if is_canceled and is_canceled():
            raise CloudError('已取消（云端任务可能仍在服务商侧继续计费，可到服务商控制台查看）。')
        st = _post_json(base + '/video/status', headers, {'requestId': rid}, 30.0)
        status = str(st.get('status') or '').lower()
        if status == 'succeed' or status == 'success':
            videos = ((st.get('results') or {}).get('videos') or [])
            if not videos or not videos[0].get('url'):
                raise CloudError(f'视频成功但没有下载地址：{_short(str(st))}')
            return _download(videos[0]['url']), '.mp4'
        if status == 'failed' or status == 'fail':
            raise CloudError('云端视频生成失败：' + str(st.get('reason') or '未知原因'))
        time.sleep(POLL_INTERVAL)
    raise CloudError('云端视频生成超时（15 分钟）。')


# ================================================================ 入库落盘
def save_cloud_images(root: str, images: list[tuple[bytes, str]], *, provider: str,
                      model: str, prompt: str) -> list[str]:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    paths = []
    for i, (data, ext) in enumerate(images):
        name = f'cloud_{provider}_{stamp}_{i + 1:02d}{ext}'
        rel = '/'.join([assets.GENERATED_DIR.replace('\\', '/'), IMAGE_SUBDIR, name])
        full = assets.safe_join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(data)
        import json
        meta = {'source': f'cloud:{provider}', 'asset_kind': 'image',
                'author': f'{get_provider(provider)["name"]}（云端生成）',
                'license': 'check-provider-terms', 'prompt': prompt, 'model': model,
                'imported_at': datetime.now().isoformat(timespec='seconds')}
        with open(full + '.json', 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        paths.append(rel.replace('\\', '/'))
    return paths


# ================================================================ 后台任务
class CloudJobManager:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def status(self, job_id: str, root: str | None = None) -> dict | None:
        with self._lock:
            j = self._jobs.get(job_id)
            if not j or (root and os.path.abspath(j.get('root', '')) != os.path.abspath(root)):
                return None
            return dict(j)

    def list_jobs(self, limit: int = 20, root: str | None = None) -> list[dict]:
        with self._lock:
            jobs = [j for j in self._jobs.values()
                    if not root or os.path.abspath(j.get('root', '')) == os.path.abspath(root)]
            jobs = sorted(jobs, key=lambda x: x.get('created_at', ''), reverse=True)
            return [dict(j) for j in jobs[:limit]]

    def _update(self, job_id: str, **kw):
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.update(kw)

    def cancel(self, job_id: str, root: str | None = None) -> dict:
        j = self.status(job_id, root=root)
        if not j:
            return {'ok': False, 'error': '任务不存在。'}
        if j.get('status') in ('queued', 'running'):
            self._update(job_id, cancel_requested=True, status='canceling')
        return {'ok': True}

    def _new_job(self, root: str, jtype: str, provider: str, model: str, prompt: str) -> str:
        jid = 'c' + uuid.uuid4().hex[:11]
        self._jobs[jid] = {
            'id': jid, 'root': os.path.abspath(root), 'type': jtype, 'engine': 'cloud',
            'provider': provider, 'model': model,
            'status': 'queued', 'phase': '连接云端服务商', 'progress': 2,
            'prompt_id': '', 'prompt': prompt, 'result': None, 'error': '',
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'cancel_requested': False,
        }
        return jid

    def submit_image(self, root: str, *, provider: str, model: str, prompt: str,
                     width: int, height: int, negative_prompt: str,
                     seed: int | None, batch: int,
                     inline_key: str = '', base_url: str = '') -> str:
        jid = self._new_job(root, 'image', provider, model, prompt)

        def run():
            try:
                self._update(jid, status='running', progress=12, phase='云端生成图片中')
                imgs = generate_image(
                    root=root, pid=provider, model=model, prompt=prompt, width=width,
                    height=height, negative_prompt=negative_prompt, seed=seed,
                    batch=batch, inline_key=inline_key, base_url=base_url)
                self._update(jid, phase='写入素材库', progress=90)
                paths = save_cloud_images(root, imgs, provider=provider, model=model, prompt=prompt)
                self._update(jid, status='completed', phase='完成', progress=100,
                             result={'paths': paths, 'engine': 'cloud'})
            except CloudError as e:
                self._fail(jid, str(e))
            except Exception as e:  # noqa: BLE001
                self._fail(jid, f'云端生图异常：{e}')

        threading.Thread(target=run, daemon=True).start()
        return jid

    def submit_animation(self, root: str, *, provider: str, model: str, prompt: str,
                         first_frame: bytes | None, duration: float, seed: int | None,
                         fps: int, max_frames: int,
                         inline_key: str = '', base_url: str = '') -> str:
        jid = self._new_job(root, 'animation', provider, model, prompt)

        def run():
            import tempfile
            tmp_path = ''
            try:
                canceled = lambda: bool(self.status(jid).get('cancel_requested'))
                self._update(jid, status='running', progress=8, phase='云端视频模型排队/生成中')
                video_bytes, ext = generate_video(
                    root=root, pid=provider, model=model, prompt=prompt,
                    first_frame=first_frame, duration=duration, seed=seed,
                    inline_key=inline_key, base_url=base_url, is_canceled=canceled)
                fd, tmp_path = tempfile.mkstemp(suffix=ext)
                with os.fdopen(fd, 'wb') as f:
                    f.write(video_bytes)
                self._update(jid, phase='下载完成，抽帧中', progress=78)
                frames, info = asset_gen.extract_frames(tmp_path, fps, max_frames)
                self._update(jid, phase='拼精灵图集并入库', progress=92)
                name = 'cloud_' + provider.split('-')[0] + '_' + datetime.now().strftime('%Y%m%d_%H%M%S')
                manifest = asset_gen.save_animation(
                    root, frames, info, name=name, prompt=prompt, model=model,
                    seed=seed if seed is not None else 0, duration=duration, fps=fps,
                    video_bytes=video_bytes, video_ext=ext,
                    source=f'cloud:{provider}',
                    author=f'{get_provider(provider)["name"]}（云端生成）')
                self._update(jid, status='completed', phase='完成', progress=100,
                             result={'name': manifest['name'], 'sheet': manifest['_sheet_rel'],
                                     'dir': manifest['_dir_rel'], 'frame_count': manifest['frame_count'],
                                     'cols': manifest['cols'], 'rows': manifest['rows'],
                                     'frame_width': manifest['frame_width'],
                                     'frame_height': manifest['frame_height'],
                                     'fps': manifest['fps'], 'engine': 'cloud'})
            except CloudError as e:
                self._fail(jid, str(e))
            except Exception as e:  # noqa: BLE001
                self._fail(jid, f'云端帧动画异常：{e}')
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        threading.Thread(target=run, daemon=True).start()
        return jid

    def _fail(self, job_id: str, msg: str):
        self._update(job_id, status='failed', error=msg)


jobs = CloudJobManager()
