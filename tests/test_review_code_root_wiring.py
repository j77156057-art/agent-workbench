# -*- coding: utf-8 -*-
"""逐任务复核**必须拿到代码根**——缺省会让每一个引用都被判「打不开」。

## 实测事故（`wf-6cd1ee3ca3c0`，八次尝试 `references_exist` 全 False）

子代理用 `read_file(path: behaviors/enemy.gd)` 读到 1543 字全文，结论 2390 字、
行号齐备、反思 `issues=0`——却被 Harness 复核否掉：

    不过: behaviors/enemy.gd (exists=False read_verified=False)

## 关键对照：同一时刻，两份审计，两个结果

探针落在 `_audit_live_answer`（**传了** `get_runtime("code_root")`）：

    {"path": "behaviors/enemy.gd", "exists": true,  "in_root": true}

落库的 `review.evidence_audit`（`api_routes/agent.py` 的 `review_output`，
**没传** `code_root`）：

    不过: behaviors/enemy.gd (exists=False read_verified=False)

`api.py` 那一处 `workflow_callbacks` 已经用 `set_context_code_root` 设好了根，
`game_workflow.py:2941` 也传了 `code_root=state.project_root`——**只有逐任务
这条漏了**。

## 失败机制

`audit_evidence` 里 `root = Path(code_root).resolve() if code_root else None`。
`root=None` 时相对路径**留在原地**，落到进程当前目录（Agent 工作台 自己仓库）去
`is_file()`——那里当然没有 `behaviors/`。

而 `in_root` 的初值是 `True`、只在 `root is not None` 时才被覆盖，于是

    exists=False  +  in_root=True

**这一对组合是「压根没查」的唯一指纹**，不是「路径存在但判定矛盾」。
本文件把它钉死，就是为了让下一次同类故障在测试里就现形。
"""
import io
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CONCL = (
    "# behaviors/enemy.gd 逐行机制清单\n\n"
    "- `behaviors/enemy.gd:6` — `const GRAVITY: float = 24.0`\n"
    "- `behaviors/enemy.gd:7` — `const CONTACT_DAMAGE: int = 10`\n"
)
STEPS = [{"action": "read_file(path: behaviors/enemy.gd)", "ok": True}]


class TheRunnerMustPassCodeRootTests(unittest.TestCase):
    """读源码断言接线：这是本条的全部内容——一个漏掉的参数。"""

    def setUp(self):
        with io.open(os.path.join(ROOT, "api_routes", "agent.py"), encoding="utf-8") as fh:
            self.src = fh.read()

    def _call_window(self, span=420):
        """`review_output(out, …)` 那次调用的原文窗口。

        **不用正则跨括号匹配**：调用里嵌了 `int(...)`，`[^)]*` 会在第一个右括号
        处截断——第一版就这么写，当场失败。这类「按源码文本断言」的守卫最容易
        被自己的正则绊倒，所以这里只取固定窗口的子串检查。
        """
        at = self.src.find("review_output(out")
        self.assertNotEqual(at, -1, "找不到逐任务的 review_output 调用")
        return self.src[at:at + span]

    def test_the_per_task_review_passes_a_code_root(self):
        self.assertIn("code_root", self._call_window(),
                      "逐任务的 review_output 没传 code_root —— 缺省时 root=None，"
                      "每个引用都会在进程当前目录下被判成打不开")

    def test_it_falls_back_to_the_request_scoped_root(self):
        """`workflow_callbacks` 已用 contextvar 设好根，这里只读。"""
        self.assertIn('get_runtime("code_root", "")', self.src,
                      "没回落到请求作用域的根——project_root 为空时又会变成空串")

    def test_it_also_passes_the_question(self):
        """`question` 决定要不要走定位类门禁；漏传会让门禁被整体跳过。"""
        self.assertIn("question", self._call_window(),
                      "review_output 没传 question——定位类门禁会被跳过，"
                      "等于复核根本没收紧")


class TheNoRootFingerprintTests(unittest.TestCase):
    """`exists=False` + `in_root=True` 是「压根没查」的唯一指纹。"""

    def test_audit_reports_that_fingerprint_when_no_root_is_given(self):
        from agent_runtime.output_audit import audit_evidence
        row = audit_evidence(
            {"text": CONCL, "trace": {"steps": STEPS}},
            question="敌人追击逻辑在哪一行？", code_root="",
            require_read=True)
        refs = row.get("references") or []
        self.assertTrue(refs)
        for ref in refs:
            self.assertFalse(ref.get("exists"),
                             "空 root 下不该声称路径存在——那是拿进程 cwd 当证据")
        # 这个组合必须在测试里显形：一旦有人改判 in_root 的初值，
        # 「没查」就会伪装成「查了但不通过」，那才是最难查的一种。
        self.assertTrue(all(ref.get("in_root") for ref in refs),
                        "in_root 的初值变了——指纹失效，故障会换个形状复发")

    def test_with_a_real_root_the_same_reference_verifies(self):
        """对照：给了真root，同一个引用必须通过。"""
        from agent_runtime.output_audit import audit_evidence
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "behaviors"))
            with open(os.path.join(tmp, "behaviors", "enemy.gd"), "w",
                      encoding="utf-8") as handle:
                handle.write("const GRAVITY: float = 24.0\n" * 10)
            row = audit_evidence(
                {"text": CONCL, "trace": {"steps": STEPS}},
                question="敌人追击逻辑在哪一行？", code_root=tmp,
                require_read=True)
            self.assertTrue(row["gate_checks"]["references_exist"],
                            "给了真 root 还是判打不开：%s" % json.dumps(
                                row.get("references"), ensure_ascii=False)[:300])
            self.assertTrue(row["gate_checks"]["reference_lines_valid"])


if __name__ == "__main__":
    unittest.main()