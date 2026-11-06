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


def _project_root() -> Path:
    return Path(os.getenv("DOCMIND_REAL_GAME_PROJECT", r"<sample-project>"))


def _godot() -> str:
    explicit = os.getenv("DOCMIND_GODOT_EXECUTABLE", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("godot4") or shutil.which("godot") or \
        next((str(path) for path in (
            Path("<godot-executable>"),
            Path("<godot-executable>"),
        ) if path.is_file()), "")


class RealGodotAcceptanceTests(unittest.TestCase):
    def test_real_project_headless_playtest(self):
        root = _project_root()
        executable = _godot()
        if not (root.is_dir() and (root / "project.godot").is_file() and executable):
            self.skipTest("real Godot project or executable is not configured")
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
