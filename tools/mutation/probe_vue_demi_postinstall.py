"""核实：vue-demi 的 postinstall 到底改写了 `lib/index.mjs` 的什么。

我已经两次把这条说错：

1. 第一次说「`lib/index.mjs` 是 postinstall 的产物，包里没有」——
   **不成立**，`npm pack` 显示 tarball 里就有 `lib/index.mjs`。
2. 第二次说「加 `--ignore-scripts` 会让它停在默认变体上」——
   也需要核实，机制在 `utils.js` 的 `copy()` 里有一行 `content.replace(/'vue'/g, ...)`。

这一份只读代码 + 比对哈希，不跑 npm。
"""

from __future__ import annotations

import hashlib
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = ROOT / "frontend/node_modules/@vueuse/core/node_modules/vue-demi"


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    lib = BASE / "lib"
    index = lib / "index.mjs"

    print("  === postinstall 的 copy() 做了什么（逐字）===")
    utils = (BASE / "scripts" / "utils.js").read_text(encoding="utf-8")
    for i, line in enumerate(utils.splitlines(), 1):
        if re.search(r"replace|writeFileSync|readFileSync|switchVersion|copy\(", line):
            print("    %3d| %s" % (i, line.rstrip()))

    print()
    print("  === 装好的 lib/index.mjs 里那行 import 是什么 ===")
    text = index.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if "from" in line and "vue" in line.lower():
            print("    %3d| %s" % (i, line.strip()[:110]))

    print()
    print("  === 占位 'vue' 还在吗（postinstall 会把它替换掉）===")
    bare = re.findall(r"'vue'", text)
    print("    裸占位 'vue' 出现 %d 次%s"
          % (len(bare), "" if bare else "  <- 已被替换/或本就是解析结果"))

    print()
    print("  === 哈希关系（这一项才是判据）===")
    d_index = sha256(index)
    print("    lib/index.mjs    %s" % d_index[:32])
    for v in sorted(p for p in lib.iterdir() if p.is_dir()):
        f = v / "index.mjs"
        if f.exists():
            d = sha256(f)
            same = "  <= 逐字相同" if d == d_index else "  (不同)"
            print("    lib/%-6s/index.mjs %s%s" % (v.name, d[:32], same))

    print()
    print("  === 判定 ===")
    v3 = lib / "v3" / "index.mjs"
    if not v3.exists():
        print("    *** 无 v3 变体，无法判定 ***")
        return
    same_v3 = sha256(v3) == d_index
    print("    index.mjs 与 v3/index.mjs 逐字相同: %s" % same_v3)
    if same_v3:
        print()
        print("    => postinstall 跑过，但**没有产生内容差异**（我们用的就是 vue，")
        print("       replace 把 'vue' 替成 'vue'，等于原样）。")
        print("    => 所以「index.mjs 是 postinstall 的产物」这个说法**是错的**：")
        print("       它在 npm 包里就有，postinstall 在我们的场景下是**幂等**的。")
        print()
        print("    ⚠ 那「加 --ignore-scripts 会弄坏前端」还成立吗？")
        print("       本探针**不能证明**它 —— 需要真跑一次 `npm ci --ignore-scripts`")
        print("       再比对产物差异。那是一次会动 node_modules 的实验，")
        print("       而且会撞上前端 agent 正在用的那棵树，**本轮不做**。")
    else:
        print()
        print("    => index.mjs 与 v3 变体不同 —— postinstall 确实改写了内容，")
        print("       说明这个包在安装时必须能执行脚本。")


if __name__ == "__main__":
    main()