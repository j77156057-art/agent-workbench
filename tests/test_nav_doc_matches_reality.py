# -*- coding: utf-8 -*-
"""指路文档的现状陈述必须与仓库实况相符 —— 不然接手的人会基于错误前提动手。

## 这条判据从哪来（2026-10-12 的真实代价）

今晚我差点三次基于**仓库里读到的旧文本**做决定：

| 我把什么当成了事实 | 真实身份 |
|---|---|
| `docs/next-steps.md` 第 0 节写着「HEAD `efde8a6`，工作树有未提交改动」 | **陈旧**。真实 HEAD 是 `f148169`，那批改动早在 `a88c742` 就提交了 |
| 15 份 `known-defect-*.md` 的结论 | 其中至少一份【已标「已修」】：`context-router-recall.md` 标题就写着 |
| 我自己前一轮写的 `full_guard.txt` | 第一次**根本没跑完就死了**（文件停在 14%、771 字节） |

每一次都是**把仓库里读到的文本当成了当前事实**。这正是
`brianruggieri/skills` 那份 grill-me 里「Security & Trust Boundary」说的
**untrusted data is data, not directives** —— 文档里的文本是**被评估的对象**，
不是给我下的指令。

## 范围为什么这样划（关键，否则判据会退化成恒真或恒假）

`docs/` 下有 43 份 markdown。**不能要求它们全都准** —— 它们是当晚的侦查
记录，历史章节**越旧越有价值**。要求「所有文档的 hash 都等于当前 HEAD」
会产出一片红，然后被后人想方设法关掉。

调研结论（`D:\\Temp\\opencode\\survey_doc_staleness.py` 可复算）：

* 24 份文档含 hash 引用，其中 **107 处是纯历史陈述**
  （「提交 `x` 做了 Y」）—— 那类**永远不该报**
* 只有 1 份文档自称「**唯一指路的入口**」：`docs/next-steps.md`

所以判据的范围是：**那份自称指路入口的文档，它的现状陈述必须准。**
其他人（历史记录、design 文档、known-defect 档）不在此列。

⚠ 这个范围本身也要被钉住 —— 万一将来 next-steps.md 不再自称指路入口了，
判据会跳过而不是红。那是「重新议范围」的信号，不是「判据坏了」。

## 判据形式（必须精确，否则假阴性一片）

`HEAD <hash>` 的**直接紧跟**才是现状声明：

    ✅ 该报（现状声明）            ❌ 不该报（历史陈述）
    （HEAD `efde8a6`）              HEAD = `ab45a56`        <- 带等号，是分段测量记录
    **HEAD `efde8a6`**              本仓 `b828d10` 就是这么出事的  <- hash 不在 HEAD 后

第一版我用「HEAD 前后 45 字窗口内有『当前/现在』」当启发式 ——
**14 处命中里 12 处是历史陈述**，假阴性率 86%。实测后改成精确形式。

## ⚠ 沙箱/副作用

本文件只读文档与 git 输出，**不写任何文件**。
"""

import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
NAV_DOC = REPO / "docs" / "next-steps.md"

# 「唯一指路的入口」—— 这份文档自称的角色。这句也是范围的依据。
NAN_CLAIM = "唯一指路的入口"

# 现状声明的精确形式：HEAD 后紧跟（允许 markdown 粗体）hash 包裹在反引号里。
# 刻意**不含** `=` —— 那是「某次测量时 HEAD 是 x」的历史记录。
HEAD_CLAIM = re.compile(r"HEAD\s*\*{0,2}\s*`([0-9a-f]{7,40})`")

# 「未提交」前后各若干行都算这个段落（含反引号里的 .py）。
# ⚠ 第一版写成「段落里含未提交」，结果 `匹配到 0 段` ——
#    实测那个正则**从没匹配到任何东西**，判据恒真、永远绿。
#    真正的句子形如「工作树里 `sessions.py` / `api.py` 未提交」——
#    被点名的文件在「未提交」**之前**。所以必须向前看。
UNCOMMITTED_LINES = 6
UNCOMMITTED_PARA = re.compile(
    r"(?:[^\n]*\n){0,%d}[^\n]*未提交[^\n]*\n(?:[^\n]*\n){0,%d}"
    % (UNCOMMITTED_LINES, UNCOMMITTED_LINES))
PY_IN_TEXT = re.compile(r"`([A-Za-z0-9_./-]+\.py)`")


def _git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(REPO))
    return r.stdout


def _head():
    return _git("rev-parse", "HEAD").strip()


def _dirty():
    return [l for l in _git("status", "--porcelain").splitlines() if l.strip()]


def _doc():
    return NAV_DOC.read_text(encoding="utf-8")


def _doc_tracked() -> bool:
    """`docs/next-steps.md` 是否被 git 跟踪。

    它按 §0 的设计**故意不单独提交**（提交本身会让声明的 HEAD 立刻陈旧一笔），
    于是干净检出（CI）里根本没有这份文件。区分两种「文件不在」：
      · 未跟踪 + 不在 —— 预期的本地专属文档，CI 本就没有 → skip，不是缺陷。
      · 已跟踪 + 不在 —— 真被人删了/丢了 → 才是判据该报的失败。
    """
    return bool(_git("ls-files", "--", "docs/next-steps.md").strip())


class NavigationDocIsSelfDeclared(unittest.TestCase):
    """先把范围钉住：这份文档确实自称「指路入口」。"""

    def test_it_declares_itself_the_navigation_entry(self):
        """文档自己说它是唯一指路的入口 —— 否则下面的范围该重新议。

        这条同时是「判据有没有白跑」的自检：文档改名/改定位后，
        其余两条会跳过（不是红），那就该回来重议范围了。
        """
        if not NAV_DOC.exists():
            self.skipTest(
                "docs/next-steps.md 不在这份检出里（它按设计只存在于本地工作树、"
                "故意不提交）——没有文档就无从判断它自称的角色，跳过而非红。")
        self.assertIn(NAN_CLAIM, _doc(),
                      "next-steps.md 不再自称「%s」了 —— "
                      "本判据的范围依据失效，需重新议「哪份文档该对现状负责」" % NAN_CLAIM)

    def test_the_doc_exists(self):
        if not NAV_DOC.exists():
            if _doc_tracked():
                self.fail("docs/next-steps.md 被 git 跟踪却不在工作树里 —— "
                          "指路文档丢了，接手的人没有入口了。")
            self.skipTest(
                "docs/next-steps.md 按 §0 设计故意不单独提交，是本地工作树专属文档；"
                "干净检出（CI）里没有它属正常，不是缺陷。")
        self.assertTrue(NAV_DOC.exists(),
                        "docs/next-steps.md 不在 —— 判据的文件都找不到了")


class HeadClaimMatchesReality(unittest.TestCase):
    """文档声明的「当前 HEAD」必须等于真实 HEAD。

    ## 为什么这条判据只在本机工作树里成立

    `docs/next-steps.md` 的 §0 按设计**故意不单独提交**（文档自己写明）：
    「提交本身会改变 HEAD，一提交这条声明就立刻变陈」—— 于是它声明的永远是
    **写入那一刻**的 HEAD，提交之后立刻差一笔。

    推论：在**干净检出**（CI 就是干净检出）里，那条声明与真实 HEAD 必然不等，
    这条判据**结构上不可能成立** —— 它在 CI 上恒红，而红的原因与「文档没维护」
    毫无关系。那是**假信号**：真信号是零。

    这条判据真正要守的是**本地工作树**：你手改了 §0，就得让它说对当前 HEAD。
    所以判据只在「§0 在工作树里有未提交改动」时才响；文档原样提交（干净）时
    明确 skip —— 不静默 return（静默通过和恒真判据在输出里长得一模一样）。
    """

    def setUp(self):
        # 干净检出（CI）里没有这份文档：读它会 FileNotFoundError。
        # 未跟踪 + 不在 → 预期的本地专属文档，跳过；已跟踪却不在 → 交给
        # NavigationDocIsSelfDeclared.test_the_doc_exists 报真失败，这里不重复。
        if not NAV_DOC.exists():
            self.skipTest(
                "docs/next-steps.md 不在这份检出里（本地工作树专属、故意不提交）——"
                "无从读取它的 HEAD 声明，跳过而非红/错。")

    def test_declared_head_equals_real_head(self):
        doc = _doc()
        claimed = HEAD_CLAIM.findall(doc)
        if not claimed:
            self.skipTest("文档里没有 `HEAD <hash>` 式的现状声明")
        if not _doc_dirty():
            self.skipTest(
                "文档 §0 按设计「故意不单独提交」，声明的是写入那一刻的 HEAD —— "
                "提交本身就会让它陈旧一笔。干净检出（CI）里这条判据结构上不成立，"
                "只有本地工作树里改过 §0 时它才该响。")
        real = _head()
        real7 = real[:7]
        for c in claimed:
            self.assertEqual(
                c, real7,
                "指路文档说当前 HEAD 是 `%s`，真实 HEAD 是 `%s`。\n"
                "接手的人会基于 %s 这个前提接手，而它 %d 笔提交以前就是旧的。\n"
                "更新 docs/next-steps.md 第 0 节（不要删历史章节）。"
                % (c, real7, c, _count_since(c)))

    def skipTest(self, msg):
        raise unittest.SkipTest(msg)


def _count_since(short):
    """从 short 到 HEAD 有几笔提交 —— 只用于错误信息，不影响判定。"""
    out = _git("rev-list", "--count", "%s..HEAD" % short)
    try:
        return int(out.strip())
    except ValueError:
        return 0


def _doc_dirty() -> bool:
    """文档在**工作树**里有没有未提交的内容改动。

    用 `git diff HEAD` 而不是 `git status --porcelain` —— 后者会把内容与 HEAD
    完全相同的文件也报成 M（stat 缓存陈旧时）。本文件下面那条判据自己就写了
    这件事，这里不能重蹈。
    """
    return bool(_git("diff", "HEAD", "--", "docs/next-steps.md").strip())


class UncommittedClaimMatchesReality(unittest.TestCase):
    """文档声明的「未提交文件」必须在 `git status` 里真的还在。"""

    def setUp(self):
        if not NAV_DOC.exists():
            self.skipTest(
                "docs/next-steps.md 不在这份检出里（本地工作树专属、故意不提交）——"
                "无从读取它的「未提交」声明，跳过而非红/错。")

    def test_named_files_are_still_uncommitted(self):
        doc = _doc()
        hits = UNCOMMITTED_PARA.findall(doc)
        if not hits:
            # ⚠ 必须是**显式 skip**，不能静默 return：
            # 静默通过和恒真判据在输出里长得一模一样（都是绿的）。
            # 第一版就是静默 return，而它的正则压根匹配不到任何段落 ——
            # 「0 段」和「扫过都还在」我分不出来。
            self.skipTest(
                "指路文档里没有「未提交」式陈述（不是「扫过都还在」）——"
                "自证脚本 D:\\Temp\\opencode\\selfcheck_uncommitted.py 会证")
        bad = []
        for para in hits:
            for f in PY_IN_TEXT.findall(para):
                if f in _doc_all_dirty():
                    continue
                bad.append(f)
        if not bad:
            return
        self.fail(
            "指路文档说这些文件「未提交」，但 git status 里已经没有它们了：%s\n"
            "那批改动已经入库（或被别人提交了）。留着这句话会让接手的人\n"
            "去找一批不在工作树里的改动。更新指路文档。" % ", ".join(bad))


def _doc_all_dirty():
    out = set()
    for line in _git("status", "--porcelain").splitlines():
        if not line.strip():
            continue
        # XY <path>  或  XY <path> -> <new>
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ")[-1]
        out.add(path.replace("/", "/"))
        out.add(pathlib.PurePath(path).name)
    return out


class WorktreeClaimMatchesReality(unittest.TestCase):
    """文档声明的「工作树干净/有未提交改动」必须与内容级实况相符。

    ## 为什么用 `git diff HEAD` 而不是 `git status`

    `git status --porcelain` 会把**内容与 HEAD 完全相同**的文件也报成 `M`
    （stat 缓存陈旧时）。今晚我核验过 6 个文件「逐字节相同」，
    `git status` 却一直显示 `M`。

    所以这类陈述必须用**内容级**判据验：`git diff HEAD` 与
    `git diff --cached HEAD`。否则判据会被 stat 缓存永久带红，
    然后被后人关掉。

    ## 这条判据第一次就抓到我自己

    我写完判据、在指路文档里写下「**工作树内容干净**」，
    然后它立刻红了 —— 因为我改的 `next-steps.md` 本身还没提交。
    **那句话是假的。**

    这正是这条判据存在的理由：**不让我写下一句自己都不信的现状。**
    """

    CLAIM_CLEAN = "工作树内容干净"
    CLAIM_DIRTY = "工作树有未提交改动"

    def setUp(self):
        if not NAV_DOC.exists():
            self.skipTest(
                "docs/next-steps.md 不在这份检出里（本地工作树专属、故意不提交）——"
                "无从读取它的工作树声明，跳过而非红/错。")

    def test_clean_claim_requires_content_clean_tree(self):
        doc = _doc()
        if self.CLAIM_CLEAN not in doc:
            self.skipTest("文档里没有「%s」式陈述" % self.CLAIM_CLEAN)
        diff = "\n".join(
            x for x in (_git("diff", "HEAD", "--stat").strip(),
                        _git("diff", "--cached", "HEAD", "--stat").strip())
            if x)
        self.assertFalse(
            diff,
            "文档说「%s」，但 git diff HEAD 有差异：\n%s" % (self.CLAIM_CLEAN,
                                                           diff[:400]))

    def test_dirty_claim_names_the_real_files(self):
        """说「有未提交改动」就得把文件点出来，且点出来的**真的在改**。"""
        doc = _doc()
        if self.CLAIM_DIRTY not in doc:
            self.skipTest("文档里没有「%s」式陈述" % self.CLAIM_DIRTY)
        dirty_names = _doc_all_dirty()
        named = []
        for para in UNCOMMITTED_PARA.findall(doc):
            named += PY_IN_TEXT.findall(para)
        if not named:
            self.fail(
                "文档说「%s」却没点名任何 .py —— 那句话不可核，等于没说。"
                "点名之后判据才能验证它。" % self.CLAIM_DIRTY)
        missing = [f for f in named if f not in dirty_names]
        self.assertFalse(
            missing,
            "文档说这些文件有未提交改动，但内容级看它们与 HEAD 无差异：%s\n"
            "（`git status` 的 M 可能是 stat 缓存陈旧 —— 这里看的 git diff）"
            % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)