"""`dev_headcheck` 的变异电池。

两条是电池跑出来才暴露的**测试自己没牙**：① 拒绝坏 `ref` 只断言"被拒了"——git 自己就拒，
删掉形状校验照样绿，于是改成断言**拒绝理由**；② 删 `--detach` 是**等价变异**（非分支名
本来就会 detach），真正的风险是 `-b <name>` 在共享仓里凭空占分支名，于是补了
"分支表 / HEAD / 工作树 / `git worktree list` 四项都不许变"。
"""

BATTERY_NAME = 'head_check'
MODULE = 'agent_runtime.head_check'
TARGET = 'agent_runtime/head_check.py'
CASE_ID = 'headcheck-catches-untracked-reference'
TEST_MODULE = 'tests.test_head_check'
BIND = 'hchk'
UNIT_CLASSES = (
        'RefTests',
    'GitAllowlistTests',
    'ImporterAttributionTests',
    'CheckOutcomeTests',
    'CleanCheckoutTests',
    'VerdictShapeTests',
    'OwnershipGuardTests',)
LIVE = ()

MUTATIONS = [
    ("空模块列表也算通过（`all([])` 是 True）",
     '"every_entry_imported": bool(results) and len(ok_imports) == len(results),',
     '"every_entry_imported": len(ok_imports) == len(results),'),
    ("truthy 字符串当成功",
     'return {"ok": row.get("ok") is True,',
     'return {"ok": bool(row.get("ok")),'),
    ("ref 不做形状校验（git 选项能进来）",
     'if len(text) > MAX_REF_LENGTH or not _REF_RE.match(text) or ".." in text:',
     'if False:'),
    ("建检出改成占分支名（-b）",
     '["worktree", "add", "--detach", str(target), sha]',
     '["worktree", "add", "-b", "headcheck-probe", str(target), sha]'),
    ("没有归属标记也照删（会拆别人的检出）",
     'if not marker.is_file():',
     'if False:'),
    ("目标已存在也照样 add",
     'if target.exists():',
     'if False:'),
    ("git 白名单放开",
     'GIT_ALLOWED = frozenset({"rev-parse", "worktree", "ls-files", "ls-tree", "status"})',
     'GIT_ALLOWED = frozenset({"rev-parse", "worktree", "ls-files", "ls-tree", "status", '
     '"commit", "reset", "checkout", "push", "add"})'),
    ("检出不收回（临时目录越堆越多）",
     'cleanup = remove_worktree(repo, target, timeout=int(timeout))',
     'cleanup = {"ok": True, "removed": True, "error": ""}'),
    ("磁盘上有就算在提交里（归因失效）",
     'on_disk = (Path(root) / relative).is_file() or (Path(root) / package).is_file()',
     'on_disk = (Path(root) / relative).is_file() or (Path(root) / package).is_file()\n'
     '    in_commit = in_commit or on_disk'),
    ("引用点不做仓内相对化",
     'row["importer"] = _relative_importer(row["importer"], target, repo)',
     'row["importer"] = row["importer"]'),
    ("traceback 帧体收 2 格缩进（把 importlib 帧当引用点）",
     "r'(?P<body>(?: {4}\\S[^\\n]*\\n?)*)')",
     "r'(?P<body>(?:[ \\t]+\\S[^\\n]*\\n?)*)')"),
    ("缺模块不再归因（只回一句原始报错）",
     'if missing:\n                    row.update(classify(',
     'if False:\n                    row.update(classify('),
    ("render 不说边界",
     'lines.append("边界：这是【能不能跑起来】的检查',
     'lines.append("备注：这是【能不能跑起来】的检查'),
    ("收回失败也不吭声",
     'if not (result.get("cleanup") or {}).get("removed"):',
     'if False:'),
]
