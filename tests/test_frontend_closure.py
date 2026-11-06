"""A 档守门：`frontend_closure` 的判据（纯 stdlib + git，零外部依赖）。

守的是 2026-11-06 的事故：工作区里 `npm run build` 过得去，因为缺的文件还在磁盘上；
单独检出 HEAD 时 Rollup 直接 `Could not resolve ./MemoryOverview.vue`。
这里不跑 npm、不读 node_modules，只回答一个问题：
**提交里的前端相对依赖，闭包是否完整。**
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from agent_runtime import head_check as hchk


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "--no-pager", *args], cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _add(repo: Path, *rels: str) -> None:
    rep = _run(repo, "add", "--", *rels)
    assert rep.returncode == 0, rep.stderr


def _commit(repo: Path, message: str = "fixture") -> str:
    rep = _run(repo, "-c", "user.email=guard@example.invalid", "-c", "user.name=guard",
               "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
    assert rep.returncode == 0, rep.stderr
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    assert _run(root, "init", "-q").returncode == 0
    return root


def test_a_complete_closure_is_green_and_actually_scanned(repo: Path) -> None:
    _write(repo, "frontend/src/main.ts", "import './leaf'\n")
    _write(repo, "frontend/src/leaf.ts", "export const a = 1\n")
    _add(repo, "frontend/src/main.ts", "frontend/src/leaf.ts")
    _commit(repo)

    result = hchk.frontend_closure(repo)

    assert result["applicable"] is True
    assert result["scanned"] == 2
    assert result["edges"] >= 1
    assert result["missing"] == []
    assert result["ok"] is True


def test_a_referenced_file_outside_the_commit_is_named(repo: Path) -> None:
    # main.ts 已提交并 import ./leaf，但 leaf.ts 只在磁盘上、没进提交。
    _write(repo, "frontend/src/main.ts", "import './leaf'\n")
    _write(repo, "frontend/src/leaf.ts", "export const a = 1\n")
    _add(repo, "frontend/src/main.ts")
    _commit(repo)

    result = hchk.frontend_closure(repo)

    assert result["applicable"] is True
    assert result["ok"] is False
    assert result["scanned"] == 1
    assert result["missing"] == [{
        "importer": "frontend/src/main.ts", "line": 1, "spec": "./leaf",
        "target": "frontend/src/leaf.ts", "on_disk": True,
    }]


def test_worker_entry_written_as_new_url_is_followed(repo: Path) -> None:
    # worker 入口不是 import 语句；漏了这条规则，worker 文件会被整条闭包放过。
    _write(repo, "frontend/src/force.ts",
           "const w = new Worker(new URL('./force.worker.ts', import.meta.url), "
           "{ type: 'module' })\n")
    _write(repo, "frontend/src/force.worker.ts", "export {}\n")
    _add(repo, "frontend/src/force.ts")
    _commit(repo)

    result = hchk.frontend_closure(repo)

    assert result["ok"] is False
    assert [(row["importer"], row["line"], row["spec"]) for row in result["missing"]] == [
        ("frontend/src/force.ts", 1, "./force.worker.ts")]


def test_a_target_missing_everywhere_is_reported_as_such(repo: Path) -> None:
    _write(repo, "frontend/src/main.ts", "import './nowhere'\n")
    _add(repo, "frontend/src/main.ts")
    _commit(repo)

    result = hchk.frontend_closure(repo)

    assert result["ok"] is False
    assert result["missing"] == [{
        "importer": "frontend/src/main.ts", "line": 1, "spec": "./nowhere",
        "target": "", "on_disk": False,
    }]


def test_no_frontend_src_is_not_a_pass(repo: Path) -> None:
    # 空清单不能换绿：`all([])` 是 True，但"什么都没扫"不等于"闭包完整"。
    _write(repo, "README.md", "no frontend here\n")
    _add(repo, "README.md")
    _commit(repo)

    result = hchk.frontend_closure(repo)

    assert result["applicable"] is False
    assert result["ok"] is False
    assert result["scanned"] == 0
    assert result["error"]


def test_dirty_worktree_edits_do_not_leak_into_the_verdict(repo: Path) -> None:
    # 提交里是干净的；工作区把 leaf.ts 改成引用一个不存在的文件。
    # 判据必须只看提交内容，否则"脏工作区"会被误判成 HEAD 坏了。
    _write(repo, "frontend/src/main.ts", "import './leaf'\n")
    _write(repo, "frontend/src/leaf.ts", "export const a = 1\n")
    _add(repo, "frontend/src/main.ts", "frontend/src/leaf.ts")
    _commit(repo)
    _write(repo, "frontend/src/leaf.ts", "import './ghost'\nexport const a = 1\n")

    result = hchk.frontend_closure(repo)

    assert result["ok"] is True
    assert result["missing"] == []


def test_check_wires_the_closure_into_checks_and_render(
        repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    _write(repo, "frontend/src/main.ts", "import './leaf'\n")
    _write(repo, "frontend/src/leaf.ts", "export {};\n")
    _add(repo, "frontend/src/main.ts")
    _commit(repo)

    result = hchk.check(repo, modules=(), timeout=60)

    assert result["checks"]["frontend_closure_ok"] is False
    assert result["passed"] is False
    # 干净检出里那个文件既不在提交里、也不在磁盘上，报出来的是引用点和 spec。
    assert "frontend/src/main.ts:1 → ./leaf" in result["error"]
    text = hchk.render(result)
    assert "前端闭包" in text
    assert "./leaf" in text
    assert result["cleanup"]["ok"] is True
