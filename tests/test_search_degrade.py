"""检索不可用**不该封掉对话** —— 辅助能力不该成为主能力的前置依赖。

## 这个 bug 长什么样

实测配置：`llm_provider=deepseek`（云端）+ `embedding_provider=ollama`（默认值）。
用户明明选了云端模型，却被拦在聊天入口，回一句：

    ⚠️ 本机 Ollama 服务未运行或不可达，无法调用本地模型。

然后让用户去装 Ollama、执行 `ollama pull bge-m3`。用户看到的是「我的云端模型坏了」，
于是去查 API Key、去换模型 —— 全找错方向。真正的解法（改 `embedding_provider`）
藏在提示的最后一条建议里。

根因在 `api.py` 的判定：

    if _prov == "ollama" or _emb == "ollama":   ← _emb 是默认值ollama，于是封掉整个入口

它问的是「有没有人依赖 Ollama」，而 Ollama 承担的是**两件不同的事**：
对话（它不可用就真没法聊）与向量检索（它不可用只是检索不了）。

## 现在的处置

    对话模型是 Ollama -> 拦（但要说清是**对话**模型不可用）
    仅检索依赖 Ollama -> 降级：告知「本次不用检索，直接作答」，照常送云端模型

同文件另一处 `provider == "ollama"` 的判定（模型切换端点）本来就只看 provider——
那说明第一处的 `or _emb` 是疏漏而非设计。
"""

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import api  # noqa: E402


class _AsyncChatMixin:
    """/api/chat 是**协程**（`async def`，内部 `return StreamingResponse(...)`），
    不是 async 生成器——所以要 `await` 拿到 Response，再消费它的 `body_iterator`。
    参数全是 `Form()`，不能按位置传。
    """

    def _collect(self, provider, embedding_provider, ollama_reachable):
        import asyncio
        import json
        captured = {}

        def fake_run(question, *a, **kw):
            captured["started"] = True
            yield {"type": "final", "text": "正常回答"}

        state = {"reachable": ollama_reachable, "guidance": "去装 Ollama",
                 "provider": provider, "embedding_provider": embedding_provider}
        with patch.object(api, "get_runtime", side_effect=lambda k, d="": {
            "llm_provider": provider, "embedding_provider": embedding_provider,
            "code_root": "", "llm_model": "m", "llm_api_key": "k",
        }.get(k, d)), \
             patch.object(api, "check_ollama", return_value=state), \
             patch.object(api.Agent, "run", fake_run):

            async def drive():
                resp = await api.chat(
                    question="你好", session_id="t-sdg", tool_mode="",
                    plan_mode="", web_mode="", thinking_mode="", images=None)
                events = []
                iterator = getattr(resp, "body_iterator", None)
                if iterator is not None:
                    async for raw in iterator:
                        chunk = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                        if not chunk.startswith("data: "):
                            continue
                        body = chunk[6:].strip()
                        if body and body != "[DONE]":
                            try:
                                events.append(json.loads(body))
                            except ValueError:
                                pass
                return events

            return asyncio.run(drive()), captured


class DownStreamTests(unittest.TestCase):
    """拦截提示的文案：必须说清是「对话」不可用，且不能误导人去查云端模型。"""

    def test_it_says_conversation_not_local_in_general(self):
        import json
        chunks = list(api._ollama_down_stream("引导内容"))
        payload = chunks[0][len("data: "):].strip()
        text = json.loads(payload)["text"]
        self.assertIn("对话", text,
                      "要说清是对话不可用——否则用户以为检索/嵌入坏了")
        self.assertIn("引导内容", text, "不能把真正的解决步骤丢掉")


class RoutingConditionTests(_AsyncChatMixin, unittest.TestCase):
    """核心断言：检索依赖 Ollama 不得触发拦截。"""

    def _chat_with(self, provider, embedding_provider, ollama_reachable):
        return self._collect(provider, embedding_provider, ollama_reachable)

    def test_a_cloud_llm_with_ollama_embeddings_is_not_blocked(self):
        """本轮的实测组合：deepseek + embedding=ollama + Ollama 没跑。"""
        events, captured = self._chat_with("deepseek", "ollama", False)
        self.assertNotIn("Ollama 服务未运行",
                         " ".join(str(e.get("text") or "") for e in events),
                         "云端对话模型被检索后端不可达牵连了——辅助能力不该绑住主能力")
        self.assertTrue(captured.get("started"), "对话应该照常跑起来")

    def test_the_user_is_told_which_part_is_broken(self):
        events, _ = self._chat_with("deepseek", "ollama", False)
        notices = [str(e.get("text") or "") for e in events
                   if e.get("type") == "notice"]
        self.assertTrue(notices, "降级必须有一条可见提示——否则用户不知道检索没跑")
        joined = " ".join(notices)
        self.assertIn("检索", joined)
        self.assertIn("与对话模型无关", joined,
                      "必须明确与对话模型无关，否则用户仍会以为云端模型坏了")

    def test_no_notice_when_ollama_is_reachable(self):
        events, _ = self._chat_with("deepseek", "ollama", True)
        joined = " ".join(str(e.get("text") or "") for e in events
                          if e.get("type") == "notice")
        self.assertNotIn("检索当前不可用", joined,
                         "Ollama 正常时不该报降级——那会造成不必要的疑虑")

    def test_a_local_llm_is_still_blocked(self):
        """对话模型本身就是 Ollama 时，必须拦——真的没法对话。"""
        events, captured = self._chat_with("ollama", "ollama", False)
        self.assertFalse(captured.get("started"),
                         "对话模型=ollama 且服务不可达时不该硬跑")
        joined = " ".join(str(e.get("text") or "") for e in events)
        self.assertIn("无法对话", joined)
        self.assertIn("对话模型", joined)


class GuidanceSeverityTests(unittest.TestCase):
    """`check_ollama` 的引导文案与严重级别：别再对着云端用户喊「去装 Ollama」。

    这是同一个误导的**第二半**：即便 /api/chat 不再封对话，`/api/config` 里的
    `ollama_status.guidance` 仍会让问答页弹黄色横幅。若不把「挡住对话」和
    「仅检索降级」分开，embedding_provider 默认 ollama 时每个云端用户都会看到
    「当前模型配置依赖本机 Ollama／或改用 qwen、deepseek」——而他已经在用 deepseek。
    """

    def _status(self, prov, emb, reachable, present=()):
        # ⚠ `EMBEDDING_MODEL` 也要钉住：它是**环境变量**（`config.py`），
        # 生产拿它去 `/api/tags` 的模型清单里对号。本机配的是 bge-m3、CI 的默认值
        # 是 text-embedding-v3 —— 不钉住的话，夹具给的模型名在 CI 上根本对不上，
        # 于是「Ollama 正常且模型已就位」被判成「模型没拉」→ 多出一条引导文案。
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                # 真实 urllib 返回 bytes——代码里有 .decode()，返回 str 会抛
                # AttributeError 并被 except 吞掉，表现为「明明可达却报不可达」。
                return ('{"models": [' + ",".join(
                    '{"name": "%s"}' % m for m in present) + ']}').encode("utf-8")

        def fake_open(url, timeout=None):
            if reachable:
                return _Resp()
            raise OSError("拒绝连接")

        with patch.object(api, "get_runtime", side_effect=lambda k, d="": {
                "llm_provider": prov, "embedding_provider": emb,
                "llm_model": "bge-m3" if prov == "ollama" else "deepseek-flash",
            }.get(k, d)), \
             patch.object(api, "EMBEDDING_MODEL", "bge-m3"), \
             patch.object(api._ollama_no_proxy, "open", side_effect=fake_open):
            return api.check_ollama(force=True)

    def test_embedding_only_outage_does_not_block_conversation(self):
        st = self._status("deepseek", "ollama", False)
        self.assertFalse(st["blocks_conversation"],
                         "仅检索不可用却被标成挡住对话——云端用户会以为模型坏了")
        self.assertTrue(st["search_degraded"])
        self.assertIn("不影响对话", st["guidance"])

    def test_embedding_only_guidance_does_not_tell_you_to_switch_llm(self):
        """旧文案建议「改用 qwen / deepseek」——用户已经在用 deepseek 了。"""
        st = self._status("deepseek", "ollama", False)
        self.assertNotIn("改用 qwen", st["guidance"],
                         "还在劝云端用户换模型：这条建议对已是 deepseek 的人毫无意义")

    def test_local_llm_outage_still_blocks(self):
        st = self._status("ollama", "ollama", False)
        self.assertTrue(st["blocks_conversation"], "对话模型=ollama 时确实没法聊")
        self.assertIn("对话模型", st["guidance"])

    def test_healthy_cloud_setup_has_no_guidance_at_all(self):
        st = self._status("deepseek", "ollama", True, present=["bge-m3"])
        self.assertEqual(st["guidance"], "")
        self.assertFalse(st["blocks_conversation"])
        self.assertFalse(st["search_degraded"])


class SourceGuardTests(unittest.TestCase):
    """防回归：**拦截**条件不能被改回「有人依赖 ollama 就封对话」。

    ## 这两条断言被重写过（原样会咬人）

    原先靠两段源码文本钉住实现形状：

        assertNotIn('if _prov == "ollama" or _emb == "ollama":')
        assertIn('elif _emb == "ollama":')

    改判定顺序（把 `route_for` 提到守卫之前，好让守卫知道这一轮会不会其实
    由云端服务）之后，两条都不成立了：

    · 第一条变成**误报**。`if _prov == "ollama" or _emb == "ollama":` 现在是
      **探活**条件——「谁依赖 Ollama 就探一下」本来就该 or，它不拦截任何东西。
      真正的拦截在下一行，条件里没有 `_emb`。断言盯错了行。

    · 第二条**钉住了 bug**。它要求降级分支必须写成 `elif`，而 `elif` 恰恰是
      「对话走云端、检索靠本机」时静默降级的原因：`_prov == "ollama"` 时
      走完拦截分支（或不拦），降级提示就被跳过，用户既没被拦也没被告知检索没了。

    所以改成盯**意图**而不是形状：找到真正 `return _ollama_down_stream(...)`
    那一行的 `if` 条件，断言它只认 `_prov`；并断言降级分支与拦截分支**相互独立**
    （不是 `elif`）。

    「云端救回对话后仍如实披露检索降级」的**行为**回归由
    `tests/test_cloud_route_survives_local_model_down.py::
    SearchDegradationIsStillDisclosedTests` 钉住——那里是按事件断言的，
    改源码形状不会让它失效。
    """

    def _chat_src(self):
        import inspect
        return inspect.getsource(api.chat)

    def _blocking_condition(self):
        """返回真正触发拦截的那行 `if` 条件（而不是探活那行）。"""
        lines = self._chat_src().splitlines()
        for i, ln in enumerate(lines):
            if "return StreamingResponse(_ollama_down_stream(" in ln:
                for j in range(i - 1, max(-1, i - 8), -1):
                    if lines[j].lstrip().startswith("if "):
                        return lines[j]
        self.fail(
            "找不到拦截分支的 if 条件。若实现改了形状，请同步更新本守卫——"
            "**不要**因此删掉它：它是本文件唯一的防回归网")

    def test_the_blocking_branch_only_triggers_for_the_llm_provider(self):
        cond = self._blocking_condition()
        self.assertIn("_prov", cond,
                      "拦截条件必须认对话模型 provider：%r" % cond)
        self.assertNotIn("_emb", cond,
                         "拦截条件里混进了 embedding provider——检索不可用不得封对话：%r" % cond)

    def test_the_downgrade_path_is_independent_of_the_blocking_branch(self):
        """降级分支不能是拦截分支的 `elif`（那会造成静默降级）。"""
        src = self._chat_src()
        self.assertIn("search_degraded_notice", src,
                      "降级路径不见了：检索不可用时会退回旧的封闭行为")
        self.assertNotIn("elif _emb", src,
                         "降级分支又变回 elif 了：对话走云端时会被静默跳过，"
                         "用户既没被拦也没被告知检索没了")
        self.assertIn('if _emb == "ollama"', src,
                      "降级分支应当是独立判断（_prov 为 ollama 时也要能披露降级）")


if __name__ == "__main__":
    unittest.main()