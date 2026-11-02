import unittest
from unittest.mock import patch
import gpu_coordinator as g

class GpuProcessMonitorTests(unittest.TestCase):
    def setUp(self):
        g.GPU_MODE='serial'; g._leases.clear(); g._processes.clear(); g._recovery_events.clear()
    def tearDown(self):
        g._leases.clear(); g._processes.clear()
    def test_register_and_heartbeat(self):
        rec=g.register_process(321,'engine:test',1,'godot')
        self.assertEqual(rec['pid'],321); self.assertTrue(g.heartbeat_process(321))
        self.assertEqual(g.process_status()['processes'][0]['owner'],'engine:test')
    def test_register_is_idempotent_and_preserves_start(self):
        first=g.register_process(322,'comfyui:service',0,'comfyui')
        second=g.register_process(322,'comfyui:service',0,'comfyui')
        self.assertEqual(first['started_at'], second['started_at'])
    def test_compute_apps_are_mapped_to_registered_owner(self):
        g.register_process(323, 'engine:test', 0, 'godot')
        g._process_probe_cache.update({'available':True,'rows':[{'pid':323,'process_name':'godot','used_mb':100}]})
        app=g.process_status()['compute_apps'][0]
        self.assertEqual(app['owner'],'engine:test'); self.assertEqual(app['purpose'],'godot')
    def test_dead_process_is_recovered_and_lease_released(self):
        g._leases[0]={'owner':'engine:test','purpose':'godot','since':0,'ttl':0}
        g.register_process(999,'engine:test',0,'godot')
        with patch.object(g,'_pid_alive',return_value=False): g._recover_processes(100)
        self.assertEqual(g._leases,{})
        self.assertEqual(g.process_status()['recovery_events'][0]['status'],'orphan_recovered')
    def test_probe_failure_is_explicit(self):
        with patch('gpu_coordinator.subprocess.run', side_effect=OSError('missing')):
            self.assertFalse(g._default_process_probe()['available'])
    def test_restore_marks_stale_leases(self):
        import tempfile, json, os
        fd, path = tempfile.mkstemp(); os.close(fd)
        try:
            with open(path,'w',encoding='utf-8') as f: json.dump({'leases':{'0':{'owner':'old','purpose':'x'}}},f)
            old=g.STATE_FILE; g.STATE_FILE=__import__('pathlib').Path(path)
            self.assertEqual(g.restore_runtime_state(),1)
            self.assertEqual(g.process_status()['recovery_events'][-1]['status'],'recovered')
        finally:
            g.STATE_FILE=old
            try: os.unlink(path)
            except OSError: pass
    def test_sample_persists_unavailable_state(self):
        import tempfile, pathlib, json, os
        # 采样曲线落在 SAMPLES_FILE（B3 后与租约 STATE_FILE 分离）
        old=g.SAMPLES_FILE; fd,path=tempfile.mkstemp(); os.close(fd); g.SAMPLES_FILE=pathlib.Path(path)
        try:
            g.set_gpu_probe(lambda: None); g._sample_once()
            with open(path,encoding='utf-8') as handle:
                data=json.load(handle)
            self.assertFalse(data['available']); self.assertFalse(data['samples'][-1]['available'])
        finally:
            g.SAMPLES_FILE=old
            try: os.unlink(path)
            except OSError: pass

if __name__ == '__main__': unittest.main()
