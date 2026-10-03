"""`agent_runtime/dev_server.py`（本地 dev server 生命周期 + 端口归属）的变异电池。

这个模块此前**一条测试都没有**，所以 §6 缺口 4 记了两年也没人动。建电池之前先量过一件事：
**真正的洞不是"端口被抢不重试"，而是"把别人的服务当成自己的"**——`dev_serve` 传的 argv
里没有端口参数（`tools.py` 的 `_shell_argv(command)`），`port=` 只是"优先去探这里"，
于是用户给的端口上若已有别人的服务，`probe` 成功、`start()` 就报就绪并把那个 URL 交回去。

电池里最要紧的是把**归属**钉住，而不是把"进程起没起"钉住：后者任何一条都能过，
前者才对应那次假绿。
"""

BATTERY_NAME = 'dev_server'
MODULE = 'agent_runtime.dev_server'
TARGET = 'agent_runtime/dev_server.py'
CASE_ID = 'devserver-refuses-to-claim-someone-elses-port'
TEST_MODULE = 'tests.test_dev_server'
#: **必须**是测试模块里那个模块级名字。写成 "ds" 时，引擎的
#: `setattr(测试模块, "ds", 变异副本)` 只是新建了一个**没人用**的属性，纯层从头到尾看的
#: 是真模块——于是纯层的变异会"活下来"，而抓到的几条全是门禁层干的（门禁题在函数体里
#: `from agent_runtime import dev_server`，拿到的才是 sys.modules 里的变异副本）。
#: 本会话在 Wave26 踩过一次同一个坑，这里写进注释免得再犯。
BIND = 'dev_server'
UNIT_CLASSES = (
    'PortHintTests',
    'OwnershipTests',
    'ArgumentGuardTests',
    'LoopbackGuardTests',)
LIVE = ()

#: 这个模块纯层就能跑（不需要浏览器、不需要网络），所以现场层是空的。
LAYER_EXEMPT = {}

#: 本块电池的变异条数下限：删一条变异必须显式改这里。
MINIMUM_MUTATIONS = 14

MUTATIONS = [
    # ---- 端口归属：那一组对应那次确定性的假绿 ----
    ("端口归属不查了：启动前有人听也照报就绪（把别人的服务当成自己的）",
     '    busy_before = bool(want_port) and port_taken(want_port)',
     '    busy_before = False'),
    ("busy_before 挪到子进程启动之后算：端口上坐的正是我们自己，它永远为 True",
     '    busy_before = bool(want_port) and port_taken(want_port)\n'
     '    if busy_before:\n'
     '        out["notes"].append("端口 %d 在启动前就已经有服务在听；那个服务不是本命令起的。"\n'
     '                            % want_port)\n'
     '    rep = process_runner.start_job(',
     '    rep = process_runner.start_job('),
    ("服务自报的端口也不算归属凭据了",
     '        owned = bool(announced)          # 端口是它自己打印的 ⇒ 归属可证',
     '        owned = False'),
    ("只要端口在应答就报就绪，不管它是谁的",
     '            if ready and not owned and busy_before:',
     '            if False:'),

    # ---- 从日志里认端口：撞端口会往后挪，有多行 ----
    ("端口认第一个而不是最后一个：撞端口后认到的是**没起来那个**地址",
     '    found = _PORT_LINE.findall(str(text or ""))\n'
     '    return int(found[-1]) if found else 0',
     '    found = _PORT_LINE.findall(str(text or ""))\n'
     '    return int(found[0]) if found else 0'),
    ("日志里认不到端口就返回一个硬编码的默认端口",
     '    found = _PORT_LINE.findall(str(text or ""))\n'
     '    return int(found[-1]) if found else 0',
     '    found = _PORT_LINE.findall(str(text or ""))\n'
     '    return int(found[-1]) if found else 5173'),

    # ---- port_taken：把"有人在听"判反 ----
    ("端口占用判断反了：有人听时说没人",
     '    try:\n'
     '        with socket.create_connection((host, int(port)), timeout=PROBE_TIMEOUT):\n'
     '            return True\n'
     '    except OSError:\n'
     '        return False',
     '    try:\n'
     '        with socket.create_connection((host, int(port)), timeout=PROBE_TIMEOUT):\n'
     '            return False\n'
     '    except OSError:\n'
     '        return True'),
    ("任何端口都算被占用",
     '    if not 1024 <= int(port) <= 65535:\n        return False',
     '    if not 1024 <= int(port) <= 65535:\n        return True'),

    # ---- 围栏：不在代码根目录内 / 目录不存在 / 命令为空 ----
    ("代码根目录的围栏失效：可以到外面去启动服务",
     '    if not (workdir == root_abs or workdir.startswith(root_abs + os.sep)):\n'
     '        out["error"] = f"拒绝在代码根目录外启动服务：{cwd}"\n'
     '        return out',
     '    if False:\n'
     '        out["error"] = f"拒绝在代码根目录外启动服务：{cwd}"\n'
     '        return out'),
    # 第一版这条写的是"把 `if not os.path.isdir(workdir)` 去掉"——那是**等价变异**：
    # `process_runner.start_job` 自己也会拒绝不存在的目录，而且文案几乎一样（只是多了
    # 绝对路径）。两层做同一件事，去掉哪一层行为都不变，所以它必然"活下来"。
    # 换成一条真正区分得开的：**报错文案里带上是哪个目录**，让"没说是哪个"也成红项。
    ("目录不存在时不说是哪个目录（只留一句无法定位的话）",
     '        out["error"] = f"工作目录不存在：{cwd or \'.\'}"',
     '        out["error"] = "工作目录不存在"'),
    ("空命令也照启动",
     '    if not argv:\n'
     '        out["error"] = "缺少要执行的命令。"\n'
     '        return out',
     '    if not argv:\n'
     '        out["error"] = ""\n'
     '        return out'),
    ("代码根目录不存在也照启动",
     '    if not root or not os.path.isdir(root):\n'
     '        out["error"] = "代码根目录不存在或未配置。"\n'
     '        return out',
     '    if False:\n'
     '        out["error"] = "代码根目录不存在或未配置。"\n'
     '        return out'),

    # ---- 回环围栏：这个工具不是浏览器，不能被用来访问别的机器 ----
    ("回环围栏失效：允许访问内网/外网地址",
     '    if host not in LOOPBACK and not host.startswith("127."):',
     '    if False:'),
    ("缺端口时不再要求显式给出（默认补一个端口就算合法）",
     '    if port is None:\n'
     '        if not default_port:\n'
     '            return "", "url 缺少端口，例如 http://127.0.0.1:5173/"\n'
     '        port = default_port',
     '    if port is None:\n'
     '        port = default_port or 80'),
    ("非 http/https 协议不再被拒",
     '    if parsed.scheme not in ("http", "https"):',
     '    if False:'),
]
