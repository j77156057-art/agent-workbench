import io
import json
import os
import sys
import tempfile
import types
import unittest
import time
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools
from agent import _clip_tool_observation


class _FakeToolResult:
    """模拟 ToolResult：带 `.text` 字符串，但自身不可切片 / 不可下标 / 不可成员判断。

    若下游未先规整成 str 就 `re.findall` / `join` / `in`，这里会主动抛 TypeError，
    从而使测试真正覆盖「工具返回值未规整即被使用」这一 bug 类。
    """

    def __init__(self, text):
        self.text = text

    def __getitem__(self, item):
        raise TypeError("'ToolResult' object is not subscriptable")

    def __contains__(self, item):
        raise TypeError("'ToolResult' object is not iterable")


class WebResearchProductTests(unittest.TestCase):
    def test_slow_engine_does_not_delay_fast_candidates(self):
        release = threading.Event()
        def blocked(_q):
            release.wait(2)
            return "· slow\n  late\n  https://slow.test"
        try:
            with patch.dict(os.environ, {"DOCMIND_WEB_SEARCH_WAIT_S": "0.05"}), \
                    patch.object(tools, "_ddg_search", side_effect=blocked), \
                    patch.object(tools, "_baidu_search", return_value="· fast\n  ok\n  https://fast.test"), \
                    patch.object(tools, "_bing_search", return_value="搜索未返回结果"):
                start = time.monotonic()
                out = tools._builtin_search("builtin_auto", "fast")
                self.assertLess(time.monotonic() - start, 0.5)
            self.assertIn("https://fast.test", out)
            self.assertIn("未完成来源：ddg", out)
        finally:
            release.set()

    def test_failures_are_not_requested_twice(self):
        with patch.object(tools, "_ddg_search", side_effect=OSError()) as ddg, \
                patch.object(tools, "_baidu_search", side_effect=OSError()) as baidu, \
                patch.object(tools, "_bing_search", side_effect=OSError()) as bing:
            out = tools._builtin_search("builtin_auto", "q")
        self.assertIn("均不可达", out)
        self.assertIn("ddg", out)
        self.assertIn("baidu", out)
        self.assertIn("bing", out)
        for backend in (ddg, baidu, bing):
            backend.assert_called_once()

    def test_keeps_thirty_candidates_and_prefers_topic_over_domain(self):
        candidates = "\n".join(f"· Godot scene tree {i}\n  reference\n  https://example.test/{i}" for i in range(30))
        irrelevant = "· official cooking docs\n  unrelated\n  https://docs.github.com/unrelated"
        out = tools._merge_builtin_results("Godot scene tree", [("ddg", irrelevant), ("bing", candidates)])
        rows = tools._formatted_search_rows(out)
        self.assertEqual(len(rows), 30)
        self.assertNotIn("unrelated", out)

    def test_ddg_links_stay_with_their_titles_regardless_of_attribute_order(self):
        page = '<a href="https://example.test/a?x=1&amp;y=2" class="result__a">A &amp; B</a>'
        with patch.object(tools.urllib.request, "urlopen", return_value=io.BytesIO(page.encode())):
            out = tools._ddg_search("q")
        self.assertIn("A & B", out)
        self.assertIn("https://example.test/a?x=1&y=2", out)

    def test_generic_engine_homepages_cannot_answer_detailed_query(self):
        raw = "· Godot official home\n  Godot engine\n  https://godotengine.org"
        with patch.object(tools, "_ddg_search", return_value=raw), \
                patch.object(tools, "_baidu_search", return_value=raw), \
                patch.object(tools, "_bing_search", return_value=raw):
            out = tools._builtin_search("builtin_auto", "Godot scene tree official docs")
        self.assertTrue(out.startswith("搜索结果相关性不足"))
        self.assertNotIn("https://godotengine.org", out)

    def test_chinese_candidate_matching_only_generic_route_term_is_rejected(self):
        query = "南宁旅游攻略景点路线"
        unrelated = "· 路线规划器通用工具\n  generic route planning\n  https://example.test/tool"
        out = tools._merge_builtin_results(query, [
            ("ddg", unrelated), ("bing", "搜索未返回结果"),
        ])
        self.assertTrue(out.startswith("搜索结果相关性不足"), out)
        self.assertIn("1 条候选", out)
        self.assertIn("bing=无候选", out)

    def test_chinese_candidate_with_topic_phrase_is_kept(self):
        query = "南宁旅游攻略景点路线"
        relevant = "· 南宁旅游攻略：景点与路线\n  南宁旅游景点路线建议\n  https://example.test/nanning"
        out = tools._merge_builtin_results(query, [("ddg", relevant)])
        self.assertIn("https://example.test/nanning", out)

    def test_cached_candidates_are_rechecked_for_relevance(self):
        unrelated = "· 路线规划器通用工具\n  generic route planning\n  https://example.test/tool"
        with patch("tools.get_web_search_provider", return_value="ddg"), \
                patch("tools._search_cache_enabled", return_value=True), \
                patch("tools._web_cache_read", return_value=unrelated), \
                patch("tools._ddg_search") as search:
            out = tools.web_search("南宁旅游攻略景点路线")
        self.assertTrue(out.startswith("搜索结果相关性不足"), out)
        search.assert_not_called()

    def test_auto_search_does_not_recheck_truncated_aggregate_snippets(self):
        aggregate = ("聚合搜索结果（1 条候选）：\n· Godot 插件\n  "
                     + "背景信息 " * 40
                     + "Godot scene tree integration\n  https://example.test/godot")
        with patch("tools.get_web_search_provider", return_value="builtin_auto"), \
                patch("tools._search_cache_enabled", return_value=False), \
                patch("tools._builtin_search", return_value=aggregate), \
                patch("tools._filter_search_results", wraps=tools._filter_search_results) as filter_results:
            out = tools.web_search("Godot scene tree docs")
        self.assertEqual(out, aggregate)
        filter_results.assert_not_called()

    def test_web_research_does_not_fetch_irrelevant_search_candidates(self):
        unrelated = "· 路线规划器通用工具\n  generic route planning\n  https://example.test/tool"
        with patch("tools.web_search", return_value=unrelated), \
                patch("tools.web_fetch") as fetch:
            out = tools.web_research("南宁旅游攻略景点路线")
        self.assertTrue(out.startswith("搜索结果相关性不足"), out)
        fetch.assert_not_called()

    def test_travel_recommendation_rejects_bing_only_candidate(self):
        with patch.object(tools, "_ddg_search", return_value="搜索未返回结果"), \
                patch.object(tools, "_baidu_search", return_value="搜索未返回结果"), \
                patch.object(tools, "_bing_search", return_value="· 泛化攻略\n  泛化摘要\n  https://example.test/travel"), \
                patch.object(tools, "_infer_search_sources", return_value=[]):
            out = tools._builtin_search("builtin_auto", "南宁旅游攻略")
        self.assertTrue(out.startswith("搜索结果相关性不足"))
        self.assertIn("Bing 单一", out)

    def test_bing_encoded_redirect_recovers_original_source(self):
        import base64
        target = "https://docs.godotengine.org/en/stable/scene.html"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        page = f'<li class="b_algo"><h2><a href="https://www.bing.com/ck/a?p=x&amp;u=a1{encoded}&amp;ntb=1">Scene tree</a></h2></li>'
        with patch.object(tools.urllib.request, "urlopen", return_value=io.BytesIO(page.encode())):
            out = tools._bing_search("Godot scene tree")
        self.assertEqual(tools._formatted_search_rows(out)[0]["url"], target)

    def test_batch_observation_keeps_thirty_compact_candidates(self):
        raw = "\n".join(f"· candidate {i}\n  {'detail ' * 15}\n  https://example.test/{i}" for i in range(30))
        self.assertEqual(_clip_tool_observation(raw, 1800, "web_search_batch"), raw)

    def test_non_temporal_queries_do_not_exclude_old_reference_material(self):
        with patch.dict(os.environ, {"WEB_SEARCH_PREFER_RECENT": "1"}):
            self.assertNotIn("after:", tools._search_recency_query("Godot 场景树教程"))
            self.assertIn("after:", tools._search_recency_query("最新 Godot 版本"))

    def test_source_score_is_explainable(self):
        score, reason = tools._source_score("https://github.com/a/b", "official docs", "")
        self.assertGreater(score, .6)
        self.assertIn("平台原站", reason)

    def test_conflict_marker(self):
        out = tools._detect_source_conflicts(["窗口 16 GB", "窗口 24 GB"])
        self.assertIn("多个数值", out)

    def test_github_api_formatter(self):
        payload = {"items": [{"full_name": "a/b", "description": "official", "html_url": "https://github.com/a/b", "stargazers_count": 3, "updated_at": "2026-01-01T00:00:00Z"}]}
        with patch("tools._json_request", return_value=payload):
            out = tools._github_search("godot")
        self.assertIn("GitHub 专用搜索", out)
        self.assertIn("a/b", out)
        self.assertIn("可信度参考", out)

    def test_bilibili_api_formatter(self):
        payload = {"data": {"result": [{"title": "教程", "bvid": "BV1xx", "description": "说明"}]}}
        with patch("tools._json_request", return_value=payload):
            out = tools._bilibili_search("godot")
        self.assertIn("B 站专用视频搜索", out)
        self.assertIn("BV1xx", out)

    def test_subtitles_requires_video_id(self):
        self.assertIn("BV 号或 av 号", tools.web_subtitles("https://www.bilibili.com/"))

    def test_specialized_failure_keeps_diagnostic_on_fallback(self):
        with patch("tools._bilibili_search", return_value="B 站专用搜索暂不可用（验证码）"), \
                patch("tools._ddg_search", return_value="· fallback\n  s\n  https://example.com"), \
                patch("tools.get_web_search_provider", return_value="ddg"), \
                patch.object(sys.modules["__main__"], "__spec__", types.SimpleNamespace(name="test")), \
                patch.dict(tools.os.environ, {"WEB_SEARCH_PREFER_RECENT": "0", "DOCMIND_WEB_CACHE": "0"}, clear=False):
            out = tools.web_search("platform: b站\n教程")
        self.assertIn("B 站专用搜索暂不可用", out)
        self.assertIn("通用搜索回退结果", out)

    def test_builtin_auto_merges_and_deduplicates_backends(self):
        rows = {
            "ddg": "· Godot official docs\n  scene tree reference\n  https://docs.godotengine.org/en/stable/scene.html",
            "baidu": "· Godot 场景树教程\n  scene tree reference\n  https://docs.godotengine.org/en/stable/scene.html\n"
                     "· Godot scene tree community guide\n  scene tree guide\n  https://example.com/godot",
            "bing": "· Godot scene tree\n  scene tree reference\n  https://example.net/godot",
        }
        with patch.dict(tools.os.environ, {"WEB_SEARCH_FANOUT": "3"}, clear=False), \
                patch.object(tools, "_ddg_search", side_effect=lambda _q: rows["ddg"]), \
                patch.object(tools, "_baidu_search", side_effect=lambda _q: rows["baidu"]), \
                patch.object(tools, "_bing_search", side_effect=lambda _q: rows["bing"]):
            out = tools._builtin_search("builtin_auto", "Godot scene tree")
        self.assertIn("聚合搜索结果", out)
        self.assertEqual(out.count("https://docs.godotengine.org/en/stable/scene.html"), 1)
        self.assertIn("https://example.com/godot", out)

    def test_web_observation_clip_keeps_research_tail(self):
        raw = "研究主题：上下文窗口\n搜索摘要：\n" + ("前段资料\n" * 1800) + "\n冲突提示（自动抽取）：\n来源尾部：128K"
        clipped = _clip_tool_observation(raw, 1800, "web_research")
        self.assertIn("中间来源正文已折叠", clipped)
        self.assertIn("来源尾部：128K", clipped)

    def test_web_research_coerces_toolresult_without_raising(self):
        # 回归 #2：web_search / web_fetch 返回不可切片的 ToolResult（非图片分支）
        # 不得抛 TypeError，且应把 .text 当正文产出可 join 的 str。
        search_tr = _FakeToolResult("· 上下文窗口文档\n  上下文窗口说明\n  https://example.com/doc")
        fetch_tr = _FakeToolResult("来源：https://example.com/doc\n正文：窗口上下文说明")
        with patch("tools.web_search", return_value=search_tr), \
                patch("tools.web_fetch", return_value=fetch_tr), \
                patch("tools._web_images_enabled", return_value=False), \
                patch.dict(tools.os.environ, {"DOCMIND_WEB_RESEARCH_MAX_SOURCES": "1"}, clear=False):
            out = tools.web_research("上下文窗口")
        self.assertIsInstance(out, str)
        self.assertIn("example.com/doc", out)
        self.assertIn("正文：窗口上下文说明", out)

    def test_auto_search_adds_relevant_platform_sources(self):
        base = "· 什么菜好吃：通用结果\n  菜品好吃体验\n  https://example.com/general"
        platform_rows = {
            "知乎": "· 什么菜好吃：知乎评价\n  菜品好吃口碑\n  https://www.zhihu.com/question/1",
            "小红书": "· 什么菜好吃：小红书探店\n  菜品好吃实拍体验\n  https://www.xiaohongshu.com/explore/1",
        }
        with patch.object(tools, "_infer_search_sources", return_value=["zhihu.com", "xiaohongshu.com"]), \
                patch.object(tools, "_search_one_source", side_effect=lambda domain, _q: (
                    tools._SEARCH_SOURCE_LABELS[domain], platform_rows[tools._SEARCH_SOURCE_LABELS[domain]])):
            out = tools._search_auxiliary_sources("什么菜好吃 推荐", base)
        self.assertIn("知乎评价", out)
        self.assertIn("小红书探店", out)
        self.assertIn("通用结果", out)

    def test_travel_queries_fan_out_beyond_general_engine(self):
        sources = tools._infer_search_sources("南宁旅游攻略 景点 路线")
        self.assertIn("zhihu.com", sources)
        self.assertIn("xiaohongshu.com", sources)
        self.assertIn("bilibili.com", sources)
        self.assertIn("tieba.baidu.com", sources)

    def test_batch_search_runs_queries_in_parallel_and_excludes_seen(self):
        rows = {
            "南宁酸菜鱼 评价": "· 南宁酸菜鱼 A 评价\n  很好吃\n  https://example.com/a",
            "南宁酸菜鱼 做法": "· 南宁酸菜鱼 A 做法\n  步骤\n  https://example.com/a\n"
                             "· 南宁酸菜鱼 B 做法\n  步骤\n  https://example.com/b",
        }
        with patch.object(tools, "web_search", side_effect=lambda q, _allow_aux=True: rows[q]):
            out = tools.web_search_batch(json.dumps({"queries": list(rows), "exclude": ["example.com/a"]}, ensure_ascii=False))
        self.assertIn("2 个查询并行", out)
        self.assertNotIn("example.com/a", out)
        self.assertIn("example.com/b", out)


if __name__ == "__main__":
    unittest.main()
