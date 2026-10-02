"""把「代码 import 了什么」与「依赖清单声明了什么」对账，并能把缺的补进去。

这一段是 vibecoding 里最常见的死法，而且失败点很偏：Agent 在工作树里 `import` 了一个新包，
本地因为「恰好装过」而一切正常，于是它认为改动完成了；可是提交里没有依赖声明，
别人 clone 下来、或者 CI 拉一个干净检出，第一句 import 就 `ModuleNotFoundError`。
`dev_headcheck` 能**发现**这件事（它把 HEAD 挂成干净检出真 import 一遍），但发现之后
没有工具能修——从「知道缺了」到「补上」之间是断的。本模块补的就是这一段。

三条设计取向，和仓库其它地方一致：

1. **对账用 `ast`，不用正则**。`import re` 与注释里的 `# import re`、字符串里的
   `"import os"` 必须是两回事；正则扫全文会把它们算成依赖，然后建议往 requirements.txt
   里塞标准库。
2. **映射靠证据，不靠猜**。import 名与发行名并非一一对应（`yaml`→`PyYAML`、
   `cv2`→`opencv-python`、`sklearn`→`scikit-learn`）。本模块按可信度依次找证据：
   `importlib.metadata.packages_distributions()`（**本机实际装着的元数据**，最硬，
   实测 `websocket`→`websocket-client`、`webview`→`pywebview`、`yaml`→`PyYAML`
   全靠它）→ 已声明依赖的 `Provides-Extra`（`psycopg[pool]` 确实带出 `psycopg-pool`）
   → 查表 → 同名。都不成立就归入 `unknown` **并明确报出来**，绝不猜一个名字写进依赖
   清单——猜错的后果是装了个不相干的包，而真正缺的那个仍然缺着。
3. **解析不了的文件必须显式报出来**。一个语法坏掉的 .py 扫不出 import，
   它不是「没有依赖」，它是「没查」。`unparsed` 单列，扫描被上限截断时 `truncated=True`，
   两者都会让报告不能被读成「依赖没问题」。

**已知边界（写在前面而不是藏起来）**：第 2 条的头号证据取自**当前解释器的已安装
元数据**，所以同一个仓库在两台机器上答案可能不同。这是有意的取舍，也正是它比"猜"
强的地方——「本机装得上、清单里没写」正是要抓的那一类。但反过来说：一个模块在
**本机没装**（常见的惰性/可选依赖）时只能落到查表或同名两条弱证据上，此时每条结论
都会带上 `confidence`，`installed: false` 明确告诉调用方这条是弱证据。

写操作（`action: apply`）只有一处：往依赖清单**追加**缺失行。不重排、不改版本、不删
任何现有行——那是人的决定。追加走原子写（临时文件 + `os.replace`），并在结果里回显
真正写进去的每一行。
"""
from __future__ import annotations

import ast
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

#: 一次扫描最多看多少个 .py。超了就把 `truncated` 置真——报告因此不能被读成「查过了」。
MAX_FILES = 4000
#: 一次最多列多少条缺失。多出来只给计数与清单前若干条。
MAX_MISSING = 120
#: 一次最多列多少条「声明了但没扫到 import」。
MAX_UNUSED = 120

#: 需求清单文件名，按优先级。只读第一个存在的（`declared.source` 会回显用的是哪个）。
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")
PYPROJECT = "pyproject.toml"

#: 永远不扫的目录名。
SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tmp", "tmp",
    "dist", "build", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox",
    ".docmind", ".docmind_sessions", ".claude", ".trae", ".chroma", ".workbuddy",
    ".uv-cache", ".uv-cache-codex", "_archived_builds", "site-packages",
    ".eggs", ".next", ".nuxt", "coverage", "htmlcov",
})


def pep503(name: str) -> str:
    """PEP 503 归一化：`Foo.Bar_baz` 与 `foo-bar-baz` 是同一个包。"""
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


#: import 名 ≠ 发行名的地方。这是**最后一条**证据——前两条（本机元数据、已声明依赖的
#: extras）都问不到时才轮到这里。只收确定对应；宁可漏（进 unknown 让人来看），
#: 也不要猜错（装了个不相干的包，真缺的那个还缺着）。
#:
#: 收录标准：这一行要么来自该包自己的文档/pyproject，要么是本仓 requirements.txt 里
#: 已经声明过的（`import docx` ← `python-docx` 这类，在本表里对着写）。
ALIASES: dict[str, str] = {
    # 本仓 requirements.txt 里已声明的：import 名 → 声明里的名字
    "yaml": "PyYAML",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "serial": "pyserial",
    "usb": "pyusb",
    "magic": "python-magic",
    "git": "GitPython",
    "fitz": "PyMuPDF",
    "MySQLdb": "mysqlclient",
    "attr": "attrs",
    "jwt": "PyJWT",
    "OpenGL": "PyOpenGL",
    "grpc": "grpcio",
    "wx": "wxPython",
    "zmq": "pyzmq",
    "Xlib": "python-xlib",
    "dns": "dnspython",
    # pywin32 的一整族顶层模块：本机元数据里只有 win32com/pythoncom 有记录，
    # win32api/win32con/win32gui 这些没有（实测 packages_distributions 返回 None），
    # 所以它们只能靠这一条。
    "win32api": "pywin32",
    "win32com": "pywin32",
    "win32con": "pywin32",
    "win32gui": "pywin32",
    "win32ui": "pywin32",
    "win32file": "pywin32",
    "win32process": "pywin32",
    "win32service": "pywin32",
    "winerror": "pywin32",
    "pywintypes": "pywin32",
    "winxpgui": "pywin32",
    "pythoncom": "pywin32",
    "isapi": "pywin32",
    "win32evtlog": "pywin32",
}

_INSTALLED_CACHE: dict[str, list[str]] | None = None


def _installed_map() -> dict[str, list[str]] | None:
    """`{顶层 import 名: [发行名, ...]}`，来自本机**已安装**的发行元数据。

    这是最硬的一条证据：它不是我们推断的，是 pip 装完写进 `.dist-info` 的事实。
    取不到（老解释器 / 环境异常）时返回 None，调用方降级到弱证据。
    """
    global _INSTALLED_CACHE
    if _INSTALLED_CACHE is not None:
        return _INSTALLED_CACHE or None
    try:
        from importlib.metadata import packages_distributions
        raw = packages_distributions()
    except Exception:  # noqa: BLE001 - 取不到就当没有，绝不因此报错
        _INSTALLED_CACHE = {}
        return None
    table = {str(k).split(".", 1)[0]: sorted(str(v) for v in (vals or ()))
             for k, vals in (raw or {}).items()}
    _INSTALLED_CACHE = table
    return table


def _extra_provides(declared: dict[str, str]) -> dict[str, set[str]]:
    """`{被带出的发行名: {声明它的依赖名, ...}}`——只从**已声明**的依赖的 extras 出发。

    `psycopg[binary,pool]` 的 extra `pool` 确实会装 `psycopg-pool`（实测本机
    `psycopg_pool` 由发行包 `psycopg-pool` 提供，而 requirements.txt 里写的是
    `psycopg[binary,pool]`）。只问已声明的那些，不装的东西问不到也不该问——
    没声明的依赖提供的 extra 与本工具要修的问题无关。
    """
    out: dict[str, set[str]] = {}
    for name in declared:
        try:
            from importlib.metadata import metadata
            fields = metadata(name).get_all("Provides-Extra") or []
        except Exception:  # noqa: BLE001 - 未安装 / 无元数据，跳过
            continue
        if not fields:
            continue
        try:
            from importlib.metadata import metadata as _md
            requires = _md(name).get_all("Requires-Dist") or []
        except Exception:  # noqa: BLE001
            continue
        for req in requires:
            match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)[^;]*;\s*extra\s*==\s*[\"']([^\"']+)",
                             str(req))
            if match and match.group(2) in set(fields):
                out.setdefault(pep503(match.group(1)), set()).add(name)
    return out


def distribution_for(module: str, *, declared: dict[str, str] | None = None,
                     installed: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """把顶层 import 名映射成发行名。**返回字典**而不是元组，因为证据强度要一起带走。

    返回 `{"distribution": str, "confidence": str, "installed": bool, "via": str}`。
    `confidence` 取值：

    * `installed` —— 本机装着的发行元数据直接指认，最硬；
    * `extra` —— 已声明依赖的 extra 会带出它；
    * `alias` —— 查表命中；
    * `exact` —— PEP 503 归一后同名（**弱证据**：本机没装时最常见，所以同时
      带 `installed: false`，调用方与人都该知道这条没被证据背书）；
    * `unknown` —— 没有依据。`distribution` 为空，调用方**不许**写进依赖清单。
    """
    top = str(module or "").strip()
    blank = {"distribution": "", "confidence": "unknown", "installed": False, "via": ""}
    if not top:
        return blank
    table = _installed_map() if installed is None else installed
    if table:
        hits = table.get(top) or []
        if hits:
            return {"distribution": hits[0], "confidence": "installed",
                    "installed": True, "via": hits[0]}
    if declared:
        via_extra = _extra_provides(declared).get(pep503(top))
        if via_extra:
            return {"distribution": top, "confidence": "extra",
                    "installed": bool((table or {}).get(top)),
                    "via": sorted(via_extra)[0]}
    if top in ALIASES:
        return {"distribution": ALIASES[top], "confidence": "alias",
                "installed": bool((table or {}).get(top)), "via": "ALIASES"}
    norm = pep503(top)
    # 发行名形状：PEP 503 允许数字开头，但 `import 2fa` 在语法上就不可能——
    # 也就是说，一个以数字开头的顶层名**永远不是**第三方包，而是本地文件被起了个
    # 非常规的名字（实测有仓库这么干）。归 unknown，让人来看，不去猜。
    if not norm or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", norm):
        return blank
    # 同名时给**归一化**后的名字：pip 认的是 PEP 503 形式，
    # `sentence_transformers` 该写进清单的是 `sentence-transformers`。
    return {"distribution": norm, "confidence": "exact",
            "installed": bool((table or {}).get(top)), "via": "same-name"}


def stdlib_names() -> frozenset[str]:
    """本解释器的标准库顶层模块名。

    已知的取舍：`sys.stdlib_module_names` 会把 `test`/`code`/`select` 这类真占着一个
    发行包的名字也算进来，于是「真缺的那个依赖」可能被误判成标准库而不出现在 missing 里。
    这条边界写在这里而不是假装没有：`declared` 里有对应声明时以声明为准（见
    `reconcile`），要彻底消掉它得在**干净环境**里逐个试 import，那是另一件事。
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return frozenset(names)
    # 老解释器兜底：只有当本机确实没有那个模块时才算标准库。
    import importlib.util
    return frozenset(n for n in getattr(sys, "builtin_module_names", ())
                     if importlib.util.find_spec(n) is None)


def _read(path: Path) -> tuple[str, str]:
    try:
        return path.read_text(encoding="utf-8"), ""
    except (OSError, UnicodeDecodeError) as exc:
        return "", "%s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------- #
# 声明侧：读依赖清单
# --------------------------------------------------------------------------- #

_REQ_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\s*\[[^\]]*\])?"                       # name[extra]
    r"(?:\s*(?P<spec>(?:[=<>!~]=?|===)\s*[^;#]+))?"  # version specifier
    r"(?:\s*;\s*(?P<marker>[^#]+))?"             # environment marker
    r"\s*(?:#.*)?$")


def parse_requirements(text: str) -> dict[str, Any]:
    """解析 requirements 文本。返回 `{name: spec, _skipped: [...]}`。"""
    names: dict[str, str] = {}
    skipped: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            # `-r other.txt` / `-e .` / `--index-url` / `-c constraints.txt`
            skipped.append(line.split()[0])
            continue
        hit = _REQ_LINE.match(line)
        if not hit:
            skipped.append(line[:60])
            continue
        names[pep503(hit.group("name"))] = (hit.group("spec") or "").strip()
    return {"names": names, "skipped": skipped}


def parse_pyproject(text: str) -> dict[str, Any]:
    """从 pyproject.toml 取声明的依赖。只读，解析不了就说解析不了。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        return {"names": {}, "skipped": [], "error": "本解释器没有 tomllib，读不了 pyproject.toml。"}
    try:
        data = tomllib.loads(text or "")
    except Exception as exc:  # noqa: BLE001 - tomllib 的异常类型随版本变
        return {"names": {}, "skipped": [], "error": "pyproject.toml 解析失败：%s" % exc}
    found: dict[str, str] = {}

    def harvest(items: Iterable[Any]) -> None:
        for item in items or ():
            text_item = str(item)
            head = text_item.split(";", 1)[0].strip()
            hit = _REQ_LINE.match(head)
            if hit:
                found[pep503(hit.group("name"))] = (hit.group("spec") or "").strip()

    project = data.get("project") if isinstance(data, dict) else None
    if isinstance(project, dict):
        harvest(project.get("dependencies") or ())
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                harvest(group or ())
    poetry = ((data.get("tool") or {}).get("poetry") or {}) if isinstance(data, dict) else {}
    if isinstance(poetry, dict):
        section = poetry.get("dependencies")
        if isinstance(section, dict):
            for name, spec in section.items():
                if pep503(name) == "python":
                    continue
                found[pep503(name)] = str(spec)
    return {"names": found, "skipped": [], "error": ""}


def declared_dependencies(root: str) -> dict[str, Any]:
    """找到并读出这个项目声明的依赖。找不到清单也要说清楚，不能当成「没声明任何东西」。"""
    base = Path(root)
    for name in REQUIREMENT_FILES:
        path = base / name
        if path.is_file():
            text, err = _read(path)
            if err:
                return {"source": name, "names": {}, "skipped": [],
                        "error": "%s 读不了：%s" % (name, err)}
            parsed = parse_requirements(text)
            parsed["source"] = name
            return parsed
    path = base / PYPROJECT
    if path.is_file():
        text, err = _read(path)
        if err:
            return {"source": PYPROJECT, "names": {}, "skipped": [],
                    "error": "%s 读不了：%s" % (PYPROJECT, err)}
        parsed = parse_pyproject(text)
        parsed["source"] = PYPROJECT
        return parsed
    return {"source": "", "names": {}, "skipped": [],
            "error": "这个项目里没有 %s 也没有 %s——没有依赖清单可比对，"
                     "下面的结果只说明代码 import 了什么，不代表依赖是齐的。"
                     % (" / ".join(REQUIREMENT_FILES), PYPROJECT)}


# --------------------------------------------------------------------------- #
# 使用侧：扫 import
# --------------------------------------------------------------------------- #

def _should_skip(rel: str) -> bool:
    parts = Path(rel).parts
    return any(part in SKIP_DIRS or part.endswith(".egg-info") for part in parts)


def iter_python_files(root: str, paths: list[str] | None = None
                      ) -> tuple[list[str], bool]:
    """列出要扫的 .py（仓库相对路径，正斜杠）。返回 `(文件列表, 是否被上限截断)`。"""
    base = Path(root)
    found: list[str] = []
    truncated = False
    if paths:
        for raw in paths:
            full = (base / str(raw).replace("\\", "/")).resolve()
            try:
                full.relative_to(base.resolve())
            except ValueError:
                continue
            if full.is_file() and full.suffix == ".py":
                found.append(str(full.relative_to(base)).replace("\\", "/"))
            elif full.is_dir():
                for path in sorted(full.rglob("*.py")):
                    rel = str(path.relative_to(base)).replace("\\", "/")
                    if _should_skip(rel):
                        continue
                    if len(found) >= MAX_FILES:
                        truncated = True
                        break
                    found.append(rel)
        return sorted(set(found)), truncated
    for path in sorted(base.rglob("*.py")):
        rel = str(path.relative_to(base)).replace("\\", "/")
        if _should_skip(rel) or not path.is_file():
            continue
        if len(found) >= MAX_FILES:
            truncated = True
            break
        found.append(rel)
    return found, truncated


def scan_imports(root: str, paths: list[str] | None = None
                 ) -> dict[str, Any]:
    """扫出每个顶层 import 名 → 引用它的文件清单。

    相对 import（`from . import x`）一律跳过：那是包内的，不可能来自依赖清单。
    `__future__` 与标准库在 `reconcile` 里滤掉，不在这里判断——保持这一层只做
    「代码确实 import 了什么」这一件事。
    """
    files, truncated = iter_python_files(root, paths)
    importers: dict[str, set[str]] = {}
    unparsed: list[dict[str, str]] = []
    for rel in files:
        text, err = _read(Path(root) / rel)
        if err:
            unparsed.append({"path": rel, "error": err})
            continue
        try:
            tree = ast.parse(text, filename=rel)
        except (SyntaxError, ValueError, RecursionError) as exc:
            unparsed.append({"path": rel, "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    if top:
                        importers.setdefault(top, set()).add(rel)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # 相对 import
                    continue
                if node.module:
                    top = node.module.split(".", 1)[0]
                    if top:
                        importers.setdefault(top, set()).add(rel)
    return {"importers": {k: sorted(v) for k, v in sorted(importers.items())},
            "files_scanned": len(files), "files_total": len(files) - len(unparsed),
            "unparsed": unparsed, "truncated": truncated}


def local_modules(root: str, *, extra_roots: list[str] | None = None) -> set[str]:
    """本仓库自己的顶层模块名（根目录的 .py、含 `__init__.py` 的目录）。

    没有这一步，`import tools` 会被算成一个待补的依赖，然后往 requirements.txt 里
    塞一条 `tools`——而那个包在 PyPI 上是真实存在的，装上以后更难查。

    `extra_roots` 是**嵌套项目根**：本仓的 `game_demo/starvoyager/` 是一个自带
    `__init__.py` 的包，`game_demo/main.py` 里 `from starvoyager.config import …`
    是**项目内**引用而不是第三方依赖。只扫仓库根会把它误报成「缺一个叫 starvoyager
    的发行包」——实测这条误报真的出现过。
    """
    names: set[str] = set()
    roots = [Path(root)] + [Path(item) for item in (extra_roots or [])]
    for base in roots:
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".py":
                names.add(entry.stem)
            elif entry.is_dir():
                # 只有自带 __init__.py 的才算一个可 import 的包；普通目录（tests/、
                # scripts/）不是模块名，报出去会变成另一类假阳性。
                if (entry / "__init__.py").is_file():
                    names.add(entry.name)
    return names


def nested_project_roots(root: str) -> list[str]:
    """找出「根目录直接子目录 + 它自带 requirements/pyproject」这种嵌套项目。

    只看根目录一层（不递归）：再深就是数据目录而不是另一个项目，递归会既慢又容易
    把 vendor 进来的第三方源码当成自己的模块。

    注意这条**认不出来**本仓的 `game_demo/`（它没有依赖清单），那种嵌套项目由
    `resolvable_locally` 按「导入方自己的目录」那条规则处理，不靠这里。
    """
    base = Path(root)
    found: list[str] = []
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        if any((entry / name).is_file() for name in REQUIREMENT_FILES) \
                or (entry / PYPROJECT).is_file():
            found.append(str(entry))
    return found


def resolvable_locally(root: str, importer: str, module: str) -> bool:
    """这个 `import` 能不能从**导入方自己所在的目录**解析到。

    这是 Python 真实的语义：`python game_demo/main.py` 会把 `game_demo/` 放进
    `sys.path`，于是 `from starvoyager.config import load_balance` 里的
    `starvoyager` 解析到 `game_demo/starvoyager/`——它是**项目内**的代码，不是
    第三方发行包。只按仓库根算 local_modules 会把它误报成「缺一个叫 starvoyager
    的包」（这条误报实测出现过）。

    从导入方目录往上逐级试，止于仓库根：再往上是别人的地盘，不该算成本项目的代码。
    """
    if not module or "/" in module or "\\" in module:
        return False
    base = Path(os.path.abspath(root))
    directory = (base / str(importer).replace("\\", "/")).parent
    try:
        directory.resolve().relative_to(base)
    except (ValueError, OSError):
        return False
    while True:
        if (directory / (module + ".py")).is_file():
            return True
        if (directory / module / "__init__.py").is_file():
            return True
        if directory == base or directory.parent == directory:
            return False
        try:
            directory.resolve().relative_to(base)
        except (ValueError, OSError):
            return False
        directory = directory.parent


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #

def reconcile(root: str, *, paths: list[str] | None = None) -> dict[str, Any]:
    """把「import 了什么」与「声明了什么」对账。这是本模块的入口。"""
    base = os.path.abspath(root or "")
    if not base or not os.path.isdir(base):
        return {"ok": False, "error": "代码根目录不存在或未配置。"}
    declared = declared_dependencies(base)
    scan = scan_imports(base, paths)
    nested = nested_project_roots(base)
    local = local_modules(base, extra_roots=nested)
    for extra in nested:
        local |= local_modules(extra)
    stdlib = stdlib_names()

    missing: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    declared_names = declared.get("names") or {}

    for module, files in (scan.get("importers") or {}).items():
        if module in local or module in stdlib:
            continue
        # 逐个导入方判「能不能从它自己目录解析到」——同一个模块对 A 文件是本地的、
        # 对 B 文件可能真是第三方（实测 game_demo/starvoyager 就是这种情况）。
        external = [rel for rel in files if not resolvable_locally(base, rel, module)]
        if not external:
            continue
        found = distribution_for(module, declared=declared_names)
        dist = found["distribution"]
        if not dist:
            unknown.append({"module": module, "importers": files[:6],
                            "file_count": len(files)})
            continue
        # 声明里有（或归一后有）就不算缺。
        if pep503(dist) in declared_names or pep503(module) in declared_names:
            continue
        missing.append({"module": module, "distribution": dist,
                        "confidence": found["confidence"],
                        "installed": found["installed"], "via": found["via"],
                        "importers": external[:6], "file_count": len(external)})

    # 「声明了但没扫到 import」只是**线索**，不是问题：pytest/ruff 这类是被命令行
    # 用起来的，脚本与被跳过的目录里也可能有。所以它不进 ok 的判定。
    used_dists = {pep503(row["distribution"]) for row in missing} \
        | {pep503(m) for m in (scan.get("importers") or {})}
    unused = [{"distribution": name, "spec": spec}
              for name, spec in sorted(declared_names.items())
              if name not in used_dists and name not in {pep503(m) for m in local}]

    notes: list[str] = []
    if declared.get("error"):
        notes.append(declared["error"])
    if declared.get("skipped"):
        notes.append("依赖清单里有 %d 行没按依赖名解析（-r/-e/--index-url 之类）：%s"
                     % (len(declared["skipped"]), ", ".join(sorted(set(declared["skipped"]))[:5])))
    if scan.get("truncated"):
        notes.append("只扫了前 %d 个 .py（上限），后面的没看——这份清单不能当「全查过了」。"
                     % MAX_FILES)
    if scan.get("unparsed"):
        notes.append("%d 个 .py 解析失败，它们的 import 没被计入（不是「没有依赖」）：%s"
                     % (len(scan["unparsed"]),
                        ", ".join("%s(%s)" % (row["path"], row["error"][:40])
                                  for row in scan["unparsed"][:5])))
    if unknown:
        notes.append("%d 个 import 映射不到发行名，需要人确认（我没有猜）：%s"
                     % (len(unknown), ", ".join(row["module"] for row in unknown[:8])))
    if nested:
        notes.append("另外把这些嵌套项目根当成了自己的模块来源（没往它们那儿找依赖）：%s"
                     % ", ".join(os.path.basename(item) for item in nested[:5]))
    weak = [row["module"] for row in missing if not row.get("installed")]
    if weak:
        notes.append("这 %d 条是**弱证据**（本机没装对应发行包，映射靠同名或查表）：%s"
                     % (len(weak), ", ".join(weak[:8])))

    # fail-closed：只看得出「没发现缺什么」，但扫描本身不完整时，不能报干净。
    complete = not scan.get("truncated") and not scan.get("unparsed") and not declared.get("error")
    return {
        "ok": not missing,
        "complete": complete,
        "error": "",
        "declared_source": declared.get("source") or "",
        "declared_count": len(declared_names),
        "declared": sorted(declared_names),
        "missing": missing[:MAX_MISSING],
        "missing_total": len(missing),
        "unknown": unknown[:MAX_MISSING],
        "unknown_total": len(unknown),
        "unused": unused[:MAX_UNUSED],
        "unused_total": len(unused),
        "nested_roots": [os.path.basename(item) for item in nested],
        "files_scanned": scan.get("files_total", 0),
        "unparsed": scan.get("unparsed", [])[:20],
        "truncated": bool(scan.get("truncated")),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# 写：只往清单追加
# --------------------------------------------------------------------------- #

def missing_lines(result: dict[str, Any]) -> list[str]:
    """给出可以追加的依赖行。`unknown` 的**不产出**——映射不确定就不能写。"""
    rows = [row for row in result.get("missing") or [] if row.get("distribution")]
    if not rows:
        return []
    stamp = time.strftime("%Y-%m-%d")
    lines = ["", "# dev_deps %s：以下包被代码 import，但依赖清单里没有声明。" % stamp]
    for row in sorted(rows, key=lambda item: pep503(item["distribution"])):
        where = ", ".join(row["importers"][:3])
        if row.get("file_count", 0) > len(row.get("importers") or []):
            where += " 等 %d 处" % row["file_count"]
        lines.append("#   %s ← %s" % (row["module"], where))
        lines.append(row["distribution"])
    return lines


def apply(root: str, result: dict[str, Any], *, source: str = "") -> dict[str, Any]:
    """把缺失依赖**追加**到依赖清单。不改版本、不删行、不重排。

    追加而不是改写，是因为「该用哪个版本」是人的决定；这里只补「漏了声明」这一条，
    而且补完仍然没有版本约束——要约束就手写一行覆盖它。
    """
    base = Path(os.path.abspath(root or ""))
    # 先看「有没有要写的东西」，再看「往哪写」：没东西可写时抱怨「没有依赖清单」是
    # 答非所问（实测踩过：unknown 项让 missing_lines 为空，错误却报成了找不到清单）。
    lines = missing_lines(result)
    if not lines:
        return {"ok": False, "applied": [], "error": "没有可追加的缺失依赖。"}
    wanted = source or result.get("declared_source") or ""
    target = base / wanted if wanted else None
    if target is None or not target.is_file():
        return {"ok": False, "applied": [], "error":
                "这个项目里没有可写入的依赖清单（找过 %s / %s）；本工具只往【已存在】的"
                "清单里追加，不会替你新建一个——「这个项目怎么管依赖」是人的决定。"
                % (" / ".join(REQUIREMENT_FILES), PYPROJECT)}
    existing, err = _read(target)
    if err:
        return {"ok": False, "applied": [], "error": "%s 读不了：%s" % (target.name, err)}
    body = existing if existing.endswith("\n") else existing + "\n"
    tmp = target.with_name(target.name + ".deps.tmp")
    try:
        tmp.write_text(body + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return {"ok": False, "applied": [], "error": "写 %s 失败：%s" % (target.name, exc)}
    written = [row for row in lines if row and not row.startswith("#")]
    return {"ok": True, "applied": written,
            "written_to": str(target), "error": ""}


def render(result: dict[str, Any], applied: dict[str, Any] | None = None) -> str:
    if result.get("error"):
        return "dev_deps 未完成：" + str(result["error"])
    source = result.get("declared_source") or "（没有依赖清单）"
    missing = result.get("missing") or []
    lines: list[str] = []
    if missing:
        lines.append("发现 %d 个【被 import 但没声明】的依赖（清单：%s，已声明 %d 条）："
                     % (result["missing_total"], source, result["declared_count"]))
        for row in missing[:20]:
            tail = "" if row["file_count"] <= len(row["importers"]) else \
                "（共 %d 处）" % row["file_count"]
            lines.append("  %s → %s%s" % (row["module"], row["distribution"],
                                          "  ← " + ", ".join(row["importers"][:2]) + tail))
        if result["missing_total"] > len(missing):
            lines.append("  …还有 %d 条没列" % (result["missing_total"] - len(missing)))
        lines.append("这就是「本地能跑、干净装不上」的那一类问题：补声明用 "
                     "`action: apply`（只追加，不改版本）。")
    else:
        lines.append("没有发现「import 了但没声明」的依赖（清单：%s，扫了 %d 个 .py）。"
                     % (source, result.get("files_scanned", 0)))
        if not result.get("complete"):
            lines.append("但这份结论**不完整**，见下面的提示——别把它读成「依赖没问题」。")
    if applied:
        if applied.get("ok"):
            lines.append("已往 %s 追加 %d 行：%s（没写版本约束，版本由你定）" % (
                os.path.basename(str(applied.get("written_to") or "")),
                len(applied.get("applied") or []), ", ".join(applied.get("applied") or [])))
        else:
            lines.append("没有写入：" + str(applied.get("error") or "未知原因"))
    if result.get("unknown"):
        lines.append("映射不到发行名、需要你确认的 import（我没有猜）：%s" % ", ".join(
            row["module"] for row in result["unknown"][:10]))
    unused = result.get("unused") or []
    if unused:
        lines.append("声明了但这次没扫到 import 的（**只是线索**：pytest/ruff 这类是命令行"
                     "用的，也可能用在被跳过的目录里）：%s"
                     % ", ".join("%s%s" % (row["distribution"],
                                            row["spec"] or "") for row in unused[:10]))
    for note in result.get("notes") or []:
        lines.append("提示: " + note)
    return "\n".join(lines)


__all__ = ["ALIASES", "MAX_FILES", "MAX_MISSING", "MAX_UNUSED", "PYPROJECT",
           "REQUIREMENT_FILES", "SKIP_DIRS", "apply", "declared_dependencies",
           "distribution_for", "iter_python_files", "local_modules", "missing_lines",
           "parse_pyproject", "parse_requirements", "pep503", "reconcile", "render",
           "scan_imports", "stdlib_names"]
