# -*- coding: utf-8 -*-
"""阶段 5 本地生成（asset_gen）全离线测试。

视频抽帧用 imageio-ffmpeg 真实写出一小段 mp4 再抽回，验证采样步长与帧数；
ComfyUI 在线交互全部 monkeypatch，不发任何网络请求。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import asset_gen as g
import asset_sources as assets
from PIL import Image


def _make_frames(n, size=(64, 64), color_step=0):
    import numpy as np
    frames = []
    for i in range(n):
        v = (i * max(1, color_step)) % 255
        arr = np.zeros((size[1], size[0], 3), dtype='uint8')
        arr[..., 0] = v
        frames.append(arr)
    return frames


def _write_test_video(path, n=48, fps=24, size=(64, 64)):
    import imageio
    imageio.mimsave(path, _make_frames(n, size=size), fps=fps, macro_block_size=None)


class GridTests(unittest.TestCase):
    def test_grid_dims(self):
        self.assertEqual(g.grid_dims(1), (1, 1))
        self.assertEqual(g.grid_dims(24), (5, 5))
        self.assertEqual(g.grid_dims(64), (8, 8))
        self.assertEqual(g.grid_dims(63), (8, 8))

    def test_spritesheet_geometry(self):
        frames = [Image.new('RGB', (40, 30), (i, i, i)) for i in range(24)]
        sheet, cols, rows, fw, fh = g.build_spritesheet(frames)
        self.assertEqual((cols, rows, fw, fh), (5, 5, 40, 30))
        self.assertEqual(sheet.size, (200, 150))


class ExtractFramesTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.video = os.path.join(self._td.name, 'clip.mp4')
        _write_test_video(self.video, n=48, fps=24)

    def tearDown(self):
        self._td.cleanup()

    def test_downsample_12fps(self):
        frames, info = g.extract_frames(self.video, target_fps=12, max_frames=64)
        # 48 帧 @24fps，步长 2 → 命中 idx 0,2,…,46 共 24 帧
        self.assertEqual(len(frames), 24)
        self.assertEqual(info['frame_count'], 24)
        self.assertEqual(info['out_fps'], 12)
        self.assertEqual(info['src_indices'][:3], [0, 2, 4])
        self.assertEqual(frames[0].size, (64, 64))

    def test_max_frames_cap(self):
        frames, info = g.extract_frames(self.video, target_fps=12, max_frames=10)
        self.assertEqual(len(frames), 10)
        self.assertEqual(info['src_indices'], [0, 2, 4, 6, 8, 10, 12, 14, 16, 18])

    def test_target_above_source_takes_all(self):
        frames, _ = g.extract_frames(self.video, target_fps=30, max_frames=64)
        self.assertEqual(len(frames), 48)

    def test_bad_video_raises(self):
        p = os.path.join(self._td.name, 'x.mp4')
        with open(p, 'wb') as f:
            f.write(b'not a video')
        with self.assertRaises(g.GenError):
            g.extract_frames(p)


class SaveAnimationLibraryTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        self.frames = [Image.new('RGB', (32, 32), (i * 4 % 255, 10, 20)) for i in range(12)]

    def tearDown(self):
        self._td.cleanup()

    def _save(self):
        info = {'src_fps': 24.0, 'out_fps': 12, 'src_indices': list(range(0, 24, 2)), 'frame_count': 12}
        return g.save_animation(
            self.root, self.frames, info, name='测试 anim run', prompt='idle loop',
            model='m.safetensors', seed=7, duration=2.0, fps=12,
            video_bytes=b'\x00\x01', video_ext='.mp4')

    def test_animation_aggregates_to_one_library_item(self):
        m = self._save()
        lib = assets.library(self.root)
        anims = [it for d in lib['dirs'] for it in d['items'] if it['kind'] == 'animation']
        self.assertEqual(len(anims), 1)
        a = anims[0]
        self.assertTrue(a['path'].endswith('_sheet.png'))
        self.assertEqual(a['fps'], 12)
        self.assertEqual(a['frame_count'], 12)
        self.assertEqual(a['cols'], 4)  # sqrt(12)=4
        self.assertEqual(a['source'], 'comfyui-h3')
        self.assertTrue(a['first_frame'].endswith('frame_0001.png'))
        # 散帧不得作为独立素材出现
        flat = [it['path'] for d in lib['dirs'] for it in d['items']]
        self.assertFalse(any('/frames/frame_' in p for p in flat))
        self.assertFalse(any(p.endswith('_source.mp4') for p in flat))
        # 图集几何
        self.assertTrue(os.path.isfile(assets.safe_join(self.root, a['path'])))
        self.assertEqual(m['frame_count'], 12)

    def test_generated_image_listed(self):
        self._save()
        paths = g.save_generated_images(self.root, [b'\x89PNG\r\n'], prompt='hero')
        # 伪 PNG 不能被图像库打开，但落盘/扫描只看字节与 sidecar
        self.assertEqual(len(paths), 1)
        lib = assets.library(self.root)
        flat = [it for d in lib['dirs'] for it in d['items']]
        imgs = [it for it in flat if it['path'] == paths[0]]
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]['kind'], 'image')
        self.assertEqual(imgs[0]['source'], 'comfyui')

    def test_broken_manifest_without_sheet_skipped(self):
        m = self._save()
        os.remove(assets.safe_join(self.root, m['_sheet_rel']))
        lib = assets.library(self.root)
        anims = [it for d in lib['dirs'] for it in d['items'] if it['kind'] == 'animation']
        self.assertEqual(anims, [])


class PatchWorkflowTests(unittest.TestCase):
    def _wf(self):
        return {
            '10': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'm.safetensors'}},
            '20': {'class_type': 'MiniMaxH3ImageToVideo', 'inputs': {'prompt': 'old'}},
            '30': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': 1}},
            '40': {'class_type': 'PrimitiveFloat', '_title': 'Float (duration)', 'inputs': {'value': 15}},
            '50': {'class_type': 'LoadImage', 'inputs': {'image': 'x.png'}},
            '60': {'class_type': 'LoraLoaderModelOnly', 'inputs': {'strength_model': 0.0}},
            '70': {'class_type': 'BasicScheduler', 'inputs': {'steps': 28}},
        }

    def test_patch_all_h3_params(self):
        wf = self._wf()
        self.assertTrue(g._patch_node(wf, 'MiniMaxH3ImageToVideo', 'a knight runs'))
        self.assertTrue(g._patch_node(wf, 'RandomNoise', 99))
        self.assertTrue(g._patch_node(wf, 'PrimitiveFloat', 5.0, title_hint='duration'))
        self.assertTrue(g._patch_node(wf, 'LoadImage', 'up.png'))
        self.assertTrue(g._patch_node(wf, 'LoraLoaderModelOnly', 1.0))
        self.assertTrue(g._patch_node(wf, 'BasicScheduler', 8))
        self.assertEqual(wf['20']['inputs']['prompt'], 'a knight runs')
        self.assertEqual(wf['30']['inputs']['noise_seed'], 99)
        self.assertEqual(wf['40']['inputs']['value'], 5.0)
        self.assertEqual(wf['50']['inputs']['image'], 'up.png')
        self.assertEqual(wf['60']['inputs']['strength_model'], 1.0)
        self.assertEqual(wf['70']['inputs']['steps'], 8)

    def test_patch_missing_node(self):
        self.assertFalse(g._patch_node({}, 'LoadImage', 'y.png'))

    def test_build_image_workflow_shape(self):
        wf = g.build_image_workflow(prompt='a slime monster', width=512, height=768,
                                    steps=8, seed=42, batch_size=3)
        self.assertEqual(wf['3']['inputs']['prompt'], 'a slime monster')
        self.assertEqual(wf['5']['inputs']['width'], 512)
        self.assertEqual(wf['5']['inputs']['height'], 768)
        self.assertEqual(wf['5']['inputs']['batch_size'], 3)


class StatusTests(unittest.TestCase):
    def test_generation_status_reports_missing(self):
        with tempfile.TemporaryDirectory() as root:
            models = os.path.join(root, 'ComfyUI', 'models')
            for rel in g.IMAGE_MODEL_FILES:
                p = os.path.join(models, *rel.split('/'))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, 'wb').close()
            # 视频权重只放一部分
            for rel in list(g.VIDEO_MODEL_FILES)[:-1]:
                p = os.path.join(models, *rel.split('/'))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                open(p, 'wb').close()
            wd = os.path.join(root, 'ComfyUI', 'user', 'default', 'workflows')
            os.makedirs(wd, exist_ok=True)
            with open(os.path.join(wd, 'minimax_h3_i2v.json'), 'w', encoding='utf-8') as f:
                json.dump({'nodes': []}, f)
            with mock.patch.object(g, '_candidate_model_roots', return_value=[(root, models)]), \
                 mock.patch.object(g.gw, 'comfy_status', return_value={'available': True, 'error': ''}):
                st = g.generation_status()
            self.assertTrue(st['online'])
            self.assertTrue(st['image']['ready'])
            self.assertFalse(st['video']['ready'])
            self.assertEqual(len(st['video']['missing']), 1)
            self.assertTrue(st['workflows']['i2v'].endswith('minimax_h3_i2v.json'))


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p

    def raise_for_status(self):
        pass


class RawHistoryTests(unittest.TestCase):
    def test_collects_video_and_image_groups(self):
        payload = {'pid': {
            'outputs': {
                '9': {'images': [{'filename': 'a.png', 'subfolder': '', 'type': 'output'}]},
                '10': {'gifs': [{'filename': 'clip.mp4', 'subfolder': 'video/X', 'type': 'output'}]},
            },
            'status': {'status_str': 'success',
                       'messages': [['executed', {'node': '9'}]]},
            'prompt': [str(i) for i in range(10)],
        }}
        with mock.patch.object(g.httpx, 'get', return_value=_FakeResp(payload)):
            h = g.raw_history('pid', 'http://127.0.0.1:8188')
        self.assertTrue(h['done'])
        groups = sorted(o['group'] for o in h['outputs'])
        self.assertEqual(groups, ['gifs', 'images'])
        vid = [o for o in h['outputs'] if o['group'] == 'gifs'][0]
        self.assertIn('/view?', vid['preview_url'])
        self.assertIn('video%2FX', vid['preview_url'])


class ComfyFreeModelsTests(unittest.TestCase):
    def _cm(self, payload=b''):
        m = mock.MagicMock()
        m.__enter__.return_value.read.return_value = b'{}'
        return m

    def test_free_posts_unload_when_idle(self):
        with mock.patch.object(g.gw, 'comfy_queue_running', return_value=False), \
             mock.patch.object(g.gw, 'gpu_free_mb', side_effect=[500, 9000]), \
             mock.patch.object(g.gw.time, 'sleep'), \
             mock.patch.object(g.gw.urllib.request, 'urlopen', return_value=self._cm()) as up:
            r = g.gw.comfy_free_models('http://127.0.0.1:8188', timeout=2)
        self.assertTrue(r['ok'])
        self.assertEqual(up.call_count, 1)
        self.assertIn('/free', up.call_args[0][0].full_url)
        self.assertIn(b'unload_models', up.call_args[0][0].data)

    def test_free_skipped_when_queue_running(self):
        with mock.patch.object(g.gw, 'comfy_queue_running', return_value=True), \
             mock.patch.object(g.gw.urllib.request, 'urlopen') as up:
            r = g.gw.comfy_free_models('http://127.0.0.1:8188')
        self.assertFalse(r['ok'])
        up.assert_not_called()


class VramHeadroomTests(unittest.TestCase):
    def test_enough_free_does_nothing(self):
        with mock.patch.object(g.gw, 'gpu_free_mb', return_value=8000), \
             mock.patch.object(g.gw, 'comfy_free_models') as fm:
            g.ensure_vram_headroom('http://x', 1024)
            fm.assert_not_called()

    def test_low_frees_models_and_recovers(self):
        seq = [600, 900, 2200]  # /free 后轮询到 2200MB
        with mock.patch.object(g.gw, 'gpu_free_mb', side_effect=seq), \
             mock.patch.object(g.gw, 'comfy_free_models', return_value={'ok': True}) as fm, \
             mock.patch.object(g.time, 'sleep'):
            g.ensure_vram_headroom('http://x', 1024)
            fm.assert_called_once_with('http://x', wait=False)

    def test_queue_running_skips_without_error(self):
        # ComfyUI 有任务执行中：不打断、不报错，交给提交门槛
        with mock.patch.object(g.gw, 'gpu_free_mb', return_value=100), \
             mock.patch.object(g.gw, 'comfy_free_models',
                               return_value={'ok': False, 'error': '任务执行中'}) as fm, \
             mock.patch.object(g.time, 'sleep'):
            g.ensure_vram_headroom('http://x', 1024)
            fm.assert_called_once()

    def test_still_low_after_free_raises(self):
        ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4])
        with mock.patch.object(g.gw, 'gpu_free_mb', return_value=400), \
             mock.patch.object(g.gw, 'comfy_free_models', return_value={'ok': True}), \
             mock.patch.object(g.time, 'sleep'), \
             mock.patch.object(g.time, 'time', side_effect=lambda: next(ticks)):
            with self.assertRaises(g.GenError) as cm:
                g.ensure_vram_headroom('http://x', 1024, timeout=0.2)
            self.assertIn('显存不足', str(cm.exception))

    def test_no_gpu_probe_skips_check(self):
        with mock.patch.object(g.gw, 'gpu_free_mb', return_value=None), \
             mock.patch.object(g.gw, 'comfy_free_models') as fm:
            g.ensure_vram_headroom('http://x', 1024)
            fm.assert_not_called()

    def test_cross_kind_forces_free_even_with_headroom(self):
        # 生完图（模型驻留）再切视频：哪怕空闲高于视频门槛也必须卸载图模型
        old = g._LAST_COMFY_KIND
        g._LAST_COMFY_KIND = 'image'
        try:
            phases = []
            with mock.patch.object(g.gw, 'gpu_free_mb', return_value=8000), \
                 mock.patch.object(g.gw, 'comfy_free_models', return_value={'ok': True}) as fm:
                g.ensure_vram_headroom(
                    'http://x', 256, kind='video',
                    on_phase=lambda cross: phases.append(cross))
            fm.assert_called_once_with('http://x', wait=False)
            self.assertEqual(phases, [True])
        finally:
            g._LAST_COMFY_KIND = old

    def test_same_kind_with_headroom_keeps_cache(self):
        old = g._LAST_COMFY_KIND
        g._LAST_COMFY_KIND = 'image'
        try:
            with mock.patch.object(g.gw, 'gpu_free_mb', return_value=8000), \
                 mock.patch.object(g.gw, 'comfy_free_models') as fm:
                g.ensure_vram_headroom('http://x', 1024, kind='image')
                fm.assert_not_called()
        finally:
            g._LAST_COMFY_KIND = old


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = self._td.name
        # 隔离真实 GPU：默认显存充足、租约释放只记录调用
        self._free_p = mock.patch.object(g.gw, 'gpu_free_mb', return_value=99999)
        self._rel_p = mock.patch.object(g.gw, 'comfy_release_job')
        self._unload_p = mock.patch.object(g.gw, 'comfy_free_models',
                                           return_value={'ok': True, 'freed_mb': 0})
        self._old_kind = g._LAST_COMFY_KIND
        g._LAST_COMFY_KIND = ''
        self.mock_release = self._rel_p.start()
        self._free_p.start()
        self._unload_p.start()

    def tearDown(self):
        g._LAST_COMFY_KIND = self._old_kind
        self._free_p.stop()
        self._rel_p.stop()
        self._unload_p.stop()
        self._td.cleanup()

    def _wait(self, jm, jid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            j = jm.status(jid)
            if j['status'] in ('completed', 'failed'):
                return j
            time.sleep(0.05)
        self.fail('job 未在时限内结束')

    def test_image_job_end_to_end(self):
        jm = g.JobManager()
        with mock.patch.object(g, 'build_image_workflow', return_value={'1': {}}) as bw, \
             mock.patch.object(g, 'submit', return_value='pid1') as ms, \
             mock.patch.object(g, 'wait', return_value={'outputs': [
                 {'group': 'images', 'filename': 'x.png', 'preview_url': 'http://x/0'}]}) as mw, \
             mock.patch.object(g, '_download', return_value=b'\x89PNG\r\n'):
            jid = jm.submit_image(self.root, url='http://x', prompt='slime', negative_prompt='',
                                  width=512, height=512, steps=8, seed=1, batch_size=1)
            j = self._wait(jm, jid)
        self.assertEqual(j['status'], 'completed', j.get('error'))
        self.assertTrue(j['result']['paths'])
        bw.assert_called_once()
        ms.assert_called_once()
        mw.assert_called_once()
        self.mock_release.assert_called_once_with('pid1')
        # 素材库可见
        lib = assets.library(self.root)
        self.assertTrue(any(it['path'] == j['result']['paths'][0]
                            for d in lib['dirs'] for it in d['items']))

    def test_animation_job_end_to_end(self):
        jm = g.JobManager()
        frames = [Image.new('RGB', (48, 48), (i, 0, 0)) for i in range(12)]
        info = {'frame_count': 12}
        with mock.patch.object(g, 'upload_image', return_value='up.png'), \
             mock.patch.object(g, 'build_h3_workflow', return_value={'1': {}}) as bh, \
             mock.patch.object(g, 'submit', return_value='pid2'), \
             mock.patch.object(g, 'wait', return_value={'outputs': [
                 {'group': 'gifs', 'filename': 'c.mp4', 'preview_url': 'http://x/v'}]}), \
             mock.patch.object(g, '_download', return_value=b'vidbytes'), \
             mock.patch.object(g, 'extract_frames', return_value=(frames, info)) as ef:
            jid = jm.submit_animation(
                self.root, url='http://x', prompt='run cycle', duration=2.0, seed=3,
                fps=12, max_frames=64, turbo=True,
                first_frame_bytes=b'png', first_frame_name='hero.png')
            j = self._wait(jm, jid)
        self.assertEqual(j['status'], 'completed', j.get('error'))
        self.assertEqual(j['result']['frame_count'], 12)
        self.assertEqual(j['result']['cols'], 4)
        ef.assert_called_once()
        bh.assert_called_once()
        self.mock_release.assert_called_once_with('pid2')
        lib = assets.library(self.root)
        anims = [it for d in lib['dirs'] for it in d['items'] if it['kind'] == 'animation']
        self.assertEqual(len(anims), 1)
        self.assertEqual(anims[0]['fps'], 12)

    def test_failed_job_records_error(self):
        jm = g.JobManager()
        with mock.patch.object(g, 'build_image_workflow', side_effect=g.GenError('ComfyUI 未运行')):
            jid = jm.submit_image(self.root, url='http://x', prompt='x', negative_prompt='',
                                  width=64, height=64, steps=8, seed=1, batch_size=1)
            j = self._wait(jm, jid)
        self.assertEqual(j['status'], 'failed')
        self.assertIn('ComfyUI', j['error'])
        # 提交前失败没有 pid，不应调用释放
        self.mock_release.assert_not_called()


class GenerateRouteTests(unittest.TestCase):
    def setUp(self):
        import config
        from starlette.testclient import TestClient
        import api as api_mod
        self.api = api_mod
        self.config = config
        self._td = tempfile.TemporaryDirectory()
        self._old_root = config.get_runtime('code_root')
        config.set_runtime('code_root', self._td.name)
        self.client = TestClient(api_mod.app)

    def tearDown(self):
        self.config.set_runtime('code_root', self._old_root or '')
        self._td.cleanup()

    def test_status_route(self):
        with mock.patch.object(g, 'generation_status',
                               return_value={'ok': True, 'online': False}):
            r = self.client.get('/api/assets/generate/status')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()['online'])

    def test_image_validation_and_submit(self):
        r = self.client.post('/api/assets/generate/image', json={'prompt': '   '})
        self.assertFalse(r.json()['ok'])
        self.assertIn('提示词', r.json()['error'])

        fake = mock.Mock()
        fake.submit_image.return_value = 'jid-1'
        with mock.patch.object(self.api.asset_gen, 'jobs', fake):
            r = self.client.post('/api/assets/generate/image', json={
                'prompt': 'a slime', 'width': 512, 'height': 768, 'batch_size': 2})
        self.assertTrue(r.json()['ok'], r.text)
        self.assertEqual(r.json()['job_id'], 'jid-1')
        kw = fake.submit_image.call_args.kwargs
        self.assertEqual(kw['width'], 512)
        self.assertEqual(kw['height'], 768)
        self.assertEqual(kw['batch_size'], 2)

    def test_animation_reads_project_frame_and_submits(self):
        frame = os.path.join(self._td.name, 'assets', 'hero.png')
        os.makedirs(os.path.dirname(frame), exist_ok=True)
        with open(frame, 'wb') as f:
            f.write(b'\x89PNG-bytes')
        fake = mock.Mock()
        fake.submit_animation.return_value = 'jid-2'
        with mock.patch.object(self.api.asset_gen, 'jobs', fake):
            r = self.client.post('/api/assets/generate/animation', json={
                'prompt': 'run cycle', 'duration': 3,
                'first_frame_path': 'assets/hero.png'})
        self.assertTrue(r.json()['ok'], r.text)
        kw = fake.submit_animation.call_args.kwargs
        self.assertEqual(kw['first_frame_bytes'], b'\x89PNG-bytes')
        self.assertEqual(kw['first_frame_name'], 'hero.png')
        self.assertEqual(kw['duration'], 3)

    def test_animation_bad_duration(self):
        r = self.client.post('/api/assets/generate/animation',
                             json={'prompt': 'x', 'duration': 99})
        self.assertFalse(r.json()['ok'])

    def test_upload_frame_route(self):
        with mock.patch.object(g, 'upload_image', return_value='up.png') as up:
            r = self.client.post(
                '/api/assets/generate/upload-frame',
                data={'url': 'http://127.0.0.1:8188'},
                files={'file': ('设定图.png', b'binary', 'image/png')})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['name'], 'up.png')
        up.assert_called_once()

    def test_upload_frame_rejects_ext(self):
        r = self.client.post('/api/assets/generate/upload-frame',
                             files={'file': ('a.txt', b'x', 'text/plain')})
        self.assertEqual(r.status_code, 400)

    def test_job_404_and_cancel(self):
        r = self.client.get('/api/assets/generate/jobs/does-not-exist')
        self.assertEqual(r.status_code, 404)
        fake = mock.Mock()
        fake.cancel.return_value = {'ok': True}
        with mock.patch.object(self.api.asset_gen, 'jobs', fake):
            r = self.client.post('/api/assets/generate/jobs/j1/cancel', json={})
        self.assertTrue(r.json()['ok'])
        fake.cancel.assert_called_once()


if __name__ == '__main__':
    unittest.main()
