import unittest, urllib.request
from unittest.mock import patch
import game_workbench as gw

# Canned INPUT_TYPES for the synthetic subgraph nodes used below.  The real
# ComfyUI converter only needs to know which inputs are widgets vs connections
# vs autogrow; we never hit the network in these unit cases.
_FAKE_OBJINFO = {
    'PrimitiveFloat': {'input': {'required': {'value': ['FLOAT']}}},
    'MathExpr': {'input': {'required': {'expression': ['STRING']},
                           'optional': {'values': ['COMFY_AUTOGROW_V3']}}},
    'Sink': {'input': {'required': {'length': ['INT']}}},
}

def fake_object_info(node_type, url):
    return _FAKE_OBJINFO.get(str(node_type), {})

def make_subgraph_ui():
    """Synthetic UI workflow mirroring the H3 shape that broke the converter:
    a subgraph instance whose interface slot drives a PrimitiveFloat -> an
    autogrow ComfyMathExpression (values.a) -> a Sink.length connection."""
    sub_id = 'sub-x'
    return {
        'nodes': [
            {'id': 105, 'type': sub_id,
             'inputs': [{'name': 'value_1', 'link': None},
                        {'name': 'prompt', 'link': None}],
             'widgets_values': [1.0, 'a prompt']},
        ],
        'links': [],
        'definitions': {
            'subgraphs': [{
                'id': sub_id,
                'inputs': [{'name': 'value_1', 'type': 'FLOAT'},
                           {'name': 'prompt', 'type': 'STRING'}],
                'outputs': [{'name': 'OUT', 'linkIds': []}],
                'nodes': [
                    # PrimitiveFloat whose `value` comes from the subgraph
                    # interface slot 0 (== the instance's value_1 widget).
                    {'id': 111, 'type': 'PrimitiveFloat',
                     'inputs': [{'name': 'value', 'link': 'L_val'}],
                     'widgets_values': [999],
                     'outputs': [{'name': 'FLOAT'}]},
                    # ComfyMathExpression: `expression` is a widget, `values`
                    # is autogrow with sub-input values.a linked to 111.
                    {'id': 107, 'type': 'MathExpr',
                     'inputs': [{'name': 'expression', 'link': None},
                                {'name': 'values.a', 'link': 'L_a'}],
                     'widgets_values': ['a*2'],
                     'outputs': [{'name': 'FLOAT'}, {'name': 'INT'}, {'name': 'BOOL'}]},
                    # Sink: `length` is a pure connection from MathExpr INT.
                    {'id': 104, 'type': 'Sink',
                     'inputs': [{'name': 'length', 'link': 'L_len'}],
                     'widgets_values': [0],
                     'outputs': []},
                ],
                'links': [
                    {'id': 'L_val', 'origin_id': '-10', 'origin_slot': 0,
                     'target_id': '111', 'target_slot': 0},
                    {'id': 'L_a', 'origin_id': '111', 'origin_slot': 0,
                     'target_id': '107', 'target_slot': 0},
                    {'id': 'L_len', 'origin_id': '107', 'origin_slot': 1,
                     'target_id': '104', 'target_slot': 0},
                ],
            }]
        }
    }

class TestComfyH3Converter(unittest.TestCase):
    @patch.object(gw, '_comfy_object_info', side_effect=fake_object_info)
    def test_subgraph_flatten_and_interface_resolution(self, _m):
        r = gw.comfy_ui_to_api_workflow(make_subgraph_ui())
        self.assertTrue(r['ok'], r.get('error'))
        wf = r['workflow']
        # exactly the three inner nodes are flattened out
        self.assertEqual(len(wf), 3, list(wf.keys()))
        ct = {n['class_type']: n for n in wf.values()}
        # interface slot value (1.0) propagated to PrimitiveFloat.value,
        # NOT the inner widget default 999
        pf = ct['PrimitiveFloat']
        self.assertEqual(pf['inputs'].get('value'), 1.0)
        # autogrow sub-input emitted and connected to the expanded primitive
        me = ct['MathExpr']
        self.assertIn('values.a', me['inputs'])
        self.assertIsInstance(me['inputs']['values.a'], list)
        self.assertEqual(len(me['inputs']['values.a']), 2)
        # Sink.length is a connection to the expanded MathExpr INT output
        sink = ct['Sink']
        self.assertIn('length', sink['inputs'])
        self.assertIsInstance(sink['inputs']['length'], list)

    @patch.object(gw, '_comfy_object_info', side_effect=fake_object_info)
    def test_legacy_uuid_alias_resolves_inside_subgraph(self, _m):
        ui = make_subgraph_ui()
        # promote the alias node type into the subgraph to ensure alias
        # resolution works for flattened (not just top-level) nodes
        sub = ui['definitions']['subgraphs'][0]
        sub['nodes'].append({'id': 130, 'type': '4c314f31-ecda-4b08-ae98-faaba1bf613f',
                             'inputs': [], 'widgets_values': [], 'outputs': []})
        r = gw.comfy_ui_to_api_workflow(ui)
        self.assertTrue(r['ok'])
        ct = {n['class_type'] for n in r['workflow'].values()}
        self.assertIn('TESpeedMiniMaxH3', ct)

    def test_real_h3_ui_workflow_converts_when_comfyui_up(self):
        # Best-effort: only meaningful against a live ComfyUI that serves the
        # real H3 node INPUT_TYPES.  Skipped in offline CI.
        try:
            with urllib.request.urlopen("http://127.0.0.1:8188/queue", timeout=3):
                pass
        except Exception:
            self.skipTest("ComfyUI not reachable")
        tpl = gw.comfy_template_workflow('minimax-h3-i2v')
        if not tpl.get('ok'):
            self.skipTest("H3 workflow not found")
        wf = tpl['workflow']
        n105 = [n for n in wf['nodes'] if str(n.get('id')) == '105'][0]
        n105['widgets_values'][3] = 1.0  # shorten length a -> 39 frames
        r = gw.comfy_ui_to_api_workflow(wf)
        self.assertTrue(r['ok'], r.get('error'))
        ct = {n['class_type'] for n in r['workflow'].values()}
        # node 105 is the subgraph *instance* (type == subgraph id), so it is
        # flattened rather than emitted as a TESpeedMiniMaxH3 node; the real
        # generation nodes must all be present instead.
        for need in ('MiniMaxH3ImageToVideo', 'ComfyMathExpression',
                     'PrimitiveFloat', 'SaveVideo', 'ResolutionSelector'):
            self.assertIn(need, ct)
        me = [n for n in r['workflow'].values() if n['class_type'] == 'ComfyMathExpression']
        self.assertTrue(me, "ComfyMathExpression missing")
        self.assertIn('values.a', me[0]['inputs'])
        # shortened length a (interface slot 5 -> PrimitiveFloat.value) propagated
        pf = [n for n in r['workflow'].values() if n['class_type'] == 'PrimitiveFloat']
        self.assertEqual(pf[0]['inputs'].get('value'), 1.0)

if __name__ == '__main__':
    unittest.main()
