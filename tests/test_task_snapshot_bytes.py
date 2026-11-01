# -*- coding: utf-8 -*-
"""任务快照必须按**字节**存：非 UTF-8 文件不能在一趟「存快照 → 回滚」里被改坏。

## 复现过的真实缺陷（修复前必红）

`task_snapshot` 用 `open(..., encoding='utf-8', errors='replace')` 读内容，
`task_revert` 再以 utf-8 写回。实测一个 27 字节的 GBK 文件回滚后变成 45 字节、
里面全是 U+FFFD，而返回值是 `{'ok': True, 'restored': 'snapshot'}` ——
**静默不可逆损坏 + 谎报成功**，两件事一起修：

- 存储改成 `content_b64` + `sha256`（文本预览只在能无损解码时才给）；
- 回滚按字节写，写完比对指纹；对不上就报失败，不许报干净。

## 为什么还要钉反向守卫

把存储整体换成 base64 很容易顺手把"文本文件也不再存可读内容"做过头 ——
预览与旧任务的可读性会静默劣化。所以第 3 条专门钉 UTF-8 仍走文本预览。
"""
import base64
import hashlib
import os
import shutil
import tempfile
import unittest

import game_workbench as gw


class TaskSnapshotBytesTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="snap_bytes_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, rel, raw: bytes):
        target = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(target) or self.root, exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(raw)
        return target

    def _snapshot_task(self, rel):
        task = {"files": [rel]}
        task["snapshot"] = gw.task_snapshot(self.root, task)
        return task

    def test_gbk_file_round_trips_byte_exact(self):
        raw = "设备温度上限 85℃，请核对。".encode("gbk")
        target = self._write("data/gbk_cfg.txt", raw)
        task = self._snapshot_task("data/gbk_cfg.txt")
        with open(target, "wb") as handle:
            handle.write(b"agent overwrote me\n")
        result = gw.task_revert(self.root, task)
        self.assertTrue(result["ok"], result)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), raw, "非 UTF-8 文件被回滚改坏了")

    def test_binary_with_nul_round_trips(self):
        raw = bytes([0x00, 0x01, 0xFE, 0xFF, 0x00, 0x80])
        target = self._write("assets/blob.bin", raw)
        task = self._snapshot_task("assets/blob.bin")
        self._write("assets/blob.bin", b"junk")
        result = gw.task_revert(self.root, task)
        self.assertTrue(result["ok"], result)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), raw)

    def test_utf8_file_still_gets_a_readable_preview(self):
        """反向守卫：改成"一律只存 base64"会让文本预览静默消失。"""
        text = "上限 85 度\n"
        target = self._write("data/notes.md", text.encode("utf-8"))
        saved = self._snapshot_task("data/notes.md")["snapshot"]["files"]["data/notes.md"]
        self.assertEqual(saved.get("content"), text, "UTF-8 文本的预览内容丢了")
        self.assertTrue(saved.get("content_b64"))
        result = gw.task_revert(self.root, {"files": ["data/notes.md"],
                                            "snapshot": {"files": {"data/notes.md": saved}}})
        self.assertTrue(result["ok"], result)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), text.encode("utf-8"))

    def test_legacy_lossy_snapshot_must_not_report_a_clean_restore(self):
        """修复之前存的快照只有替换过的文本 —— 字节回不来，就不许报 `ok: True`。"""
        target = self._write("data/gbk_cfg.txt", "上限 85℃".encode("gbk"))
        legacy = {"mtime_ns": 1, "size": 9, "content": "\ufffd\ufffd\ufffd\ufffd 85\ufffd"}
        result = gw.task_revert(self.root, {"files": ["data/gbk_cfg.txt"],
                                            "snapshot": {"files": {"data/gbk_cfg.txt": legacy}}})
        row = result["results"][0]
        self.assertEqual(row.get("restored"), "snapshot-lossy", row)
        self.assertFalse(row.get("ok"), "旧快照被报成干净还原 —— 谎报成功回来了")
        self.assertFalse(result["ok"], result)
        self.assertTrue(os.path.isfile(target), "lossy 路径不该把文件变没")
        with open(target, "rb") as handle:
            self.assertNotEqual(handle.read(), "上限 85℃".encode("gbk"),
                                "这条用例的前提：旧快照的字节本来就救不回来")

    def test_tampered_snapshot_fails_instead_of_claiming_success(self):
        raw = b"original bytes\n"
        target = self._write("data/ok.txt", raw)
        saved = self._snapshot_task("data/ok.txt")["snapshot"]["files"]["data/ok.txt"]
        self.assertEqual(saved["sha256"], hashlib.sha256(raw).hexdigest(),
                         "指纹必须是改前内容的，不是别的")
        saved["content_b64"] = base64.b64encode(b"some other bytes\n").decode("ascii")
        result = gw.task_revert(self.root, {"files": ["data/ok.txt"],
                                            "snapshot": {"files": {"data/ok.txt": saved}}})
        self.assertFalse(result["ok"], result)
        self.assertIn("hash mismatch", str(result["results"][0]), result)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"some other bytes\n",
                             "写回去是 base64 里的内容；指纹不符只是**不许报成功**，不回滚写入")

    def test_large_file_is_not_snapshotted_inline(self):
        big = b"x" * (513 * 1024)
        self._write("data/big.bin", big)
        saved = self._snapshot_task("data/big.bin")["snapshot"]["files"]["data/big.bin"]
        self.assertNotIn("content_b64", saved, "超过上限还内联存内容，快照会自己变成磁盘大户")
        self.assertEqual(saved["size"], len(big))


if __name__ == "__main__":
    unittest.main()
