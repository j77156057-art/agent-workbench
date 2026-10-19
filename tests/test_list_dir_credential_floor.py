# -*- coding: utf-8 -*-
"""FLOOR-ListDir-Shallow-Leak 锁定测试：list_dir 单层浏览必须与 read_file/grep/glob 同一道凭据地板。

全离线、临时目录当 code_root，绝不触碰真实凭据盘。锁四件事：
  ① 列根目录时，凭据条目（.ssh/.aws/.kube 目录 + .netrc 文件）不得作为 [DIR]/[FILE] 行
     出现，连"名字 + 子项计数 / 字节数"这类存在性信息都不给；
  ② 被扣必须诚实披露（出现"跳过/凭据"字样），但只报数量、绝不打印被扣条目的具体名
     —— 同 glob 口径：list_dir 的输出本身就是名称，列名 = 反向泄露凭据位置。
     （披露语里的 ".ssh/.aws/.kube/.gnupg/.netrc/sudoers" 是固定类别图例，非发现结果，允许。）
  ③ 普通条目不受误伤（src/ 目录、README.md 文件照常列出），空目录仍"（空）"且无跳过噪声；
  ④ 显式 path: 指向凭据目录 → 不枚举其内部（顶层地板，既有行为，一并锁死）。
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

CRED_DIRS = (".ssh", ".aws", ".kube")
# 具体文件名/深层名：地板命中后这些字符串在任何输出里都不得出现。
DISCOVERED_NAMES = ("id_rsa", "credentials.json", "config.yaml")


class ListDirCredentialFloor(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="listdir_floor_")
        self._mk(".ssh/id_rsa", "x")
        self._mk(".aws/credentials.json", "x")
        self._mk(".kube/config.yaml", "x")
        self._mk(".netrc", "x")
        self._mk("src/app.py", "x")
        self._mk("README.md", "x")
        self._patcher = mock.patch.object(T, "_get_code_root", return_value=self.root)
        self._patcher.start()

    def _mk(self, rel, body):
        fp = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "w", encoding="utf-8") as f:
            f.write(body)

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _row_lines(out):
        return [ln for ln in out.split("\n")
                if ln.startswith("[DIR") or ln.startswith("[FILE")]

    def test_credential_entries_not_listed(self):
        rows = " ".join(self._row_lines(T.list_dir("")))
        for d in CRED_DIRS:
            self.assertNotIn(d + "/", rows, "凭据目录被列出: %s" % d)
        self.assertNotIn("] .netrc", rows, "凭据文件被列出: .netrc")
        for name in DISCOVERED_NAMES:
            self.assertNotIn(name, rows)

    def test_normal_entries_still_listed(self):
        out = T.list_dir("")
        self.assertIn("src", out)
        self.assertIn("README.md", out)

    def test_withhold_disclosed_but_count_only(self):
        out = T.list_dir("")
        self.assertIn("跳过", out)
        self.assertIn("凭据", out)
        # 具体被扣文件名在全文（含披露语）都绝不出现——这才算"只报数不报名"
        for name in DISCOVERED_NAMES:
            self.assertNotIn(name, out)

    def test_empty_root_no_skip_noise(self):
        empty = tempfile.mkdtemp(prefix="listdir_empty_")
        try:
            with mock.patch.object(T, "_get_code_root", return_value=empty):
                out = T.list_dir("")
            self.assertIn("（空）", out)
            self.assertNotIn("跳过", out)
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_explicit_credential_dir_not_enumerated(self):
        out = T.list_dir("path: .ssh")
        self.assertNotIn("id_rsa", out)


if __name__ == "__main__":
    unittest.main()
