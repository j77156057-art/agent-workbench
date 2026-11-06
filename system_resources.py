# -*- coding: utf-8 -*-
"""系统资源采样 + 能力画像（只读，供 ``GET /api/system/resources`` 使用）。

为什么必须放在后端：浏览器拿不到真实的 CPU / GPU 占用率。
``navigator.hardwareConcurrency`` 只是核数、``deviceMemory`` 是粗粒度档位、
``storage.estimate()`` 只给配额、``WEBGL_debug_renderer_info`` 常被屏蔽，
所以「实时占用率」只能在服务进程里用 psutil / NVML 采样，前端只做展示与判定。

硬约束：
- 采样轻量：``psutil.cpu_percent(interval=None)`` 非阻塞 + 后台定时线程；
  接口只读内存里最近一次快照，TTL 1.5s，响应时间与采样间隔无关。
- GPU 诚实：pynvml / nvidia-smi 拿不到就返回 ``available=false`` + ``reason``，
  **绝不**用 0 冒充「空闲」。A 卡 / 集显 / 无驱动一律如实标不可用。
- 能力画像静态、开局一次：CPU / GPU / OS / 内存容量，输出 ``tier`` 与带实测值的
  ``reasons``，让 UI 能解释「为什么降级」。
"""
from __future__ import annotations

import copy
import os
import platform
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # psutil 是硬依赖，但测试环境可能只导入 api；缺了就明确报不可用。
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

SAMPLE_INTERVAL_S = 1.5
GPU_INTERVAL_TICKS = 2          # GPU 探针比 CPU 贵（要起子进程），约 3s 一次
NVIDIA_SMI_TIMEOUT_S = 2.0

# 已知弱卡 / 软件渲染关键字（小写匹配）。命中即判低端。
WEAK_GPU_PATTERNS = (
    'mali-400', 'mali 400',
    'swiftshader', 'llvmpipe', 'softpipe', 'software rasterizer',
    'microsoft basic render driver',
)

_DISCRETE_GPU_PATTERNS = (
    'nvidia', 'geforce', 'quadro', 'rtx ', 'gtx ', 'radeon rx', 'radeon pro',
    'intel arc', 'arc a', 'arc b',
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _smi_path() -> str | None:
    """定位 nvidia-smi：先查 PATH，Windows 再试系统目录。"""
    found = shutil.which('nvidia-smi')
    if found:
        return found
    if os.name == 'nt':
        candidate = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'nvidia-smi.exe'
        if candidate.is_file():
            return str(candidate)
    return None


def _hidden_kwargs() -> dict[str, Any]:
    """Windows 下不弹黑框；其它平台无此参数。"""
    if os.name == 'nt':
        return {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    return {}


def _one_line(text: str, limit: int = 180) -> str:
    text = ' '.join((text or '').split())
    return text[:limit]


def _probe_gpu_nvml() -> dict[str, Any] | None:
    """优先 pynvml（无子进程开销）；未装时返回明确原因，NVML 初始化失败同样返回原因。"""
    try:
        import pynvml  # type: ignore
    except ImportError:
        # pynvml 是可选依赖：没有它就走 nvidia-smi；两者都拿不到时
        # probe_gpu 返回 available=false 并带上这条原因（不把「取不到」当 0%）。
        return {'available': False, 'reason': 'pynvml 不可用，GPU 指标跳过'}
    try:
        pynvml.nvmlInit()
        count = pynvml.nvmlDeviceGetCount()
        if count <= 0:
            return {'available': False, 'reason': 'NVML 初始化成功但未发现 GPU 设备'}
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        raw_name = pynvml.nvmlDeviceGetName(handle)
        name = raw_name.decode('utf-8', 'replace') if isinstance(raw_name, bytes) else str(raw_name)
        util = pynvml.nvmlDeviceGetUtilizationRates(handle).gpu
        memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
        try:
            driver = pynvml.nvmlSystemGetDriverVersion()
            driver = driver.decode('utf-8', 'replace') if isinstance(driver, bytes) else str(driver)
        except Exception:  # noqa: BLE001
            driver = None
        return {
            'available': True, 'reason': '', 'source': 'pynvml',
            'index': 0, 'device_count': count, 'name': name,
            'utilization_percent': _round(util, 0),
            'memory_used_mb': _round(memory.used / (1024 * 1024), 0),
            'memory_total_mb': _round(memory.total / (1024 * 1024), 0),
            'driver_version': driver,
        }
    except Exception as exc:  # noqa: BLE001
        return {'available': False, 'reason': 'pynvml 采样失败：%s: %s' % (type(exc).__name__, _one_line(str(exc)))}
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:  # noqa: BLE001
            pass


def probe_gpu() -> dict[str, Any]:
    """返回一份 GPU 采样；不可用时给出可读原因，不编造数字。"""
    found = _probe_gpu_nvml()
    if found is not None and found.get('available'):
        return found

    smi = _smi_path()
    if not smi:
        reason = '未找到 nvidia-smi（无 NVIDIA 驱动，或本机为非 NVIDIA 平台）'
        if found and found.get('reason'):
            reason = '%s；%s' % (reason, found['reason'])
        return {
            'available': False, 'reason': reason, 'source': None, 'index': None,
            'name': None, 'utilization_percent': None, 'memory_used_mb': None,
            'memory_total_mb': None, 'driver_version': None, 'sampled_at': _iso_now(),
        }

    query = 'index,utilization.gpu,memory.used,memory.total,name,driver_version'
    try:
        proc = subprocess.run(
            [smi, '--query-gpu=' + query, '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_S, **_hidden_kwargs(),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            'available': False, 'reason': 'nvidia-smi 执行失败：%s: %s' % (
                type(exc).__name__, _one_line(str(exc))),
            'source': 'nvidia-smi', 'index': None, 'name': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'driver_version': None, 'sampled_at': _iso_now(),
        }
    if proc.returncode != 0:
        detail = _one_line(proc.stderr or proc.stdout or '')
        return {
            'available': False,
            'reason': 'nvidia-smi 返回码 %s：%s' % (proc.returncode, detail or '无输出'),
            'source': 'nvidia-smi', 'index': None, 'name': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'driver_version': None, 'sampled_at': _iso_now(),
        }

    lines = [line for line in (proc.stdout or '').splitlines() if line.strip()]
    if not lines:
        return {
            'available': False, 'reason': 'nvidia-smi 没有返回任何设备行',
            'source': 'nvidia-smi', 'index': None, 'name': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'driver_version': None, 'sampled_at': _iso_now(),
        }
    first = [part.strip() for part in lines[0].split(',')]
    if len(first) < 5:
        return {
            'available': False, 'reason': 'nvidia-smi 输出无法解析：%s' % _one_line(lines[0]),
            'source': 'nvidia-smi', 'index': None, 'name': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'driver_version': None, 'sampled_at': _iso_now(),
        }
    try:
        index = int(first[0])
        utilization = float(first[1])
        used_mb = float(first[2])
        total_mb = float(first[3])
        name = first[4]
        driver = first[5] if len(first) > 5 else None
    except (TypeError, ValueError):
        return {
            'available': False, 'reason': 'nvidia-smi 数值无法解析：%s' % _one_line(lines[0]),
            'source': 'nvidia-smi', 'index': None, 'name': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'driver_version': None, 'sampled_at': _iso_now(),
        }
    return {
        'available': True, 'reason': '', 'source': 'nvidia-smi', 'index': index,
        'device_count': len(lines), 'name': name,
        'utilization_percent': _round(utilization, 0),
        'memory_used_mb': _round(used_mb, 0),
        'memory_total_mb': _round(total_mb, 0),
        'driver_version': driver,
        'sampled_at': _iso_now(),
    }


def _windows_video_controllers() -> list[str]:
    """静态枚举显卡型号（只用于画像；拿不到就返回空，不猜）。"""
    if os.name != 'nt':
        return []
    script = (
        'Get-CimInstance Win32_VideoController | '
        'Where-Object { $_.Name } | Select-Object -ExpandProperty Name'
    )
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, text=True, timeout=4.0, **_hidden_kwargs(),
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in (proc.stdout or '').splitlines() if line.strip()]


def _cpu_static() -> dict[str, Any]:
    physical = logical = None
    freq_current = freq_max = None
    if psutil is not None:
        try:
            physical = psutil.cpu_count(logical=False)
        except Exception:  # noqa: BLE001
            physical = None
        try:
            logical = psutil.cpu_count(logical=True)
        except Exception:  # noqa: BLE001
            logical = None
        try:
            freq = psutil.cpu_freq()
            if freq is not None:
                freq_current = _round(freq.current, 0) or None
                freq_max = _round(freq.max, 0) or None
        except Exception:  # noqa: BLE001
            freq_current = freq_max = None
    return {
        'physical_cores': physical,
        'logical_cores': logical,
        'freq_mhz': freq_current,
        'freq_max_mhz': freq_max,
        'freq_known': bool(freq_current),
    }


def _is_weak_gpu_name(name: str) -> bool:
    low = (name or '').lower()
    # UHD / Iris 是 4xxx 之后的世代，不能被下面的「hd graphics 数字」规则误伤。
    if 'intel' in low and ('uhd' in low or 'iris' in low):
        return False
    if any(pattern in low for pattern in WEAK_GPU_PATTERNS):
        return True
    match = re.search(r'hd graphics\s*(\d{3,4})', low)
    if match:
        try:
            return int(match.group(1)) <= 4600
        except ValueError:
            return False
    # 无编号的老 Intel HD Graphics（Arrandale / Sandy Bridge 早期）同样算弱卡。
    return 'intel' in low and 'hd graphics' in low


def _is_discrete_gpu(name: str) -> bool:
    low = (name or '').lower()
    return any(pattern in low for pattern in _DISCRETE_GPU_PATTERNS)


def _os_signal() -> dict[str, Any]:
    system = platform.system()
    release = platform.release()
    version = platform.version()
    old = False
    retired = False
    if system == 'Windows':
        try:
            # Windows 以 NT 内核号命名：6.x = Win7/8/8.1，10/11 才是现代系统。
            major = int(str(release).split('.')[0])
        except (TypeError, ValueError):
            major = 0
        if major < 10:
            old = True
        elif release.startswith('10') and not version.startswith('10.0.2'):
            # Windows 10 已于 2025-10 停止支持：仅作为加权项，不做否决。
            retired = True
    elif system == 'Darwin':
        try:
            # Darwin 19 == macOS 10.15；更早的版本已不被浏览器支持。
            old = int(str(release).split('.')[0]) < 19
        except (TypeError, ValueError):
            pass
    elif system == 'Linux':
        try:
            old = int(str(release).split('.')[0]) < 5
        except (TypeError, ValueError):
            pass
    return {
        'platform': system, 'release': release, 'version': version,
        'old': old, 'retired': retired,
    }


def _memory_static() -> dict[str, Any]:
    if psutil is None:
        return {'total_bytes': None, 'total_gb': None}
    try:
        total = int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001
        return {'total_bytes': None, 'total_gb': None}
    return {'total_bytes': total, 'total_gb': round(total / (1024 ** 3), 1)}


def judge_profile(cpu: dict[str, Any], gpu: dict[str, Any], system: dict[str, Any],
                  models: list[str] | None = None,
                  browser: dict[str, Any] | None = None) -> dict[str, Any]:
    """静态能力画像：CPU / GPU 是硬否决，系统与运行时信号只做加权。

    ``gpu`` 是动态采样（可能 ``available=false``）；``models`` 是静态枚举到的显卡型号。
    关键约定：**GPU 采样不可用本身不是降级理由**（拿不到不等于机器弱），
    只有型号命中黑名单 / WebGL 软件渲染 / 明确无独显才判低端。
    """
    reasons: list[dict[str, Any]] = []
    hard_low = False
    weighted = 0
    mid_hint = False

    physical = cpu.get('physical_cores')
    logical = cpu.get('logical_cores')
    if isinstance(physical, int) and physical <= 2:
        hard_low = True
        reasons.append({
            'kind': 'cpu_physical', 'detail': '物理核数 %d ≤ 2，属于低端 CPU' % physical,
            'value': str(physical), 'threshold': '物理核 ≤ 2',
        })
    elif isinstance(physical, int) and physical <= 4:
        mid_hint = True
    if isinstance(logical, int) and logical <= 4:
        hard_low = True
        reasons.append({
            'kind': 'cpu_logical', 'detail': '逻辑线程 %d ≤ 4，属于低端 CPU' % logical,
            'value': str(logical), 'threshold': '逻辑线程 ≤ 4',
        })
    elif isinstance(logical, int) and logical <= 8:
        mid_hint = True
    if physical is None and logical is None:
        reasons.append({
            'kind': 'cpu_unknown', 'detail': 'CPU 核数读取失败', 'value': 'unknown',
            'threshold': '仅作信息，不参与降级',
        })
    if not cpu.get('freq_known'):
        reasons.append({
            'kind': 'cpu_freq_unknown', 'detail': 'CPU 主频系统未提供', 'value': 'unknown',
            'threshold': '仅作信息，不参与降级',
        })

    models = [name for name in (models or []) if name]
    dynamic_name = gpu.get('name') if gpu.get('available') else None
    all_names = models + ([dynamic_name] if dynamic_name and dynamic_name not in models else [])
    weak_names = [name for name in all_names if _is_weak_gpu_name(name)]
    software = any(
        marker in (name or '').lower()
        for name in all_names
        for marker in ('swiftshader', 'llvmpipe', 'software')
    )
    discrete = [name for name in all_names if _is_discrete_gpu(name)]
    if software:
        hard_low = True
        reasons.append({
            'kind': 'gpu_software_render', 'detail': 'GPU 报告为软件渲染：%s' % all_names[0],
            'value': all_names[0], 'threshold': '命中 SwiftShader / llvmpipe / Software',
        })
    elif weak_names:
        hard_low = True
        reasons.append({
            'kind': 'gpu_weak_model', 'detail': 'GPU 型号属于已知弱卡：%s' % weak_names[0],
            'value': weak_names[0], 'threshold': 'Intel HD 4xxx 及更早 / Mali-400',
        })
    elif all_names and not discrete:
        # 枚举到了显卡但都不是独显：按需求「无独显 → 低端」。
        hard_low = True
        reasons.append({
            'kind': 'gpu_integrated_only', 'detail': '只有集成显卡，没有独立显卡：%s' % '、'.join(all_names),
            'value': '、'.join(all_names), 'threshold': '无独显',
        })
    if not all_names:
        # 采样不可用 / 枚举为空：只作信息，绝不据此判低端。
        reasons.append({
            'kind': 'gpu_unknown',
            'detail': 'GPU 占用率不可用：%s' % (gpu.get('reason') or '未检测到 GPU 信息'),
            'value': 'unavailable', 'threshold': '仅作信息，不参与降级',
        })

    if system.get('old'):
        weighted += 2
        reasons.append({
            'kind': 'os_old',
            'detail': '操作系统版本过老：%s %s' % (system.get('platform'), system.get('release')),
            'value': '%s %s' % (system.get('platform'), system.get('release')),
            'threshold': 'Windows < 10 / macOS < 10.15 / Linux 内核 < 5',
        })
    elif system.get('retired'):
        weighted += 1
        reasons.append({
            'kind': 'os_retired', 'detail': '操作系统已停止支持：Windows 10',
            'value': 'Windows 10', 'threshold': '加权 +1',
        })

    memory_gb = (browser or {}).get('device_memory_gb')
    if isinstance(memory_gb, (int, float)) and memory_gb <= 4:
        weighted += 2
        reasons.append({
            'kind': 'device_memory', 'detail': '设备内存档位 ≤ 4GB',
            'value': '%sGB' % memory_gb, 'threshold': 'deviceMemory ≤ 4GB',
        })
    heap_limit = (browser or {}).get('js_heap_limit_mb')
    if isinstance(heap_limit, (int, float)) and heap_limit > 0 and heap_limit < 512:
        weighted += 1
        reasons.append({
            'kind': 'js_heap', 'detail': 'JS 堆上限偏小',
            'value': '%dMB' % heap_limit, 'threshold': 'jsHeapSizeLimit < 512MB',
        })
    if (browser or {}).get('prefers_reduced_motion'):
        weighted += 1
        reasons.append({
            'kind': 'reduced_motion', 'detail': '系统已开启「减少动态效果」',
            'value': 'on', 'threshold': 'prefers-reduced-motion 已开',
        })
    if (browser or {}).get('no_long_frame_observer'):
        weighted += 1
        reasons.append({
            'kind': 'no_raf', 'detail': '浏览器缺少长帧观测能力',
            'value': 'missing', 'threshold': '无 requestAnimationFrame',
        })

    if hard_low:
        tier = 'low'
    elif mid_hint or weighted >= 2:
        tier = 'mid'
    else:
        tier = 'high'

    return {
        'tier': tier,
        'reasons': reasons,
        'cpu': cpu,
        'gpu': {
            'models': all_names,
            'available': bool(gpu.get('available')),
            'reason': gpu.get('reason') or '',
            'software_render': software,
            'discrete': bool(discrete),
        },
        'system': system,
    }


def build_static_profile(models: list[str] | None = None,
                         browser: dict[str, Any] | None = None) -> dict[str, Any]:
    """开局一次的能力画像：读 CPU/GPU/OS/内存静态信息后交给 ``judge_profile``。

    ``models`` 显式传入时不再枚举（单测用）；``browser`` 仅用于前端信号（可选）。
    """
    if models is None:
        models = _windows_video_controllers()
    cpu = _cpu_static()
    gpu = probe_gpu()
    system = _os_signal()
    mem = _memory_static()
    profile = judge_profile(cpu, gpu, system, models=models, browser=browser)
    profile['memory_total_gb'] = mem.get('total_gb')
    return profile


class SystemSampler:
    """后台定时采样 + 短 TTL 缓存；HTTP 端只读最近快照。"""

    def __init__(self, interval_s: float = SAMPLE_INTERVAL_S,
                 root: Path | None = None) -> None:
        self._interval = max(0.5, float(interval_s))
        self._root = root or Path(__file__).resolve().parent
        self._lock = threading.RLock()
        self._snapshot: dict[str, Any] | None = None
        self._profile: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._primed = False
        self._tick = 0
        self._gpu_last: dict[str, Any] = {
            'available': False, 'reason': 'GPU 采样尚未启动', 'source': None,
            'utilization_percent': None, 'memory_used_mb': None, 'memory_total_mb': None,
            'name': None, 'sampled_at': None,
        }
        self._process = None

    # -- 生命周期 -----------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._process = psutil.Process() if psutil is not None else None
        # 画像只算一次（会起 nvidia-smi / CIM，放在启动期而不是每个请求里）。
        try:
            self._profile = build_static_profile()
        except Exception as exc:  # noqa: BLE001
            self._profile = {
                'tier': 'mid', 'reasons': [{
                    'kind': 'profile_error', 'detail': '能力画像失败：%s: %s' % (
                        type(exc).__name__, exc), 'value': 'error',
                    'threshold': '仅作信息，不参与降级',
                }], 'cpu': {}, 'gpu': {}, 'system': {}, 'memory_total_gb': None,
            }
        self._sample(force_gpu=True)
        self._thread = threading.Thread(target=self._loop, name='docmind-system-sampler', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    # -- 采样 ---------------------------------------------------------------
    def _sample(self, force_gpu: bool = False) -> None:
        with self._lock:
            self._tick += 1
        cpu = self._sample_cpu()
        memory = self._sample_memory()
        disk = self._sample_disk()
        need_gpu = force_gpu or (self._tick % GPU_INTERVAL_TICKS == 0)
        if need_gpu:
            try:
                gpu = probe_gpu()
            except Exception as exc:  # noqa: BLE001
                gpu = {
                    'available': False, 'reason': 'GPU 采样异常：%s: %s' % (
                        type(exc).__name__, _one_line(str(exc))),
                    'source': None, 'utilization_percent': None,
                    'memory_used_mb': None, 'memory_total_mb': None, 'name': None,
                    'sampled_at': _iso_now(),
                }
            with self._lock:
                self._gpu_last = gpu
        with self._lock:
            gpu = copy.deepcopy(self._gpu_last)
        snapshot = {
            'cpu': cpu,
            'memory': memory,
            'disk': disk,
            'gpu': gpu,
            'profile': copy.deepcopy(self._profile) if self._profile else None,
            'sampled_at': _iso_now(),
        }
        with self._lock:
            self._snapshot = snapshot

    def _sample_cpu(self) -> dict[str, Any]:
        static = _cpu_static()
        overall = None
        process_percent = None
        warming_up = False
        if psutil is not None:
            if not self._primed:
                # 冷采会阻塞（interval>0）或返回无意义值（首次 None 恒为 0），
                # 所以只预热、本次不给数，等下一轮（1.5s 后）再报真实占用。
                self._primed = True
                warming_up = True
                try:
                    psutil.cpu_percent(interval=None)
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    overall = _round(psutil.cpu_percent(interval=None), 1)
                except Exception:  # noqa: BLE001
                    overall = None
                if self._process is not None:
                    try:
                        process_percent = _round(self._process.cpu_percent(interval=None), 1)
                    except Exception:  # noqa: BLE001
                        process_percent = None
        return {
            'overall_percent': overall,
            'process_percent': process_percent,
            'physical_cores': static['physical_cores'],
            'logical_cores': static['logical_cores'],
            'freq_mhz': static['freq_mhz'],
            'freq_max_mhz': static['freq_max_mhz'],
            'freq_known': static['freq_known'],
            'warming_up': warming_up,
            'sampled_at': _iso_now(),
        }

    def _sample_memory(self) -> dict[str, Any]:
        if psutil is None:
            return {'total_bytes': None, 'used_bytes': None, 'available_bytes': None,
                    'percent': None, 'error': 'psutil 未安装'}
        try:
            mem = psutil.virtual_memory()
        except Exception as exc:  # noqa: BLE001
            return {'total_bytes': None, 'used_bytes': None, 'available_bytes': None,
                    'percent': None, 'error': '%s: %s' % (type(exc).__name__, exc)}
        return {
            'total_bytes': int(mem.total),
            'used_bytes': int(mem.used),
            'available_bytes': int(mem.available),
            'percent': _round(mem.percent, 1),
        }

    def _sample_disk(self) -> dict[str, Any]:
        if psutil is None:
            return {'path': str(self._root), 'total_bytes': None, 'used_bytes': None,
                    'free_bytes': None, 'percent': None, 'error': 'psutil 未安装'}
        try:
            usage = psutil.disk_usage(str(self._root))
        except Exception as exc:  # noqa: BLE001
            return {'path': str(self._root), 'total_bytes': None, 'used_bytes': None,
                    'free_bytes': None, 'percent': None,
                    'error': '%s: %s' % (type(exc).__name__, exc)}
        return {
            'path': str(self._root),
            'total_bytes': int(usage.total),
            'used_bytes': int(usage.used),
            'free_bytes': int(usage.free),
            'percent': _round(usage.percent, 1),
        }

    # -- 读取 ---------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        self.start()  # 幂等：测试直接调端点、没跑 lifespan 时也能拿到数据
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot) if self._snapshot else None
        if snapshot is None:
            return {
                'cpu': {'overall_percent': None, 'process_percent': None, 'warming_up': True},
                'memory': {'percent': None, 'error': '采样尚未完成'},
                'disk': {'percent': None, 'error': '采样尚未完成'},
                'gpu': self._gpu_last,
                'profile': self._profile,
                'sampled_at': _iso_now(),
            }
        return snapshot


_SAMPLER = SystemSampler()


def start() -> None:
    _SAMPLER.start()


def stop() -> None:
    _SAMPLER.stop()


def snapshot() -> dict[str, Any]:
    return _SAMPLER.snapshot()


def reset_for_tests() -> None:
    """单测钩子：换一个全新采样器，避免跨用例共享预热状态。"""
    global _SAMPLER
    _SAMPLER.stop()
    _SAMPLER = SystemSampler()


__all__ = [
    'SystemSampler', 'judge_profile', 'build_static_profile', 'probe_gpu',
    'start', 'stop', 'snapshot', 'reset_for_tests', 'SAMPLE_INTERVAL_S',
]
