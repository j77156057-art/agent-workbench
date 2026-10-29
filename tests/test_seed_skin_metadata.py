# -*- coding: utf-8 -*-
"""种子皮肤的展示元信息必须和生成物一致。

`skins.ts` 里 seed 项的 name / desc / swatch 是手写的，生成器只重写
`skins-seed.css`。实测发生过一次：--new-seed 之后皮肤选择器还显示旧种子号、
旧圆角与相反的深浅描述 —— 用户看到的是错误的皮肤说明。
这里从生成物反查，把两者钉在一起。
"""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CSS = ROOT / "frontend" / "src" / "workbench" / "skins-seed.css"
TS = ROOT / "frontend" / "src" / "workbench" / "skins.ts"


class SeedSkinMetadataTests(unittest.TestCase):
    def setUp(self):
        self.css = CSS.read_text(encoding="utf-8")
        self.ts = TS.read_text(encoding="utf-8")
        self.seed = re.search(r"种子：(\d+)", self.css).group(1)
        self.radius = re.search(r"--r-md:\s*(\d+)px", self.css).group(1)
        self.dark = re.search(r"--skin-dark:\s*(\d)", self.css).group(1) == "1"
        self.line = next(
            line for line in self.ts.splitlines() if "key: 'seed'" in line)

    def test_picker_name_matches_generated_seed(self):
        self.assertIn(
            "name: '种子 %s'" % self.seed, self.ts,
            "skins.ts 的 seed 名称与 skins-seed.css 的种子号不一致 —— "
            "换种子后忘了同步展示文案")

    def test_picker_desc_matches_generated_radius_and_base(self):
        self.assertIn("%spx 圆角" % self.radius, self.line,
                      "desc 里的圆角与生成物 --r-md=%spx 不一致" % self.radius)
        base = "深底" if self.dark else "浅底"
        self.assertIn(base, self.line, "desc 的深浅与 --skin-dark 不一致")

    def test_picker_swatch_uses_generated_colors(self):
        for token in ("--bg", "--text", "--accent"):
            color = re.search(r"%s:\s*(#[0-9a-fA-F]{6})" % token, self.css).group(1)
            self.assertIn(color, self.line,
                          "swatch 里缺少生成物的 %s（%s）" % (token, color))


if __name__ == "__main__":
    unittest.main()
