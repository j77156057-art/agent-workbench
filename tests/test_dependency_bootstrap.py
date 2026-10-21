import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dependency_bootstrap as bootstrap


#: venv 的目录布局随平台变：Windows 是 `Scripts\python.exe`，POSIX 是 `bin/python`。
#: 生产侧的 `_python_for_venv()` 按平台算路径，夹具若写死 Windows 形状，
#: 在 Linux 上就会被判成 `virtual environment is incomplete` —— 那是**夹具**错了，
#: 不是产品错了。
#: 这里**独立地**按平台拼一遍（不直接调用生产那个函数），夹具才有资格校验生产规则。
_VENV_DIR = "Scripts" if os.name == "nt" else "bin"
_VENV_PYTHON = "python.exe" if os.name == "nt" else "python"
_VENV_PIP = "pip.exe" if os.name == "nt" else "pip"


def _make_venv(venv: Path) -> None:
    """造一个「看起来完整」的 venv 目录（按当前平台的布局）。"""
    folder = venv / _VENV_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _VENV_PYTHON).touch()
    (folder / _VENV_PIP).touch()


class DependencyBootstrapTests(unittest.TestCase):
    def test_first_run_syncs_and_second_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("example-package==1.0\n", encoding="utf-8")
            calls = []

            def fake_run(command, cwd, *, label):
                calls.append((command, label))
                if "-m" in command and "venv" in command:
                    _make_venv(Path(command[-1]))

            with patch.object(bootstrap, "_run", side_effect=fake_run):
                first = bootstrap.ensure_dependencies(root, frontend=False)
                second = bootstrap.ensure_dependencies(root, frontend=False)

            self.assertTrue(first["created_venv"])
            self.assertTrue(first["python_changed"])
            self.assertFalse(second["python_changed"])
            labels = [label for _, label in calls]
            self.assertEqual(labels.count("同步 Python 依赖"), 1)
            state = json.loads((root / ".docmind" / "dependency-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["requirements_hash"], first["requirements_hash"])

    def test_requirements_change_triggers_upgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("example-package==1.0\n", encoding="utf-8")
            _make_venv(root / ".venv")
            calls = []
            with patch.object(bootstrap, "_run", side_effect=lambda command, cwd, *, label: calls.append(label)):
                bootstrap.ensure_dependencies(root, frontend=False)
                (root / "requirements.txt").write_text("example-package==2.0\n", encoding="utf-8")
                result = bootstrap.ensure_dependencies(root, frontend=False)
            self.assertTrue(result["python_changed"])
            self.assertEqual(calls.count("同步 Python 依赖"), 2)

    def test_old_python_fails_fast_with_actionable_message(self):
        """老解释器要在**装依赖之前**就说清楚要哪个版本。

        代码里用了 3.10+ 的写法，老解释器否则会装完全部依赖才炸，
        而且炸在某个包的 import 上 —— 空白电脑上第一次装的人看不懂。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("example-package==1.0\n", encoding="utf-8")
            calls = []
            with patch.object(bootstrap.sys, "version_info", (3, 9, 18)):
                with patch.object(bootstrap, "_run", side_effect=lambda *a, **k: calls.append(a)):
                    with self.assertRaises(RuntimeError) as ctx:
                        bootstrap.ensure_dependencies(root, frontend=False)
            self.assertIn("3.11", str(ctx.exception))
            self.assertEqual(calls, [], "版本不够时不该开始装依赖")

    def test_supported_python_passes(self):
        with patch.object(bootstrap.sys, "version_info", (3, 12, 1)):
            self.assertTrue(bootstrap.check_python_version().startswith("3.12"))


if __name__ == "__main__":
    unittest.main()
