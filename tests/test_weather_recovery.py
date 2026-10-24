import datetime
import json
import unittest
from unittest.mock import patch

import agent
import tools
from tests.test_agent_trace import _FakeLLM, _IsoBase


class WeatherTests(_IsoBase):
    def test_transport_fare_now_reaches_the_model(self):
        """「留地板、拆其余」：出行票价澄清门已从 run() 聊天路径拆掉。

        旧行为：`run()` 开头用正则发现「没给日期/出发到达」，在调模型之前就弹
        一句 harness 写的澄清、标 degraded（`llm.i == 0`，模型根本没被叫）。

        新行为：那是靠正则猜「信息够不够」、把一句正常的话挡在模型之前，属于
        用户抱怨的「处处受限」。拆掉后照常进模型（缺信息时模型自己会追问或直接检索）。
        （判据函数 `_transport_request_clarification` 保留，直测仍在钉它。）
        """
        llm = _FakeLLM(["Final Answer: 好的，我先按国庆假期帮你查一下高铁和飞机价格"])
        a = agent.Agent(llm=llm, session_id="transport-date")
        events = list(a.run("北京到南宁高铁还是飞机，多少钱", web_enabled=True))
        self.assertGreaterEqual(llm.i, 1,
                                "票价澄清门已拆，这句话应照常进模型")
        gate_finals = [e for e in events
                       if e.get("type") == "final"
                       and e.get("clarification_required") and e.get("degraded")]
        self.assertEqual(gate_finals, [],
                         "不该再出现由票价澄清门产生的 degraded 澄清 final")

    def test_followup_ticket_request_now_reaches_the_model(self):
        """上文有出行词的追问同样不再被澄清门挡住——照常进模型。"""
        llm = _FakeLLM(["Final Answer: 我给你查一下当前余票和价格"])
        a = agent.Agent(llm=llm, session_id="transport-followup")
        a.history = [{"user": "我想去南宁玩", "assistant": "可以规划行程。"}]
        events = list(a.run("我要确切的计划，具体多少钱，目前有什么票", web_enabled=True))
        self.assertGreaterEqual(llm.i, 1,
                                "出行追问澄清门已拆，这轮应进模型而非被挡下")
        gate_finals = [e for e in events
                       if e.get("type") == "final"
                       and e.get("clarification_required") and e.get("degraded")]
        self.assertEqual(gate_finals, [],
                         "不该再出现由出行澄清门产生的 degraded 澄清 final")

    def test_transport_tool_never_marks_search_snippet_as_fare(self):
        with patch.object(tools, "web_search_batch", return_value="· 票价摘要\n  800 元\n  https://example.test"):
            out = tools.web_transport(json.dumps({
                "origin": "北京", "destination": "南宁", "departure_date": "2099-01-02"
            }, ensure_ascii=False))
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["verified_fare"])
        self.assertIn("不能当作当前价格", payload["message"])

    def test_transport_tool_rejects_missing_date(self):
        out = tools.web_transport(json.dumps({"origin": "北京", "destination": "南宁"}, ensure_ascii=False))
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("departure_date", payload["needs"])

    def test_transport_final_drops_unverified_price(self):
        out = agent._guard_transport_final(
            "北京到南宁目前约 800 元，还有余票。",
            "我要确切的计划，具体多少钱，目前有什么票",
            [],
        )
        self.assertNotIn("800", out)
        self.assertIn("无法确认票价", out)

    def test_missing_city_now_reaches_the_model(self):
        """「留地板、拆其余」：缺城市天气澄清门已从 run() 聊天路径拆掉。

        旧行为：`run()` 开头用正则判定「没给城市」，在调模型之前就弹一句
        harness 写的澄清（`llm.i == 0`）。这属于靠正则猜意图、把正常的话挡在
        模型之前。拆掉后照常进模型：模型会自己用定位/追问/检索补齐。
        （判据函数 `_missing_weather_location` 保留，直测仍钉它——见下条。）
        """
        llm = _FakeLLM(["Final Answer: 我先根据定位查一下你所在城市的天气"])
        a = agent.Agent(llm=llm, session_id="weather")
        events = list(a.run("查一下今天的天气怎么样", web_enabled=True))
        self.assertGreaterEqual(llm.i, 1,
                                "城市澄清门已拆，这句应照常进模型")
        gate_finals = [e for e in events
                       if e.get("type") == "final"
                       and e.get("clarification_required") and e.get("degraded")]
        self.assertEqual(gate_finals, [],
                         "不该再出现由天气澄清门产生的 degraded 澄清 final")

    def test_explicit_city_is_not_blocked(self):
        self.assertFalse(agent._missing_weather_location("查一下北海今天的天气", []))
        self.assertFalse(agent._missing_weather_location("今天天气", [{"user": "我在广西北海"}]))
        self.assertTrue(agent._missing_weather_location("今天天气", [{"user": "你好", "assistant": "你可能在东莞"}]))

    def test_forecast_date_is_checked(self):
        place = {"name": "北海", "country_code": "CN", "latitude": 21.48, "longitude": 109.12}
        with patch.object(tools, "_json_request", side_effect=[{"results": [place]}, {"daily": {"time": ["2024-02-22"]}}]):
            self.assertIn("日期与今天不一致", tools.web_weather('北海'))

    def test_forecast_returns_dated_city_evidence(self):
        today = datetime.datetime.now().astimezone().date().isoformat()
        place = {"name": "北海", "country_code": "CN", "latitude": 21.48, "longitude": 109.12}
        with patch.object(tools, "_json_request", side_effect=[{"results": [place]}, {"daily": {"time": [today]}}]) as request:
            out = tools.web_weather('北海')
        self.assertIn(today, out)
        self.assertIn("北海", out)
        self.assertIn("start_date=" + today, request.call_args.args[0])

    def test_ambiguous_city_requires_confirmation(self):
        with patch.object(tools, "_json_request", return_value={"results": [{"name": "同名城"}, {"name": "同名城"}]}) as request:
            self.assertIn("需要明确地点", tools.web_weather("同名城"))
        self.assertEqual(request.call_count, 1)

    def test_failure_restores_tool_evidence_on_continue(self):
        a = agent.Agent(llm=_FakeLLM(["Final Answer: ok"]), session_id="recovery")
        def broken(*args, **kwargs):
            yield {"type": "action", "text": "web_search(北海天气)"}
            yield {"type": "observation", "text": "来源：https://example.test/beihai\n尚未核对日期"}
            raise RuntimeError("rejected")
        with patch.object(a, "_run", side_effect=broken):
            with self.assertRaises(RuntimeError):
                list(a.run("查北海天气"))
        restored = agent.Agent(llm=a.llm, session_id="recovery")
        messages = restored._build_messages("继续")
        self.assertTrue(any("example.test/beihai" in m["content"] for m in messages))
        self.assertEqual(a.last_turn_record["outcome"], "failed")

    def test_api_failure_is_marked_as_error_and_does_not_suggest_changing_model(self):
        import json
        import api
        from starlette.testclient import TestClient
        from types import SimpleNamespace
        fake = SimpleNamespace(llm=_FakeLLM([]))
        def fail(*args, **kwargs):
            raise RuntimeError("backend failed")
        fake.run = fail
        with patch.object(api, "_agent_for", return_value=fake), \
                patch.object(api, "check_ollama", return_value={"reachable": True}), \
                patch.object(api, "dev_capture_bug", return_value=""), \
                patch.object(api, "route_for", return_value={"route": "local", "complexity": 0, "reason": "test"}):
            with TestClient(api.app) as client:
                response = client.post("/api/chat", data={"question": "查北海天气"})
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        final = next(item for item in events if item["type"] == "final")
        self.assertEqual(final["status"], "error")
        self.assertNotIn("换一个能加载", final["text"])


if __name__ == "__main__":
    unittest.main()
