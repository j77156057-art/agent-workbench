"""引擎清单分支的用例：改完必须真的走一遍。

来历：SIM115 那批把这几处 `open()` 改成了 `with`，CI 的改动行覆盖率门当场点名
`game_workbench.py:114-116 / 125-126 / 148-149`「这一行改完没有任何用例执行过」。
既有用例只覆盖 unreal 的 `.uproject` 分支（`tests/test_unreal_asset_index.py`），
Unity 的 ProjectVersion / .meta guid 与 unreal 的 `.Build.cs` 依赖解析都没有人走过。
门是对的量具，补的是用例 —— 不是把门槛降下来。
"""

import contextlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import game_workbench as gw

#: 夹具不能放在系统临时目录下：`engine_inspect` 会跳过任何路径段叫 `Temp` 的目录
#: （Unity 自己就有 Temp/），而本机默认临时目录就是 `D:\Temp` —— 整个夹具树会被静默跳过，
#: 用例就会「什么都没读到」。CI 的 /tmp 没这个问题，本机有。所以落在仓库内的 .tmp 下。
_FIXTURE_BASE = Path(__file__).resolve().parent.parent / ".tmp"


@contextlib.contextmanager
def _fixture_root():
    _FIXTURE_BASE.mkdir(exist_ok=True)
    root = tempfile.mkdtemp(prefix="dm-engine-inspect-", dir=str(_FIXTURE_BASE))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class UnityInspectTests(unittest.TestCase):
    def test_project_version_and_meta_guid_are_read(self):
        with _fixture_root() as root:
            _write(root, os.path.join("ProjectSettings", "ProjectVersion.txt"),
                   "m_EditorVersion: 6000.0.23f1\n"
                   "m_EditorVersionWithRevision: 6000.0.23f1 (abc123)\n")
            _write(root, os.path.join("Assets", "Art", "Hero.png.meta"),
                   "fileFormatVersion: 2\nguid: 0123456789abcdef0123456789abcdef\n")
            out = gw.engine_inspect(root, "unity")
        self.assertTrue(out["ok"], out)
        versions = [m.get("version") for m in out["manifests"]]
        self.assertIn("6000.0.23f1", versions, "ProjectVersion 没被读出来：%r" % out["manifests"])
        guids = [a.get("guid") for a in out["assets"]]
        self.assertIn("0123456789abcdef0123456789abcdef", guids,
                      ".meta 的 guid 没被读出来：%r" % out["assets"])


class UnrealBuildCsTests(unittest.TestCase):
    def test_build_cs_dependencies_are_parsed(self):
        with _fixture_root() as root:
            _write(root, os.path.join("Source", "MyGame", "MyGame.Build.cs"),
                   'PublicDependencyModuleNames.AddRange(new string[] { "Core", "Engine" });\n')
            out = gw.engine_inspect(root, "unreal")
        builds = [s for s in out["symbols"] if s.get("kind") == "build"]
        self.assertTrue(builds, "没有把 .Build.cs 收成 build 条目：%r" % out["symbols"])
        self.assertIn("Core", builds[0].get("dependencies", []),
                      "依赖没被解析出来：%r" % builds[0])


if __name__ == "__main__":
    unittest.main()
