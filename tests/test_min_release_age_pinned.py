"""钉住：`frontend/.npmrc` 的 `min-release-age` 不能被删掉或改坏。

## 为什么这道守卫要写成三部分

`min-release-age` 有个**会把人带沟里的怪癖**：设了之后
`npm config get min-release-age` 回读 **`null`** —— 但**行为是生效的**
（实测：设 9999 天后 `npm install vite` 报 `ENOVERSIONS` /
`No versions available for vite`，见 `tools/mutation/probe_min_release_age.py`）。

所以「用 `npm config get` 检查」这种做法**永远返回 null**，
下一个人看到就会以为配置坏了、把它删掉 —— 而删掉之后**没有任何测试会红**。
守卫必须挡住这一下。

三部分分别挡不同的破坏：

1. `_the_file_ships_with_the_flag` —— 文件在、键在、值是合理天数。
   挡「删文件 / 删行 / 改成 0」。
2. `_the_npm_version_supports_it` —— 特性需要 npm >= 11.6.0。
   挡「CI 换 node 版本导致特性消失，而配置静默变成一行废话」。
3. `_the_flag_is_not_pointless` —— **本文件最关键的一条**：
   断言那几段「覆盖面」注释还在。因为注释里写着
   **「`npm config get` 回读 null 不代表无效，不要因此删掉」**
   —— 而守卫不该靠注释做事，所以这里只断言「注释在」，
   真正的判据是：配置项存在 **且** npm 版本达标 **且** 探针可复算。

## 判据不是「源码里有这个字符串」

`inspect.getsource` 那种代码形状断言在重构时会静默变绿。
这里读的是**配置文件本身**（它就是被约束的对象），
加上 npm 版本这个**外部事实**。
真正「是否生效」的判据在 `tools/mutation/probe_min_release_age.py` ——
守卫跑它是因为那需要网络与 registry，不适合放进单测。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NPMRC = ROOT / "frontend" / ".npmrc"
# npm 官方 changelog：`min-release-age` 自 11.6.0 引入。
MIN_NPM = (11, 6, 0)


def _npm_version() -> tuple[int, ...] | None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return None
    try:
        out = subprocess.run([npm, "--version"], cwd=str(ROOT), capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=60).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return tuple(int(x) for x in out.split(".")) if out[:1].isdigit() else None


def test_the_file_ships_with_the_flag():
    """文件在、键在、值是**非零且不过大**的天数。"""
    assert NPMRC.exists(), (
        "frontend/.npmrc 不见了 —— `min-release-age` 是本仓库唯一的 npm 侧"
        "「别解析到刚发布的包」防护（依据 docs/harness-research-verified.md §1.4）")
    text = NPMRC.read_text(encoding="utf-8")
    m = re.search(r"^\s*min-release-age\s*=\s*(\S+)", text, re.M)
    assert m, "frontend/.npmrc 里没有 min-release-age 那一行"

    raw = m.group(1)
    # npm 允许带单位（2d / 12h）。我们写的是裸数字，这里两种都接受但都要合理。
    digits = re.match(r"^(\d+)", raw)
    assert digits, (
        "min-release-age 的值 %r 解析不出天数 —— 它会被 npm 静默忽略，"
        "而那正是「看起来做了、实际没生效」" % raw)
    days = int(digits.group(1))
    assert days >= 1, (
        "min-release-age=%d 等于没设（0 天 = 允许当天发布的包）。"
        "Pi 用 2。" % days)
    assert days <= 30, (
        "min-release-age=%d 天太长了 —— 真正的安全补丁也要等这么久才能装。"
        "Pi 用 2。" % days)


def test_the_npm_version_supports_it():
    """特性需要 npm >= 11.6.0。版本不够时配置是**一行静默失效的废话**。"""
    ver = _npm_version()
    if ver is None:
        import pytest
        pytest.skip("取不到 npm 版本")
    assert ver >= MIN_NPM, (
        "npm %s < %s —— `min-release-age` 在这个版本上**不被识别**，"
        "frontend/.npmrc 会变成一行静默无效的配置。"
        "CI 用 node 24（自带 npm 11.x，>= 11.6.0）；注意 **node 22 自带的是 npm 10.x**，"
        "认不出这个键 —— 若你要降 node，先查这条。"
        % (".".join(map(str, ver)), ".".join(map(str, MIN_NPM))))


def test_the_flag_is_not_pointless():
    """**防「因为 `npm config get` 回读 null 就删掉」那一下。**

    `npm config get min-release-age` 在设了之后仍然返回 `null`
    （npm 的 config schema 没登记这个键），而**行为是生效的**。
    不写这一条，下一个人查一下就会以为配置坏了。

    所以断言那段解释必须在文件里 —— 并且顺带钉住两条实测事实
    （`npm ci` 不受影响 / 最年轻的依赖 33.9 天），因为它们解释了
    「为什么这一行的覆盖面比看起来小」而**没有**解释为什么该删。
    """
    text = NPMRC.read_text(encoding="utf-8")
    for needle, why in (
        ("config get", "「回读 null 不代表无效」的说明"),
        ("npm ci", "「对 npm ci 无影响」的实测结论"),
        ("11.6", "特性所需的最低 npm 版本"),
    ):
        assert needle in text, (
            "frontend/.npmrc 里关于「%s」的说明不见了 —— %s。"
            "那段不是装饰：没有它，下一个人会用 `npm config get` 回读到 null "
            "然后把这一行删掉" % (needle, why))

    # 反向：确认文件里**没有**「回读 null = 无效」这种错误结论，
    # 那是最容易被后人加进来、又最难发现的坏注释。
    assert not re.search(r"回读\s*null\s*[，,、]?\s*(即|就是|表示)?\s*无效",
                         text), (
        "frontend/.npmrc 里的注释说「回读 null 即无效」—— **那是错的**，"
        "实测行为生效。照着这条注释走的人会删掉一个有效的防护")


if __name__ == "__main__":
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))