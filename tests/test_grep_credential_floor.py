# -*- coding: utf-8 -*-
"""FLOOR-Grep-Floor-Bypass 锁定测试：grep 递归内容搜索必须与 read_file 同一道凭据地板。
全离线、纯假密钥、临时目录当 code_root，绝不触碰真实凭据盘。
锁三件事：
  ① 带代码扩展名的凭据文件（.aws/credentials.json、.kube/config.yaml）被 grep 跳过——
     不泄漏内容，且诚实披露"未搜索、别据此下否定结论"；
  ② 显式 path: 指向凭据路径 → 直接给硬地板拒绝（与 read_file 一致）；
  ③ 普通代码文件不受误伤（正常命中、无跳过披露）。
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

FAKE_JSON = "zzzFAKEJSONKEYNOTREAL88"
FAKE_YAML = "zzzFAKETOKENNOTREAL77"


class GrepCredentialFloor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="grep_floor_")
        os.makedirs(os.path.join(self.root, ".aws"), exist_ok=True)
        os.makedirs(os.path.join(self.root, ".kube"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "src"), exist_ok=True)
        with open(os.path.join(self.root, ".aws", "credentials.json"), "w", encoding="utf-8") as f:
            f.write('{"aws_secret_access_key": "%s"}\n' % FAKE_JSON)
        with open(os.path.join(self.root, ".kube", "config.yaml"), "w", encoding="utf-8") as f:
            f.write("token: %s\n" % FAKE_YAML)
        with open(os.path.join(self.root, "src", "app.py"), "w", encoding="utf-8") as f:
            f.write("SECRET_MARKER = 'top-secret-normal-code'\n")
        self._patcher = mock.patch.object(T, "_get_code_root", return_value=self.root)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cred_file_with_code_ext_not_leaked(self):
        """窄口子核心：带 .json/.yaml 的凭据文件，grep 不得吐出密钥行。"""
        out_json = T.grep("aws_secret_access_key")
        self.assertNotIn(FAKE_JSON, out_json)
        out_yaml = T.grep("FAKETOKENNOTREAL")
        self.assertNotIn(FAKE_YAML, out_yaml)

    def test_skip_is_disclosed_not_silent(self):
        """跳过必须诚实披露，不许把'没命中'伪装成'这里没这内容'（SYM-Absence-Overclaim 教训）。"""
        out = T.grep("aws_secret_access_key")
        self.assertIn("未搜索", out)
        self.assertIn("凭据", out)
        # 明示别下否定结论
        self.assertIn("没命中", out)

    def test_explicit_credential_path_refused(self):
        """显式 path: 指到凭据目录/文件 → 直接硬地板拒绝（与 read_file 一致）。"""
        out = T.grep("aws_secret, path: .aws/credentials.json")
        self.assertIn("安全策略拒绝", out)
        self.assertNotIn(FAKE_JSON, out)

    def test_normal_code_file_still_searched_no_overblock(self):
        """③ 普通代码文件不受误伤：正常命中。注意——整仓扫描只要树里有凭据文件，
        那条诚实跳过披露就会出现（与既有 pruned_state 同理，属正确行为），所以这里
        锁的是'app.py 命中且未被列入跳过'，而非'无披露'。"""
        out = T.grep("SECRET_MARKER")
        self.assertIn("app.py", out)
        self.assertIn("top-secret-normal-code", out)
        # 普通文件绝不能被当成凭据路径跳过（披露清单里只应列 .aws/.kube 那些）
        self.assertNotIn("src/app.py", out.split("已跳过", 1)[-1] if "已跳过" in out else "")


if __name__ == "__main__":
    unittest.main()
