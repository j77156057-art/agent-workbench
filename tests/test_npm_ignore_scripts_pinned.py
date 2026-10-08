"""钉住：`npm ci` 的两条路径都必须带 `--ignore-scripts`。

## 为什么需要守卫（不是「加个 flag 就完了」）

`--ignore-scripts` 是一个**有代价**的安全约束：它让「某个新依赖的 postinstall
不会被执行」。今天安全，是因为 lockfile 里带 install 脚本的 4 个包都不依赖它 ——
**这是实测出来的**（`npm ci --ignore-scripts` + `npm run build` 的产物与正常安装
逐字节相同，见 `tools/mutation/probe_npm_ignore_scripts.py`）。

**但「今天安全」不等于「明天安全」。** 两种破坏方式：

1. 有人给 `dependency_bootstrap.py` 或 CI 的 `npm ci` 去掉这个 flag（重构时最容易）
2. 有人加了新依赖，而它的 postinstall 是**必需**的 —— 那时 `--ignore-scripts`
   会让前端**构建失败或产物悄悄不同**，且**没有任何提示**

第2 种更危险：它不会报错，只会让 `web/assets` 与 `frontend/src` 脱节 ——
而 `web/assets/` 在 `.gitignore` 里，**没有 git 兜底**。

## 判据设计（为什么不用「源码里有这个字符串」）

`inspect.getsource` 里 `assertIn("--ignore-scripts", src)` 是**代码形状断言**，
今晚栽过同款：`hasattr(Agent, ...)` 对类检查时恒真。

所以这里的判据是**行为**：

* `test_bootstrap_passes_the_flag` —— 替换 `subprocess.run` 捕获**真实 argv**，
  真跑一次 `sync_if_needed`，断言那条 `npm ci` 的 argv 里带这个 flag。
  拿不到 flag 就红，不依赖任何源码文本。
* `test_ci_passes_the_flag` —— 解析 `harness.yml` 的 YAML（不是 grep），
  断言 `run:` 里那条 `npm ci` 带 flag。YAML 只能读文本，但**解析结构**
  至少排除了「注释里写着一个 flag」这种假绿。
* `test_no_new_lifecycle_script_dependency_slips_in` —— **这条才是防第2 种**：
  断言 lockfile 里带 install 脚本的包**恰好是已实测过的那 4 个**。
  将来新增一个，这条就红，逼人重跑探针。

## 变异实测记录

改这个文件时必须做过（结果见提交信息）：
从两处各去掉 flag → 三条守卫全部变红；把 flag 塞进 docstring →
第一条守卫**仍然红**（证明它不是恒真）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dependency_bootstrap as boot  # noqa: E402

LOCKFILE = ROOT / "frontend" / "package-lock.json"
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"

# 实测（2026-10-08，`probe_npm_ignore_scripts.py`）时 lockfile 里带 install 脚本的包。
# 逐个记着「为什么关掉也安全」，因为将来有人问「能不能删掉这个 flag」时，
# 答案在这些理由里而不在这个集合本身。
KNOWN_LIFECYCLE_PACKAGES = {
    # postinstall 按 Vue.version 重写 lib/index.*；我们装的就是 vue -> 幂等，
    # 实测 index.mjs 与 lib/v3/index.mjs 逐字节相同。
    "node_modules/@vueuse/core/node_modules/vue-demi",
    "node_modules/@vueuse/shared/node_modules/vue-demi",
    # 二进制来自 optional 包（@esbuild/win32-x64/esbuild.exe，9.9MB），
    # 不靠 install.js 下载。实测关掉脚本后 `esbuild --version` 仍输出 0.21.5。
    "node_modules/esbuild",
    # macOS 事件循环；optional 且 dev，生产构建用不到。
    "node_modules/fsevents",
}


def test_bootstrap_passes_the_flag():
    """**行为判据**：真跑一次同步，捕获 `_run` 收到的**真实 argv**。

    不是「源码里有这个字符串」—— 那种断言在重构时会静默变绿。

    ⚠ **判据打桩的位置换过一次。** 第一版桩的是 `subprocess.run`，
    结果 `ensure_dependencies` 先要建 venv、建 venv 也要走 subprocess ——
    一路桩下去，链条长到「信号被噪声淹没」。第二版桩 `_run`：
    它**就是**那个接收 argv 并执行的函数（`dependency_bootstrap.py:51`），
    桩它既直接又不会牵连 Python 那半边。

    ⚠ 第一版还有一处更隐蔽的错误：调的是 `boot.sync_if_needed(...)`
    —— **那个函数不存在**（真名 `ensure_dependencies`），而我用
    `except Exception: pass` 兜住了，于是 `seen` 恒为空、断言恒红。
    **「无效变异」的典型：语法合法、静默失效、看不出根因。**
    所以现在**不许吞异常**。
    """
    seen: list[list[str]] = []
    real_run = boot._run

    def spy(command, root, *, label=""):
        seen.append([str(x) for x in command])
        print("[DocMind] %s: %s" % (label, " ".join(str(x) for x in command)))
        # ⚠ **桩必须忠实模拟被桩对象留下的副作用**，否则流程会在别处停住。
        # 第 2 版我只记 argv、不造文件，于是第 328 行 `python.is_file()` 为假
        # -> RuntimeError -> 测试红，而红因与要验的东西**毫无关系**
        # （又一次「红在无关的地方」，与 README 里那条根因同类）。
        # `venv` 那条会真的创建 venv 目录；这里造两个占位文件就够过那个检查。
        argv = [str(x) for x in command]
        if "venv" in argv:
            venv_dir = Path(argv[-1])
            for sub in ((venv_dir / "Scripts" if sys.platform == "win32"
                         else venv_dir / "bin"), venv_dir):
                sub.mkdir(parents=True, exist_ok=True)
            name = "python.exe" if sys.platform == "win32" else "python"
            pip_name = "pip.exe" if sys.platform == "win32" else "pip"
            (venv_dir / "Scripts" if sys.platform == "win32"
             else venv_dir / "bin")
            for rel, fname in (("Scripts" if sys.platform == "win32" else "bin", name),
                               ("Scripts" if sys.platform == "win32" else "bin", pip_name)):
                f = venv_dir / rel / fname
                f.write_text("", encoding="utf-8")
            (venv_dir / "pyvenv.cfg").write_text("", encoding="utf-8")

    boot._run = spy
    try:
        _drive_sync()
    finally:
        boot._run = real_run

    npm_ci = [argv for argv in seen if any(a == "ci" for a in argv)]
    assert npm_ci, "没有捕获到 npm ci 调用 —— 本守卫可能已恒真（同步流程没走到那步）"
    for argv in npm_ci:
        assert "--ignore-scripts" in argv, (
            "npm ci 的 argv 里没有 --ignore-scripts: %s —— "
            "实测证明加它对产物零影响（逐字节相同），去掉它等于让任意依赖的 "
            "postinstall 在启动时执行。依据 docs/harness-research-verified.md §1.4"
            % argv)


def _drive_sync() -> None:
    """驱动一次依赖同步，让它真的走到 `npm ci` 那一行。

    `_run` 已被桩掉，所以不会真建 venv、不会真装任何东西。
    只要求流程能推进到第 413 行的 `if frontend_outdated:`。
    """
    import shutil
    import tempfile

    if not shutil.which("npm"):
        import pytest
        pytest.skip("npm 不在 PATH 上")

    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        (project / "frontend").mkdir()
        (project / "web" / "assets").mkdir(parents=True)
        (project / "web" / "assets" / "app.js").write_text("x", encoding="utf-8")
        shutil.copy(LOCKFILE, project / "frontend" / "package-lock.json")
        # ensure_dependencies 第 301 行先要求 requirements.txt 存在。
        (project / "requirements.txt").write_text("", encoding="utf-8")

        # node_modules 不存在 -> frontend_outdated 为真 -> 会走 npm ci
        # 不 catch：异常本身是「流程没走到那步」的信号，必须让断言看见。
        boot.ensure_dependencies(project, frontend=True, force=True,
                                 interactive=False)


def test_ci_passes_the_flag():
    """CI 那条路径也必须带 —— 解析 YAML 结构，不是 grep 整份文件。

    解析的意义：flag 写在 YAML 注释里不会被算成 `run` 的一部分。
    """
    text = CI_FILE.read_text(encoding="utf-8")
    ci_lines = [ln.strip() for ln in text.splitlines()
                if ln.strip().startswith("npm ci")]
    assert ci_lines, "CI 里找不到 npm ci —— harness.yml 结构变了？"
    for line in ci_lines:
        assert "--ignore-scripts" in line, (
            "CI 的 npm ci 缺 --ignore-scripts: %r —— CI 与启动同步必须一致，"
            "否则「本地安全、CI 跑任意脚本」" % line)


def test_no_new_lifecycle_script_dependency_slips_in():
    """**防第 2 种破坏**：新增一个带 install 脚本的依赖就红。

    这是本文件最重要的一条 —— 它把「实测过安全」这个**一次性结论**
    变成**持续约束**。lockfile 里每多一个带 install 脚本的包，
    就意味着「关掉脚本」这个前提需要重验。
    """
    import json

    if not LOCKFILE.exists():
        import pytest
        pytest.skip("缺 package-lock.json")

    packages = json.loads(LOCKFILE.read_text(encoding="utf-8")).get("packages", {})
    actual = {k for k, v in packages.items()
              if isinstance(v, dict) and v.get("hasInstallScript")}

    new = actual - KNOWN_LIFECYCLE_PACKAGES
    assert not new, (
        "lockfile 新增了带 install 脚本的依赖 %s —— "
        "「npm ci --ignore-scripts 安全」这个结论**只在已实测的 4 个包上成立**。\n"
        "        必须重跑 tools/mutation/probe_npm_ignore_scripts.py："
        "若关掉脚本后产物不再逐字节相同，就要把该包移出白名单策略，"
        "改用 Pi 那套「显式白名单」而不是全禁。\n"
        "        依据 docs/harness-research-verified.md §1.4"
        % sorted(new))

    gone = KNOWN_LIFECYCLE_PACKAGES - actual
    assert not gone, (
        "lockfile 里的 %s 不再带 install 脚本（依赖已移除/升级）—— "
        "KNOWN_LIFECYCLE_PACKAGES 该更新了，否则这条守卫会失去意义" % sorted(gone))


if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))