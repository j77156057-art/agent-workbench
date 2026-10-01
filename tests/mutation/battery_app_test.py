"""`dev_apptest` 整页守卫的变异电池（三层对照组）。

两条只有真起服务才看得见（"就绪探测说没起来也当起来了"、"服务不回收"），标了 `("live",)`；
其余围栏（回环 / 站内路径 / 接口白名单）、判定表、构建新鲜度、隔离判据都能在纯层与门禁层抓。
"""

BATTERY_NAME = 'app_test'
MODULE = 'agent_runtime.app_test'
TARGET = 'agent_runtime/app_test.py'
CASE_ID = 'apptest-refuses-a-stale-build-as-current'
TEST_MODULE = 'tests.test_app_test'
BIND = 'at'
UNIT_CLASSES = (
        'ArgShapeTests',
    'SandboxTests',
    'BundleFreshnessTests',
    'ActivationTests',
    'ProbeTests',
    'EvaluateTableTests',
    'RenderTests',
    'PreflightTests',)
LIVE = (
        'LivePageTests',
    (
                'test_nothing_is_left_behind',
        'test_a_backend_that_cannot_start_is_red_not_silent',    ),)

MUTATIONS = [
    # ---- 判定表：每一条都对应一类"看起来绿其实没测" ----
    ("后端没应答也算起来了",
     '        "backend_started": bool(observed.get("ready")),',
     '        "backend_started": True,'),
    ("状态根没隔离也算过（会往仓库真登记表上写）",
     '        "state_isolated": bool(observed.get("isolated")),',
     '        "state_isolated": True,'),
    ("项目没激活也算过",
     '        "project_activated": bool(observed.get("activated")),',
     '        "project_activated": True,'),
    ("旧构建上的绿灯也当没问题",
     '        "bundle_built_after_latest_source": bool(fresh.get("built"))'
     ' and bool(fresh.get("fresh")),',
     '        "bundle_built_after_latest_source": bool(fresh.get("built")),'),
    ("外壳没挂上也算过",
     '        "shell_mounted": int(observed.get("shell") or 0) >= 1,',
     '        "shell_mounted": True,'),
    ("标签少了一个也照样绿",
     '        "nav_renders_every_tab": int(observed.get("tabs") or 0)'
     ' == int(wanted.get("tabs") or 0),',
     '        "nav_renders_every_tab": int(observed.get("tabs") or 0) >= 1,'),
    ("后端数据到没到页面都不查（只看文件树有没有行）",
     '                                     and observed.get("fixture_in_tree") is True,',
     '                                     and True,'),
    ("console 错误被忽略",
     '        "console_clean": not (observed.get("console_errors") or []),',
     '        "console_clean": True,'),
    ("失败请求被忽略",
     '        "no_failed_requests": not (observed.get("failed_requests") or []),',
     '        "no_failed_requests": True,'),
    ("点击没生效也算点了",
     '        "nav_click_applied": bool(observed.get("click_ok")),',
     '        "nav_click_applied": True,'),
    ("活动标签本来就是这样：点了没换视图也判通过",
     '                                  and str(observed.get("active_before") or "")\n'
     '                                  != str(observed.get("active_after") or "")),',
     '                                  and True),'),
    ("点完变空正文也算「换了内容」",
     '            and str(observed.get("content_after") or "") != _digest("")),',
     '            and True),'),
    ("正文被截断也敢说「这句话没有」",
     '        "page_text_read_wholly": (not needles) or not observed.get("body_truncated"),',
     '        "page_text_read_wholly": True,'),
    ("文本断言把「没读到」当「读到了」",
     '        "extra_texts_rendered": all(texts.get(needle) is True for needle in needles),',
     '        "extra_texts_rendered": all(texts.get(needle) is not False'
     ' for needle in needles),'),
    ("现场没收回也算干净",
     '        "artifacts_cleaned": bool(cleanup.get("removed"))'
     ' and bool(cleanup.get("job_stopped"))',
     '        "artifacts_cleaned": bool(cleanup.get("removed")) and True'),
    # ---- 围栏与前置检查 ----
    ("页面路径不做围栏（能拼出协议/查询/空白）",
     '    if not text.startswith("/") or text.startswith("//"):',
     '    if False:'),
    ("页面路径允许查询串与片段（能把页面指向别处）",
     '    if ".." in text or any(ch in text for ch in "?#\\"\'<>\\\\ \\t\\n"):',
     '    if False:'),
    ("文本断言不过长度与条数上限",
     '        if len(clean) > 120:',
     '        if False:'),
    ("端口范围不校验",
     '    if not 1024 <= int(port) <= 65535:',
     '    if False:'),
    ("子进程 env 只带状态根一个键（连 PATH 都没有）",
     '    return {**os.environ, "DOCMIND_STATE_ROOT": str(state_root)}',
     '    return {"DOCMIND_STATE_ROOT": str(state_root)}'),
    ("接口白名单形同虚设（能打到任意 /api）",
     '    if parsed.path not in _API_PATHS:',
     '    if False:'),
    ("回环限制被去掉（可以拿去访问内网任意机器）",
     '    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):',
     '    if False:'),
    ("注册接口回 200 就信（不核对 current 与清单）",
     '    return {"ok": bool(project_id) and status == 200 and isinstance(posted, dict)\n'
     '               and current == project_id and ids == [project_id],',
     '    return {"ok": status == 200,'),
    ("登记表里混着别的项目也当隔离成功",
     '                and len(verdict.get("listed") or []) == 1)',
     '                and True)'),
    ("临时状态根什么都没写出来也算隔离",
     '    wrote_something = state_root.is_dir() and any(state_root.iterdir())',
     '    wrote_something = True'),
    ("node_modules 被算进源码（新鲜度这条会永远红）",
     '        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".tmp",'
     ' "__pycache__")]',
     '        pass'),
    ("源码比产物新也说产物是新的",
     '    return {"built": True, "fresh": newest_source <= newest_build,',
     '    return {"built": True, "fresh": True,'),
    ("没有构建产物也继续跑（页面是空的）",
     '    if not (build / "index.html").is_file():',
     '    if False:'),
    ("api.py 不存在也照样往下起进程",
     '    if not (base / "api.py").is_file():',
     '    if False:'),
    ("就绪探测说没起来也当起来了",
     '    state["ready"] = bool(server.get("ready"))',
     '    state["ready"] = True', ("live",)),
    ("服务不回收（job 泄漏在这里）",
     '            stopped = dev_server.stop(str(state["job_id"]))',
     '            stopped = {"ok": True}', ("live",)),
    ("夹具文件与上一次运行共用一个名字（缓存能冒充通过）",
     '    fixture = "probe_%s.py" % uuid.uuid4().hex[:6]',
     '    fixture = "probe_fixed.py"'),
    ("沙箱目录名不含随机量（两次运行互相踩）",
     '    sandbox = root / ("docmind-apptest-%d-%s" % (int(time.time()), uuid.uuid4().hex[:6]))',
     '    sandbox = root / "docmind-apptest"'),
    ("渲染器把红灯也报成通过",
     '        "通过" if result.get("ok") else "未通过", result.get("url"), result.get("elapsed"))',
     '        "通过", result.get("url"), result.get("elapsed"))'),
    ("没开始也照样渲染出一张表（把「未能开始」藏掉）",
     '    if not result.get("checks"):',
     '    if False:'),
]
