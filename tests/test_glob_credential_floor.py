# -*- coding: utf-8 -*-
"""FLOOR-Glob-Floor-Bypass 锁定测试：dev_glob 的文件名搜索必须与 read_file/grep 同一道凭据地板。

全离线、纯假密钥、临时目录当 code_root，绝不触碰真实凭据盘。锁四件事：
  ① 凭据路径（.ssh/id_rsa 无扩展名、.aws/credentials.json、.kube/config.yaml、.netrc）
     不得被 dev_glob 枚举进结果；
  ② 被扣必须诚实披露（出现"跳过/凭据"字样），但披露只报数量、绝不打印具体凭据全路径
     —— 比 grep 更严：glob 的输出本身就是路径，打印被扣路径等于反向泄露凭据位置；
  ③ 普通文件不受误伤（src/app.py、README.md 正常命中）；
  ④ code_intel.glob_files 认 skip_predicate 回调（机制层独立锁定，不依赖 tools 接线）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools as T  # noqa: E402
from agent_runtime import code_intel  # noqa: E402

FAKE = "zzzFAKEPRIVKEYNOTREAL99"
CREDS = (".ssh/id_rsa", ".aws/credentials.json", ".kube/config.yaml", ".netrc")


class GlobCredentialFloor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="glob_floor_")
        self._mk(".ssh/id_rsa", "-----BEGIN PRIVATE----- %s" % FAKE)
        self._mk(".aws/credentials.json", '{"key": "%s"}' % FAKE)
        self._mk(".kube/config.yaml", "token: %s" % FAKE)
        self._mk(".netrc", "password %s" % FAKE)
        self._mk("src/app.py", "print('normal')")
        self._mk("README.md", "hello")
        self._patcher = mock.patch.object(T, "_get_code_root", return_value=self.root)
        self._patcher.start()

    def _mk(self, rel, body):
        fp = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(body + "\n")

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _result_lines(out):
        # render_glob lists matched files as lines indented by two spaces.
        return [ln[2:] for ln in out.split("\n") if ln.startswith("  ")]

    def test_credential_paths_not_enumerated(self):
        rl = set(self._result_lines(T.dev_glob("**/*")))
        for cred in CREDS:
            self.assertNotIn(cred, rl, "凭据路径被枚举: %s" % cred)
        self.assertIn("src/app.py", rl)
        self.assertIn("README.md", rl)

    def test_specific_credential_pattern_withheld(self):
        for pat in ("id_rsa", "*credential*", "config.yaml"):
            out = T.dev_glob(pat)
            for full in (".ssh/id_rsa", ".aws/credentials.json", ".kube/config.yaml"):
                self.assertNotIn(full, out, "pattern=%s 泄露了 %s" % (pat, full))

    def test_withhold_is_disclosed_but_count_only(self):
        out = T.dev_glob("**/*")
        self.assertIn("跳过", out)          # 诚实披露：确有被扣
        self.assertIn("凭据", out)
        for full in (".ssh/id_rsa", ".aws/credentials.json", ".kube/config.yaml"):
            self.assertNotIn(full, out)     # 但披露只报数量、不吐具体路径

    def test_no_overblock_on_normal_search(self):
        out = T.dev_glob("*.py")
        self.assertIn("src/app.py", out)
        self.assertNotIn("id_rsa", out)

    def test_glob_files_honours_predicate(self):
        def withhold_py(path):
            return path.endswith(".py")
        res = code_intel.glob_files(self.root, "**/*", skip_predicate=withhold_py)
        self.assertTrue(res["ok"], res["error"])
        files = set(res["files"])
        self.assertNotIn("src/app.py", files)
        self.assertIn("src/app.py", res["skipped"])
        self.assertIn("README.md", files)


if __name__ == "__main__":
    unittest.main()
