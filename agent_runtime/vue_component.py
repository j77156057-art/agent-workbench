"""Wave16：`.vue` 组件级守卫——用项目自带的编译器把真实 SFC 挂进真浏览器。

这一条补的是整个 vibecoding 循环里最后没被守住的一段：**接线本身**。
Wave8 的语音行为回路（`voice_loop`）验的是"生产函数被这样接起来时行为对不对"，
那段胶水是我**对照 `AutonomousCockpit.vue` 转写**的，不是从 `.vue` 里 import 出来的——
所以组件文件被改坏（props 名换了、emit 参数从 id 变成 label、`v-if` 条件写反）
以前是没有任何守卫的。

**为什么不用 vitest**：量化过一次（`vitest@2 + @vue/test-utils + jsdom` 带来
1 critical + 3 moderate + 1 high 的 dev 公告，critical 是 Vitest UI server 任意文件读；
vitest 3 要 vite 6/7 而本树是 vite 5），用户选了"用真浏览器覆盖 `.vue` 行为"这条。
本模块因此**零新依赖**：编译器是仓库里已有的 `@vue/compiler-sfc`（vue-tsc 带进来的，
版本与 `vue` 完全一致），打包器是前端自己的 `esbuild`，点击与读状态是 Wave13 的
`dev_page_action`，静态服务是 Wave7 从 `visual_acceptance` 抽出来的 `serve_static`。

**三件被实测决定了的设计**：

1. esbuild 只能用 **JS API**（`build()` + `onLoad` 插件），CLI 没有插件位；而插件里
   `compileScript` 的输出**仍然是 TypeScript**（`interface Member {...}`、类型标注），
   所以返回的 loader 必须跟着 SFC 自己的 `lang` 走——写死 `loader:'js'` 时 esbuild 会在
   `interface` 那行报 `Expected ";" but found "Member"`（实测）。
2. 页面要能挂起来必须**从同一个 bundle 里拿 `createApp`/`h`**：组件入口自己 import 的
   vue 与页面再 import 一次 vue 会是两个实例，指令/provide 全断。生成的入口因此是
   `import Comp from '<目标.vue>'` + `export { createApp, h } from 'vue'` 同一张图。
3. 中文字符串在 bundle 里是 **`\\uXXXX` 大写转义**（esbuild 默认 charset=ascii），
   想在产物文本里断言"界面有这句话"必须断言**渲染后的 DOM**，不是断言 bundle 源码。

**fail-closed 的样子**：没写任何断言（只 `mount`）判未通过——"挂上了"不是证据；
断言的 emit 名不在组件自己 `defineEmits` 声明的清单里也判未通过，否则会退化成
"永远等不到"的假守卫。
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from agent_runtime import page_action, visual_acceptance as visual

MAX_COMPONENT_BYTES = 1_500_000
MAX_CLICKS = 8
MAX_SELECTORS = 12
#: 允许 `[data-x=2]` 这种属性选择器开头（组件守卫里很常用），但绝不允许以 `-` 开头
#: （会被当成 git/CLI 选项）、带空格、`;`、`$`、反引号或换行。
SELECTOR_RE = re.compile(r"^[.#\[*]?[A-Za-z0-9_*][A-Za-z0-9_.:#\[\]*=\"'-]{0,119}$")
_NODE = ("node",)
ESBUILD_API = ("node_modules", "esbuild", "lib", "main.js")
SFC_API = ("node_modules", "@vue", "compiler-sfc", "dist", "compiler-sfc.cjs.js")

# 生成的 node 侧编译器：绝对路径 createRequire，脚本本身可以放在仓外的状态目录里跑。
COMPILE_SCRIPT = """import { createRequire } from 'node:module'
import { writeFile, readFile } from 'node:fs/promises'
const require = createRequire(%(require_anchor)s)
const { build } = require('esbuild')
const sfc = require('@vue/compiler-sfc')
const { parse, compileScript, compileStyle } = sfc
const fsPath = await import('node:path').then((m) => m.default)
const entry = process.argv[2]
const outfile = process.argv[3]
function styleId(file) {
  let sum = 0
  for (const ch of file) sum = (sum * 31 + ch.charCodeAt(0)) >>> 0
  return 'dv' + sum.toString(36)
}
const vueSfc = {
  name: 'vue-sfc',
  setup(inner) {
    inner.onLoad({ filter: /\\.vue$/ }, async (args) => {
      const source = await readFile(args.path, 'utf8')
      const id = styleId(args.path)
      const parsed = parse(source, { filename: args.path })
      if (parsed.errors.length) {
        throw new Error('SFC 解析失败 ' + args.path + '：'
          + parsed.errors.map((e) => e.message).join('; '))
      }
      const d = parsed.descriptor
      const script = compileScript(d, { id, inlineTemplate: true })
      const styles = []
      for (const style of d.styles) {
        const compiled = compileStyle({ source: style.content, filename: args.path,
                                        id: 'data-v-' + id, scoped: !!style.scoped })
        if (compiled.errors.length) {
          throw new Error('样式编译失败 ' + args.path + '：' + compiled.errors[0])
        }
        styles.push(compiled.code)
      }
      // compileScript 的输出保留 TS 语法，loader 必须跟着 SFC 的 lang 走
      const lang = ((d.scriptSetup || d.script || {}).lang || '')
      return { loader: /ts|tsx/.test(lang) ? 'ts' : 'js',
               contents: script.content + '\\nexport const __vueStyles__ = '
                 + JSON.stringify(styles.join('\\n')) + ';\\n',
               resolveDir: fsPath.dirname(args.path) }
    })
  },
}
try {
  const out = await build({ entryPoints: [entry], bundle: true, format: 'esm',
    write: false, logLevel: 'silent', plugins: [vueSfc],
    define: { 'process.env.NODE_ENV': '"development"', '__VUE_OPTIONS_API__': 'true',
              '__VUE_PROD_DEVTOOLS__': 'false',
              '__VUE_PROD_HYDRATION_MISMATCH_DETAILS__': 'false' } })
  const text = out.outputFiles[0].text
  if (!out.outputFiles[0].text.includes('__vueStyles__')) {
    console.log(JSON.stringify({ ok: false, error: '产物里没有 __vueStyles__：入口没走到 SFC 插件' }))
  } else {
    await writeFile(outfile, text)
    console.log(JSON.stringify({ ok: true, bytes: text.length,
                                 warnings: out.warnings.map((w) => w.text).slice(0, 8),
                                 exports: /export\\{[\\s\\S]*?\\}/.test(text) }))
  }
} catch (err) {
  console.log(JSON.stringify({ ok: false, error: String(err && err.message || err).slice(0, 800) }))
}
"""

MOUNT_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>%(title)s</title></head><body>
<div id="app"></div><button id="sync">同步状态</button><pre id="state">{}</pre>
<script type="module">
import { Comp, __vueStyles__, createApp, h } from './%(bundle)s';
const CFG = %(config)s;
const style = document.createElement('style');
style.textContent = __vueStyles__;
document.head.appendChild(style);
const state = window.__vueGuard = { mounted: false, emits: [], warnings: [], errors: [],
                                     facts: {} };
const show = () => { document.getElementById('state').textContent = JSON.stringify(state); };
const cap = (name) => name.charAt(0).toUpperCase() + name.slice(1);
// emit 监听按组件【自己声明的 emits】建：声明里没有的名字永远等不到，
// 那正是"守卫看起来在守其实没守"的那一类。
const handlers = {};
for (const name of (Comp.emits || [])) {
  handlers['on' + cap(name)] = (...args) => { state.emits.push({ name, args }); show(); };
}
try {
  const app = createApp({ render: () => h(Comp, { ...CFG.props, ...handlers }) });
  app.config.warnHandler = (msg) => { state.warnings.push(String(msg).slice(0, 200)); };
  app.mount('#app');
  state.mounted = true;
  state.declaredEmits = Comp.emits || [];
  state.rendered = !!document.querySelector(CFG.root || '#app > *');
} catch (err) { state.errors.push(String((err && err.message) || err)); }
document.getElementById('sync').addEventListener('click', () => {
  const facts = {};
  // 非法选择器必须当场记成页面错误：以前 present/absent 与 count 各写一份 try，
  // 把其中一份改成"静默返回"就没人知道——现在只有一条 probe() 路径。
  const probe = (sel) => {
    try {
      const nodes = document.querySelectorAll(sel);
      return { any: nodes.length > 0, n: nodes.length };
    } catch (err) {
      state.errors.push('bad selector ' + sel);
      return { any: false, n: -1 };
    }
  };
  for (const sel of (CFG.expect_present || [])) facts['present:' + sel] = probe(sel).any;
  for (const sel of (CFG.expect_absent || [])) facts['absent:' + sel] = !probe(sel).any;
  for (const pair of (CFG.counts || [])) facts['count:' + pair[0]] = probe(pair[0]).n;
  for (const needle of (CFG.texts || [])) {
    facts['text:' + needle] = (document.getElementById('app').textContent || '').includes(needle);
  }
  state.facts = facts;
  state.vueNodes = document.querySelectorAll('#app *').length;
  show();
});
show();
window.__vueGuardReady = true;
</script></body></html>
"""


class VueComponentError(RuntimeError):
    """组件守卫无法开始。"""


def workdir_for(frontend: Path) -> Path:
    """产物目录：必须在【前端树内】。

    入口里 `import { createApp } from 'vue'` 是裸说明符，esbuild 与 node 都从入口所在目录
    向上找 node_modules——放在仓外的临时目录里就解析不到（实测 exit=1 的 ESM loader 报错）。
    `frontend/.tmp/` 命中 .gitignore 的 `.tmp/` 规则，不会污染 git 状态。
    """
    return frontend / ".tmp" / "vue-guard"


def parse_request(*, component: str, props: str = "", clicks: str = "",
                  expect_present: str = "", expect_absent: str = "", counts: str = "",
                  texts: str = "", expect_emit: str = "",
                  expect_emit_args: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把工具文本报文变成 (config, 点击计划)，非法输入在这里就抛，不起浏览器。"""
    plan = click_plan(clicks)
    args_value = json.loads(expect_emit_args) if str(expect_emit_args or "").strip() else None
    if expect_emit_args and not isinstance(args_value, list):
        raise VueComponentError("expect_emit_args 必须是 JSON 数组。")
    config = {"props": parse_props(props),
              "expect_present": selectors(expect_present),
              "expect_absent": selectors(expect_absent),
              "counts": count_plan(counts),
              "texts": text_needles(texts),
              "expect_emit": str(expect_emit or ""),
              "expect_emit_args": args_value, "root": "#app > *"}
    return config, plan


def frontend_dir(root: str | Path) -> Path:
    base = Path(root).resolve()
    for candidate in (base / "frontend", base):
        if (candidate / "package.json").is_file() and (candidate / "src").is_dir():
            return candidate
    raise VueComponentError("找不到前端目录（需要 package.json + src/）。")


def component_file(frontend: Path, rel: str) -> Path:
    """只接受前端树内的 .vue，且必须在磁盘上真的存在。

    除了 `src/**`，还允许 `frontend/.tmp/**`：那是 gitignore 的位置，用来指向一份
    【故意改坏的组件副本】做反证——守卫必须能红，而在真组件上原地做变异会撞别人
    正在编辑的文件（本会话为这条踩过两次）。除这两处之外一律拒绝。
    """
    text = str(rel or "").strip().replace("\\", "/")
    if not text.endswith(".vue") or text.startswith("/") or ".." in text.split("/"):
        raise VueComponentError("组件路径必须是前端目录内的 .vue 相对路径，收到：%s" % text[:120])
    parts = text.split("/")
    if parts[0] == "frontend":
        parts = parts[1:]
    if not parts or (parts[0] != "src" and parts[0] != ".tmp"):
        raise VueComponentError("组件路径要相对于前端目录（src/... 或 .tmp/...），收到：%s"
                                % text[:120])
    path = frontend.joinpath(*parts)
    if not path.is_file():
        raise VueComponentError("组件文件不存在：%s" % text)
    if path.stat().st_size > MAX_COMPONENT_BYTES:
        raise VueComponentError("组件太大（%d 字节），不敢整份编进 bundle。" % path.stat().st_size)
    return path


def selectors(values: str, *, limit: int = MAX_SELECTORS, label: str = "选择器") -> list[str]:
    out: list[str] = []
    for item in re.split(r"[,\n]", str(values or "")):
        clean = item.strip()
        if not clean:
            continue
        if not SELECTOR_RE.match(clean):
            raise VueComponentError("%s形状不被接受：%s" % (label, clean[:120]))
        if clean not in out:
            out.append(clean)
    if len(out) > limit:
        raise VueComponentError("%s最多 %d 条，收到 %d 条。" % (label, limit, len(out)))
    return out


def count_plan(values: str, *, limit: int = MAX_SELECTORS) -> list[list[Any]]:
    """.wmd-item=2 → [[".wmd-item", "2"]]：选择器与期望值必须拆开传给页面，
    整串当选择器会让 querySelectorAll 抛错、把整份事实吞成空 dict（实测）。"""
    out: list[list[Any]] = []
    for item in re.split(r"[,\n]", str(values or "")):
        clean = item.strip()
        if not clean:
            continue
        sel, sep, want = clean.rpartition("=")
        if not sep or not sel or not want.strip().isdigit():
            raise VueComponentError("数量断言要写成 `.选择器=数字`，收到：%s" % clean[:120])
        selectors(sel)                      # 形状与上限都走同一道校验
        row = [sel, want.strip()]
        if row not in out:
            out.append(row)
    if len(out) > limit:
        raise VueComponentError("数量断言最多 %d 条，收到 %d 条。" % (limit, len(out)))
    return out


def text_needles(values: str, *, limit: int = MAX_SELECTORS) -> list[str]:
    """文本断言不是 CSS 选择器：`1/2`、`工作流团队` 这类都要能断言，
    所以只限长度/条数/换行，不过选择器形状那一关。"""
    out: list[str] = []
    for item in str(values or "").splitlines():
        clean = item.strip()
        if not clean:
            continue
        if len(clean) > 120:
            raise VueComponentError("文本断言太长（>120 字符）：%s" % clean[:60])
        if clean not in out:
            out.append(clean)
    if len(out) > limit:
        raise VueComponentError("文本断言最多 %d 条，收到 %d 条。" % (limit, len(out)))
    return out


def click_plan(values: str) -> list[dict[str, Any]]:
    """`.wmd-item#1` 形式的点击清单：`#<序号>` 是 nth。

    没写 `#n` 时 nth 是【未指定】而不是 0——命中多个就让 page_action 拒绝执行并回候选清单，
    静默点第一个会把守卫指到错误的元素上（以前这里写死默认 0，正好和 `nth: 0` 撞在一起）。
    """
    plan: list[dict[str, Any]] = []
    for item in re.split(r"[,\n]", str(values or "")):
        clean = item.strip()
        if not clean:
            continue
        nth: Any = ""
        match = re.search(r"#(\d+)$", clean)
        if match:
            nth = int(match.group(1))
            clean = clean[: match.start()]
        if not SELECTOR_RE.match(clean):
            raise VueComponentError("点击目标形状不被接受：%s" % clean[:120])
        plan.append({"selector": clean, "nth": nth})
    if len(plan) > MAX_CLICKS:
        raise VueComponentError("点击最多 %d 次，收到 %d 次。" % (MAX_CLICKS, len(plan)))
    return plan


def parse_props(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise VueComponentError("props 必须是 JSON 对象：%s" % str(exc)[:160]) from exc
    if not isinstance(data, dict):
        raise VueComponentError("props 必须是 JSON 对象（键值对），收到 %s。" % type(data).__name__)
    return data


def compile_component(frontend: Path, component: Path, target: Path, *,
                      script: str = COMPILE_SCRIPT,
                      runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """生成入口 + 编译脚本，用前端自带的 esbuild JS API 打成一张浏览器可用的图。"""
    if not frontend.joinpath(*ESBUILD_API).is_file():
        return {"ok": False, "error": "前端没有 esbuild（先在该目录 npm ci）。"}
    if not frontend.joinpath(*SFC_API).is_file():
        return {"ok": False, "error": "前端没有 @vue/compiler-sfc（vue-tsc 带的，先 npm ci）。"}
    target.mkdir(parents=True, exist_ok=True)
    rel = component.relative_to(frontend).as_posix()
    entry = target / "entry.mjs"
    entry.write_text(
        "import Comp, { __vueStyles__ } from '%s'\n"
        "import { createApp, h } from 'vue'\n"
        "export { Comp, __vueStyles__, createApp, h }\n"
        % posix_relative(target, component),
        encoding="utf-8", newline="\n")
    runner_file = target / "compile.mjs"
    # createRequire 要一个【绝对路径】当解析锚点，用前端自己的 package.json，
    # 这样脚本本身放在仓外的临时目录里也能 require 到前端的 esbuild / compiler-sfc。
    runner_file.write_text(script % {"require_anchor": json.dumps(
        (frontend / "package.json").as_posix())}, encoding="utf-8", newline="\n")
    bundle = target / "app.mjs"
    with contextlib.suppress(FileNotFoundError):
        bundle.unlink()
    node = shutil.which("node") or ""
    if not node:
        return {"ok": False, "error": "找不到 node，无法编译 .vue。"}
    try:
        proc = runner([node, runner_file.as_posix(), entry.as_posix(), bundle.as_posix()],
                      cwd=str(frontend), capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "编译 .vue 超时（180s）。"}
    line = next((l for l in reversed((proc.stdout or "").splitlines()) if l.strip().startswith("{")),
                "")
    if not line:
        return {"ok": False, "error": "编译器没回话（exit=%r）：%s" % (
            proc.returncode, ((proc.stderr or proc.stdout or "").strip()[-300:]))}
    try:
        data = json.loads(line)
    except ValueError:
        return {"ok": False, "error": "编译器的回话不是 JSON：%s" % line[:240]}
    if not data.get("ok"):
        return {"ok": False, "error": data.get("error") or "编译失败（无更多细节）",
                "stderr": (proc.stderr or "")[-300:]}
    if not bundle.is_file() or bundle.stat().st_size < 1000:
        return {"ok": False, "error": "编译说成功了，但产物不在或太小：%s" % bundle}
    return {"ok": True, "bundle": bundle.name, "bytes": bundle.stat().st_size,
            "warnings": data.get("warnings") or [], "component": rel}


def posix_relative(target: Path, component: Path) -> str:
    """esbuild 的 import 说明符要相对路径、正斜杠（Windows 上 Path 会给反斜杠）。"""
    import os.path as _path
    return Path(_path.relpath(str(component), str(target))).as_posix()


def mount_page(bundle: str, config: dict[str, Any], path: Path, title: str = "组件守卫页") -> Path:
    path.write_text(MOUNT_TEMPLATE % {
        "title": title, "bundle": bundle,
        "config": json.dumps(config, ensure_ascii=False)}, encoding="utf-8", newline="\n")
    return path


def evaluate(state: dict[str, Any], config: dict[str, Any], clicks: list[dict[str, Any]],
             click_results: list[dict[str, Any]], *, synced: bool = True) -> dict[str, Any]:
    """把"页面回来的事实"与"我要求的事实"逐条对上，任何一条对不上都不算通过。"""
    declared = [str(item) for item in (state.get("declaredEmits") or [])]
    emit_names = [str(row.get("name")) for row in (state.get("emits") or [])]
    want_present = list(config.get("expect_present") or [])
    want_absent = list(config.get("expect_absent") or [])
    want_counts = [list(row) for row in (config.get("counts") or [])]
    want_texts = list(config.get("texts") or [])
    expect_emit = str(config.get("expect_emit") or "")
    expect_args = config.get("expect_emit_args")
    facts = state.get("facts") or {}
    assertions = {
        "mounted": bool(state.get("mounted")) and not (state.get("errors") or []),
        "rendered": bool(state.get("rendered")),
        # 读状态那一次点击必须成功：它抛错时 facts 是空的，每条断言都只是"没成立"，
        # 看不出真正原因——所以这一条必须单独红出来。
        "state_read_back": bool(synced) and bool(facts),
        "no_component_errors": not (state.get("errors") or []),
        "no_vue_warnings": not (state.get("warnings") or []),
        # sync 那一次点击不算进"要求的点击"，它只是读状态的手段
        "every_click_applied": all(bool(row.get("ok")) for row in click_results[:len(clicks)])
                               if clicks else True,
    }
    for sel in want_present:
        assertions["present:%s" % sel] = facts.get("present:" + sel) is True
    for sel in want_absent:
        assertions["absent:%s" % sel] = facts.get("absent:" + sel) is True
    for sel, want in want_counts:
        got = facts.get("count:" + str(sel))
        assertions["count:%s=%s" % (sel, want)] = got == int(want)
    for needle in want_texts:
        assertions["text:%s" % needle] = facts.get("text:" + needle) is True
    if expect_emit:
        assertions["emit_declared_by_component"] = expect_emit in declared
        assertions["emit_recorded"] = expect_emit in emit_names
        if expect_args is not None:
            hit = next((row for row in (state.get("emits") or [])
                        if str(row.get("name")) == expect_emit), None)
            assertions["emit_args_exact"] = bool(hit) and hit.get("args") == list(expect_args)
    asked = bool(want_present or want_absent or want_counts or want_texts or expect_emit)
    assertions["assertions_declared"] = asked
    failed = [name for name, ok in assertions.items() if not ok]
    return {"assertions": assertions, "failed": failed, "passed": not failed,
            "declared_emits": declared, "observed_emits": emit_names,
            "emit_names_seen": emit_names, "facts": facts,
            "warnings": (state.get("warnings") or [])[:5],
            "errors": (state.get("errors") or [])[:5]}


def run_guard(root: str | Path, *, component: str, props: str = "", clicks: str = "",
              expect_present: str = "", expect_absent: str = "", counts: str = "",
              texts: str = "", expect_emit: str = "", expect_emit_args: str = "",
              timeout: float = 60.0) -> dict[str, Any]:
    """编译 → 起服务 → 真点击 → 逐条判定 → 收回。"""
    base = Path(root).resolve()
    started = time.monotonic()
    try:
        frontend = frontend_dir(base)
    except VueComponentError as exc:
        return {"ok": False, "passed": False, "checks": {}, "error": str(exc)}
    # 产物目录必须落在【前端树内】：入口里 `import { createApp } from 'vue'` 是裸说明符，
    # esbuild 与 node 都从入口文件所在目录向上找 node_modules——放在仓外的临时目录里
    # 就解析不到（实测 exit=1 的 ESM loader 报错）。`frontend/.tmp/` 命中 .gitignore 的
    # `.tmp/` 规则，不会污染 git 状态，用完整目录删掉。
    workdir = workdir_for(frontend)
    try:
        target = Path(workdir) / ("guard-%d" % int(time.time()))
        comp = component_file(frontend, component)
        config, plan = parse_request(component=component, props=props, clicks=clicks,
                                     expect_present=expect_present, expect_absent=expect_absent,
                                     counts=counts, texts=texts, expect_emit=expect_emit,
                                     expect_emit_args=expect_emit_args)
    except (VueComponentError, ValueError) as exc:
        return {"ok": False, "passed": False, "checks": {}, "error": str(exc)}
    built = compile_component(frontend, comp, target)
    if not built["ok"]:
        return {"ok": False, "passed": False, "checks": {"compiled": False},
                "error": built["error"], "component": str(comp)}
    page = mount_page(built["bundle"], config, target / "guard.html")
    server = thread = session = None
    page_id = ""
    click_results: list[dict[str, Any]] = []
    console_errors: list[Any] = []
    failed_requests: list[Any] = []
    synced = False           # 必须在 try 之前初始化：中途抛错时下面还要读它
    cleaned: dict[str, Any] = {"removed": False, "error": "没走到收回"}
    state: dict[str, Any] = {}
    start_error = ""
    try:
        server, thread = visual.serve_static(target)
        url = "http://127.0.0.1:%d/%s" % (server.server_port, page.name)
        session = page_action.open_page(base, url=url, timeout=timeout)
        page_id = session["page"]
        console_errors += list(session.get("console_errors") or [])
        failed_requests += list(session.get("failed_requests") or [])
        for item in plan:
            outcome = page_action.click(base, page=page_id, selector=item["selector"],
                                        nth=item["nth"], screenshot=False)
            click_results.append(outcome)
            console_errors += list(outcome.get("console_errors") or [])
            failed_requests += list(outcome.get("failed_requests") or [])
        sync = page_action.click(base, page=page_id, selector="#sync", screenshot=False)
        # 读状态那一次点击单独记：它抛错时 facts 会是空 dict，
        # 让每条事实断言都"没成立"却看不出原因，是最坏的一种红。
        synced = bool(sync.get("ok")) and not (sync.get("console_errors") or [])
        console_errors += list(sync.get("console_errors") or [])
        failed_requests += list(sync.get("failed_requests") or [])
        read = page_action.read_page(base, page=page_id, selector="#state", mode="text")
        try:
            state = json.loads(str(read.get("text") or "{}"))
        except ValueError:
            start_error = "读不回组件状态：%s" % str(read.get("text"))[:160]
    except (page_action.PageActionError, visual.VisualAcceptanceError) as exc:
        start_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 挂不起来要报原因，不能报通过
        start_error = "组件守卫异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
    finally:
        if page_id:
            with contextlib.suppress(Exception):
                page_action.close_page(page_id)
        visual.stop_static(server, thread)
        cleaned = clean_target(target)
    if start_error:
        return {"ok": False, "passed": False, "checks": {"compiled": True, "mounted": False},
                "error": start_error, "component": str(comp)}
    judged = evaluate(state, config, plan, click_results, synced=synced)
    # 产物没收回干净也算未通过：孤儿目录堆起来就是这台机器上次 99°C 的那类债
    checks = {"compiled": True, "artifacts_cleaned": bool(cleaned.get("removed")),
              **judged["assertions"]}
    return {"ok": judged["passed"], "passed": judged["passed"], "checks": checks,
            "component": built["component"], "bundle_bytes": built["bytes"],
            "compile_warnings": built["warnings"], "assertions": judged["assertions"],
            "failed": judged["failed"], "declared_emits": judged["declared_emits"],
            "observed_emits": judged["observed_emits"], "facts": judged["facts"],
            "vue_warnings": judged["warnings"], "vue_errors": judged["errors"],
            "clicks": [{"selector": row["selector"], "nth": row["nth"],
                        "effect": (outcome.get("effect") or "?")}
                       for row, outcome in zip(plan, click_results)],
            "console_errors": console_errors[:5], "failed_requests": failed_requests[:5],
            "cleaned": cleaned, "artifacts_dir": str(target),
            "elapsed": round(time.monotonic() - started, 2),
            "error": "" if cleaned.get("removed") else cleaned.get("error", "")}


def clean_target(target: Path, *, attempts: int = 4,
                 remover: Callable[[Path], None] = shutil.rmtree,
                 pause: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """删掉本次的临时产物目录，并【如实回报有没有删干净】。

    `shutil.rmtree(..., ignore_errors=True)` 会把失败吞掉：浏览器刚关掉时 `app.mjs` 还可能
    被句柄占着，一次删不掉就再没人知道（实测就是这样留下了孤儿目录）。删不干净要红出来。
    `remover`/`pause` 可注入是为了能测"删不干净"这一路——真句柄占用没法稳定复现。
    """
    error = ""
    for attempt in range(attempts):
        if not target.exists():
            return {"removed": True, "attempts": attempt, "error": ""}
        try:
            remover(target)
        except OSError as exc:
            error = str(exc)[:180]
            pause(0.25)
    return {"removed": not target.exists(), "attempts": attempts,
            "error": "" if not target.exists() else "产物目录没删干净：%s（%s）" % (target, error)}


def render(result: dict[str, Any]) -> str:
    if not result.get("checks"):
        return "组件守卫未能开始：%s" % result.get("error", "未知原因")
    if not result["checks"].get("compiled"):
        return "组件编译失败：%s" % result.get("error", "未知原因")
    head = "组件守卫【%s】：%s（bundle %s 字节，%s 秒）" % (
        "通过" if result.get("ok") else "未通过", result.get("component"),
        result.get("bundle_bytes"), result.get("elapsed"))
    lines = [head]
    for name in (result.get("failed") or []):
        lines.append("未通过项：%s" % name)
    for item in (result.get("vue_errors") or [])[:3]:
        lines.append("组件抛错：%s" % str(item)[:180])
    for item in (result.get("vue_warnings") or [])[:3]:
        lines.append("Vue 警告：%s" % str(item)[:180])
    for row in (result.get("clicks") or []):
        lines.append("点击：%s[#%s] → %s" % (row["selector"], row["nth"], row["effect"]))
    if result.get("declared_emits"):
        lines.append("组件声明的 emits：%s；这次收到的：%s" % (
            ", ".join(result["declared_emits"]) or "无",
            json.dumps(result.get("observed_emits") or [], ensure_ascii=False)))
    if result.get("facts"):
        lines.append("页面事实：%s" % json.dumps(result["facts"], ensure_ascii=False)[:400])
    if result.get("console_errors"):
        lines.append("console 错误：%s" % json.dumps(
            [str(row.get("text"))[:120] for row in result["console_errors"][:3]],
            ensure_ascii=False))
    if not (result.get("cleaned") or {}).get("removed"):
        lines.append("注意：临时产物目录没收回干净（%s）" % (result.get("artifacts_dir") or "?"))
    if not result.get("ok"):
        lines.append("为什么不可信：%s" % (result.get("error") or "有断言未成立"))
    lines.append("边界：挂的是【真实的 .vue】（项目自带的 compiler-sfc 编的），不是转写的胶水；"
                 "但它只证明这一个组件在这组 props 下的接线行为，不证明整个页面的集成，"
                 "也不听真网关。")
    return "\n".join(lines)


__all__ = ["VueComponentError", "clean_target", "click_plan", "compile_component",
           "component_file",
           "count_plan", "evaluate", "frontend_dir", "mount_page", "parse_props",
           "posix_relative", "render", "run_guard", "selectors", "text_needles"]
