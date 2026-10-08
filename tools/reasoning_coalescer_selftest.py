"""自检：`_ReasoningCoalescer` —— 合并切点，但一个字都不能少。

## 为什么这个自检的断言是「内容守恒」

合并器的职责**只有**改切点。若它顺手做了去重、裁剪、strip 或丢缓冲区，
那就是**静默丢模型输出** —— 而 reasoning 是用户唯一能看到「模型在想什么」的内容，
丢它比不合并严重得多。

所以核心断言是：**把所有 chunk 拼起来 == 把所有输入拼起来，逐字节相等。**
这条断言直接对着机制本身，而不是对着「chunk 数变少了」这种表面读数。

## 为什么要有变异反向守卫

「合并后事件变少」这条断言是**很容易恒真**的 —— 只要实现里多丢点数据，
事件数当然会变少。所以必须**故意把实现改坏**，确认守恒断言会红。
否则自检只是一份让自己安心的摆设（本项目已栽 9 次同类错）。

## 不调真模型

构造数据 + 直接实例化类，1 秒跑完。
"""

from __future__ import annotations

import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo_root import ensure_on_path  # noqa: E402

ensure_on_path()


def real_shape(total_chars: int = 129270, avg: float = 2.30) -> list[str]:
    """复刻实测那次跑的粒度：56210 个事件 / 129270 字符 / 平均 2.30 字符。"""
    n = int(total_chars / avg)
    # 让总字符数精确等于 total_chars，最后一片吃掉余数
    out, used = [], 0
    for i in range(n - 1):
        piece = "思" if i % 3 == 0 else ("考" if i % 3 == 1 else "。")
        out.append(piece)
        used += len(piece)
    out.append("x" * (total_chars - used))
    return out


def main() -> int:
    print("=" * 72)
    print("  自检：合并切点、内容守恒")
    print("=" * 72)

    import agent as agent_mod

    Coalescer = agent_mod._ReasoningCoalescer
    print("  阈值: min=%d max=%d"
          % (Coalescer(min_chars=200)._min, Coalescer(min_chars=200)._max))

    ok = True

    # ---- [1] 内容守恒（核心断言）----
    print()
    print("  [1] 内容守恒：拼起来的 chunk == 拼起来的输入")
    src = real_shape()
    c = Coalescer()
    pending, chunks = [], []
    for i, piece in enumerate(src):
        pending.append(piece)
        chunks.extend(c.take(pending, final=(i == len(src) - 1)))
    joined_in, joined_out = "".join(src), "".join(chunks)
    same = joined_in == joined_out
    print("      输入 %d 片段 / %d 字符" % (len(src), len(joined_in)))
    print("      输出 %d 块 / %d 字符" % (len(chunks), len(joined_out)))
    print("      降幅 %.0f 倍（%d -> %d）"
          % (len(src) / max(1, len(chunks)), len(src), len(chunks)))
    print("      逐字节相等: %s" % same)
    if not same:
        ok = False
        print("      *** 内容被改动或丢失 —— 合并器只准改切点 ***")
    if len(chunks) >= len(src):
        ok = False
        print("      *** 事件数没降 —— 合并器没生效 ***")

    # ---- [2] pending 原地清空（reasoning_sink 契约）----
    print()
    print("  [2] pending 必须被原地清空 —— 它是 chat_kwargs['reasoning_sink']")
    c2 = Coalescer()
    p2 = ["a", "b", "c"]
    c2.take(p2)
    print("      take() 后 pending 长度 = %d" % len(p2))
    if len(p2) != 0:
        ok = False
        print("      *** 没清空 —— 上游会重复往里 append，同一段会被发多次 ***")

    # ---- [3] 不得在 min_chars 之前切 ----
    print()
    print("  [3] 不得早于 min_chars=200 切（否则又退回逐 token）")
    c3 = Coalescer(min_chars=200, max_chars=1200)
    short = ["a" * 50]
    out3 = c3.take(short)
    print("      喂 50 字符 -> 切出 %d 块" % len(out3))
    if out3:
        ok = False
        print("      *** 提前切了 ***")

    # ---- [4] final=True 必须吐出剩余 ----
    print()
    print("  [4] 轮末 final=True 必须把残余全部吐出（否则丢掉最后的思考）")
    c4 = Coalescer(min_chars=200)
    p4 = ["尾巴" * 30]          # 60 字符，达不到 min
    pre = c4.take(p4)
    post = c4.take(p4, final=True)
    print("      final 前切出 %d 块，final 后切出 %d 块，合计字符 %d"
          % (len(pre), len(post), sum(len(x) for x in pre + post)))
    if len(pre) + len(post) != 1 or (pre + post)[0] != "尾巴" * 30:
        ok = False
        print("      *** 残余丢失或被重复发出 ***")

    # ---- [5] 变异反向守卫：故意让实现丢数据，守恒断言必须变红 ----
    print()
    print("  [5] 变异反向守卫：把实现改坏（切点前多截 5 字），守恒必须失败")

    class Broken(Coalescer):
        def take(self, pending, final=False):          # noqa: ANN001
            for item in pending:
                self._buf += str(item)
            pending.clear()
            out = []
            while True:
                if final:
                    if self._buf:
                        out.append(self._buf)
                        self._buf = ""
                    break
                cut = self._cut_at(self._buf)
                if cut <= 0:
                    break
                out.append(self._buf[:cut - 5 if cut > 5 else 0])   # 故意丢 5 字
                self._buf = self._buf[cut:]
            return out

    b = Broken()
    p5, out5 = [], []
    for i, piece in enumerate(src[:400]):
        p5.append(piece)
        out5.extend(b.take(p5, final=(i == 399)))
    broken_same = ("".join(src[:400]) == "".join(out5))
    print("      变异体内容逐字节相等: %s（期望 False）" % broken_same)
    if broken_same:
        ok = False
        print("      *** 守恒断言恒真 —— 它抓不住丢数据 ***")

    print()
    print("=" * 72)
    print("  结论：%s" % ("五项全过" if ok else "*** 有失败 ***"))
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
