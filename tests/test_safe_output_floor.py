"""审计 F2 回归：data_formats._safe_output 的凭据地板。

F2 修复前：延迟导入 `tools._hard_floor_reason` 失败时静默 fail-open（`_floor = None`
跳过地板，凭据写被悄悄放行）。
F2 修复后：导入失败 **fail-closed**（拒绝写入）+ `logger.warning` 告警；正常时
凭据路径仍被拦（data_formats 用 `Path.resolve()` 已 realpath 化，是 6 入口里最稳的一处）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import data_formats as DF  # noqa: E402
import tools as T  # noqa: E402


class SafeOutputFloorTest(unittest.TestCase):
    def _tmp_root(self):
        return tempfile.mkdtemp(prefix="safe_output_floor_")

    def test_safe_output_blocks_when_floor_triggered(self):
        # 地板判定触发时，_safe_output 必须拒绝（回归：地板仍接线）
        root = self._tmp_root()
        with mock.patch.object(T, "_hard_floor_reason", return_value="安全策略拒绝：测试凭据"):
            with self.assertRaises(DF.DataFormatError) as ctx:
                DF._safe_output(root, "sub/file.txt")
        self.assertIn("安全策略拒绝", str(ctx.exception))

    def test_safe_output_allows_normal_when_floor_none(self):
        # 地板返回 None 时，正常相对路径放行并返回解析后的 Path
        root = self._tmp_root()
        with mock.patch.object(T, "_hard_floor_reason", return_value=None):
            target = DF._safe_output(root, "sub/file.txt")
        self.assertTrue(str(target).replace("\\", "/").endswith("sub/file.txt"))

    def test_safe_output_fail_closed_when_import_fails(self):
        # F2 核心：延迟导入 tools._hard_floor_reason 失败 → 不再静默放行，改为拒绝 + 告警
        root = self._tmp_root()
        real = getattr(T, "_hard_floor_reason", None)
        try:
            delattr(T, "_hard_floor_reason")
            with mock.patch.object(DF.logger, "warning") as warn:
                with self.assertRaises(DF.DataFormatError) as ctx:
                    DF._safe_output(root, "sub/file.txt")
                self.assertIn("安全护栏", str(ctx.exception))
                self.assertTrue(warn.called, "导入失败时必须告警（F2：不再是静默 fail-open）")
        finally:
            if real is not None:
                setattr(T, "_hard_floor_reason", real)


if __name__ == "__main__":
    unittest.main()
