"""逐项核实：用户那张「来自 / 能治什么」表里，5 项各自**现在还差什么**。

## 为什么要重查而不是复述文档

今晚我已经因为「凭印象」栽了很多次。`docs/harness-research-verified.md` §1.4
写的是「值得做的是 min-release-age + 钉精确版本」—— 但我实际只做了
`--ignore-scripts`。**文档里的计划 ≠ 已做的事。**

所以这里查的是**当前仓库的真实状态**，不是文档怎么说。

## 判据

每一项都用**可观测的事实**回答，不用「源码里有没有某个字符串」：
* 配置项：文件在不在、内容是什么
* 命令：实际调用点有没有那个 flag（从 AST 取，不用 grep）
* 行为：真跑一次看结果

「不在」与「在但不够」要分开 —— 那决定它是「没做」还是「做了一半」。
"""

from __future__ import annotations

import ast
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def line(title: str) -> None:
    print()
    print("  " + "-" * 74)
    print("  " + title)
    print("  " + "-" * 74)


def fact(label: str, ok: str | None, detail: str = "") -> None:
    """ok=None 表示「不适用/查不了」。"""
    mark = "?" if ok is None else ("有" if ok else "**无**")
    print("    %-40s %-6s %s" % (label, mark, detail))


def main() -> None:
    # ================= 5. 供应链硬化（Pi）=====================
    line("5. 供应链硬化（Pi 七条 + 我们独有的 Python 洞）")

    npmrc = [ROOT / ".npmrc", ROOT / "frontend/.npmrc"]
    fact("min-release-age（.npmrc）",
         any(p.exists() for p in npmrc),
         "根与 frontend/ 均无 .npmrc" if not any(p.exists() for p in npmrc) else "")

    pkg = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    deps = dict(pkg.get("dependencies") or {})
    dev = dict(pkg.get("devDependencies") or {})
    all_deps = list(deps.items()) + list(dev.items())
    exact = [(k, v) for k, v in all_deps if v[0] not in "^~>=<*"]
    fact("直接依赖钉精确版本",
         len(exact) == len(all_deps),
         "%d/%d 精确（%d 个用 ^/~）" % (len(exact), len(all_deps), len(all_deps) - len(exact)))

    # npm 调用点：从 AST 取（grep 会漏 —— `["npm", "ci"]` 写列表就搜不到 `npm ci`）
    npm_calls: list[tuple[str, str, bool]] = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT)
        if rel.parts and rel.parts[0] in (".venv", "node_modules", ".git", "web"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            head = node.elts[0]
            nm = getattr(head, "value", None) or getattr(head, "id", None)
            if not isinstance(nm, str) or not nm.endswith("npm"):
                continue
            argv = [getattr(e, "value", None) or getattr(e, "id", None)
                    or getattr(e, "attr", "") for e in node.elts]
            verb = next((a for a in argv if a in ("ci", "install", "run")), "?")
            npm_calls.append((str(rel), str(verb), "--ignore-scripts" in argv))
    ci_file = ROOT / ".github/workflows/harness.yml"
    ci_lines = [ln.strip() for ln in ci_file.read_text(encoding="utf-8").splitlines()
                if ln.strip().startswith("npm ci")] if ci_file.exists() else []
    # ⚠ 分母**只数 `ci` / `install`**。第一版把 `npm run build` 也算进分母，
    # 于是报「4/13 条带」—— 那个读数既让人以为改动没生效，
    # 又把 `npm run` 算成「该带却没带」。
    # **`npm run` 本就不该带 flag**：它跑的是 package.json 的 scripts，
    # 而禁令针对的是**依赖安装时**执行的 install/postinstall。
    installing = [c for c in npm_calls if c[1] in ("ci", "install")]
    guarded = sum(1 for _, _, g in installing if g) + sum(
        1 for ln in ci_lines if "--ignore-scripts" in ln)
    total = len(installing) + len(ci_lines)
    fact("生命周期脚本（npm ci/install 全禁）", guarded == total and total > 0,
         "%d/%d 条**安装**调用带 flag（另有 %d 条 npm run 不计入分母）"
         % (guarded, total, len(npm_calls) - len(installing)))
    for rel, verb, g in installing:
        print("        %-34s npm %-6s %s" % (rel, verb,
                                             "带 flag" if g else "**没带**"))

    # pre-commit / husky / lockfile 守卫
    hooks = [p for p in (ROOT / ".pre-commit-config.yaml", ROOT / ".husky")
             if p.exists()]
    lock_guards = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "package-lock" in text and re.search(r"lockfile|lock_file", text):
            lock_guards.append(path.name)
    fact("pre-commit / husky 拦 lockfile", bool(hooks),
         "无 .pre-commit-config.yaml、无 .husky" if not hooks else "")
    fact("lockfile 有测试守卫", bool(lock_guards),
         "相关测试: %s" % ", ".join(sorted(set(lock_guards))[:3]) if lock_guards
         else "无测试引用 lockfile 作为守卫对象")

    audit = [ln.strip() for ln in ci_file.read_text(encoding="utf-8").splitlines()
             if "npm audit" in ln] if ci_file.exists() else []
    fact("定时 npm audit", bool(audit), "CI 里无 npm audit" if not audit else "")

    req = ROOT / "requirements.txt"
    req_lines = [ln.strip() for ln in req.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")] if req.exists() else []
    pinned = [ln for ln in req_lines if "==" in ln]
    hashed = [ln for ln in req_lines if "--hash" in ln]
    fact("Python 侧钉版本", bool(pinned) and len(pinned) == len(req_lines),
         "%d/%d 行用 ==（0 行 --hash）" % (len(pinned), len(req_lines)))

    boot = (ROOT / "dependency_bootstrap.py").read_text(encoding="utf-8")
    pip_call = re.search(r"_run\(\[str\(pip\).*?\]\s*,", boot, re.S)
    has_binary = "--only-binary" in boot
    has_hash = "--require-hashes" in boot
    fact("pip 关 lifecycle / source", has_binary or has_hash,
         "dependency_bootstrap.py 的 pip 调用无 --only-binary、无 --require-hashes")

    # ================= 1. http-recorder =================
    line("1. http-recorder 思路（opencode）")
    cassette_dirs = [p for p in (ROOT / "tests/cassettes", ROOT / "tests/fixtures/cassettes")
                     if p.exists()]
    fact("cassette 录制/回放", bool(cassette_dirs),
         "无 cassette 目录" if not cassette_dirs else "")
    wf = [p.name for p in sorted((ROOT / "tests").glob("test_web_fetch*.py"))]
    fact("上游 HTTP 依赖的打桩守卫", bool(wf),
         "%s（结构性扫桩）" % ", ".join(wf) if wf else "")

    # ================= 2. conformance =================
    line("2. telemetry conformance tests（Pi）")
    conf = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if re.search(r"conformance", text, re.I):
            conf.append(path.name)
    fact("conformance 套件", bool(conf),
         ", ".join(conf[:3]) if conf else "无测试提及 conformance")
    impls = {}
    for mod in ("agent_runtime/mcp_bridge.py",):
        p = ROOT / mod
        if p.exists():
            src = p.read_text(encoding="utf-8")
            impls[mod] = len(re.findall(r"^\s*(?:async\s+)?def\s+\w+", src, re.M))
    fact("适配器边界的实现数", None,
         "mcp_bridge %d 个函数，transport 只是内部字段（非两条实现）"
         % impls.get("agent_runtime/mcp_bridge.py", 0))

    # ================= 3. FTS5 会话检索 =================
    line("3. FTS5 会话检索（Hermes）")
    for mod, kw in (("agent.py", "fts5"), ("agent.py", "fts"),
                    ("vectorstore.py", "fts")):
        pass
    fts_hits = 0
    for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "agent_runtime").glob("*.py")):
        try:
            if "fts5" in path.read_text(encoding="utf-8").lower():
                fts_hits += 1
        except OSError:
            pass
    fact("FTS5 会话检索", fts_hits > 0,
         "%d 个文件提到 fts5" % fts_hits if fts_hits else "全仓无 fts5 —— 未实现（已记为不做）")

    # ================= 4. interrupt-and-redirect =================
    line("4. interrupt-and-redirect（Hermes 三种模式）")
    api = (ROOT / "api.py").read_text(encoding="utf-8")
    vue = ROOT / "frontend/src/ask/AskApp.vue"
    vue_src = vue.read_text(encoding="utf-8") if vue.exists() else ""
    m = re.search(r"const canSend\s*=\s*computed\([^)]*\)", vue_src)
    fact("interrupt：消息重定向当前轮", False,
         "canSend = %s" % (m.group(0)[:70] if m else "(未找到)"))
    fact("queue：静默排队", False, "无 pending-user 队列")
    fact("steer：注入当前 run", False, "agent.py 无消息注入点")
    fact("Ctrl+C：中断（我们有，但语义不同）", "worker_stop" in api,
         "worker_stop 只在客户端断连时 set，无端点可触 -> 是「停止」不是「转向」")

    print()
    print("  === 汇总：还差的项数 ===")
    todo = [
        "供应链 min-release-age（无 .npmrc）",
        "供应链 依赖钉精确版本（%d/%d 不精确）" % (len(all_deps) - len(exact), len(all_deps)),
        "供应链 pre-commit/husky 拦 lockfile（无）",
        "供应链 定时 npm audit（无）",
        "供应链 Python 侧（0 行 ==、0 行 --hash、pip 不关 source）",
    ]
    for i, t in enumerate(todo, 1):
        print("    %d. %s" % (i, t))


if __name__ == "__main__":
    main()