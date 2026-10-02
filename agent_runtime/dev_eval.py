"""Dev-lane 能力评测与回归门：代码任务这条链到底通不通。

现有评测只覆盖检索（retrieval_eval）与工作流（workflow_eval），代码任务一条都没有——
于是「新工具是否真的可用、护栏是否还在」全凭手感。本模块补这一段，语义与
workflow_eval 一致：本地确定性评测是唯一事实源，CI 与人工跑同一份题、同一套判定。

测的是【harness 这条循环】而不是模型智商：每题给一个 fixture 代码库 + 一段固定的
ReAct 轨迹，用真 Agent + 真工具跑，然后三处同时断言——
① 工具确实被调用过；② 观察结果满足条件（含/不含某些文本）；③ 【文件系统真的变成什么样】。
第 ③ 点是关键：只测「模型调了对的工具」而不管副作用，补丁打了一半、护栏被绕过这类
问题照样溜过去。模型质量评测走 run_golden.py + agent_eval.py（需要真模型，不在这个门里）。

用法：
  python -m agent_runtime.dev_eval --self-check
  python -m agent_runtime.dev_eval --self-check --baseline .github/dev-eval-baseline.json --json
  python -m agent_runtime.dev_eval --write-baseline .github/dev-eval-baseline.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping

import config
import temp_state


def _check(name: str, ok: Any, detail: Any = "") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": str(detail)[:300]}


# --------------------------------------------------------------------------- #
# 脚本化模型：按预定顺序吐出 ReAct 步骤，最后一轮必须收尾
# --------------------------------------------------------------------------- #

class _ScriptedLLM:
    """把 cases 里的 steps 当成模型输出，其余与真实回路完全一致。"""

    def __init__(self, steps: list[str]):
        self._steps = list(steps)
        self.calls = 0

    def chat(self, messages, stream=True, **kwargs):  # noqa: ANN001, ARG002
        index = min(self.calls, len(self._steps) - 1)
        self.calls += 1
        return [self._steps[index]]

    def count_tokens(self, text):  # noqa: ARG002
        return 0


def _react(tool: str, arg: str) -> dict[str, str]:
    """结构化工具步：题库里存【用了哪个工具 + 什么入参】，渲染交给回放器。

    存成渲染好的字符串会让「这题动了哪些工具」查不出来——覆盖率体检和
    validate_cases 就都成了摆设。
    """
    return {"tool": tool, "input": arg}


def _final(text: str) -> dict[str, str]:
    return {"final": text}


def _render(step: dict[str, str]) -> str:
    if "tool" in step:
        return "Thought: 先看证据\nAction: %s\nAction Input: %s" % (
            step["tool"], step.get("input") or "")
    return "Thought: 够了\nFinal Answer: %s" % (step.get("final") or "完成")


# --------------------------------------------------------------------------- #
# fixture 代码库
# --------------------------------------------------------------------------- #

_TS_FIXTURE = {
    "frontend/src/App.tsx": (
        "// handleResize 只在这里被提到，不算引用\n"
        "export function handleResize() { return 1 }\n"
        "export function mount() { return handleResize() }\n"
    ),
    "frontend/src/notes.ts": "export const text = 'handleResize 出现在字符串里'\n",
}

_PATCH_TARGET = "one\ntwo\nthree\n"
_OTHER_TARGET = "alpha\nbeta\ngamma\n"

_CLEAN_PATCH = """--- a/a.txt
+++ b/a.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

_BAD_SECOND_PATCH = _CLEAN_PATCH + """--- a/b.txt
+++ b/b.txt
@@ -1,3 +1,3 @@
 alpha
-this line does not exist
+whatever
 gamma
"""


def _git_ready() -> bool:
    try:
        return subprocess.run(["git", "--version"], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _git(root: str, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("git %s 失败：%s" % (" ".join(args),
                                              (proc.stdout + proc.stderr)[:200]))


def _build_fixture(kind: str, root: str) -> str:
    """按题型铺一个 fixture 代码库，返回跳过原因（""=不跳）。"""
    files: dict[str, str] = {}
    commits = 0
    if kind == "ts-refs":
        files = dict(_TS_FIXTURE)
    elif kind == "git-history":
        files = {"a.py": "VALUE = 1\n", "b.py": "other = 2\n", "README.md": "# demo\n"}
        commits = 2
    elif kind == "patch":
        files = {"a.txt": _PATCH_TARGET, "b.txt": _OTHER_TARGET,
                 "bad.py": "def broken(:\n    return 1\n", "ok.py": "print(1)\n"}
    elif kind == "lanes":
        files = {"frontend/src/workbench/live.ts": "export const a = 1\n",
                 "agent_runtime/realtime_omni.py": "X = 1\n"}
    elif kind == "serve":
        files = {"index.html": "<p>hi</p>\n"}
    elif kind == "proposal":
        # 一个干净提交，之后故意留下一处未提交改动 + 一个未跟踪文件：
        # 这正是 dev_propose 的输入形态，也是它最容易把仓库弄脏的时刻
        files = {"a.py": "x = 1\n", "notes.md": "# notes\n"}
        commits = 1
    elif kind == "plain":
        files = {"notes.md": "# notes\n"}
    for rel, body in files.items():
        full = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        Path(full).write_text(body, encoding="utf-8", newline="\n")
    if commits:
        if not _git_ready():
            return "git 不可用"
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "eval@example.com")
        _git(root, "config", "remote.origin.url",
             "https://github.com/acme/rag-agent.git")
        _git(root, "config", "user.name", "First Author")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "first: add a.py",
             "--author=First Author <first@example.com>")
        if commits >= 2:
            _git(root, "commit", "-q", "--allow-empty", "-m", "second: touch b.py",
                 "--author=Second Author <second@example.com>")
        if kind == "proposal":
            Path(os.path.join(root, "a.py")).write_text("x = 2\n", encoding="utf-8",
                                                        newline="\n")
            Path(os.path.join(root, "new.md")).write_text("# new\n", encoding="utf-8",
                                                          newline="\n")
    return ""


# --------------------------------------------------------------------------- #
# cases
# --------------------------------------------------------------------------- #

def _case_read_file_escape(root: str, run: Callable[..., dict]) -> list[dict]:
    outside = os.path.join(os.path.dirname(root), "secret.txt")
    Path(outside).write_text("TOP-SECRET\n", encoding="utf-8")
    result = run("read_file", "../secret.txt")
    return [_check("escape_refused", "拒绝" in result["text"] or "越界" in result["text"],
                   result["text"][:160]),
            _check("secret_not_leaked", "TOP-SECRET" not in result["text"], result["text"][:160])]


def _case_command_gate(root: str, run: Callable[..., dict]) -> list[dict]:
    result = run("run_command", "rm -rf /")
    return [_check("blocked", "拦" in result["text"] or "拒" in result["text"],
                   result["text"][:160]),
            _check("did_not_run", "rm" not in result["text"].lower()
                   or "拦截" in result["text"] or "拦下" in result["text"], result["text"][:160])]


def _case_mcp_server_read_only(root: str, run: Callable[..., dict]) -> list[dict]:
    from agent_runtime import mcp_server

    buffer = io.StringIO()
    from contextlib import redirect_stdout
    with redirect_stdout(buffer):
        mcp_server.main(["--list"])
    listed = buffer.getvalue().split()
    with redirect_stdout(buffer):
        mcp_server.main(["--list", "--allow-writes"])
    with_writes = buffer.getvalue().split()
    return [_check("read_only_default", "dev_glob" in listed and "dev_patch" not in listed,
                   " ".join(listed[:6])),
            _check("writes_opt_in", "dev_patch" in with_writes and "dev_move" in with_writes,
                   " ".join(with_writes[-6:])),
            _check("exec_never_exposed", not any(
                name in set(listed) | set(with_writes)
                for name in ("run_command", "python_exec", "dev_mcp_call")), "名单泄漏")]


def _case_mcp_inline_opt_in(root: str, run: Callable[..., dict]) -> list[dict]:
    from agent_runtime import mcp_bridge

    class Fake:
        def __init__(self):
            self.list_calls: list[str] = []

        def server_configs(self, _root):
            return [{"key": "off", "transport": "stdio", "enabled": True},
                    {"key": "on", "transport": "stdio", "enabled": True,
                     "inline_tools": True}]

        def list_tools(self, _root, key):
            self.list_calls.append(key)
            return {"ok": True, "tools": [{"name": "ping", "description": "d",
                                          "input_schema": {"type": "object",
                                                           "properties": {
                                                               "msg": {"type": "string"}},
                                                           "required": ["msg"]}}]}

        def call_tool(self, _root, key, name, arguments=None, timeout=30):
            return {"ok": True, "text": "pong:%s" % (arguments or {}).get("msg", "")}

    fake = Fake()
    real = sys.modules.get("mcp_client")
    mcp_bridge.reset_cache()
    sys.modules["mcp_client"] = fake
    try:
        found = mcp_bridge.discover(root)
        names = [row["name"] for row in found["entries"]]
        specs = mcp_bridge.build_specs(found["entries"])
        out = list(specs.values())[0].func("hi") if specs else ""
    finally:
        if real is None:
            sys.modules.pop("mcp_client", None)
        else:
            sys.modules["mcp_client"] = real
        mcp_bridge.reset_cache()
    return [_check("only_marked_inlined", names == ["mcp__on__ping"], str(names)),
            _check("unmarked_not_handshaked", fake.list_calls == ["on"], str(fake.list_calls)),
            _check("callable", "pong:hi" in out, out[:120])]


def _case_propose_keeps_the_repo_clean(root: str, run: Callable[..., dict]) -> list[dict]:
    """dev_propose 的价值前提：产物写到仓库外，打包不会把 `git status` 弄脏。

    用 dev_git_diff 自己当探针——它会把未跟踪文件单列出来，产物一旦落进工作树就显形。
    """
    before = run("dev_git_diff", "stat: true")["text"]
    out = run("dev_propose", "title: eval 交付产物\ntest_plan: pytest -k dev\nlane: EVAL")["text"]
    after = run("dev_git_diff", "stat: true")["text"]
    patch_line = next((row.split("：", 1)[1].strip() for row in out.splitlines()
                       if row.strip().startswith("补丁：")), "")
    inside = bool(patch_line) and os.path.normpath(patch_line).startswith(
        os.path.normpath(root))
    return [_check("artifact_made", "提案已生成" in out and "未提交、未推送" in out, out[:160]),
            _check("artifacts_outside_repo", bool(patch_line) and not inside, patch_line[:160]),
            _check("status_not_polluted", before == after,
                   "打包前后仓库状态不一致：%r vs %r" % (before[:80], after[:80]))]


def _case_ci_never_invents_status(root: str, run: Callable[..., dict]) -> list[dict]:
    """没 token、连不上时不能编一个「看起来通过」的状态出来。"""
    out = run("dev_ci_status", "per_page: 3")["text"]
    return [_check("reports_incomplete", "未完成" in out, out[:160]),
            _check("no_invented_green", not any(word in out for word in ("通过", "全部绿", "success")),
                   out[:160])]


def _case_media_fixture_closes_without_a_device(root: str, run: Callable[..., dict]) -> list[dict]:
    """语音回路不该只能靠人对着麦克风喊：夹具内容与线上包形必须算得出来。

    这条刻意【不启动浏览器】——CI 上也要能跑；浏览器回环归 tests/test_media_fixture.py。
    """
    import wave

    from agent_runtime import media_fixture

    out = run("dev_media", "action: build\nscript: sil:0.3 talk:0.7 sil:1.3")["text"]
    fixtures = media_fixture.build_fixtures(root, script="sil:0.3 talk:0.7 sil:1.3", stamp="eval")
    with wave.open(str(fixtures["audio"]), "rb") as handle:
        shape = (handle.getnchannels(), handle.getsampwidth(), handle.getframerate())
        frames = handle.getnframes()
    probe = media_fixture.MediaProbe()
    tail_ok = garbage_ok = False
    report: dict[str, Any] = {"violations": [], "packets": []}

    def packet(sequence: int, payload: bytes) -> bytes:
        header = json.dumps({"v": 1, "type": "audio.chunk", "sequence": sequence,
                             "captured_at": int(time.time() * 1000)})
        return header.encode("utf-8") + b"\n" + payload

    try:
        url = probe.start()
        from websockets.sync.client import connect
        with connect(url, open_timeout=5) as client:
            client.send(json.dumps({"v": 1, "type": "hello"}))
            client.recv(timeout=5)
            client.send(b'{"v":9,"type":"audio.chunk","sequence":1,"captured_at":1}\n1234')
            client.send(packet(1, media_fixture.build_pcm(media_fixture.parse_script("talk:0.1"))))
            for sequence in range(2, 2 + media_fixture.TAIL_CHUNKS):
                client.send(packet(sequence, bytes(media_fixture.CHUNK_BYTES)))
            reply = json.loads(client.recv(timeout=5))
            tail_ok = reply.get("type") == "model.delta" and reply.get("final") is True
        report = probe.report()
        garbage_ok = (bool(report["violations"])
                      and len(report["packets"]) == 1 + media_fixture.TAIL_CHUNKS)
    except Exception as exc:  # noqa: BLE001 - 门禁要如实报告缺什么依赖
        return [_check("probe_runs", False, "%s: %s" % (type(exc).__name__, str(exc)[:160]))]
    finally:
        probe.stop()
    pattern = str(fixtures["expected_pattern"])
    return [_check("build_is_reachable", "假设备启动参数" in out, out[:160]),
            _check("fake_device_flags_present",
                   any(flag.startswith("--use-file-for-fake-audio-capture=")
                       for flag in fixtures["flags"]), fixtures["flags"]),
            _check("fixture_is_16k_mono_pcm16",
                   shape == (1, 2, 16_000) and frames == int(fixtures["duration"] * 16_000),
                   "%s %s" % (shape, frames)),
            _check("tail_is_at_least_one_second", pattern.endswith("." * media_fixture.TAIL_CHUNKS),
                   pattern),
            _check("gate_shape_opens_and_closes",
                   fixtures["gate_shape"]["opens_gate"] and fixtures["gate_shape"]["closes_gate"],
                   fixtures["gate_shape"]),
            _check("silence_tail_closes_the_turn", tail_ok, ""),
            _check("junk_is_a_violation_not_a_drop", garbage_ok, report["violations"])]


def _case_page_action_refuses_and_never_claims_success(root: str, run: Callable[..., dict]) -> list[dict]:
    """交互原语最贵的错是「点了但没生效却报成功」。这条不启动浏览器也要把护栏钉住。"""
    import json as _json

    from agent_runtime import page_action

    class _Stub:
        def __init__(self, resolve):
            self.resolve = resolve
            self.console, self.failed_requests, self.runtime_errors = [], [], []
            self.events: list[tuple[str, dict]] = []

        def call(self, method, params=None):
            self.events.append((method, params or {}))
            return {"data": ""}

        def evaluate(self, expression):
            if "MutationObserver" in expression:
                return 0
            if "__docmindTarget = null" in expression:
                return dict(self.resolve)
            return {"count": 1, "found": True, "visible": True, "text": "x",
                    "href": "http://127.0.0.1:1/", "title": "t"}

        def drain(self, seconds=0.5, **_kwargs):
            pass

    class _Stack:
        """替会话占位的空栈：门禁只验护栏，不需要真的收浏览器。"""

        def close(self):
            return None

    def session(resolve):
        holder = _Stack()
        row = page_action._Session("eval%03d" % len(page_action._sessions), holder, _Stub(resolve),
                                   Path(root), "http://127.0.0.1:1/", {}, None, None)
        with page_action._lock:
            page_action._sessions[row.id] = row
        return row, holder

    geometry = {"tag": "button", "text": "保存", "visible": True, "disabled": False,
                "x": 10, "y": 10, "w": 40, "h": 20, "id": "save"}
    ambiguous = {"count": 2, "nth": 0, "matched": geometry, "view": {"width": 900, "height": 500},
                 "href": "http://127.0.0.1:1/", "title": "t", "candidates": [geometry, geometry]}
    hidden = {"count": 1, "nth": 0, "matched": {**geometry, "visible": False},
              "view": {"width": 900, "height": 500}, "href": "http://127.0.0.1:1/", "title": "t",
              "candidates": [{**geometry, "visible": False}]}
    results = []
    for label, shape, word in (("ambiguous", ambiguous, "命中多个"), ("hidden", hidden, "不可见")):
        row, holder = session(shape)
        refused, message = False, ""
        try:
            page_action.click(root, page=row.id, selector="#save")
        except page_action.PageActionError as exc:
            refused, message = True, str(exc)
        finally:
            dispatched = bool(row.devtools.events)
            page_action._sessions.pop(row.id, None)
            try:
                holder.close()
            except Exception:  # noqa: BLE001 - 替身栈，收尾失败不影响判定
                pass
        results.append(_check("%s_refused" % label, refused and word in message, message[:160]))
        results.append(_check("%s_dispatched_nothing" % label, not dispatched,
                              "定位没成立却还是派发了事件"))
    missing = run("dev_page_action", "action: click\nselector: #save")["text"]
    bad = run("dev_page_action", "action: teleport")["text"]
    expression_probe = []

    def capture(expression):
        expression_probe.append(expression)
        return None

    stub = _Stub(ambiguous)
    stub.evaluate = capture
    page_action._evaluate(stub, page_action.RESOLVE_JS,
                          {"selector": "", "text": 'a");window.pwned=1;//', "nth": 0, "scope": ""})
    prefix = "(" + page_action.RESOLVE_JS + ")("
    payload = expression_probe[0][len(prefix):-1] if expression_probe else "{}"
    try:
        escaped = _json.loads(payload).get("text") == 'a");window.pwned=1;//'
    except ValueError:
        escaped = False
    before = {"mutations": 10, "console": 0, "failed": 0, "runtime": 0,
              "href": "http://127.0.0.1:1/", "title": "t"}
    error_seen = page_action._verdict(before, {**before, "mutations": 30, "console": 1})
    still = page_action._verdict(before, dict(before))
    return results + [
        _check("needs_a_session", "page" in missing and ("缺少" in missing or "找不到" in missing),
               missing[:160]),
        _check("unknown_action_is_text", "不支持" in bad, bad[:160]),
        _check("arguments_travel_as_literals", escaped and expression_probe[0].count("window.pwned") == 1,
               (expression_probe[0][:160] if expression_probe else "没有生成表达式")),
        _check("errors_outrank_dom_changes", error_seen["verdict"] == "error_seen", error_seen),
        _check("no_change_is_not_success", still["verdict"] == "no_change", still),
    ]


def _case_testrun_reads_failures_and_reruns_only_them(root: str, run: Callable[..., dict]) -> list[dict]:
    """跑测试→读失败→只重跑失败：整段必须是结构化的，且 0 收集绝不能读成通过。"""
    from agent_runtime import test_run

    tests = Path(root) / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    tests.joinpath("test_good.py").write_text("def test_ok():\n    assert True\n",
                                              encoding="utf-8", newline="\n")
    tests.joinpath("test_bad.py").write_text("def test_wrong():\n    assert 1 == 2, '算错了'\n",
                                             encoding="utf-8", newline="\n")
    Path(root).joinpath("pytest.ini").write_text("[pytest]\n", encoding="utf-8", newline="\n")
    first = test_run.run(root, timeout=180)
    recorded = test_run.load_failures(root)
    rerun = test_run.run(root, only_failed=True, timeout=180)
    empty = test_run.run(root, paths="tests/test_not_there.py", timeout=180)
    hostile = test_run.run(root, paths="--collect-only", timeout=180)
    mismatch = test_run.run(root, runner=lambda argv, **kw: {
        "ok": True, "exit_code": 1, "timed_out": False, "error": "",
        "output": "5 passed in 0.01s\n"})
    return [_check("a_red_suite_is_red", not first["ok"] and first["counts"]["failed"] == 1,
                   {"counts": first["counts"], "checks": first["checks"]}),
            _check("the_failure_is_named_with_a_reason",
                   bool(first["failures"])
                   and first["failures"][0]["nodeid"] == "tests/test_bad.py::test_wrong"
                   and "算错了" in first["failures"][0]["reason"], first["failures"]),
            _check("failures_are_recorded_for_the_rerun",
                   recorded == ["tests/test_bad.py::test_wrong"], recorded),
            _check("only_failed_reruns_just_that_test",
                   not rerun["ok"] and rerun["scope"]["mode"] == "only_failed"
                   and rerun["counts"]["failed"] == 1 and rerun["counts"]["passed"] == 0,
                   {"scope": rerun["scope"], "counts": rerun["counts"]}),
            _check("zero_collected_is_never_green",
                   not empty["ok"] and not empty["checks"]["tests_collected"], empty["checks"]),
            _check("option_shaped_path_starts_no_process",
                   not hostile["ok"] and hostile.get("command") in (None, []), hostile.get("error")),
            _check("counts_and_exit_code_must_agree",
                   not mismatch["ok"] and not mismatch["checks"]["counts_agree_with_exit"],
                   mismatch["checks"]),
            _check("missing_coverage_is_declared_not_skipped",
                   not test_run.run(root, coverage=True, timeout=180)["ok"]
                   and not test_run.coverage_available() or test_run.coverage_available(),
                   "coverage_available=%s" % test_run.coverage_available())]


def _case_vue_guard_can_go_red(root: str, run: Callable[..., dict]) -> list[dict]:
    """组件守卫的判定表在【没有 node / 没有浏览器】时也必须可验——否则这条门禁就是装饰。

    真挂载那一半在 `tests/test_vue_component.py` 的现场类里（含把组件**副本**改坏两条反证）；
    这里钉的是"红了才有意义"的那部分：判定表、参数闸门、以及页面必须按组件自己声明的
    emits 建监听。
    """
    from agent_runtime import vue_component as vcomp

    state = {"mounted": True, "rendered": True, "errors": [], "warnings": [],
             "declaredEmits": ["focus"], "emits": [{"name": "focus", "args": ["t2"]}],
             "facts": {"present:.wmd-panel": True, "absent:.nope": True,
                       "count:.wmd-item": 2, "text:1/2": True}}
    cfg = {"props": {}, "expect_present": [".wmd-panel"], "expect_absent": [".nope"],
           "counts": [[".wmd-item", "2"]], "texts": ["1/2"], "expect_emit": "focus",
           "expect_emit_args": ["t2"]}
    ok = vcomp.evaluate(state, cfg, [], [], synced=True)
    label_only = vcomp.evaluate(dict(state, emits=[{"name": "focus", "args": ["写作"]}]),
                                cfg, [], [], synced=True)
    undeclared = vcomp.evaluate(dict(state, emits=[]), dict(cfg, expect_emit="pick"),
                                [], [], synced=True)
    blind = vcomp.evaluate(state, {"props": {}, "expect_present": [], "expect_absent": [],
                                   "counts": [], "texts": [], "expect_emit": "",
                                   "expect_emit_args": None}, [], [], synced=True)
    no_read = vcomp.evaluate(dict(state, facts={}), cfg, [], [], synced=False)
    warned = vcomp.evaluate(dict(state, warnings=["Missing prop: active"]), cfg, [], [],
                            synced=True)
    page = (Path(root) / "page.html")
    vcomp.mount_page("app.mjs", cfg, page)
    html = page.read_text(encoding="utf-8")
    fe = Path(root) / "frontend"
    (fe / "src").mkdir(parents=True, exist_ok=True)
    fe.joinpath("package.json").write_text('{"name":"x"}\n', encoding="utf-8", newline="\n")
    fe.joinpath("src", "Ok.vue").write_text("<template><p>ok</p></template>\n",
                                            encoding="utf-8", newline="\n")
    fe.joinpath("src", "store.ts").write_text(
        "export function openThing(p) { return p }\n", encoding="utf-8", newline="\n")
    (fe / "other").mkdir(parents=True, exist_ok=True)
    fe.joinpath("other", "Real.vue").write_text("<template><p>x</p></template>\n",
                                                encoding="utf-8", newline="\n")
    # 第二层（mount 包装：驱动共享状态）的离线判据——真凭据在现场类里，这里钉"能不能红"
    shared = dict(cfg, setup=[{"call": "openThing", "args": []}], store_call="openThing")
    drove = vcomp.evaluate(dict(state, setupRan=["openThing"]), shared, [], [], synced=True)
    skipped = vcomp.evaluate(state, shared, [], [], synced=True)
    thrown = vcomp.evaluate(dict(state, setupRan=["openThing"],
                                 errors=["setup openThing 抛错：x"]), shared, [], [], synced=True)

    def refuses_setup_target(name: str) -> bool:
        try:
            vcomp.parse_request(fe, component="src/Ok.vue", composable="src/store.ts",
                                store_call=name)
        except vcomp.VueComponentError:
            return True
        return False

    def refuses(rel: str) -> bool:
        try:
            vcomp.component_file(fe, rel)
        except vcomp.VueComponentError:
            return True
        return False

    def refused_by_the_fence(rel: str) -> bool:
        """`other/Real.vue` 磁盘上真的有：它被拒必须拒在**围栏**上，
        不能拒在"碰巧不存在"上——后者让删掉围栏的变异照样绿（实测）。"""
        try:
            vcomp.component_file(fe, rel)
        except vcomp.VueComponentError as exc:
            return "相对" in str(exc)
        return False

    hostile = ("../outside.vue", "/etc/passwd.vue", "src/../x.vue", "other/Ok.vue",
               "src/Nope.vue", "src/Ok.txt")
    return [_check("a_faithful_table_passes", ok["passed"], ok["failed"]),
            _check("emit_label_instead_of_id_is_red",
                   not label_only["passed"]
                   and "emit_args_exact" in label_only["failed"], label_only["failed"]),
            _check("undeclared_emit_name_is_red",
                   not undeclared["passed"]
                   and "emit_declared_by_component" in undeclared["failed"], undeclared["failed"]),
            _check("mounting_without_assertions_is_red",
                   not blind["passed"] and "assertions_declared" in blind["failed"],
                   blind["failed"]),
            _check("a_failed_state_read_is_named_not_masked",
                   "state_read_back" in no_read["failed"], no_read["failed"]),
            _check("vue_warning_is_red", "no_vue_warnings" in warned["failed"],
                   warned["failed"]),
            _check("listeners_come_from_the_component", "Comp.emits" in html, html[:120]),
            _check("hostile_component_paths_are_refused",
                   all(refuses(rel) for rel in hostile), [rel for rel in hostile
                                                           if not refuses(rel)]),
            _check("the_real_component_is_not_refused", refuses("src/Ok.vue") is False, ""),
            _check("existing_file_outside_src_is_refused_by_the_fence",
                   refused_by_the_fence("other/Real.vue"), ""),
            _check("nth_zero_differs_from_unset",
                   vcomp.click_plan(".a, .b#0") == [{"selector": ".a", "nth": ""},
                                                    {"selector": ".b", "nth": 0}],
                   vcomp.click_plan(".a, .b#0")),
            _check("setup_rows_must_all_have_run",
                   "setup_applied" not in drove["failed"]
                   and "setup_applied" in skipped["failed"],
                   [drove["failed"], skipped["failed"]]),
            _check("a_thrown_setup_call_is_red", "setup_applied" in thrown["failed"],
                   thrown["failed"]),
            _check("reading_state_never_clicks_the_page",
                   "new MutationObserver(collect)" in html, html[-160:]),
            _check("store_call_must_be_a_real_export",
                   refuses_setup_target("nope") and not refuses_setup_target("openThing"), ""),
            _check("the_page_always_gets_a_store_binding",
                   "export const STORE = {}" in vcomp.entry_source("a.vue")
                   and "import * as STORE from" in vcomp.entry_source("a.vue", "s.ts"), "")]


def _case_apptest_refuses_a_stale_page_as_current(root: str, run: Callable[..., dict]) -> list[dict]:
    """整页守卫在【没有浏览器、没有后端】时也必须能验它的判定表与围栏。

    真起服务那一半在 `tests/test_app_test.py` 的现场类里（含"页面不存在"与"后端起不来"
    两条反证）；这里钉的是最容易骗自己的三类假绿：①拿**旧构建**当现在的代码；
    ②只信注册接口的 200；③点了导航却没换视图／现场没收回。
    """
    from agent_runtime import app_test as ap

    good = {"ready": True, "isolated": True, "activated": True, "shell": 1, "tabs": 4,
            "rows": 1, "fixture_in_tree": True, "console_errors": [], "failed_requests": [],
            "click_ok": True, "active_before": "概览", "active_after": "素材",
            "content_before": "467:aaaa", "content_after": "182:bbbb",
            "body_truncated": False, "texts": {}}
    want = {"tab": "素材", "texts": [], "tabs": 4}
    fresh = {"built": True, "fresh": True, "stale_by": 0, "detail": "x"}
    clean = {"removed": True, "job_stopped": True, "live_pages": 0}
    judged = ap.evaluate(good, want, fresh, clean)
    stale = ap.evaluate(good, want, dict(fresh, fresh=False, stale_by=900), clean)
    still_the_same_view = ap.evaluate(dict(good, active_before="素材"), want, fresh, clean)
    no_fixture = ap.evaluate(dict(good, fixture_in_tree=False), want, fresh, clean)
    left_running = ap.evaluate(good, want, fresh, dict(clean, live_pages=1))
    no_shell = ap.evaluate(dict(good, shell=0, tabs=3), want, fresh, clean)
    truncated = ap.evaluate(dict(good, body_truncated=True, texts={"资源管理器": False}),
                            dict(want, texts=["资源管理器"]), fresh, clean)
    listing = {"current": "prj-1", "projects": [{"project_id": "prj-1"}]}
    state_dir = Path(root) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    def stream_fail(over: dict) -> list[str]:
        """流式判定表：基线是"形状全对"，只翻一样东西，看是不是恰好红那一条。"""
        good = {"native": True, "mic_on": True,
                "heard": [{"chunks": 29, "bytes": 92800, "peak": 0.685}],
                "replied": [{"bytes": 92800, "round": 1}, {"bytes": 92800, "round": 2}],
                "rounds": [1, 2]}
        good.update(over)
        table = ap.stream_checks(good)
        return [name for name, ok in table.items() if not ok]

    def stream_rounds() -> list[int]:
        """两条字幕必须各配各的轮次：正则跨行贪就会把第一条配到最后一条。"""
        text = ("你说 | 29 片 · 92800 字节 · 峰值 0.685 | AI | 收到 92800 字节，峰值 0.685，"
                "第 1 轮剧本回复。" + chr(10)
                + "你说 | 31 片 · 99200 字节 · 峰值 0.700 | AI | "
                "收到 99200 字节，峰值 0.700，第 2 轮剧本回复。")
        return ap.read_turns(text, "当前模式：原生实时 · 可打断")["rounds"]

    def refuses(fn, *args) -> bool:
        try:
            fn(*args)
        except ap.AppTestError:
            return True
        return False

    return [_check("a_faithful_integration_table_passes", judged["passed"], judged["failed"]),
            _check("a_stale_build_is_never_current_code",
                   not stale["passed"]
                   and "bundle_built_after_latest_source" in stale["failed"], stale["failed"]),
            _check("a_200_from_the_register_call_alone_is_not_enough",
                   not ap.activate(200, {"current": "", "projects": []},
                                   {"project_id": "prj-1"})["ok"]
                   and ap.activate(200, listing, {"project_id": "prj-1"})["ok"], ""),
            _check("an_empty_state_root_is_not_isolation",
                   not ap.state_isolated(state_dir, {"ok": True, "listed": ["prj-1"]}), ""),
            _check("clicking_a_tab_that_stays_active_is_red",
                   "view_changed_on_click" in still_the_same_view["failed"],
                   still_the_same_view["failed"]),
            _check("the_fixture_name_has_to_reach_the_tree",
                   "backend_data_reaches_page" in no_fixture["failed"], no_fixture["failed"]),
            _check("leftovers_are_red", "artifacts_cleaned" in left_running["failed"],
                   left_running["failed"]),
            _check("an_emptier_page_is_not_a_rendered_page",
                   {"shell_mounted", "nav_renders_every_tab"} <= set(no_shell["failed"]),
                   no_shell["failed"]),
            _check("a_truncated_read_is_not_evidence",
                   "page_text_read_wholly" in truncated["failed"], truncated["failed"]),
            _check("only_the_two_local_api_paths_are_reachable",
                   all(refuses(ap.http_json, url) for url in (
                       "http://example.com/api/projects", "http://10.0.0.5/api/projects",
                       "http://127.0.0.1/api/ingest_code")), ""),
            _check("page_paths_cannot_escape_the_site",
                   all(refuses(ap.page_path, bad) for bad in ("//evil/x", "../etc/passwd",
                                                              "/a?b=1", "/a b")), ""),
            _check("the_child_keeps_its_environment",
                   bool([k for k in ap.server_env(state_dir) if k.upper() == "PATH"])
                   and str(ap.server_env(state_dir)[
                       "DOCMIND_STATE_ROOT"]) == str(state_dir), "")]


def _case_headcheck_catches_an_untracked_reference(root: str, run: Callable[..., dict]) -> list[dict]:
    """HEAD 自检：同一份代码，差一个 `git add`，判红→判绿。

    这题的存在理由就是本仓 2026-10-01 那次：pathspec 提交绕过索引，引用未入库模块的
    import 进了 HEAD，工作树里跑测试永远绿，单独检出 HEAD 必红。
    """
    from agent_runtime import head_check

    repo = Path(root) / "hchk"
    repo.mkdir(parents=True, exist_ok=True)
    _git(str(repo), "init", "-q")
    repo.joinpath("good.py").write_text("VALUE = 1\n", encoding="utf-8", newline="\n")
    repo.joinpath("entry.py").write_text("import good\nimport helper_missing\nTOP = good.VALUE\n",
                                         encoding="utf-8", newline="\n")
    repo.joinpath("helper_missing.py").write_text("SEEN = []\n", encoding="utf-8", newline="\n")
    _git(str(repo), "add", "good.py", "entry.py")
    _git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "引用了一个还没入库的文件")
    broken = head_check.check(str(repo), modules=("entry",), timeout=60)
    failure = (broken.get("failures") or [{}])[0]
    stranger = repo / "someone-elses-checkout"
    stranger.mkdir(exist_ok=True)
    refused = head_check.remove_worktree(str(repo), stranger)
    empty_list = head_check.check(str(repo), modules=(), timeout=60)
    _git(str(repo), "add", "helper_missing.py")
    _git(str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "把被引用的文件也提交掉")
    fixed = head_check.check(str(repo), modules=("entry",), timeout=60)
    leftovers = list(head_check.worktree_base(str(repo)).glob("*")) if \
        head_check.worktree_base(str(repo)).is_dir() else []
    return [_check("broken_head_is_red", not broken["ok"], broken.get("error", "")),
            _check("untracked_reference_is_named",
                   failure.get("missing") == "helper_missing"
                   and failure.get("in_commit") is False and failure.get("on_disk") is True,
                   failure),
            _check("importer_points_at_the_real_line",
                   failure.get("importer") == "entry.py" and failure.get("importer_line") == 2,
                   {"importer": failure.get("importer"), "line": failure.get("importer_line")}),
            _check("committing_the_file_turns_it_green", fixed["ok"], fixed.get("failures")),
            _check("nothing_attempted_is_not_green", not empty_list["ok"], empty_list["checks"]),
            _check("foreign_checkout_is_never_removed",
                   not refused["ok"] and stranger.exists(), refused),
            _check("no_checkout_left_behind", not leftovers, [str(p) for p in leftovers]),
            _check("option_like_ref_refused", not head_check.resolve_ref(str(repo), "--help")["ok"],
                   "把 git 选项当 ref 收了")]


def _case_voice_glue_would_catch_unconditional_pumping(root: str, run: Callable[..., dict]) -> list[dict]:
    """语音行为回路的护栏在【没有浏览器】时也必须可验：胶水调用与判定表。"""
    from agent_runtime import voice_loop

    page = voice_loop.voice_page_script("ws://127.0.0.1:9/live")
    missing = [name for name in voice_loop.GLUE_CALLS if name not in page]

    def rows(labels):
        out, at = [], 0.0
        for index, label in enumerate(labels):
            out.append({"kind": "audio", "label": label, "bytes": 3_200, "sequence": index + 1,
                        "rms": 0.28 if label == "T" else 0.0, "at": round(at, 3),
                        "gap": 0.1 if index else 0.0})
            at += 0.1
        return out

    pumped = rows(["T"] * 8 + ["."] * 40 + ["T"] * 8)     # 逐帧无条件推流的样子
    sent = [{"type": "hello.ok"}, {"type": "model.delta", "final": True}]
    control = [{"type": "hello"}, {"type": "cancel"}]
    state = {"mic": "on", "helloOk": True, "errors": [],
             "serverEvents": ["hello.ok", "model.delta"]}
    pumped_verdict = voice_loop.verdict(pumped, control, sent, state=state, violations=[])
    # 尾后真的停了 2.7 秒：间隔要落在【尾后第一片】上（gaps[after]），
    # 写在尾巴里（gaps[after-1]）量到的是 100ms，永远看不出流有没有停。
    resumed = rows(["."] + ["T"] * 7 + ["q"] * 3 + ["."] * 12 + ["T"] * 3)
    resumed[23]["gap"] = 2.7
    proper_verdict = voice_loop.verdict(resumed, control, sent, state=state, violations=[])
    # 截断在尾巴中间、又没有继续观察够：无门限页面也不许冒充"停下来了"
    truncated = rows(["T"] * 8 + ["."] * 15)[:20]
    truncated_verdict = voice_loop.verdict(truncated, control, sent, state=state, violations=[])
    try:
        voice_loop.voice_page_script("http://example.com:9/live")
        shape = "接受"
    except voice_loop.VoiceLoopError:
        shape = "拒绝"
    return [_check("glue_calls_production", not missing, "胶水缺生产调用：%s" % missing),
            _check("tail_rules_transcribed", "signal.state === 'tail'" in page
                   and "new Uint8Array(3200)" in page, "tail 零值替换或 audio-ready 标记没照抄"),
            _check("unconditional_pumping_fails", not pumped_verdict["passed"],
                   pumped_verdict["checks"]),
            _check("truncated_always_pumping_fails", not truncated_verdict["passed"],
                   truncated_verdict["checks"]),
            _check("a_faithful_shape_passes", proper_verdict["passed"], proper_verdict["checks"]),
            _check("non_loopback_ws_%s" % shape, shape == "拒绝", shape)]


def _case_bat_guard_refuses_an_escaped_closing_quote(root: str,
                                                    run: Callable[..., dict]) -> list[dict]:
    r"""批处理引号门：`--root "%~dp0"` 必须判红，正确写法与 cmd 自己解析的地方必须判绿。

    存在的理由是本仓 2026-10-02 那次启动失败：`%~dp0` 展开后以反斜杠结尾，紧跟其后的
    闭合引号被外部程序按 MSVCRT 规则当成「转义过的引号」，bootstrap 于是收到
    `D:\WorkBuddy\rag-agent"`（路径末尾多一个字面引号）。最后一组检查是【把前提量出来】
    而不是相信注释：连 Python 自己拼 argv 的 list2cmdline 都要在这种值前再补一个反斜杠，
    所以手写的那对引号一定是转义、不是闭合。
    """
    from agent_runtime import bat_guard

    def rules(text: str) -> list[str]:
        return [row["rule"] for row in bat_guard.find_issues(text)]

    bs, dq = chr(92), chr(34)
    bad_quoted = "py -3 boot.py --root " + dq + "%~dp0" + dq
    bad_unquoted = "py -3 boot.py --root %~dp0"
    good_dot = "py -3 boot.py --root " + dq + "%~dp0." + dq
    good_strip = ("set " + dq + "ROOT=%~dp0" + dq + "\npy x --root "
                  + dq + "%ROOT:~0,-1%" + dq)
    cmd_parsed = ("cd /d " + dq + "%~dp0" + dq + "\nif exist " + dq + "%~dp0a.bat" + dq
                  + " goto x\nset " + dq + "OTHER=%ERRORLEVEL%" + dq)
    inline_after_if = "if exist " + dq + "%DOCMIND_ENTRY%" + dq + " py x --root " + dq + "%~dp0" + dq
    one_hop = "set " + dq + "ROOT=%~dp0" + dq + "\npy x --root " + dq + "%ROOT%" + dq
    empty = Path(root) / "bg-nothing"
    empty.mkdir(parents=True, exist_ok=True)
    corpus = Path(root) / "bg-corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    launcher = corpus / "run.bat"
    launcher.write_bytes(("@echo off\r\npy x --root " + dq + "%~dp0" + dq
                          + "\r\n").encode("gbk"))
    clean_dir = Path(root) / "bg-clean"
    clean_dir.mkdir(parents=True, exist_ok=True)
    (clean_dir / "ok.bat").write_bytes(("cd /d " + dq + "%~dp0" + dq + "\r\n").encode("gbk"))
    report = bat_guard.audit(corpus)
    row = (report["findings"] or [{}])[0]
    nothing = bat_guard.audit(empty)
    all_clean = bat_guard.audit(clean_dir)
    dir_value = "D:" + bs + "my dir" + bs
    wire = subprocess.list2cmdline(["--root", dir_value])
    hand = "--root " + dq + dir_value + dq
    return [_check("the_shipped_bug_is_caught",
                   rules(bad_quoted) == [bat_guard.RULE_QUOTED], rules(bad_quoted)),
            _check("the_mirror_mistake_is_caught",
                   rules(bad_unquoted) == [bat_guard.RULE_UNQUOTED], rules(bad_unquoted)),
            _check("the_one_character_fix_is_clean", rules(good_dot) == [], rules(good_dot)),
            _check("the_substring_fix_is_clean", rules(good_strip) == [], rules(good_strip)),
            _check("cmd_parsed_places_are_not_arguments", rules(cmd_parsed) == [],
                   rules(cmd_parsed)),
            _check("an_inline_command_after_if_is_still_checked",
                   rules(inline_after_if) == [bat_guard.RULE_QUOTED], rules(inline_after_if)),
            _check("a_variable_holding_the_directory_is_the_same_bug",
                   rules(one_hop) == [bat_guard.RULE_QUOTED], rules(one_hop)),
            _check("a_real_file_reports_its_path_line_and_fix",
                   row.get("path") == str(launcher) and row.get("line") == 2
                   and "%~dp0." in str(row.get("fix", "")),
                   {k: row.get(k) for k in ("path", "line", "rule", "token")}),
            _check("nothing_scanned_is_never_reported_as_clean",
                   not nothing["ok"] and nothing["files_scanned"] == 0, nothing),
            _check("a_clean_corpus_is_green_and_counts_its_files",
                   all_clean["ok"] and all_clean["files_scanned"] == 1, all_clean),
            _check("a_correct_quoter_escapes_that_very_quote",
                   bs + dq in wire and hand != wire, {"wire": wire, "hand": hand})]


DEV_DATASET: tuple[dict[str, Any], ...] = (
    {
        "id": "lookup-by-filename", "fixture": "ts-refs",
        "steps": [_react("dev_glob", "*.tsx"), _final("只有一个 tsx")],
        "expect": {"called": ["dev_glob"],
                   "observation_contains": ["frontend/src/App.tsx"]},
        "note": "找文件不该靠 list_dir 一层层翻",
    },
    {
        "id": "precise-references-ignore-comment-and-string", "fixture": "ts-refs",
        "steps": [_react("dev_find_references", "handleResize"), _final("两处真引用")],
        "expect": {"called": ["dev_find_references"],
                   "observation_contains": ["App.tsx", "mount()"],
                   "observation_not_contains": ["只在这里被提到", "出现在字符串里"]},
        "note": "tree-sitter 层的价值：注释与字符串不误报",
    },
    {
        "id": "history-answers-who-and-what", "fixture": "git-history",
        "steps": [_react("dev_git_log", "paths: a.py"), _final("a.py 由 First Author 引入")],
        "expect": {"called": ["dev_git_log"],
                   "observation_contains": ["first: add a.py", "First Author"]},
        "note": "只读历史：判断是否重复劳动",
    },
    {
        "id": "diagnostics-find-real-errors", "fixture": "patch",
        "steps": [_react("dev_diagnostics", "target: bad.py\nscope: py"),
                  _final("语法有错")],
        "expect": {"called": ["dev_diagnostics"],
                   "observation_contains": ["bad.py", "E9"]},
        "note": "结构化诊断而不是「过/不过」",
    },
    {
        "id": "diagnostics-clean-file-is-clean", "fixture": "patch",
        "steps": [_react("dev_diagnostics", "target: ok.py\nscope: py"), _final("没问题")],
        "expect": {"called": ["dev_diagnostics"],
                   "observation_contains": ["没有发现问题"]},
        "note": "报「干净」的前提是真跑成了，不是没跑",
    },
    {
        "id": "patch-applies-atomically", "fixture": "patch",
        "steps": [_react("dev_patch", _CLEAN_PATCH), _final("改好了")],
        "expect": {"called": ["dev_patch"], "observation_contains": ["补丁已应用"],
                   "files": {"a.txt": "one\nTWO\nthree\n", "b.txt": _OTHER_TARGET}},
        "post": "patch",
        "note": "打对的地方要真的打对",
    },
    {
        "id": "patch-rejects-partial-and-writes-nothing", "fixture": "patch",
        "steps": [_react("dev_patch", _BAD_SECOND_PATCH), _final("被打回")],
        "expect": {"called": ["dev_patch"], "observation_contains": ["补丁被拒绝"],
                   "files": {"a.txt": _PATCH_TARGET, "b.txt": _OTHER_TARGET}},
        "note": "多文件补丁第二块不匹配时第一个文件必须原样——不留半成品（断效果而不是断措辞）",
    },
    {
        "id": "patch-refuses-delete-and-escape", "fixture": "patch",
        "steps": [_react("dev_patch", "--- a/a.txt\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-one\n"),
                  _final("不让删")],
        "expect": {"called": ["dev_patch"], "observation_contains": ["拒绝"],
                   "files": {"a.txt": _PATCH_TARGET}},
        "note": "删除不许借补丁发生",
    },
    {
        "id": "lane-claim-blocks-overlap", "fixture": "lanes",
        "steps": [_react("dev_lanes", "action: claim\nlane: AI-A\nowner: 甲\n"
                                      "paths: frontend/src/**"),
                  _react("dev_lanes", "action: claim\nlane: AI-B\nowner: 乙\n"
                                      "paths: frontend/src/workbench/live.ts"),
                  _final("冲突，找甲")],
        "expect": {"called": ["dev_lanes"],
                   "observation_contains": ["认领成功", "冲突", "AI-A", "甲"],
                   "files_absent": []},
        "note": "多人同仓的排他：冲突要当场说出对方是谁",
    },
    {
        "id": "lane-check-answers-ownership", "fixture": "lanes",
        "steps": [_react("dev_lanes", "action: claim\nlane: AI-A\nowner: 甲\n"
                                      "paths: agent_runtime/**"),
                  _react("dev_lanes", "action: check\npath: agent_runtime/realtime_omni.py"),
                  _final("这块是甲的")],
        "expect": {"called": ["dev_lanes"], "observation_contains": ["AI-A", "甲"]},
        "note": "动文件前先问归属",
    },
    {
        "id": "write-tool-blocked-by-command-gate", "fixture": "plain",
        "direct": _case_command_gate,
        "uses": ["run_command"],
        "note": "换个入口也不能绕过命令黑名单",
    },
    {
        "id": "read-sandbox-holds", "fixture": "plain",
        "direct": _case_read_file_escape,
        "uses": ["read_file"],
        "note": "越界读取必须被拒且不回显内容",
    },
    {
        "id": "mcp-server-publishes-read-only-first", "fixture": "plain",
        "direct": _case_mcp_server_read_only,
        "uses": ["mcp_server"],
        "note": "对外出口默认只读，写与执行要显式开",
    },
    {
        "id": "connector-injection-is-opt-in", "fixture": "plain",
        "direct": _case_mcp_inline_opt_in,
        "uses": ["mcp_bridge"],
        "note": "没打标记的连接器连握手都不该发生",
    },
    {
        "id": "proposal-artifacts-stay-outside-the-repo", "fixture": "proposal",
        "direct": _case_propose_keeps_the_repo_clean,
        "uses": ["dev_propose", "dev_git_diff"],
        "note": "交付产物不能把仓库弄脏——多人同仓时这是事故",
    },
    {
        "id": "ci-status-never-invents-a-green", "fixture": "plain",
        "direct": _case_ci_never_invents_status,
        "uses": ["dev_ci_status"],
        "note": "读不到 CI 时必须明说，不能编一个通过状态出来",
    },
    {
        "id": "media-fixture-closes-without-a-device", "fixture": "plain",
        "direct": _case_media_fixture_closes_without_a_device,
        "uses": ["dev_media"],
        "note": "语音回路不再依赖真麦克风：夹具、包形、静音尾都算得出来",
    },
    {
        "id": "page-action-refuses-and-never-claims-success", "fixture": "plain",
        "direct": _case_page_action_refuses_and_never_claims_success,
        "uses": ["dev_page_action"],
        "note": "点了没生效绝不能报成功：歧义/隐藏/禁用不派发，报错优先于 DOM 变化",
    },
    {
        "id": "vue-guard-can-go-red", "fixture": "plain",
        "direct": _case_vue_guard_can_go_red,
        "uses": ["dev_vue"],
        "note": "组件守卫的判定表必须每条能红：只挂不断言、emit 名字没声明、读状态失败、"
                "要求的 setup 调用没跑成都要红；读状态不能靠点击",
    },
    {
        "id": "apptest-refuses-a-stale-build-as-current", "fixture": "plain",
        "direct": _case_apptest_refuses_a_stale_page_as_current,
        "uses": ["dev_apptest"],
        "note": "整页守卫最容易的三种假绿：旧构建当现在的代码、只信注册接口的 200、"
                "点了导航却没换视图——每条都必须判红；页面与后端地址一律锁在回环与站内路径上",
    },
    {
        "id": "testrun-rerereads-failures-not-just-an-exit-code", "fixture": "plain",
        "direct": _case_testrun_reads_failures_and_reruns_only_them,
        "uses": ["dev_testrun"],
        "note": "0 收集不是通过；失败要 named+带原因+可只重跑；计数与退出码不自洽就判不可信",
    },
    {
        "id": "headcheck-catches-untracked-reference", "fixture": "plain",
        "direct": _case_headcheck_catches_an_untracked_reference,
        "uses": ["dev_headcheck"],
        "note": "提交引用了没入库的文件时，只有干净检出能发现；工作树里跑永远绿",
    },
    {
        "id": "voice-glue-catches-unconditional-pumping", "fixture": "plain",
        "direct": _case_voice_glue_would_catch_unconditional_pumping,
        "uses": ["dev_media"],
        "note": "语音接线行为回路：逐帧无条件推流必须判未通过，不能只会报绿",
    },
    {
        "id": "bat-guard-refuses-an-escaped-closing-quote", "fixture": "plain",
        "direct": _case_bat_guard_refuses_an_escaped_closing_quote,
        "note": "启动器引号门只有一种失效方式：把 cmd 自己解析的地方也判红，那就没人会留着它。"
                "红的是「展开后以反斜杠结尾的引号实参」与「裸的目录展开」；绿的是 "
                "%~dp0. 与 %ROOT:~0,-1% 两种正解，外加 cd / if 条件 / set 这三处",
    },
)


# --------------------------------------------------------------------------- #
# 执行与判定
# --------------------------------------------------------------------------- #

def _read_files(root: str, names: list[str]) -> dict[str, str]:
    out = {}
    for rel in names:
        full = os.path.join(root, *rel.split("/"))
        try:
            out[rel] = Path(full).read_text(encoding="utf-8")
        except OSError:
            out[rel] = ""
    return out


def evaluate_case(case: Mapping[str, Any]) -> dict[str, Any]:
    cid = str(case.get("id") or "?")
    report: dict[str, Any] = {"id": cid, "checks": [], "skipped": "", "passed": False}
    root = tempfile.mkdtemp(prefix="docmind_deveval_")
    state = tempfile.mkdtemp(prefix="docmind_deveval_state_")
    # finally 里已经会删这两个目录；标归属是为了被硬杀时下一个进程能扫掉（实测留了 121 个）。
    temp_state.claim(root)
    temp_state.claim(state)
    import tools as tools_module
    prev_root = tools_module.get_runtime("code_root")
    prev_state = config.STATE_ROOT
    try:
        reason = _build_fixture(str(case.get("fixture") or "plain"), root)
        if reason:
            report["skipped"] = reason
            report["passed"] = True
            return report
        config.STATE_ROOT = state
        tools_module.set_runtime("code_root", root)
        expect = dict(case.get("expect") or {})

        def run(tool: str, arg: str) -> dict[str, Any]:
            meta = tools_module.TOOLS.get(tool) or {}
            func = meta.get("func")
            if not callable(func):
                return {"text": "__TOOL_MISSING__:%s" % tool}
            try:
                out = func(arg)
            except Exception as exc:  # noqa: BLE001
                return {"text": "__TOOL_RAISED__:%s" % exc}
            return {"text": out if isinstance(out, str) else str(getattr(out, "text", out))}

        direct = case.get("direct")
        if direct is not None:
            report["checks"] = list(direct(root, run))
        else:
            from agent import Agent
            steps = [_render(row) for row in (case.get("steps") or [])]
            actions: list[str] = []
            observations: list[str] = []
            agent = Agent(llm=_ScriptedLLM(steps), session_id=None)
            for event in agent.run(str(case.get("question") or "按步骤执行。"), stream=True):
                kind = event.get("type")
                if kind == "action":
                    actions.append(str(event.get("text") or event.get("tool") or ""))
                elif kind == "observation":
                    observations.append(str(event.get("text") or ""))
            blob = "\n".join(observations)
            for tool in expect.get("called") or []:
                report["checks"].append(_check(
                    "called:%s" % tool, any(tool in line for line in actions),
                    " / ".join(actions[:3])))
            for needle in expect.get("observation_contains") or []:
                report["checks"].append(_check("contains:%s" % str(needle)[:40],
                                               str(needle) in blob, blob[:200]))
            for needle in expect.get("observation_not_contains") or []:
                report["checks"].append(_check("not_contains:%s" % str(needle)[:40],
                                               str(needle) not in blob, blob[:200]))
        for rel, want in (expect.get("files") or {}).items():
            got = _read_files(root, [rel])[rel]
            report["checks"].append(_check("file:%s" % rel, got == want,
                                           "实际 %r" % got[:160]))
        report["passed"] = all(item["ok"] for item in report["checks"]) and bool(report["checks"])
        if not report["checks"]:
            report["checks"] = [_check("has_checks", False, "这题没产生任何断言")]
        return report
    except Exception as exc:  # noqa: BLE001 - 单题异常不能拖垮整轮
        report["checks"] = [_check("case_error", False, "%s: %s" % (type(exc).__name__, exc))]
        report["passed"] = False
        return report
    finally:
        tools_module.set_runtime("code_root", prev_root)
        config.STATE_ROOT = prev_state
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)


def validate_cases(cases: list[dict[str, Any]], tools_module: Any) -> list[str]:
    """题集自身的体检：工具被删了、题号重了、没有断言——这些都会让门禁变成装饰。"""
    problems: list[str] = []
    seen: set[str] = set()
    for case in cases:
        cid = str(case.get("id") or "")
        if not cid:
            problems.append("有题目缺少 id")
            continue
        if cid in seen:
            problems.append("%s：id 重复" % cid)
        seen.add(cid)
        used = [str(row.get("tool") or "") for row in case.get("steps") or [] if "tool" in row]
        used += [str(row.get("tool") or "") for row in case.get("verify") or []]
        if not (case.get("expect") or case.get("direct")):
            problems.append("%s：既没有 expect 也没有 direct" % cid)
        if not used and not case.get("direct"):
            problems.append("%s：既没调用工具也没有 direct 断言" % cid)
        for tool in used:
            if tool not in getattr(tools_module, "TOOLS", {}):
                problems.append("%s：用了不存在的工具 %s" % (cid, tool))
    return problems


def evaluate(dataset: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    import tools as tools_module
    cases = [dict(case) for case in (dataset or DEV_DATASET)]
    results = [evaluate_case(case) for case in cases]
    problems = validate_cases(cases, tools_module)
    ran = [row for row in results if not row["skipped"]]
    passed = [row for row in ran if row["passed"]]
    return {"ok": bool(ran) and not problems and len(passed) == len(ran),
            "problems": problems,
            "total": len(results), "scored": len(ran),
            "passed": len(passed), "failed": [row["id"] for row in ran if not row["passed"]],
            "skipped": [row["id"] for row in results if row["skipped"]],
            "results": results}


def compare_to_baseline(report: Mapping[str, Any], baseline_path: str | Path) -> dict[str, Any]:
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    before = {str(row.get("id")): row for row in baseline.get("results") or []}
    regressions = [row["id"] for row in report["results"]
                   if not row["skipped"] and before.get(row["id"], {}).get("passed")
                   and not row["passed"]]
    improvements = [row["id"] for row in report["results"]
                    if not row["skipped"] and not before.get(row["id"], {}).get("passed")
                    and row["passed"]]
    new_cases = [row["id"] for row in report["results"] if row["id"] not in before]
    score = int(baseline.get("passed") or 0) / max(1, int(baseline.get("scored") or 1))
    now = report["passed"] / max(1, report["scored"])
    return {"ok": not regressions and not report.get("failed"),
            "regressions": regressions, "improvements": improvements, "new_cases": new_cases,
            "baseline_pass_rate": round(score, 4), "current_pass_rate": round(now, 4),
            "regressed": bool(regressions)}


def write_baseline(report: Mapping[str, Any], path: str | Path) -> None:
    payload = {"schema_version": 1, "total": report["total"], "scored": report["scored"],
               "passed": report["passed"], "skipped": report["skipped"],
               "results": [{"id": row["id"], "passed": row["passed"],
                            "skipped": row["skipped"],
                            "failed_checks": [item["name"] for item in row["checks"]
                                             if not item["ok"]]}
                           for row in report["results"]]}
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="dev lane 能力评测门")
    parser.add_argument("--self-check", action="store_true", help="跑内置题集（默认就是）")
    parser.add_argument("--baseline", default="", help="与基线比对，出现回退则退出码非零")
    parser.add_argument("--write-baseline", default="", help="把本次结果写成基线文件")
    parser.add_argument("--json", action="store_true", help="机器可读输出（CI 用）")
    args = parser.parse_args(argv)

    report = evaluate()
    gate = compare_to_baseline(report, args.baseline) if args.baseline else None
    if args.write_baseline:
        write_baseline(report, args.write_baseline)
    if args.json:
        print(json.dumps({"report": report, "gate": gate}, ensure_ascii=False))
    else:
        print("dev-eval：%d/%d 通过（共 %d 题，跳过 %d）"
              % (report["passed"], report["scored"], report["total"], len(report["skipped"])))
        for row in report["results"]:
            if row["passed"] or row["skipped"]:
                continue
            bad = [item for item in row["checks"] if not item["ok"]]
            print("  失败 %s：%s" % (row["id"], "; ".join(
                "%s→%s" % (item["name"], item["detail"][:90]) for item in bad[:3])))
        if gate:
            print("  基线 %s → 现在" % gate["baseline_pass_rate"], gate["current_pass_rate"],
                  "| 回退：", gate["regressions"] or "无")
    ok = report["ok"] and not (gate and gate["regressed"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
