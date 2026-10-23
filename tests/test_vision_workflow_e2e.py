# -*- coding: utf-8 -*-
"""T9：跨模块离线 e2e（不触外网、不触真实窗口）。

四条端到端链路：
1. 本地 HTML 夹具 + web_fetch 工具 → native 视觉模型的 Observation 消息确实带图；
2. 同一链路在非视觉模型下，所有发给模型的消息都不含 image content（红线）；
3. tester 子代理经 delegate 同款 _run_child 调 game_screenshot（假抓帧）→
   截图进入子代理观察消息，结论回流给父代理；
4. start_workflow(generic) 停在方案选择门（无任务/无执行）；EDA 规划产出的
   DAG 任务字段（role/mcp/tools/depends_on）经 manager 端到端可执行。
"""
import io
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent as agent_mod  # noqa: E402
import tools as tools_mod  # noqa: E402
from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402
from agent_runtime.tools import ToolResult  # noqa: E402


def _png(color, size=(120, 80)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


_PAGE = """<html><head>
<meta property="og:image" content="/og.png">
<title>Godot 报错排查</title></head><body>
<img src="/img/godot-error-screenshot.png" alt="Godot fatal error 报错弹窗截图">
</body></html>"""

_SHOT_DATA_URL = "data:image/jpeg;base64,QUJDREVGRw=="  # 替身帧，视觉层只搬运不校验像素


def _act(tool, inp):
    return f"Thought: t\nAction: {tool}\nAction Input: {inp}"


class _ScriptedLLM:
    provider = "fake"
    model = "fake-e2e"

    def __init__(self, scripts, capability, sink=None):
        self._scripts = list(scripts)
        self.calls = 0
        self.batches = []
        self._sink = sink if sink is not None else []
        self.last_usage = {"prompt_tokens": 1, "completion_tokens": 1}
        self.capability = capability

    def chat(self, messages, stream=True, **kwargs):
        self.batches.append(messages)
        self._sink.append(messages)
        script = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        if stream:
            def g():
                for ch in script:
                    yield ch
            return g()
        return script

    def count_tokens(self, text):
        return 0

    def clone(self):
        # 子代理拿独立游标，但共享 sink 供断言检查它实际收到的消息。
        return _ScriptedLLM(self._scripts, self.capability, self._sink)


class _PageHandler(BaseHTTPRequestHandler):
    pages = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in self.pages:
            self.send_response(404)
            self.end_headers()
            return
        ctype, body = self.pages[path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WebFetchVisionE2ETests(unittest.TestCase):
    server = thread = None
    base = ""

    @classmethod
    def setUpClass(cls):
        _PageHandler.pages = {
            "/page.html": ("text/html; charset=utf-8", _PAGE.encode("utf-8")),
            "/og.png": ("image/png", _png((255, 0, 0))),
            "/img/godot-error-screenshot.png": ("image/png", _png((0, 200, 0))),
        }
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _observation_messages(self, batches):
        return [m for batch in batches for m in batch
                if isinstance(m, dict) and m.get("role") == "user"
                and "Observation:" in (m.get("content") or "")]

    def test_native_model_receives_web_images_end_to_end(self):
        llm = _ScriptedLLM(
            [_act("web_fetch", self.base + "/page.html"),
             "Thought: done\nFinal Answer: 已结合截图确认报错样式。"],
            {"context_window": 32768, "vision": "native"})
        agent = agent_mod.Agent(
            llm=llm,
            tool_registry={"web_fetch": {"description": "抓网页",
                                         "func": tools_mod.web_fetch}})
        agent.web_enabled = True
        with patch.object(tools_mod, "_image_host_blocked", lambda host: False), \
                patch.object(tools_mod, "_web_images_enabled", lambda: True):
            events = list(agent.run("查一下这个报错页", stream=True))
        self.assertTrue(any(e.get("type") == "final" for e in events))
        obs = self._observation_messages(llm.batches)
        self.assertEqual(len(obs), 1)
        images = obs[0].get("images") or []
        self.assertTrue(images, "native 模型的网页观察必须携带图片")
        self.assertTrue(all(str(i).startswith("data:image/jpeg;base64,") for i in images))
        self.assertIn("含", obs[0]["content"])

    def test_non_vision_model_never_gets_web_images(self):
        os.environ.pop("DOCMIND_VISION_MODEL", None)
        llm = _ScriptedLLM(
            [_act("web_fetch", self.base + "/page.html"),
             "Thought: done\nFinal Answer: 仅依据文本回答。"],
            {"context_window": 32768, "vision": "none"})
        agent = agent_mod.Agent(
            llm=llm,
            tool_registry={"web_fetch": {"description": "抓网页",
                                         "func": tools_mod.web_fetch}})
        agent.web_enabled = True
        with patch.object(tools_mod, "_image_host_blocked", lambda host: False), \
                patch.object(tools_mod, "_web_images_enabled", lambda: False):
            list(agent.run("查一下这个报错页", stream=True))
        for batch in llm.batches:
            for msg in batch:
                if isinstance(msg, dict):
                    self.assertNotIn("images", msg)


class TesterScreenshotE2ETests(unittest.TestCase):
    def test_tester_child_observation_image_flows_into_conclusion(self):
        sink = []

        def fake_screenshot(_arg):
            return ToolResult(
                ok=True, text="截图完成：320x240，来源 foreground",
                data={"images": [_SHOT_DATA_URL],
                      "image_sources": ["foreground://fake-shot.jpg"]})

        llm = _ScriptedLLM(
            [_act("game_screenshot", "target: foreground"),
             "Thought: shot\nFinal Answer: 截图中看到红色 Fatal Error 弹窗，判定运行异常。"],
            {"context_window": 32768, "vision": "native"}, sink)
        parent = agent_mod.Agent(
            llm=llm,
            tool_registry={"game_screenshot": {"description": "游戏截图",
                                               "func": fake_screenshot}})
        result = parent._run_child(
            "tester", "启动游戏后截图判断当前画面是否有报错弹窗", reflect=False)
        self.assertEqual(result["status"], "ok", result.get("error"))
        self.assertIn("Fatal Error", result["conclusion"])
        obs = [m for batch in sink for m in batch
               if isinstance(m, dict) and m.get("role") == "user"
               and "Observation:" in (m.get("content") or "")]
        self.assertTrue(obs, "子代理必须实际收到截图观察消息")
        self.assertEqual(obs[0].get("images"), [_SHOT_DATA_URL])


class WorkflowGateE2ETests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def test_generic_start_parks_without_execution(self):
        started = self.manager.start(
            "为某模块补齐实现与自动化验证", kind="generic",
            llm_enabled=False, project_root=self.root)
        wid = started["workflow_id"]
        state = self.manager.get(wid)
        self.assertEqual(state["kind"], "generic")
        self.assertIn(state["status"], ("awaiting_choice", "generating_options"))
        # 人工门之前：无任务 DAG、无任何子代理执行痕迹
        self.assertEqual(state["tasks"], [])
        self.assertFalse(state.get("results"))
        # 选方案后仍停在任务审批门，不直接执行
        self.manager.choose(wid, "recommended")
        state = self.manager.get(wid)
        self.assertFalse(state.get("results"))

    def test_eda_plan_dag_fields_round_trip(self):
        started = self.manager.start(
            "把一块 STM32 控制板从原理图带到可投产 PCB", kind="eda",
            llm_enabled=False, project_root=self.root)
        wid = started["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid)
        tasks = planned["tasks"]
        # 连接器读取（schematic）为根任务，research 并行可选——
        # 见 wf-b087d3f6b77f：schematic 挂在 research 之后会被 research 的空
        # 结论污染、导致子代理 steps=0。修好后 schematic 排最前、depends_on 为空。
        self.assertEqual([t["id"] for t in tasks],
                         ["schematic", "research", "layout", "verify", "report"])
        trio = {"dev_route_connector", "dev_list_connector_tools", "dev_mcp_call"}
        for task in tasks:
            self.assertTrue({"id", "role", "task", "tools", "mcp", "depends_on"}
                            <= set(task), task)
        self.assertEqual([t["role"] for t in tasks],
                         ["schematic", "researcher", "layout", "tester", "reporter"])
        by_id = {t["id"]: t for t in tasks}
        self.assertEqual(by_id["research"]["mcp"], "deny")
        self.assertTrue(by_id["research"].get("optional"),
                        "research 必须是并行可选，失败不得阻断连接器任务")
        self.assertEqual(by_id["schematic"]["depends_on"], [],
                         "schematic 连接器读取必须是根任务")
        for tid in ("schematic", "layout", "verify"):
            self.assertEqual(by_id[tid]["mcp"], "allow", tid)
            self.assertTrue(trio.issubset(set(by_id[tid]["tools"])), tid)
        self.assertEqual(by_id["verify"]["depends_on"], ["layout"])
        # layout/verify 可选：其失败不得阻断末尾 report 收尾节点
        self.assertTrue(by_id["layout"].get("optional"))
        self.assertTrue(by_id["verify"].get("optional"))
        self.assertEqual(by_id["report"]["role"], "reporter")
        self.assertEqual(by_id["report"]["depends_on"],
                         ["schematic", "research", "layout", "verify"])
        self.assertEqual(by_id["report"]["mcp"], "deny")
        # 规划完成仍未开始执行
        state = self.manager.get(wid)
        self.assertFalse(state.get("results"))


if __name__ == "__main__":
    unittest.main()
