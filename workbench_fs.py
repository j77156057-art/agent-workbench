"""开发工作台文件系统接口（P0 任务 2）。

设计原则（对齐既有分区治理哲学）：
- 所有路径都是「相对 code_root 的正斜杠相对路径」；统一在沙箱内 realpath 解析，
  拒绝绝对路径/盘符/``..`` 越界/符号链接逃逸。
- 复用 regions 模块的分区配置与 ``git`` 调用；分区契约文件（regions.json /
  DOCMIND_RULES.md / DEV_INDEX.md / dev_changesets.jsonl）与任何 ``.git`` 内路径禁写禁删，
  改分区必须走既有审批流。
- 写操作护栏：扩展名白名单、512KB 单文件上限、.py 用 compile() 进程内语法校验
  （frozen 环境无 python.exe，不能走 py_compile 子进程，与 regions._builtin_py_verify 一致）、
  保存用 mtime 乐观锁防覆盖、未跟踪文件删除需显式 force。
- 纯逻辑函数全部显式接收 ``root`` 参数（不依赖运行时），便于在临时目录里做单元测试；
  APIRouter 只负责从运行时取 code_root、把 FsError 转成 HTTP 响应。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import ast
import mimetypes
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

from config import get_runtime, CODE_ROOT
from ingest import _CODE_EXT, _SKIP_DIRS, _MAX_CODE_FILE
import symbols as symlib
from regions import load_region_config, _git, _git_missing_hint
from data_formats import DataFormatError, read_file as inspect_project_file

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WB_MAX_FILE_BYTES = 512 * 1024  # 单文件读写上限
TREE_MAX_NODES = 2000           # 目录树节点硬上限，防止巨型工程拖垮前端
_STATUS_TTL = 5.0               # git 状态快照缓存秒数

# 文件树专用跳过集：DocMind 自身的运行时/临时目录。它们不在共享的
# ingest._SKIP_DIRS 里（那是索引层用的），但会污染「代码工作台」的目录树——
# 本工程实测 .docmind(6137)+.tmp(1714)+.docmind_sessions(876) 就 8727 个节点，
# 直接把 build_tree 顶到 2000 上限、触发「文件过多」截断，用户真实源码反而被裁掉。
# 只在本模块的文件树遍历里剔除，不动索引逻辑，零副作用。
TREE_SKIP_DIRS = {".docmind", ".tmp", ".docmind_sessions"}

# 根目录下受保护的契约/配置文件（改分区结构必须走既有审批流）
PROTECTED_ROOT_FILES = {
    "regions.json",
    "DOCMIND_RULES.md",
    "DEV_INDEX.md",
    "dev_changesets.jsonl",
}
# 任意层级受保护目录
PROTECTED_PARTS = {".git", ".docmind_backups", ".chroma"}

# 可在工作台编辑的扩展名 -> 语言标识（CodeMirror 按此选语言包/降级）
WB_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
    ".tsx": "tsx", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".txt": "text", ".css": "css", ".html": "html",
    ".cs": "csharp", ".gd": "gdscript", ".gdshader": "glsl", ".lua": "lua",
    ".cfg": "ini", ".ini": "ini", ".csv": "csv",
    ".tscn": "ini", ".tres": "ini", ".res": "ini",
    ".sh": "shell", ".java": "java", ".go": "go", ".rs": "rust", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".scala": "scala",
    # 2026-10-23 补齐常见文本/代码后缀：过去这些不在白名单，只能以「只读文本预览」
    # 呈现（不高亮、不可编辑），用户误以为「格式不符合、显示是假的」。CodeMirror 对
    # 无对应语言包的后缀会优雅降级为纯文本，但仍可正常编辑。
    ".vue": "vue", ".svelte": "html", ".astro": "html",
    ".mjs": "javascript", ".cjs": "javascript",
    ".sql": "sql", ".pyi": "python", ".log": "text",
    ".ps1": "shell", ".bat": "text", ".cmd": "text",
    ".rst": "markdown", ".properties": "ini", ".gradle": "text",
    ".json5": "json", ".graphql": "text", ".gql": "text",
    ".proto": "text", ".tf": "text", ".pl": "text", ".r": "text", ".dart": "text",
}
WB_EDIT_EXTS = set(WB_LANG) | set(_CODE_EXT)
WB_TEXT_NAMES = {".gitignore", ".gitattributes", ".editorconfig", ".npmrc", ".prettierrc"}
# 图片类：不按文本读，改走 /api/fs/raw 用 <img> 预览（矢量 svg 也当图看更直观）。
WB_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
    ".tif", ".tiff", ".svg", ".avif",
}


def _is_image_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in WB_IMAGE_EXTS


def _editable_text_name(target: str) -> bool:
    return os.path.splitext(target)[1].lower() in WB_EDIT_EXTS or os.path.basename(target).lower() in WB_TEXT_NAMES


class FsError(Exception):
    """业务错误：携带 HTTP 状态码与可选 extra（前端按 extra 做分支 UI）。"""

    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# 沙箱与路径
# ---------------------------------------------------------------------------
def _norm_rel(rel) -> str:
    """把输入路径归一为正斜杠相对路径；非法/越界输入直接拒绝。"""
    if rel is None or str(rel).strip() == "":
        raise FsError(400, "缺少 path 参数。")
    s = str(rel).replace("\\", "/").strip()
    if "\x00" in s:
        raise FsError(400, "路径包含非法字符。")
    if s.startswith("/") or "://" in s or (len(s) >= 2 and s[1] == ":"):
        raise FsError(403, "只允许使用代码库内的相对路径，拒绝绝对路径/盘符/URL。")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts:
        raise FsError(400, "path 不能为空。")
    if any(p == ".." for p in parts):
        raise FsError(403, "路径越界（包含 ..），已拒绝。")
    return "/".join(parts)


def _require_root(root) -> str:
    if not root or not os.path.isdir(str(root)):
        raise FsError(
            400,
            "未配置代码库根目录或目录不存在，请先在问答页索引代码目录。",
        )
    return os.path.abspath(str(root))


def _resolve(root, rel, *, must_exist=False, for_write=False):
    """归一 + realpath 解析；返回 (绝对路径, 归一相对路径)。"""
    root_abs = _require_root(root)
    rel_n = _norm_rel(rel)
    parts = rel_n.split("/")
    target = os.path.realpath(os.path.join(root_abs, *parts))
    # realpath 后再做一次包含判断，挡住符号链接/junction 逃逸
    if not (target == root_abs or target.startswith(root_abs + os.sep)):
        raise FsError(403, "路径越出代码库根目录，已拒绝。")
    if must_exist and not os.path.exists(target):
        raise FsError(404, f"文件或目录不存在：{rel_n}")
    if for_write:
        _ensure_writable(rel_n)
    return target, rel_n


def _ensure_writable(rel_n: str):
    """写操作保护：契约文件 / .git / 备份目录禁写。"""
    parts = rel_n.split("/")
    if parts[0] in PROTECTED_ROOT_FILES and len(parts) == 1:
        raise FsError(403, f"{rel_n} 是分区契约文件，请通过分区管理功能修改，工作台禁止直接写。")
    bad = [p for p in parts if p in PROTECTED_PARTS]
    if bad:
        raise FsError(403, f"受保护目录 {bad[0]}/ 禁止在工作台中修改。")


def _lang_of(name: str) -> str:
    return WB_LANG.get(os.path.splitext(name)[1].lower(), "text")


def _region_dirs(root) -> dict:
    """region 目录名(相对) -> meta。"""
    out = {}
    try:
        for meta in load_region_config(root):
            out[meta["dir"].replace("\\", "/").strip("/")] = meta
    except Exception:  # noqa: BLE001
        pass
    return out


def _region_of(rel_n: str, rdirs: dict):
    top = rel_n.split("/", 1)[0]
    return rdirs.get(top)


def _regions_enabled(root) -> bool:
    return os.path.isfile(os.path.join(root, "regions.json"))


# ---------------------------------------------------------------------------
# git 快照（tree / 删除护栏共用，5 秒缓存）
# ---------------------------------------------------------------------------
_status_cache: dict = {}


def _git_snapshot(root):
    """返回 {repo_abs: {"tracked": set(posix rel), "dirty": set(posix rel)}}。

    每个分区目录是一个独立 git 仓库；根目录本身也可能是仓库（兼容普通项目）。
    tracked 来自 git ls-files；dirty 来自 git status --porcelain -z（-z 保证
    CJK 路径不被 core.quotePath 转义）。
    """
    now = time.time()
    cached = _status_cache.get(root)
    if cached and now - cached[0] < _STATUS_TTL:
        return cached[1]

    repos = []
    if os.path.isdir(os.path.join(root, ".git")):
        repos.append(root)
    for d in _region_dirs(root):
        rd = os.path.join(root, d)
        if os.path.isdir(os.path.join(rd, ".git")):
            repos.append(rd)

    snap: dict = {}
    for repo in repos:
        tracked: set = set()
        dirty: set = set()
        ok, out = _git(["ls-files"], cwd=repo)
        if ok:
            tracked = {ln.replace("\\", "/") for ln in out.splitlines() if ln}
        ok, out = _git(["status", "--porcelain", "-z", "--untracked-files=all"], cwd=repo)
        if ok and out:
            tokens = out.split("\x00")
            i = 0
            while i < len(tokens):
                rec = tokens[i]
                if not rec:
                    i += 1
                    continue
                status, path = rec[:2], rec[3:]
                p = path.replace("\\", "/")
                if status.strip():
                    dirty.add(p)
                # R/C 记录后面跟第二个 NUL 路径（目标路径），跳过对齐
                if status[0] in ("R", "C") and i + 1 < len(tokens):
                    i += 1
                i += 1
        snap[os.path.realpath(repo)] = {"tracked": tracked, "dirty": dirty}
    _status_cache[root] = (now, snap)
    return snap


def _find_repo(target: str, snap: dict):
    """找包含 target 的（最深）git 仓库；返回 (repo_abs, state) 或 (None, None)。"""
    cur = os.path.realpath(target)
    root_repo = None
    while True:
        if cur in snap:
            return cur, snap[cur]
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None, None


def _git_fields(target: str, rel_n: str, snap: dict):
    """计算节点的 tracked / dirty 字段（不在任何仓库则均为 None）。"""
    repo, state = _find_repo(os.path.dirname(target) if os.path.isfile(target) else target, snap)
    if not repo:
        return None, None
    in_repo = os.path.relpath(target, repo).replace("\\", "/")
    return (in_repo in state["tracked"], in_repo in state["dirty"])


def invalidate_status(root=None):
    """写操作后清缓存（测试与保存/删除/改名后调用）。"""
    if root is None:
        _status_cache.clear()
    else:
        _status_cache.pop(os.path.abspath(str(root)), None)


# ---------------------------------------------------------------------------
# 1) tree
# ---------------------------------------------------------------------------
def build_tree(root, depth: int = 4) -> dict:
    root_abs = _require_root(root)
    rdirs = _region_dirs(root_abs)
    enabled = _regions_enabled(root_abs)
    snap = _git_snapshot(root_abs) if (enabled or os.path.isdir(os.path.join(root_abs, ".git"))) else {}

    counter = {"n": 0, "truncated": False}

    def make_node(dir_abs: str, name: str, rel_n: str, level: int):
        counter["n"] += 1
        if counter["n"] > TREE_MAX_NODES:
            counter["truncated"] = True
            return None
        # 文件/目录可能在遍历过程中被并发写入者增删（本工程常态：两并发写入者），
        # 这里整体防住，避免 listdir 之后 stat 之前目标消失导致未捕获异常 -> HTTP 500。
        try:
            is_dir = os.path.isdir(dir_abs)
        except OSError:
            return None
        rmeta = _region_of(rel_n, rdirs) if rel_n else None
        node = {
            "path": rel_n,
            "name": name,
            "type": "dir" if is_dir else "file",
            "region": rmeta["key"] if rmeta else None,
            "region_name": rmeta["name"] if rmeta else None,
            "lang": None if is_dir else _lang_of(name),
            "writable": is_dir or _editable_text_name(name),
            "children": [],
        }
        if not is_dir:
            try:
                node["size"] = os.path.getsize(dir_abs)
            except OSError:
                # 文件在 listdir 之后、stat 之前被并发写入者删除：记为无大小而非 500。
                node["size"] = None
            try:
                tracked, dirty = _git_fields(dir_abs, rel_n, snap)
            except Exception:  # noqa: BLE001
                tracked, dirty = None, None
            node["tracked"] = tracked
            node["dirty"] = dirty
        return node

    def walk(dir_abs: str, rel_prefix: str, level: int):
        if depth >= 0 and level >= depth:
            return []
        try:
            names = sorted(os.listdir(dir_abs), key=lambda n: (not os.path.isdir(os.path.join(dir_abs, n)), n.lower()))
        except OSError:
            return []
        nodes = []
        for name in names:
            if name in _SKIP_DIRS and os.path.isdir(os.path.join(dir_abs, name)):
                continue
            if name in TREE_SKIP_DIRS and os.path.isdir(os.path.join(dir_abs, name)):
                continue
            rel_n = f"{rel_prefix}/{name}" if rel_prefix else name
            child_abs = os.path.join(dir_abs, name)
            node = make_node(child_abs, name, rel_n, level + 1)
            if node is None:
                return nodes
            if node["type"] == "dir":
                node["children"] = walk(child_abs, rel_n, level + 1)
                if counter["truncated"]:
                    nodes.append(node)
                    return nodes
            nodes.append(node)
        return nodes

    nodes = walk(root_abs, "", 0)
    return {
        "ok": True,
        "code_root": root_abs,
        "regions_enabled": enabled,
        "regions": [
            {"key": m["key"], "name": m["name"], "dir": m["dir"], "desc": m.get("desc", "")}
            for m in rdirs.values()
        ],
        "nodes": nodes,
        "truncated": counter["truncated"],
    }


# ---------------------------------------------------------------------------
# 2) file（读全文）
# ---------------------------------------------------------------------------
def read_full(root, rel) -> dict:
    target, rel_n = _resolve(root, rel, must_exist=True)
    if os.path.isdir(target):
        raise FsError(400, f"{rel_n} 是目录，不是文件。")
    size = os.path.getsize(target)
    editable = _editable_text_name(target)
    if size > WB_MAX_FILE_BYTES:
        editable = False
    with open(target, "rb") as fh:
        raw = fh.read() if editable else fh.read(min(size, 64 * 1024))
    preview_kind = None
    preview_note = None
    preview_url = None
    if _is_image_name(target):
        # 图片：不读文本、不塞假 hex。交给 /api/fs/raw 用 <img> 直接渲染原图。
        preview_kind = "image"
        preview_note = "图片预览；只读，不载入文本内容。"
        content = ""
        editable = False
        preview_url = f"/api/fs/raw?path={quote(rel_n)}"
    elif editable and b"\x00" not in raw[:8192]:
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            editable = False
    else:
        editable = False
    if not editable and preview_kind != "image":
        text_preview = None
        if (os.path.splitext(target)[1].lower() not in {".bin", ".scn"}
                and b"\x00" not in raw[:8192]):
            try:
                candidate = raw.decode("utf-8-sig")
                if sum(ord(char) < 32 and char not in "\r\n\t" for char in candidate[:8192]) <= 1:
                    text_preview = candidate
            except UnicodeDecodeError:
                pass
        if text_preview is not None:
            content = text_preview
            preview_kind = "text"
            preview_note = ("文件较大，仅显示前 64KB；此预览不可保存。" if size > WB_MAX_FILE_BYTES
                            else "此文件类型仅供查看；保存请使用对应格式工具。")
        elif size <= 32 * 1024 * 1024:
            try:
                inspected = inspect_project_file(target)
            except (DataFormatError, OSError, ValueError) as exc:
                inspected = {"format": "binary", "warning": str(exc), "hex_preview": raw[:256].hex(" ")}
            if (inspected.get("binary") is False and inspected.get("encoding") not in (None, "binary")
                    and b"\x00" not in raw[:8192]):
                preview_kind = "text"
                preview_note = ("文件较大，仅显示前 64KB；此预览不可保存。" if size > WB_MAX_FILE_BYTES
                                else "此文件类型仅供查看；保存请使用对应格式工具。")
                content = raw.decode(str(inspected["encoding"]), errors="replace")
            else:
                preview_kind = "binary"
                preview_note = str(inspected.get("warning") or "二进制文件，无法预览文本内容。")
                content = "\n".join([
                    "（二进制文件 · 只读信息，非文本内容）",
                    f"类型：{inspected.get('format', 'binary')}",
                    f"大小：{size} 字节",
                    f"SHA-256：{inspected.get('sha256', '未计算')}",
                    f"说明：{preview_note}",
                ])
        else:
            preview_kind = "binary"
            preview_note = "文件超过 32MB，仅显示文件头；不会载入或修改完整文件。"
            content = (f"文件：{rel_n}\n大小：{size} 字节\n说明：{preview_note}\n"
                       f"文件头（前 256 字节）：\n{raw[:256].hex(' ')}")
    root_abs = os.path.abspath(str(root))
    rdirs = _region_dirs(root_abs)
    rmeta = _region_of(rel_n, rdirs)
    snap = _git_snapshot(root_abs)
    tracked, dirty = (None, None)
    if snap:
        try:
            tracked, dirty = _git_fields(target, rel_n, snap)
        except Exception:  # noqa: BLE001
            tracked, dirty = None, None
    return {
        "ok": True,
        "path": rel_n,
        "content": content,
        "lang": _lang_of(rel_n) if editable else ("image" if preview_kind == "image" else "text"),
        "size": size,
        "mtime": os.path.getmtime(target),
        "region": rmeta["key"] if rmeta else None,
        "region_name": rmeta["name"] if rmeta else None,
        "writable": editable,
        "preview_kind": preview_kind,
        "preview_note": preview_note,
        "preview_url": preview_url,
        "tracked": tracked,
        "dirty": dirty,
    }


# ---------------------------------------------------------------------------
# .py 语法校验（进程内 compile，frozen 安全）
# ---------------------------------------------------------------------------
def _check_python_syntax(content: str, rel_n: str):
    try:
        compile(content, rel_n, "exec")
    except SyntaxError as e:
        raise FsError(
            422,
            f"Python 语法校验失败，已取消写入（原文件未改动）：{e.msg}（行 {e.lineno}）",
            {"lineno": e.lineno, "msg": e.msg},
        )


def _code_write_collection() -> str:
    """写侧（增量重索引 / 删除 / 重命名）必须与读侧 `search_code` 落在**同一个** chroma 集合。

    多项目模式下读侧查 `docmind_code__<pid>`（`projects.code_collection(pid)`），而写侧若
    写死 legacy `docmind_code`，就会出现「改了文件、删了文件，但项目集合里的旧块永不被清、
    新块 search_code 又看不见」的脑裂——语义首查系统性返回改前/已删代码。

    这里复用**读侧唯一事实来源** `tools._project_code_scope`：显式 `code_collection` →
    `enabled()` + `current_project_id()` → `projects.code_collection(pid)`；拿不到时回落
    legacy 名，与 search_code 里 `collection or CODE_COLLECTION_NAME` 完全同构。
    取集合名失败绝不阻断写盘（调用方本就 try/except 兜底）。
    """
    from config import CODE_COLLECTION_NAME
    try:
        from tools import _project_code_scope
        coll, _root = _project_code_scope()
        if coll:
            return str(coll)
    except Exception:  # noqa: BLE001 —— 退化成 legacy 名，宁可少一道隔离也不打死保存
        pass
    return CODE_COLLECTION_NAME


def _reindex_one(root_abs: str, target: str, rel_n: str) -> int:
    """保存后增量更新代码问答集合；调用方负责捕获所有异常。"""
    from ingest import ingest_code_file
    from vectorstore import delete_by_source

    coll = _code_write_collection()
    delete_by_source(rel_n, collection=coll)
    return ingest_code_file(target, root_abs, collection=coll)


# ---------------------------------------------------------------------------
# 3) save
# ---------------------------------------------------------------------------
def save_file(root, rel, content, if_mtime=None, reindex: bool = False) -> dict:
    if not isinstance(content, str):
        raise FsError(400, "content 必须是字符串。")
    target, rel_n = _resolve(root, rel, for_write=True)
    if os.path.isdir(target):
        raise FsError(409, f"{rel_n} 已存在且是目录。")
    ext = os.path.splitext(target)[1].lower()
    if not _editable_text_name(target):
        raise FsError(403, f"不允许写入 {ext or '无扩展名'} 文件（不在可编辑白名单内）。")
    data = content.encode("utf-8")
    if len(data) > WB_MAX_FILE_BYTES:
        raise FsError(413, f"写入内容超过上限（{WB_MAX_FILE_BYTES // 1024}KB），已取消。")
    if ext == ".py" and content.strip():
        _check_python_syntax(content, rel_n)

    existed = os.path.isfile(target)
    server_mtime = os.path.getmtime(target) if existed else None
    if existed and if_mtime is not None and server_mtime is not None:
        if abs(float(if_mtime) - server_mtime) > 1.5:
            raise FsError(
                409,
                "文件在编辑器外被修改，已阻止覆盖。请对比后选择覆盖或另存。",
                {"server_mtime": server_mtime},
            )

    changed = True
    if existed:
        with open(target, "rb") as fh:
            changed = fh.read() != data
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    # newline="" 关闭写入翻译，保留编辑器给出的原始换行
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    invalidate_status(root)

    root_abs = os.path.abspath(str(root))
    rdirs = _region_dirs(root_abs)
    rmeta = _region_of(rel_n, rdirs)
    warnings: list = []
    code_chunks = None
    if changed and reindex and ext in _CODE_EXT:
        try:
            code_chunks = _reindex_one(root_abs, target, rel_n)
        except Exception as e:  # noqa: BLE001  索引失败不回滚文件保存
            warnings.append(f"代码索引增量更新失败（可点「重新索引」修复）：{type(e).__name__}: {e}")
    # 语义标签：内容变了就把旧标签置为待刷新（人工标签保留并标 stale）
    if changed and ext in _CODE_EXT:
        try:
            import semantic_tags as stags
            stags.invalidate_scan(root_abs)
            stags.on_saved(root_abs, rel_n)
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "path": rel_n,
        "mtime": os.path.getmtime(target),
        "changed": changed,
        "region": rmeta["key"] if rmeta else None,
        "region_name": rmeta["name"] if rmeta else None,
        "reindexed": {"code_chunks": code_chunks} if code_chunks is not None else None,
        "reindex_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 4) create
# ---------------------------------------------------------------------------
def create_path(root, rel, kind: str, content: str = "") -> dict:
    kind = (kind or "").strip().lower()
    if kind not in ("file", "folder"):
        raise FsError(400, "type 必须是 file 或 folder。")
    target, rel_n = _resolve(root, rel, for_write=True)
    if os.path.exists(target):
        raise FsError(409, f"{rel_n} 已存在。")
    if kind == "folder":
        os.makedirs(target, exist_ok=False)
        return {"ok": True, "path": rel_n, "node": {"path": rel_n, "name": os.path.basename(target), "type": "dir", "children": []}}
    ext = os.path.splitext(target)[1].lower()
    if not _editable_text_name(target):
        raise FsError(403, f"不允许创建 {ext or '无扩展名'} 文件（不在可编辑白名单内）。")
    if content and not isinstance(content, str):
        raise FsError(400, "content 必须是字符串。")
    data = content or ""
    if ext == ".py" and data.strip():
        _check_python_syntax(data, rel_n)
    if len(data.encode("utf-8")) > WB_MAX_FILE_BYTES:
        raise FsError(413, "文件内容超过上限。")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    invalidate_status(root)
    if kind == "file":
        try:
            import semantic_tags as stags
            stags.invalidate_scan(os.path.abspath(str(root)))
        except Exception:  # noqa: BLE001
            pass
    return {
        "ok": True,
        "path": rel_n,
        "node": {
            "path": rel_n, "name": os.path.basename(target), "type": "file",
            "lang": _lang_of(rel_n), "writable": True, "size": len(data.encode("utf-8")),
            "tracked": False, "dirty": True,
        },
    }


# ---------------------------------------------------------------------------
# 5) rename / move
# ---------------------------------------------------------------------------
def rename_path(root, rel, new_rel, reindex: bool = False) -> dict:
    target, rel_n = _resolve(root, rel, must_exist=True, for_write=True)
    dst, new_n = _resolve(root, new_rel, for_write=True)
    if os.path.exists(dst):
        raise FsError(409, f"目标已存在：{new_n}")
    # 目标父目录必须存在（避免 rename 顺带隐式建目录，行为更可预期）
    if not os.path.isdir(os.path.dirname(dst)):
        raise FsError(404, f"目标文件夹不存在：{os.path.dirname(new_n)}")

    root_abs = os.path.abspath(str(root))
    rdirs = _region_dirs(root_abs)
    if _regions_enabled(root_abs):
        src_region = _region_of(rel_n, rdirs)
        dst_region = _region_of(new_n, rdirs)
        if (src_region or {}).get("key") != (dst_region or {}).get("key"):
            raise FsError(
                403,
                "禁止跨分区移动文件（会破坏分区依赖契约）。请让 Agent 使用 dev_refactor "
                "走契约校验后搬移，或在同一分区内整理。",
            )

    snap = _git_snapshot(root_abs)
    repo, state = _find_repo(target, snap)
    git_renamed = False
    if repo:
        in_repo_old = os.path.relpath(target, repo).replace("\\", "/")
        in_repo_new = os.path.relpath(dst, repo).replace("\\", "/")
        if in_repo_old in state["tracked"]:
            ok, _ = _git(["mv", in_repo_old, in_repo_new], cwd=repo)
            git_renamed = ok
    if not git_renamed:
        shutil.move(target, dst)
    invalidate_status(root_abs)

    warnings: list = []
    if reindex and os.path.isfile(dst) and os.path.splitext(dst)[1].lower() in _CODE_EXT:
        try:
            from vectorstore import delete_by_source
            delete_by_source(rel_n, collection=_code_write_collection())
            _reindex_one(root_abs, dst, new_n)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"代码索引更新失败（可点「重新索引」修复）：{type(e).__name__}: {e}")
    try:
        import semantic_tags as stags
        stags.invalidate_scan(root_abs)
        stags.on_renamed(root_abs, rel_n, new_n)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "from": rel_n, "to": new_n, "git_renamed": git_renamed, "reindex_warnings": warnings}


# ---------------------------------------------------------------------------
# 6) delete
# ---------------------------------------------------------------------------
def _iter_files(dir_abs: str):
    for dp, dns, fns in os.walk(dir_abs):
        # .git 永不进入删除清单（PROTECTED_PARTS 已拦，双保险）
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            yield os.path.join(dp, fn)


def delete_path(root, rel, recursive: bool = False, force: bool = False) -> dict:
    target, rel_n = _resolve(root, rel, must_exist=True, for_write=True)

    if os.path.isdir(target):
        try:
            entries = sorted(os.listdir(target))
        except OSError:
            entries = []
        entries = [e for e in entries if e != ".git"]
        if entries and not recursive:
            raise FsError(409, f"文件夹非空（{len(entries)} 项），删除需 recursive=true 二次确认。",
                          {"sample": entries[:10], "count": len(entries)})
        # 目录删除不可逆性检查：内含未跟踪文件时必须 force
        snap = _git_snapshot(os.path.abspath(str(root)))
        untracked = []
        for f in _iter_files(target):
            repo, state = _find_repo(f, snap)
            if not repo:
                untracked.append(os.path.relpath(f, target).replace("\\", "/"))
            else:
                in_repo = os.path.relpath(f, repo).replace("\\", "/")
                if in_repo not in state["tracked"]:
                    untracked.append(os.path.relpath(f, target).replace("\\", "/"))
        if untracked and not force:
            raise FsError(
                409,
                f"文件夹中有 {len(untracked)} 个未纳入 git 的文件，删除不可恢复，需 force=true 二次确认。",
                {"untracked_sample": untracked[:10], "untracked_count": len(untracked)},
            )
        shutil.rmtree(target)
        invalidate_status(root)
        try:
            import semantic_tags as stags
            stags.invalidate_scan(os.path.abspath(str(root)))
            stags.on_deleted(os.path.abspath(str(root)), rel_n)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "deleted": rel_n, "recoverable": not untracked}

    # 单文件
    snap = _git_snapshot(os.path.abspath(str(root)))
    repo, state = _find_repo(target, snap)
    tracked = bool(repo and os.path.relpath(target, repo).replace("\\", "/") in state["tracked"])
    if not tracked and not force:
        raise FsError(409, "该文件未纳入 git，删除后不可恢复，需 force=true 二次确认。")
    os.remove(target)
    invalidate_status(root)
    if os.path.splitext(target)[1].lower() in _CODE_EXT:
        try:
            from vectorstore import delete_by_source
            delete_by_source(rel_n, collection=_code_write_collection())
        except Exception:  # noqa: BLE001
            pass
    try:
        import semantic_tags as stags
        stags.invalidate_scan(os.path.abspath(str(root)))
        stags.on_deleted(os.path.abspath(str(root)), rel_n)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "deleted": rel_n, "recoverable": tracked}


# ---------------------------------------------------------------------------
# 7) gitlog
# ---------------------------------------------------------------------------
def git_log(root, rel, limit: int = 20) -> dict:
    target, rel_n = _resolve(root, rel, must_exist=True)
    root_abs = os.path.abspath(str(root))
    probe = target if os.path.isdir(target) else os.path.dirname(target)
    root_real = os.path.realpath(root_abs)
    cur = os.path.realpath(probe)
    repo = None
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            repo = cur
            break
        if cur == root_real:
            break
        parent = os.path.dirname(cur)
        if parent == cur or not cur.startswith(root_real + os.sep):
            break
        cur = parent
    if not repo:
        raise FsError(400, "该文件不在任何 git 仓库内（分区尚未初始化 git）。")
    in_repo = os.path.relpath(target, repo).replace("\\", "/")
    limit = max(1, min(int(limit or 20), 100))
    ok, out = _git(
        ["log", f"-{limit}", "--pretty=format:%H%x09%h%x09%ad%x09%an%x09%s",
         "--date=iso-strict", "--", in_repo],
        cwd=repo,
    )
    if not ok:
        raise FsError(400, f"git log 失败：{out[:300]}")
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) != 5:
            continue
        full, short, dt, author, subject = parts
        ts = None
        try:
            ts = datetime.fromisoformat(dt).timestamp()
        except Exception:  # noqa: BLE001
            ts = None
        commits.append({"hash": short, "full_hash": full, "time": ts, "time_raw": dt,
                        "author": author, "message": subject})
    return {"ok": True, "path": rel_n, "commits": commits}


# ---------------------------------------------------------------------------
# P4：仓库级「改动总览」（只读）
# ---------------------------------------------------------------------------
# 与 _git_snapshot 的区别：那个只产出 {path} 两个集合给文件树打状态点，
# 丢了「是新增还是修改还是删除」。总览必须保留状态字母，所以单独跑。
# XY 两列（index 与 worktree）合并成一个用户语义：
#   untracked(??) / added(A) / deleted(D) / renamed(R) / modified(M) / conflict(U,AA,DD…)
_CHANGE_ORDER = {"conflict": 0, "modified": 1, "added": 2, "deleted": 3,
                 "renamed": 4, "untracked": 5}
_CHANGE_LABEL = {"conflict": "冲突", "modified": "修改", "added": "新增",
                 "deleted": "删除", "renamed": "改名", "untracked": "未跟踪"}
# 单仓库最多返回的改动条目（防巨型仓库拖垮前端；截断时置 truncated 标志）
WB_CHANGES_MAX = 500


def _classify_change(xy: str) -> str:
    """把 porcelain 的两列状态码归一成 6 种用户语义之一。"""
    x, y = xy[0], xy[1]
    if x == "?" or y == "?":
        return "untracked"
    if "U" in xy or xy in ("AA", "DD"):
        return "conflict"
    if "R" in xy or "C" in xy:
        return "renamed"
    if x == "A" or y == "A":
        return "added"
    if x == "D" or y == "D":
        return "deleted"
    return "modified"


def _repo_for_root(root_abs: str) -> list:
    """列出本次总览要扫的仓库根（绝对路径）。

    与 _git_snapshot 保持同一口径：根目录本身可能是仓库（普通项目），
    每个 region 目录也可能是独立仓库。两者都收，去重。
    """
    repos = []
    root_abs = os.path.abspath(str(root_abs))
    if os.path.isdir(os.path.join(root_abs, ".git")):
        repos.append(root_abs)
    for d in _region_dirs(root_abs):
        rd = os.path.join(root_abs, d.replace("/", os.sep))
        if os.path.isdir(os.path.join(rd, ".git")):
            repos.append(rd)
    # 去重且保持顺序（realpath 归一，避免大小写/软链造成的重复项）
    seen, out = set(), []
    for r in repos:
        key = os.path.realpath(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def git_changes(root) -> dict:
    """只读：列出所有 git 仓库里的未提交改动。

    返回 ``{"ok": True, "repos": [...], "total": n, "truncated": bool}``，
    每个 repo 含 ``{"name", "dir", "branch", "files": [...]}``。
    ``dir`` 是相对 code_root 的正斜杠路径（根仓库为 ``""``），供前端跳转/分组。

    **完全不写盘**：只跑 ``rev-parse`` / ``status`` / ``branch`` 三个只读命令，
    也不碰 5 秒状态缓存（那是给文件树用的，语义更粗）。
    """
    root_abs = os.path.abspath(str(root))
    if not os.path.isdir(root_abs):
        raise FsError(400, "代码库根目录不存在。")

    repos_out: list = []
    total = 0
    truncated = False

    for repo in _repo_for_root(root_abs):
        rel_dir = os.path.relpath(repo, root_abs).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        name = os.path.basename(repo) or os.path.basename(root_abs)

        # 分支名：detached HEAD 时 symbolic-ref 失败，退回短哈希
        branch = ""
        ok, out = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
        if ok and out.strip():
            branch = out.strip()
            if branch == "HEAD":  # detached
                ok2, out2 = _git(["rev-parse", "--short", "HEAD"], cwd=repo)
                branch = f"(detached {out2.strip()})" if ok2 else "(detached)"
        if not branch:
            branch = "(未知)"

        ok, out = _git(["status", "--porcelain", "-z", "--untracked-files=all"],
                       cwd=repo)
        if not ok:
            # 单个仓库读失败不该让整个总览 500；记下原因让前端能显示
            repos_out.append({"name": name, "dir": rel_dir, "branch": branch,
                              "files": [], "error": out[:200]})
            continue

        files: list = []
        if out:
            tokens = out.split("\x00")
            i = 0
            while i < len(tokens):
                rec = tokens[i]
                if not rec:
                    i += 1
                    continue
                xy, path = rec[:2], rec[3:]
                p = path.replace("\\", "/")
                kind = _classify_change(xy)
                item = {"path": p, "kind": kind, "label": _CHANGE_LABEL[kind],
                        "xy": xy}
                # R/C 记录：**第一个 token 是新路径**，NUL 之后那个才是旧路径
                # （实测 `git mv old.txt new.txt` → `R  new.txt\0old.txt\0`）。
                # 这跟"源在前"的直觉相反，写反了会显示成 `新 -> 旧` 的倒序。
                if xy[0] in ("R", "C") and i + 1 < len(tokens):
                    nxt = tokens[i + 1]
                    if nxt:
                        item["path"] = nxt.replace("\\", "/")   # 源（旧名）
                        item["path_to"] = p                     # 目标（新名）
                    i += 1
                files.append(item)
                i += 1

        # 稳定排序：冲突最优先（要人处理），其次按语义组，组内按路径
        files.sort(key=lambda f: (_CHANGE_ORDER.get(f["kind"], 9), f["path"]))

        if len(files) > WB_CHANGES_MAX:
            files = files[:WB_CHANGES_MAX]
            truncated = True

        total += len(files)
        repos_out.append({"name": name, "dir": rel_dir, "branch": branch,
                          "files": files})

    return {"ok": True, "repos": repos_out, "total": total,
            "truncated": truncated, "empty": total == 0}


# ---------------------------------------------------------------------------
# P3：git 一键回滚（文件级）+ 历史版本读取/恢复
# ---------------------------------------------------------------------------
# 只接受提交哈希（gitlog 返回值），拒绝任意 ref 表达式/选项注入
_GIT_REF_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _find_repo_bounded(root_abs: str, target: str):
    """从 target（目录取自身/文件取父目录）向上、止于 code_root，找最近的 .git。"""
    root_real = os.path.realpath(root_abs)
    cur = os.path.realpath(target if os.path.isdir(target) else os.path.dirname(target))
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        if cur == root_real:
            break
        parent = os.path.dirname(cur)
        if parent == cur or not cur.startswith(root_real + os.sep):
            break
        cur = parent
    return None


def _git_file_ctx(root, rel):
    """回滚类操作的统一解析与护栏：返回 (target, rel_n, repo, in_repo_posix)。"""
    target, rel_n = _resolve(root, rel, must_exist=True)
    _ensure_writable(rel_n)  # 契约文件 / .git 等 403
    if os.path.isdir(target):
        raise FsError(400, f"不是文件：{rel_n}（回滚只支持单个文件）。")
    repo = _find_repo_bounded(os.path.abspath(str(root)), target)
    if not repo:
        raise FsError(400, "该文件不在任何 git 仓库内（分区尚未初始化 git），无法回滚。")
    in_repo = os.path.relpath(target, repo).replace("\\", "/")
    return target, rel_n, repo, in_repo


def revert_file(root, rel) -> dict:
    """放弃单个已跟踪文件的全部未提交改动（含暂存区），恢复为 HEAD 版本。

    语义对齐用户「AI 改坏了一键回滚」：checkout HEAD -- <path> 同时重置
    index 与工作区。未跟踪新文件 HEAD 中不存在，拒绝并引导走删除。
    """
    target, rel_n, repo, in_repo = _git_file_ctx(root, rel)
    # 破坏性操作不走 5 秒状态缓存，直接实时查 git（ls-files/status 均为本地只读命令）
    ok_tr, _ = _git(["ls-files", "--error-unmatch", "--", in_repo], cwd=repo)
    if not ok_tr:
        raise FsError(409, "该文件还未纳入 git（是新文件），无法用 git 回滚；不需要它请直接删除。")
    # 已 git add 但 HEAD 中不存在的「新增暂存」也不能 checkout HEAD
    ok_head, _ = _git(["cat-file", "-e", f"HEAD:{in_repo}"], cwd=repo)
    if not ok_head:
        raise FsError(409, "该文件是本次新增的（HEAD 提交中不存在），无法回滚到 HEAD；不需要它请直接删除。")
    ok_st, st = _git(["status", "--porcelain", "-z", "--", in_repo], cwd=repo)
    if not (ok_st and st):
        return {"ok": True, "path": rel_n, "reverted": False, "reason": "clean",
                "mtime": os.path.getmtime(target)}
    ok, err = _git(["checkout", "-q", "HEAD", "--", in_repo], cwd=repo)
    if not ok:
        raise FsError(400, f"git 回滚失败：{err[:300]}")
    invalidate_status(root)
    if os.path.splitext(target)[1].lower() in _CODE_EXT:
        try:
            import semantic_tags as stags
            stags.on_saved(os.path.abspath(str(root)), rel_n)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "path": rel_n, "reverted": True,
            "mtime": os.path.getmtime(target)}


def _git_show_blob(repo: str, spec: str) -> str:
    """git show 原样取 blob 文本：不能走 regions._git（它会 rstrip 掉末尾换行）。"""
    try:
        proc = subprocess.run(
            ["git", "show", spec], cwd=repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except FileNotFoundError:
        raise FsError(400, _git_missing_hint())
    except Exception as e:  # noqa: BLE001
        raise FsError(400, f"无法执行 git：{e}")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise FsError(404, f"该提交中不存在此文件，或提交无效：{msg[:200]}")
    return proc.stdout


def git_show_at(root, rel, ref) -> dict:
    """读取文件在某次提交时的内容（git show <hash>:<path>），仅供文本可编辑类型。"""
    target, rel_n, repo, in_repo = _git_file_ctx(root, rel)
    ref = str(ref or "").strip().lower()
    if not _GIT_REF_RE.match(ref):
        raise FsError(400, "ref 只能是 git 提交哈希（4-40 位十六进制）。")
    if not _editable_text_name(target):
        raise FsError(403, "历史版本仅支持可编辑的文本文件。")
    out = _git_show_blob(repo, f"{ref}:{in_repo}")
    if len(out.encode("utf-8")) > WB_MAX_FILE_BYTES:
        raise FsError(413, f"该历史版本超过上限（{WB_MAX_FILE_BYTES // 1024}KB），无法在工作台恢复。")
    return {"ok": True, "path": rel_n, "ref": ref, "content": out,
            "size": len(out.encode("utf-8"))}


def restore_file_at(root, rel, ref, reindex: bool = True) -> dict:
    """把文件恢复成某次提交时的内容：只写工作区，不动 HEAD 与提交历史。

    取 git show 内容后复用 save_file 的全部写护栏（白名单/大小/.py 语法校验/
    mtime 强制覆盖/增量索引），恢复后表现为一次普通未提交修改，可再次回滚。
    """
    shown = git_show_at(root, rel, ref)
    saved = save_file(root, rel, shown["content"], None, reindex=reindex)
    saved["ref"] = ref
    saved["restored"] = True
    return saved


# ---------------------------------------------------------------------------
# P1：符号语义地图
# ---------------------------------------------------------------------------
SYMBOL_MAP_MAX_FILES = 1000


def file_symbols(root, rel):
    """单文件符号信封（路径走沙箱解析）。"""
    target, rel_n = _resolve(root, rel, must_exist=True)
    if os.path.isdir(target):
        raise FsError(400, f"不是文件：{rel_n}")
    env = symlib.file_symbols(target)
    if env is None:
        raise FsError(404, f"读取失败：{rel_n}")
    env = dict(env)
    env["ok"] = True
    env["path"] = rel_n
    return env


def build_symbol_map(root):
    """遍历代码库构建全项目符号地图：files[].symbols 扁平表 + 分区归属 + 统计。"""
    root_abs = _require_root(root)
    rdirs = _region_dirs(root_abs)
    files, skipped = [], 0
    by_kind: dict = {}

    for dp, dns, fns in os.walk(root_abs):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(fns):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _CODE_EXT:
                continue
            if len(files) >= SYMBOL_MAP_MAX_FILES:
                skipped += 1
                continue
            fp = os.path.join(dp, fn)
            try:
                if os.path.getsize(fp) > _MAX_CODE_FILE:
                    continue
                env = symlib.file_symbols(fp)
                if env is None:
                    continue
            except Exception:  # noqa: BLE001
                continue
            rel = os.path.relpath(fp, root_abs).replace("\\", "/")
            rmeta = _region_of(rel, rdirs)
            syms = env.get("symbols") or []
            for s in syms:
                by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
            files.append({
                "rel": rel,
                "lang": env.get("lang", ""),
                "region": rmeta["key"] if rmeta else "",
                "region_name": rmeta["name"] if rmeta else "",
                "class_name": env.get("class_name", ""),
                "extends": env.get("extends", ""),
                "doc": env.get("doc", ""),
                "symbols": syms,
            })

    files.sort(key=lambda f: f["rel"])
    return {
        "ok": True,
        "code_root": root_abs,
        "regions_enabled": bool(rdirs),
        "files": files,
        "stats": {
            "files": len(files),
            "symbols": sum(by_kind.values()),
            "by_kind": by_kind,
            "skipped": skipped,
        },
    }


# ---------------------------------------------------------------------------
# P1：关系图（继承边 + 场景挂载边 + 高置信调用边）
# ---------------------------------------------------------------------------

_GD_RES_PATH = re.compile(r'^"?res://(.+?)"?$')
_TSCN_EXT_LINE = re.compile(r"^\[ext_resource\s+(.*?)\]\s*$")
_TSCN_NODE_LINE = re.compile(r'^\[node\b')
_TSCN_SCRIPT_REF = re.compile(r'^\s*script\s*=\s*ExtResource\(\s*"([^"]+)"\s*\)')
_ATTR_PATH_RE = re.compile(r'path="([^"]*)"')
_ATTR_TYPE_RE = re.compile(r'type="([^"]*)"')


def _base_simple_name(base: str) -> str:
    """Foo / pkg.mod.Foo / Foo[T] -> Foo（用于和项目内类名匹配）。"""
    b = base.strip()
    b = b.split("[", 1)[0]
    return b.split(".")[-1].strip()


def _split_top_commas(s: str):
    """按顶层逗号切分 Python bases 串（泛型参数里的逗号不切）。"""
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch in "[(<":
            depth += 1
        elif ch in "])>":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _scene_script_refs(text: str):
    """解析 .tscn：返回 [(node 段内 script 赋值行号(1 基), 目标 rel)]。

    仅取 ext_resource（type=Script 或路径像脚本）声明的 id→res 路径映射，
    再在各 [node] 段内找 ``script = ExtResource("id")`` 赋值。
    """
    refs = {}  # ext_resource id -> rel
    edges = []  # (line, rel)
    cur_node_line = 0
    for i, ln in enumerate(text.split("\n"), start=1):
        s = ln.strip()
        m = _TSCN_EXT_LINE.match(s)
        if m:
            attrs = m.group(1)
            mp = _ATTR_PATH_RE.search(attrs)
            mt = _ATTR_TYPE_RE.search(attrs)
            if not mp:
                continue
            path = mp.group(1)
            typ = mt.group(1) if mt else ""
            if not path.startswith("res://"):
                continue
            mid = re.search(r'id="([^"]*)"', attrs)
            if not mid:
                continue
            ext = os.path.splitext(path)[1].lower()
            if typ == "Script" or ext in (".gd", ".cs"):
                refs[mid.group(1)] = path[len("res://"):].replace("\\", "/")
            continue
        if s.startswith("["):
            cur_node_line = i if _TSCN_NODE_LINE.match(s) else 0
            continue
        if cur_node_line:
            m = _TSCN_SCRIPT_REF.match(ln)
            if m:
                rel = refs.get(m.group(1))
                if rel:
                    edges.append((i, rel))
    return edges


def _mask_gdscript(text: str) -> str:
    """Blank strings and comments while preserving length/newlines for line offsets."""
    out = list(text)
    quote = None
    escaped = False
    for i, ch in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            elif ch != "\n":
                out[i] = " "
        elif ch in ("'", '"'):
            quote = ch
            out[i] = " "
        elif ch == "#":
            j = i
            while j < len(text) and text[j] != "\n":
                out[j] = " "
                j += 1
    return "".join(out)


def build_relation_graph(root):
    """全项目关系图：用户节点（类/匿名脚本/场景）+ 高置信关系边。

    - inherits：GDScript ``extends`` / Python class bases（项目内类直连，
      引擎与第三方基类聚合成 external 节点；隐式 RefCounted/object 不出边）；
    - mounts：.tscn 场景节点经 ExtResource 挂载脚本的组成边。
    """
    root_abs = _require_root(root)
    rdirs = _region_dirs(root_abs)

    nodes = []          # 用户节点 + 外部节点，按稳定顺序
    edges = []
    node_index = {}     # 节点 id -> 在 nodes 中的下标
    user_by_name = {}   # 项目内类名 -> 节点 id（gd class_name / py 顶层类）
    gd_user_by_name = {}
    py_user_by_name = {}
    gd_by_rel = {}      # .gd rel -> 节点 id（含匿名脚本，res:// 继承与挂载都指向它）
    py_by_rel_name = {} # (rel, class name) -> node id
    methods_by_node = {}  # node id -> 顶层方法名
    py_classes = []     # (节点 id, bases detail 串)
    scene_rels = set()
    skipped = 0

    def add_node(node):
        nid = node["id"]
        if nid in node_index:
            return node_index[nid]
        node_index[nid] = len(nodes)
        nodes.append(node)
        return node_index[nid]

    def external_id(kind, label):
        return f"ext:{kind}:{label}"

    def ensure_external(kind, label, sub=""):
        nid = external_id(kind, label)
        if nid not in node_index:
            add_node({
                "id": nid, "label": label, "sub": sub, "kind": kind,
                "rel": "", "line": 0, "region": "", "region_name": "",
                "external": True, "doc": "",
            })
        return nid

    def add_edge(source, target, kind, label, line=0):
        edges.append({
            "source": source, "target": target, "kind": kind,
            "label": label, "line": line,
        })

    # ---- 第一遍：建用户节点 ----
    walked = []  # (rel, ext, abs_path)
    for dp, dns, fns in os.walk(root_abs):
        dns[:] = [d for d in dns if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in sorted(fns):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _CODE_EXT:
                continue
            if len(walked) >= SYMBOL_MAP_MAX_FILES:
                skipped += 1
                continue
            fp = os.path.join(dp, fn)
            try:
                if os.path.getsize(fp) > _MAX_CODE_FILE:
                    continue
            except OSError:
                continue
            walked.append((os.path.relpath(fp, root_abs).replace("\\", "/"), ext, fp))

    for rel, ext, fp in walked:
        if ext not in (".gd", ".py", ".tscn"):
            continue
        rmeta = _region_of(rel, rdirs)
        region = rmeta["key"] if rmeta else ""
        region_name = rmeta["name"] if rmeta else ""
        try:
            env = symlib.file_symbols(fp)
        except Exception:  # noqa: BLE001
            env = None
        if env is None:
            continue

        if ext == ".gd":
            cn = (env.get("class_name") or "").strip()
            doc = env.get("doc", "")
            label = cn or os.path.splitext(os.path.basename(rel))[0]
            nid = f"gd:{rel}"
            add_node({
                "id": nid, "label": label,
                "sub": "" if cn else rel,
                "kind": "class" if cn else "script",
                "rel": rel, "line": 1, "region": region,
                "region_name": region_name, "external": False, "doc": doc,
            })
            gd_by_rel[rel] = nid
            if cn and cn not in user_by_name:
                user_by_name[cn] = nid
            if cn:
                gd_user_by_name.setdefault(cn, nid)
            methods_by_node[nid] = {
                s.get("name") for s in (env.get("symbols") or [])
                if s.get("kind") == "function" and not s.get("parent")
            }

        elif ext == ".py":
            for s in env.get("symbols") or []:
                if s.get("kind") != "class":
                    continue
                nid = f"py:{rel}:{s['name']}"
                add_node({
                    "id": nid, "label": s["name"], "sub": rel,
                    "kind": "class", "rel": rel, "line": s["start"],
                    "region": region, "region_name": region_name,
                    "external": False, "doc": s.get("doc", ""),
                })
                py_classes.append((nid, s.get("detail", "")))
                user_by_name.setdefault(s["name"], nid)
                py_user_by_name.setdefault(s["name"], nid)
                py_by_rel_name[(rel, s["name"])] = nid
                methods_by_node[nid] = {
                    child.get("name") for child in (env.get("symbols") or [])
                    if child.get("kind") == "function" and child.get("parent") == s["name"]
                }

        elif ext == ".tscn":
            nid = f"scene:{rel}"
            add_node({
                "id": nid,
                "label": os.path.splitext(os.path.basename(rel))[0],
                "sub": rel, "kind": "scene", "rel": rel, "line": 1,
                "region": region, "region_name": region_name,
                "external": False, "doc": "",
            })
            scene_rels.add(rel)

    # ---- 第二遍：继承边 ----
    for rel, ext, fp in walked:
        if ext == ".gd":
            source = gd_by_rel.get(rel)
            if not source:
                continue
            env = symlib.file_symbols(fp) or {}
            ref = (env.get("extends") or "").strip()
            if not ref:
                continue  # 隐式 RefCounted/Object 不画
            mr = _GD_RES_PATH.match(ref)
            if mr:
                target_rel = mr.group(1).replace("\\", "/")
                target = gd_by_rel.get(os.path.normpath(target_rel).replace("\\", "/"))
                if target:
                    add_edge(source, target, "inherits", "extends")
                continue
            simple = ref.split(".")[-1]
            target = user_by_name.get(simple)
            if target:
                add_edge(source, target, "inherits", "extends")
            else:
                add_edge(source, ensure_external("engine", ref),
                         "inherits", "extends")

        elif ext == ".py":
            env = symlib.file_symbols(fp) or {}
            class_lines = {s["name"]: s["start"] for s in (env.get("symbols") or [])
                           if s.get("kind") == "class"}
            for nid, detail in py_classes:
                if not nid.startswith("py:" + rel + ":"):
                    continue
                cls_name = nid.rsplit(":", 1)[-1]
                for base in _split_top_commas(detail):
                    simple = _base_simple_name(base)
                    if not simple or simple == "object":
                        continue
                    target = user_by_name.get(simple)
                    if target:
                        add_edge(nid, target, "inherits", "bases",
                                 line=class_lines.get(cls_name, 0))
                    else:
                        add_edge(nid, ensure_external("external", simple, "python"),
                                 "inherits", "bases",
                                 line=class_lines.get(cls_name, 0))

    # ---- 第三遍：场景挂载边 ----
    for rel in sorted(scene_rels):
        fp = os.path.join(root_abs, *rel.split("/"))
        try:
            with open(fp, encoding="utf-8-sig", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        source = f"scene:{rel}"
        seen = set()
        for line, target_rel in _scene_script_refs(text):
            target = gd_by_rel.get(target_rel)
            if not target:
                continue
            key = (source, target, "mounts")
            if key in seen:
                continue
            seen.add(key)
            add_edge(source, target, "mounts", "挂载", line=line)

    # ---- 第四遍：调用边（仅能解析到项目内类和已定义方法的调用）----
    # 节点粒度图中，相同源/目标的多次调用合并成一条边，保留首行与方法列表。
    call_edges = {}

    def add_call(source, target, method, line):
        if not source or not target or source == target:
            return
        if method not in methods_by_node.get(target, set()):
            return
        key = (source, target, "calls")
        edge = call_edges.get(key)
        if edge is None:
            edge = {
                "source": source, "target": target, "kind": "calls",
                "label": "调用", "line": line, "methods": [method],
            }
            call_edges[key] = edge
            edges.append(edge)
        elif method not in edge["methods"]:
            edge["methods"].append(method)

    gd_var_type = re.compile(
        r"^\s*(?:@[\w.]+(?:\([^\n]*\))?\s*)*(?:static\s+)?var\s+"
        r"(\w+)\s*:\s*([A-Za-z_]\w*)\b"
    )
    gd_func = re.compile(r"^\s*(?:static\s+)?func\s+\w+\s*\((.*?)\)")
    gd_param = re.compile(r"(?:^|,)\s*(\w+)\s*:\s*([A-Za-z_]\w*)\b")
    gd_call = re.compile(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(")

    for rel, ext, fp in walked:
        if ext == ".gd":
            source = gd_by_rel.get(rel)
            if not source:
                continue
            try:
                with open(fp, encoding="utf-8-sig", errors="ignore") as f:
                    raw = f.read()
            except OSError:
                continue
            masked = _mask_gdscript(raw)
            # 类成员映射只收列 0 声明：缩进的局部 var 不能进全局映射，
            # 否则其类型会泄漏到其他函数，给未定义接收者造出假调用边。
            field_types = {}
            for ln in masked.splitlines():
                if ln[:1] in (" ", "\t"):
                    continue
                m = gd_var_type.match(ln)
                if m and m.group(2) in gd_user_by_name:
                    field_types[m.group(1)] = m.group(2)
            active_types = dict(field_types)
            for line_no, ln in enumerate(masked.splitlines(), start=1):
                m = gd_func.match(ln)
                if m:
                    active_types = dict(field_types)
                    for pm in gd_param.finditer(m.group(1)):
                        if pm.group(2) in gd_user_by_name:
                            active_types[pm.group(1)] = pm.group(2)
                m = gd_var_type.match(ln)
                if m and m.group(2) in gd_user_by_name:
                    active_types[m.group(1)] = m.group(2)
                for cm in gd_call.finditer(ln):
                    receiver, method = cm.groups()
                    target = gd_user_by_name.get(receiver)
                    if target is None:
                        target = gd_user_by_name.get(active_types.get(receiver, ""))
                    if target:
                        add_call(source, target, method, line_no)

        elif ext == ".py":
            try:
                with open(fp, encoding="utf-8-sig", errors="ignore") as f:
                    tree = ast.parse(f.read(), filename=fp)
            except (OSError, SyntaxError):
                continue

            def annotation_name(node):
                try:
                    return _base_simple_name(ast.unparse(node))
                except Exception:  # noqa: BLE001
                    return ""

            imported = {}
            module_aliases = {}
            for item in tree.body:
                if isinstance(item, ast.ImportFrom):
                    for alias in item.names:
                        target_name = alias.name.split(".")[-1]
                        if target_name in py_user_by_name:
                            imported[alias.asname or target_name] = target_name
                elif isinstance(item, ast.Import):
                    for alias in item.names:
                        module_aliases[alias.asname or alias.name.split(".")[-1]] = alias.name

            for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                source = py_by_rel_name.get((rel, cls.name))
                if not source:
                    continue
                fields = {}
                for child in cls.body:
                    if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                        typ = annotation_name(child.annotation)
                        if typ in py_user_by_name:
                            fields[child.target.id] = typ
                for fn in (n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    var_types = dict(fields)
                    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
                        if arg.annotation:
                            typ = annotation_name(arg.annotation)
                            if typ in py_user_by_name:
                                var_types[arg.arg] = typ
                    for node in ast.walk(fn):
                        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                            typ = annotation_name(node.annotation)
                            if typ in py_user_by_name:
                                var_types[node.target.id] = typ
                        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                            continue
                        receiver = node.func.value
                        target = None
                        if isinstance(receiver, ast.Name):
                            target = py_user_by_name.get(imported.get(receiver.id, receiver.id))
                            if target is None:
                                target = py_user_by_name.get(var_types.get(receiver.id, ""))
                        elif (isinstance(receiver, ast.Attribute) and isinstance(receiver.value, ast.Name)
                              and receiver.value.id in module_aliases):
                            target = py_user_by_name.get(receiver.attr)
                        if target:
                            add_call(source, target, node.func.attr, node.lineno)

    by_kind_edges = {}
    for e in edges:
        by_kind_edges[e["kind"]] = by_kind_edges.get(e["kind"], 0) + 1
    user_count = sum(1 for n in nodes if not n["external"])

    return {
        "ok": True,
        "code_root": root_abs,
        "regions_enabled": bool(rdirs),
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "files": len(walked),
            "nodes": len(nodes),
            "user_nodes": user_count,
            "external_nodes": len(nodes) - user_count,
            "edges": len(edges),
            "edges_by_kind": by_kind_edges,
            "skipped": skipped,
        },
    }


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
router = APIRouter(prefix="/api/fs", tags=["workbench-fs"])


def _runtime_root() -> str:
    root = (get_runtime("code_root") or CODE_ROOT or "").strip()
    return _require_root(root)


def _err(e: FsError) -> JSONResponse:
    body = {"ok": False, "error": e.message}
    if e.extra:
        body.update(e.extra)
    return JSONResponse(body, status_code=e.status)


class SaveReq(BaseModel):
    path: str
    content: str
    if_mtime: float | None = None
    reindex: bool = True
    task_id: str = ""


class CreateReq(BaseModel):
    path: str
    type: str = "file"
    content: str = ""


class RenameReq(BaseModel):
    path: str
    new_path: str
    reindex: bool = True


class DeleteReq(BaseModel):
    path: str
    recursive: bool = False
    force: bool = False


class RevertReq(BaseModel):
    path: str


class RestoreReq(BaseModel):
    path: str
    ref: str
    reindex: bool = True


@router.get("/tree")
def tree_ep(depth: int = 4):
    try:
        return build_tree(_runtime_root(), depth=depth)
    except FsError as e:
        return _err(e)


@router.get("/file")
def file_ep(path: str):
    try:
        return read_full(_runtime_root(), path)
    except FsError as e:
        return _err(e)


@router.get("/raw")
def raw_ep(path: str):
    """按原样吐图片字节，供前端 <img> 预览。仅限 WB_IMAGE_EXTS，
    路径经 _resolve 沙箱校验（拒绝 .. / 绝对路径 / URL），不可用来读源码/密钥等任意文件。"""
    try:
        root = _runtime_root()
        target, rel_n = _resolve(root, path, must_exist=True)
        if os.path.isdir(target):
            raise FsError(400, f"{rel_n} 是目录，不是文件。")
        if os.path.splitext(target)[1].lower() not in WB_IMAGE_EXTS:
            raise FsError(403, "仅允许预览图片文件。")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        return FileResponse(target, media_type=ctype,
                            headers={"Cache-Control": "no-store"})
    except FsError as e:
        return _err(e)


@router.post("/save")
def save_ep(req: SaveReq):
    try:
        root = _runtime_root()
        if req.task_id:
            from game_workbench import list_tasks, validate_task_scope
            tasks = [t for t in list_tasks(root) if str(t.get('id')) == req.task_id]
            if not tasks: raise FsError(403, "任务不存在，拒绝写入。")
            scope = validate_task_scope(root, {**tasks[0], "files": [req.path]})
            if not scope["ok"]: raise FsError(403, "文件不在当前任务允许范围内。", {"scope": scope})
        return save_file(root, req.path, req.content, req.if_mtime, reindex=req.reindex)
    except FsError as e:
        return _err(e)


@router.post("/create")
def create_ep(req: CreateReq):
    try:
        return create_path(_runtime_root(), req.path, req.type, req.content)
    except FsError as e:
        return _err(e)


@router.post("/rename")
def rename_ep(req: RenameReq):
    try:
        return rename_path(_runtime_root(), req.path, req.new_path, reindex=req.reindex)
    except FsError as e:
        return _err(e)


@router.post("/delete")
def delete_ep(req: DeleteReq):
    try:
        return delete_path(_runtime_root(), req.path, req.recursive, req.force)
    except FsError as e:
        return _err(e)


@router.get("/gitlog")
def gitlog_ep(path: str, limit: int = 20):
    try:
        return git_log(_runtime_root(), path, limit=limit)
    except FsError as e:
        return _err(e)


@router.get("/git-changes")
def git_changes_ep():
    """P4：仓库级未提交改动总览（只读）。前端「改动总览」面板的数据源。"""
    try:
        return git_changes(_runtime_root())
    except FsError as e:
        return _err(e)


@router.post("/revert")
def revert_ep(req: RevertReq):
    """P3：放弃该文件全部未提交改动，恢复为 HEAD 版本。"""
    try:
        return revert_file(_runtime_root(), req.path)
    except FsError as e:
        return _err(e)


@router.get("/git-show")
def git_show_ep(path: str, ref: str):
    """P3：读取文件在某次提交时的内容（历史版本预览）。"""
    try:
        return git_show_at(_runtime_root(), path, ref)
    except FsError as e:
        return _err(e)


@router.post("/restore-at")
def restore_at_ep(req: RestoreReq):
    """P3：把文件恢复成某次提交时的内容（只写工作区，不动提交历史）。"""
    try:
        return restore_file_at(_runtime_root(), req.path, req.ref, reindex=req.reindex)
    except FsError as e:
        return _err(e)


@router.get("/symbols")
def symbols_ep(path: str):
    """单文件符号大纲。"""
    try:
        return file_symbols(_runtime_root(), path)
    except FsError as e:
        return _err(e)


@router.get("/symbol-map")
def symbol_map_ep():
    """全项目符号语义地图（符号扁平表 + 分区归属 + 统计）。"""
    try:
        return build_symbol_map(_runtime_root())
    except FsError as e:
        return _err(e)


@router.get("/relation-graph")
def relation_graph_ep():
    """全项目关系图（继承边 + 场景挂载组成边）。"""
    try:
        return build_relation_graph(_runtime_root())
    except FsError as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 阶段 1：分区卡片（首页用）——在 regions.list_regions 基础上补
# 未提交改动计数 + 最近一次提交摘要，全部走本地 git 只读命令。
# ---------------------------------------------------------------------------
def build_region_cards(root) -> dict:
    root_abs = _require_root(root)
    from regions import list_regions
    snap = _git_snapshot(root_abs)
    root_is_repo = os.path.isdir(os.path.join(root_abs, ".git"))

    def _repo_of(region_dir_rel: str):
        rd = os.path.join(root_abs, *region_dir_rel.split("/"))
        if os.path.isdir(os.path.join(rd, ".git")):
            return rd, True
        if root_is_repo:
            return root_abs, False
        return None, False

    def _last_commit(repo: str):
        ok, out = _git(
            ["log", "-1", "--pretty=format:%H%x09%h%x09%s%x09%an%x09%ad",
             "--date=iso-strict"],
            cwd=repo,
        )
        if not ok or not out.strip():
            return None
        parts = out.split("\t", 4)
        if len(parts) != 5:
            return None
        full, short, subject, author, dt = parts
        ts = None
        try:
            ts = datetime.fromisoformat(dt).timestamp()
        except Exception:  # noqa: BLE001
            pass
        return {"hash": short, "full_hash": full, "message": subject,
                "author": author, "time": ts, "time_raw": dt}

    cards = []
    for r in list_regions(root_abs):
        repo, own_repo = _repo_of(r["dir"]) if r["exists"] else (None, False)
        dirty_count = 0
        if repo is not None:
            state = snap.get(os.path.realpath(repo))
            if state:
                if own_repo:
                    dirty_count = len(state["dirty"])
                else:
                    prefix = r["dir"].replace("\\", "/").strip("/") + "/"
                    dirty_count = sum(1 for p in state["dirty"]
                                      if p == r["dir"].strip("/") or p.startswith(prefix))
        last = _last_commit(repo) if repo is not None else None
        cards.append({
            **r,
            "dirty_count": dirty_count,
            "last_commit": last,
            "own_repo": own_repo,
        })
    return {"ok": True, "code_root": root_abs, "regions": cards}


@router.get("/region-cards")
def region_cards_ep():
    """分区首页卡片：契约状态/文件数/未提交改动数/最近一次提交。"""
    try:
        return build_region_cards(_runtime_root())
    except FsError as e:
        return _err(e)


# ---------------------------------------------------------------------------
# 阶段 1：语义业务标签 + 大白话定位
# ---------------------------------------------------------------------------
class TagUpdateReq(BaseModel):
    path: str
    tags: list[str]
    summary: str = ""


@router.get("/semantic-tags")
def semantic_tags_ep(refresh: int = 0, limit: int = 60):
    """业务标签。refresh=1 时现场跑增量标注（LLM 批量 + 规则降级），可能耗时数十秒。"""
    try:
        root = _runtime_root()
        import semantic_tags as stags
        if refresh:
            return stags.ensure_tags(root, limit=max(1, min(int(limit or 60), 300)))
        return stags.tag_status(root)
    except FsError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001  LLM/依赖失败不搞挂整个面板
        return JSONResponse(
            {"ok": False, "error": f"语义标签生成失败：{type(e).__name__}: {e}"},
            status_code=200,
        )


@router.post("/semantic-tags/update")
def semantic_tags_update_ep(req: TagUpdateReq):
    """人工修正标签：写入后 origin=manual，自动刷新不再覆盖。"""
    try:
        root = _runtime_root()
        target, rel_n = _resolve(root, req.path, must_exist=True)
        if os.path.isdir(target):
            raise FsError(400, "只能给文件打标签。")
        import semantic_tags as stags
        stags.invalidate_scan(root)
        return stags.manual_update(root, rel_n, req.tags, req.summary)
    except FsError as e:
        return _err(e)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": f"文件不存在：{e}"}, status_code=404)


@router.get("/locate")
def locate_ep(q: str, limit: int = 20):
    """大白话定位代码：业务标签/文件名/符号 + 向量语义混合检索。"""
    try:
        root = _runtime_root()
        import semantic_tags as stags
        q = (q or "").strip()
        if not q:
            return {"ok": True, "query": q, "files": [], "regions": [], "total": 0}
        return stags.locate(root, q, limit=max(1, min(int(limit or 20), 50)))
    except FsError as e:
        return _err(e)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            {"ok": False, "error": f"定位失败：{type(e).__name__}: {e}"},
            status_code=200,
        )
