# -*- coding: utf-8 -*-
"""FastAPI 路由注册的护栏测试。

**为什么需要它**：同路径同方法被注册两次时，FastAPI 不会报错——请求会命中**先注册**的那个，
后写的处理器变成永远收不到请求的死代码。这种问题语法检查、导入检查、单元测试全都看不出来，
只有在真机上点到那个端点的行为跟预期不一样时才暴露，极难定位。

真实案例（2026-09-14）：`/api/engine/embed` 曾同时存在"支持引擎视窗矩形"的新版和不支持 rect 的旧版，
旧版先注册 → 新版死掉，但一切"看起来"都正常。

另外顺手守住几个关键端点的存在性与请求模型字段，防止被"更窄的旧实现"顶替。
"""
import collections
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402


def _routes():
    out = []
    for route in api.app.routes:
        path = getattr(route, 'path', '')
        if not path:
            continue
        for method in sorted(getattr(route, 'methods', []) or []):
            if method in ('HEAD', 'OPTIONS'):
                continue
            out.append((path, method))
    return out


class ApiRouteTests(unittest.TestCase):
    def test_workflow_choice_keeps_execution_behind_user_approval(self):
        from starlette.testclient import TestClient
        from api_routes import agent as agent_routes

        with patch.object(agent_routes.WORKFLOWS, 'choose', return_value={
                'workflow_id': 'wf-gate-test', 'status': 'planning'}) as choose:
            response = TestClient(api.app).post(
                '/api/agent/workflow/wf-gate-test/choice', json={'choice': 'opt_1'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json().get('ok'))
        self.assertFalse(choose.call_args.kwargs['auto_execute'])

    def test_no_duplicate_path_and_method(self):
        """同路径+同方法只能注册一次；重复会让后注册的处理器静默失效。"""
        counter = collections.Counter(_routes())
        dupes = sorted(k for k, c in counter.items() if c > 1)
        self.assertEqual(dupes, [], '存在重复注册的路由（后者永远收不到请求）：%s' % dupes)

    def test_key_endpoints_present(self):
        routes = set(_routes())
        for path, method in [
            ('/api/chat', 'POST'),
            ('/api/ingest', 'POST'),
            ('/api/engine/start', 'POST'),
            ('/api/engine/stop', 'POST'),
            ('/api/engine/reload', 'POST'),
            ('/api/engine/changes', 'GET'),
            ('/api/engine/embed', 'POST'),
            ('/api/engine/place', 'POST'),
            ('/api/engine/detach', 'POST'),
            ('/api/engine/focus', 'POST'),
            ('/api/desktop/host', 'GET'),
            ('/api/desktop/host', 'POST'),
            ('/api/scene/graph', 'GET'),
            ('/api/scene/op', 'POST'),
            ('/api/runtime/events', 'GET'),
            ('/api/runtime/clear', 'POST'),
            ('/api/comfy/start', 'POST'),
            ('/api/comfy/stop', 'POST'),
        ]:
            self.assertIn((path, method), routes, '端点缺失：%s %s' % (method, path))

    def test_engine_embed_model_supports_viewport_rect(self):
        """嵌入请求模型必须带 rect 四要素与 offset_y。

        如果哪天被"不支持矩形"的窄版本顶替，前端发过去的 x/y/width/height 会被忽略，
        引擎就会按铺满模式摆放——画面盖住整个界面，而接口仍返回 ok:true。
        """
        fields = set(getattr(api.EngineEmbedReq, 'model_fields', {}) or {})
        for name in ('host_hwnd', 'x', 'y', 'width', 'height', 'offset_y', 'title_hint'):
            self.assertIn(name, fields, 'EngineEmbedReq 缺少字段：%s' % name)

    def test_engine_start_model_accepts_rect(self):
        """`/api/engine/start` 要能一次带走 rect（UI 的「嵌入工作台」就靠这个）。"""
        fields = set(getattr(api.EngineReq, 'model_fields', {}) or {})
        self.assertIn('rect', fields)
        self.assertIn('embed', fields)
        self.assertIn('host_hwnd', fields)

    def test_no_duplicate_top_level_definitions(self):
        """顶层类/函数不能重名——后者会静默覆盖前者。

        api.py 里曾经有两次 `class EngineEmbedReq`（完整版在前、窄版在后），
        覆盖后处理器读 `req.x` 直接 AttributeError；而当时还有个重复的旧处理器在生效，
        所以这个错被完全掩盖，直到把重复处理器删掉才暴露出来。
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ('api.py', 'game_workbench.py', 'tools.py', 'agent.py',
                     'scene_runtime.py', 'workbench_fs.py', 'flows.py'):
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                continue
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read(), path)
            defined = [node.name for node in tree.body
                       if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            dupes = sorted(k for k, c in collections.Counter(defined).items() if c > 1)
            self.assertEqual(dupes, [], '%s 存在重复的顶层定义（后者会覆盖前者）：%s' % (name, dupes))

    def test_engine_embed_endpoint_binds_rect_payload(self):
        """真打一次 `/api/engine/embed`：模型绑定 + 处理器执行，不允许 500。

        用"没有引擎在跑"这个必然失败但**合法**的路径来验证——它证明处理器真的执行到了
        `engine_embed()`，而不是在 `req.x` 上抛 AttributeError。
        """
        import tempfile

        import config
        from starlette.testclient import TestClient

        previous = config.get_runtime('code_root')
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config.set_runtime('code_root', tmp)
                # 本文件守的是「路由/处理器真的执行」，不是 root 解析逻辑。分片乱序时
                # 别处残留的项目/会话上下文会抢占 _project_root_or_error（返回已清理的
                # 临时目录 → 「未配置代码库」）。这里钉死返回 tmp，隔离环境态，
                # 保证走到 engine_embed()。
                with patch.object(api, '_project_root_or_error', return_value=tmp):
                    client = TestClient(api.app)
                    resp = client.post('/api/engine/embed', json={
                        'host_hwnd': 1, 'x': 10, 'y': 20, 'width': 100, 'height': 50, 'offset_y': -1})
                self.assertNotEqual(resp.status_code, 500, resp.text)
                body = resp.json()
                self.assertFalse(body.get('ok'))
                self.assertIn('引擎', body.get('error', ''),
                              '期望走到"引擎尚未运行"，实际：%s' % body)
        finally:
            config.set_runtime('code_root', previous or '')

    def test_sse_streams_without_extra_gpu_lease(self):
        """问答 SSE 层不得自己申请 GPU 租约（P1-3 合并抓出的致命自锁）。

        llm.py 的 _ollama_chat 已以 owner='ollama' 持租约；SSE 再以 ollama:chat
        申请，serial 模式不同 owner 不可重入 → 每次等 2s 后必报"GPU 正忙"。
        这里把 ollama 探活与 agent.run 全部打桩：只要 SSE 流能正常吐出 token、
        协调器里没有残留 holder，就证明锁不在 SSE 层。
        """
        import config
        from starlette.testclient import TestClient

        old_provider = config.get_runtime('llm_provider')
        config.set_runtime('llm_provider', 'ollama')
        try:
            with patch.object(
                    api, 'check_ollama', return_value={'reachable': True, 'guidance': ''}), \
                 patch.object(
                    api.Agent, 'run', return_value=iter([
                        {'type': 'token', 'text': '正常回答'},
                        {'type': 'final', 'text': '正常回答'},
                    ])):
                client = TestClient(api.app)
                resp = client.post('/api/chat', data={'question': '你好'})
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.text
            self.assertIn('正常回答', body)
            self.assertNotIn('GPU 正忙', body)
            holders = api.gpu_status().get('holders') or []
            self.assertEqual(holders, [], 'SSE 结束后残留租约：%s' % holders)
        finally:
            config.set_runtime('llm_provider', old_provider or '')

    def test_no_ollama_lease_in_sse_sources(self):
        """静态护栏：两个 event_stream 里不得再出现 ollama:* 的二次租约 owner。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'api.py'), encoding='utf-8') as f:
            source = f.read()
        self.assertNotIn("gpu_acquire('ollama:chat'", source)
        self.assertNotIn("gpu_acquire('ollama:selection'", source)
        self.assertNotIn('gpu_release', source)

    def test_comfy_provenance_route(self):
        from starlette.testclient import TestClient
        c=TestClient(api.app)
        good=c.post('/api/comfy/provenance/validate', json={'author':'a','license':'MIT','source_url':'https://example.com'})
        self.assertTrue(good.json()['ok']); self.assertFalse(good.json()['review_required'])
        bad=c.post('/api/comfy/provenance/validate', json={'source_url':'javascript:bad'})
        self.assertFalse(bad.json()['ok'])


if __name__ == '__main__':
    unittest.main()
