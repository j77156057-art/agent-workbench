import json
import os, tempfile, unittest
from game_workbench import validate_task_scope, task_snapshot, engine_logs, scene_tree, runtime_events
from gpu_coordinator import acquire, release, status

class AgentWorkflowTests(unittest.TestCase):
    def test_task_scope_and_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'behaviors'))
            p=os.path.join(root,'behaviors','player.gd')
            with open(p,'w') as fh: fh.write('x')
            task={'region':'behaviors','files':['behaviors/player.gd'],'allowed_paths':['behaviors']}
            self.assertTrue(validate_task_scope(root,task)['ok'])
            self.assertIsNotNone(task_snapshot(root,task)['files']['behaviors/player.gd'])
            self.assertFalse(validate_task_scope(root, {'region':'behaviors','files':['values/x.gd']})['ok'])

    def test_engine_log_error_parse(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root,'.docmind_engine.log'),'w') as f: f.write('SCRIPT ERROR: res://behaviors/player.gd:12 parse error\n')
            r=engine_logs(root); self.assertEqual(r['errors'][0]['line'],12)
            self.assertEqual(r['errors'][0]['path'],'behaviors/player.gd')

    def test_engine_log_parses_two_line_script_error(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, '.docmind_engine.log'), 'w', encoding='utf-8') as f:
                f.write("Godot Engine v4.7.2.stable\n")
                f.write("SCRIPT ERROR: Invalid call. Nonexistent function 'take_damage' in base 'Nil'.\n")
                f.write("   at: player.take_damage (res://behaviors/enemy.gd:44)\n")
            r = engine_logs(root)
            self.assertEqual(r['error_count'], 1)
            self.assertEqual(len(r['errors']), 1)
            self.assertEqual(r['errors'][0]['path'], 'behaviors/enemy.gd')
            self.assertEqual(r['errors'][0]['line'], 44)
            self.assertIn('take_damage', r['errors'][0]['message'])

    def test_engine_log_flood_dedupes_but_keeps_raw_count(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, '.docmind_engine.log'), 'w', encoding='utf-8') as f:
                for _ in range(60):
                    f.write("SCRIPT ERROR: Invalid call. Nonexistent function 'take_damage' in base 'Nil'.\n")
                    f.write("   at: player.take_damage (res://behaviors/enemy.gd:44)\n")
            r = engine_logs(root)
            self.assertEqual(len(r['errors']), 1)
            self.assertEqual(r['error_count'], 60,
                             '同一错误重复刷屏时必须保留原始错误行数，不能只剩 1')

    def test_engine_log_contains_finds_marker_outside_tail(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, '.docmind_engine.log'), 'w', encoding='utf-8') as f:
                f.write("CLICK_PROBE CLICKED 961,540\n")
                for _ in range(120):
                    f.write("SCRIPT ERROR: Invalid call. Nonexistent function 'take_damage' in base 'Nil'.\n")
                    f.write("   at: player.take_damage (res://behaviors/enemy.gd:44)\n")
            tail = engine_logs(root, limit=20)
            self.assertNotIn('CLICK_PROBE', "\n".join(tail['lines']))
            found = engine_logs(root, limit=20, contains="CLICK_PROBE")
            self.assertEqual(found['match_count'], 1)
            self.assertIn('CLICK_PROBE CLICKED 961,540', found['matches'][0])

    def test_engine_log_summary_fields_survive_a_flooded_tail(self):
        """证据摘要必须排在 lines 前面：反思只保留观察前 1600 字。

        实测 `wf-4dd2abca90c9`：点击已落地、CLICK_PROBE 与 match_count 都在
        工具结果里，但 300 行 HEARTBEAT 把摘要挤出前 1600 字，反思判据只能
        看到心跳，于是把一条完全正确的结论判成「无证据」。
        """
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, '.docmind_engine.log'), 'w',
                      encoding='utf-8') as f:
                f.write("CLICK_PROBE CLICKED 600,450\n")
                for _ in range(300):
                    f.write("HEARTBEAT_FRAME steady\n")
            r = engine_logs(root, limit=200, contains="CLICK_PROBE")
            head = json.dumps(r, ensure_ascii=False)[:1600]
            self.assertIn('"marker_samples"', head)
            self.assertIn("CLICK_PROBE CLICKED 600,450", head)
            self.assertIn('"match_count"', head)
            at_lines = head.find('"lines":')
            if at_lines != -1:
                self.assertLess(head.find('"marker_samples"'), at_lines,
                                "summary 字段排到了 lines 后面，截断时会被挤掉")

    def test_gpu_serial_gate(self):
        self.assertTrue(acquire('test', 0.1)); self.assertEqual(status()['active'],'test'); release('test'); self.assertIsNone(status()['active'])

    def test_scene_tree_and_runtime_events(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, 'scenes'))
            with open(os.path.join(root,'scenes','main.tscn'),'w') as f:
                f.write('[node name="Main" type="Node3D"]\n[node name="Player" type="CharacterBody3D" parent="."]\n')
            tree = scene_tree(root, 'scenes/main.tscn')
            self.assertEqual([x['name'] for x in tree['nodes']], ['Main','Player'])
            runtime_events(root, [{'type':'method','name':'Player.take_damage'}])
            self.assertEqual(runtime_events(root)['events'][0]['type'], 'method')

if __name__ == '__main__': unittest.main()
