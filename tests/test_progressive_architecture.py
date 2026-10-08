import unittest

from agent_runtime.context_router import ContextRouter
from agent_runtime.tools import Capability, SideEffect, ToolSpec
from tools import TOOLS, tool_schemas


class ProgressiveArchitectureTests(unittest.TestCase):
    def test_registry_is_fully_typed(self):
        self.assertTrue(TOOLS)
        self.assertTrue(all(isinstance(spec, ToolSpec) for spec in TOOLS.values()))
        write = TOOLS["apply_edit"]
        self.assertEqual(write.capability, Capability.WRITE_LOCAL)
        self.assertEqual(write.side_effect, SideEffect.MUTATING)
        self.assertFalse(write.parallel_safe)
        self.assertIn("input_schema", write)
        self.assertIn("owner_app", write)

    def test_structured_schema_preserves_metadata_contract(self):
        schema = tool_schemas(["search_code"])[0]["function"]
        self.assertEqual(schema["parameters"]["properties"]["query"]["type"], "string")
        self.assertIn("input", schema["parameters"]["properties"])

    def test_web_exposure_follows_the_switch_not_the_wording(self):
        """外网工具暴露**只由用户的联网开关决定**，不再要求问题措辞也命中。

        ⚠ **2026-10-08 改了本条的前提。** 原断言是：

            ordinary = route("请解释这个函数", web_enabled=True)
            assertNotIn("web", ordinary.tool_groups)     # 靠 _WEB 正则不命中

        那要求「关键词也猜中」才给外网工具，而实测有**两个**坏处：

        1. **授权已给而能力不可达** —— 用户打开联网开关，问「查下高铁票」，
           而 `_WEB` 不命中（它只认名词），于是 `web_search` 调不到。
        2. 三个 harness 都不按措辞挑工具（Pi 不涉及、Hermes 显式配置、
           opencode 预算截断 + 轮转），我们的关键词判定是唯一异类。

        所以闸门改成 `web_enabled` 本身。这条现在断言新契约：
        **开则 web 组在、关则连 sources 里都没有 web**。
        """
        router = ContextRouter()
        off = router.route("查下高铁票", code_root="D:/project", web_enabled=False)
        on = router.route("查下高铁票", code_root="D:/project", web_enabled=True)
        self.assertNotIn("web", off.tool_groups)
        self.assertNotIn("web", off.sources)
        self.assertIn("web", on.tool_groups)
        self.assertIn("web", on.sources)
        # 开关关着时，正文里也不该出现 web 工具组
        self.assertTrue(all(g != "web" for g in off.tool_groups), off.tool_groups)

    def test_project_bug_questions_use_project_audit_context(self):
        router = ContextRouter()
        plan = router.route("你看看目前的游戏有什么bug吗", code_root="D:/project")
        joined = "\n".join(plan.messages)
        self.assertIn("当前项目缺陷审查", joined)
        self.assertIn("dev_list_bugs", joined)
        self.assertIn("不能作为当前缺陷结论或唯一证据", joined)


if __name__ == "__main__":
    unittest.main()
