# 引用精度（Citation Precision）指标设计

> **恢复说明（2026-11-01）**：本文件由项目内 DocMind agent 于 12:16 生成（3923 字节），
> 随后被 Qoder 当成评测污染误删。会话存储 `.docmind_sessions/web-d8b93fc4b07d4c069415c303.json`
> 的 `resume_context` 里保留了前约 3.0 KB 原文，已逐字恢复；被截断的「案例 2 后半 + 案例 3」
> 由 Qoder 按同一结构补写。原稿里的示例路径是 Godot 工程的（`player.gd` / `enemy.gd`），
> 与本仓库无关，已换成本仓库真实坐标。全文标注哪些是查到的事实、哪些是推断。
> 每条 `path:line` 由 `.tmp/check_docs_refs.py` 校验「文件存在 + 行号不越界」。

## 一、2025 年工业界怎么评引用准确性（引用准确性 / groundedness）

### 1. GaRAGe 基准（Grounding Annotations for RAG Evaluation）

- **事实**：arXiv:2506.07671（ACL 2025 Findings，amazon-science/GaRAGe）。人工策划长答案，
  并**为答案的每一句标注其支撑段落（grounding passage）**，从而细粒度判断「这句话到底有没有来源撑着」。
  https://arxiv.org/abs/2506.07671 　https://aclanthology.org/2025.findings-acl.875.pdf
- **误判点**：依赖高质量人工标注，成本高；对引用格式变化敏感（路径写法、行号表示法一变就判不上）；
  标注的是「段落级支撑」，**不检查是否精确到行**，也不管引用内容与上下文是否真的相关。

### 2. ALCE：引用的 precision / recall / NLI

- **事实**：「Enabling Large Language Models to Generate Text with Citations」（Gao, Yen, Yu, Chen，EMNLP 2023）。
  把引用当成可计数的对象：流畅度 + **正确性（引用的段落能否支持该句）** + **引用质量（precision/recall，用 NLI 判定）**。
  https://aclanthology.org/2023.emnlp-main.741/
  （**未确认**：该文 arXiv 编号在多篇综述里分别写成 2305.13669 / 2305.13888 / 2305.14627，互相矛盾，
  故本文件只挂 ACL Anthology 这个可复核的锚点。）
- **误判点**：NLI 模型自身会错，把「同义复述」判成「不支持」→ 假阴性（漏报，更危险）；
  只在**段落**粒度比对，行号写错一样过；对「引用了一堆但每句只挂最宽松的那条」不敏感。

### 3. FActScore：原子事实 + 精确率

- **事实**：arXiv:2305.14251（EMNLP 2023）。把长答案拆成**原子事实**，逐条比对是否被参考资料支持，
  用「被支持的原子事实占比」当精确率。https://arxiv.org/abs/2305.14251 　https://github.com/shmsw25/factscore
- **误判点**：拆分本身是 LLM 做的，拆得不一样分数就漂；只算 precision 不算 recall，
  「一条正确引用 + 九条无证据断言」能拿到 0.1，看起来像全对；对齐参考资料需要检索，检索错就整批冤枉。

### 4. 商用 RAG 评测指南的常见做法（2025）

- **事实**：把 groundedness / faithfulness / citation accuracy 列为必测项，主流做法是
  **LLM-as-judge + 检索片段回溯比对**，并按「每条声明 → 证据」配对打分。
  https://www.getmaxim.ai/articles/complete-guide-to-rag-evaluation-metrics-methods-and-best-practices-for-2025/
  https://futureagi.com/blog/rag-evaluation-metrics-2025/ 　https://galileo.ai/blog/rag-evaluation-tools
- **误判点（推断）**：整套指标跑一次就要调一次模型 → **不确定、不可复现、进不了 CI**。
  本仓库的判分器是确定性规则优先（`agent_eval.py:3` 的模块定位：「默认走确定性规则，离线可跑，适合 CI」），
  所以工业界的 LLM-judge 路线只能当补充档，不能当门。

## 二、本仓库落地的引用精度指标设计

**推断（本文件的核心主张）**：代码库问答场景下，「引用准确性」有一个比 LLM-judge 硬得多的判据——
**回答里写出的（文件，行号）必须在这个 root 下真实存在、行号不越界、且那一行确实含被引的东西**。
这三条全是确定性检查，不需要模型，不需要标注，用现有 golden/record 结构就能算。

现状可复用的一半：活体侧已经在做同类检查（`references_exist` 见 `agent_runtime/output_audit.py:773`，
`reference_lines_valid` 见 `agent_runtime/output_audit.py:774`，入口 `agent.py:3363`，
人话文案 `agent.py:3428`、`agent.py:3429`）。**缺的是离线侧**：`agent_eval.py` 的 `score_record`
（`agent_eval.py:78`）没有任何一条 check 会去看回答里引用的坐标是真是假。

### 函数签名

```python
def citation_precision_score(rec, code_root=".", strict_mode=True, *, judge=None):
    """引用精度：答案里的 (文件, 行号) 在本 root 下真实存在且指向相关代码的比例。

    Args:
        rec: 单条结果记录（results.jsonl 的一行，见 run_golden.py:392 的 rec 构造）
        code_root: 判定时用的仓库根（必须是用户在看的那个 root，不是 shadow 副本）
        strict_mode: True 时行号越界即算这条引用失效；False 只查文件存在性
        judge: 可选的语义相关性判定（默认 None = 只做符号名命中，不调模型）

    Returns:
        dict: {"refs": int, "valid": int, "precision": float,
               "checks": [...], "scored": bool, "passed": bool}
    """
```

签名对齐现有判分器：`score_record(rec, llm=None, judge=False)`（`agent_eval.py:78`）——
返回值里同样带 `checks` 列表，因此可以直接 `add()` 进现有 checks 而不动汇总逻辑。

### 判定步骤

1. **提取引用模式**：从 `rec["final"]`（取法同 `agent_eval.py:39` 的 `_final`）里抽
   `文件名:行号` / `相对/路径.py:123-145` 形式的坐标；行号支持区间。
2. **验证文件存在性**：路径先相对 `code_root` 解析，解析后必须仍在 `code_root` 内
   （越界的直接判失效，同 `agent_runtime/output_audit.py:134` `_project_root_claims` 的 root 口径）。
3. **验证行号有效性**：`1 <= 行号 <= 该文件实际行数`；`strict_mode` 下越界即失效。
4. **验证内容相关性**：命中行（或其 ±1 上下文窗口）必须含**题目里点名的标识符**，
   且不是纯注释/空行/docstring；无标识符可比时降级为「只查存在性」并如实标注。
5. **综合评分**：`precision = valid / refs`，逐条生成 check 塞进现有 `checks`。
   与 `score` 的关系不变：`score` 仍是过了几条除以几条（`agent_eval.py:148`），
   是否算过仍由「全部 checks 为真」决定（`agent_eval.py:143`）。

### 3 个误判边界案例

#### 案例 1：文件存在但行号越界

- **现象（示例坐标，故意越界，非本仓库引用）**：答案引用 `agent_eval.py:999`，而该文件实际只有 318 行。
- **误判风险**：只查文件存在性的指标会放行（工业界段落级指标普遍如此，见上文 GaRAGe 误判点）。
- **处理**：`strict_mode` 下标为「行号越界」，计入失效引用；活体侧同判据文案见 `agent.py:3429`。

#### 案例 2：坐标真实但内容无关

- **现象**：结论是「`passed` 是所有 checks 的合取」，答案却引用 `agent_eval.py:1`
  ——那行是模块 docstring `"""黄金题自动打分 + baseline 回归门。`，字在，式不在（真式在 `:143`）。
- **误判风险**：存在性 + 行号有效性全绿；段落级 NLI 也可能认为「这个文件确实讲打分」而放行。
- **处理**：第 4 步要求命中行含被引标识符（`passed`）且非 docstring/注释行，判为相关性失败。

#### 案例 3：路径真实但属于另一份工作树副本（Qoder 补写）

- **现象**：本仓库在 `.docmind/projects/*/headcheck/*/` 下有整仓副本
  （已知缺陷记录：`docs/known-defect-docmind-shadow-repo-copies.md`）。答案引用
  `agent_eval.py:143` 时，若 root 被解析成副本目录，文件存在、行号也合法，检查全绿——
  但用户在他的 root 里根本看不到那份行内容（副本可能是旧提交）。
- **误判风险**：纯 filesystem 检查**结构上无法发现**这种错，因为「存在」是真的。
- **处理**：第 2 步强制 `in_root`（解析后必须以 `code_root` 为前缀），并把 `repo_rev` 记进结果行；
  跨提交比较时改用 `git show <rev>:path` 取那一行，而不是读工作树当前内容。
  这也正是本文件「坐标会漂」的处置口径。

## 三、接进现有 expect，不动发布门

- **事实**：题库结构是 `{id, level, question, expect, note}`，`expect` 的键直接驱动 checks
  （例如 `golden/questions_v2.json` 里 G01 用 `must_include` / `any_of_call` / `min_actions` / `max_actions` / `no_error`）。
- **推断**：加两个键即可，`require_citations`（≥N 条引用）与 `min_citation_precision`（阈值），
  由 `score_record` 内部调用本函数、把结果 `add()` 进 `checks`。
- **事实**：这么做会自动被发布门吃到 —— `agent_eval.py:255` 是
  `if report["failed"] > 0 or report["unscored"] > 0`，且 `compare_to_baseline`
  只看每题 `passed`（`agent_eval.py:207`、`agent_eval.py:210`）。
- **边界**：一题若 `refs == 0`，本指标不该产出一条「成功」的 check —— 按 `agent_eval.py:142`
  的 `scored = bool(checks)` 语义，应让它进 `unscored`（`agent_eval.py:198`）而不是冒充满分。
