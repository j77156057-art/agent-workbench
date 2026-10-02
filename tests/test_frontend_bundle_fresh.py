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
1. `web/` 产物不比源码旧（时间戳）；
2. 关键标识符**确实进了产物**（光看时间戳不够：构建可能因故没打包那个 chunk）；
3. 产物里确实含用户会看到的中文字符串（`costLabel` 的 LABEL 表）。

第 2、3 条是这个测试真正值钱的地方：时间戳新但内容旧，是比"时间戳旧"更隐蔽的故障。
"""

import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "frontend", "src")
WEB_ASSETS = os.path.join(ROOT, "web", "assets")

#: 用户在界面上真正会看到的字符串。改了它们却没重新构建，页面就会显示旧文案。
EXPECTED_STRINGS = ["该模型免费", "本地模型", "未配置单价，费用未计入"]

#: 源码里的标识符：证明带计价状态的那条链路进了产物。
EXPECTED_IDENTS = ["pricing_state"]


def _newest_mtime(root, patterns=("*.py", "*.ts", "*.vue")):
    newest = 0.0
    for pattern in patterns:
        for path in glob.glob(os.path.join(root, "**", pattern), recursive=True):
            try:
                newest = max(newest, os.path.getmtime(path))
            except OSError:
                continue
    return newest


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

    def test_the_bundle_is_not_older_than_the_sources(self):
        """这条对应真实事故：改了 src 没重建，页面跑的是旧产物。"""
        bundle_newest = _newest_mtime(WEB_ASSETS, ("*.js",))
        source_newest = _newest_mtime(SRC, ("*.ts", "*.vue"))
        self.assertGreater(
            bundle_newest, source_newest,
            "web/assets（%.0f）比 frontend/src（%.0f）旧——页面会加载旧代码。"
            "请跑 `cd frontend && npm run build`。" % (bundle_newest, source_newest))

    def test_the_cost_label_strings_reached_the_bundle(self):
        """时间戳新也不够——构建可能根本没把那个 chunk 打进去。"""
        blob = _read_bundle()
        missing = [text for text in EXPECTED_STRINGS if text not in blob]
        self.assertEqual(missing, [],
                         "这些界面文案不在 web/ 产物里，说明没重新构建：%s" % missing)

    def test_the_pricing_state_plumbing_reached_the_bundle(self):
        blob = _read_bundle()
        missing = [name for name in EXPECTED_IDENTS if name not in blob]
        self.assertEqual(missing, [],
                         "计价状态字段不在 web/ 产物里，说明没重新构建：%s" % missing)


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
