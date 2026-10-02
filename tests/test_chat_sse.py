"""`POST /api/chat` 真问答链路：真 SSE 包装 + 真 ReAct 循环 + 真工具执行，零网络。

这一份**不打 `Agent.run`**——那 6 条已有用例全都打了，于是经由这个端点，
`agent.py` 的 ReAct 主循环、`llm.py` 的 provider 分派、`tools.py` 的工具执行一条都没真跑过。
这里让真 Agent 跑完整一轮，判据交给 `agent_runtime/chat_sse.py`。

## 为什么不用 `provider="mock"`（这条是量出来的，不是偏好）

先按最省事的做法试了 mock：它确实把 ReAct 循环与工具都跑起来了（实测 345 个 chunk、
332 个 token 事件、两次 action/observation），但**结构性地结束不了一轮**——
`_mock_chat` 的「拿到观察 → 作答」分支（`llm.py:1119-1122`）吐的是**不带
`Final Answer:` 标记的裸文本**，而 `agent.py:3443` 的格式失守判定会连拒两轮，然后
发一条 `final(status="error")`。也就是说 **mock provider 无法完成一次问答**（产品侧的
离线演示模式同样跑不完，这里只报不改——`llm.py` 正在被别人改）。

所以改用**可注入的剧本 LLM**，但仍然零共享文件改动：让生产逻辑自己解析出会话 Agent
（`api._agent_for(session_id)`，key/project 绑定/工具注册全走真的），只把它的 `llm`
换成剧本对象。

## 三个必须钉住的前提，缺一个都会让这条用例"绿得没有意义"

1. **压两个 provider**：`api.py:2168` 的条件是 `_prov == "ollama" or _emb == "ollama"`，
   而本仓 `.env` 把 `EMBEDDING_PROVIDER` 设成了 ollama —— 只压 `llm_provider` 照样会走
   `check_ollama` 的真 socket 探活（最坏阻塞 8 秒）。用例直接断言 `check_ollama` 零次调用。
2. **对面说话的那个必须是我**：云端升级（`api.py:2238-2271`）会用 `run(llm=cloud_llm)`
   逐请求覆盖，把剧本 LLM 绕过。所以断言剧本 LLM 真的被调用过——它一次没说话就说明
   真 provider 接管了，这条用例就不是零网络的了。
3. **不联网**：判据里显式断言没有 `web_search`、观察里没有「未找到」。它们一旦出现就红，
   而不是悄悄联网跑完再报绿。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent as agent_mod      # noqa: E402
import agent_memory            # noqa: E402
import agent_trace             # noqa: E402
import config                  # noqa: E402
import sessions                # noqa: E402
import api                     # noqa: E402
from agent_runtime import chat_sse  # noqa: E402

#: 走精选素材目录那条路（读仓内 `assets_catalog.json`，零网络）。这些词实测能命中
#: 目录里的 26 条（"Pixel Platformer" 等），所以观察里不会出现「未找到」。
ASSET_QUESTION = "像素风角色素材有哪些？"
ASSET_QUERY = "像素风角色"


class ScriptedQALM:
    """两拍子的假 LLM：先调一次工具，再把**工具观察里的一截**写进 Final Answer。

    逐字符流式（和真实 provider 一样），所以这条链路真的会产出几十个 `token` 事件。
    `calls` 是"对面到底是不是我"的自证：云端升级那档会用 `run(llm=…)` 覆盖掉我，
    那种情况下 `calls` 停在 0，用例立刻红——而不是悄悄换成真模型跑完再报绿。
    """

    provider = "scripted"
    model = "scripted-1"
    capability = {"vision": False}

    def __init__(self):
        self.calls = 0
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def chat(self, messages, stream=True, **kw):
        last = str((messages[-1] or {}).get("content") if messages else "")
        self.calls += 1
        if "Observation:" in last:
            snippet = last.rsplit("Observation:", 1)[-1].strip().split("\n")[0][:120]
            text = ("Thought: 证据够了\nFinal Answer: 根据精选素材目录，"
                    "像素风角色可以用 %s —— 以上就是可用清单。" % snippet)
        else:
            text = ("Thought: 先查精选素材目录\nAction: search_assets\n"
                    "Action Input: %s" % ASSET_QUERY)
        self.last_usage = {"prompt_tokens": 40, "completion_tokens": max(1, len(text) // 3)}
        if not stream:
            return text

        def generate():
            for char in text:
                yield char
        return generate()

    def count_tokens(self, text):
        return max(0, len(str(text or "")) // 3)


class ChatTurnCase(unittest.TestCase):
    """隔离写盘路径、压住 provider、把会话 Agent 的 llm 换成剧本对象。"""

    SESSION = "chat-sse-case"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_chat_sse_")
        for module, name, value in (
                (agent_trace, "TRACE_FILE", os.path.join(self.tmp, "traces.jsonl")),
                (sessions, "SESSIONS_DIR", os.path.join(self.tmp, "sessions")),
                (agent_memory, "DB_PATH", os.path.join(self.tmp, "memory.sqlite3"))):
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, getattr(module, name))
        agent_trace.TRACE_ENABLED = True
        for key in ("llm_provider", "embedding_provider", "code_root", "llm_mode"):
            self.addCleanup(config.set_runtime, key, config.get_runtime(key))
        config.set_runtime("llm_provider", "mock")
        config.set_runtime("embedding_provider", "local")
        config.set_runtime("code_root", str(self.tmp))
        config.set_runtime("llm_mode", "fixed")
        # 会话 Agent 是模块级缓存：用完要把**这一轮新增的键**删掉，别漏给别人。
        before = set(api._SESSION_AGENTS)

        def _drop_new():
            for key in [k for k in api._SESSION_AGENTS if k not in before]:
                api._SESSION_AGENTS.pop(key, None)
        self.addCleanup(_drop_new)

        self.scripted = ScriptedQALM()
        self.client = TestClient(api.app)

    def turn(self, question: str = ASSET_QUESTION, **extra):
        """真打一次 `/api/chat`。**不打 `Agent.run`、不打 `_agent_for`。**

        换 LLM 走的是 `agent_mod.LLMClient` 这个**构造接缝**（`agent.py:1703` 的
        `self.llm = llm or LLMClient()`），而不是自己去猜 `_agent_for` 的缓存键——
        请求内外的 `_ctx_project_id()` 可以不同，键一错就换到了另一个 Agent 上，
        实测那样会**静默用回 mock**（探针里就是这么发现的：输出全是 mock 的文案）。
        """
        with patch.object(agent_mod, "LLMClient", lambda *a, **k: self.scripted), \
                patch.object(api, "check_ollama", return_value={"reachable": True}) as probe:
            response = self.client.post("/api/chat",
                                        data={"question": question,
                                              "session_id": self.SESSION, **extra})
        self.assertEqual(probe.call_count, 0,
                         "provider 压住了却还是打了 ollama 探活——这一轮就不是零网络的")
        self.assertEqual(response.status_code, 200, response.text[:200])
        # "对面说话的那个必须是我"：被云端升级覆盖掉的话，这里是 0
        self.assertGreaterEqual(self.scripted.calls, 2,
                                "剧本 LLM 只被调用了 %s 次——它被逐请求覆盖了，"
                                "这一轮用的不是它" % self.scripted.calls)
        body = response.content
        events = chat_sse.parse_events(body)
        return body, events, chat_sse.verdict(events, chunk_count=chat_sse._chunks(body))


class RealTurnTests(ChatTurnCase):
    def test_a_full_turn_runs_the_react_loop_and_a_real_tool(self):
        """这一条是本文件存在的理由：一轮问答真的走完 ReAct + 工具 + SSE 包装。"""
        _body, events, judged = self.turn()
        self.assertTrue(judged["passed"], chat_sse.render(judged))
        types = [row.get("type") for row in events]
        self.assertIn("action", types)
        self.assertIn("observation", types)

    def test_the_tool_actually_ran_offline(self):
        """动作名要落在精选素材目录那条路上——不是随便哪个 action 都算数。"""
        _body, events, judged = self.turn()
        called = [str(name) for name in judged["observed"]["actions"] if name]
        self.assertIn("search_assets", called, judged["observed"])
        self.assertNotIn("web_search", called, "这一轮调了会联网的工具")

    def test_the_stream_was_incremental_rather_than_one_buffered_blob(self):
        """整轮只吐一个 data 块的话，"流式"就没被验到——缓冲后一次性吐和真流式长得一样。"""
        body, _events, judged = self.turn()
        self.assertGreaterEqual(chat_sse._chunks(body), 2, judged["observed"])
        self.assertGreaterEqual(judged["observed"]["n_tokens"], 2,
                                "只有 0~1 个 token 事件，这一轮不是流式吐出来的")

    def test_the_final_quotes_something_only_a_tool_could_know(self):
        """凭据设计（§5.10）：final 必须原样带着工具观察的一截，否则它可以凭空写。"""
        _body, events, judged = self.turn()
        final = judged["observed"]["final"]
        observations = [str(row.get("text") or "") for row in events
                        if row.get("type") == "observation"]
        self.assertTrue(observations, "这一轮没有任何 observation")
        self.assertTrue(chat_sse._quotes(str(final.get("text") or ""), observations),
                        "final 没有引述工具观察：%r" % str(final.get("text"))[:200])

    def test_the_turn_is_not_one_of_the_model_free_early_exits(self):
        """容量兜底 / 追问 / 强制收尾 / 报错——四条都会"产出一个 final"，必须逐条排除。"""
        _body, _events, judged = self.turn()
        final = judged["observed"]["final"]
        for flag in ("offline", "degraded", "clarification_required"):
            self.assertFalse(final.get(flag), "final 带着 %s：%r" % (flag, final))
        self.assertNotEqual(str(final.get("status") or ""), "error", final)


class VerdictTableTests(unittest.TestCase):
    """判据本身要被测：`chat_sse.verdict` 是这半的牙齿，它自己不能是装饰。"""

    def good(self):
        # 载荷形状必须照**真实**的样子写：工具名在 `text` 的 `name(args)` 里，
        # 不在独立字段上。第一版这里写的是 `{"tool": ...}`，于是纯层压根没走到
        # 真实代码路径，电池 11 条变异全"活下来"——看着像守卫没牙，其实是夹具写歪了。
        return [
            {"type": "route", "route": "chat"},
            {"type": "action", "text": "search_assets(像素风角色)", "step_cost": 1},
            {"type": "observation",
             "text": "Observation: · Pixel Platformer ［2d-sprites｜CC0］ 像素风平台跳跃角色与地块"},
            {"type": "token", "text": "根"},
            {"type": "final", "text": "根据检索到的内容：Pixel Platformer ［2d-sprites］ 这就是说明。"},
            {"type": "done"},
        ]

    def test_a_second_action_without_a_second_observation_is_still_broken(self):
        """两条 action 只回一条 observation —— 配平判据不能是 `>= 1`。"""
        rows = self.good()
        rows.insert(2, {"type": "action", "text": "list_dir(.)", "step_cost": 1})
        judged = chat_sse.verdict(rows, chunk_count=8)
        self.assertIn("every_action_has_an_observation", judged["failed"])

    def test_evidence_on_a_later_line_of_the_observation_still_counts(self):
        """工具结果常常是多行的：只看第一行的话，后面的证据一律不算。"""
        rows = self.good()
        rows[2] = {"type": "observation",
                   "text": "Observation: 命中 3 条\n· Liberated Pixel Cup Base ［CC-BY-SA］\n"
                           "· Pixel Platformer ［2d-sprites｜CC0］ 像素风平台跳跃角色与地块"}
        rows[4] = {"type": "final",
                   "text": "可以用 Pixel Platformer ［2d-sprites｜CC0］ 像素风平台跳跃角色与地块。"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertNotIn("final_quotes_a_tool_observation", judged["failed"],
                         "证据在观察的第三行，却没被认出来：%s" % judged["why"])
        self.assertTrue(judged["passed"], chat_sse.render(judged))

    def test_one_shared_character_is_not_a_quotation(self):
        """窗口不能缩到 1：一个共同字符就当引述，等于没有引述。"""
        rows = self.good()
        rows[2] = {"type": "observation", "text": "Observation: Pixel Platformer"}
        rows[4] = {"type": "final", "text": "P"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("final_quotes_a_tool_observation", judged["failed"])

    def test_a_miss_marker_observation_is_not_an_offline_turn(self):
        """观察里出现「未找到」：mock 下一轮就改调 web_search，这一轮已经不是零网络。"""
        rows = self.good()
        rows[2] = {"type": "observation", "text": "Observation: 未找到相关素材。"}
        rows[4] = {"type": "final", "text": "根据检索到的内容：未找到相关素材。"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("no_miss_marker_in_observations", judged["failed"])

    def test_an_action_with_no_readable_tool_name_is_flagged(self):
        """读不出工具名的 action 不当"没有工具"——那是"没读出来"，必须红。"""
        rows = self.good()
        rows[1] = {"type": "action", "text": "（这一步没有工具名）"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("every_action_names_its_tool", judged["failed"])

    def test_the_tool_name_is_read_from_both_payload_shapes(self):
        self.assertEqual(chat_sse._tool_name({"type": "action", "text": "search_assets(像素)"}),
                         "search_assets")
        self.assertEqual(chat_sse._tool_name({"type": "action", "tool": "read_file"}), "read_file")
        self.assertEqual(chat_sse._tool_name({"type": "action", "text": "没有工具名"}), "")

    def test_the_honest_shape_passes(self):
        judged = chat_sse.verdict(self.good(), chunk_count=6)
        self.assertTrue(judged["passed"], chat_sse.render(judged))

    def test_a_capacity_reply_is_not_a_turn(self):
        rows = [{"type": "final", "text": "本轮额度用完了", "offline": True}, {"type": "done"}]
        judged = chat_sse.verdict(rows, chunk_count=2)
        self.assertFalse(judged["passed"])
        self.assertIn("final_is_not_an_offline_capacity_reply", judged["failed"])
        self.assertIn("一条模型都没调", judged["why"])

    def test_a_degraded_final_is_not_a_verified_answer(self):
        rows = self.good()
        rows[-2] = {"type": "final", "text": "证据如下：Pixel Platformer", "degraded": True}
        self.assertIn("final_is_not_degraded",
                      chat_sse.verdict(rows, chunk_count=6)["failed"])

    def test_a_clarification_request_is_not_an_answer(self):
        rows = self.good()
        rows[-2] = {"type": "final", "text": "你要我改哪个文件？", "clarification_required": True}
        self.assertIn("final_is_not_a_clarification_request",
                      chat_sse.verdict(rows, chunk_count=6)["failed"])

    def test_an_error_final_is_red_even_though_the_stream_looked_fine(self):
        """HTTP 200 + 有 final + 有 done —— 这三样齐全也可能是彻底失败。"""
        rows = self.good()
        rows[-2] = {"type": "final", "text": "出错了", "status": "error", "error_kind": "runtime"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertFalse(judged["passed"])
        self.assertIn("final_is_not_an_error", judged["failed"])

    def test_a_final_with_no_tool_evidence_is_red(self):
        """凭空写一句"根据检索到的内容：……"也必须红——那正是本判据要挡的形状。"""
        rows = self.good()
        rows[-2] = {"type": "final", "text": "根据检索到的内容：这是你要的答案。"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("final_quotes_a_tool_observation", judged["failed"])

    def test_a_network_tool_turn_is_not_an_offline_turn(self):
        rows = self.good()
        rows[1] = {"type": "action", "tool": "web_search", "input": "像素"}
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("no_network_tool_was_called", judged["failed"])
        self.assertIn("不是零网络链路", judged["why"])

    def test_an_action_without_an_observation_is_a_broken_loop(self):
        rows = self.good()
        rows = [row for row in rows if row.get("type") != "observation"]
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("every_action_has_an_observation", judged["failed"])

    def test_a_single_chunk_is_not_a_stream(self):
        judged = chat_sse.verdict(self.good(), chunk_count=1)
        self.assertIn("stream_had_more_than_one_chunk", judged["failed"])

    def test_a_missing_done_means_the_stream_was_cut_short(self):
        rows = [row for row in self.good() if row.get("type") != "done"]
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("done_terminates_the_stream", judged["failed"])

    def test_route_and_done_alone_are_not_evidence(self):
        """它们是无条件发的（`api.py:2349`/`2430`），只有这两条时判据必须红。"""
        judged = chat_sse.verdict([{"type": "route"}, {"type": "done"}], chunk_count=2)
        self.assertFalse(judged["passed"])
        for name in ("at_least_one_tool_action", "final_is_present",
                     "final_quotes_a_tool_observation"):
            self.assertIn(name, judged["failed"])


    def test_a_turn_with_no_token_events_is_not_a_stream(self):
        rows = self.good()
        rows = [row for row in rows if row.get("type") != "token"]
        judged = chat_sse.verdict(rows, chunk_count=6)
        self.assertIn("tokens_were_streamed", judged["failed"])

    def test_tool_tokens_count_as_tokens_too(self):
        """token 事件是流式的证据；工具那几步各有 step_cost，但事件类型仍是 token。"""
        judged = chat_sse.verdict(self.good(), chunk_count=6)
        self.assertTrue(judged["checks"]["tokens_were_streamed"])


class ParsingTests(unittest.TestCase):
    def test_keep_alive_comments_are_not_events(self):
        body = 'data: {"type":"token","text":"a"}\n: keep-alive\n\ndata: {"type":"done"}\n'
        types = [row["type"] for row in chat_sse.parse_events(body)]
        self.assertEqual(types, ["token", "done"])

    def test_a_non_data_line_carrying_valid_json_is_still_not_an_event(self):
        """`:` 心跳去掉 `data:` 之后不是合法 JSON，所以光靠"解析失败"就躲开是**运气**。

        真正的 SSE `event:` 行去掉前缀后**是**合法 JSON——只靠 JSON 解析兜着的那种实现，
        在这一条上就会把它当成事件。所以这里要用这条形状的输入钉住"只认 `data:`"。
        """
        body = ('data: {"type":"token","text":"a"}\n'
                'event: {"type":"injected"}\n'
                'id: 17\n'
                'data: {"type":"done"}\n')
        types = [row["type"] for row in chat_sse.parse_events(body)]
        self.assertEqual(types, ["token", "done"])

    def test_a_bare_json_line_without_the_data_prefix_is_not_an_event(self):
        """裸 JSON 行（代理重写、手写夹具、别的 SSE 实现）也不该被当成事件。

        这条是上面那条的**补充**：真实协议里 `event: {...}` 去掉前缀后剩下 `: {...}`，
        `json.loads` 必然失败——所以只靠"解析失败"躲开是运气。裸 JSON 行才真正区分得开
        "认 `data:`" 与"不认"。
        """
        body = ('data: {"type":"token","text":"a"}\n'
                '{"type":"injected"}\n'
                'data: {"type":"done"}\n')
        types = [row["type"] for row in chat_sse.parse_events(body)]
        self.assertEqual(types, ["token", "done"])

    def test_a_truncated_chunk_does_not_kill_the_parse(self):
        """半截 JSON 不该把整轮带崩——但也绝不能被当成一个事件。"""
        body = 'data: {"type":"token"\ndata: {"type":"done"}\n'
        self.assertEqual([row["type"] for row in chat_sse.parse_events(body)], ["done"])

    def test_bytes_and_str_parse_the_same(self):
        rows = [{"type": "done"}]
        for payload in ('data: {"type":"done"}\n', b'data: {"type":"done"}\n'):
            self.assertEqual(chat_sse.parse_events(payload), rows)

    def test_chunk_counting_reads_the_raw_body(self):
        body = 'data: {"type":"a"}\ndata: {"type":"b"}\n: keep-alive\ndata: {"type":"c"}\n'
        self.assertEqual(chat_sse._chunks(body), 3)


if __name__ == "__main__":
    unittest.main()
