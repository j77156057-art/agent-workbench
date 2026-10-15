# -*- coding: utf-8 -*-
"""T5：工作流领域画像（generic/game）与 kind 字段（离线）。

- TR-5.1 use_llm=false：generic 启动的选项/兜底 DAG 不含游戏措辞；
  game 与改造前逐字段一致（快照）。
- TR-5.2 旧无 kind 状态 JSON 经 _load 迁移为 kind=="game"，可继续 choose/plan。
- TR-5.3 API：不传 kind → generic；非法 kind 回退 generic，不报错。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_runtime.game_workflow import GameWorkflowManager  # noqa: E402
from agent_runtime.workflow_profiles import (  # noqa: E402
    GENERIC, GAME, PROFILES, VALID_KINDS, get_profile, normalize_kind)

GAME_WORDS = ("游戏", "玩法", "关卡", "Playtest")

GAME_OPTION_SNAPSHOT = [
    {"id": "recommended", "title": "按 3D 游戏原型推进",
     "summary": "先建立最小可运行原型，再按验证结果迭代。",
     "recommended": True, "source": "direct", "requires_web": False},
    {"id": "design_first", "title": "先完成设计与技术方案",
     "summary": "先拆玩法、场景、数据和验证标准，再开始改文件。",
     "recommended": False, "source": "local", "requires_web": False},
    {"id": "web_research", "title": "联网补充资料后再选",
     "summary": "搜索引擎/插件/最新资料，再重新生成方案选项。",
     "recommended": False, "source": "web", "requires_web": True},
    {"id": "custom", "title": "我自己描述目标",
     "summary": "由用户补充更具体的效果、限制或参考作品。",
     "recommended": False, "source": "user", "requires_web": False},
]

GAME_FALLBACK_SNAPSHOT = [
    {"id": "design", "role": "designer", "task": "明确玩法、场景、输入和验收标准",
     "depends_on": []},
    {"id": "prototype", "role": "coder", "task": "创建最小可运行游戏原型",
     "depends_on": ["design"]},
    {"id": "verify", "role": "tester", "task": "执行自测、Playtest 并反馈失败证据",
     "depends_on": ["prototype"]},
]


class ProfileRegistryTests(unittest.TestCase):
    def test_registry_contents(self):
        self.assertEqual(set(VALID_KINDS), {"generic", "game", "eda"})
        self.assertIs(get_profile("generic"), GENERIC)
        self.assertIs(get_profile("game"), GAME)

    def test_unknown_kind_falls_back_to_generic(self):
        for raw in (None, "", "hw", 123):
            self.assertIs(get_profile(raw), GENERIC)
        self.assertEqual(normalize_kind("GAME"), "game")
        self.assertEqual(normalize_kind(" game "), "game")
        self.assertEqual(normalize_kind("eda"), "eda")


class GenericProfileContentTests(unittest.TestCase):
    def test_generic_options_have_no_game_wording(self):
        for sources in (("direct",), ("local",), ("web",)):
            rows = GENERIC.option_dicts("做一个 EDA 元件库批量检查工具", sources)
            ids = [row["id"] for row in rows]
            # web 已在来源中时不再给 web_research 选项
            self.assertNotIn("web_research", ids if "web" in sources else [])
            blob = json.dumps(rows, ensure_ascii=False)
            for word in GAME_WORDS:
                self.assertNotIn(word, blob, "generic 选项含游戏措辞 %r：%s" % (word, blob))
        # 恰好一个推荐项
        rows = GENERIC.option_dicts("任意任务", ("direct",))
        self.assertEqual(sum(1 for r in rows if r["recommended"]), 1)
        self.assertEqual(rows[0]["id"], "recommended")
        self.assertEqual(rows[0]["title"], "最小可运行版本先行")

    def test_generic_fallback_dag_is_domain_neutral(self):
        tasks = GENERIC.fallback_tasks()
        self.assertEqual([t["id"] for t in tasks], ["research", "implement", "verify"])
        self.assertEqual([t["role"] for t in tasks],
                         ["researcher", "coder", "tester"])
        blob = json.dumps(tasks, ensure_ascii=False)
        for word in GAME_WORDS:
            self.assertNotIn(word, blob)

    def test_generic_prompts_forbid_game_wording(self):
        self.assertIn("禁止出现「游戏」", GENERIC.option_instruction)
        self.assertIn("禁止出现「游戏」", GENERIC.task_instruction)
        self.assertNotIn("designer", GENERIC.task_roles)
        self.assertNotIn("artist", GENERIC.task_roles)


class GameSnapshotTests(unittest.TestCase):
    """game 画像必须与改造前逐字段一致。"""

    def test_game_options_match_snapshot(self):
        rows = GAME.option_dicts("做一个打怪小游戏", ("direct",))
        self.assertEqual(rows, GAME_OPTION_SNAPSHOT)

    def test_game_2d_detection_unchanged(self):
        rows = GAME.option_dicts("2d 横版像素", ("direct",))
        self.assertEqual(rows[0]["title"], "按 2D 游戏原型推进")

    def test_game_web_option_inserted_as_before(self):
        rows = GAME.option_dicts("3d 游戏", ("local",))
        self.assertEqual([r["id"] for r in rows],
                         ["recommended", "design_first", "web_research", "custom"])
        web = next(r for r in rows if r["id"] == "web_research")
        self.assertTrue(web["requires_web"])
        self.assertEqual(web["source"], "web")

    def test_game_fallback_matches_snapshot(self):
        self.assertEqual(GAME.fallback_tasks(), GAME_FALLBACK_SNAPSHOT)


class ManagerProfileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.manager = GameWorkflowManager(tempfile.mkdtemp())

    def test_generic_start_and_fallback_plan(self):
        started = self.manager.start("做一个 EDA 网表批量校验脚本")
        self.assertEqual(started["kind"], "generic")
        blob = json.dumps(started["options"], ensure_ascii=False)
        for word in GAME_WORDS:
            self.assertNotIn(word, blob)
        wid = started["workflow_id"]
        chosen = self.manager.choose(wid, "recommended")
        self.assertEqual(chosen["status"], "planning")
        planned = self.manager.plan(wid)
        self.assertEqual([t["id"] for t in planned["tasks"]],
                         ["research", "implement", "verify"])
        self.assertEqual([t["role"] for t in planned["tasks"]],
                         ["researcher", "coder", "tester"])

    def test_game_start_matches_legacy_behavior(self):
        started = self.manager.start("做一个打怪小游戏", kind="game")
        self.assertEqual(started["kind"], "game")
        self.assertEqual(started["options"], GAME_OPTION_SNAPSHOT)
        wid = started["workflow_id"]
        self.manager.choose(wid, "recommended")
        planned = self.manager.plan(wid)
        self.assertEqual(
            [{k: t[k] for k in ("id", "role", "task", "depends_on")}
             for t in planned["tasks"]],
            GAME_FALLBACK_SNAPSHOT)

    def test_unknown_kind_normalized_at_start(self):
        started = self.manager.start("随便什么长任务", kind="wat")
        self.assertEqual(started["kind"], "generic")

    def test_generic_custom_request_regenerates_neutral_options(self):
        started = self.manager.start("先随便做点东西")
        wid = started["workflow_id"]
        chosen = self.manager.choose(
            wid, "custom", custom_request="改成批量处理 BOM 物料清单")
        blob = json.dumps(chosen["options"], ensure_ascii=False)
        for word in GAME_WORDS:
            self.assertNotIn(word, blob)

    def test_legacy_state_without_kind_migrates_to_game(self):
        started = self.manager.start("旧版游戏工作流", kind="game")
        wid = started["workflow_id"]
        path = self.manager._path(wid)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("kind", raw)
        del raw["kind"]
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.manager._states.pop(wid, None)

        loaded = self.manager._load(wid)
        self.assertEqual(loaded.kind, "game")
        # 迁移后仍可正常推进 choose/plan，且沿用游戏兜底
        chosen = self.manager.choose(wid, "recommended")
        self.assertEqual(chosen["status"], "planning")
        planned = self.manager.plan(wid)
        self.assertEqual([t["id"] for t in planned["tasks"]],
                         ["design", "prototype", "verify"])


class EdaProfileTests(unittest.TestCase):
    def test_eda_registered(self):
        self.assertEqual(normalize_kind("eda"), "eda")
        self.assertIs(get_profile("eda"), PROFILES["eda"])

    def test_eda_options_keywords(self):
        rows = PROFILES["eda"].option_dicts("做一块 STM32 控制板", ("direct",))
        self.assertEqual([r["id"] for r in rows],
                         ["recommended", "schematic_pcb", "web_research", "custom"])
        blob = json.dumps(rows, ensure_ascii=False)
        for word in ("ERC", "DRC", "原理图", "PCB", "元件库"):
            self.assertIn(word, blob, "EDA 选项缺少关键词：%s" % word)
        self.assertTrue(rows[0]["recommended"])

    def test_eda_fallback_dag_contract(self):
        tasks = PROFILES["eda"].fallback_tasks()
        self.assertEqual([t["id"] for t in tasks],
                         ["research", "schematic", "layout", "verify"])
        self.assertEqual([t["role"] for t in tasks],
                         ["researcher", "schematic", "layout", "tester"])
        self.assertEqual([t["depends_on"] for t in tasks],
                         [[], ["research"], ["schematic"], ["layout"]])
        by_id = {t["id"]: t for t in tasks}
        # 连接器操作阶段全部 mcp=allow，调研阶段 deny
        self.assertEqual(by_id["research"]["mcp"], "deny")
        for tid in ("schematic", "layout", "verify"):
            self.assertEqual(by_id[tid]["mcp"], "allow", tid)
        connector_trio = {"dev_route_connector", "dev_list_connector_tools", "dev_mcp_call"}
        for tid in ("schematic", "layout", "verify"):
            self.assertTrue(connector_trio.issubset(set(by_id[tid]["tools"])),
                            "%s 缺连接器三件套：%r" % (tid, by_id[tid]["tools"]))
        self.assertIn("read_file", by_id["schematic"]["tools"])
        self.assertIn("python_exec", by_id["verify"]["tools"])
        # 任务必须要求先核实连接器工具名
        self.assertIn("dev_list_connector_tools", by_id["schematic"]["task"])
        # 验收可检验：DRC 零错误或列明豁免
        self.assertIn("DRC", by_id["verify"]["task"])

    def test_eda_task_hint_requires_explicit_tools_whitelist(self):
        hint = PROFILES["eda"].task_instruction
        for word in ("EDA", "显式 tools 白名单", "dev_list_connector_tools",
                     "mcp", "ERC/DRC", "豁免"):
            self.assertIn(word, hint)
        self.assertIn("schematic", PROFILES["eda"].task_roles)
        self.assertIn("layout", PROFILES["eda"].task_roles)

    def test_eda_subagent_roles_cover_task_tools(self):
        import agent as agent_mod
        # 兜底 DAG 每个任务的 tools 白名单必须是对应子代理角色工具的子集，
        # 否则 _run_child 的收窄逻辑会把连接器工具洗掉。
        for task in PROFILES["eda"].fallback_tasks():
            role = agent_mod._SUBAGENT_ROLES[task["role"]]
            missing = set(task.get("tools", [])) - set(role["tools"])
            self.assertFalse(missing, "角色 %s 缺少任务工具：%s" % (task["role"], missing))

    def test_eda_manager_fallback_plan(self):
        manager = GameWorkflowManager(tempfile.mkdtemp())
        started = manager.start("把一块 STM32 控制板从原理图带到可投产 PCB", kind="eda")
        self.assertEqual(started["kind"], "eda")
        wid = started["workflow_id"]
        manager.choose(wid, "recommended")
        planned = manager.plan(wid)
        self.assertEqual([t["id"] for t in planned["tasks"]],
                         ["research", "schematic", "layout", "verify"])


class WorkflowStartApiKindTests(unittest.TestCase):
    """TR-5.3：API 缺省/非法 kind 行为。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api_routes.agent import build_router
        import projects as projects_mod

        # 2026-10-15：`launch_workflow` 的启动一致性校验（10-14 加的）要求
        # 请求头 project_id 与 root 反查得到的 project_id **必须一致**。
        # 旧 fake 每次调 `_project_root_or_error()` 都返回一个新临时目录、
        # 又硬编码 `_request_project_id() == "p-test"`，于是每发一次请求都被判
        # 「project_id 与 project_root 不匹配」→ ok:false，三个用例全红。
        # `projects.project_id(root)` 是纯函数（prj-sha1(路径)），不碰注册表，
        # 所以让它俩同源即满足校验，且不污染任何全局状态。
        class _FakeCtx:
            def __init__(self):
                self._root = tempfile.mkdtemp()
                self._pid = projects_mod.project_id(self._root)

            def _project_root_or_error(self):
                return self._root

            def _request_project_id(self):
                return self._pid

        app = FastAPI()
        app.include_router(build_router(_FakeCtx()))
        self.client = TestClient(app)

    def _post(self, payload):
        from api_routes import agent as agent_routes

        captured = {}

        def fake_start(request, **kwargs):
            captured.update(kwargs)
            captured["request"] = request
            return {"workflow_id": "wf-test", "status": "awaiting_choice",
                    "options": []}

        stub = type("_WF", (), {"start": staticmethod(fake_start)})()
        with patch.object(agent_routes, "WORKFLOWS", stub):
            resp = self.client.post("/api/agent/workflow/start", json=payload)
        return resp, captured

    def test_missing_kind_defaults_generic(self):
        resp, captured = self._post({"prompt": "长任务", "use_llm": False})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(captured["kind"], "generic")

    def test_invalid_kind_falls_back_generic(self):
        resp, captured = self._post(
            {"prompt": "长任务", "use_llm": False, "kind": "nonsense"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(captured["kind"], "generic")

    def test_game_kind_passthrough(self):
        resp, captured = self._post(
            {"prompt": "做个游戏", "use_llm": False, "kind": "game"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(captured["kind"], "game")


if __name__ == "__main__":
    unittest.main()
