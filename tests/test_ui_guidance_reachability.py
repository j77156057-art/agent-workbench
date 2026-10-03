"""用户界面里**存在**它文案所承诺的控件 —— 一类"指向不存在的东西"的兜底。

## 又一个同形 bug

报错文案写着：

    或在「⚙ 模型设置」把 embedding_provider 换成云端嵌入服务。

而那时的 `ModelSettingsDialog.vue` 里**根本没有 embedding_provider 这个控件**。
后端 `api.py` 早就完整支持（`ModelSaveReq.embedding_provider` +
`set_embedding_provider` + `reset_collection` + 清空提示），**只有前端从未发送过
这个字段**。

结果是：用户拿到一条**无法执行的建议**，只能去装 Ollama——尽管他真正要的云端
对话根本不依赖它。

这与本项目最近修的一串 bug 完全同形：

| |表面症状 | 真实原因 |
|---|---|---|
| 11 个写盘工具 | 测试全绿 | 断言的是运行期不读的死副本 |
| 轮次快照 | 写入无报错 | 字段漏搬，写得进读不回来 |
| `task_branch` | 端点"坏了" | 少一个 import |
| **本文件的指引** | **建议无法执行** | **控件从未接线** |

根因都一样：**没有一层检查去验证"承诺的东西真的存在"**，而不是验证"代码写了"。

## 这个文件做什么

两条断言，都是静态可查的：

1. 文案里出现的每个「去某处改某项」，那一项在对应组件里**真的有绑定**
   （`v-model` 或 `modelApi.save` 的载荷字段）
2. 后端受理的每个模型配置字段，前端**真的发得出去**

它不检查界面好不好看、不检查交互是否合理——只检查"承诺"与"实现"是否对得上。
"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend", "src")


def _read(*parts):
    with open(os.path.join(FRONTEND, *parts), encoding="utf-8-sig") as handle:
        return handle.read()


def _api():
    with open(os.path.join(ROOT, "api.py"), encoding="utf-8-sig") as handle:
        return handle.read()


def _embeddings():
    with open(os.path.join(ROOT, "embeddings.py"), encoding="utf-8-sig") as handle:
        return handle.read()


def _interface_body(src, name):
    """取出 `export interface <name> { ... }` 的那一段。

    截到下一个 `export interface` 为止——否则「字段在 SaveReq 里」这条断言会被
    后面的接口误判为通过，变成一盏永远绿的灯。
    """
    start = src.index("export interface %s" % name)
    rest = src[start:]
    nxt = rest.find("export interface", 1)
    return rest if nxt == -1 else rest[:nxt]


class RemediationInstructionsAreActionableTests(unittest.TestCase):
    """报错/提示文案里承诺的每一项，用户都得真能在界面上找到。"""

    def test_embedding_provider_has_a_real_control(self):
        """本轮的直接受害者：文案让用户改embedding_provider，控件却不存在。"""
        dialog = _read("workbench", "components", "ModelSettingsDialog.vue")
        self.assertIn(
            'v-model="embeddingProvider"', dialog,
            "设置弹窗里没有 embedding_provider 控件——而 api.py 的引导文案"
            "明确让用户「在⚙ 模型设置里把 embedding_provider 换成云端服务」。"
            "那条建议因此无法执行。")
        self.assertIn(
            "embedding_provider: embeddingProvider.value", dialog,
            "控件存在但保存时没把值发出去：用户选了等于没选，"
            "而后端早就支持这个字段。")

    def test_the_control_is_labelled_as_optional(self):
        """措辞必须说清它只服务上传文档，否则「没配向量」看起来像环境不完整。

        代码检索走 grep / read_file，全程不碰向量——这跟主流 harness 一致。
        """
        dialog = _read("workbench", "components", "ModelSettingsDialog.vue")
        self.assertIn("可选", dialog,
                      "知识库检索后端必须标成可选——它是辅助能力，"
                      "不该让人以为不配它整套环境就不完整")
        self.assertIn("不影响问答与代码检索", dialog,
                      "必须说清它与问答/代码检索无关：代码检索走 grep，本来就不用向量")

    def test_the_cloud_embedding_option_states_its_key_requirement(self):
        """云端嵌入复用**对话模型的 Key**（embeddings.py 的 llm_api_key）。

        对话模型不是 qwen 时，用户填的是别家厂商的 Key，打 DashScope 会 401。
        这个约束必须在界面上说破，不能让人「选了才发现不能用」。
        """
        dialog = _read("workbench", "components", "ModelSettingsDialog.vue")
        self.assertIn("DASHSCOPE_API_KEY", dialog,
                      "云端嵌入的 Key 来源没说明：它复用对话模型的 Key，"
                      "对话模型不是 qwen 时必然认证失败")

    def test_the_backend_field_is_declared_in_the_frontend_type(self):
        """字段没写进类型，「漏传」就只能靠人眼发现。"""
        model_api = _read("workbench", "api", "model.ts")
        self.assertIn("export interface SaveModelReq", model_api)
        body = _interface_body(model_api, "SaveModelReq")
        self.assertIn(
            "embedding_provider?", body,
            "SaveModelReq 里没有 embedding_provider——后端受理它，前端类型里却没有，"
            "于是漏传不会有任何编译期提示")


class BackendAcceptsWhatTheUiSendsTests(unittest.TestCase):
    """反方向：前端发的，后端得收。"""

    def test_the_save_handler_acts_on_embedding_provider(self):
        """控件要真能改到状态，否则又是一个"选了等于没选"。"""
        src = _api()
        self.assertIn("set_embedding_provider(req.embedding_provider)", src)
        # 切换向量后端必须重建集合：不同后端维度不同（bge-m3=1024 / local=256），
        # 沿用旧向量会让检索静默出错。
        self.assertIn("reset_collection()", src,
                      "切换嵌入后端后没重建集合：新旧向量维度不通用，"
                      "沿用旧向量会静默返回错误结果")


class NoStrayDeadRemedyTests(unittest.TestCase):
    """扫描文案里的「去某处改某项」，防止又指向不存在的控件。"""

    #: (文案所在文件, 必须能对应到真实绑定的字段)
    GUIDANCE_TARGETS = [
        ("embedding_provider", "workbench", "components", "ModelSettingsDialog.vue"),
    ]

    def test_every_referenced_setting_exists_in_the_ui(self):
        api_src = _api()
        for field, *ui_parts in self.GUIDANCE_TARGETS:
            self.assertIn(field, api_src, "文案里没提到 %s，本用例跳过" % field)
            ui_src = _read(*ui_parts)
            self.assertTrue(
                ('v-model="embeddingProvider"' in ui_src)
                or ("%s:" % field in ui_src),
                "文案提到了 %s，但 %s 里没有对应控件" % (field, ui_parts[-1]))

    def test_ui_does_not_advertise_settings_that_do_not_exist(self):
        """反向检查：界面上教的每个环境变量，后端必须真的读它。

        否则会出现「界面上教用户设一个根本不生效的环境变量」——和本文档开头
        那个 bug 是同一个形状，只是方向相反。

        判别标准是**出现在引号里的字符串**：环境变量在文案里一定被写成
        ``DASHSCOPE_API_KEY`` 这样的字面量。而 `ASK_PREFS_EVENT` 这种 SCREAMING_CASE
        的标识符是前端的 DOM 事件名（``addEventListener(ASK_PREFS_EVENT, ...)``），
        不是环境变量——第一版正则把它误判了，教训是：判"环境变量"不能只看字形。
        """
        dialog = _read("workbench", "components", "ModelSettingsDialog.vue")
        backend = _api() + _embeddings()
        quoted = set(re.findall(r"['\"]([A-Z][A-Z0-9_]{5,})['\"]", dialog))
        for var in sorted(quoted):
            self.assertIn(
                var, backend,
                "界面教用户设置环境变量 %s，但后端从不读它——这条指引是假的" % var)


if __name__ == "__main__":
    unittest.main()