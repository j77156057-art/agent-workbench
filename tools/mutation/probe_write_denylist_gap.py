"""探针：`_resolve_in_root` 对「凭据类路径」的态度。

Hermes 的 `HERMES_WRITE_SAFE_ROOT` 有一层**与白名单无关的地板**：
把 safe-root 指到 `$HOME` 也不允许写 `~/.ssh/id_rsa`。

我们要回答的是同一件事：**我们的 `code_root` 约束之上，有没有这样一层？**

具体测三件事：
1. `code_root` = 项目目录时，凭据路径能不能通过（应该不能）
2. `code_root` 被设成 `$HOME` 或其上级时，**能不能**写到 `~/.ssh/`（这是敞口）
3. 语义上有没有任何「与 root 无关」的禁止项

注意：这个探针**只读**，不写任何文件 —— 它问的是 `_resolve_in_root` 返回什么，
不是真的落盘。
"""
from __future__ import annotations

import io
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> None:
    import tools as T

    saved = {}
    for key in ("code_root",):
        try:
            from config import get_runtime, set_runtime
            saved[key] = get_runtime(key, "")
        except Exception:
            saved[key] = None
            set_runtime = None

    def with_root(value: str, fn):
        if set_runtime is not None:
            set_runtime("code_root", value)
        return fn()

    project = str(ROOT)
    home = os.path.expanduser("~")

    print("  当前 code_root : %s" % (saved.get("code_root") or "(未设置)"))
    print("  项目目录       : %s" % project)
    print("  用户 HOME      : %s" % home)
    print()

    cases = [
        ("项目内写普通文件", os.path.join(project, "a.py"), project),
        ("项目内写 .env", os.path.join(project, ".env"), project),
        ("项目内写 .ssh/id_rsa", os.path.join(project, ".ssh", "id_rsa"), project),
        ("HOME 内写 .ssh/id_rsa", os.path.join(home, ".ssh", "id_rsa"), home),
        ("HOME 为 root 时越界写 .aws/credentials",
         os.path.join(home, ".aws", "credentials"), home),
        ("上级目录写 .netrc", os.path.join(os.path.dirname(home), ".netrc"),
         os.path.dirname(home)),
    ]
    print("  %-42s %-8s %s" % ("用例", "code_root", "结果"))
    print("  " + "-" * 96)
    for label, target, root_value in cases:
        try:
            out = with_root(root_value, lambda: T._resolve_in_root(target))
        except Exception as exc:
            print("  %-42s %-8s 抛异常 %s" % (label, "?", str(exc)[:30]))
            continue
        resolved = out[0] if isinstance(out, tuple) else out
        verdict = "放行" if resolved else "拒绝"
        print("  %-42s %-8s %s%s"
              % (label, os.path.basename(root_value)[:8], verdict,
                 "" if resolved else "  <- 越界被拦" if root_value != project else ""))

    print()
    print("  === 结论判据 ===")
    ok_home, _, _ = with_root(home, lambda: T._resolve_in_root(
        os.path.join(home, ".ssh", "id_rsa")))
    ok_project_ssh, _, _ = with_root(project, lambda: T._resolve_in_root(
        os.path.join(project, ".ssh", "id_rsa")))
    print("  code_root=HOME 时能写 ~/.ssh/id_rsa : %s" % ("能" if ok_home else "不能"))
    print("  项目内能写 .ssh/id_rsa              : %s" % ("能" if ok_project_ssh else "不能"))
    print()
    if ok_home:
        print("  => **有敞口**：只要 code_root 被设成 HOME 或其上级，")
        print("     凭据路径就与普通项目文件无差别 —— 没有与 root 无关的地板。")
        print("     Hermes 在这一点上更强：safe-root 指到 $HOME 也不允许写 ~/.ssh/id_rsa。")
    else:
        print("  => 没有敞口。")


if __name__ == "__main__":
    main()