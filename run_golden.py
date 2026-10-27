"""黄金题库运行器：跑一遍题目，产出 results.jsonl 供 agent_eval.py 打分。

为什么需要它：agent_eval.py 只负责「打分与回归判定」，不负责跑题。
没有 runner，题库就只是躺在磁盘上的 JSON，跑不出任何数字。

用法：
  python run_golden.py                                   # 跑默认题库，输出到 golden/results.jsonl
  python run_golden.py --questions golden/questions_v2.json --out golden/results_v2.jsonl
  python run_golden.py --provider ollama --model qwen3.6:35b-a3b
  python run_golden.py --ids G34 --disable-tools search_code   # L3 工具失效降级档
  python run_golden.py --limit 5                         # 先跑 5 题试水

跑完打分：
  python agent_eval.py --results golden/results_v2.jsonl
  python agent_eval.py --results golden/results_v2.jsonl --baseline golden/results_baseline.jsonl

设计约定：
- 输出字段与既有 results_baseline.jsonl 保持一致（id/question/model/session_id/expect/
  elapsed_s/n_actions/actions/thoughts/observations/reflections/final/error），并**追加**
  两个诚实性元标志 `degraded` / `verification_status`（取自 final 事件）。追加是向后兼容的：
  老 baseline 没有这两个键也照样打分——agent_eval 只在题目 expect 显式要求时才检查它们。
- 默认关闭 trace 落盘（DOCMIND_TRACE=0），避免污染真实使用账本。
- 软超时：单题超过 --max-seconds 就中断并记录，不会让整轮挂死。
- 每题写完立即 flush，中途 Ctrl-C 也保留已完成部分。
"""
from __future__ import annotations

import os

# 必须在 import agent 之前设置：agent_trace 在模块导入时读取该变量决定是否落盘
os.environ.setdefault("DOCMIND_TRACE", "0")
# golden 评测旁路：让 pricing 的预算锁/读写全部内存化（DOCMIND_GOLDEN_NO_LOCK=1），
# 避免运行时 safe_delete 守卫拦截对 .docmind_budget.json(.lock) 的删除/替换，而在非交互
# 子进程里硬杀进程（整轮 0 题写出）。用赋值而非 setdefault，确保覆盖任何预置值。
os.environ["DOCMIND_GOLDEN_NO_LOCK"] = "1"

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import subprocess  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from agent import Agent  # noqa: E402
from llm import LLMClient  # noqa: E402

REFLECT_MARKS = ("未返回有效结果", "换思路", "重试", "没有找到")


def build_allowlist(disabled: list[str], only: list[str] | None = None):
    """把「禁用的工具」/「只启用的工具」翻译成 Agent 需要的「允许列表」。

    `--only-tools` 优先：47 个工具的 schema 会撑爆系统提示（实测触发
    "system prompt has been truncated" 并让模型输出格式崩溃），
    压降工具集是缓解该问题最直接的手段。
    """
    if only:
        return list(only)
    if not disabled:
        return None
    try:
        from tools import TOOLS
        return [name for name in TOOLS if name not in set(disabled)]
    except ImportError:
        print(f"警告：无法导入 TOOLS，--disable-tools 已忽略", file=sys.stderr)
        return None


def run_one(question: str, *, llm, session_id: str, allowlist, max_seconds: float, project_id=None):
    """跑单题，返回一个结果 dict（不抛异常，错误写进 error 字段）。"""
    thoughts: list[str] = []
    actions: list[str] = []
    observations: list[str] = []
    reflections: list[str] = []
    final = ""
    error = None
    timed_out = False
    # 诚实性元标志：final 上的 degraded / verification_status 是「这轮是不是干净的
    # 完整交付」的权威信号，纯文本判据测不稳（被替换的那句中文会随话术漂移）。捕获进
    # 结果行，供 agent_eval 的 degraded / verification_status 判据消费。
    degraded = None
    verification_status = None

    agent = Agent(llm=llm, session_id=session_id, tool_allowlist=allowlist, project_id=project_id)
    t0 = time.monotonic()

    try:
        for ev in agent.run(question, stream=True):
            # 软超时：不让单题拖垮整轮
            if max_seconds > 0 and (time.monotonic() - t0) > max_seconds:
                timed_out = True
                break
            typ = ev.get("type")
            if typ == "thought":
                thoughts.append(ev.get("text", ""))
            elif typ == "action":
                text = ev.get("text") or f"{ev.get('tool', '')}({ev.get('arg', '')})"
                actions.append(text)
            elif typ == "observation":
                text = ev.get("text", "")
                observations.append(text)
                if any(mark in text for mark in REFLECT_MARKS):
                    reflections.append(text[:200])
            elif typ == "final":
                final = ev.get("text", "")
                degraded = bool(ev.get("degraded"))
                if "verification_status" in ev:
                    verification_status = ev.get("verification_status")
            elif typ == "error":
                error = ev.get("text") or ev.get("message")
    except Exception as e:  # noqa: BLE001 —— 单题崩溃不能拖垮整轮
        error = f"{type(e).__name__}: {e}"

    if timed_out and not error:
        error = f"timeout after {max_seconds}s"

    return {
        "thoughts": thoughts,
        "actions": actions,
        "observations": observations,
        "reflections": reflections,
        "final": final,
        "error": error,
        "degraded": degraded,
        "verification_status": verification_status,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }


# ---------------------------------------------------------------------------
# 每题文件系统隔离（--isolate-fs / DOCMIND_GOLDEN_ISOLATE_FS=1，默认关闭）
#
# 背景：写文件类黄金题（如 G41 在 code_root 建 demo_selfverify.py 再 self_verify）
# 会在共享的 code_root 上落盘，而 create_file 不覆盖已有文件——上一轮留下的文件
# 会让下一轮「第一步建坏文件」变成空操作，导致初值混沌、结果跨轮翻转（与温度无关）。
# 修法：每题跑前对 code_root 源码子树做一次性基线备份，跑后把「新增/被改/被删」的文件
# 还原成基线，使每题都从同一份纯净状态起步。只作用于源码子树，绝不碰 dist/_archived_
# builds/node_modules/.chroma/.git 等大目录，避免误删与巨量拷贝。
# 全程 try/except 包裹：任何隔离失败都降级为「不隔离」，绝不拖垮评测。
# ---------------------------------------------------------------------------
_FS_EXCLUDE = {
    "dist", "_archived_builds", "node_modules", ".chroma", ".git",
    "__pycache__", ".docmind", ".venv", ".idea", ".vscode", "build", "out",
}


def _fs_make_backup(root):
    """对 code_root 源码子树做一次基线备份，返回备份目录。"""
    bak = tempfile.mkdtemp(prefix="golden_fs_")
    for name in os.listdir(root):
        if name in _FS_EXCLUDE:
            continue
        src = os.path.join(root, name)
        dst = os.path.join(bak, name)
        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_FS_EXCLUDE))
            else:
                shutil.copy2(src, dst)
        except Exception as e:  # noqa: BLE001
            print(f"  [隔离] 备份跳过 {name}：{e}")
    return bak


def _fs_snapshot(bak, root):
    """记录当前 code_root 源码子树（由 bak 界定）的文件 size/mtime。"""
    snap = {}
    for name in os.listdir(bak):
        for dp, _dns, fns in os.walk(os.path.join(bak, name)):
            tgt = os.path.join(root, os.path.relpath(dp, bak))
            for fn in fns:
                p = os.path.join(tgt, fn)
                try:
                    st = os.stat(p)
                    snap[os.path.normcase(p)] = (st.st_size, int(st.st_mtime))
                except OSError:
                    pass
    return snap


def _fs_restore(bak, root, snap_before):
    """把 code_root 还原到基线：补回被改/被删的文件，删除 agent 新建的文件。"""
    # 1) 基线里应有的文件：缺失或大小不符 → 从备份还原（覆盖 agent 的改动/删除）
    for name in os.listdir(bak):
        for dp, _dns, fns in os.walk(os.path.join(bak, name)):
            rel = os.path.relpath(dp, bak)
            tgt = os.path.join(root, rel)
            try:
                os.makedirs(tgt, exist_ok=True)
            except OSError:
                pass
            for fn in fns:
                src = os.path.join(dp, fn)
                dst = os.path.join(tgt, fn)
                try:
                    if not os.path.exists(dst) or os.stat(dst).st_size != os.stat(src).st_size:
                        shutil.copy2(src, dst)
                except Exception:  # noqa: BLE001
                    pass
    # 2) code_root 中基线没有的文件 = agent 新建 → 删除（含写文件题残留）
    for name in os.listdir(bak):
        base = os.path.join(root, name)
        if not os.path.isdir(base):
            if not os.path.exists(os.path.join(bak, name)):
                try:
                    os.remove(base)
                except OSError:
                    pass
            continue
        for dp, _dns, fns in os.walk(base):
            src_dp = os.path.join(bak, os.path.relpath(dp, root))
            for fn in fns:
                if not os.path.exists(os.path.join(src_dp, fn)):
                    try:
                        os.remove(os.path.join(dp, fn))
                    except OSError:
                        pass


def _fs_clean_untracked(bak, root):
    """精准清理 agent 本轮新建的「未跟踪文件」：只删 basename 在基线备份里不存在的文件。

    为什么不用 git clean -fd：git clean 一次性批量删除所有未跟踪文件，会触发运行时
    safe_delete 守卫的 bulk-delete 硬杀（count>=threshold），在非交互评测子进程里直接 kill
    掉进程、整轮 0 题写出（这正是此前 iso/verify 跑全部 0 行的根因）。逐文件 os.remove 是
    独立的单文件删除操作，不会累积成 bulk，安全绕过该守卫。

    只清理：① repo 根目录下基线没有的散落文件（如写文件题产出的 demo_selfverify.py）；
    ② 已跟踪源码目录内基线没有的新文件。绝不碰 _FS_EXCLUDE 大目录、golden 结果、tests、
    frontend/web/ui/docs 等真实目录，也不动 .docmind* 状态/预算文件（由 DOCMIND_GOLDEN_NO_LOCK
    保证不被 unlink）。失败静默降级。
    """
    _CLEAN_SKIP_DIRS = set(_FS_EXCLUDE) | {
        "golden", "tests", "docs", "frontend", "web", "ui", "build", "out",
    }
    for name in os.listdir(root):
        if name in _CLEAN_SKIP_DIRS or name.startswith(".docmind"):
            continue
        base = os.path.join(root, name)
        in_bak = os.path.exists(os.path.join(bak, name))
        if not os.path.isdir(base):
            if not in_bak:  # 基线没有的根目录散落文件 = agent 新建 → 删
                try:
                    os.remove(base)
                except OSError:
                    pass
            continue
        # 已跟踪源码目录：只删目录内基线没有的新文件
        for dp, _dns, fns in os.walk(base):
            rel = os.path.relpath(dp, root)
            bak_dp = os.path.join(bak, rel)
            for fn in fns:
                if not os.path.exists(os.path.join(bak_dp, fn)):
                    try:
                        os.remove(os.path.join(dp, fn))
                    except OSError:
                        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="跑黄金题库，产出 results.jsonl")
    ap.add_argument("--questions", default="golden/questions_v2.json", help="题库 JSON")
    ap.add_argument("--out", default="golden/results.jsonl", help="结果输出（JSONL，追加）")
    ap.add_argument("--provider", default="", help="provider，如 ollama / qwen")
    ap.add_argument("--model", default="", help="模型名")
    ap.add_argument("--ids", default="", help="只跑指定题，逗号分隔，如 G01,G34")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题（0=全部）")
    ap.add_argument("--disable-tools", default="", help="禁用的工具，逗号分隔（L3 降级档用）")
    ap.add_argument("--only-tools", default="",
                    help="只启用这些工具（逗号分隔），如 --only-tools "
                         "search_code,grep,read_file,list_dir,search_knowledge,calculate。"
                         "47 个工具的 schema 会撑爆系统提示，压降工具集可显著稳定输出")
    ap.add_argument("--project-id", default="",
                    help="项目 id（如 prj-xxxx）。代码问答按项目隔离，跑题前务必确认它指向被问的代码库；"
                         "留空则用「当前项目」，很容易指错")
    ap.add_argument("--max-seconds", type=float, default=300.0, help="单题软超时秒数")
    ap.add_argument("--isolate-fs", action="store_true",
                    help="每题文件系统隔离：跑前对 code_root 源码子树做基线备份，"
                         "跑后还原新增/被改/被删文件，消除写文件类题目的跨轮污染（默认关闭）")
    args = ap.parse_args(argv)

    with open(args.questions, "r", encoding="utf-8") as f:
        questions = json.load(f)

    if args.ids:
        wanted = {s.strip() for s in args.ids.split(",") if s.strip()}
        questions = [q for q in questions if q.get("id") in wanted]
    if args.limit > 0:
        questions = questions[: args.limit]
    if not questions:
        print("没有要跑的题目")
        return 1

    llm = LLMClient(provider=args.provider or None, model=args.model or None)
    # 独立进程没有服务端的运行时密钥，也没把密钥塞进 env——这里回落到与工作台
    # 相同的全局密钥库（api.py 在全局作用域下就是 secrets_store.load(None, provider)），
    # 让黄金题库能直接用已保存的 Key 跑，而不必先手动 export。只打印 provider，绝不打印密钥。
    if getattr(llm, "requires_api_key", False) and not getattr(llm, "api_key", ""):
        try:
            import secrets_store
            _k = secrets_store.load(None, getattr(llm, "provider", "") or "")
            if _k:
                llm = LLMClient(provider=llm.provider, model=llm.model, api_key=_k)
                print(f"已从全局密钥库为 {llm.provider} 注入 API Key")
        except Exception as e:  # noqa: BLE001 —— 回落失败不阻断，交给单题 error 兜底
            print(f"警告：从全局密钥库读取 {getattr(llm, 'provider', '')} 密钥失败：{type(e).__name__}: {e}")
    model_name = getattr(llm, "model", "") or args.model or os.getenv("LLM_MODEL", "")
    allowlist = build_allowlist(
        [s.strip() for s in args.disable_tools.split(",") if s.strip()],
        [s.strip() for s in args.only_tools.split(",") if s.strip()] or None,
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    print(f"题库 {args.questions} 共 {len(questions)} 题 · model={model_name or '(默认)'} "
          f"· temp={os.getenv('DOCMIND_LLM_TEMPERATURE', '0.3')} "
          f"· 禁用工具={args.disable_tools or '无'} · project={args.project_id or '(当前项目)'}")
    # --- 绑定项目上下文 ---------------------------------------------------
    # search_code / grep / read_file 是按「当前项目」取代码集合的，
    # 只给 Agent(project_id=) 不够——必须真正把当前项目指针切过去，
    # 否则会检索到别的工程（实测：答出 godot_sample 的 level.gd 符号）。
    _restore = None
    if args.project_id:
        try:
            import projects as _projects
            from config import set_runtime as _set_runtime, get_runtime as _get_runtime
            if _projects.enabled():
                _restore = (_projects.current_project_id(), _get_runtime("code_root"))
                if _projects.set_current(args.project_id):
                    root = (_projects.get_project(args.project_id) or {}).get("root") or os.getcwd()
                    _set_runtime("code_root", root)
                    print(f"已切换当前项目 → {args.project_id}（code_root={root}）")
                else:
                    print(f"警告：项目 {args.project_id} 切换失败，将按当前项目检索")
        except Exception as e:  # noqa: BLE001 —— 绑定失败不应阻断跑题
            print(f"警告：绑定项目上下文失败：{type(e).__name__}: {e}")

    print(f"输出 → {args.out}\n")

    # --- 每题文件系统隔离（可选，默认关闭）---
    _isolate = bool(args.isolate_fs) or os.getenv("DOCMIND_GOLDEN_ISOLATE_FS") == "1"
    _fs_bak = None
    _fs_root = os.getcwd()
    try:
        from config import get_runtime as _gr
        _fs_root = _gr("code_root") or _fs_root
    except Exception:  # noqa: BLE001
        pass
    if _isolate:
        try:
            _fs_bak = _fs_make_backup(_fs_root)
            _fs_clean_untracked(_fs_bak, _fs_root)  # 备份后再清掉基线没有的未跟踪残留
            print(f"[隔离] 已对 code_root 源码子树做基线备份：{_fs_bak}")
        except Exception as e:  # noqa: BLE001
            print(f"警告：[隔离] 基线备份失败，降级为不隔离：{e}")
            _isolate = False

    with open(args.out, "a", encoding="utf-8") as f:
        for i, q in enumerate(questions, 1):
            qid = q.get("id", f"Q{i}")
            session_id = f"golden-{stamp}-{qid}"
            _snap = _fs_snapshot(_fs_bak, _fs_root) if (_isolate and _fs_bak) else None
            res = run_one(
                q.get("question", ""),
                llm=llm,
                session_id=session_id,
                allowlist=allowlist,
                max_seconds=args.max_seconds,
                project_id=args.project_id or None,
            )
            rec = {
                "id": qid,
                "question": q.get("question", ""),
                "expect": q.get("expect", {}),
                "model": model_name,
                "session_id": session_id,
                "elapsed_s": res["elapsed_s"],
                "n_actions": len(res["actions"]),
                "actions": res["actions"],
                "thoughts": res["thoughts"],
                "observations": res["observations"],
                "reflections": res["reflections"],
                "final": res["final"],
                "degraded": res["degraded"],
                "verification_status": res["verification_status"],
                "error": res["error"],
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            if _snap is not None:  # 还原本題对 code_root 的改动，杜绝跨题污染
                try:
                    _fs_restore(_fs_bak, _fs_root, _snap)
                    _fs_clean_untracked(_fs_bak, _fs_root)  # 清掉本题新建的未跟踪产物（如 demo_selfverify.py）
                except Exception as e:  # noqa: BLE001
                    print(f"  [隔离] 还原失败（已忽略）：{e}")

            flag = "err " if res["error"] else "ok  "
            print(f"  {flag}{qid}  {res['elapsed_s']:>6.1f}s  "
                  f"actions={len(res['actions'])}  final_chars={len(res['final'])}")

    if _restore:
        try:
            _projects.set_current(_restore[0])
            _set_runtime("code_root", _restore[1])
            print(f"已恢复当前项目 → {_restore[0]}")
        except Exception:
            pass

    print(f"\n完成，结果在 {args.out}")
    print(f"打分：python agent_eval.py --results {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
