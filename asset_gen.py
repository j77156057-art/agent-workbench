# -*- coding: utf-8 -*-
"""本地 AI 素材生成（阶段 5）。

- 文生图：本地 ComfyUI + Z-Image Turbo，产物写入 ``assets/generated/images``；
- 图生帧动画：本地 ComfyUI + MiniMax H3（i2v）先出一段短视频，再抽帧
  （默认 12fps、最多 64 帧）为 PNG 序列，并拼出 SpriteSheet，统一落到
  ``assets/generated/animations/<名称>/``，附 ``*.anim.json`` 动画清单。

设计约束（见阶段 5 决策）：
- v1 不做背景抠像（帧保留原背景）；
- 视频必须来自真实视频模型管线（UNETLoader fl2va + MiniMaxH3ImageToVideo +
  CreateVideo + 视频 VAE + SaveVideo），禁止用单图推镜/图片合成冒充；
- ffmpeg 由系统提供：优先 ``IMAGEIO_FFMPEG_EXE``，其次 PATH 中的 ``ffmpeg``；
  分发版不再捆绑 imageio-ffmpeg 的 exe（约 100MB），缺 ffmpeg 时给出明确中文指引；
- ComfyUI 为常驻外部进程，提交/取历史/取消复用 game_workbench，本模块只负责
  参数化、产物后处理与入库登记。
"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import uuid
from datetime import datetime

import httpx

import asset_sources as assets
import game_workbench as gw

# ---------------------------------------------------------------- 参数 / 模型
DEFAULT_FPS = 12
DEFAULT_MAX_FRAMES = 64
IMAGE_SUBDIR = 'images'
ANIM_SUBDIR = 'animations'
ANIM_SUFFIX = assets.ANIM_SUFFIX
VIDEO_EXTS = ('.mp4', '.webm', '.mov', '.mkv')
# 视频大模型（20GB+）靠 ComfyUI 逐层 offload 在小显存卡上也能跑，
# 提交门槛比生图低；可用 DOCMIND_VIDEO_MIN_FREE_MB 覆盖。
VIDEO_MIN_FREE_MB = int(os.getenv('DOCMIND_VIDEO_MIN_FREE_MB', '256') or 256)
# 生图提交前要求的空闲显存；与协调器 COMFY_MIN_FREE_MB 默认对齐。
IMAGE_MIN_FREE_MB = int(os.getenv('DOCMIND_IMAGE_MIN_FREE_MB',
                                  str(int(gw.COMFY_MIN_FREE_MB))) or int(gw.COMFY_MIN_FREE_MB))

# Z-Image 文生图链路：相对 <comfy>/models 的权重路径
IMAGE_MODEL_FILES = {
    'unet/z_image_turbo-Q8_0.gguf',
    'clip/Qwen3-4B-Q8_0.gguf',
    'vae/ae.safetensors',
}
# MiniMax H3 图生视频链路所需权重
VIDEO_MODEL_FILES = {
    'diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors',
    'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
    'vae/minimax_h3_video_vae_fp16.safetensors',
    'vae/minimax_h3_audio_vae_fp32.safetensors',
    'loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors',
}
COMFY_WORKFLOW_SUBDIR = os.path.join('ComfyUI', 'user', 'default', 'workflows')

# H3 拍平后节点字段在不同节点包里可能有差异，按候选名顺序尝试，找不到就明确报错。
_PATCH_FIELDS = {
    'MiniMaxH3ImageToVideo': ('prompt', 'text', 'positive_prompt'),
    'RandomNoise': ('noise_seed', 'seed'),
    'PrimitiveFloat': ('value', 'float_value'),
    'PrimitiveInt': ('value', 'int_value'),
    'LoadImage': ('image',),
    'LoraLoaderModelOnly': ('strength_model', 'strength'),
    'BasicScheduler': ('steps',),
}


class GenError(Exception):
    """生成链路可展示给用户的错误（中文）。"""


# ================================================================ 环境就绪探测
def _candidate_model_roots() -> list[tuple[str, str]]:
    """返回去重后的 (comfy_root, models_dir)。便携版为 <root>/ComfyUI/models。"""
    roots: list[str] = []
    env = str(os.getenv('DOCMIND_COMFY_ROOT', '') or '').strip()
    if env:
        roots.append(env)
    roots += [r'D:\ComfyUI', r'C:\ComfyUI']
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for root in roots:
        for models_dir in (os.path.join(root, 'ComfyUI', 'models'), os.path.join(root, 'models')):
            if os.path.isdir(models_dir) and models_dir.lower() not in seen:
                seen.add(models_dir.lower())
                out.append((root, models_dir))
    return out


def _workflows_dir(root: str) -> str:
    return os.path.join(root, 'ComfyUI', 'user', 'default', 'workflows')


def find_h3_workflow(root: str, kind: str = 'i2v') -> str:
    """在 ComfyUI 用户工作流目录里找 MiniMax H3 基底工作流。

    kind='i2v' 找图生视频（含 LoadImage），'t2v' 找文生视频。
    优先精确的 minimax_h3_<kind>.json，找不到再按前缀兜底。
    """
    wd = _workflows_dir(root)
    if not os.path.isdir(wd):
        return ''
    try:
        names = [n for n in os.listdir(wd) if n.lower().endswith('.json')]
    except OSError:
        return ''
    exact = f'minimax_h3_{kind}.json'
    for n in names:
        if n.lower() == exact:
            return os.path.join(wd, n)
    # 排除 ref2v/ref2va 与定制案例（wuxia/inkduel），节点结构与通用 i2v 不同。
    # 本机无官方通用名 minimax_h3_i2v.json，silverhair 版是官方 i2v 示例
    # （含 LoadImage 首帧），作为首选基底。
    bad = ('ref2', 'wuxia', 'inkduel')
    candidates = [
        n for n in names
        if 'minimax_h3' in n.lower() and kind in n.lower()
        and not any(b in n.lower() for b in bad)
    ]
    candidates.sort(key=lambda n: (0 if 'silverhair' in n.lower() else 1, len(n)))
    return os.path.join(wd, candidates[0]) if candidates else ''


def _check_models(models_dir: str, required: set[str]) -> list[str]:
    return sorted(rel for rel in required if not os.path.isfile(os.path.join(models_dir, *rel.split('/'))))


def generation_status(url: str = 'http://127.0.0.1:8188') -> dict:
    """盘点本地 ComfyUI 服务 / 权重 / 工作流是否齐备（只读，缺口驱动）。"""
    try:
        st = gw.comfy_status(url)
        online = bool(st.get('available'))
        online_error = st.get('error') or ''
    except Exception as e:  # noqa: BLE001 - 探测不能抛
        online, online_error = False, str(e)

    roots = _candidate_model_roots()
    root = roots[0][0] if roots else ''
    models_dir = roots[0][1] if roots else ''
    image_missing = _check_models(models_dir, IMAGE_MODEL_FILES) if models_dir else sorted(IMAGE_MODEL_FILES)
    video_missing = _check_models(models_dir, VIDEO_MODEL_FILES) if models_dir else sorted(VIDEO_MODEL_FILES)

    i2v_wf = find_h3_workflow(root, 'i2v') if root else ''
    t2v_wf = find_h3_workflow(root, 't2v') if root else ''
    return {
        'ok': True,
        'online': online,
        'online_error': online_error,
        'comfy_root': root,
        'models_dir': models_dir,
        'image': {'ready': online and not image_missing, 'missing': image_missing},
        'video': {
            'ready': online and not video_missing and bool(i2v_wf),
            'missing': video_missing + ([] if i2v_wf else ['工作流 minimax_h3_i2v*.json']),
        },
        'workflows': {'i2v': i2v_wf, 't2v': t2v_wf},
    }


# ================================================================ 工作流参数化
def build_image_workflow(*, prompt: str, negative_prompt: str = 'blurry, low quality',
                         width: int = 512, height: int = 512, steps: int = 8,
                         seed: int = 42, batch_size: int = 1,
                         filename_prefix: str = 'docmind_zimage') -> dict:
    """Z-Image Turbo 静态 API workflow（固定节点号见 game_workbench 模板）。"""
    tpl = gw.comfy_template_workflow('z-image-turbo')
    if not tpl.get('ok'):
        raise GenError(tpl.get('error') or '无法构造 Z-Image 工作流')
    wf = tpl['workflow']
    params = {'prompt': prompt, 'negative_prompt': negative_prompt,
              'width': int(width), 'height': int(height), 'steps': int(steps),
              'seed': int(seed), 'filename_prefix': filename_prefix}
    applied = gw.comfy_apply_parameters(wf, params)
    if not applied.get('ok'):
        raise GenError(applied.get('error') or 'Z-Image 参数应用失败')
    wf = applied['workflow']
    if isinstance(wf.get('5'), dict) and isinstance(wf['5'].get('inputs'), dict):
        wf['5']['inputs']['batch_size'] = max(1, min(int(batch_size), 4))
    return wf


def _patch_node(wf: dict, class_type: str, value, *, title_hint: str = '') -> bool:
    """按 class_type 定位唯一节点并把 value 写入候选字段；返回是否成功。"""
    fields = _PATCH_FIELDS.get(class_type, ())
    matched = [
        n for n in wf.values()
        if isinstance(n, dict) and n.get('class_type') == class_type
    ]
    if title_hint and matched:
        # 拍平成 API 图后节点标题通常丢失：hint 只在多个同类节点时用于消歧；
        # 找不到带标题的但全图只有一个同类节点时，直接用它。
        hinted = [
            n for n in matched
            if title_hint.lower() in str(n.get('_title', '')).lower()
        ]
        if hinted:
            matched = hinted
        elif len(matched) != 1:
            return False
    targets = matched
    if not targets:
        return False
    node = targets[0]
    inputs = node.setdefault('inputs', {})
    for f in fields:
        if f in inputs:
            inputs[f] = value
            return True
    # 字段不存在（拍平后只保留了有连接/有值的输入）：补写第一个候选名。
    if fields:
        inputs[fields[0]] = value
        return True
    return False


def build_h3_workflow(url: str, *, prompt: str, duration: float = 5.0, seed: int = 1,
                      image_name: str = '', turbo: bool = True) -> dict:
    """读本地 MiniMax H3 i2v UI 工作流 → 拍平成 API 图 → 覆盖参数。

    需要 ComfyUI 在线（拍平要 /object_info 校验节点字段）。
    """
    status = generation_status(url)
    if not status['online']:
        raise GenError('ComfyUI 未运行，无法构造视频工作流。')
    root = status['comfy_root']
    path = find_h3_workflow(root, 'i2v')
    if not path:
        raise GenError('未找到 MiniMax H3 图生视频工作流（minimax_h3_i2v*.json）。')
    try:
        with open(path, encoding='utf-8') as f:
            ui = json.load(f)
    except OSError as e:
        raise GenError(f'无法读取 H3 工作流：{e}') from e

    converted = gw.comfy_ui_to_api_workflow(ui, url)
    if not converted.get('ok'):
        raise GenError(converted.get('error') or 'H3 工作流拍平失败')
    wf = converted['workflow']

    errors = []
    if not _patch_node(wf, 'MiniMaxH3ImageToVideo', prompt):
        errors.append('提示词节点 MiniMaxH3ImageToVideo')
    if not _patch_node(wf, 'RandomNoise', int(seed)):
        errors.append('随机种子节点 RandomNoise')
    if not _patch_node(wf, 'PrimitiveFloat', float(duration), title_hint='duration'):
        errors.append('时长节点 PrimitiveFloat(Float duration)')
    if image_name and not _patch_node(wf, 'LoadImage', str(image_name)):
        errors.append('首帧节点 LoadImage')
    # 8 步加速 LoRA：开启时 strength=1.0 且采样步数降到 8；关闭走默认 28 步。
    if not _patch_node(wf, 'LoraLoaderModelOnly', 1.0 if turbo else 0.0):
        errors.append('加速 LoRA 节点 LoraLoaderModelOnly')
    if turbo and not _patch_node(wf, 'BasicScheduler', 8):
        errors.append('调度步数节点 BasicScheduler')
    if errors:
        raise GenError('H3 工作流缺少参数节点：' + '、'.join(errors))
    return wf


# ================================================================ ComfyUI 原语
def _base(url: str) -> str:
    return str(url).rstrip('/')


def upload_image(url: str, data: bytes, filename: str = 'firstframe.png') -> str:
    """上传首帧到 ComfyUI input 目录，返回可填入 LoadImage 的文件名。"""
    try:
        r = httpx.post(
            _base(url) + '/upload/image',
            files={'image': (filename, data, 'image/png')},
            data={'type': 'input', 'overwrite': 'true'},
            timeout=60,
        )
        r.raise_for_status()
        name = str(r.json().get('name') or '')
        if not name:
            raise GenError('ComfyUI 未返回上传文件名。')
        return name
    except httpx.HTTPError as e:
        raise GenError(f'首帧上传失败：{e}') from e


def _view_url(url: str, rec: dict) -> str:
    import urllib.parse
    q = urllib.parse.urlencode({
        'filename': rec.get('filename') or '',
        'subfolder': rec.get('subfolder') or '',
        'type': rec.get('type') or 'output',
    })
    return _base(url) + '/view?' + q


def raw_history(prompt_id: str, url: str) -> dict:
    """直连 ComfyUI /history，同时收集图片/视频(gifs/videos)/音频/files 产物。

    game_workbench.comfy_history 只归并 images；SaveVideo 的产物在 gifs/videos，
    故此处保留原始 outputs 结构作为事实来源。
    """
    try:
        r = httpx.get(_base(url) + '/history/' + str(prompt_id), timeout=10)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        raise GenError(f'ComfyUI 状态查询失败：{e}') from e
    item = data.get(prompt_id, data)
    outputs: list[dict] = []
    for node in (item.get('outputs') or {}).values():
        if not isinstance(node, dict):
            continue
        for group in ('images', 'gifs', 'videos', 'audio', 'files'):
            for rec in (node.get(group) or []):
                if isinstance(rec, dict) and rec.get('filename'):
                    x = dict(rec)
                    x['group'] = group
                    x['preview_url'] = _view_url(url, x)
                    outputs.append(x)
    status = item.get('status', {}) or {}
    status_str = str(status.get('status_str') or '')
    messages = status.get('messages') or []
    executed = sum(1 for m in messages if isinstance(m, list) and m and m[0] in ('execution_cached', 'executed'))
    total = len(item.get('prompt', {}) or {}) or 1
    failed = status_str in ('error', 'failed')
    done = bool(item.get('outputs')) or status_str in ('success', 'error', 'failed')
    return {
        'ok': True, 'prompt_id': prompt_id, 'outputs': outputs,
        'done': done, 'failed': failed, 'status_str': status_str,
        'progress': {'executed_nodes': executed, 'total_nodes': total,
                     'percent': round(executed * 100 / total, 1)},
        'raw_status': status,
    }


def _download(url: str, preview_url: str, max_bytes: int = 600 * 1024 * 1024) -> bytes:
    try:
        with httpx.stream('GET', preview_url, timeout=120) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_bytes(1024 * 1024):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise GenError(f'生成产物超过 {max_bytes // (1024 * 1024)}MB 上限。')
            return bytes(buf)
    except httpx.HTTPError as e:
        raise GenError(f'生成产物下载失败：{e}') from e


def submit(workflow: dict, url: str, min_free_mb=None, root=None) -> str:
    r = gw.comfy_queue(workflow, url, min_free_mb=min_free_mb, root=root)
    if not r.get('ok'):
        raise GenError(r.get('error') or '工作流提交失败')
    pid = str((r.get('response') or {}).get('prompt_id') or '')
    if not pid:
        raise GenError('ComfyUI 未返回 prompt_id。')
    return pid


# 本进程内上一次 ComfyUI 任务的链路（'image'/'video'）。
# 生图模型与 H3 视频模型不同家族，跨链路切换必须腾退；同链路连跑则可吃缓存。
_LAST_COMFY_KIND = ''


def ensure_vram_headroom(url: str, required_mb: int, *, kind: str = '', on_phase=None,
                         timeout: float = 25.0) -> None:
    """提交前确保 GPU 有 required_mb 空闲显存；不够或跨链路时让 ComfyUI 卸载驻留模型。

    ComfyUI 默认会把上一次的模型缓存在显存里（H3 约 10GB），导致同卡的下一次
    生成在协调器门槛处被拦、或跨链路（生图↔生视频）加载时 OOM。策略：
    - 同链路且空闲已够：不动缓存，同类型连跑不付重载代价；
    - 跨链路（如生完图再生视频）：无论空闲多少都卸载，给新模型家族腾出整卡；
    - 空闲不够且 ComfyUI 有任务在跑：不打断，直接放行交提交门槛判定；
    - 卸载后仍不够：明确报缺多少，不静默排队。
    显存探测不到（无 nvidia-smi 等）且非跨链路时不拦截。
    """
    global _LAST_COMFY_KIND
    free = gw.gpu_free_mb()
    cross = bool(kind) and bool(_LAST_COMFY_KIND) and kind != _LAST_COMFY_KIND
    under_threshold = free is not None and free < required_mb
    if not cross and (free is None or not under_threshold):
        return
    if on_phase:
        try:
            on_phase(cross)
        except Exception:  # noqa: BLE001 - 进度回调不能影响腾退
            pass
    r = gw.comfy_free_models(url, wait=False)
    if not r.get('ok'):
        # 有任务执行中/服务异常：不在这里误杀，让提交时的门槛给出结论
        return
    if cross:
        # /free 同步返回后模型已卸载，跨链路不再按门槛硬等，显存读数稍后刷新即可
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.0)
        free = gw.gpu_free_mb()
        if free is not None and free >= required_mb:
            return
    raise GenError(
        f'显存不足：已让 ComfyUI 卸载驻留模型，当前空闲 {free} MB，'
        f'本次生成需要 {required_mb} MB。请关闭其他占用显存的程序后重试。')


def wait(prompt_id: str, url: str, *, timeout: int = 1200, interval: float = 2.0,
         on_progress=None) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = raw_history(prompt_id, url)
        if on_progress:
            try:
                on_progress(last)
            except Exception:  # noqa: BLE001 - 进度回调不能打断等待
                pass
        if last['failed']:
            msg = _extract_error(last)
            raise GenError(f'ComfyUI 生成失败：{msg or last["status_str"]}')
        if last['done'] and last['outputs']:
            return last
        time.sleep(interval)
    raise GenError('生成超时（%d 秒）。' % timeout)


def _extract_error(history: dict) -> str:
    for m in (history.get('raw_status') or {}).get('messages') or []:
        if isinstance(m, list) and m and m[0] == 'execution_error' and len(m) > 1:
            return str((m[1] or {}).get('exception_message') or m[1])
    return ''


# ================================================================ 视频抽帧 / 图集
def grid_dims(n: int) -> tuple[int, int]:
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    return cols, rows


def _ensure_ffmpeg() -> None:
    """解析 ffmpeg 可执行文件：IMAGEIO_FFMPEG_EXE → 系统 PATH → 包内捆绑。

    分发版已剔除 imageio-ffmpeg 捆绑的 100MB exe，所以最后一步在冻结包里
    必然失败；这时给出明确的中文指引，避免用户看到 PackageNotFoundError
    或 ExecutableNotFound 这类体系级报错。源码安装仍可复用 venv 内的捆绑版。
    """
    exe = str(os.getenv('IMAGEIO_FFMPEG_EXE', '') or '').strip()
    if exe and os.path.isfile(exe):
        return
    found = shutil.which('ffmpeg')
    if found:
        os.environ['IMAGEIO_FFMPEG_EXE'] = found
        return
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
        return
    except Exception:  # 捆绑版被裁掉 / 解析失败
        raise GenError('未找到 ffmpeg：请安装系统 ffmpeg 并加入 PATH，'
                       '或设置 IMAGEIO_FFMPEG_EXE 指向 ffmpeg.exe。') from None


def extract_frames(video_path: str, target_fps: int = DEFAULT_FPS,
                   max_frames: int = DEFAULT_MAX_FRAMES) -> tuple[list, dict]:
    """均匀抽帧为 RGB PIL 图像。

    步长 = 源fps / 目标fps（目标≥源时逐帧）；最多取 max_frames 帧，
    源视频不够长时有多少取多少（不复制帧填充）。
    """
    _ensure_ffmpeg()
    try:
        import imageio
    except Exception as e:  # 冻结包缺元数据等
        raise GenError(f'视频解码组件不可用（imageio 加载失败）：{e}') from e
    from PIL import Image
    reader = None
    frames: list = []
    taken: list[int] = []
    try:
        reader = imageio.get_reader(video_path)
        try:
            md = reader.get_meta_data()
            src_fps = float(md.get('fps') or 24)
        except (TypeError, ValueError):
            src_fps = 24.0
        if not src_fps or src_fps <= 0:
            src_fps = 24.0
        step = 1.0 if target_fps >= src_fps else src_fps / float(target_fps)
        wanted = sorted({int(round(k * step)) for k in range(max_frames)})
        wanted_set = set(wanted)
        max_wanted = wanted[-1] if wanted else 0
        for i, arr in enumerate(reader):
            if i > max_wanted:
                break
            if i in wanted_set:
                frames.append(Image.fromarray(arr).convert('RGB'))
                taken.append(i)
    except GenError:
        raise
    except Exception as e:  # imageio/ffmpeg 打开或解码失败
        raise GenError(f'无法读取视频帧：{e}') from e
    finally:
        if reader is not None:
            reader.close()
    if not frames:
        raise GenError('未能从视频中抽取到任何帧。')
    info = {'src_fps': round(src_fps, 3), 'out_fps': int(target_fps),
            'src_indices': taken, 'frame_count': len(frames)}
    return frames, info


def build_spritesheet(frames: list) -> tuple[object, int, int, int, int]:
    from PIL import Image
    fw, fh = frames[0].size
    cols, rows = grid_dims(len(frames))
    sheet = Image.new('RGB', (cols * fw, rows * fh), (0, 0, 0))
    for k, fr in enumerate(frames):
        sheet.paste(fr.convert('RGB'), ((k % cols) * fw, (k // cols) * fh))
    return sheet, cols, rows, fw, fh


# ================================================================ 入库落盘
def _now_iso() -> str:
    return datetime.now().isoformat(timespec='seconds')


def _write_generated(root: str, rel_path: str, data: bytes, sidecar: dict) -> str:
    """写生成物文件 + 同名 .json sidecar，返回相对项目根的 POSIX 路径。"""
    full = assets.safe_join(root, rel_path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as f:
        f.write(data)
    meta = dict(sidecar)
    meta.setdefault('imported_at', _now_iso())
    with open(full + '.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return rel_path.replace('\\', '/')


def save_generated_images(root: str, images: list[bytes], *, prompt: str,
                          model: str = 'z_image_turbo-Q8_0.gguf',
                          dest_subdir: str = IMAGE_SUBDIR) -> list[str]:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    paths = []
    for i, data in enumerate(images):
        name = f'zimage_{stamp}_{i + 1:02d}.png'
        rel = '/'.join([assets.GENERATED_DIR.replace('\\', '/'), dest_subdir, name])
        meta = {'source': 'comfyui', 'asset_kind': 'image', 'author': 'Z-Image（本地生成）',
                'license': 'Apache-2.0', 'prompt': prompt, 'model': model}
        paths.append(_write_generated(root, rel, data, meta))
    return paths


def save_animation(root: str, frames: list, info: dict, *, name: str, prompt: str,
                   model: str, seed, duration, fps: int = DEFAULT_FPS,
                   video_bytes: bytes | None = None, video_ext: str = '.mp4',
                   first_frame_name: str = '', dest_subdir: str = ANIM_SUBDIR,
                   source: str = 'comfyui-h3',
                   author: str = 'MiniMax H3（本地生成）') -> dict:
    """落盘帧序列 + SpriteSheet + 动画清单；返回清单（含相对路径）。"""
    safe = assets.safe_name(name, default='animation').replace(' ', '_')
    dir_rel = '/'.join([assets.GENERATED_DIR.replace('\\', '/'), dest_subdir, safe])
    sheet_name = f'{safe}_sheet.png'

    full_dir = assets.safe_join(root, dir_rel)
    frames_dir = os.path.join(full_dir, 'frames')
    os.makedirs(frames_dir, exist_ok=True)

    frame_paths = []
    for k, fr in enumerate(frames, start=1):
        fn = f'frame_{k:04d}.png'
        fr.save(os.path.join(frames_dir, fn), format='PNG')
        frame_paths.append(f'frames/{fn}')

    sheet, cols, rows, fw, fh = build_spritesheet(frames)
    sheet.save(os.path.join(full_dir, sheet_name), format='PNG')

    video_rel = ''
    if video_bytes:
        ext = video_ext if video_ext.lower().endswith(VIDEO_EXTS) else '.mp4'
        video_rel = f'{safe}_source{ext}'
        with open(os.path.join(full_dir, video_rel), 'wb') as f:
            f.write(video_bytes)

    manifest = {
        'name': safe,
        'kind': 'animation',
        'asset_kind': 'animation',
        'source': source,
        'author': author,
        'license': 'check-model-card',
        'created_at': _now_iso(),
        'imported_at': _now_iso(),
        'model': model,
        'prompt': prompt,
        'seed': seed,
        'duration': duration,
        'fps': int(fps),
        'frame_count': len(frames),
        'frame_width': fw,
        'frame_height': fh,
        'cols': cols,
        'rows': rows,
        'sheet': sheet_name,
        'frames_dir': 'frames',
        'frame_prefix': 'frame_',
        'frame_ext': '.png',
        'first_frame': first_frame_name,
        'video': video_rel,
        'frames': frame_paths,
        'src_extract': info,
    }
    with open(os.path.join(full_dir, safe + ANIM_SUFFIX), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    manifest['_dir_rel'] = dir_rel
    manifest['_sheet_rel'] = f'{dir_rel}/{sheet_name}'
    return manifest


# ================================================================ 后台生成任务
class JobManager:
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
            jobs = [j for j in self._jobs.values() if not root or os.path.abspath(j.get('root', '')) == os.path.abspath(root)]
            jobs = sorted(jobs, key=lambda x: x.get('created_at', ''), reverse=True)
            return [dict(j) for j in jobs[:limit]]

    def _update(self, job_id: str, **kw):
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.update(kw)

    def cancel(self, job_id: str, url: str, root: str | None = None) -> dict:
        j = self.status(job_id, root=root)
        if not j:
            return {'ok': False, 'error': '任务不存在。'}
        pid = j.get('prompt_id')
        if pid and j.get('status') in ('queued', 'running'):
            r = gw.comfy_cancel(pid, url, root=j.get('root'))
            if not r.get('ok'):
                return r
        self._update(job_id, cancel_requested=True, status='canceling')
        return {'ok': True}

    # ---- 提交入口
    def submit_image(self, root: str, *, url: str, prompt: str, negative_prompt: str,
                     width: int, height: int, steps: int, seed: int, batch_size: int) -> str:
        job_id = uuid.uuid4().hex[:12]
        self._jobs[job_id] = {
            'id': job_id, 'root': os.path.abspath(root), 'type': 'image', 'status': 'queued', 'phase': '准备工作流',
            'progress': 0, 'prompt_id': '', 'prompt': prompt, 'result': None,
            'error': '', 'created_at': _now_iso(), 'cancel_requested': False,
        }
        t = threading.Thread(target=self._run_image, args=(job_id, root, url),
                             kwargs=dict(prompt=prompt, negative_prompt=negative_prompt,
                                         width=width, height=height, steps=steps,
                                         seed=seed, batch_size=batch_size), daemon=True)
        t.start()
        return job_id

    def submit_animation(self, root: str, *, url: str, prompt: str, duration: float,
                         seed: int, fps: int, max_frames: int, turbo: bool,
                         first_frame_bytes: bytes | None,
                         first_frame_name: str = '') -> str:
        job_id = uuid.uuid4().hex[:12]
        self._jobs[job_id] = {
            'id': job_id, 'root': os.path.abspath(root), 'type': 'animation', 'status': 'queued', 'phase': '准备工作流',
            'progress': 0, 'prompt_id': '', 'prompt': prompt, 'result': None,
            'error': '', 'created_at': _now_iso(), 'cancel_requested': False,
        }
        t = threading.Thread(target=self._run_animation, args=(job_id, root, url),
                             kwargs=dict(prompt=prompt, duration=duration, seed=seed,
                                         fps=fps, max_frames=max_frames, turbo=turbo,
                                         first_frame_bytes=first_frame_bytes,
                                         first_frame_name=first_frame_name), daemon=True)
        t.start()
        return job_id

    # ---- 实际执行（后台线程）
    def _on_progress(self, job_id: str, phase: str, cap: tuple[int, int]):
        def cb(hist):
            p = hist.get('progress') or {}
            pct = cap[0] + int((cap[1] - cap[0]) * (p.get('percent', 0) / 100.0))
            self._update(job_id, status='running', phase=phase, progress=min(pct, cap[1] - 1),
                         prompt_id=hist.get('prompt_id') or '')
        return cb

    def _run_image(self, job_id, root, url, **kw):
        global _LAST_COMFY_KIND
        pid = ''
        try:
            ensure_vram_headroom(
                url, IMAGE_MIN_FREE_MB, kind='image',
                on_phase=lambda cross: self._update(
                    job_id,
                    phase='正在切换模型，腾出生图显存…' if cross else '正在腾出显存…',
                    progress=3))
            wf = build_image_workflow(**kw)
            pid = submit(wf, url, root=root)
            _LAST_COMFY_KIND = 'image'
            self._update(job_id, prompt_id=pid)
            hist = wait(pid, url, on_progress=self._on_progress(job_id, '本地生成图片中', (5, 80)))
            imgs = [o for o in hist['outputs']
                    if o['group'] == 'images' and str(o.get('filename', '')).lower().endswith(
                        ('.png', '.jpg', '.jpeg', '.webp'))]
            if not imgs:
                raise GenError('工作流没有产出图片。')
            data = [_download(url, o['preview_url']) for o in imgs]
            self._update(job_id, phase='写入素材库', progress=88)
            paths = save_generated_images(root, data, prompt=kw.get('prompt', ''))
            self._update(job_id, status='completed', phase='完成', progress=100,
                         result={'paths': paths})
        except GenError as e:
            self._fail(job_id, str(e))
        except Exception as e:  # noqa: BLE001 - 后台任务必须收口
            self._fail(job_id, f'本地生图异常：{e}')
        finally:
            # 自轮询路径不走 comfy_history，必须显式释放 GPU 租约，
            # 否则 600s TTL 内的下一次生成会被"GPU 正忙"挡住。
            if pid:
                gw.comfy_release_job(pid)

    def _run_animation(self, job_id, root, url, **kw):
        global _LAST_COMFY_KIND
        tmp_path = ''
        pid = ''
        try:
            # 已有 ComfyUI input 文件名（走 upload-frame 上传或素材库转存）时直接用；
            # 否则本任务随带字节时现场上传。
            image_name = kw.get('first_frame_name', '') or ''
            fbytes = kw.get('first_frame_bytes')
            if fbytes:
                self._update(job_id, phase='上传首帧', progress=3)
                image_name = upload_image(url, fbytes, f'docmind_{job_id}.png')
            wf = build_h3_workflow(
                url, prompt=kw['prompt'], duration=kw['duration'], seed=kw['seed'],
                image_name=image_name, turbo=kw['turbo'])
            ensure_vram_headroom(
                url, VIDEO_MIN_FREE_MB, kind='video',
                on_phase=lambda cross: self._update(
                    job_id,
                    phase='正在切换模型，腾出生视频显存…' if cross else '正在腾出显存…',
                    progress=5))
            pid = submit(wf, url, min_free_mb=VIDEO_MIN_FREE_MB, root=root)
            _LAST_COMFY_KIND = 'video'
            self._update(job_id, prompt_id=pid)
            hist = wait(pid, url, timeout=1800,
                        on_progress=self._on_progress(job_id, 'H3 视频模型生成中（较慢）', (6, 70)))
            # 新版 SaveVideo 把 mp4 放在 outputs.images（带 animated 标记），
            # 旧版放在 gifs/videos，故统一按扩展名识别。
            vids = [o for o in hist['outputs']
                    if str(o.get('filename', '')).lower().endswith(VIDEO_EXTS)]
            if not vids:
                raise GenError('工作流没有产出视频文件。')
            self._update(job_id, phase='下载视频', progress=74)
            video_bytes = _download(url, vids[0]['preview_url'])
            import tempfile
            fd, tmp_path = tempfile.mkstemp(suffix='.mp4')
            with os.fdopen(fd, 'wb') as f:
                f.write(video_bytes)
            self._update(job_id, phase='抽帧', progress=80)
            frames, info = extract_frames(tmp_path, kw['fps'], kw['max_frames'])
            self._update(job_id, phase='拼精灵图集并入库', progress=92)
            name = 'anim_' + datetime.now().strftime('%Y%m%d_%H%M%S')
            manifest = save_animation(
                root, frames, info, name=name, prompt=kw['prompt'],
                model='minimax_h3_fl2va_pruned_int8_convrot.safetensors',
                seed=kw['seed'], duration=kw['duration'], fps=kw['fps'],
                video_bytes=video_bytes, first_frame_name=kw.get('first_frame_name', ''))
            self._update(job_id, status='completed', phase='完成', progress=100,
                         result={'name': manifest['name'], 'sheet': manifest['_sheet_rel'],
                                 'dir': manifest['_dir_rel'], 'frame_count': manifest['frame_count'],
                                 'cols': manifest['cols'], 'rows': manifest['rows'],
                                 'frame_width': manifest['frame_width'],
                                 'frame_height': manifest['frame_height'],
                                 'fps': manifest['fps']})
        except GenError as e:
            self._fail(job_id, str(e))
        except Exception as e:  # noqa: BLE001
            self._fail(job_id, f'帧动画生成异常：{e}')
        finally:
            if pid:
                gw.comfy_release_job(pid)
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _fail(self, job_id, msg):
        self._update(job_id, status='failed', error=msg)


# 进程级单例（FastAPI 常驻进程内复用；重启后任务状态清空，产物已落盘）
jobs = JobManager()
