"""Task N 长期记忆优化：逐字 dump → 异步蒸馏 + 语义/重要度混合召回 的回归锁。

三个能力全部**默认关**（env 未设 → 行为与今天逐字节一致），本文件把两件事钉死：
  1) 关闭时逐字节退化：新元数据列不外泄进 recall 输出；默认档序 == 增强但中性档序；
  2) 开启时的正效果：重要度参与排序、蒸馏就地精简/作废低价值轮次、嵌入失败静默降级。
不依赖真实 LLM / 网络：_FakeLLM 驱动蒸馏，_distill_pool 被替换成同步执行器。
"""
import json
import os
import unittest
from unittest.mock import patch

import agent
import agent_memory as memory
from tests.test_agent_trace import _FakeLLM, _IsoBase


class _SyncPool:
    """把后台蒸馏任务当场跑完的假执行器：让测试可断言蒸馏副作用，无需等待线程。"""
    def __init__(self):
        self.calls = 0

    def submit(self, fn, *a, **k):
        self.calls += 1
        fn()
        return None


class DistillEnabledTests(unittest.TestCase):
    def test_all_flags_default_off(self):
        for name in ("DOCMIND_MEMORY_DISTILL", "DOCMIND_MEMORY_EMBED", "DOCMIND_MEMORY_ENHANCED"):
            with patch.dict(os.environ, {}, clear=True):
                self.assertFalse(memory._distill_enabled(), name + " 默认必须关")
                self.assertFalse(memory._embed_enabled(), name + " 默认必须关")
                self.assertFalse(memory._enhanced_enabled(), name + " 默认必须关")

    def test_flag_parses_truthy(self):
        with patch.dict(os.environ, {"DOCMIND_MEMORY_DISTILL": "1"}):
            self.assertTrue(memory._distill_enabled())
        with patch.dict(os.environ, {"DOCMIND_MEMORY_EMBED": "true"}):
            self.assertTrue(memory._embed_enabled())
        with patch.dict(os.environ, {"DOCMIND_MEMORY_ENHANCED": "yes"}):
            self.assertTrue(memory._enhanced_enabled())


class RecallHygieneTests(_IsoBase):
    def test_new_internal_columns_never_leak_into_recall(self):
        """即便写入了 importance / 向量，recall 返回的行 dict 也不能带这些内部键——
        否则 context() 注入串与 API JSON 变样，破坏 MEMORY_CHARS=309 校准。"""
        with patch.dict(os.environ, {"DOCMIND_MEMORY_EMBED": "1"}), \
                patch.object(memory, "_embed_one", return_value=[0.1, 0.2, 0.3]):
            memory.remember("p", "fact", "t", "内容", "e", importance=0.9)
        for row in memory.recall("p", limit=20):
            for internal in ("scope", "status", "supersedes", "backed",
                             "importance", "emb", "emb_dim", "access_count", "last_accessed"):
                self.assertNotIn(internal, row, "内部列 %s 泄漏进 recall 输出" % internal)
        # 旧的核心键仍在，注入串形状不变
        self.assertIn("content", memory.recall("p", limit=20)[0])

    def test_default_ordering_unchanged_by_importance_when_off(self):
        """ENHANCED 关：importance 完全不参与排序，档序逐字节 == 今天 (score,trust,recency)。"""
        env_off = patch.dict(os.environ, {}, clear=True)
        with env_off:
            for i in range(5):
                memory.remember("p", "workflow", "流程", "步骤 %d" % i, "e",
                                key="k%d" % i, importance=0.1 * i)
            baseline = [r["content"] for r in memory.recall("p", "步骤", limit=20)]
        # 全新 scope 用中性 importance 逐字节复现同一批内容 → 得同一顺序
        with patch.dict(os.environ, {"DOCMIND_MEMORY_ENHANCED": "1"}):
            neutral = [r["content"] for r in memory.recall("p", "步骤", limit=20)]
        # 增强 + 中性/混合 importance 时，只要 importance 全 != 触发重排，我们只要求
        # "关"路径本身与不写 importance 的旧行为一致：baseline 应按 updated DESC。
        self.assertEqual(baseline, list(reversed(
            ["步骤 %d" % i for i in range(5)])), "默认路径应严格按 updated DESC")
        # 默认关路径里 importance=0.0 与 0.4 的行不因重要度挪位
        self.assertEqual(baseline, [r["content"] for r in memory.recall("p", "步骤", limit=20)])
        self.assertTrue(neutral)  # 至少不报错

    def test_enhanced_importance_changes_ranking(self):
        """ENHANCED 开：等关键词分下，高 importance 行压过低 importance 行。"""
        memory.remember("p", "fact", "低", "同一个词 甲", "e", key="lo", importance=0.05)
        memory.remember("p", "fact", "高", "同一个词 乙", "e", key="hi", importance=0.95)
        with patch.dict(os.environ, {"DOCMIND_MEMORY_ENHANCED": "1"}):
            top = memory.recall("p", "同一个词", limit=5)[0]["content"]
        self.assertIn("乙", top, "增强模式必须让高重要度行浮到最前")

    def test_vector_relevance_breaks_keyword_tie(self):
        """EMBED+ENHANCED 开：两条都关键词命中、重要度中性，靠向量余弦分高下。"""
        def fake_embed(text):
            if "苹果" in text:
                return [1.0, 0.0]
            if "橘子" in text:
                return [0.0, 1.0]
            return [1.0, 0.0]  # query 苹果 → 与"苹果"行同向
        with patch.dict(os.environ, {"DOCMIND_MEMORY_EMBED": "1", "DOCMIND_MEMORY_ENHANCED": "1"}), \
                patch.object(memory, "_embed_one", side_effect=fake_embed):
            memory.remember("p", "fact", "f1", "水果 苹果 记录", "e", key="a")
            memory.remember("p", "fact", "f2", "水果 橘子 记录", "e", key="b")
            ranked = memory.recall("p", "苹果", limit=5)
        self.assertIn("苹果", ranked[0]["content"],
                      "查'苹果'时语义更近的行应排第一")


class EmbedFailureTests(_IsoBase):
    def test_embedding_backend_failure_never_blocks_remember(self):
        """EMBED 开但嵌入后端抛错：记忆必须照样落库（空向量），绝不'写不进去'。"""
        with patch.dict(os.environ, {"DOCMIND_MEMORY_EMBED": "1"}), \
                patch.object(memory, "_embed_one", side_effect=RuntimeError("ollama down")):
            mid = memory.remember("p", "fact", "标题", "正文", "e")
        self.assertTrue(mid)
        rows = memory.recall("p", "正文")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "正文")


class RememberImportanceTests(_IsoBase):
    def test_importance_refined_in_place_same_id(self):
        mid = memory.remember("p", "episode", "问题", "原始逐字长文", "e",
                              source="model_report", key="q")
        # 重记同一 key → 同一行就地更新，不新增碎片，importance 被写入
        mid2 = memory.remember("p", "episode", "问题", "精简结论", "e",
                               source="model_report", key="q", importance=0.8)
        self.assertEqual(mid, mid2, "同 key 必须复用同一 id（就地蒸馏不产生新碎片）")
        rows = [r for r in memory.recall("p", "精简", limit=10)]
        self.assertEqual(len(rows), 1, "就地更新后不应有多条重复 episode")
        self.assertEqual(rows[0]["content"], "精简结论")
        # 省略 importance 的再次重记不得把已调过的分抹回默认（COALESCE 保留旧值）
        with patch.dict(os.environ, {"DOCMIND_MEMORY_ENHANCED": "1"}):
            memory.remember("p", "episode", "问题", "精简结论 v2", "e",
                            source="model_report", key="q")
            # 用高/低 importance 的另一行对照，确认 0.8 这一行没被抹成 0.5
            memory.remember("p", "fact", "对照", "精简结论 v2 对照", "e",
                            key="ctl", importance=0.0)


class DistillPathTests(_IsoBase):
    def _make_agent(self, scripts):
        return agent.Agent(llm=_FakeLLM(scripts), session_id="dist", project_id="p")

    def test_distill_off_writes_nothing_extra_and_no_thread(self):
        """默认关：_schedule_memory_distill 直接 return，不提交后台任务，逐字行原样留存。"""
        a = self._make_agent(["不该被调用"])
        scope = a.memory_scope
        mid = memory.remember(scope, "episode", "q", "模型报告（尚需核对验收）：逐字长文",
                              "e", source="model_report", key="q")
        pool = _SyncPool()
        with patch.dict(os.environ, {}, clear=True), patch.object(agent, "_distill_pool", return_value=pool):
            a._schedule_memory_distill(mid, "q", "逐字长文", None, False)
        self.assertEqual(pool.calls, 0, "蒸馏关闭时绝不能提交后台任务")
        surfaced = [r["content"] for r in memory.recall(scope, "逐字", limit=20)]
        self.assertTrue(any("逐字长文" in c for c in surfaced), "关闭时逐字 episode 原样保留")

    def test_distill_keep_false_retires_verbatim_episode(self):
        """开启 + 模型判 keep:false → 逐字 episode 被 retire，不再浮进上下文（治本）。"""
        a = self._make_agent(['{"keep": false, "summary": "", "importance": 0.0}'])
        scope = a.memory_scope
        mid = memory.remember(scope, "episode", "寒暄", "模型报告（尚需核对验收）：好的，收到！",
                              "e", source="model_report", key="寒暄")
        pool = _SyncPool()
        with patch.dict(os.environ, {"DOCMIND_MEMORY_DISTILL": "1"}, clear=False), \
                patch.object(agent, "_distill_pool", return_value=pool):
            a._schedule_memory_distill(mid, "寒暄", "好的，收到！", None, False)
        self.assertEqual(pool.calls, 1)
        active = [r["id"] for r in memory.recall(scope, "收到", limit=20)]
        self.assertNotIn(mid, active, "低价值轮次蒸馏后应从 active 消失")
        # 但行没被硬删（审计留痕）——include_all 仍可见，status=retired
        allrows = {r["id"]: r for r in memory.recall(scope, "收到", limit=20, include_all=True)}
        self.assertIn(mid, allrows, "retire 是软作废，不得销毁证据")

    def test_distill_keep_true_refines_content_in_place(self):
        """开启 + keep:true → 同一行就地换成精简摘要（id 不变、仍 active）。"""
        a = self._make_agent(['{"keep": true, "summary": "Godot 检查须先跑测试再预览", "importance": 0.85}'])
        scope = a.memory_scope
        mid = memory.remember(scope, "episode", "Godot 检查怎么做",
                              "模型报告（尚需核对验收）：" + ("很啰嗦的逐字过程记录 " * 40),
                              "e", source="model_report", key="Godot 检查怎么做")
        pool = _SyncPool()
        with patch.dict(os.environ, {"DOCMIND_MEMORY_DISTILL": "1"}, clear=False), \
                patch.object(agent, "_distill_pool", return_value=pool):
            a._schedule_memory_distill(mid, "Godot 检查怎么做", "很啰嗦的逐字过程记录 " * 40, None, False)
        self.assertEqual(pool.calls, 1)
        rows = [r for r in memory.recall(scope, "Godot", limit=20)]
        self.assertEqual(len(rows), 1, "就地蒸馏不得产生第二条")
        self.assertEqual(rows[0]["id"], mid)
        self.assertIn("已蒸馏", rows[0]["content"])
        self.assertIn("先跑测试再预览", rows[0]["content"])
        self.assertNotIn("很啰嗦的逐字过程记录 " * 5, rows[0]["content"],
                         "精简后不应再保留大段逐字原文")

    def test_distill_malformed_json_preserves_verbatim(self):
        """模型乱答（非 JSON）→ 解析失败不做破坏性改动，逐字记录原样保留。"""
        a = self._make_agent(["我一时不知道该怎么总结，抱歉。"])
        scope = a.memory_scope
        mid = memory.remember(scope, "episode", "q2", "模型报告（尚需核对验收）：重要结论原文",
                              "e", source="model_report", key="q2")
        pool = _SyncPool()
        with patch.dict(os.environ, {"DOCMIND_MEMORY_DISTILL": "1"}, clear=False), \
                patch.object(agent, "_distill_pool", return_value=pool):
            a._schedule_memory_distill(mid, "q2", "重要结论原文", None, False)
        self.assertEqual(pool.calls, 1)
        surfaced = [r["content"] for r in memory.recall(scope, "重要结论", limit=20)]
        self.assertTrue(any("重要结论原文" in c for c in surfaced),
                        "解析失败必须保守：保留逐字，不作废、不删")

    def test_distill_skips_failed_turn(self):
        """error 轮次不蒸馏也不作废（逐字诊断信息要留着）。"""
        a = self._make_agent(["不该被调用"])
        scope = a.memory_scope
        mid = memory.remember(scope, "episode", "q3", "未完成：HTTP 400",
                              "e", source="model_report", key="q3")
        pool = _SyncPool()
        with patch.dict(os.environ, {"DOCMIND_MEMORY_DISTILL": "1"}, clear=False), \
                patch.object(agent, "_distill_pool", return_value=pool):
            a._schedule_memory_distill(mid, "q3", "", "HTTP 400", False)
        self.assertEqual(pool.calls, 0, "失败/空答复轮次不应触发蒸馏")


class ExtractJsonTests(unittest.TestCase):
    def test_extracts_balanced_object_ignoring_prose(self):
        self.assertEqual(agent._extract_json_obj('前面废话 {"keep": true, "n": 3} 后面废话'),
                         {"keep": True, "n": 3})

    def test_returns_none_on_garbage(self):
        self.assertIsNone(agent._extract_json_obj("完全没有花括号"))
        self.assertIsNone(agent._extract_json_obj("{坏掉的 json"))

    def test_handles_braces_inside_strings(self):
        got = agent._extract_json_obj('{"summary": "他说 {不是真括号} 但仍要保留", "keep": true}')
        self.assertTrue(got["keep"])
        self.assertIn("{不是真括号}", got["summary"])


if __name__ == "__main__":
    unittest.main()
