"""分区开发 2.0（Region-based Development）：在 1.0 的「物理子目录 + 每分区独立 git」之上，
增加三层能力，把「分目录」升级成「受约束的模块化架构」：

1) 分区即配置（regions.json 声明式）：不再写死 4 个分区，可任意扩展（关卡/UI/音频/网络存档…），
   每区可声明 depends_on（依赖方向）、exports（对外接口文件）、verify（该区校验命令）。
2) 契约中心 + 依赖方向：verify_contracts() 校验依赖方向无环、被依赖区导出文件存在；
   写工具按方向校验，把「防堆叠」从文件层升到语义层。
3) 工具层：分区内受控读写、每区校验门、跨区安全搬移、每区提交、跨区变更集（绑定整体回滚）。

设计要点（详见《分区开发设计.md》）：
- REGIONS 默认集在 DEFAULT_REGIONS；若 code_root 下有 regions.json，则以它为准（用户可改）。
- init_regions 会写出 regions.json（若原本没有），使分区从「代码写死」变为「配置驱动」。
- 依赖方向是 DAG；behaviors→values/assets、levels→assets/values 等，禁止反向，避免循环耦合。
- 每区独立 git 仓库；commit_all 把一次功能改动在多个区分支提交并记进 dev_changesets.jsonl，
  回滚时整体 revert，比「每区独立 git」更进一步。
"""
import json
import os
import subprocess
import sys
import uuid
import shutil
from datetime import datetime

from config import get_runtime, CODE_ROOT
from game_workbench import require_approval

# ---------------------------------------------------------------------------
# 默认分区定义（DEFAULT_REGIONS）。新增/调整分区：改这里，或在项目根放 regions.json 覆盖。
# 字段：key(唯一) / dir(子目录) / name(中文) / desc / access(接入方式) /
#       depends_on(依赖的其它分区 key，构成 DAG) / exports(对外接口文件) / verify(该区校验命令，空=跳过)
# ---------------------------------------------------------------------------
DEFAULT_REGIONS = [
    {
        "key": "assets", "dir": "assets", "name": "素材区",
        "desc": "美术资源 / 精灵 / 音效 / 模型",
        "access": "其它分区经 dev_asset_get 拉取，不内联源码",
        "depends_on": [], "exports": ["manifest.json"], "verify": "builtin:json",
    },
    {
        "key": "values", "dir": "values", "name": "数值区",
        "desc": "伤害 / 成长 / 经济等数值表",
        "access": "集中配置，避免散落各处",
        "depends_on": [], "exports": ["balance.schema.json"], "verify": "builtin:json",
    },
    {
        "key": "bugs", "dir": "bugs", "name": "bug 区",
        "desc": "捕获的异常 / 堆栈 / 复现步骤",
        "access": "dev_capture_bug 写入，按分区归因",
        "depends_on": [], "exports": [], "verify": "builtin:json",
    },
    {
        "key": "behaviors", "dir": "behaviors", "name": "角色行为区",
        "desc": "状态机 / 行为树 / 动作脚本",
        "access": "与数值区解耦，各自演进",
        "depends_on": ["values", "assets"], "exports": [], "verify": "builtin:py",
    },
    {
        "key": "levels", "dir": "levels", "name": "关卡·场景区",
        "desc": "地图 / 场景 / 关卡配置",
        "access": "引用数值与素材，不直接写逻辑",
        "depends_on": ["assets", "values"], "exports": [], "verify": "builtin:py",
    },
    {
        "key": "ui", "dir": "ui", "name": "UI·HUD 区",
        "desc": "菜单 / HUD / 对话树 / 本地化 i18n",
        "access": "引用数值做显示，不内联业务逻辑",
        "depends_on": ["values"], "exports": [], "verify": "builtin:py",
    },
    {
        "key": "audio", "dir": "audio", "name": "音频区",
        "desc": "混音 / 事件触发配置",
        "access": "引用素材区的音频文件",
        "depends_on": ["assets"], "exports": [], "verify": "builtin:json",
    },
    {
        "key": "net", "dir": "net", "name": "网络·存档区",
        "desc": "联机 / 存档序列化（最易出 bug，单独隔离）",
        "access": "独立演进，谨慎改",
        "depends_on": ["values"], "exports": [], "verify": "builtin:py",
    },
]

# 兼容旧引用：regions.REGIONS
REGIONS = DEFAULT_REGIONS

_REGION_CONFIG_NAME = "regions.json"
_CHANGESETS_NAME = "dev_changesets.jsonl"


# ---------------------------------------------------------------------------
# 内置校验器：作为默认分区的 verify 命令（builtin:py / builtin:json）。
# 不依赖用户侧脚本、不经 _run_region_cmd 的安全黑名单，由 run_verify 分发。
# - builtin:py   ：编译目录下所有 .py（语法检查）
# - builtin:json ：校验目录下所有 .json / .toml 可解析
# ---------------------------------------------------------------------------
def _builtin_py_verify(dir_abs):
    """语法检查目录下所有 .py。返回 (ok, output)。

    必须在进程内用 compile() 完成，不能 subprocess 调 `sys.executable -m compileall`：
    PyInstaller 打包后 sys.executable 是 DocMind.exe（onedir 内无 python.exe），
    再启它只会打开浏览器并阻塞，导致校验恒失败/假通过。compile() 只编译不执行，
    不会运行被测代码，安全且在 frozen / 源码两种模式下行为一致。
    """
    bad, checked = [], 0
    for dp, dns, fns in os.walk(dir_abs):
        dns[:] = [d for d in dns if d not in {".git", "__pycache__", ".venv", "venv"}]
        for name in fns:
            if not name.endswith(".py"):
                continue
            p = os.path.join(dp, name)
            try:
                with open(p, encoding="utf-8") as fh:
                    compile(fh.read(), p, "exec")
                checked += 1
            except SyntaxError as e:
                bad.append(f"{os.path.relpath(p, dir_abs)}: {e.msg}（行 {e.lineno}）")
            except Exception as e:  # noqa: BLE001
                bad.append(f"{os.path.relpath(p, dir_abs)}: {type(e).__name__}: {e}")
    if bad:
        return False, "Python 语法检查失败:\n" + "\n".join(bad)[:2000]
    return True, f"已编译检查 {checked} 个 .py 文件，全部通过。"


def _builtin_json_verify(dir_abs):
    """校验目录下所有 .json / .toml 可解析。返回 (ok, output)。"""
    import json as _json
    try:
        import tomllib as _toml
    except Exception:  # noqa: BLE001
        _toml = None
    bad, checked = [], 0
    try:
        names = os.listdir(dir_abs)
    except OSError:
        names = []
    for name in names:
        p = os.path.join(dir_abs, name)
        if not os.path.isfile(p):
            continue
        low = name.lower()
        try:
            if low.endswith(".json"):
                with open(p, encoding="utf-8") as fh:
                    _json.load(fh)
                checked += 1
            elif low.endswith(".toml") and _toml is not None:
                with open(p, "rb") as fh:
                    _toml.load(fh)
                checked += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{name}: {e}")
    if bad:
        return False, "JSON/TOML 解析失败:\n" + "\n".join(bad)
    return True, f"已校验 {checked} 个 JSON/TOML 文件，全部可解析。"


def run_verify(dir_abs, verify_cmd):
    """分发校验命令：builtin:* 走内置校验器；自定义命令返回 (None, None) 由调用方走 _run_region_cmd。"""
    vc = (verify_cmd or "").strip()
    if not vc:
        return None, None
    if vc.startswith("builtin:py"):
        return _builtin_py_verify(dir_abs)
    if vc.startswith("builtin:json"):
        return _builtin_json_verify(dir_abs)
    return None, None


def _get_code_root():
    return (get_runtime("code_root") or CODE_ROOT or "").strip()


def _region_dir(root, region):
    return os.path.join(root, region["dir"])


def load_region_config(root):
    """读取分区配置：优先 regions.json（用户可改），否则用 DEFAULT_REGIONS。"""
    cfg = os.path.join(root, _REGION_CONFIG_NAME) if root else ""
    if root and os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                data = json.load(f)
            regs = data.get("regions")
            normalized = _normalize_regions(regs) if isinstance(regs, list) else []
            if normalized:
                return normalized
        except Exception:
            pass
    return list(DEFAULT_REGIONS)


def validate_region_config(root, regions=None):
    """Validate region identities, directories, dependencies and exports before applying them."""
    regions = _normalize_regions(regions if regions is not None else load_region_config(root))
    errors, keys, dirs = [], set(), set()
    root_abs = os.path.realpath(root or "")
    for r in regions:
        if r["key"] in keys:
            errors.append(f"重复分区 key：{r['key']}")
        keys.add(r["key"])
        d = os.path.realpath(os.path.join(root_abs, r["dir"]))
        if d in dirs:
            errors.append(f"分区目录重复：{r['dir']}")
        dirs.add(d)
        if not (d == root_abs or d.startswith(root_abs + os.sep)):
            errors.append(f"分区目录越界：{r['dir']}")
    dir_list = list(dirs)
    for i, a in enumerate(dir_list):
        for b in dir_list[i + 1:]:
            if a.startswith(b + os.sep) or b.startswith(a + os.sep):
                errors.append(f"分区目录不能互相嵌套：{a} / {b}")
    for r in regions:
        if r["key"] in r.get("depends_on", []):
            errors.append(f"分区不能依赖自身：{r['key']}")
        for dep in r.get("depends_on", []):
            if dep not in keys:
                errors.append(f"{r['key']} 依赖不存在的分区：{dep}")
        for ex in r.get("exports", []):
            target = os.path.realpath(os.path.join(root_abs, r["dir"], ex))
            base = os.path.realpath(os.path.join(root_abs, r["dir"]))
            if not (target == base or target.startswith(base + os.sep)):
                errors.append(f"{r['key']} 导出路径越界：{ex}")
    return {"ok": not errors, "errors": errors, "regions": regions}


def get_region_map(root):
    return {r["key"]: r for r in load_region_config(root)}


_GIT_MISSING_HINT = (
    "未找到 git 可执行文件（不在 PATH 中）。分区初始化/提交/回滚依赖 Git，"
    "请先安装 Git for Windows（https://git-scm.com/download/win，安装时勾选 "
    "“Git from the command line and also from 3rd-party software”），"
    "安装后重新打开 Agent 工作台 再试。"
)
_GIT_MISSING_HINT_FROZEN = (
    "未能启动随包附带的 Git（MinGit）。分发版应在程序目录的 MinGit\\cmd 下自带 git，"
    "请检查该目录是否被杀毒软件删除或隔离；恢复后重新打开 Agent 工作台，"
    "或重新获取完整的 Agent 工作台 分发包。"
)


def _git_missing_hint():
    import sys
    return _GIT_MISSING_HINT_FROZEN if getattr(sys, "frozen", False) else _GIT_MISSING_HINT


def _git_available():
    """预检 git 是否可用；返回 (True, '') 或 (False, 给用户的明确提示)。"""
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=15)
        return True, ""
    except FileNotFoundError:
        return False, _git_missing_hint()
    except Exception as e:  # noqa: BLE001
        return False, f"无法执行 git：{e}。请确认 Git 已安装并加入 PATH。"


def _git(args, cwd):
    """在 cwd 内执行 git；返回 (ok, out)。失败返回 (False, stderr)。"""
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError:
        # 裸 shell 里没有 git 时 subprocess 抛 WinError 2，给出可操作提示而非晦涩系统错误
        return False, _git_missing_hint()
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    # 只能 rstrip：porcelain -z 首条记录可能以状态列空格开头（如 " M path"），
    # strip() 会吃掉它导致后续按列解析时路径错位（"alues/..."），未暂存修改漏标脏。
    return True, (proc.stdout or "").rstrip()


def _count_files(d):
    n = 0
    for dp, _, fns in os.walk(d):
        if ".git" in dp.split(os.sep):
            continue
        n += len(fns)
    return n


def _is_dirty(d):
    """分区 git 工作区是否有未提交改动。"""
    if not os.path.isdir(os.path.join(d, ".git")):
        return False
    ok, out = _git(["status", "--porcelain"], cwd=d)
    return bool(ok and out.strip())


def _ignore_in_parent(root):
    """若父仓库已存在 .git，把各分区目录加进父 .gitignore，避免双重跟踪（只追加、幂等）。"""
    if not os.path.isdir(os.path.join(root, ".git")):
        return
    gi = os.path.join(root, ".gitignore")
    existing = set()
    if os.path.isfile(gi):
        with open(gi, encoding="utf-8", errors="ignore") as f:
            existing = {line.strip() for line in f}
    new_lines = [meta["dir"] + "/" for meta in load_region_config(root)
                 if (meta["dir"] + "/") not in existing]
    if ".docmind_backups/" not in existing:
        new_lines.append(".docmind_backups/")
    if new_lines:
        with open(gi, "a", encoding="utf-8") as f:
            f.write("\n# Agent 工作台 分区开发：各分区为独立 git 仓库，父仓库不跟踪其内容\n")
            f.write("\n".join(new_lines) + "\n")


def init_regions(root=None, regions_list=None):
    """建全部配置分区目录，每区 git init 独立仓库，写导出接口桩、README、regions.json、
    DEV_INDEX.md 与 DOCMIND_RULES.md（含依赖方向）。返回 (ok, message)。

    regions_list: 可选，显式传入自定义分区清单（由 Agent 依据代码库研判后给出）。
      传入后会归一化并覆盖写入 regions.json，使分区从「默认建议」变为「Agent 判定方案」。
    """
    root = (root or _get_code_root() or "").strip()
    if not root or not os.path.isdir(root):
        return (
            False,
            f"未配置代码库根目录或目录不存在：{root or '(空)'}。"
            "请先用 /api/ingest_code 指定代码目录，或传入 root 参数。",
        )

    # 决定要初始化的分区清单：优先用调用方显式给出的自定义方案，否则读配置/默认
    if regions_list is not None:
        # 应用「自定义分区方案」属于结构性操作，需先经审批门禁
        gate = require_approval(root, "apply_regions", "*")
        if gate:
            return False, gate
        regions = _normalize_regions(regions_list)
        if not regions:
            return False, "自定义分区列表为空或格式非法（每个分区需含 key 与 dir 字段）。"
    else:
        regions = load_region_config(root)
    validation = validate_region_config(root, regions)
    if not validation["ok"]:
        return False, "分区配置校验失败：\n- " + "\n- ".join(validation["errors"])
    regions = validation["regions"]
    # git 是分区仓库/变更集/回滚的硬性前置依赖：在写任何文件之前预检，
    # 避免 git init 失败后留下 regions.json、.gitignore 追加与半成品分区目录。
    git_ok, git_hint = _git_available()
    if not git_ok:
        return False, git_hint
    _ignore_in_parent(root)

    # 写出 regions.json：自定义方案覆盖写；否则仅在缺失时写（保证用户手动改动不被覆盖）
    cfg_path = os.path.join(root, _REGION_CONFIG_NAME)
    if not os.path.isfile(cfg_path) or regions_list is not None:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"regions": regions}, f, ensure_ascii=False, indent=2)

    created = []
    for meta in regions:
        d = os.path.join(root, meta["dir"])
        os.makedirs(d, exist_ok=True)
        if not os.path.isdir(os.path.join(d, ".git")):
            ok, err = _git(["init", "-q"], cwd=d)
            if not ok:
                return False, f"git init 失败（{meta['name']}）：{err}"
            # 为工具托管的独立仓库设置本地提交身份，避免机器无全局 git 身份时提交失败
            _git(["config", "user.email", "docmind@local"], cwd=d)
            _git(["config", "user.name", "Agent Workbench"], cwd=d)
        # 导出接口桩文件（让契约校验有东西可验）
        for ex in meta.get("exports") or []:
            ex_path = os.path.join(d, ex)
            if not os.path.isfile(ex_path):
                os.makedirs(os.path.dirname(ex_path) or d, exist_ok=True)
                with open(ex_path, "w", encoding="utf-8") as f:
                    if ex.endswith(".json"):
                        f.write("{}\n")
                    else:
                        f.write("")
        readme = os.path.join(d, "README.md")
        if not os.path.isfile(readme):
            deps = meta.get("depends_on") or []
            with open(readme, "w", encoding="utf-8") as f:
                f.write(
                    f"# {meta['name']}（{meta['dir']}）\n\n"
                    f"{meta['desc']}\n\n"
                    f"接入方式：{meta['access']}\n"
                    + (f"依赖分区：{', '.join(deps)}\n" if deps else "依赖分区：无（基础分区）\n")
                    + "\n> 本目录是独立 git 仓库，可单独 commit / 回滚，不影响其它分区。\n"
                )
        created.append(meta["name"])

        # 初始化仓库即留下可回滚的基线提交。
        ok_status, status = _git(["status", "--porcelain"], cwd=d)
        if ok_status and status.strip():
            _git(["add", "-A"], cwd=d)
            _git(["commit", "-m", "docmind: initialize region"], cwd=d)

    _write_dev_index(root, regions)
    _write_rules(root, regions)

    return (
        True,
        f"已初始化 {len(created)} 个分区：{', '.join(created)}。"
        f"生成 regions.json / DEV_INDEX.md / DOCMIND_RULES.md（含依赖方向约束）。",
    )


def scaffold_region(root, key):
    """为单个缺失分区补建目录与声明的导出接口桩（+README）。

    与 init_regions 的区别：只处理一个分区，不重写 regions.json / DEV_INDEX /
    DOCMIND_RULES；若本项目的其它分区均为独立 git 仓库则沿用该模式（git init +
    基线提交），否则仅创建目录文件、纳入项目主仓库统一管理。返回 (ok, detail)。
    """
    root = (root or _get_code_root() or "").strip()
    if not root or not os.path.isdir(root):
        return False, f"未配置代码库根目录或目录不存在：{root or '(空)'}。"
    regions = load_region_config(root)
    meta = next((r for r in regions if r.get("key") == key), None)
    if not meta:
        return False, f"未知分区：{key}（不在 regions.json 配置中）"

    # 目录安全：配置里的 dir 必须是根目录内的相对路径，禁止绝对路径与 .. 逃逸
    rel = meta["dir"].replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not rel or os.path.isabs(meta["dir"]) or ".." in parts:
        return False, f"分区目录配置非法：{meta['dir']}"
    d = os.path.normpath(os.path.join(root, *parts))
    if os.path.commonpath([d, os.path.normpath(root)]) != os.path.normpath(root):
        return False, f"分区目录逃逸出代码库根目录：{meta['dir']}"
    if os.path.exists(d) and not os.path.isdir(d):
        return False, f"{rel} 已存在且不是目录，无法创建分区。"

    created_dir = not os.path.isdir(d)
    os.makedirs(d, exist_ok=True)
    created_files = []

    # 导出接口桩文件（与 init_regions 完全一致：JSON 写 {}，其余写空文件）
    for ex in meta.get("exports") or []:
        ex_path = os.path.join(d, ex)
        if os.path.exists(ex_path) and not os.path.isfile(ex_path):
            continue
        if not os.path.isfile(ex_path):
            os.makedirs(os.path.dirname(ex_path) or d, exist_ok=True)
            with open(ex_path, "w", encoding="utf-8") as f:
                f.write("{}\n" if ex.endswith(".json") else "")
            created_files.append(ex)

    readme = os.path.join(d, "README.md")
    if not os.path.isfile(readme):
        deps = meta.get("depends_on") or []
        footer = (
            "\n> 本目录是独立 git 仓库，可单独 commit / 回滚，不影响其它分区。\n"
            if _siblings_use_git(root, regions, key)
            else "\n> 由 Agent 工作台 分区可视化一键创建，纳入项目主仓库统一版本管理。\n"
        )
        with open(readme, "w", encoding="utf-8") as f:
            f.write(
                f"# {meta['name']}（{meta['dir']}）\n\n"
                f"{meta['desc']}\n\n"
                f"接入方式：{meta['access']}\n"
                + (f"依赖分区：{', '.join(deps)}\n" if deps else "依赖分区：无（基础分区）\n")
                + footer
            )
        created_files.append("README.md")

    # 本项目其它分区都是独立 git 仓库时，新区沿用同一模式并留下基线提交
    git_warning = ""
    if _siblings_use_git(root, regions, key) and not os.path.isdir(os.path.join(d, ".git")):
        ok, err = _git(["init", "-q"], cwd=d)
        if ok:
            _git(["config", "user.email", "docmind@local"], cwd=d)
            _git(["config", "user.name", "Agent Workbench"], cwd=d)
            ok_status, status = _git(["status", "--porcelain"], cwd=d)
            if ok_status and status.strip():
                _git(["add", "-A"], cwd=d)
                _git(["commit", "-m", "docmind: initialize region"], cwd=d)
        else:
            git_warning = f"（目录与文件已创建，但 git init 失败：{err}）"

    return True, {
        "key": key,
        "name": meta["name"],
        "dir": meta["dir"],
        "created_dir": created_dir,
        "created_files": created_files,
        "git_warning": git_warning,
    }


def _siblings_use_git(root, regions, key):
    """项目中已存在的其它分区是否都采用独立 git 仓库（用于决定新区的创建模式）。"""
    existing = [
        r for r in regions
        if r.get("key") != key and os.path.isdir(os.path.join(root, r["dir"]))
    ]
    if not existing:
        return False
    return all(os.path.isdir(os.path.join(root, r["dir"], ".git")) for r in existing)


def fill_region_exports(root, key):
    """为「目录已存在、但缺导出接口文件」的分区补齐缺失的导出桩。

    只写缺失的文件（JSON 写 {}，其余写空文件），不动 README/regions.json，
    也不自动提交（与工作台保存流程一致）。返回 (ok, detail)。
    """
    root = (root or _get_code_root() or "").strip()
    if not root or not os.path.isdir(root):
        return False, f"未配置代码库根目录或目录不存在：{root or '(空)'}。"
    regions = load_region_config(root)
    meta = next((r for r in regions if r.get("key") == key), None)
    if not meta:
        return False, f"未知分区：{key}（不在 regions.json 配置中）"
    d = os.path.join(root, meta["dir"])
    if not os.path.isdir(d):
        return False, f"{meta['name']} 目录尚未创建，请先一键创建分区。"

    created_files = []
    for ex in meta.get("exports") or []:
        ex_path = os.path.normpath(os.path.join(d, ex))
        # 导出文件必须落在分区目录内，禁止绝对路径与 .. 逃逸
        if os.path.commonpath([ex_path, os.path.normpath(d)]) != os.path.normpath(d):
            return False, f"导出文件路径非法：{ex}"
        if os.path.exists(ex_path) and not os.path.isfile(ex_path):
            return False, f"{ex} 已存在且不是文件，无法生成导出桩。"
        if not os.path.isfile(ex_path):
            os.makedirs(os.path.dirname(ex_path) or d, exist_ok=True)
            with open(ex_path, "w", encoding="utf-8") as f:
                f.write("{}\n" if ex.endswith(".json") else "")
            created_files.append(ex)

    return True, {
        "key": key,
        "name": meta["name"],
        "dir": meta["dir"],
        "created_files": created_files,
    }


# ---------------------------------------------------------------------------
# Agent 研判分区：默认 8 个分区仅作「初始建议」，真实分区由 Agent 依据代码库判断。
# propose_regions() 扫描顶层目录与资源类型，给出带证据（evidence）的建议方案；
# _normalize_regions() 校验/补全自定义方案字段；init_regions(regions_list=...) 落地。
# ---------------------------------------------------------------------------
_MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tga", ".tiff",
    ".wav", ".mp3", ".ogg", ".flac", ".aiff", ".m4a",
    ".glb", ".gltf", ".fbx", ".obj", ".blend", ".dae", ".3ds",
    ".ttf", ".otf", ".woff", ".woff2", ".fnt",
    ".mp4", ".mov", ".webm", ".avi",
    ".glsl", ".hlsl", ".shader", ".cginc",
}
_DATA_EXTS = {".json", ".csv", ".yaml", ".yml", ".xml", ".toml", ".ini", ".tsv", ".cfg"}
_CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".cs", ".java", ".go", ".rs",
              ".cpp", ".c", ".h", ".hpp", ".lua", ".gd", ".kt", ".swift", ".rb"}
# 扫描时跳过的依赖/缓存/构建目录，避免噪声与卡顿
_SCAN_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", "build",
              "dist", ".idea", ".vscode", "Library", "obj", "bin"}
# 目录名关键词 -> 分区 key（启发式，Agent 可据真实结构增删/调整）
_DIR_HINTS = {
    "assets": ["asset", "art", "artwork", "res", "resource", "sprite", "texture", "gfx", "graphic"],
    "values": ["value", "balance", "config", "data", "stat", "tuning", "economy"],
    "bugs": ["bug", "issue", "defect", "crash", "error", "exception"],
    "behaviors": ["ai", "behavior", "behaviour", "fsm", "statemachine", "character",
                  "entity", "actor", "npc", "creature", "mob", "agent"],
    "levels": ["level", "scene", "map", "stage", "world"],
    "ui": ["ui", "hud", "menu", "widget", "gui", "interface", "ux", "panel"],
    "audio": ["audio", "sound", "music", "sfx", "voice"],
    "net": ["net", "network", "server", "multiplayer", "save", "sync", "online"],
}
# 核心分区：默认恒含（安全/基础性质），其余按检出信号决定
_CORE_KEYS = {"assets", "values", "bugs"}
_DEFAULT_BY_KEY = {r["key"]: r for r in DEFAULT_REGIONS}


def _normalize_regions(regions):
    """校验并补全分区字段；返回归一化列表（丢弃缺 key/dir 或重复的非法项）。

    depends_on / exports 既接受列表也接受逗号分隔字符串，统一归一为列表。
    """
    out = []
    seen = set()
    for r in regions or []:
        if not isinstance(r, dict):
            continue
        key = (r.get("key") or "").strip()
        d = (r.get("dir") or "").strip()
        if not key or not d:
            continue
        # 分区目录必须是 code_root 内的相对路径，防止 regions.json/API
        # 通过 ..、绝对路径或盘符把 Git/写操作带到工程外部。
        raw_d = d.replace("\\", "/")
        if (not raw_d or raw_d.startswith(".") or raw_d.startswith("/") or
                raw_d.startswith("~") or
                (len(raw_d) >= 2 and raw_d[1] == ":") or
                "//" in raw_d or
                any(part in ("", ".", "..") for part in raw_d.split("/"))):
            continue
        d = raw_d.strip("/")
        if (not d or d.startswith(".") or d.startswith("/") or
                ":" in d or any(part in ("", ".", "..") for part in d.split("/"))):
            continue
        if any(ch in d for ch in '*?[]'):
            continue
        if key in seen:
            continue
        seen.add(key)

        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, str):
                return [x.strip() for x in v.split(",") if x.strip()]
            return [str(x).strip() for x in v if str(x).strip()]

        exports = r.get("exports") or []
        safe_exports = []
        for ex in exports if isinstance(exports, list) else []:
            ex = str(ex).replace("\\", "/").strip("/")
            if ex and not ex.startswith(".") and ":" not in ex and not any(p in ("", ".", "..") for p in ex.split("/")):
                safe_exports.append(ex)
        out.append({
            "key": key,
            "dir": d.strip("/\\").replace("\\", "/"),
            "name": (r.get("name") or "").strip() or key,
            "desc": (r.get("desc") or "").strip(),
            "access": (r.get("access") or "").strip(),
            "depends_on": _as_list(r.get("depends_on")),
            "exports": safe_exports,
            "verify": (r.get("verify") or "").strip(),
        })
    return out


def _analyze_root(root, max_depth=2):
    """扫描代码库顶层目录：统计每个目录文件数与各类型扩展名（浅层，跳过依赖/缓存目录）。"""
    top = []
    try:
        names = sorted(os.listdir(root))
    except Exception:
        return top
    for name in names:
        p = os.path.join(root, name)
        if os.path.isdir(p) and name not in _SCAN_SKIP:
            exts = {}
            n = 0
            for dp, dns, fns in os.walk(p):
                if dp[len(p):].count(os.sep) >= max_depth:
                    dns[:] = []
                    continue
                dns[:] = [d for d in dns if d not in _SCAN_SKIP]
                for fn in fns:
                    n += 1
                    exts[os.path.splitext(fn)[1].lower()] = exts.get(os.path.splitext(fn)[1].lower(), 0) + 1
            top.append({"name": name, "files": n, "exts": exts, "is_dir": True})
        elif os.path.isfile(p):
            ext = os.path.splitext(name)[1].lower()
            top.append({"name": name, "files": 1, "exts": {ext: 1}, "is_dir": False})
    return top


def _detect_signals(top):
    """根据顶层目录名与扩展名，给出每个默认分区的命中证据。返回 {key: {hit, evidence[]}}。"""
    res = {}
    for key, hints in _DIR_HINTS.items():
        evidence, hit = [], False
        for d in top:
            nm = d["name"].lower()
            matched = [h for h in hints if h in nm]
            if matched:
                evidence.append(f"目录 {d['name']}/ 命中关键词（{', '.join(matched)}）")
                hit = True
        if key == "assets":
            for d in top:
                m = sum(d["exts"].get(e, 0) for e in _MEDIA_EXTS)
                if m >= 3:
                    evidence.append(f"目录 {d['name']}/ 含 {m} 个媒体/资源文件（图/音/模型/字体等）")
                    hit = True
        if key == "values":
            for d in top:
                dt = sum(d["exts"].get(e, 0) for e in _DATA_EXTS)
                if dt >= 3 and any(k in d["name"].lower() for k in ("balanc", "config", "stat", "tuning", "data")):
                    evidence.append(f"目录 {d['name']}/ 含 {dt} 个数据/配置表（json/csv/yaml…）")
                    hit = True
        res[key] = {"hit": hit, "evidence": evidence}
    return res


def _custom_suggestions(top, signals):
    """识别未命中默认分区、但含大量代码的顶层模块目录，建议作为独立分区。"""
    sug = []
    all_hints = [h for hs in _DIR_HINTS.values() for h in hs]
    for d in top:
        if not d["is_dir"]:
            continue
        nm = d["name"].lower()
        if any(h in nm for h in all_hints):
            continue  # 已被某默认分区关键词命中
        code = sum(d["exts"].get(e, 0) for e in _CODE_EXTS)
        if code >= 2:
            sug.append({
                "key": nm, "dir": d["name"], "name": nm.capitalize(),
                "desc": f"从代码库实际结构识别出的「{d['name']}/」模块，含 {code} 个代码文件，建议作独立分区。",
                "access": "按模块边界隔离，避免与默认分区耦合。",
                "depends_on": [], "exports": [], "verify": "",
                "evidence": [f"目录 {d['name']}/ 含 {code} 个代码文件，未命中默认分区关键词，建议独立成区。"],
            })
    return sug[:6]


def propose_regions(root=None):
    """依据真实代码库结构研判分区方案（默认 8 区仅作初始建议）。

    返回：
      {ok, root, analysis:{顶层目录/文件/各类计数},
       proposed:[默认分区 + detected/evidence/included/reason],
       custom_suggestions:[代码库特有的可独立分区],
       summary: 中文建议}
    Agent 可据 proposed 的 detected/included 标志裁掉无关分区、据 custom_suggestions 增补，
    再把最终清单交给 dev_apply_regions / api 落地。
    """
    root = (root or _get_code_root() or "").strip()
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"未配置代码库根目录或目录不存在：{root or '(空)'}。"}
    root = os.path.abspath(root)
    top = _analyze_root(root)
    signals = _detect_signals(top)

    proposed = []
    for key, meta in _DEFAULT_BY_KEY.items():
        sinfo = signals.get(key, {"hit": False, "evidence": []})
        include = key in _CORE_KEYS or sinfo["hit"]
        r = dict(meta)
        r["detected"] = bool(sinfo["hit"])
        r["evidence"] = sinfo["evidence"]
        r["included"] = include
        r["reason"] = (
            "默认核心分区，建议保留（素材/数值/bug 隔离是防堆叠基础）" if key in _CORE_KEYS
            else ("代码库中检出相关信号，建议启用" if sinfo["hit"]
                  else "代码库未检出相关信号，可暂不启用或手动增补")
        )
        proposed.append(r)

    customs = _custom_suggestions(top, signals)

    n_images = sum(d["exts"].get(e, 0) for d in top for e in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tga", ".tiff"))
    n_audio = sum(d["exts"].get(e, 0) for d in top for e in (".wav", ".mp3", ".ogg", ".flac", ".aiff", ".m4a"))
    n_model = sum(d["exts"].get(e, 0) for d in top for e in (".glb", ".gltf", ".fbx", ".obj", ".blend", ".dae", ".3ds"))
    n_data = sum(d["exts"].get(e, 0) for d in top for e in _DATA_EXTS)
    n_code = sum(d["exts"].get(e, 0) for d in top for e in _CODE_EXTS)

    included_keys = [r["key"] for r in proposed if r["included"]]
    summary = (
        f"已分析代码库 {root}：顶层目录 {len([d for d in top if d['is_dir']])} 个，"
        f"检出 图像{n_images}/音频{n_audio}/模型{n_model} 媒体，数据表{n_data}，代码{n_code}。"
        f"建议启用分区：{', '.join(included_keys)}。"
        + (f"另识别出 {len(customs)} 个可独立分区模块（见 custom_suggestions）。" if customs else "")
        + " 默认 8 个分区仅作初始建议，请结合真实结构增删/调整后再应用。"
    )

    return {
        "ok": True,
        "root": root,
        "analysis": {
            "top_level_dirs": [d["name"] for d in top if d["is_dir"]],
            "top_level_files": [d["name"] for d in top if not d["is_dir"]],
            "counts": {"images": n_images, "audio": n_audio, "models": n_model,
                       "data": n_data, "code": n_code},
        },
        "proposed": proposed,
        "custom_suggestions": customs,
        "summary": summary,
    }


def _write_dev_index(root, regions):
    lines = [
        "# 分区开发索引（DEV_INDEX）",
        "",
        "> 由 Agent 工作台 开发模式自动生成（改完分区后可用 dev_rebuild_index 重算）。",
        "> 每个分区是 code_root 下的独立子目录 + 独立 git 仓库；依赖方向见 `依赖`。",
        "",
    ]
    for meta in regions:
        d = os.path.join(root, meta["dir"])
        n = _count_files(d)
        deps = meta.get("depends_on") or []
        lines.append(f"- **{meta['name']}**（`{meta['dir']}/`）—— {meta['desc']}")
        lines.append(
            f"  - 接入：{meta['access']}；依赖：{', '.join(deps) if deps else '无'}；当前文件数：{n}"
        )
    lines += [
        "",
        "## 约定",
        "- 修改某分区只动对应子目录，跨区写会被工具拦截。",
        "- 依赖方向为单向（如 behaviors→values），禁止反向，避免循环耦合。",
        "- 每完成一个分区改动在该分区内 commit；一次功能跨多区用 dev_commit_all 绑定回滚。",
        "",
    ]
    with open(os.path.join(root, "DEV_INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def rebuild_dev_index(root=None):
    """依据当前分区配置重算 DEV_INDEX.md（分区改动后调用）。"""
    root = (root or _get_code_root() or "").strip()
    if not root or not os.path.isdir(root):
        return False, "未配置代码库根目录。"
    regions = load_region_config(root)
    _write_dev_index(root, regions)
    return True, f"已重算 DEV_INDEX.md（{len(regions)} 个分区）。"


def _write_rules(root, regions):
    rlines = [
        "# Agent 工作台 分区开发规则（本文件由 /api/ingest_code 注入 Agent 系统提示）",
        "",
        "本项目采用「分区开发」：代码按分工落在以下独立子目录，每个目录是独立 git 仓库。",
        "",
    ]
    for meta in regions:
        deps = meta.get("depends_on") or []
        dep_s = f"，依赖：{', '.join(deps)}" if deps else "，为基础分区"
        rlines.append(
            f"- {meta['name']}（`{meta['dir']}/`）：{meta['desc']}。接入方式：{meta['access']}{dep_s}。"
        )
    rlines += [
        "",
        "强制约束：",
        "- 任何文件修改必须落在上述某个分区子目录内；禁止在分区外随意新建/改写文件（越区写会被拦截）。",
        "- 依赖方向为单向：仅允许依赖于 `depends_on` 中列出的分区，禁止反向依赖，避免循环耦合。",
        "- 数值类改动只进 `values/`；美术资源只进 `assets/`（其它分区经接口引用，不内联）；"
        "异常只归集到 `bugs/`；角色行为只进 `behaviors/`。",
        "- 每完成一个分区的改动应在该分区目录内 commit；一次功能跨多区请用 dev_commit_all 绑定回滚。",
        "- 新建文件前先用 search_code/grep 确认分区内无重复实现，防止代码堆叠。",
        "",
    ]
    with open(os.path.join(root, "DOCMIND_RULES.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(rlines))


def verify_contracts(root=None):
    """校验契约：依赖方向无环、被依赖区导出文件存在、依赖目标存在。
    返回 {ok, errors[], graph:{key:deps}}。"""
    root = (root or _get_code_root() or "").strip()
    regions = load_region_config(root)
    rmap = {r["key"]: r for r in regions}
    errors = []

    # 1) 依赖目标存在
    for r in regions:
        for dep in r.get("depends_on") or []:
            if dep not in rmap:
                errors.append(f"{r['name']} 依赖了不存在的分区：{dep}")

    # 2) 环检测（拓扑排序）
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {r["key"]: WHITE for r in regions}
    cycle = []

    def visit(k, stack):
        color[k] = GRAY
        stack.append(k)
        for nxt in rmap[k].get("depends_on") or []:
            if nxt not in rmap:
                continue
            if color[nxt] == GRAY:
                cycle.append(stack[stack.index(nxt):] + [nxt])
                return True
            if color[nxt] == WHITE and visit(nxt, stack):
                return True
        stack.pop()
        color[k] = BLACK
        return False

    for r in regions:
        if color[r["key"]] == WHITE:
            if visit(r["key"], []):
                break
    if cycle:
        errors.append("检测到循环依赖：" + " → ".join(cycle[0]))

    # 3) 导出接口文件存在
    for r in regions:
        d = os.path.join(root, r["dir"]) if root else None
        for ex in r.get("exports") or []:
            if not d or not os.path.isfile(os.path.join(d, ex)):
                errors.append(f"{r['name']} 缺少导出接口文件：{ex}（契约校验失败）")

    return {"ok": len(errors) == 0, "errors": errors, "graph": {r["key"]: r.get("depends_on") or [] for r in regions}}


def commit_region(root, key, message):
    """在某分区独立仓库内提交。返回 (ok, out)。"""
    # 「留地板、拆其余」重构：git 提交可逆（git log / reset），提交前的审批卡属于
    # "处处受限"而非灾难地板，拆掉 —— 直接提交。
    gate = None
    if gate:
        return False, gate
    rmap = get_region_map(root)
    if key not in rmap:
        return False, f"未知分区：{key}"
    d = os.path.join(root, rmap[key]["dir"])
    if not os.path.isdir(os.path.join(d, ".git")):
        return False, f"{rmap[key]['name']} 尚未初始化 git 仓库"
    _git(["add", "-A"], cwd=d)
    ok, out = _git(["commit", "-m", message or "docmind: update"], cwd=d)
    if not ok and "nothing to commit" in out:
        return True, "无改动可提交"
    return ok, out


def commit_all(root, message):
    """把所有分区仓库各提交一次，并把本次改动记进 dev_changesets.jsonl（绑定回滚）。
    返回 (ok, {id, commits})。"""
    # 「留地板、拆其余」重构：同 commit_region —— 提交可逆，提交前的审批卡是
    # "处处受限"而非灾难地板，拆掉。
    gate = None
    if gate:
        return False, gate
    contract = verify_contracts(root)
    if not contract["ok"]:
        return False, {"id": None, "commits": {}, "errors": {"contracts": "; ".join(contract["errors"])}}
    regions = load_region_config(root)
    # 在跨区提交前保留轻量工作区快照，覆盖未提交内容的恢复场景。
    snapshot_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    snapshot_root = os.path.join(root, ".docmind_backups", snapshot_id)
    try:
        for r in regions:
            src = os.path.join(root, r["dir"])
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(snapshot_root, r["dir"]), ignore=shutil.ignore_patterns(".git"))
    except OSError:
        snapshot_id = ""
    commits = {}
    for r in regions:
        d = os.path.join(root, r["dir"])
        if not os.path.isdir(os.path.join(d, ".git")):
            continue
        _git(["add", "-A"], cwd=d)
        ok, out = _git(["commit", "-m", message or "docmind: update"], cwd=d)
        if ok:
            ok2, h = _git(["rev-parse", "HEAD"], cwd=d)
            if ok2:
                commits[r["key"]] = h
        elif "nothing to commit" not in out:
            commits[r["key"]] = f"error: {out}"
    errors = {k: v for k, v in commits.items() if isinstance(v, str) and v.startswith("error:")}
    if errors:
        return False, {"id": None, "commits": commits, "errors": errors}
    # 不为“没有任何改动”的操作创建空变更集，避免回滚列表出现无效记录。
    if not commits:
        return True, {"id": None, "commits": {}, "message": "没有可提交改动"}
    cs_id = uuid.uuid4().hex[:8]
    rec = {"id": cs_id, "message": message or "", "commits": commits, "snapshot": snapshot_id or None}
    with open(os.path.join(root, _CHANGESETS_NAME), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return True, {"id": cs_id, "commits": commits}


def list_changesets(root):
    """返回 dev_changesets.jsonl 中的变更集列表。"""
    path = os.path.join(root, _CHANGESETS_NAME)
    out = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    return out


def rollback_changeset(root, cs_id):
    """回滚某变更集：对每个分区 revert 其记录的 commit（生成新提交撤销）。返回 (ok, detail)。"""
    gate = require_approval(root, "rollback_changeset", cs_id)
    if gate:
        return False, gate
    target = None
    for rec in list_changesets(root):
        if rec.get("id") == cs_id:
            target = rec
            break
    if not target:
        return False, f"未找到变更集：{cs_id}"
    if target.get("rollback_status") == "completed":
        return False, f"变更集 {cs_id} 已经回滚过，不能重复回滚。"
    rmap = get_region_map(root)
    detail = []
    failed = False
    for key, h in (target.get("commits") or {}).items():
        if not isinstance(h, str) or h.startswith("error"):
            continue
        if key not in rmap:
            failed = True
            detail.append(f"{key}: 分区配置不存在")
            continue
        d = os.path.join(root, rmap[key]["dir"])
        if _is_dirty(d):
            failed = True
            detail.append(f"{rmap[key]['name']}: 工作区有未提交改动，已跳过（请先提交或清理）")
            continue
        ok, out = _git(["revert", "--no-edit", h], cwd=d)
        if not ok:
            failed = True
        detail.append(f"{rmap[key]['name']}: {'OK' if ok else out}")
    ok = not failed
    if ok:
        path = os.path.join(root, _CHANGESETS_NAME)
        records = list_changesets(root)
        for rec in records:
            if rec.get("id") == cs_id:
                rec["rollback_status"] = "completed"
                rec["rolled_back_at"] = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return ok, detail


def list_regions(root=None):
    """返回各分区状态：存在/git就绪/分支/脏状态/依赖/导出/校验命令/文件数。"""
    root = (root or _get_code_root() or "").strip()
    regions = load_region_config(root)
    out = []
    for meta in regions:
        d = os.path.join(root, meta["dir"]) if root else None
        exists = bool(d and os.path.isdir(d))
        git = bool(d and os.path.isdir(os.path.join(d, ".git")))
        branch = ""
        if git:
            ok, outp = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=d)
            branch = outp if ok else ""
        exports = meta.get("exports") or []
        out.append({
            "key": meta["key"],
            "name": meta["name"],
            "dir": meta["dir"],
            "desc": meta["desc"],
            "access": meta["access"],
            "depends_on": meta.get("depends_on") or [],
            "exports": exports,
            # 仅目录已存在时统计缺失导出，供前端「补齐导出桩」按钮判定
            "missing_exports": (
                [ex for ex in exports if not os.path.isfile(os.path.join(d, ex))]
                if exists else []
            ),
            "verify": meta.get("verify") or "",
            "exists": exists,
            "git": git,
            "branch": branch,
            "dirty": bool(exists and git and _is_dirty(d)),
            "files": _count_files(d) if exists else 0,
        })
    return out


def region_git_info(root, key, limit=12):
    """Return recent commits and working diff for one region."""
    rmap = get_region_map(root)
    if key not in rmap: return False, {"error": f"未知分区：{key}"}
    d = os.path.join(root, rmap[key]["dir"])
    if not os.path.isdir(os.path.join(d, ".git")): return False, {"error": "分区尚未初始化 Git"}
    ok1, log = _git(["log", f"-{int(limit)}", "--pretty=format:%h%x09%ad%x09%s", "--date=short"], cwd=d)
    ok2, diff = _git(["diff", "--stat"], cwd=d)
    return (ok1 and ok2), {"region": key, "log": log.splitlines() if log else [], "diff": diff}
