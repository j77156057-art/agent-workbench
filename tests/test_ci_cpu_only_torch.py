"""CI 装依赖时用的是 CPU 版 torch，而不是拖着整套 CUDA 的 PyPI 那份。

现象（不是推测）：run 41411405586 的 sqlite lane 在 `Install dependencies` 里死于
`OSError: [Errno 28] No space left on device`，位置就在 `Installing collected packages`
解包那一段；同一份 `requirements.txt` 在另一台 runner（同一个 commit 的重跑与上一轮 run）
上过了。也就是说**每条 lane 都在拿磁盘余量赌运气**，而红的时候只会显示一句 pip 的
errno 28，看不出是结构性问题。

量到的数：
- `sentence-transformers`（`requirements.txt:69`）把 PyPI 的 `torch` 连带
  `nvidia-cudnn-cu13 / cublas / cusolver / cusparse / cufft / nccl / nvshmem / triton`
  一起拉进来。按该步骤日志里的每条 MB 级下载合计：**3.27 GB**，步骤耗时 **2 分 12 秒**。
- GitHub 托管 runner **没有 GPU**，所以这一堆字节永远是死重：装它只会挤磁盘、拖时间，
  换不来任何被验证的行为。
- CPU 索引对得上：抓 `https://download.pytorch.org/whl/cpu/torch/` 的索引页实测有
  `torch-2.15.0+cpu-cp312-cp312-manylinux_2_28_x86_64.whl`，正好是 runner 的
  cp312 / x86_64；而 `+cpu` 这一支不依赖 `nvidia-*`，所以 CUDA 那批根本不会进解析结果。

为什么**顺序**才是关键：PEP 440 下已安装的 `2.15.0+cpu` 满足 `sentence-transformers`
对 torch 的约束，而第二步没有 `--upgrade`，pip 看见「已满足」就不回头换 PyPI 那份。
把两行调了个顺序（先 requirements 再 torch）就回到原样 —— 所以这条必须有断言。
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"
CPU_INDEX = "https://download.pytorch.org/whl/cpu"


def _steps():
    with open(CI_FILE, encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    return [s for s in doc["jobs"]["test"].get("steps") or [] if isinstance(s, dict)]


def _code(step) -> str:
    """命令文本，剥掉 `#` 注释行 —— 注释里会出现被断言的那些词。"""
    run = str(step.get("run") or "")
    return "\n".join(ln for ln in run.splitlines() if not ln.strip().startswith("#"))


def _top_level_imports(src: str):
    """挑出模块级（顶格）的 sentence_transformers 导入 —— 懒加载的反面形状。"""
    return [ln for ln in src.splitlines()
            if re.match(r"^(from sentence_transformers|import sentence_transformers)\b", ln)]


def _install_step():
    hits = [s for s in _steps() if "pip install -r requirements.txt" in _code(s)]
    return hits


class TorchCpuInstallTests(unittest.TestCase):
    def test_install_step_installs_torch_from_the_cpu_index(self):
        hits = _install_step()
        self.assertEqual(len(hits), 1, "装依赖的步骤不唯一：%d" % len(hits))
        code = _code(hits[0])
        self.assertIn(CPU_INDEX, code,
                      "CI 又从 PyPI 拿带 CUDA 的 torch 了：一次 3.27 GB 死重，磁盘余量赌运气")
        torch_lines = [ln for ln in code.splitlines() if CPU_INDEX in ln]
        self.assertEqual(len(torch_lines), 1, "CPU 索引那行不唯一")
        self.assertRegex(torch_lines[0], r"pip install\s+--index-url\s+\S+/whl/cpu\s+torch\s*$",
                         "CPU 索引那一行必须只装 torch 本身：%r" % torch_lines[0].strip())

    def test_cpu_index_comes_before_requirements(self):
        """顺序反了就等于没改：先装 requirements 会把 CUDA 那批先拉下来。"""
        code = _code(_install_step()[0])
        lines = [ln for ln in code.splitlines() if "pip install" in ln]
        self.assertEqual(len(lines), 2, "装依赖应当是两行（torch CPU 先行，再 requirements）：%s" % lines)
        self.assertIn(CPU_INDEX, lines[0], "第一行不是 CPU 索引的 torch")
        self.assertIn("requirements.txt", lines[1], "第二行不是 requirements.txt")

    def test_no_cuda_index_url_sneaks_in(self):
        code = _code(_install_step()[0])
        for bad in ("/whl/cu", "download.pytorch.org/whl/stable"):
            self.assertNotIn(bad, code, "装依赖里混进了 GPU 索引 %r" % bad)

    def test_exit_codes_are_not_swallowed(self):
        step = _install_step()[0]
        code = _code(step)
        self.assertFalse(step.get("continue-on-error"), "continue-on-error 会把装不上变成绿的")
        for swallow in ("|| true", "|| :", "2>/dev/null"):
            self.assertNotIn(swallow, code, "退出码被 %r 吞掉了" % swallow)

    def test_step_is_not_parked_on_one_matrix_lane(self):
        step = _install_step()[0]
        self.assertNotIn("if", step, "装依赖被挂上条件：另一条 lane 就没装依赖了")


class WhyThisIsSafeTests(unittest.TestCase):
    """CPU 版够用的**结构性依据**，不是「看起来没问题」。"""

    def test_sentence_transformers_is_the_thing_that_pulls_torch(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?im)^\s*sentence-transformers\b",
                         "requirements.txt 里没有 sentence-transformers：那这个守卫的前提就变了")

    def test_product_code_imports_it_lazily_not_at_module_level(self):
        """`retrieval.py` 里那句 import 在函数体内 —— 所以 CI 永远不会真去要 GPU 版 torch。

        有人把它提成模块级 import 时，这条必须响：那时 sentence-transformers 进入
        导入链，装没装上就不再是「懒加载可选件」的问题，而 lane 是否还需要 torch
        也得重新量。
        """
        src = (ROOT / "agent_runtime" / "retrieval.py").read_text(encoding="utf-8")
        self.assertEqual(_top_level_imports(src), [],
                         "sentence_transformers 变成了模块级导入：%s" % _top_level_imports(src))
        self.assertIn("from sentence_transformers import", src,
                      "函数体内的懒导入不见了：这条可选依赖可能整个被删掉")

    def test_the_lazy_import_detector_is_not_vacuous(self):
        """判据本身要能认出那个形状，否则它只是在读文件。

        为什么不真去改 `retrieval.py` 做变异：把导入提到模块级会让本机没装的包进入导入链，
        测试收集阶段就 `ModuleNotFoundError` —— 那是一次「为了错误的原因变红」的杀死，
        证明不了任何事。夹具对照才是干净的证伪。
        """
        module_level = "from sentence_transformers import CrossEncoder\n\n\ndef f():\n    pass\n"
        lazy = "def f():\n    from sentence_transformers import CrossEncoder\n    return CrossEncoder\n"
        self.assertEqual(_top_level_imports(module_level),
                         ["from sentence_transformers import CrossEncoder"],
                         "判据认不出模块级导入 —— 那它对真实文件也永远是空的")
        self.assertEqual(_top_level_imports(lazy), [], "判据把函数体内的导入也算成模块级")


class SiblingStepsSurviveTests(unittest.TestCase):
    def test_the_gates_next_to_it_still_own_their_commands(self):
        """改这一步不许把邻门的命令吃掉（YAML 同键后者覆盖前者）。"""
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
