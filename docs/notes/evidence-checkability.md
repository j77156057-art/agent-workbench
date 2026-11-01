# 证据可核性指标（Evidence Checkability Metric, ECM）

> **本文档状态**：设计草案，待评审与实现
> **作者**：DocMind Agent
> **最后更新**：2026-11-01
>
> **恢复说明（Qoder 补，2026-11-01）**：本文件由 DocMind agent 于 12:23 生成（7167 字节），
> 随后被 Qoder 当成评测污染误删。会话存储
> `.docmind_sessions/web-e3cbd52264134b2db56d20ba.json` 的 `resume_context` 里保留了前约 2.0 KB
> （到「判定步骤」第 4 条），已逐字恢复；被截断的尾部（边界案例、最小实现、pytest 骨架）由 Qoder
> 按原稿结构与要求补写。原稿「外部现状」里两条链接无法复核，已改标 **未确认** 并补进可复核来源。
> 每条 `path:line` 由 `.tmp/check_docs_refs.py` 校验「文件存在 + 行号不越界」。
> 全文继续标注**事实 / 推断**。

## 外部现状（查到的事实）

根据 2025-2026 年业界对 RAG/Agent 系统的评估研究，citation accuracy（引用准确性）和
groundedness（事实依据性）是两个关键评估维度：

1. **GaRAGe 基准**（2025，可复核）：arXiv:2506.07671 / ACL 2025 Findings，
   人工策划长答案并**逐句标注 grounding passage**，做细粒度 groundedness 评估。
   https://arxiv.org/abs/2506.07671 　代码 https://github.com/amazon-science/GaRAGe
2. **ALCE**（EMNLP 2023，可复核）：把引用当可计数对象，给出流畅度 / 正确性 /
   引用质量（precision + recall，用 NLI 判「段落是否支持该句」）。
   https://aclanthology.org/2023.emnlp-main.741/
   （**未确认**：其 arXiv 编号在多篇综述里互相矛盾，故只挂 Anthology 锚点。）
3. **FActScore**（EMNLP 2023，可复核）：答案拆原子事实，逐条回溯证据，取「被支持比例」为精确率。
   https://arxiv.org/abs/2305.14251 　https://github.com/shmsw25/factscore
4. **商用评测指南（2025）**：groundedness / faithfulness / citation accuracy 列为必测，
   主流实现是 LLM-as-judge + 检索片段回溯。
   https://www.getmaxim.ai/articles/complete-guide-to-rag-evaluation-metrics-methods-and-best-practices-for-2025/
   https://futureagi.com/blog/rag-evaluation-metrics-2025/ 　https://galileo.ai/blog/rag-evaluation-tools
5. **Q2D-Web 基准**（原稿所列，**未确认**）：原稿写「面向智能体式 RAG 的大规模评测，含 agent citations
   三套相关性标注」，链接为 https://hub.baai.ac.cn/paper/7c8edaf2-84f5-4d31-a650-1bb33966be7f ——
   Qoder 本轮未能独立复核该条目，**不要当已核实事实引用**。
6. 原稿另引 https://arxiv.org/abs/2601.14949（「学术引用预测基准，要求精确到段落与数据点」）——
   编号与内容均**未确认**，保留原样但按未确认对待。

> **共同缺口（推断）**：以上路线要么靠人工标注（GaRAGe），要么靠模型判定（ALCE 的 NLI、
> FActScore 的拆分、商用 LLM-judge）。**都不满足本仓库判分器的既有前提**：
> 确定性规则优先、离线可跑、进 CI —— 见 `agent_eval.py:3` 的模块定位。

## 指标定义（我的推断）

基于现有 `agent_eval.py` 的 `expect` 结构和 golden 题库的设计理念，提出「证据可核性」指标
（Evidence Checkability Metric, ECM），核心定义为：

> **每条结论都必须能落到（文件，行号）的具体位置，且该行内容与结论语义相关；
> 无法定位到具体文件行号的结论，其可核性得分为 0。**

ECM 不是单一数字，而是一组可组合的检查项，直接复用 `agent_eval.py` 的 `expect` 字段结构。

**Qoder 补的口径约束**：定义里的「语义相关」如果不给确定性判据，就会退化成又一次 LLM-judge，
破坏上面那条前提。所以 ECM 把「相关」降级成**可比符号**：命中行必须含题目点名的标识符
（或题目 `expect.must_include` 里的串），且不是空行/纯注释/docstring。这条是确定性检查；
模型判定只作为可选加强档（同 `score_record(rec, llm=None, judge=False)`，`agent_eval.py:78`）。

## 判定步骤（我的推断）

ECM 的判定步骤完全集成到现有 `agent_eval.py` 打分流程中，只需扩展 `expect` 支持新检查项：

1. **文件定位检查**（`must_locate_in_file`）：答案必须含具体文件路径，且该路径在本项目代码库中存在。
2. **行号定位检查**（`must_locate_at_line`）：答案必须含具体行号（`:123` / `L123`），且在文件有效范围内。
3. **语义相关性检查**（`must_semantically_relate`）：引用行内容必须与结论相关，不能是无关代码行或注释。
4. **多源交叉验证**（`cross_verify_sources`）：复杂结论要求至少两个独立（文件, 行号）且内容互证。

这些检查项可直接加进 `expect`，无需改 `agent_eval.py` 核心。

**Qoder 补的实现约束**：

- 现有键的命名风格是 `must_include` / `any_of_call` / `min_actions`（事实：见
  `golden/questions_v2.json` 的 G01，以及 `score_record` 里对它们的消费，`agent_eval.py:78` 起）。
  上面 4 个动词式键与风格不符，落地时建议改成数值阈值型：`require_citations`、
  `min_evidence_checkability`，把「定位/行号/相关」合成一个比例指标，避免一题挂 4 个键却只能全过或全不过。
- 产出的每条检查必须走 `score_record` 内部的 `add()`（`agent_eval.py:85`），
  这样 `checks` 的合取语义（`agent_eval.py:143`：`passed = scored and all(c["ok"] for c in checks)`）
  与比例语义（`agent_eval.py:148`）都自动继承，不必另造一套汇总。
- **不产伪绿**：答案一条引用都没有时（`refs == 0`），不能报「可核性 100%」。按
  `agent_eval.py:142`（`scored = bool(checks)`）的语义让它落进 `unscored`
  （`agent_eval.py:198`），并被发布门当失败处理（`agent_eval.py:255`）。

## 边界案例（Qoder 补写）

1. **坐标会漂**：引用 `agent_eval.py:143` 在当前提交正确；同一份答案在下一个提交可能被检出失效，
   因为行号会移动。**处理**：结果行必须记 `repo_rev`；跨提交比对用
   `git show <rev>:agent_eval.py` 取那一行，而不是读工作树。回归门本身只认 `passed`
   （`agent_eval.py:207` 的 `compare_to_baseline`、`agent_eval.py:210` 的 `base_map`），
   所以坐标漂移必须在跑分当下就判，不能留给 baseline 对比。
2. **行号对但那一行是注释**：见 `docs/notes/citation-precision.md` 案例 2
   （`agent_eval.py:1` 是模块 docstring，不含被引表达式）。**处理**：符号命中 + 排除注释/docstring 行。
3. **控制行撞车**：题目要求引用 `MIN_TASK_TOKEN_BUDGET` 所在行，答案引用了同一标识符在注释里出现的行 ——
   文件存在、行号合法、标识符也命中，但那是解释文字不是定义。**处理**：命中行需通过「含赋值/定义形态」的
   次级判据，或要求题目 `expect` 明确 `require_definition_line`；否则如实标成「相关性判定降级」。
4. **路径来自工具输出、带 Windows 分隔符**：`grep`/`dir` 的观测里常见 `agent_runtime\memory\health.py:88`
   这种写法。存在性检查若只做 POSIX 前缀比较会误判越界。**处理**：提取阶段把 `\` 归一为 `/`
   再解析；活体侧已有同类口径（`agent_runtime/output_audit.py:488` 的 `_observed_ref_paths` 从工具观测里取路径）。

## 最小实现（Qoder 补写）

```python
# agent_eval.py 内新增（确定性，无新依赖）
def evidence_checkability(rec, *, root, rev="", judge=None):
    """回答里每条 (文件, 行号) 是否真存在、行号不越界、且含被引符号。返回 checks 列表。"""
    final = _final(rec)                       # 复用 agent_eval.py:39
    refs = _extract_refs(final)               # 示例：需支持 a.py:12-15 区间，且把 \ 归一为 /
    if not refs:
        return [], False                      # scored=False -> 进 unscored
    checks = []
    for path, lo, hi in refs:
        lines = _read_lines_at(root, rev, path)   # rev 非空则 git show
        if lines is None:
            checks.append({"name": f"ref:{path}", "ok": False, "detail": "文件打不开或越出 root"})
            continue
        if not (1 <= lo <= hi <= len(lines)):
            checks.append({"name": f"ref:{path}:{lo}", "ok": False, "detail": f"行号越界（实 {len(lines)} 行）"})
            continue
        blob = "\n".join(lines[lo - 1:hi])
        ok = _mentions_cited_symbol(blob, rec)     # 非注释/含标识符
        checks.append({"name": f"ref:{path}:{lo}-{hi}", "ok": ok,
                       "detail": "" if ok else "命中行不含被引符号"})
    return checks, True
```

挂载点：`score_record` 末尾，把返回的 checks 逐条 `add()`；`expect` 新增
`require_citations`（整数字，最小引用条数）与 `min_evidence_checkability`（0~1 阈值）。

### pytest 用例骨架

```python
# tests/test_evidence_checkability.py
import json, subprocess, pytest
from agent_eval import evidence_checkability, score_record

@pytest.fixture(scope="module")
def rev():
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()

def test_real_coordinate_with_symbol_passes(root_dir, rev):
    rec = {"id": "X1", "final": "判定式在 agent_eval.py:143"}
    checks, scored = evidence_checkability(rec, root=root_dir, rev=rev)
    assert scored and len(checks) == 1 and checks[0]["ok"]

def test_line_out_of_range_fails(root_dir, rev):
    rec = {"id": "X2", "final": "见 agent_eval.py:99999"}   # 示例坐标，故意越界
    checks, _ = evidence_checkability(rec, root=root_dir, rev=rev)
    assert checks and not checks[0]["ok"] and "越界" in checks[0]["detail"]

def test_docstring_line_fails_relevance(root_dir, rev):
    rec = {"id": "X3", "expect": {"must_include": ["passed"]},
           "final": "见 agent_eval.py:1"}
    checks, _ = evidence_checkability(rec, root=root_dir, rev=rev)
    assert checks and not checks[0]["ok"]

def test_zero_refs_is_not_scored(root_dir, rev):
    checks, scored = evidence_checkability({"id": "X4", "final": "总之没问题"},
                                           root=root_dir, rev=rev)
    assert checks == [] and scored is False

def test_backslash_path_is_normalised(root_dir, rev):
    rec = {"id": "X5", "final": r"见 agent_runtime\memory\health.py:88"}
    checks, scored = evidence_checkability(rec, root=root_dir, rev=rev)
    assert scored and checks[0]["ok"], checks
```

**先证伪自己（纪律）**：`test_real_coordinate_with_symbol_passes` 与
`test_docstring_line_fails_relevance` 必须成对存在 —— 少了后者，第 3 步「相关性」改成恒真也不会被抓到；
少了前者，实现把一切判死也照样绿。两条一起才让这条检查有牙。
