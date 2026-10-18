# -*- coding: utf-8 -*-
"""①②③④ 修复经**真实 /api/chat HTTP + SSE**端到端复验。

这份**不打单元层**——①②③④ 的单测（test_stub_falsifiable / test_provenance_traceable /
grep 截断披露 / test_chat_sse）都是直接调 `tools.read_file`、`tools.grep` 或
`output_audit.audit_evidence`，或在纯层判 `chat_sse.verdict`。它们证明的是**零件对**，
不是**这条零件真的接在 HTTP 出口上**。本文件把**真 Agent 经真 `/api/chat`**跑起来，
判据只看 SSE 流里透出的 `observation` / `reflection` / `final` 事件文本——即：

  · ① 截断整读之后，送达**之外**的尾段经 HTTP 拿到的是**真实正文**、不是假存根；
  · ② 送达**之内**重读经 HTTP 拿到的是**新的可证伪条件句**存根（"你此刻仍能看到"）；
  · ④ grep 触顶的"已达 40 条上限并截断"披露**原样**出现在 SSE observation 里；
  · ②③ 的门禁 FAIL 在**定位类问题**上经 HTTP 落成 `reflection` 事件并点名数字；
  · **修复回归**：同一份"确定性口吻 + 截断证据"的过度声称，即便问题**不是**定位类
    （不落 `_LOCATION_QUERY`、`required=False`），审计新增 `content_fail`、消费者放宽为
    `(required or content_fail) and not gate_ok` → 纯计数题谎报"准确总数"现在也经 HTTP
    外显 `reflection` 并在坚持不改时给 final 挂上"核验：FAIL"结论注记。

零网络前提沿用 `test_chat_sse.py`：压 `llm_provider=mock`/`embedding_provider=local`、
压 `check_ollama` 断言零调用、把会话 Agent 的 `llm` 经 `agent.LLMClient` 构造接缝换成
**队列剧本**（`QueueLM`）。剧本按 `self.calls` 逐拍吐出预写的 ReAct 文本，从而让
真工具、真审计、真 SSE 包装在确定性输入下跑完整一轮。
"""
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starlette.testclient import TestClient  # noqa: E402

import agent as agent_mod       # noqa: E402
import agent_memory             # noqa: E402
import agent_trace              # noqa: E402
import config                   # noqa: E402
import sessions                 # noqa: E402
import tools as tools_mod       # noqa: E402
import api                      # noqa: E402
from agent_runtime import chat_sse, output_audit  # noqa: E402

#: 一个够大的文件（1400 行）触发 read_file 整读 12000 字截断，尾段落在第 905 行。
BIG_LINES = 1400
#: grep 命中数要 > _HIT_CAP(40) 才触发"已达 40 条上限并截断"披露。
SPAWN_LINES = 100
SPAWN_PATTERN = "SPAWN_POINT"


class QueueLM:
    """按调用次序逐拍吐预写 ReAct 文本的剧本 LLM（逐字符流式）。

    `turns` 里每一项是**一整段**模型输出文本（含 `Thought:`/`Action:`/`Action Input:`
    或 `Final Answer:`）。`calls` 从 1 开始；耗尽后回一句无害收尾，绝不让循环挂死。
    """

    provider = "scripted"
    model = "scripted-queue"
    capability = {"vision": False}

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def chat(self, messages, stream=True, **kw):
        idx = self.calls
        self.calls += 1
        if idx < len(self.turns):
            text = self.turns[idx]
        else:
            text = "Thought: 剧本耗尽，兜底收尾\nFinal Answer: 收尾。"
        self.last_usage = {"prompt_tokens": 40,
                           "completion_tokens": max(1, len(text) // 3)}
        if not stream:
            return text

        def generate():
            for char in text:
                yield char
        return generate()

    def count_tokens(self, text):
        return max(0, len(str(text or "")) // 3)


class HttpE2EBase(unittest.TestCase):
    """搭好隔离环境：临时 code_root、压 provider、注入 QueueLM、真打 /api/chat。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.makedirs(os.path.join(self.tmp.name, "b"))
        with open(os.path.join(self.tmp.name, "b", "big.gd"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join("l%05d_%s" % (i, "x" * 40)
                               for i in range(1, BIG_LINES + 1)))
        with open(os.path.join(self.tmp.name, "spawns.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("\n".join("SPAWN_POINT_%d = spawn_config(%d)" % (i, i)
                               for i in range(1, SPAWN_LINES + 1)))

        for module, name, value in (
                (agent_trace, "TRACE_FILE", os.path.join(self.tmp.name, "traces.jsonl")),
                (sessions, "SESSIONS_DIR", os.path.join(self.tmp.name, "sessions")),
                (agent_memory, "DB_PATH", os.path.join(self.tmp.name, "memory.sqlite3"))):
            original = getattr(module, name)
            setattr(module, name, value)
            self.addCleanup(setattr, module, name, original)
        enabled = agent_trace.TRACE_ENABLED
        agent_trace.TRACE_ENABLED = True
        self.addCleanup(setattr, agent_trace, "TRACE_ENABLED", enabled)
        for key in ("llm_provider", "embedding_provider", "code_root", "llm_mode"):
            self.addCleanup(config.set_runtime, key, config.get_runtime(key))
        config.set_runtime("llm_provider", "mock")
        config.set_runtime("embedding_provider", "local")
        config.set_runtime("code_root", self.tmp.name)
        config.set_runtime("project_rules", "")
        config.set_runtime("llm_mode", "fixed")
        # read_file 送达登记表按 code_root 累积：每条用例前清空，避免跨用例污染。
        tools_mod.clear_read_files()
        self.addCleanup(tools_mod.clear_read_files)

        before = set(api._SESSION_AGENTS)

        def _drop_new():
            for key in [k for k in api._SESSION_AGENTS if k not in before]:
                api._SESSION_AGENTS.pop(key, None)
        self.addCleanup(_drop_new)

        self.client = TestClient(api.app)

    def run_turn(self, question, turns, session_id):
        """真打一次 /api/chat，返回 (events, final_text, reflection_texts, obs_texts)。"""
        lm = QueueLM(turns)
        with patch.object(agent_mod, "LLMClient", lambda *a, **k: lm), \
                patch.object(api, "check_ollama",
                             return_value={"reachable": True}) as probe:
            response = self.client.post("/api/chat",
                                        data={"question": question,
                                              "session_id": session_id})
        self.assertEqual(probe.call_count, 0, "provider 压住却还是打了 ollama 探活——非零网络")
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertGreaterEqual(lm.calls, 1, "剧本 LLM 一次都没被调用——用的不是它")
        events = chat_sse.parse_events(response.content)
        obs_texts = [str(e.get("text") or "") for e in events if e.get("type") == "observation"]
        reflections = [str(e.get("text") or "") for e in events if e.get("type") == "reflection"]
        finals = [e for e in events if e.get("type") == "final"]
        final_text = str(finals[-1].get("text") or "") if finals else ""
        return events, final_text, reflections, obs_texts


class ReadFixThroughHttpTests(HttpE2EBase):
    """①②：截断读与假存根在 HTTP/SSE 出口的原样透出。"""

    def test_truncated_read_tail_reread_returns_real_not_stub_over_http(self):
        # 三拍：整读(截断) → 尾段(送达之外，应真读) → 送达内(应命中②存根)。
        turns = [
            "Thought: 先看整份\nAction: read_file\nAction Input: path: b/big.gd",
            "Thought: 尾段没看到，按行号补读\nAction: read_file\n"
            "Action Input: path: b/big.gd start: 900 end: 910",
            "Thought: 送达内再读一次应当命中存根\nAction: read_file\n"
            "Action Input: path: b/big.gd start: 5 end: 8",
            "Thought: 证据够了\nFinal Answer: big.gd 第 905 行确有内容。",
        ]
        _ev, _fin, _rf, obs = self.run_turn(
            "b/big.gd 里第 905 行有什么内容？", turns, "http-e2e-1")
        self.assertEqual(len(obs), 3, "应有三条 observation：%r" % obs)
        whole, tail, served = obs[0], obs[1], obs[2]
        # ① 前置：整读确实触发了截断（否则后两拍没意义）。
        self.assertIn("已截断", whole, "构造的整读未在 HTTP 出口体现截断：%r" % whole[:200])
        # ①：送达之外的尾段经 HTTP 给的是真实正文，绝不被假存根挡回。
        self.assertIn("l00905", tail, "尾段经 HTTP 没拿到真实正文：%r" % tail[:200])
        self.assertNotIn("未变化", tail, "送达之外被假存根挡掉——① 护栏在 HTTP 层被破")
        # ②：送达内重读经 HTTP 命中**新的可证伪条件句**存根。
        self.assertIn("未变化", served, "送达内应命中存根：%r" % served[:200])
        self.assertIn("你此刻仍能在上下文里看到", served,
                      "② 的条件句存根措辞没经 HTTP 透出：%r" % served[:200])


class GrepCapThroughHttpTests(HttpE2EBase):
    """④：grep 触顶披露原样出现在 SSE observation。"""

    def test_grep_hit_cap_disclosure_surfaces_in_http_observation(self):
        turns = [
            "Thought: 数一下刷点\nAction: grep\nAction Input: %s" % SPAWN_PATTERN,
            "Thought: 触顶了\nFinal Answer: 看到很多刷点。",
        ]
        _ev, _fin, _rf, obs = self.run_turn(
            "%s 出现在哪些地方？" % SPAWN_PATTERN, turns, "http-e2e-grep")
        joined = "\n".join(obs)
        self.assertIn("条上限并", joined,
                      "grep 触顶披露未经 HTTP 透出：%r" % joined[:300])
        self.assertIn("截断", joined, "披露里应说明被截断")


class AuditSurfacingThroughHttpTests(HttpE2EBase):
    """②③④ 门禁：定位类问题上的过度声称，经 HTTP 落成 reflection 事件。"""

    def test_location_overclaim_fails_gate_and_reflects_over_http(self):
        # 定位类问题（含 逻辑/哪个文件/实现）→ required=True。剧本先 grep 触顶
        # （产生 step.truncated），再用"准确总数"确定性口吻收尾 → count_evidence_complete=False。
        turns = [
            "Thought: 先扫刷点\nAction: grep\nAction Input: %s" % SPAWN_PATTERN,
            "Thought: 统计\nFinal Answer: 敌人生成逻辑在 spawns.py，准确总数：194。",
            # reflection 回环后再来一拍，仍不过门 → 落 verdict note。
            "Thought: 再报一次\nFinal Answer: 敌人生成逻辑在 spawns.py，准确总数：194。",
        ]
        _ev, final_text, reflections, _obs = self.run_turn(
            "敌人刷新逻辑在哪个文件实现？给个准确的敌人总数。", turns, "http-e2e-ref")
        self.assertTrue(reflections,
                        "定位类过度声称应在 HTTP 层产出 reflection 事件")
        joined_refl = "\n".join(reflections)
        # reflection 点名确定性计数与截断的碰撞（ev_label / count hint）。
        self.assertIn("194", joined_refl,
                      "reflection 没点名声称的数字 194：%r" % joined_refl[:300])
        self.assertTrue(("截断" in joined_refl) or ("确定性口吻" in joined_refl),
                        "reflection 未交代截断证据/确定性口吻的碰撞：%r" % joined_refl[:300])
        # 二次仍不过门 → final 带上核验结论。
        self.assertIn("核验", final_text,
                      "定位类未过门的 final 应带核验结论注记：%r" % final_text[:300])


class PureCountSurfacingFixedTests(HttpE2EBase):
    """修复回归：纯计数题（不落 _LOCATION_QUERY）的确定性过度声称**现在必须外显**。

    此前这道门只对定位类问题发声（消费者 `if required and not gate_ok`），纯计数题
    谎报"准确总数"即便 `audit_evidence` 亲口算出 `verdict=FAIL`，HTTP 层也一个
    reflection 都不发。修法：审计新增 `content_fail`（答案自招的计数/来源过度声称，
    与问题类型无关），消费者放宽为 `(required or content_fail) and not gate_ok`。
    本用例把这条边界**从"静默放行"翻成"必须外显"**钉死在 HTTP 出口。
    """

    def test_pure_count_overclaim_now_surfaces_reflection_over_http(self):
        # 三拍：grep 触顶（step.truncated）→ 确定性口吻"准确总数：194" →
        # reflection 回环后**仍**坚持同一数字 → 落 verdict note。问题不含任何定位词。
        turns = [
            "Thought: 先扫刷点\nAction: grep\nAction Input: %s" % SPAWN_PATTERN,
            "Thought: 统计\nFinal Answer: 这批刷点配置准确总数：194。",
            "Thought: 再报一次\nFinal Answer: 这批刷点配置准确总数：194。",
        ]
        _ev, final_text, reflections, _obs = self.run_turn(
            "这批刷点配置一共多少个？给一个准确的总数。", turns, "http-e2e-count-fixed")
        # 修复核心：非定位题现在也要产出 reflection 事件。
        self.assertTrue(reflections,
                        "纯计数题的确定性过度声称应经 HTTP 外显 reflection（修复回归）")
        joined_refl = "\n".join(reflections)
        self.assertIn("194", joined_refl,
                      "reflection 没点名声称的数字 194：%r" % joined_refl[:300])
        self.assertTrue(("截断" in joined_refl) or ("确定性" in joined_refl),
                        "reflection 未交代截断证据/确定性口吻的碰撞：%r" % joined_refl[:300])
        # 二次仍不过门 → final 带上核验结论，且点名那条自相矛盾的声称（非空泛 FAIL）。
        self.assertIn("核验", final_text,
                      "纯计数题未过门的 final 应带核验结论注记：%r" % final_text[:300])
        self.assertIn("FAIL", final_text, "核验结论应如实标 FAIL：%r" % final_text[:300])
        self.assertIn("准确总数", final_text,
                      "核验注记应点名『准确总数』这条被截断证据支撑的声称：%r" % final_text[:300])

    def test_audit_itself_computes_fail_and_content_fail_when_not_required(self):
        """把修复的数据源钉死：审计对同一输入**确实**判 FAIL，并置 `content_fail=True`。

        `required=False`（非定位题）但 `content_fail=True`——正是消费者放宽后用来
        驱动外显的那一位。缺了它，纯计数题依旧够不到这道门。
        """
        steps = [{"action": "grep(%s)" % SPAWN_PATTERN, "ok": True, "truncated": True}]
        audit = output_audit.audit_evidence(
            {"text": "这批刷点配置准确总数：194。",
             "trace": {"steps": steps}},
            question="这批刷点配置一共多少个？给一个准确的总数。",
            code_root=self.tmp.name)
        self.assertFalse(audit["required"], "该问题不该被判成定位类")
        self.assertFalse(audit["gate_ok"], "确定性计数 × 截断证据本应判门失败")
        self.assertEqual(audit["verdict"], "FAIL", "审计本身应已算出 FAIL")
        self.assertIs(audit["checks"].get("count_evidence_complete"), False)
        self.assertTrue(audit.get("content_fail"),
                        "答案自招的计数过度声称必须置 content_fail=True，否则非定位题仍不外显")


if __name__ == "__main__":
    unittest.main()
