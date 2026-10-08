"""钉住 `docs/harness-research-verified.md` §1.4 的两个供应链结论。

这两个结论都是「看起来做完了、实际没生效」那类，所以判据必须是**可观测状态**，
不是「代码里有没有某个字符串」。

## 结论一：不能给 `npm ci` 加 `--ignore-scripts`

依据不是「`--ignore-scripts` 听起来很安全」，而是**实测**：

* lockfile 里 263 个包条目中 4 个标了 `hasInstallScript`
* 其中 `@vueuse/*/vue-demi` 的 `lib/index.mjs` **是 postinstall 生成的产物**
  （npm 包解包后只有 `bin/ lib/ scripts/ package.json LICENSE README.md`）
* 实测 `lib/index.mjs` 的 SHA256 == `lib/v3/index.mjs`，≠ `lib/v2/index.mjs`
  —— 证明 postinstall 确实跑过，且按 `Vue.version` 选了 v3

所以判据是**哈希相等**：哪天 `lib/index.mjs` 不再等于任何变体，
就说明 postinstall 没跑（或被禁了），前端会在运行时崩。

## 结论二：`--ignore-scripts` 只覆盖了一条 npm 路径

三个 npm 调用点，两个没关脚本。**判据是从 AST 取调用点**，
而不是 grep 字符串 —— 因为 grep 会漏（`["npm", "ci"]` 写成列表就搜不到 `npm ci`）。

只读。不跑 npm，不改任何文件。
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]

NPM_CALL_SITES = {
    "dependency_bootstrap.py": "启动时同步前端依赖",
    "agent_runtime/tool_install.py": "agent 安装 MCP 工具",
    "frontend/package.json": "前端脚本（build/test）",
}
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"
LOCKFILE = ROOT / "frontend" / "package-lock.json"
VUE_DEMI = ROOT / "frontend/node_modules/@vueuse/core/node_modules/vue-demi"


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def npm_invocations(tree: ast.AST) -> list[list[str]]:
    """取出所有 `npm ...` 形式的调用参数。AST 判据，不受书写方式影响。"""
    out: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.List, ast.Tuple)):
            continue
        elts = node.elts
        if not elts:
            continue
        first = elts[0]
        name = None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            name = first.value
        elif isinstance(first, ast.Name):
            name = first.id
        if name is None or not str(name).endswith("npm"):
            continue
        argv: list[str] = []
        for e in elts:
            if isinstance(e, ast.Constant) and isinstance(e.value, str):
                argv.append(e.value)
            elif isinstance(e, ast.Name):
                argv.append(e.id)
            elif isinstance(e, ast.Attribute):
                argv.append(e.attr)
            else:
                argv.append("<expr>")
        out.append(argv)
    return out


def main() -> int:
    print("=" * 76)
    print("  结论一的判据：vue-demi 的 postinstall 产物是否在位")
    print("=" * 76)

    if not VUE_DEMI.exists():
        print("  *** node_modules 不存在（未 npm ci？）—— 无法验证 ***")
        print("  装好依赖后重跑。")
        return 1

    lib = VUE_DEMI / "lib"
    shipped = sorted(p.name for p in VUE_DEMI.iterdir())
    print("  npm 包解包后的顶层内容: %s" % ", ".join(shipped))
    print("  ⚠ 注意：这里查的是【已安装目录】，不是 npm 包（tarball）内容。")
    print("    tarball 里本来就有 lib/index.mjs —— 见下面【本探针不能证明什么】。")

    index = lib / "index.mjs"
    variants = {v.name: v / "index.mjs" for v in sorted(lib.iterdir())
                if v.is_dir() and (v / "index.mjs").exists()}
    if not index.exists():
        print("  *** lib/index.mjs 不存在 —— postinstall 没跑（或被 --ignore-scripts 禁了）***")
        print("  这正是加 --ignore-scripts 会造成的状态。")
        return 1

    digest = sha256(index)
    print()
    print("  lib/index.mjs SHA256 = %s" % digest)
    matched = [name for name, path in variants.items() if sha256(path) == digest]
    for name, path in sorted(variants.items()):
        mark = "  <= 与产物相同" if name in matched else ""
        print("  lib/%-6s/index.mjs %s%s" % (name, sha256(path)[:16], mark))

    if not matched:
        print()
        print("  *** index.mjs 不等于任何变体 —— postinstall 状态不明 ***")
        return 1
    print()
    print("  => 匹配变体: %s —— postinstall 确实跑过，按 Vue.version 切到了该变体。" % matched)

    print()
    print("=" * 76)
    print("  结论二的判据：三条 npm 路径里哪些关了 lifecycle 脚本")
    print("=" * 76)

    guarded: list[str] = []
    unguarded: list[str] = []

    for rel, why in NPM_CALL_SITES.items():
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        argv_lists = npm_invocations(tree)
        if not argv_lists:
            continue
        for argv in argv_lists:
            sub = [a for a in argv if a in ("ci", "install", "run")]
            verb = sub[0] if sub else "?"
            label = "%s（%s）: npm %s" % (rel, why, verb)
            if "--ignore-scripts" in argv:
                guarded.append(label)
                print("  [已关] %s" % label)
            elif verb in ("ci", "install"):
                unguarded.append(label)
                print("  [未关] %s" % label)

    if CI_FILE.exists():
        for line in CI_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("npm ci") or s.startswith("npm install"):
                label = "CI（%s）: %s" % (CI_FILE.name, s)
                if "--ignore-scripts" in s:
                    guarded.append(label)
                    print("  [已关] %s" % label)
                else:
                    unguarded.append(label)
                    print("  [未关] %s" % label)

    print()
    print("  已关 %d 条 / 未关 %d 条" % (len(guarded), len(unguarded)))

    print()
    print("  === 仍会跑 lifecycle 脚本的包（来自 lockfile）===")
    if not LOCKFILE.exists():
        print("  *** 无 lockfile ***")
        return 1
    packages: dict[str, Any] = json.loads(LOCKFILE.read_text(encoding="utf-8")).get("packages", {})
    scripts = [(k, v) for k, v in packages.items()
               if isinstance(v, dict) and v.get("hasInstallScript")]
    print("  lockfile 包条目 %d 个，其中带 install 脚本 %d 个:" % (len(packages), len(scripts)))
    for key, meta in sorted(scripts):
        flags = " ".join(f for f, v in (("dev", "dev"), ("optional", "optional"))
                         if meta.get(f))
        print("    %-46s %s" % (key.replace("node_modules/", ""), flags or "生产依赖"))

    prod = [k for k, m in scripts if not m.get("dev") and not m.get("optional")]
    print()
    if prod:
        print("  => 生产依赖里有带 install 脚本的包: %s" % ", ".join(prod))
        print("     它们会在【启动同步】与【CI】两条未关脚本的路径上执行。")
    else:
        print("  => 带 install 脚本的包全是 dev/optional，生产路径不受影响。")

    print()
    print("  === 【本探针不能证明什么】—— 恒真判据的边界 ===")
    print("  1. 上面「index.mjs 与某变体哈希相同」只证明 postinstall **跑过**，")
    print("     **不能**证明「不跑就坏」。vue-demi 的 copy() 做的是")
    print("     content.replace(/'vue'/g, \"'vue'\") —— 我们装的就是 vue，**幂等**。")
    print("  2. 本探针**没有跑过 npm**。「加 --ignore-scripts 会不会坏前端」是")
    print("     **未验证假设**，需要真跑 `npm ci --ignore-scripts` 再比对构建产物。")
    print("     我曾因「esbuild 的 postinstall 在下载二进制」断定会坏 —— **不成立**，")
    print("     二进制来自 optionalDependency（@esbuild/win32-x64），不靠该脚本下载。")
    print("  3. 「package-lock.json 里标了 hasInstallScript」是**声明**，不是**实测**：")
    print("     npm 可能因平台/optional 而跳过某些包。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())