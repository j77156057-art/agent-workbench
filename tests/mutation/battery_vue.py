"""`dev_vue` 组件守卫的变异电池（含 Wave17a 的共享状态第二层）。

第二层里几条（`store_call` 取到的不是函数、setup 调的是异步方法、读状态改成只靠点击、
页面 import 了 STORE 却没人导出）**只有真浏览器才看得见**：它们标了 `("live",)`，
纯层与门禁都抓不住时交给现场层。本环境没有浏览器或前端工具链时现场层跑不动，
引擎把它们记成「本环境未验证」而不是"抓住"——这是两件事。
"""

BATTERY_NAME = 'vue'
MODULE = 'agent_runtime.vue_component'
TARGET = 'agent_runtime/vue_component.py'
CASE_ID = 'vue-guard-can-go-red'
TEST_MODULE = 'tests.test_vue_component'
BIND = 'vc'
UNIT_CLASSES = (
        'PathGuardTests',
    'ArgShapeTests',
    'RequestParsingTests',
    'MountPageTests',
    'EvaluateTableTests',
    'RenderTests',
    'CompileArtifactTests',
    'ArtifactLocationTests',
    'EntrySourceTests',)
LIVE = (
        'LiveComponentTests',
    (
                'test_shared_state_can_be_driven_before_mount',
        'test_a_store_call_that_is_not_a_function_is_red',
        'test_a_setup_call_that_returns_a_promise_is_red',
        'test_a_setup_name_the_store_does_not_have_is_red_not_silent',    ),)

#: 故意不作为电池的层：它们要么慢得离谱、要么必须 import 整个工具注册面，
#: 与「这条守卫会不会红」无关。写在这里必须说清原因（否则就该进 UNIT_CLASSES）。
LAYER_EXEMPT = {'ToolSurfaceTests': '要 import tools 全量注册面；与「这条守卫会不会红」无关'}

#: 本块电池的变异条数下限：删一条变异必须显式改这里，「守卫悄悄变少」不该是绿灯
MINIMUM_MUTATIONS = 37

MUTATIONS = [
    ("只挂不断言也算通过",
     '    assertions["assertions_declared"] = asked',
     '    assertions["assertions_declared"] = True'),
    ("emit 只看名字不看实参",
     'assertions["emit_args_exact"] = bool(hit) and hit.get("args") == list(expect_args)',
     'assertions["emit_args_exact"] = bool(hit)'),
    ("不再核对 emit 是否由组件声明",
     'assertions["emit_declared_by_component"] = expect_emit in declared',
     'assertions["emit_declared_by_component"] = True'),
    ("读状态失败不再单独红（facts 空一片假红）",
     '        "state_read_back": bool(synced),',
     '        "state_read_back": True,'),
    ("Vue 警告被忽略",
     '"no_vue_warnings": not (state.get("warnings") or []),',
     '"no_vue_warnings": True,'),
    ("组件抛错被忽略",
     '"no_component_errors": not (state.get("errors") or []),',
     '"no_component_errors": True,'),
    ("没渲染东西也算挂上了（把报出来的事实捏成真的）",
     '    assertions["rendered"] = bool(state.get("rendered"))',
     '    assertions["rendered"] = True'),
    ("点击没生效也算点了",
     'all(bool(row.get("ok")) for row in click_results[:len(clicks)])',
     'True'),
    ("组件路径不做围栏（能编仓外任意文件）",
     '    if not text.endswith(suffixes) or text.startswith("/")'
     ' or ".." in text.split("/"):',
     '    if False:'),
    ("围栏放行前端树外的顶层目录（composable 共用这一道）",
     '    if not parts or (parts[0] != "src" and parts[0] != ".tmp"):',
     '    if False:'),
    ("共享状态模块只认 .vue（.ts composable 被拒，第二层永远用不上）",
     'suffixes=(".ts", ".js", ".mjs")',
     'suffixes=(".vue",)'),
    ("选择器允许以 - 开头（会被当选项）",
     'SELECTOR_RE = re.compile(r"^[.#\\[*]?[A-Za-z0-9_*][A-Za-z0-9_.:#\\[\\]*=\\"\'-]{0,119}$")',
     'SELECTOR_RE = re.compile(r"^[.#\\[*-]?[A-Za-z0-9_*-][A-Za-z0-9_.:#\\[\\]*=\\"\'-]{0,119}$")'),
    ("nth 缺省回到 0（与显式 nth:0 再也分不开）",
     '        nth: Any = ""\n        match = re.search(r"#(\\d+)$", clean)',
     '        nth: Any = 0\n        match = re.search(r"#(\\d+)$", clean)'),
    ("数量断言不校验期望值",
     '        if not sep or not sel or not want.strip().isdigit():',
     '        if False:'),
    ("文本断言被当 CSS 选择器校验（合法断言被拒）",
     '              "texts": text_needles(texts),',
     '              "texts": selectors(texts),'),
    ("监听用写死的事件名，不看组件声明",
     "for (const name of (Comp.emits || [])) {",
     "for (const name of ['focus']) {"),
    ("非法选择器不再被记成页面错误",
     "      state.errors.push('bad selector ' + sel);",
     "      // 静默吞掉"),
    ("非法选择器把整份事实吞成空 dict",
     "      return { any: false, n: -1 };",
     "      return { any: true, n: 1 };"),
    ("编译说成功就信，不看产物",
     'if not bundle.is_file() or bundle.stat().st_size < 1000:',
     'if False:'),
    ("loader 写死 js（TS 组件编不出来）",
     "return { loader: /ts|tsx/.test(lang) ? 'ts' : 'js',",
     "return { loader: 'js',"),
    ("不打 process.env.NODE_ENV define",
     "define: { 'process.env.NODE_ENV': '\"development\"', '__VUE_OPTIONS_API__': 'true',",
     "define: { '__VUE_OPTIONS_API__': 'true',"),
    ("createRequire 锚点不指向前端目录",
     '        (frontend / "package.json").as_posix())}, encoding="utf-8", newline="\\n")',
     '        str(Path.cwd() / "package.json"))}, encoding="utf-8", newline="\\n")'),
    ("收回用 ignore_errors 一次删（孤儿目录没人知道）",
     '    error = ""\n    for attempt in range(attempts):',
     '    import shutil as _s\n    _s.rmtree(target, ignore_errors=True)\n'
     '    return {"removed": True, "attempts": 0, "error": ""}\n'
     '    error = ""\n    for attempt in range(0):'),
    ("产物目录写到仓外（裸说明符解析不到，且污染状态目录）",
     '    return frontend / ".tmp" / "vue-guard"',
     '    import tempfile\n    return Path(tempfile.gettempdir()) / "vue-guard-outside"'),
    # ---- 第二层（mount 包装：挂载前驱动共享状态）----
    ("要求了 setup 调用，却没跑成也算通过",
     '        "setup_applied": (not want_setup) or (',
     '        "setup_applied": True or ('),
    ("setup 抛错被吞掉（只数跑了几条）",
     '            and not [err for err in (state.get("errors") or [])'
     ' if str(err).startswith("setup")]),',
     '            and True),'),
    ("store_call 不再核对是不是模块导出的名字",
     '    if store_call and store_call not in available:',
     '    if False:'),
    ("导出扫不出来时返回空清单（等于把所有 setup 名字判成不存在）",
     '    if not out:\n        # 扫不出任何导出却返回空清单',
     '    if False:\n        # 扫不出任何导出'),
    ("setup 里的函数名不做形状校验（能塞进任意表达式）",
     '        if not re.match(r"^[A-Za-z_$][\\w$]{0,63}$", name):',
     '        if False:'),
    ("setup 的静态白名单被绕过（任何名字都放行）",
     '        if name not in available and not loose:',
     '        if False:'),
    ("setup 的实参被丢掉（组件收到 undefined）",
     '        plan.append({"call": name, "args": args})',
     '        plan.append({"call": name, "args": []})'),
    ("读状态改成只靠点击（弹窗一开就读不到真状态）",
     "new MutationObserver(collect).observe(document.getElementById('app'),\n"
     "                                      { childList: true, subtree: true,"
     " attributes: true });",
     "    /* 只有 #sync 被点时才刷新 */", ("live",)),
    ("页面 import 了 STORE 却没人导出它（整页链接期就炸）",
     "export { Comp, __vueStyles__, createApp, h, STORE }",
     "export { Comp, __vueStyles__, createApp, h }"),
    ("没有 composable 时不导出 STORE（页面的 import 清单是固定的）",
     "export const STORE = {}",
     "const STORE_NOT_EXPORTED = {}"),
    ("store_call 取到的不是函数时静默继续",
     "    state.errors.push('store_call 不可调用：' + CFG.store_call);",
     "    api = {};", ("live",)),
    ("setup 调的是异步方法也当同步确认过",
     "      state.errors.push('setup 返回了 Promise，无法同步确认：' + row.call);",
     "      void back;", ("live",)),
    ("setup 名字在共享对象上取不到时静默跳过",
     "  if (typeof fn !== 'function') { state.errors.push('setup 不可调用：' + row.call);"
     " continue; }",
     "  if (typeof fn !== 'function') { continue; }", ("live",)),
]
