# -*- coding: utf-8 -*-
"""系统资源采样与能力画像的门禁测试。

这里只测**纯判定**与**诚实性**，不打真实 GPU（机器有没有 N 卡不该决定测试红绿）：
- CPU 物理 ≤2 / 逻辑 ≤4 → 低端；
- GPU 型号命中黑名单 / 软件渲染 / 无独显 → 低端；
- **GPU 采样不可用本身绝不能判低端**（拿不到 ≠ 机器弱）；
- 采样不可用时占用率必须是 null + 原因，不能填 0 冒充空闲。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import system_resources as sr  # noqa: E402


def _cpu(physical=8, logical=16, freq=3200):
    return {
        'physical_cores': physical, 'logical_cores': logical,
        'freq_mhz': freq, 'freq_max_mhz': freq, 'freq_known': bool(freq),
    }


def _gpu(available=False, name=None, reason='nvidia-smi 不存在'):
    return {'available': available, 'name': name, 'reason': reason}


def _system(old=False, retired=False):
    return {'platform': 'Windows', 'release': '11', 'version': '10.0.22000',
            'old': old, 'retired': retired}


class JudgeProfileTests(unittest.TestCase):
    def test_low_end_cpu_is_low(self):
        profile = sr.judge_profile(_cpu(2, 4), _gpu(), _system(), models=['NVIDIA GeForce RTX 4090'])
        self.assertEqual(profile['tier'], 'low')
        kinds = {reason['kind'] for reason in profile['reasons']}
        self.assertIn('cpu_physical', kinds)
        self.assertIn('cpu_logical', kinds)
        # 每条理由都要带实测值与阈值，UI 才能解释。
        for reason in profile['reasons']:
            self.assertIn('value', reason)
            self.assertIn('threshold', reason)

    def test_weak_gpu_blacklist_is_low(self):
        profile = sr.judge_profile(
            _cpu(8, 16), _gpu(False), _system(), models=['Intel(R) HD Graphics 4000'])
        self.assertEqual(profile['tier'], 'low')
        self.assertTrue(any(r['kind'] == 'gpu_weak_model' for r in profile['reasons']))

    def test_software_render_is_low(self):
        profile = sr.judge_profile(
            _cpu(8, 16), _gpu(False), _system(), models=['ANGLE (Google, SwiftShader)'])
        self.assertEqual(profile['tier'], 'low')
        self.assertTrue(any(r['kind'] == 'gpu_software_render' for r in profile['reasons']))

    def test_integrated_only_is_low(self):
        profile = sr.judge_profile(
            _cpu(8, 16), _gpu(False), _system(), models=['Intel(R) UHD Graphics 630'])
        self.assertEqual(profile['tier'], 'low')
        self.assertTrue(any(r['kind'] == 'gpu_integrated_only' for r in profile['reasons']))

    def test_gpu_unavailable_is_not_low(self):
        """验收硬条件：GPU 采样不可用 ≠ 低端。"""
        profile = sr.judge_profile(_cpu(8, 16), _gpu(), _system(), models=[])
        self.assertNotEqual(profile['tier'], 'low')
        unknown = [r for r in profile['reasons'] if r['kind'] == 'gpu_unknown']
        self.assertEqual(len(unknown), 1, '不可用要留下一条信息型理由')
        self.assertIn('仅作信息', unknown[0]['threshold'])

    def test_gpu_unavailable_weak_cpu_stays_low_only_by_cpu(self):
        profile = sr.judge_profile(_cpu(4, 8), _gpu(), _system(), models=[])
        self.assertEqual(profile['tier'], 'mid', '4 核 8 线程是 mid，不应被不可用的 GPU 拖成 low')

    def test_weighted_signals_do_not_veto_alone(self):
        profile = sr.judge_profile(
            _cpu(8, 16), _gpu(False), _system(retired=True), models=['NVIDIA GeForce RTX 4090'],
            browser={'device_memory_gb': 8, 'prefers_reduced_motion': True})
        self.assertNotEqual(profile['tier'], 'low', '单一加权项不得否决整机')

    def test_reasons_carry_measured_values(self):
        profile = sr.judge_profile(_cpu(2, 2), _gpu(False), _system(), models=[])
        for reason in profile['reasons']:
            self.assertTrue(reason['detail'])
            self.assertTrue(reason['value'])


class GpuHonestyTests(unittest.TestCase):
    def test_probe_without_nvidia_reports_unavailable_not_zero(self):
        """没有 nvidia-smi 时：available=false + 原因，占用率保持 None（不是 0）。"""
        with patch.object(sr, '_probe_gpu_nvml', return_value=None), \
             patch.object(sr, '_smi_path', return_value=None):
            gpu = sr.probe_gpu()
        self.assertFalse(gpu['available'])
        self.assertIsNone(gpu['utilization_percent'])
        self.assertIsNone(gpu['memory_used_mb'])
        self.assertIn('nvidia-smi', gpu['reason'])
        self.assertIn('非 NVIDIA', gpu['reason'])

    def test_probe_nonzero_exit_reports_stderr(self):
        class _Proc:
            returncode = 9
            stdout = ''
            stderr = 'NVIDIA-SMI has failed because it couldn\'t communicate with the driver'

        with patch.object(sr, '_probe_gpu_nvml', return_value=None), \
             patch.object(sr, '_smi_path', return_value='nvidia-smi'), \
             patch.object(sr.subprocess, 'run', return_value=_Proc()):
            gpu = sr.probe_gpu()
        self.assertFalse(gpu['available'])
        self.assertIsNone(gpu['utilization_percent'])
        self.assertIn('9', gpu['reason'])
        self.assertIn('driver', gpu['reason'])

    def test_probe_parses_real_smi_line(self):
        class _Proc:
            returncode = 0
            stdout = '0, 37, 11401, 12227, NVIDIA GeForce RTX 3090, 552.22\n'
            stderr = ''

        with patch.object(sr, '_probe_gpu_nvml', return_value=None), \
             patch.object(sr, '_smi_path', return_value='nvidia-smi'), \
             patch.object(sr.subprocess, 'run', return_value=_Proc()):
            gpu = sr.probe_gpu()
        self.assertTrue(gpu['available'])
        self.assertEqual(gpu['utilization_percent'], 37.0)
        self.assertEqual(gpu['memory_used_mb'], 11401.0)
        self.assertEqual(gpu['memory_total_mb'], 12227.0)
        self.assertEqual(gpu['name'], 'NVIDIA GeForce RTX 3090')


class EndpointShapeTests(unittest.TestCase):
    def test_endpoint_returns_all_required_sections(self):
        from starlette.testclient import TestClient
        import api

        response = TestClient(api.app).get('/api/system/resources')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        for key in ('cpu', 'memory', 'disk', 'gpu', 'profile', 'sampled_at'):
            self.assertIn(key, body)
        self.assertIn('tier', body['profile'])
        self.assertIn('reasons', body['profile'])
        # 采样诚实：不可用时占用率不得是数字。
        if not body['gpu'].get('available'):
            self.assertIsNone(body['gpu'].get('utilization_percent'))
            self.assertTrue(body['gpu'].get('reason'))


if __name__ == '__main__':
    unittest.main()
