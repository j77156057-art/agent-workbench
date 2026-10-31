# -*- coding: utf-8 -*-
"""评测流量隔离：记忆**读**按被问项目，**写**改投独立 scope。

为什么值得单独钉：黄金题一跑就是几十题，过去全写进被问项目的记忆。实测真实项目
56 个记忆会话里 55 个是 `golden-*`，46 条候选全部来自评测或旧库迁移，还留下 12 条
pinned 的误判 error 钩子 —— 也就是说「记忆在长东西」这件事几乎全是评测流量制造的。
而基线可比性要求**读**不能变（改读等于改提示词），所以只能改写入 scope。
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import agent as agent_mod
import agent_memory
import config
from agent_runtime.memory import episodes, store, taskbook


class _LLM:
    provider = "stub"
    model = "stub"

    def __init__(self):
        self.messages = []

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        self.messages.append([dict(m) for m in messages])
        text = "Final Answer: 常量 OBS_MAX_CHARS 的默认值是 1200。"
        return iter([text]) if stream else text


class EvalMemoryIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_iso_")
        self._saved = {key: os.environ.get(key)
                       for key in ("DOCMIND_MEMORY_DIR", "DOCMIND_MEMORY_V2")}
        os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(self.tmp, "memory")
        config._RUNTIME.pop("memory_v2", None)
        config.set_runtime("memory_v2", True)
        self.project = "prj-real"
        self.scope = "eval-golden"

    def tearDown(self):
        # 真回合会提交异步固化任务；不排空就会在环境变量还原之后写进下一个用例的存储。
        from agent_runtime.memory import consolidate
        consolidate.drain()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._RUNTIME.pop("memory_v2", None)

    def _run(self, session_id, **kwargs):
        llm = _LLM()
        agent = agent_mod.Agent(llm=llm, session_id=session_id,
                                 project_id=self.project, **kwargs)
        list(agent.run("OBS_MAX_CHARS 默认值是多少", stream=True))
        return agent, llm

    def _episode_lines(self, project, session_id):
        target = episodes.path(project, session_id)
        if not os.path.exists(target):
            return 0
        with open(target, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def test_isolated_run_writes_only_into_the_eval_scope(self):
        self._run("golden-iso-1", memory_write_project=self.scope)
        self.assertEqual(self._episode_lines(self.scope, "golden-iso-1"), 1,
                         "评测回合应落在隔离桶里")
        self.assertEqual(self._episode_lines(self.project, "golden-iso-1"), 0,
                         "被问项目被写进了记忆 —— 隔离没生效")
        self.assertFalse(os.path.isdir(store.safe_path("projects", self.project, "session")),
                         "被问项目下不该出现任何评测会话目录")

    def test_default_keeps_today_behaviour(self):
        """**反向守卫**：不传隔离 scope 时必须逐字保持旧行为（写回本项目），
        否则真实会话的记忆会静默停止积累，而所有单测照绿。"""
        agent, _llm = self._run("web-real-1")
        self.assertEqual(agent.memory_write_project, "")
        self.assertEqual(self._episode_lines(self.project, "web-real-1"), 1)
        self.assertEqual(self._episode_lines(self.scope, "web-real-1"), 0)

    def test_reads_still_follow_the_asked_project(self):
        """读路径不受隔离影响：被问项目的 L3 任务书照样注入。"""
        taskbook.open_book(self.project, "评测隔离", ["必须先跑 pytest 才能勾掉"])
        _agent, llm = self._run("golden-iso-2", memory_write_project=self.scope)
        self.assertTrue(llm.messages, "模型一次都没被调用")
        joined = "\n".join(str(m.get("content") or "") for m in llm.messages[0])
        self.assertIn("必须先跑 pytest 才能勾掉", joined,
                      "写入隔离把读取也一起隔掉了 —— 基线会不可比")

    def test_run_golden_defaults_to_the_isolated_scope(self):
        """跑题脚本的默认值本身就是防线：谁忘了传参也不该污染真实项目。

        ⚠ **不许在这里 `import run_golden`。** 那个模块在导入期就写进程环境
        （`DOCMIND_TRACE=0`、`DOCMIND_GOLDEN_NO_LOCK=1`，它必须早于 import agent），
        于是 `agent_trace.TRACE_ENABLED` 与 `pricing._golden_mode()` 在**整个进程剩余
        时间里**被翻转。`unittest discover` 是单进程跑的：实测这一句 import 把该 lane
        的红从 30 放大到 146，且每次红的是同一批用例（trace/计价/预算类）。
        被验的对象是"源码里的默认值"，所以用 AST 读，不执行导入。
        """
        import ast
        import pathlib

        source = pathlib.Path(__file__).resolve().parents[1] / "run_golden.py"
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        consts = {node.targets[0].id: node.value.value for node in tree.body
                  if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                  and isinstance(node.value, ast.Constant)}
        self.assertEqual(consts.get("EVAL_MEMORY_SCOPE"), "eval-golden")

        run_one = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == "run_one")
        # run_one 的 memory_write_project 在 `*` 之后 —— 默认值落在 kw_defaults 里，
        # 只看 args.defaults 会读到 None（第一版就栽在这）。
        positional = len(run_one.args.args) - len(run_one.args.defaults)
        table = dict(zip([a.arg for a in run_one.args.args][positional:], run_one.args.defaults))
        table.update({arg.arg: value for arg, value
                      in zip(run_one.args.kwonlyargs, run_one.args.kw_defaults)
                      if value is not None})
        scope_default = table.get("memory_write_project")
        self.assertIsNotNone(scope_default, "run_one 的 memory_write_project 没有默认值")
        # 默认值写成 `= EVAL_MEMORY_SCOPE`（一个 Name），不是字面量；两种都要认，
        # 否则把常量名内联成字符串反而会让测试通过/失败得莫名其妙。
        if isinstance(scope_default, ast.Name):
            self.assertIn(scope_default.id, consts, "默认值引用了一个不存在的常量")
            self.assertEqual(consts[scope_default.id], "eval-golden")
        else:
            self.assertIsInstance(scope_default, ast.Constant)
            self.assertEqual(scope_default.value, "eval-golden")

    def test_v1_episode_memory_follows_the_same_isolation(self):
        """v2 分家了，v1 的 `episode` 记忆也得跟着走。

        否则评测照旧往真实项目的旧记忆里灌几十条，只是换了张表 —— 而 `memory_search`
        会把两张表一起回灌进提示词。
        """
        with patch.object(agent_memory, "DB_PATH", os.path.join(self.tmp, "mem.sqlite3")):
            self._run("golden-iso-3", memory_write_project=self.scope)
            kinds = {}
            for label, scope in (("eval", agent_memory.scope_key(self.scope, "developer")),
                                 ("real", agent_memory.scope_key(self.project, "developer"))):
                rows = agent_memory.recall(scope, "", limit=50, include_all=True)
                kinds[label] = sorted(str(dict(row).get("kind") or "") for row in rows)
            self.assertIn("episode", kinds["eval"],
                          "评测回合的 episode 没落到隔离桶：%s" % kinds["eval"])
            self.assertNotIn("episode", kinds["real"],
                             "被问项目的旧记忆里出现了评测 episode：%s" % kinds["real"])


if __name__ == "__main__":
    unittest.main()
