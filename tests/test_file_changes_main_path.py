# -*- coding: utf-8 -*-
"""主路径的「本轮改了哪些文件」必须进 `file_changes`，进而进 episode 的 `files`。

## 为什么单独钉这一条

2026-11-01 修学习环漏斗口径时实测出来的：真实项目 21 个回合里 **3 个调了 `create_file`**，
但 episode 的 `files` 全是空 —— 于是漏斗报「改了文件 0」，断点被归到
「真实回合 → 改了文件」，看着像"这个项目只用问答、不写代码"，实际是**记录漏了**。

根因是路径不对称：`file_changes` 只有两处生产者 —— 子代理的 observation 出口
（`agent.py` 的 `_run_child`）与 resume 账本的合并，主代理自己写盘那条路
（`_run` 里发 observation 事件的地方）只把 `artifacts` 挂到事件上，从不落进清单。
`Agent.__init__` 里那个 `self._pending_file_changes` 更是**只初始化、没人写**，
看起来像有过这套机制，实际从没生效过。

## 三条判据为什么这么定

1. 正向：调 `create_file` 的回合，episode `files` 必须含该路径。
2. 反向：只调 `read_file` 的回合，`files` 必须仍为空 —— 否则任何读工具带个 path
   就会被记成"改了文件"，把漏报换成误报（一样是说谎）。
3. 去重：同一文件在一回合里被新建又修改，清单里只能出现一次 —— 重复条目会虚报
   改动面，也会把 `_snapshot` 的 100 条上限白白喂满。
   （形状上还有条硬约束：**不许填 `kind`**，它是 `preview_adapters` 的适配器路由键，
   填过 `"file"` 实测让产物清单变 0 条；episode 只留 path 清单，所以这条由代码注释钉，
   不由本文件断言。）
"""
import os
import shutil
import tempfile
import unittest

import agent as agent_mod
import config
from agent_runtime.memory import consolidate, episodes


class _ScriptedLLM:
    """按脚本逐轮回话：先出 Action，再出 Final Answer。"""

    provider = "stub"
    model = "stub"

    def __init__(self, replies):
        self._replies = list(replies)
        self.messages = []

    def count_tokens(self, text):
        return max(1, len(str(text or "")) // 4)

    def chat(self, messages, stream=True, **kw):
        self.messages.append([dict(m) for m in messages])
        text = self._replies.pop(0) if self._replies else "Final Answer: 收尾。"
        return iter([text]) if stream else text


class MainPathFileChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dm_fc_")
        self.repo = tempfile.mkdtemp(prefix="dm_fc_repo_")
        self._saved = {key: os.environ.get(key)
                       for key in ("DOCMIND_MEMORY_DIR", "DOCMIND_MEMORY_V2")}
        os.environ["DOCMIND_MEMORY_DIR"] = os.path.join(self.tmp, "memory")
        config._RUNTIME.pop("memory_v2", None)
        config.set_runtime("memory_v2", True)
        self._prev_root = config.get_runtime("code_root")
        config.set_runtime("code_root", self.repo)
        self.project = "prj-fc"

    def tearDown(self):
        consolidate.drain()  # 真回合会提交异步固化，不排空会写进下一个用例的存储
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config._RUNTIME.pop("memory_v2", None)
        if self._prev_root is None:
            config._RUNTIME.pop("code_root", None)
        else:
            config.set_runtime("code_root", self._prev_root)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def _run_turn(self, session_id, replies):
        llm = _ScriptedLLM(replies)
        agent = agent_mod.Agent(llm=llm, session_id=session_id, project_id=self.project)
        list(agent.run("随便做点什么", stream=True))
        rows = episodes.latest(self.project, session_id, limit=5)
        self.assertTrue(rows, "这一回合没落进 episode —— 测试没测到真路径")
        return rows[0], llm

    def test_write_tool_on_main_path_lands_in_episode_files(self):
        row, _llm = self._run_turn("web-write", [
            "Thought: 建个文件\nAction: create_file\n"
            "Action Input: path: notes/demo.py\nnew_text: # demo\nprint('hi')\n",
            "Final Answer: 已创建 notes/demo.py。",
        ])
        self.assertIn("notes/demo.py", list(row.get("files") or []),
                      "主代理写盘没进 file_changes —— 漏斗会把「改了文件」报成 0")

    def test_same_file_touched_twice_is_listed_once(self):
        """去重靠 `_seen_file_changes`：新建后又改同一个文件，清单里只能出现一次。

        原样重复会虚报改动面，也会把 `_snapshot` 的 100 条上限喂满。
        """
        row, _llm = self._run_turn("web-dup", [
            "Thought: 建个文件\nAction: create_file\n"
            "Action Input: path: notes/dup.py\nnew_text: # v1\n",
            "Thought: 再改一次\nAction: apply_edit\n"
            "Action Input: path: notes/dup.py\nnew_text: # v2\n",
            "Final Answer: 两处都写了同一个文件。",
        ])
        files = list(row.get("files") or [])
        self.assertEqual(files.count("notes/dup.py"), 1,
                         "同一文件被记了多次：%s" % files)

    def test_read_only_turn_records_no_file_change(self):
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "a.py"), "w", encoding="utf-8") as handle:
            handle.write("X = 1\n")
        row, _llm = self._run_turn("web-read", [
            "Thought: 先看看\nAction: read_file\nAction Input: path: src/a.py\n",
            "Final Answer: X 是 1。",
        ])
        self.assertEqual(list(row.get("files") or []), [],
                         "只读工具也被记成改动 —— 漏报修成了误报，一样是说谎")


if __name__ == "__main__":
    unittest.main()
