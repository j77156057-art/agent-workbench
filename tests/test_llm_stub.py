"""`agent_runtime/llm_stub.py`：让整条问答链路在零网络下跑到**生产 provider 那条路**。

`tests/test_chat_sse.py` 的真回合是往 Agent 上**注入**剧本 LLM——那验的是 ReAct 循环、
工具执行与 SSE 包装，但**不经过** `llm.py` 的 provider 选择、`base_url` 解析与 OpenAI SDK
那条真实客户端路径；而 `dev_apptest` 把 uvicorn 起成**子进程**，注入法在那里根本用不上。

这里补的就是那一层：把 `provider = "custom"`（产品里的正式 provider）指到本模块起的
回环假服务上，走 `LLM_BASE_URL` 那条路（`llm.py:567-571`），**零共享文件改动**。
"""
import json
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

import agent_memory            # noqa: E402
import agent_trace             # noqa: E402
import config                  # noqa: E402
import sessions                # noqa: E402
import api                     # noqa: E402
from agent_runtime import chat_sse, llm_stub  # noqa: E402

#: 系统提示里解释 ReAct 格式的那段文字**也含 "Observation:" 这个词**——第一版
#: `observation_snippet` 在所有消息里搜它，于是把提示当成了工具结果、第一轮就作答、
#: 一个工具都不调。夹具里显式带上这段，就是为了让这条错法**有测试盯着**。
SYSTEM_WITH_FORMAT = (
    "你在每轮要么输出 `Action:` + `Action Input:`，要么以 `Final Answer:` 收尾。"
    "工具结果会以 `Observation:` 前缀回灌给你。"
)


class ReplyShapeTests(unittest.TestCase):
    def test_the_first_turn_calls_the_asset_tool(self):
        text = llm_stub.reply_for([{"role": "user", "content": "像素风角色素材有哪些？"}])
        self.assertEqual(llm_stub.tool_name_in(text), "search_assets")
        self.assertIn("Action Input:", text)

    def test_the_final_turn_quotes_the_observation_verbatim(self):
        observation = "· Pixel Platformer ［2d-sprites｜CC0］ 像素风平台跳跃角色与地块"
        text = llm_stub.reply_for([
            {"role": "user", "content": "像素风角色素材有哪些？"},
            {"role": "assistant", "content": "Thought: 查一下\nAction: search_assets\n"
                                            "Action Input: 像素风角色"},
            {"role": "user", "content": "Observation: %s" % observation}])
        self.assertTrue(text.startswith("Thought:"), text)
        self.assertIn("Final Answer:", text)
        self.assertIn(observation, text)

    def test_the_format_hint_in_the_system_prompt_is_not_a_tool_result(self):
        """**只看最后一条**：系统提示里那段格式说明含 "Observation:" 这个词。"""
        text = llm_stub.reply_for([{"role": "system", "content": SYSTEM_WITH_FORMAT},
                                   {"role": "user", "content": "像素风角色素材有哪些？"}])
        self.assertEqual(llm_stub.observation_snippet(
            [{"role": "system", "content": SYSTEM_WITH_FORMAT},
             {"role": "user", "content": "像素风角色素材有哪些？"}]), "")
        self.assertIn("Action:", text, "第一轮必须去调工具，不能直接作答")

    def test_a_trailing_harness_hint_is_not_quoted(self):
        """观察末尾常常跟着一句 harness 提示——引述它等于把凭据换成一句套话。

        实测：那次的 final 成了「根据检索到的内容，（请基于观察继续，或给出 Final Answer）
        —— 以上就是可用清单。」，`final_quotes_a_tool_observation` 因此红。
        """
        messages = [{"role": "user", "content":
                     "Observation: · Pixel Platformer ［2d-sprites｜CC0］ 像素风平台跳跃角色\n"
                     "· Space Shooter Redux ［2d-sprites｜CC0］ 384 个太空射击素材\n"
                     "（请基于观察继续，或给出 Final Answer）"}]
        snippet = llm_stub.observation_snippet(messages, action="search_assets")
        self.assertTrue(snippet.startswith("·"), "引述的必须是工具产出的那一行：%r" % snippet)
        self.assertNotIn("请基于观察继续", snippet)
        # 引述进去之后，凭据确实出现在 final 里
        final = llm_stub.reply_for(messages, action="search_assets")
        self.assertIn(snippet, final)
        self.assertNotIn("请基于观察继续", final)

    def test_list_dir_skips_its_header_line(self):
        """`list_dir` 的首行是表头，凭据（随机文件名）在带 `[FILE]` 的那一行。"""
        messages = [{"role": "user", "content":
                     "Observation: 目录 (代码库根) 共 1 项：\n"
                     "[FILE] probe_601a0c.py  (10 字节)"}]
        snippet = llm_stub.observation_snippet(messages, action="list_dir")
        self.assertIn("probe_601a0c.py", snippet)
        self.assertNotIn("共 1 项", snippet)
        self.assertIn("probe_601a0c.py", llm_stub.reply_for(messages, action="list_dir"))

    def test_list_dir_first_turn_lists_the_root(self):
        text = llm_stub.reply_for([{"role": "user", "content": "玩家受伤扣血的逻辑在哪里实现？"}],
                                  action="list_dir")
        self.assertEqual(llm_stub.tool_name_in(text), "list_dir")
        self.assertIn("Action Input: .", text)

    def test_a_corrective_prompt_makes_it_stop_calling_tools(self):
        """harness 的纠正提示一出现就必须收尾——不然会空转到被强制收尾。

        实测（问答页那一档）：桩不认 `Evidence check:` / `Reflection:` / `Nudge:`，
        于是一直回 `Action: list_dir`，一轮问答跑了 6 轮，最后靠"模型反复空转"的兜底停，
        渲染出来的是兜底证据块而不是一条干净的答复。
        """
        base = [{"role": "user", "content": "玩家受伤扣血的逻辑在哪里实现？"},
                {"role": "assistant", "content": "Thought: x\nAction: list_dir\nAction Input: ."},
                {"role": "user", "content": "Observation: 目录 (代码库根) 共 1 项：\n"
                                           "[FILE] probe_23bb39.py  (10 字节)"}]
        for prompt in ("Evidence check: 这是定位类问题，但最终回答缺少可核验的 read_file 原文。",
                       "Reflection: 你刚刚已经用完全相同的参数调用过 list_dir。",
                       "Nudge: 禁止再调用任何工具。请立即基于上文全部 Observation 输出 "
                       "`Final Answer:`"):
            with self.subTest(prompt=prompt[:20]):
                text = llm_stub.reply_for(base + [{"role": "user", "content": prompt}],
                                          action="list_dir")
                self.assertEqual(llm_stub.tool_name_in(text), "",
                                 "纠正提示之后还在调工具：%r" % text[:80])
                self.assertIn("Final Answer:", text)
                self.assertIn("probe_23bb39.py", text, "凭据要落在收尾的答复里")

    def test_a_later_observation_still_counts(self):
        text = llm_stub.reply_for([
            {"role": "system", "content": SYSTEM_WITH_FORMAT},
            {"role": "assistant", "content": "Thought: x\nAction: search_assets\n"
                                            "Action Input: 像素"},
            {"role": "user", "content": "Observation: · UI Pack ［ui｜CC0］ 322 个 UI 套件"}])
        self.assertIn("Final Answer:", text)
        self.assertIn("UI Pack", text)

    def test_the_last_user_text_handles_both_content_shapes(self):
        self.assertEqual(llm_stub.last_user_text(
            [{"role": "user", "content": "甲"}, {"role": "assistant", "content": "x"},
             {"role": "user", "content": "乙"}]), "乙")
        self.assertEqual(llm_stub.last_user_text(
            [{"role": "user", "content": [{"type": "text", "text": "拼"}, {"x": 1},
                                          {"type": "text", "text": "起来"}]}]), "拼起来")
        self.assertEqual(llm_stub.last_user_text([]), "")


class FrameShapeTests(unittest.TestCase):
    def frames(self, text="你好世界"):
        body = llm_stub.sse_frames(text, model="stub-1").decode("utf-8")
        rows = [line[len("data:"):].strip() for line in body.splitlines()
                if line.startswith("data:")]
        return body, rows

    def test_it_ends_with_done(self):
        _body, rows = self.frames()
        self.assertEqual(rows[-1], "[DONE]")

    def test_the_content_is_reassembled_verbatim(self):
        body, rows = self.frames("一二三四五" * 20)
        pieces = [json.loads(row)["choices"][0]["delta"].get("content", "")
                  for row in rows if row != "[DONE]"
                  and json.loads(row).get("choices")]
        self.assertEqual("".join(pieces), "一二三四五" * 20)

    def test_the_last_frame_with_choices_says_stop(self):
        """带正文的分片之后还有两个收尾块（stop 帧与 usage 帧），所以不能按 `[-2]` 取。"""
        _body, rows = self.frames()
        with_choices = [json.loads(row) for row in rows
                        if row != "[DONE]" and json.loads(row).get("choices")]
        self.assertEqual(with_choices[-1]["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(with_choices[0]["choices"][0]["finish_reason"])

    def test_usage_rides_on_a_choicless_frame(self):
        """`llm.py:278` 是 `if not chunk.choices: continue` —— usage 就挂在这种块上。"""
        _body, rows = self.frames()
        last = json.loads(rows[-2])          # 最后一块（`[DONE]` 之前）
        self.assertEqual(last["choices"], [])
        self.assertIn("usage", last)

    def test_the_long_text_is_not_one_single_frame(self):
        _body, rows = self.frames("很长的一段正文" * 30)
        self.assertGreater(len(rows), 3, "整段塞一个分片就不叫流式了")


class StubServerTests(unittest.TestCase):
    def test_it_answers_models_and_chat_completions(self):
        import urllib.request
        handle = llm_stub.start()
        try:
            with urllib.request.urlopen(handle["url"] + "/models", timeout=10) as resp:
                self.assertIn("stub-1", resp.read().decode())
            body = json.dumps({"model": "stub-1", "stream": False,
                               "messages": [{"role": "user", "content": "像素风角色素材"}]})
            req = urllib.request.Request(handle["url"] + "/chat/completions",
                                         data=body.encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode())
            self.assertIn("search_assets",
                          payload["choices"][0]["message"]["content"])
        finally:
            llm_stub.stop(handle)

    def test_env_for_points_the_child_at_this_very_stub(self):
        handle = llm_stub.start()
        try:
            env = llm_stub.env_for(handle)
            self.assertEqual(env["LLM_PROVIDER"], "custom")
            self.assertEqual(env["LLM_BASE_URL"], handle["url"])
            self.assertTrue(env["LLM_BASE_URL"].startswith("http://127.0.0.1:"))
            self.assertEqual(env["EMBEDDING_PROVIDER"], "local")
        finally:
            llm_stub.stop(handle)


class ProviderPathTurnTests(unittest.TestCase):
    """真打一次 `/api/chat`：**不注入 LLM**，让 `llm.py` 走它自己的 provider 选择。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_llm_stub_")
        for module, name, value in (
                (agent_trace, "TRACE_FILE", os.path.join(self.tmp, "traces.jsonl")),
                (sessions, "SESSIONS_DIR", os.path.join(self.tmp, "sessions")),
                (agent_memory, "DB_PATH", os.path.join(self.tmp, "memory.sqlite3"))):
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, getattr(module, name))
        agent_trace.TRACE_ENABLED = True
        self.handle = llm_stub.start()
        self.addCleanup(llm_stub.stop, self.handle)
        for key, value in (("llm_provider", "custom"), ("llm_base_url", self.handle["url"]),
                           ("llm_model", "stub-1"), ("llm_api_key", "stub-key"),
                           ("embedding_provider", "local"), ("llm_mode", "fixed"),
                           ("code_root", self.tmp)):
            self.addCleanup(config.set_runtime, key, config.get_runtime(key))
            config.set_runtime(key, value)
        before = set(api._SESSION_AGENTS)
        self.addCleanup(lambda: [api._SESSION_AGENTS.pop(k, None)
                                 for k in list(api._SESSION_AGENTS) if k not in before])
        self.client = TestClient(api.app)

    def test_a_turn_completes_through_the_real_provider_path(self):
        def _explode(*_a, **_k):        # pragma: no cover - 被调用就是失败
            raise AssertionError("走了 Ollama 探活：这一档要的是零网络")
        with patch.object(api, "check_ollama", _explode):
            resp = self.client.post("/api/chat", data={"question": "像素风角色素材有哪些？",
                                                       "session_id": "stub-case"})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        judged = chat_sse.verdict(chat_sse.parse_events(resp.content),
                                  chunk_count=chat_sse._chunks(resp.content))
        self.assertTrue(judged["passed"], chat_sse.render(judged))
        self.assertIn("search_assets", judged["observed"]["actions"])

    def test_the_answer_quotes_what_the_tool_returned(self):
        resp = self.client.post("/api/chat", data={"question": "像素风角色素材有哪些？",
                                                   "session_id": "stub-case-2"})
        events = chat_sse.parse_events(resp.content)
        final = [row for row in events if row.get("type") == "final"][-1]
        observations = [str(row.get("text") or "") for row in events
                        if row.get("type") == "observation"]
        self.assertTrue(observations, "这一轮没有任何 observation")
        self.assertTrue(chat_sse._quotes(str(final.get("text") or ""), observations),
                        "final 没有原样引述工具观察：%r" % str(final.get("text"))[:200])


if __name__ == "__main__":
    unittest.main()
