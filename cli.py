# -*- coding: utf-8 -*-
"""Agent 工作台 运维/联调命令行（最小集：doctor / chat / bridge）。

定位：**只做 API 客户端 + 预检**，不复制任何业务逻辑。
- doctor : 服务存活、模型配置、开关状态、code_root 一致性、待确认队列
- chat   : 通过 /api/chat 走一轮完整 SSE（UTF-8/中文安全）
- bridge : 查看 / 确认 / 拒绝 PTC 待确认写

硬约束：
1. bridge 子命令一律走 HTTP。待确认队列在**服务进程内存**里，另起进程
   import tools 去确认只会看到空队列。
2. 本文件只依赖标准库，不 import 项目模块（避免 CLI 启动时拖起整棵依赖树）。

用法示例::

    python cli.py doctor
    python cli.py --port 8013 doctor
    python cli.py chat "帮我看看这段代码" --session dev --project prj-xxx
    python cli.py chat --file question.txt
    python cli.py bridge list --session dev
    python cli.py bridge confirm W2 --session dev
    python cli.py bridge reject W2 --session dev
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

DEFAULT_PORT = 8000
DEFAULT_TIMEOUT = 10.0
CHAT_TIMEOUT = 600.0


class CliError(Exception):
    """可预期的操作失败：由 main() 统一打印成一行中文错误。"""


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------

def _tune_stdio() -> None:
    """管道/GBK 控制台下遇到不可编码字符时用 ? 代替，绝不因打印崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docmind",
        description="Agent 工作台 运维/联调命令行（doctor / chat / bridge）。",
    )
    parser.add_argument(
        "--base",
        default="",
        help="服务地址，如 http://127.0.0.1:8013（默认读 DOCMIND_API_BASE，否则 127.0.0.1:DOCMIND_PORT/8000）",
    )
    parser.add_argument("--port", type=int, default=None, help="只给端口，等价于 --base http://127.0.0.1:<port>")
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="请求超时秒数（doctor/bridge 默认 10，chat 默认 600）",
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="命令")

    # doctor -----------------------------------------------------------------
    p_doc = sub.add_parser("doctor", help="检查服务、模型、开关、code_root 与待确认队列")
    p_doc.add_argument("--session", default="", help="只统计该会话的待确认写（默认全部）")
    p_doc.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    # chat -------------------------------------------------------------------
    p_chat = sub.add_parser("chat", help="发一轮对话并流式打印结果")
    p_chat.add_argument("question", nargs="*", help='问题文本；传 "-" 表示从 stdin 读')
    p_chat.add_argument("--file", default="", help="从 UTF-8 文件读问题")
    p_chat.add_argument("--session", default="default", help="会话 id（默认 default）")
    p_chat.add_argument("--project", default="", help="项目 id，作为 X-DocMind-Project 头发送")
    p_chat.add_argument("--thoughts", action="store_true", help="显示 thought 事件")
    p_chat.add_argument("--verbose", action="store_true", help="显示 turn/route/context 等事件")
    p_chat.add_argument("--full", action="store_true", help="action/observation 不截断")
    p_chat.add_argument("--json", action="store_true", help="每行原样输出一个 SSE JSON 对象")
    p_chat.add_argument("--timeout", type=float, default=argparse.SUPPRESS, help="本轮 SSE 超时秒数（默认 600）")

    # bridge -----------------------------------------------------------------
    p_bridge = sub.add_parser("bridge", help="查看/确认/拒绝 PTC 待确认写")
    b_sub = p_bridge.add_subparsers(dest="bridge_command", required=True, metavar="动作")

    p_list = b_sub.add_parser("list", help="列出待确认写")
    p_list.add_argument("--session", default="", help="只列该会话（默认全部）")
    p_list.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_list.add_argument("--timeout", type=float, default=argparse.SUPPRESS)

    p_confirm = b_sub.add_parser("confirm", help="确认一条待确认写（达到阈值才落盘）")
    p_confirm.add_argument("id", help="待确认写 id，如 W2")
    p_confirm.add_argument("--session", default="default", help="会话 id（默认 default）")
    p_confirm.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_confirm.add_argument("--timeout", type=float, default=argparse.SUPPRESS)

    p_reject = b_sub.add_parser("reject", help="拒绝并丢弃一条待确认写")
    p_reject.add_argument("id", help="待确认写 id，如 W2")
    p_reject.add_argument("--session", default="default", help="会话 id（默认 default）")
    p_reject.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_reject.add_argument("--timeout", type=float, default=argparse.SUPPRESS)

    return parser


def _resolve_base(args: argparse.Namespace) -> str:
    env_base = (os.getenv("DOCMIND_API_BASE") or "").strip()
    if args.base:
        base = args.base.strip()
    elif args.port is not None:
        base = "http://127.0.0.1:%d" % int(args.port)
    elif env_base:
        base = env_base
    else:
        raw = (os.getenv("DOCMIND_PORT") or str(DEFAULT_PORT)).strip()
        try:
            port = int(raw)
        except ValueError as exc:
            raise ValueError("DOCMIND_PORT 不是整数：%r" % raw) from exc
        base = "http://127.0.0.1:%d" % port
    if "://" not in base:
        base = "http://" + base
    return base.rstrip("/")


def _request_json(base, path, *, params=None, payload=None, timeout=DEFAULT_TIMEOUT):
    """GET/POST JSON。失败统一抛 CliError（带服务端返回摘要）。"""
    url = base + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001 - 读错误体失败不该掩盖原始错误
            pass
        raise CliError("%s 返回 HTTP %s：%s" % (path, exc.code, detail or exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise CliError("连不上 %s（%s）。服务起了吗？端口/--base 对吗？" % (base, exc.reason)) from exc
    except OSError as exc:
        raise CliError("请求 %s 失败：%s" % (path, exc)) from exc

    try:
        return json.loads(body)
    except ValueError as exc:
        raise CliError("%s 返回的不是 JSON：%s" % (path, body[:200])) from exc


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _cmd_doctor(base, args) -> int:
    timeout = args.timeout or DEFAULT_TIMEOUT
    report = {
        "base": base,
        "ok": False,
        "health": None,
        "config": None,
        "bridge": None,
        "code_root": None,
        "errors": [],
    }
    checks = []  # (level, text)
    hard_fail = False

    def _try(path, params=None):
        try:
            return _request_json(base, path, params=params, timeout=timeout)
        except CliError as exc:
            report["errors"].append("%s: %s" % (path, exc))
            return None

    health = _try("/api/health")
    report["health"] = health
    if not (isinstance(health, dict) and health.get("ok")):
        checks.append(("FAIL", "服务存活: 无法访问 /api/health"))
        hard_fail = True
        return _finish_doctor(base, args, report, checks, hard_fail)

    checks.append(("OK", "服务存活: %s @ %s" % (health.get("service") or "docmind", base)))

    cfg = _try("/api/config")
    report["config"] = cfg
    if isinstance(cfg, dict):
        prov = cfg.get("llm_provider") or "?"
        model = cfg.get("llm_model") or "?"
        has_key = bool(cfg.get("has_key"))
        checks.append(("OK" if has_key else "FAIL",
                       "模型: %s / %s（key %s）" % (prov, model, "已配置" if has_key else "缺失")))
        if not has_key:
            hard_fail = True

        cap = cfg.get("capability") or {}
        win = cap.get("context_window") or 0
        try:
            win = int(win)
        except (TypeError, ValueError):
            win = 0
        if win > 0:
            est = int(win * 0.33)  # 与 config.OUTPUT_TOKEN_FRACTION 的默认 0.33 一致
            msg = "上下文窗口: %d；输出预算估算 ≈ %d（窗口×0.33）" % (win, est)
            if str(prov).lower().startswith(("qwen", "dashscope")) and est > 32768:
                checks.append(("WARN", msg + "；qwen 云端单次输出上限约 32768，"
                                            "建议启动前设 DOCMIND_OUTPUT_MAX_TOKENS=30000"))
            else:
                checks.append(("OK", msg))

        ec = cfg.get("edit_confirm")
        checks.append(("OK" if ec else "WARN", "edit_confirm: %s" % ("开启" if ec else "关闭")))

        mode = cfg.get("llm_mode") or "fixed"
        if mode != "fixed":
            checks.append(("OK", "模型模式: %s" % mode))

        for warning in (cfg.get("warnings") or []):
            checks.append(("WARN", "配置告警: %s" % warning))
    else:
        checks.append(("WARN", "模型配置: /api/config 不可用，跳过"))

    bridge = _try("/api/bridge_writes", params={"session": args.session})
    report["bridge"] = bridge
    if isinstance(bridge, dict):
        enabled = bool(bridge.get("write_bridge"))
        pending = bridge.get("pending") or []
        suffix = "（会话 %s）" % args.session if args.session else ""
        checks.append(("OK" if enabled else "WARN",
                       "写桥接(服务报告): %s；待确认写 %d 条%s"
                       % ("开启" if enabled else "关闭", len(pending), suffix)))
        for item in pending[:5]:
            checks.append(("INFO", "  #%s tier=%s need=%s/%s %s %s"
                           % (item.get("id"), item.get("tier"), item.get("confirms"),
                              item.get("need"), item.get("tool"), item.get("path") or "")))
        if len(pending) > 5:
            checks.append(("INFO", "  …另有 %d 条，用 bridge list 查看" % (len(pending) - 5)))
    else:
        checks.append(("WARN", "写桥接: /api/bridge_writes 不可用，跳过"))

    code_root = _try("/api/code_root_status")
    report["code_root"] = code_root
    if isinstance(code_root, dict):
        effective = str(code_root.get("effective") or "").strip()
        source = code_root.get("source") or "?"
        if not effective:
            checks.append(("WARN", "code_root 为空（来源 %s）：PTC 子进程的路径档位可能被估成 tier3" % source))
        elif code_root.get("mismatch_top_vs_project"):
            checks.append(("WARN", "code_root: %s（来源 %s）；顶层与当前项目不一致，检索可能跑在旧目录"
                           % (effective, source)))
        else:
            checks.append(("OK", "code_root: %s（来源 %s）" % (effective, source)))
    else:
        checks.append(("WARN", "code_root: /api/code_root_status 不可用，跳过"))

    return _finish_doctor(base, args, report, checks, hard_fail)


def _finish_doctor(base, args, report, checks, hard_fail) -> int:
    report["ok"] = not hard_fail
    report["checks"] = [{"level": lvl, "text": text} for lvl, text in checks]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if hard_fail else 0

    print("Agent Workbench doctor @ %s" % base)
    for level, text in checks:
        print(("[%s]" % level).ljust(8) + text)
    warnings = sum(1 for level, _ in checks if level == "WARN")
    if hard_fail:
        print("\n结论：有硬性失败，先修 [FAIL] 项。")
    else:
        print("\n结论：基本可用%s。" % ("（有 %d 条告警，见上）" % warnings if warnings else ""))
    return 1 if hard_fail else 0


# ---------------------------------------------------------------------------
# chat（SSE）
# ---------------------------------------------------------------------------

def _encode_multipart(fields) -> tuple:
    boundary = "----DocMindCLI" + uuid.uuid4().hex
    chunks = []
    for name, value in fields.items():
        chunks.append("--%s\r\n" % boundary)
        chunks.append('Content-Disposition: form-data; name="%s"\r\n' % name)
        chunks.append("Content-Type: text/plain; charset=utf-8\r\n\r\n")
        chunks.append(str(value))
        chunks.append("\r\n")
    chunks.append("--%s--\r\n" % boundary)
    return "".join(chunks).encode("utf-8"), "multipart/form-data; boundary=%s" % boundary


def _read_question(args) -> str:
    parts = []
    if args.file:
        try:
            parts.append(Path(args.file).read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise CliError("读不了 --file %s：%s" % (args.file, exc)) from exc

    positional = list(args.question or [])
    if positional == ["-"]:
        data = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
        parts.append(data)
    elif positional:
        parts.append(" ".join(positional))

    if not parts:
        if sys.stdin.isatty():
            return ""
        parts.append(sys.stdin.buffer.read().decode("utf-8-sig", "replace"))

    return "\n".join(part.strip() for part in parts if part.strip()).strip()


def _clip(text, limit, full) -> str:
    text = str(text or "")
    if full or len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断 %d 字符，--full 看全文）" % (len(text) - limit)


def _block(label, text, limit, full) -> None:
    if not text:
        return
    sys.stdout.write("\n%s %s\n" % (label, _clip(text, limit, full).rstrip()))
    sys.stdout.flush()


def _render_event(ev, streamed, args, verbose) -> None:
    etype = ev.get("type")
    if etype == "token":
        text = ev.get("text") or ""
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
            streamed.append(text)
    elif etype == "final":
        _flush_final(ev, streamed)
    elif etype == "thought":
        if args.thoughts:
            _block("[thought]", ev.get("text") or "", 300, args.full)
    elif etype == "action":
        _block("[action]", ev.get("text") or "", 500, args.full)
    elif etype == "observation":
        _block("[observation]", ev.get("text") or "", 2000, args.full)
    elif etype == "turn":
        if verbose:
            print("[turn] session=%s id=%s" % (ev.get("session_id"), ev.get("turn_id")))
    elif etype == "route":
        if verbose:
            print("[route] %s (complexity=%s)" % (ev.get("route"), ev.get("complexity")))
    elif etype == "context":
        if verbose:
            print("[context] %s/%s (%s%%)" % (ev.get("used_tokens"), ev.get("context_window"), ev.get("percent")))
    elif etype == "done":
        pass
    elif verbose:
        print("[%s] %s" % (etype, json.dumps(ev, ensure_ascii=False)[:300]))


def _final_tail(text, joined):
    """final 相对已流式内容的尾巴；对不上返回 None（调用方补打全文）。

    首 token 常带前导空格、final 却已 trim —— 只按逐字节前缀比会把这种情况
    误判成「完全对不上」，于是整段答案被重复打印并带上 `[final]`。故再退一步：
    两侧去掉首尾空白后若仍构成前缀，就按 trim 后的内容取尾巴。宁可边界空白
    略有出入，也不能把正常输出变成双份。
    """
    prefix = joined.rstrip()
    if text.startswith(prefix):
        return text[len(prefix):]
    core, trimmed = text.strip(), prefix.strip()
    if core.startswith(trimmed):
        return core[len(trimmed):]
    return None


def _flush_final(ev, streamed) -> None:
    """token 已流式打印过就不再整段重放 final，避免答案出现两遍。"""
    text = str(ev.get("text") or "")
    joined = "".join(streamed)
    status = ev.get("status") or "ok"

    if text:
        if status == "error":
            # 错误终态必须完整可见：宁可和已流的半截重复，也绝不吞掉失败信息
            sys.stdout.write(("\n" if joined else "") + text)
        elif not joined:
            sys.stdout.write(text)
        else:
            tail = _final_tail(text, joined)
            if tail is None:
                # token 与 final 完全对不上：补打 final 全文，宁可重复也不吞信息
                sys.stdout.write("\n[final] " + text)
            elif tail.strip():
                # final 比已流式内容多出尾巴（如补充结论）：只补尾巴，绝不吞
                sys.stdout.write(tail if tail.startswith("\n") else "\n" + tail)

    if not text or not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def _cmd_chat(base, args) -> int:
    question = _read_question(args)
    if not question:
        raise CliError('问题为空：给一个参数、用 --file，或从 stdin 管道传入（也可传 "-"）。')

    body, content_type = _encode_multipart({
        "question": question,
        "session_id": args.session or "default",
    })
    headers = {"Content-Type": content_type, "Accept": "text/event-stream"}
    if args.project:
        headers["X-DocMind-Project"] = args.project

    req = urllib.request.Request(base + "/api/chat", data=body, headers=headers, method="POST")
    timeout = args.timeout or CHAT_TIMEOUT
    streamed = []
    final_status = None

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    ev = json.loads(payload)
                except ValueError:
                    if args.verbose and not args.json:
                        print("[raw] %s" % payload)
                    continue

                if args.json:
                    print(payload, flush=True)
                else:
                    _render_event(ev, streamed, args, args.verbose)

                if ev.get("type") == "final":
                    final_status = ev.get("status") or "ok"
                elif ev.get("type") == "done" and final_status is None:
                    final_status = "ok"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        raise CliError("/api/chat 返回 HTTP %s：%s" % (exc.code, detail or exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise CliError("连不上 %s（%s）。服务起了吗？端口/--base 对吗？" % (base, exc.reason)) from exc
    except OSError as exc:
        raise CliError("对话流中断：%s" % exc) from exc

    if final_status is None:
        print("\n[WARN] 流在 final 之前结束，本轮结果未知。", file=sys.stderr)
        return 1
    return 1 if final_status == "error" else 0


# ---------------------------------------------------------------------------
# bridge
# ---------------------------------------------------------------------------

def _cmd_bridge(base, args) -> int:
    timeout = args.timeout or DEFAULT_TIMEOUT

    if args.bridge_command == "list":
        data = _request_json(base, "/api/bridge_writes",
                             params={"session": args.session}, timeout=timeout)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0

        enabled = bool(data.get("write_bridge"))
        pending = data.get("pending") or []
        print("写桥接(服务报告): %s" % ("开启" if enabled else "关闭"))
        print("待确认写: %d 条%s" % (len(pending), "（会话 %s）" % args.session if args.session else ""))
        if not pending:
            print("（无）")
            return 0
        for item in pending:
            print("")
            print("#%s  tier=%s  need=%s/%s  tool=%s  session=%s"
                  % (item.get("id"), item.get("tier"), item.get("confirms"),
                     item.get("need"), item.get("tool"), item.get("session")))
            if item.get("path"):
                print("    path: %s" % item.get("path"))
            if item.get("reason"):
                print("    reason: %s" % item.get("reason"))
        return 0

    if args.bridge_command == "confirm":
        route, verb = "/api/confirm_bridge_write", "确认"
    else:
        route, verb = "/api/reject_bridge_write", "拒绝"

    data = _request_json(base, route,
                         payload={"id": args.id, "session": args.session}, timeout=timeout)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        message = data.get("message") or ("成功" if data.get("ok") else "失败")
        print("%s #%s：%s" % (verb, args.id, message))
    return 0 if data.get("ok") else 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    _tune_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.base and args.port is not None:
        print("--base 与 --port 只能给一个。", file=sys.stderr)
        return 2
    try:
        base = _resolve_base(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        if args.command == "doctor":
            return _cmd_doctor(base, args)
        if args.command == "chat":
            return _cmd_chat(base, args)
        if args.command == "bridge":
            return _cmd_bridge(base, args)
    except CliError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
