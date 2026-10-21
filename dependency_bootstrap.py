"""Install and update source-runtime dependencies before Agent Workbench starts.

The frozen distribution already carries Python dependencies, so this helper is
for source checkouts and build machines. It is intentionally stdlib-only so it
can run before importing the application and can create a missing .venv.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


STATE_RELATIVE = Path(".docmind") / "dependency-state.json"
PYTHON_REQUIREMENTS = "requirements.txt"
FRONTEND_LOCK = Path("frontend") / "package-lock.json"

#: 显式版本号。依赖清单的哈希对人没有意义（`af9a783f…`）——真正要回答「有没有新版本」
#: 的是一个能读、能念、能比较的号。现在还是测试阶段，所以按测试版标。
VERSION_FILE = "VERSION"

#: 上一次真正同步进去的清单快照。有了它才能回答「这次**具体**改了什么」——
#: 只比哈希只能说「变了」，说不出变了哪几条。
SNAPSHOT_DIR_RELATIVE = Path(".docmind") / "dependency-state"
SNAPSHOT_REQUIREMENTS = "requirements.txt"
SNAPSHOT_LOCK = "package-lock.json"

#: 测试版标记。出现在版本号里就在界面上照实标「测试版」，不假装是正式版。
PRERELEASE_RE = re.compile(r"(alpha|beta|rc|dev|pre)", re.I)

#: 最低 Python 版本。
#:
#: 代码里用了 3.10+ 的写法（`X | Y` 注解、`match` 等），老解释器会在
#: **装完依赖之后**才炸，报错还很难懂（多半是某个包 import 时的语法错误）。
#: 这里提前拦住并说清楚要哪个版本 —— 空白电脑上第一次装依赖的人，
#: 看到的是"请装 Python 3.11+"而不是一屏看不懂的 traceback。
MIN_PYTHON = (3, 11)


def check_python_version() -> str:
    """返回当前 Python 版本；低于 MIN_PYTHON 直接抛错（信息要能照做）。"""
    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info < MIN_PYTHON:
        raise RuntimeError(
            "需要 Python %d.%d 或更高版本（当前 %s）。"
            "请到 https://www.python.org/downloads/ 安装后重试。"
            % (MIN_PYTHON[0], MIN_PYTHON[1], version))
    return version


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_if_exists(path: Path) -> str:
    return _sha256(path) if path.is_file() else ""


def _run(command: list[str], root: Path, *, label: str) -> None:
    print(f"[Agent Workbench] {label}: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=str(root), check=False)
    if completed.returncode:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}")


def _python_for_venv(venv: Path) -> Path:
    candidate = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return candidate


def _pip_for_venv(venv: Path) -> Path:
    candidate = venv / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    return candidate


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="dependency-state-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class DetailsRequested(Exception):
    """用户在提示框里选了「先看详情」——这不是拒绝，也不是同意。"""


def _console_prompt(project: Path, summary: str) -> bool:
    """控制台问一句要不要升级。看详情时把明细打出来**再问一次**。

    输入不是 tty（CI、管道、服务化启动）时**不拦**：直接升级，保持旧行为。
    拦下来的话 CI 会在等待输入上永久挂住，或者直接失败。
    """
    if not (sys.stdin and sys.stdin.isatty()):
        print("[Agent 工作台] 非交互环境（无控制台输入），自动升级。")
        return True

    print("=" * 60)
    print("  Agent 工作台 检查依赖")
    print("=" * 60)
    print(summary)
    while True:
        try:
            answer = input("是否升级？[Y] 升级 / [n] 这次先跳过 / [d] 先看详情：")
        except (EOFError, KeyboardInterrupt):
            # 用户按了 Ctrl+C / 输入被关掉：当成「先跳过」，别硬往上装。
            print()
            return False
        choice = answer.strip().lower()
        if choice == "d":
            print()
            print(format_update_details(project))
            print()
            continue
        if choice in ("", "y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("  请输入 Y / n / d。")


def _find_npm() -> str | None:
    configured = os.getenv("DOCMIND_NPM_BIN", "").strip()
    if configured:
        return configured
    return shutil.which("npm")


# --------------------------------------------------------------------------
# 版本与「这次更新了什么」
# --------------------------------------------------------------------------

def read_version(project: Path) -> str:
    """读显式版本号；没有这个文件就返回空串（视作「未标版本」的老安装）。"""
    try:
        return (project / VERSION_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def version_label(version: str) -> str:
    """版本号旁边标不标「测试版」——现在是测试阶段，别装成正式版。"""
    if not version:
        return "未标版本"
    return "%s（测试版）" % version if PRERELEASE_RE.search(version) else version


def _snapshot_dir(project: Path) -> Path:
    return project / SNAPSHOT_DIR_RELATIVE


def _read_snapshot(project: Path, name: str) -> str:
    try:
        return (_snapshot_dir(project) / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_snapshot(project: Path, name: str, text: str) -> None:
    folder = _snapshot_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


#: `名字` / `名字==版本` / `名字>=1.0,<2` —— 只取包名和约束，注释与空行丢掉。
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*([=<>!~].*)?$")


def requirement_changes(old_text: str, new_text: str) -> dict:
    """按包名对比两份 requirements，返回新增/删除/改约束三类。"""
    def parse(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in (text or "").splitlines():
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            m = _REQ_LINE.match(stripped)
            if not m:
                continue
            out[m.group(1).lower()] = (m.group(2) or "").strip()
        return out

    old, new = parse(old_text), parse(new_text)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
    return {"added": added, "removed": removed, "changed": changed,
            "old": old, "new": new}


def frontend_changes(old_text: str, new_text: str) -> dict:
    """package-lock 太大，只报数量与变化的包名。"""
    try:
        old_doc = json.loads(old_text or "{}")
        new_doc = json.loads(new_text or "{}")
    except (TypeError, ValueError):
        return {"parsed": False, "changed": [], "added": [], "removed": []}

    def versions(doc) -> dict[str, str]:
        out: dict[str, str] = {}
        packages = doc.get("packages")
        if isinstance(packages, dict):
            for name, meta in packages.items():
                if isinstance(meta, dict) and meta.get("version"):
                    out[name] = str(meta["version"])
        return out

    old, new = versions(old_doc), versions(new_doc)
    return {
        "parsed": True,
        "changed": sorted(n for n in set(old) & set(new) if old[n] != new[n]),
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
    }


def format_update_details(project: Path, *, max_names: int = 8) -> str:
    """「这次具体更新了什么」——详情视图。控制台上用 `[d]` 唤出。

    **没有可比的历史时必须说清楚**。快照是随本功能一起引入的，老安装第一次进来
    没有历史可比；那时若照常输出就会变成「新增 41 项、前端新增 259 项」——
    全是假账（实测就是这个）。谎报的详情比没有详情更坏：用户会以为这一版
    改了 41 个包。
    """
    old_py = _read_snapshot(project, SNAPSHOT_REQUIREMENTS)
    if not old_py.strip():
        return ("（没有可比的上一次记录，无法列出差异。\n"
                "  这是本功能首次运行；下次启动起就能逐项对比了。）")
    py = requirement_changes(old_py,
                             (project / PYTHON_REQUIREMENTS).read_text(encoding="utf-8")
                             if (project / PYTHON_REQUIREMENTS).is_file() else "")
    lines = ["Python 依赖："]
    if not (py["added"] or py["removed"] or py["changed"]):
        lines.append("  （无变化）")
    for name in py["added"][:max_names]:
        lines.append("  + %s %s" % (name, py["new"][name] or "(未指定版本)"))
    for name in py["removed"][:max_names]:
        lines.append("  - %s（已移除）" % name)
    for name in py["changed"][:max_names]:
        lines.append("  ~ %s %s -> %s" % (name, py["old"][name] or "*", py["new"][name] or "*"))
    for label, names in (("新增", py["added"]), ("移除", py["removed"]), ("改版本", py["changed"])):
        if len(names) > max_names:
            lines.append("  …… 另有 %d 项%s" % (len(names) - max_names, label))

    lock = project / FRONTEND_LOCK
    lines.append("前端依赖：")
    fe = frontend_changes(_read_snapshot(project, SNAPSHOT_LOCK),
                          lock.read_text(encoding="utf-8") if lock.is_file() else "")
    if not fe["parsed"]:
        lines.append("  （无法解析 package-lock.json）")
    elif not (fe["added"] or fe["removed"] or fe["changed"]):
        lines.append("  （无变化）")
    else:
        lines.append("  新增 %d 项、移除 %d 项、改版本 %d 项"
                     % (len(fe["added"]), len(fe["removed"]), len(fe["changed"])))
        for name in fe["changed"][:max_names]:
            lines.append("  ~ %s" % name)
        if len(fe["changed"]) > max_names:
            lines.append("  …… 另有 %d 项改版本" % (len(fe["changed"]) - max_names))
    return "\n".join(lines)


def format_update_summary(project: Path, old_version: str, new_version: str) -> str:
    """一行摘要 + 变更计数。提示框里先给这个，详情才按需展开。"""
    head = ("检测到新版本：%s → %s\n"
            % (version_label(old_version), version_label(new_version)))
    tail = "  升级会下载并替换本机依赖，可能需要几分钟。"
    old_py = _read_snapshot(project, SNAPSHOT_REQUIREMENTS)
    if not old_py.strip():
        # 没历史可比时不报计数 —— 见 `format_update_details` 里那段说明：
        # 首次运行时实测报过「新增 41 项、前端新增 259 项」，全是假账。
        return head + "  （首次运行，没有可比的上一次记录，变更项数暂无法列出。）\n" + tail
    py = requirement_changes(old_py,
                             (project / PYTHON_REQUIREMENTS).read_text(encoding="utf-8")
                             if (project / PYTHON_REQUIREMENTS).is_file() else "")
    py_total = len(py["added"]) + len(py["removed"]) + len(py["changed"])
    lock = project / FRONTEND_LOCK
    fe = frontend_changes(_read_snapshot(project, SNAPSHOT_LOCK),
                          lock.read_text(encoding="utf-8") if lock.is_file() else "")
    fe_total = ((len(fe["added"]) + len(fe["removed"]) + len(fe["changed"]))
                if fe["parsed"] else 0)
    return (head + "  Python 依赖变更 %d 项，前端依赖变更 %d 项。\n" % (py_total, fe_total)
            + tail)


def ensure_dependencies(root: str | os.PathLike[str], *, force: bool = False,
                        frontend: bool = True, interactive: bool = True) -> dict:
    """Create/update the local source environment and return a JSON report.

    版本变化时**先问用户**再动手：清单哈希变了说明厂商下发了新版本，但「要不要装」
    是用户的决定。拒绝过一次就记下来（`declined_version`），之后同一版本不再追问 ——
    否则每次启动都弹，应用就没法用了。
    """
    project = Path(root).resolve()
    requirements = project / PYTHON_REQUIREMENTS
    if not requirements.is_file():
        raise FileNotFoundError(f"missing {requirements}")
    check_python_version()
    venv = project / ".venv"
    python = _python_for_venv(venv)
    pip = _pip_for_venv(venv)
    state_path = project / STATE_RELATIVE
    previous = _load_state(state_path)
    req_hash = _hash_if_exists(requirements)
    lock_hash = _hash_if_exists(project / FRONTEND_LOCK)
    current_version = read_version(project)
    report = {
        "root": str(project),
        "python": str(python),
        "requirements_hash": req_hash,
        "frontend_lock_hash": lock_hash,
        "python_changed": False,
        "frontend_changed": False,
        "created_venv": False,
        "version": current_version,
        "installed_version": previous.get("installed_version", ""),
        "declined_version": previous.get("declined_version", ""),
        "update_declined": False,
    }

    if not python.is_file():
        _run([sys.executable, "-m", "venv", str(venv)], project, label="创建 Python 环境")
        report["created_venv"] = True
    if not python.is_file() or not pip.is_file():
        raise RuntimeError(f"virtual environment is incomplete: {venv}")

    python_changed = force or report["created_venv"] or previous.get("requirements_hash") != req_hash
    frontend_outdated = False
    npm = _find_npm() if frontend else None
    lock_file = project / FRONTEND_LOCK
    if frontend:
        frontend_outdated = bool(
            npm and lock_file.is_file() and (
                force or previous.get("frontend_lock_hash") != lock_hash or
                not (project / "frontend" / "node_modules").is_dir() or
                not any((project / "web").glob("assets/*.js"))))

    version_moved = bool(
        current_version and report["installed_version"]
        and current_version != report["installed_version"])
    # 「已否决」只在**真的有版本号**时才算。没写 VERSION 的老安装（`current_version`
    # 是空串）此前被算成「已否决 == '' == ''」-> 依赖同步被永久跳过 ——
    # 而那正是升级前所有安装的形态。否决是用户的**明确答复**，不能由「没版本号」
    # 推断出来。
    already_declined = bool(current_version
                            and report["declined_version"] == current_version)
    needs_sync = (python_changed or frontend_outdated) and not already_declined

    # ---- 用户决定权：清单变了就问，不问就自动（旧行为）----
    # 已被否决的版本**直接跳过整次同步**，不只是「这次不问」。
    # 否则会出现探针量到的那个形状：第一次拒绝、下次启动 `requirements_hash` 仍对不上
    # -> 又要同步 -> 又不问（因为已否决）-> 于是**默默装上了**。决定权只在第一次
    # 生效，等于没有。
    # `force` 是用户显式要求同步（CI、构建机），所以它**越过**否决；
    # 否则「已拒绝」就成了一个连 `--force` 都出不来的死角。
    if already_declined and (python_changed or frontend_outdated) and not force:
        print("[Agent 工作台] 版本 %s 的更新已被你跳过（决定仍有效）。想升级请重新运行并选择 Y。"
              % version_label(current_version))
        report["python_changed"] = False
        report["frontend_changed"] = False
        report["update_declined"] = True
        return report

    if needs_sync and version_moved and interactive:
        accepted = _console_prompt(
            project,
            format_update_summary(project, report["installed_version"], current_version))
        if not accepted:
            # 记一次「已拒绝 + 版本号」。关键是把**版本号**一起记下来：清单此后
            # 还会变（同一个版本号下继续开发），但用户对这个版本的答复不该被反复问。
            report["update_declined"] = True
            report["declined_version"] = current_version
            _save_state(state_path, {
                "schema": 2,
                "python_version": sys.version.split()[0],
                "requirements_hash": previous.get("requirements_hash", ""),
                "frontend_lock_hash": previous.get("frontend_lock_hash", ""),
                "installed_version": report["installed_version"],
                "declined_version": current_version,
                "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            })
            print("[Agent 工作台] 已跳过版本 %s 的更新。仍想用当前版本，继续正常工作。"
                  % version_label(current_version))
            report["python_changed"] = False
            report["frontend_changed"] = False
            return report

    if python_changed:
        # Do not pin pip itself: the requirements file is the project-owned contract.
        _run([str(pip), "install", "--upgrade", "-r", str(requirements)], project,
             label="同步 Python 依赖")
        report["python_changed"] = True
        _write_snapshot(project, SNAPSHOT_REQUIREMENTS,
                        requirements.read_text(encoding="utf-8"))

    # Persist the Python half before touching npm. A locked frontend binary or
    # a temporary network failure must not make the next launch reinstall all
    # Python packages again.
    _save_state(state_path, {
        "schema": 2,
        "python_version": sys.version.split()[0],
        "requirements_hash": req_hash,
        "frontend_lock_hash": previous.get("frontend_lock_hash", ""),
        "installed_version": current_version or previous.get("installed_version", ""),
        "declined_version": "",
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    })

    if frontend_outdated:
        # `--ignore-scripts` 是**实测过的结论**，不是预防性洁癖：
        # `npm ci --ignore-scripts` + `npm run build` 的产物与正常安装**逐字节相同**
        # （25 个 .js 零差异），而 lockfile 里带 install 脚本的包只有 4 个，其中
        # 生产依赖那两个（vue-demi）的 postinstall 在我们的场景下是**幂等**的
        # （`copy()` 做的是 `replace(/'vue'/g, "'vue'")`，我们装的就是 vue）。
        # 依据与探针见 docs/harness-research-verified.md §1.4，
        # 守卫在 tests/test_npm_ignore_scripts_pinned.py。
        _run([npm, "ci", "--ignore-scripts"], project / "frontend",
             label="同步前端依赖")
        _run([npm, "run", "build"], project / "frontend", label="构建前端资源")
        report["frontend_changed"] = True
        _write_snapshot(project, SNAPSHOT_LOCK,
                        lock_file.read_text(encoding="utf-8"))
    elif frontend and not npm and not any((project / "web").glob("assets/*.js")):
        raise RuntimeError("npm is required because built frontend assets are missing")

    state = {
        "schema": 2,
        "python_version": sys.version.split()[0],
        "requirements_hash": req_hash,
        "frontend_lock_hash": lock_hash,
        "installed_version": current_version or previous.get("installed_version", ""),
        "declined_version": "",
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    _save_state(state_path, state)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare Agent Workbench source dependencies")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--force", action="store_true", help="同步全部依赖，即使哈希未变化")
    parser.add_argument("--no-frontend", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--yes", action="store_true",
                        help="不询问，发现新版本直接升级（CI/无人值守）")
    parser.add_argument("--details", action="store_true",
                        help="只打印本次更新的具体内容，不做任何改动")
    parser.add_argument("--version", action="store_true", help="打印版本号后退出")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if args.version:
        print(version_label(read_version(root)))
        return 0
    if args.details:
        print(format_update_details(root))
        return 0
    try:
        result = ensure_dependencies(args.root, force=args.force,
                                     frontend=not args.no_frontend,
                                     interactive=not args.yes)
    except (OSError, RuntimeError, FileNotFoundError) as exc:
        print(f"[Agent 工作台] 依赖准备失败：{exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        changed = []
        if result["created_venv"]:
            changed.append("Python 环境")
        if result["python_changed"]:
            changed.append("Python 依赖")
        if result["frontend_changed"]:
            changed.append("前端依赖和资源")
        print("[Agent 工作台] 依赖已就绪" + ("：" + "、".join(changed) if changed else "（无需更新）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
