import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import dependency_bootstrap as bootstrap


class DependencyBootstrapTests(unittest.TestCase):
    def test_first_run_syncs_and_second_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "requirements.txt").write_text("example-package==1.0\n", encoding="utf-8")
            calls = []

            def fake_run(command, cwd, *, label):
                calls.append((command, label))
                if "-m" in command and "venv" in command:
                    venv = Path(command[-1])
                    (venv / "Scripts").mkdir(parents=True)
                    (venv / "Scripts" / "python.exe").touch()
                    (venv / "Scripts" / "pip.exe").touch()

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
            venv = root / ".venv" / "Scripts"
            venv.mkdir(parents=True)
            (venv / "python.exe").touch()
            (venv / "pip.exe").touch()
            calls = []
            with patch.object(bootstrap, "_run", side_effect=lambda command, cwd, *, label: calls.append(label)):
                bootstrap.ensure_dependencies(root, frontend=False)
                (root / "requirements.txt").write_text("example-package==2.0\n", encoding="utf-8")
                result = bootstrap.ensure_dependencies(root, frontend=False)
            self.assertTrue(result["python_changed"])
            self.assertEqual(calls.count("同步 Python 依赖"), 2)


if __name__ == "__main__":
    unittest.main()
