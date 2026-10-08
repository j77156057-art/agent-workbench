"""真跑验证 `b710f2d`：deepseek-flash 开思考后**真的回 reasoning_content 吗**。

## 只验这一件事

不插 key 的部分已全部核实完（参数形状合文档 / tool_choice 不发 /
`reasoning_content` 映射存在 / 前端流式自动展开已做）。剩下只有真实请求能回答。

## 成本

两次请求，`max_tokens=256`。用户明知代价，明确同意了。

## ⚠ 三条判据纪律（这条探针自己最容易犯的错）

1. **请求失败 ≠ 模型不回 reasoning。** 401/403/超时/限流都要单独报，
   **绝不允许推导出「这个模型不支持思考」**——那会和原 bug 同一个方向地误导。
2. **两个方向都要测。** 只测「开着能回」不够：还得测「关着不回」。
   若两个方向都回 reasoning，说明参数根本没生效，那才是真失败。
3. **不打印 key。** 只打印 key 是否存在的布尔值。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ensure_on_path()

MODEL = os.getenv("DOCMIND_E2E_MODEL", "deepseek-flash")
MAX_TOKENS = int(os.getenv("DOCMIND_PROBE_MAX_TOKENS", "256"))
QUESTION = os.getenv(
    "DOCMIND_PROBE_Q",
    "1+1等于几？只回答数字。",
)


def key_state() -> tuple[bool, str, str]:
    """返回 (有没有 key, 来源说明, key 本身) 。**只在内存里用，不打印 key。**。

    ⚠⚠ 顺序必须是**应用启动时那段**（`api.py:_restore_persisted_llm`），
    不能自己去猜环境变量。文档原文：
        「模型选择保存在 STATE_FILE，API key 只从当前项目的 secrets_store 读取，
          绝不写入状态文件或接口响应。」

    第一版这条探针去查 `DASHSCOPE_API_KEY`（那是 omni 实时语音用的），
    **把「找到一个不相关的 key」当成「有 key」**，于是发了个空 key 上去拿 401。
    """
    import config
    if (config.get_runtime("llm_api_key") or "").strip():
        return True, "config.set_runtime('llm_api_key')", config.get_runtime("llm_api_key")
    # 与 _restore_persisted_llm 同路：从项目 secrets_store 读
    try:
        import secrets_store
        root = (config.get_runtime("code_root") or "").strip()
        if not root:
            return False, "", ""
        k = secrets_store.load(root, "deepseek")
        if k:
            config.set_runtime("llm_api_key", k)
            return True, "secrets_store.load(project_root, 'deepseek')", k
    except Exception as exc:  # noqa: BLE001
        return False, "读 secrets_store 失败: %s" % type(exc).__name__, ""
    return False, "", ""


def call(thinking_on: bool, stream: bool) -> dict:
    """发一次真实请求，返回结构化结果。异常不吞。"""
    from llm import LLMClient

    c = LLMClient(provider="deepseek", model=MODEL)
    reasoning: list[str] = []
    content: list[str] = []
    err = ""

    try:
        # 走与生产一致的路径：enable_thinking -> extra_body
        if stream:
            gen = c.chat(
                [{"role": "user", "content": QUESTION}],
                stream=True,
                enable_thinking=thinking_on,
                reasoning_sink=reasoning,
            )
            for tok in gen:
                content.append(tok)
        else:
            out = c.chat(
                [{"role": "user", "content": QUESTION}],
                stream=False,
                enable_thinking=thinking_on,
                reasoning_sink=reasoning,
            )
            content.append(str(out or ""))
    except Exception as exc:  # noqa: BLE001
        err = "%s: %s" % (type(exc).__name__, str(exc)[:160])

    return {
        "thinking_on": thinking_on,
        "stream": stream,
        "ok": not err,
        "error": err,
        "reasoning_chars": sum(len(x) for x in reasoning),
        "reasoning_head": "".join(reasoning)[:150],
        "content_chars": sum(len(x) for x in content),
        "content_head": "".join(content)[:150],
    }


def main() -> int:
    print("=" * 74)
    print("  真跑验证：deepseek-flash 开思考是否真的回 reasoning_content")
    print("=" * 74)
    print("  模型      : %s" % MODEL)
    print("  max_tokens: %d" % MAX_TOKENS)
    print("  题目      : %s" % QUESTION)

    import config
    # 允许从外面指定项目根（探针是新进程，没有应用启动时那份运行时）
    _croot = (os.getenv("DOCMIND_PROBE_CODE_ROOT") or "").strip()
    if _croot:
        config.set_runtime("code_root", _croot)
        print("  code_root : %r（来自 DOCMIND_PROBE_CODE_ROOT）" % _croot)

    has_key, src, _k = key_state()
    print("  API key   : %s" % ("有（来自 %s）" % src if has_key else "*** 未配置 ***"))
    print("  code_root : %r" % config.get_runtime("code_root"))
    print()

    if not has_key:
        print("  *** 没有 API key —— **本次不产出任何结论**。")
        print("     「没跑成」不等于「模型不回 reasoning」，这两件事不能混。")
        print()
        print("     ⚠ key 的查找路径已按 `api.py:_restore_persisted_llm` 走：")
        print("       运行时 llm_api_key -> 项目 secrets_store(code_root, provider)。")
        print("       **它不在 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 环境变量里。**")
        print("     code_root 为空时设 DOCMIND_PROBE_CODE_ROOT=<项目绝对路径> 再跑。")
        return 2

    from llm import LLMClient
    probe = LLMClient(provider="deepseek", model=MODEL)
    cap = getattr(probe, "capability", {}) or {}
    print("  能力画像  : thinking=%r（应为 'toggle'）" % cap.get("thinking"))
    if cap.get("thinking") != "toggle":
        print("  *** 画像不是 toggle —— 生产根本不会发 thinking 参数，")
        print("      这一次跑即使成功也证明不了用户那条路径。先修画像。")
        return 3
    print()

    print("  === 请求①：思考开（enable_thinking=True，流式）===")
    on = call(True, stream=True)
    print("      成功=%s  reasoning=%d 字  content=%d 字"
          % (on["ok"], on["reasoning_chars"], on["content_chars"]))
    if not on["ok"]:
        print("      错误: %s" % on["error"])
    elif on["reasoning_head"]:
        print("      reasoning 开头: %s" % on["reasoning_head"][:100])

    print()
    print("  === 请求②：思考关（enable_thinking=False，流式）===")
    off = call(False, stream=True)
    print("      成功=%s  reasoning=%d 字  content=%d 字"
          % (off["ok"], off["reasoning_chars"], off["content_chars"]))
    if not off["ok"]:
        print("      错误: %s" % off["error"])

    print()
    print("=" * 74)
    print("  判定")
    print("=" * 74)

    if not on["ok"] or not off["ok"]:
        failed = [k for k, v in (("①开", on), ("②关", off)) if not v["ok"]]
        print("  ⚠ **至少一次请求失败**（%s）—— 无法判定。" % "、".join(failed))
        print("     失败可能是鉴权/网络/限流，**不能**据此说模型支不支持思考。")
        return 4

    if on["reasoning_chars"] > 0 and off["reasoning_chars"] == 0:
        print("  ✅ **验证通过。**")
        print("     开 -> reasoning %d 字；关 -> reasoning %d 字。" %
              (on["reasoning_chars"], off["reasoning_chars"]))
        print("     即：`thinking.type` 参数**真实生效**，"
              "且 `llm.py` 的 reasoning_sink 真的收到了内容。")
        print("     => `b710f2d` 从「文档上正确」升级为「真跑验证过」。")
        return 0

    if on["reasoning_chars"] > 0 and off["reasoning_chars"] > 0:
        print("  ❌ **参数没生效。**")
        print("     开和关都回了 reasoning（%d / %d 字）—— 说明 "
              "`thinking.type` 被上游忽略了。" %
              (on["reasoning_chars"], off["reasoning_chars"]))
        print("     那 `b710f2d` 仍然只是「文档正确」，开关会是能点但没效果。")
        return 5

    if on["reasoning_chars"] == 0 and off["reasoning_chars"] == 0:
        print("  ⚠ **两次都没有 reasoning。** 两种可能，本探针分不开：")
        print("       (a) 这道题太简单，模型没产生思考（`%s`）" % QUESTION)
        print("       (b) 参数没生效 / 模型不回 reasoning_content")
        print("     换一道需要推理的题（设 DOCMIND_PROBE_Q）再跑一次。")
        return 6

    # on == 0, off > 0 —— 反常
    print("  ❌ **反常：关着反而回了 reasoning（%d 字），开着没有（%d 字）。**"
          % (off["reasoning_chars"], on["reasoning_chars"]))
    print("     方向反了，说明参数语义和我们想的不一致。")
    return 7


if __name__ == "__main__":
    sys.exit(main())
