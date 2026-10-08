"""钉住：`requirements.txt` 里的 `proxy_tools==0.1.0`。

## 为什么要专门为一行写守卫

因为这一行**不是普通依赖声明**，它是**堵一个已确认的投毒面**：

* `proxy_tools` 不是我们的直接依赖，是 `pywebview==6.2.1` 的 `requires_dist`
  拉进来的，而**那条声明没有版本上下界**
* 它在 PyPI 上**六年只发过 0.1.0 一个版本**（2014-05-05，sdist only）
* 在本笔改动之前，**我们没有任何版本约束保护它**

所以下一个人看到「这是个传递依赖 / 没人 import 它 / 版本号好老」就删掉它，
是完全合理的动作 —— 而那会把洞重新打开。**这一行必须有守卫。**

## 三条守卫挡三种不同的破坏

1. `_pinned_exactly` —— 那行在、且是 `==0.1.0`
2. `_no_hash_on_that_line` —— **不能顺手加 `--hash`**。这不是洁癖：
   实测（`tools/mutation/probe_pin_one_hash.py`）给这一行加 hash 会让 pip
   进入 `--require-hashes` 模式，而那个模式要求**本文件所有行**都用 `==`
   钉死。报错原文：
   `ERROR: In --require-hashes mode, all requirements must have their versions pinned with ==.`
   也就是**加一行 hash = 变成全量 hash 钉死（42 行 -> 172 行）**。
   那个决定不该被这一行顺带拖进来。
3. `_the_whole_file_has_no_hash` —— 整个文件里**不能出现 `--hash`**。
   只要出现一个，整个文件就被拖进 hash 模式 —— 同上，那是个契约变更。

## 判据为什么不是「文件里有这个字符串」

守卫 1 与 2 读的是 `requirements.txt` 本身（它就是被约束的对象），
但**光断言「在」不够** —— 那证明不了「有效」。
真正的有效性判据是 `probe_pin_one_hash.py` 那个实测：
它跑 `pip install --dry-run` 并验证「加 hash 会牵连全文件」这个后果。
守卫里断言的「为什么不能加 hash」那段注释必须在，
因为那是下一个人唯一会看到的解释。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQ = ROOT / "requirements.txt"

PINNED = "proxy_tools==0.1.0"


def effective_lines() -> list[str]:
    return [ln.strip() for ln in REQ.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_pinned_exactly():
    """那一行在，且是 `==0.1.0`。"""
    lines = effective_lines()
    assert PINNED in lines, (
        "requirements.txt 里没有 `%s` —— 它是堵 `pywebview` 那个"
        "**无版本约束**的传递依赖用的。删掉它等于把洞重新打开。"
        "依据 docs/harness-research-verified.md §1.4" % PINNED)

    # 也不能写成裸 `proxy_tools`（那样约束就没了）—— 那正是改动前的状态。
    bare = [ln for ln in lines
            if re.match(r"^proxy_tools\s*($|#)", ln)]
    assert not bare, (
        "requirements.txt 里有裸的 `proxy_tools`（%r）—— 版本约束没了，"
        "而 `pywebview` 声明它时没有上下界，所以每次 --upgrade 都会重新解析。"
        % bare)


def test_no_hash_on_that_line():
    """**不能顺手加 `--hash`** —— 实测那会拖整个文件进 hash 模式。"""
    for ln in effective_lines():
        if ln.startswith("proxy_tools"):
            assert "--hash" not in ln, (
                "给 proxy_tools 那行加了 --hash：实测会让 pip 进入 "
                "`--require-hashes` 模式，而那个模式要求**本文件所有行**都用 "
                "`==` 钉死（报错原文：In --require-hashes mode, all requirements "
                "must have their versions pinned with ==）。"
                "也就是一行 hash = 全量 hash 钉死（42 行 -> 172 行）——"
                "那件事该单独决定，不该被这一行顺带拖进来。")


def test_the_whole_file_has_no_hash():
    """整个文件里不能出现 `--hash`。

    只要出现一个，pip 就进 hash 模式，同上。
    要全量 hash 化时，把这条守卫改成「列出全部 172 个 hash」——
    **但那一次要连带把 C 路的验证重跑一遍**。
    """
    hashed = [ln for ln in effective_lines() if "--hash" in ln]
    assert not hashed, (
        "requirements.txt 里出现了 `--hash`（%d 行）—— 整个文件会被拖进 "
        "`--require-hashes` 模式，本文件其余 %d 行全部会解析失败。"
        "要么撤掉这些 hash，要么做完全量 hash 化并改写这条守卫。"
        % (len(hashed), len(effective_lines()) - len(hashed)))

    # 那段「为什么不加 hash」的解释必须在 —— 那是下一个人唯一会看到的理由。
    text = REQ.read_text(encoding="utf-8")
    assert "require-hashes" in text, (
        "requirements.txt 里关于「加 hash 会牵连全文件」的解释不见了 —— "
        "下一个人看到「为什么这个包不钉 hash」会以为是漏了")


if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))