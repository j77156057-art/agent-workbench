"""Optional acceptance against a real Godot project on the developer machine.

The test is intentionally opt-in by environment or by the repository's
standard local sample location.  CI machines without Godot or the project
skip it; when available it launches the actual main scene headlessly and
requires the project's runtime bridge to emit ``player_ready``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import unittest
from pathlib import Path


_PROJECT_ENV = "DOCMIND_REAL_GAME_PROJECT"
_ENGINE_ENV = "DOCMIND_GODOT_EXECUTABLE"


def _project_root() -> Path:
    return Path(os.getenv(_PROJECT_ENV, "").strip() or r"<sample-project>")


def _godot() -> str:
    explicit = os.getenv(_ENGINE_ENV, "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("godot4") or shutil.which("godot") or \
        next((str(path) for path in (
            Path("<godot-executable>"),
            Path("<godot-executable>"),
        ) if path.is_file()), "")


def _project_skip_reason(root: Path) -> str:
    raw = os.getenv(_PROJECT_ENV, "").strip()
    if not raw:
        return ("未设置 %s：默认占位符不是真实项目；"
                "设成含 project.godot 的项目根目录后可运行" % _PROJECT_ENV)
    if not root.is_dir():
        return "%s=%r 指向的目录不存在；改成含 project.godot 的项目根目录" % (_PROJECT_ENV, raw)
    return "%s=%r 不是 Godot 项目（缺 project.godot）" % (_PROJECT_ENV, raw)


def _engine_skip_reason() -> str:
    raw = os.getenv(_ENGINE_ENV, "").strip()
    if not raw:
        return ("未设置 %s，PATH 里也没有 godot4/godot；"
                "设成 Godot 可执行文件的完整路径后可运行" % _ENGINE_ENV)
    return "%s=%r 指向的文件不存在或不是文件；改成 Godot 可执行文件的完整路径" % (
        _ENGINE_ENV, raw)


class RealGodotAcceptanceTests(unittest.TestCase):
    def test_real_project_headless_playtest(self):
        root = _project_root()
        executable = _godot()
        if not (root.is_dir() and (root / "project.godot").is_file()):
            self.skipTest(_project_skip_reason(root))
        if not executable:
            self.skipTest(_engine_skip_reason())
        completed = subprocess.run(
            [executable, "--headless", "--path", str(root), "--quit-after", "3"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        self.assertEqual(completed.returncode, 0, output[-4000:])
        self.assertIn('"type":"player_ready"', output)
        self.assertNotIn("SCRIPT ERROR", output)
        self.assertNotIn("Parse Error", output)


if __name__ == "__main__":
    unittest.main()
