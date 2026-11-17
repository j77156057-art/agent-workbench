"""默认依赖清单不再包含 torch —— 它跟着 sentence-transformers 一起被拆到可选档。

背景（实测，不是推测）：
- `sentence-transformers` 是 `cross_encoder` 重排的可选依赖，却会把 torch 硬拉进来。
  干净 venv 里拆开前实测：torch 513.7 MB / 14,603 文件，整棵子树
  （torch + scipy + transformers + sympy + sklearn + networkx + ...）
  **889.5 MB / 30,514 文件**，占 site-packages 54.3%，而默认路径一次都不 import。
- linux 上 PyPI 的 torch 还会拖 `nvidia-*-cu13` + `triton` + `cudnn`，
  一次安装 3.27 GB（run 41411405586 的 sqlite lane 直接死于 No space left）。
- 现在默认清单只装真正要用的东西；CI 也不再需要从 PyTorch CPU 索引预装 torch。

本文件测的是**依赖形状**，不是重排功能：
1. `requirements.txt` 不含 sentence-transformers / torch；
2. `requirements-ml.txt` 承接它，并写清「什么时候需要」；
3. CI 的安装步骤只有一条 `-r requirements.txt`，没有偷偷预装 torch；
4. `retrieval.py` 里的导入仍在函数体内（懒加载），所以没装 s-t 也不影响启动；
5. `packaging` 仍被声明（PEP 440 语义断言要用它）。
"""

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_ML = ROOT / "requirements-ml.txt"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"

#: 默认档里绝不该出现的发行包（sentence-transformers 的直接依赖 + 它的传递依赖）。
ML_ONLY_PACKAGES = ("sentence-transformers", "sentence_transformers", "torch",
                    "transformers", "scipy", "scikit-learn", "sympy", "networkx")


def _steps():
    with open(CI_FILE, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return [s for s in doc["jobs"]["test"].get("steps") or [] if isinstance(s, dict)]


def _code(step) -> str:
    """命令文本，剥掉 `#` 注释行 —— 注释里会出现被断言的那些词。"""
    run = str(step.get("run") or "")
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


def _declared(path: Path) -> list[str]:
    """按 requirements 的行格式取包名（去掉注释、标记、版本约束）。"""
    names = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        hit = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if hit:
            names.append(re.sub(r"[-_.]+", "-", hit.group(1)).lower())
    return names


def _top_level_imports(src: str):
    """挑出模块级（顶格）的 sentence_transformers 导入 —— 懒加载的反面形状。"""
    return [ln for ln in src.splitlines()
            if re.match(r"^(from sentence_transformers|import sentence_transformers)\b", ln)]


def _install_step():
    return [s for s in _steps() if "-r requirements.txt" in _code(s)]


class DefaultRequirementsShapeTests(unittest.TestCase):
    def test_default_requirements_do_not_declare_the_ml_stack(self):
        declared = _declared(REQUIREMENTS)
        for pkg in ML_ONLY_PACKAGES:
            self.assertNotIn(pkg, declared,
                             "默认清单里又出现了 %r：它会重新把 torch 拉进每一次安装" % pkg)

    def test_optional_file_carries_sentence_transformers(self):
        self.assertTrue(REQUIREMENTS_ML.is_file(),
                        "requirements-ml.txt 不在：可选重排的安装入口丢了")
        self.assertIn("sentence-transformers", _declared(REQUIREMENTS_ML))

    def test_optional_file_says_when_it_is_needed(self):
        text = REQUIREMENTS_ML.read_text(encoding="utf-8")
        for fragment in ("cross_encoder", "heuristic", "pip install -r requirements-ml.txt"):
            self.assertIn(fragment, text,
                          "可选清单没写清 %r：用户不知道什么时候该装它" % fragment)

    def test_packaging_is_still_declared(self):
        """PEP 440 语义断言（见下）用的量具必须在清单里，不能靠传递安装。"""
        self.assertIn("packaging", _declared(REQUIREMENTS))


class CiInstallStepTests(unittest.TestCase):
    def test_exactly_one_install_step(self):
        hits = _install_step()
        self.assertEqual(len(hits), 1, "装依赖的步骤不唯一：%d" % len(hits))

    def test_no_torch_preinstall_step_remains(self):
        code = _code(_install_step()[0])
        self.assertNotIn(CPU_INDEX, code,
                         "CI 仍在从 PyTorch CPU 索引预装 torch：默认清单已经不需要它")
        self.assertNotIn("/whl/cu", code, "装依赖里混进了 GPU 索引")
        pip_lines = [ln for ln in code.splitlines() if "pip install" in ln]
        self.assertEqual(len(pip_lines), 1,
                         "装依赖应当只剩一行 -r requirements.txt：%s" % pip_lines)
        self.assertNotRegex(code, r"pip install[^\n]*\btorch\b",
                            "CI 仍在显式安装 torch")

    def test_no_optional_ml_file_in_ci(self):
        """CI 只验默认路径；可选档装不装都不该影响 lane。"""
        code = _code(_install_step()[0])
        self.assertNotIn("requirements-ml.txt", code)

    def test_exit_codes_are_not_swallowed(self):
        step = _install_step()[0]
        code = _code(step)
        self.assertFalse(step.get("continue-on-error"), "continue-on-error 会把装不上变成绿的")
        for swallow in ("|| true", "|| :", "2>/dev/null"):
            self.assertNotIn(swallow, code, "退出码被 %r 吞掉了" % swallow)

    def test_step_is_not_parked_on_one_matrix_lane(self):
        step = _install_step()[0]
        self.assertNotIn("if", step, "装依赖被挂上条件：另一条 lane 就没装依赖了")


class LazyImportTests(unittest.TestCase):
    """没装 s-t 时启动不能崩 —— 依据是那句 import 在函数体内，而不是模块级。"""

    def test_product_code_imports_it_lazily(self):
        src = (ROOT / "agent_runtime" / "retrieval.py").read_text(encoding="utf-8")
        self.assertEqual(_top_level_imports(src), [],
                         "sentence_transformers 变成了模块级导入：没装它的环境启动就崩")
        self.assertIn("from sentence_transformers import", src,
                      "函数体内的懒导入不见了：这条可选依赖可能整个被删掉")

    def test_the_lazy_import_detector_is_not_vacuous(self):
        module_level = "from sentence_transformers import CrossEncoder\n\n\ndef f():\n    pass\n"
        lazy = "def f():\n    from sentence_transformers import CrossEncoder\n    return CrossEncoder\n"
        self.assertEqual(_top_level_imports(module_level),
                         ["from sentence_transformers import CrossEncoder"],
                         "判据认不出模块级导入 —— 那它对真实文件也永远是空的")
        self.assertEqual(_top_level_imports(lazy), [], "判据把函数体内的导入也算成模块级")

    def test_missing_package_degrades_instead_of_raising(self):
        """真把包从导入链里拿走：重排应退回原顺序，而不是抛 ImportError。"""
        import agent_runtime.retrieval as retrieval

        class _Doc:
            def __init__(self, text):
                self.page_content = text
                self.metadata = {}

        reranker = retrieval.SentenceTransformerReranker("BAAI/bge-reranker-base")
        docs = [_Doc("b"), _Doc("a")]
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            out = reranker.rerank("q", docs)
        self.assertEqual([d.page_content for d in out], ["b", "a"],
                         "缺包时必须退回原始顺序，而不是重排或抛异常")
        self.assertTrue(reranker.error, "降级没有留痕：error 为空，事后无法区分「没装」和「排序一致」")


class PackagingSemanticsTests(unittest.TestCase):
    def test_cpu_local_version_satisfies_the_torch_constraint(self):
        """`+cpu` 本地版本号能满足 `>=` 约束 —— requirements-ml.txt 解析的前提仍成立。"""
        from packaging.specifiers import SpecifierSet

        self.assertTrue(SpecifierSet(">=2.2").contains("2.15.0+cpu"),
                        "本地版本号不再满足 >= 约束：装 CPU 档时会被换回 CUDA 那份")
        self.assertFalse(SpecifierSet(">=2.2").contains("2.1.0+cpu"),
                         "约束判据反了：低于 2.2 也算满足")


class SiblingStepsSurviveTests(unittest.TestCase):
    def test_the_gates_next_to_it_still_own_their_commands(self):
        joined = "\n".join(_code(s) for s in _steps())
        for fragment, label in (("ruff check", "静态缺陷门"),
                                ("npm run lint", "前端 lint 门"),
                                ("coverage_eval", "改动行覆盖率门"),
                                ("coverage json", "覆盖率报告那一步"),
                                ("npm run test:node", "前端行为测试")):
            self.assertIn(fragment, joined, "%s 的命令不见了" % label)

    def test_the_setup_actions_are_still_where_they_were(self):
        uses = [str(s.get("uses") or "") for s in _steps()]
        for repo in ("actions/checkout", "actions/setup-python", "actions/setup-node"):
            self.assertTrue(any(u.startswith(repo + "@") for u in uses), "%s 步骤没了" % repo)


if __name__ == "__main__":
    unittest.main()
