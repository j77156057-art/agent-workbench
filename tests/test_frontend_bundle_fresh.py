# -*- coding: utf-8 -*-
"""「改了源码但页面还是旧的」这类故障，必须能自动发现。

这个仓库反复吃过这个亏，两次形态不同：
1. `bat_guard` 抓到过：`web/` 比 `frontend/src` 旧，页面跑的是上一次构建，
   而所有前端测试都绿（它们测的是**源码**，不是**产物**）。
2. 本轮又踩了一次：改完 `frontend/src` 没跑 `npm run build`，用户打开设置页
   看到的是 22:54 的旧 bundle，问"前端怎么没更新"。

关键认识：**前端测试全绿 ≠ 页面是新的**。`vue-tsc` 与 `node --test` 验的
都是 `frontend/src`，而用户看到的是 `web/assets`。两者之间没有任何机制保证同步
——所以这个文件就是那个缺失的机制。

它验三件事：
1. `web/` 产物是用**当前**源码构建的（核对构建清单里的源码哈希）；
2. 关键标识符**确实进了产物**（光看哈希不够：构建可能因故没打包那个 chunk）；
3. 产物里确实含用户会看到的中文字符串（`costLabel` 的 LABEL 表）。

第 2、3 条是这个测试真正值钱的地方：哈希对但内容缺，比哈希旧更隐蔽。

## 为什么不用 mtime

「产物比源码新」这条判据**在 CI 上是碰巧成立的**：git checkout 会把所有文件的
mtime 刷成同一时刻，于是它只是构建顺序的副产品；而在本地，它又可能**偶然放过**
「改了 src 忘了 build」——文件内容没真改、只有 mtime 变了的时候，mtime 仍然
显示产物较新。

哈希不受这两种干扰：清单由**这次构建自己**写下（`frontend/vite.config.ts` 的
`buildManifest` 插件），谁改了源码又没重建，清单里的哈希就对不上，立刻红。
清单本身是构建产物（`web/` 在 .gitignore 里），所以它只存在于构建过的那台
机器上——而这正是它该在的地方：要核对的正是「这台机器上的产物」对不对得上
「这台机器上的源码」。
"""
import glob
import hashlib
import json
import os
import re
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
WEB_ASSETS = os.path.join(ROOT, "web", "assets")
#: 构建时写下的来源清单（`web/build-manifest.json`）。
MANIFEST = os.path.join(ROOT, "web", "build-manifest.json")

#: 用户在界面上真正会看到的字符串。改了它们却没重新构建，页面就会显示旧文案。
EXPECTED_STRINGS = ["该模型免费", "本地模型", "未配置单价，费用未计入"]

#: 源码里的标识符：证明带计价状态的那条链路进了产物。
EXPECTED_IDENTS = ["pricing_state"]


def _sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _current_sources(source_root):
    """当前 source_root 下每个文件的哈希（相对路径 → sha256）。"""
    out = {}
    for path in glob.glob(os.path.join(source_root, "**", "*"), recursive=True):
        if not os.path.isfile(path):
            continue
        rel = os.path.relpath(path, source_root).replace(os.sep, "/")
        out[rel] = _sha256(path)
    return out


def _source_diff(manifest_path, source_root):
    """比对构建清单与当前源码，返回问题描述列表（空 = 对得上）。

    三个方向都要查：改过的、删掉的、新增的 —— 只查「改过的」会漏掉
    「新增了源文件但没重建」与「删了源文件但没重建」。
    """
    with open(manifest_path, encoding="utf-8") as handle:
        data = json.load(handle)
    recorded = data.get("sources") or {}
    current = _current_sources(source_root)
    problems = []
    for rel, digest in sorted(recorded.items()):
        now = current.get(rel)
        if now is None:
            problems.append("源文件已被删除但没重新构建：%s" % rel)
        elif now != digest:
            problems.append("源文件改过但没重新构建：%s" % rel)
    for rel in sorted(current):
        if rel not in recorded:
            problems.append("新增了源文件但没重新构建：%s" % rel)
    return problems


def _read_bundle():
    blob = ""
    for path in glob.glob(os.path.join(WEB_ASSETS, "*.js")):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                blob += handle.read()
        except OSError:
            continue
    return blob


class BundleFreshnessTests(unittest.TestCase):

    def setUp(self):
        if not os.path.isdir(WEB_ASSETS):
            self.skipTest("web/assets 不存在——先跑 `cd frontend && npm run build`")

    def test_the_bundle_was_built_from_the_current_sources(self):
        """这条对应真实事故：改了 src 没重建，页面跑的是旧产物。"""
        if not os.path.isfile(MANIFEST):
            self.fail(
                "web/build-manifest.json 不存在——构建流程没写来源清单。"
                "请确认 `frontend/vite.config.ts` 里的 buildManifest 插件还在，"
                "并跑 `cd frontend && npm run build`")
        problems = _source_diff(MANIFEST, SRC)
        self.assertEqual(
            problems, [],
            "产物不是用当前源码构建的：" + chr(10) + chr(10).join(problems[:20]) +
            chr(10) + "请跑 `cd frontend && npm run build`")

    def test_the_cost_label_strings_reached_the_bundle(self):
        """哈希对也不够——构建可能根本没把那个 chunk 打进去。"""
        blob = _read_bundle()
        missing = [text for text in EXPECTED_STRINGS if text not in blob]
        self.assertEqual(missing, [],
                         "这些界面文案不在 web/ 产物里，说明没重新构建：%s" % missing)

    def test_the_pricing_state_plumbing_reached_the_bundle(self):
        blob = _read_bundle()
        missing = [name for name in EXPECTED_IDENTS if name not in blob]
        self.assertEqual(missing, [],
                         "计价状态字段不在 web/ 产物里，说明没重新构建：%s" % missing)


class SourceDiffJudgementTests(unittest.TestCase):
    """判据本身要能咬住东西 —— 否则又是一道永远绿的灯。"""

    def _tree(self, root, files):
        for rel, body in files.items():
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(body)

    def _manifest(self, root, files):
        sources = {rel: hashlib.sha256(body).hexdigest() for rel, body in files.items()}
        path = os.path.join(root, "m.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "source_root": "src", "sources": sources}, handle)
        return path

    def test_an_unchanged_tree_has_no_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            files = {"a.ts": b"A", "b.vue": b"B"}
            self._tree(src, files)
            manifest = self._manifest(tmp, files)
            self.assertEqual(_source_diff(manifest, src), [])

    def test_a_changed_source_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            files = {"a.ts": b"A"}
            self._tree(src, files)
            manifest = self._manifest(tmp, files)
            self._tree(src, {"a.ts": b"A2"})
            problems = _source_diff(manifest, src)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("a.ts", problems[0])

    def test_a_new_source_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            files = {"a.ts": b"A"}
            self._tree(src, files)
            manifest = self._manifest(tmp, files)
            self._tree(src, {"a.ts": b"A", "c.ts": b"C"})
            problems = _source_diff(manifest, src)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("c.ts", problems[0])

    def test_a_deleted_source_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            files = {"a.ts": b"A", "b.ts": b"B"}
            self._tree(src, files)
            manifest = self._manifest(tmp, files)
            os.remove(os.path.join(src, "b.ts"))
            problems = _source_diff(manifest, src)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("b.ts", problems[0])

class SourceConsistencyTests(unittest.TestCase):
    """源码侧：三处展示点必须共用同一个口径，避免改了一处漏了另一处。"""

    def _read(self, rel):
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(path):
            self.skipTest("缺文件: %s" % rel)
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_the_shared_helper_exists(self):
        source = self._read("frontend/src/workbench/composables/costLabel.ts")
        for word in ("free", "local", "unknown"):
            self.assertIn(word, source)

    def test_all_three_display_sites_use_it(self):
        for rel in ("frontend/src/workbench/components/SettingsView.vue",
                    "frontend/src/workbench/components/HarnessPanel.vue",
                    "frontend/src/workbench/components/FlowCanvas.vue"):
            self.assertIn("costLabel", self._read(rel), rel)

    def test_nothing_infers_free_from_a_zero_cost(self):
        """前端不许拿 `cost === 0` 反推"免费"——那正是原先的毛病。"""
        for rel in ("frontend/src/workbench/components/SettingsView.vue",
                    "frontend/src/workbench/components/HarnessPanel.vue",
                    "frontend/src/workbench/components/FlowCanvas.vue"):
            source = self._read(rel)
            # 允许注释里讨论这件事，只禁止真的写成判断条件
            code = re.sub(r"//.*|/\*.*?\*/|\*.*", "", source, flags=re.S)
            self.assertNotRegex(
                code, r"cost\s*[=!]=\s*0|0\s*==\s*\w*cost",
                "%s 里出现了拿 0 反推状态的判断——必须读后端给的 pricing_state" % rel)

    def test_the_trace_type_declares_the_state(self):
        self.assertIn("pricing_state", self._read("frontend/src/workbench/api/harness.ts"))


if __name__ == "__main__":
    unittest.main()
