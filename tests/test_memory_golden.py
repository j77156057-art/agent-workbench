# -*- coding: utf-8 -*-
"""把 `tests/golden/memory/` 的 24 条离线用例接进 pytest / CI。

执行与比对**完全复用** `runner.run_case`：同一套子进程隔离、同一份断言、
同一份快照比对。这里只负责「一条用例 = 一个 pytest case」以及两道防漂移闸。

- 离线用例默认全跑（不联网、StubLLM、独立临时目录）。
- `cases/live/live_smoke.json` 默认 skip；只有 `--live` 或
  `DOCMIND_GOLDEN_LIVE=1` 才调用真实模型，不进 CI。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNNER_PATH = HERE / "golden" / "memory" / "runner.py"

#: 离线用例数由人显式钉住：新增/删除用例必须同时改这个数，让变化在评审里可见。
EXPECTED_OFFLINE_CASES = 24


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "docmind_memory_golden_runner", str(RUNNER_PATH))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


runner = _load_runner()

ALL_CASES = runner._case_files()
OFFLINE_CASES = [path for path in ALL_CASES
                 if not runner._is_live(path, runner._load_case(path))]
LIVE_CASES = [path for path in ALL_CASES
              if runner._is_live(path, runner._load_case(path))]


def _failure_text(row):
    detail = runner._collect_failure(row) or row.get("diff") or ""
    return "status=%s\n%s" % (row.get("status"), detail)


@pytest.mark.parametrize("case_path", OFFLINE_CASES,
                         ids=[path.stem for path in OFFLINE_CASES])
def test_offline_case_matches_its_snapshot(case_path):
    """一条离线用例一个测试：用例名就是 case id，失败可直接定位。"""
    row, _ = runner.run_case(case_path, timeout=runner.PER_CASE_TIMEOUT_SECONDS)
    assert row["status"] == runner._PASS, (
        "记忆黄金用例 %s 未通过：%s" % (case_path.stem, _failure_text(row)))


def test_offline_case_count_is_pinned():
    """数量闸：离线用例固定 24 条，变化即失败。"""
    assert len(OFFLINE_CASES) == EXPECTED_OFFLINE_CASES, (
        "离线黄金用例数应为 %d，实际 %d；新增/删除用例必须同时改 "
        "tests/test_memory_golden.py 的 EXPECTED_OFFLINE_CASES，让数量变化可见。"
        "现有：%s" % (EXPECTED_OFFLINE_CASES, len(OFFLINE_CASES),
                   sorted(path.stem for path in OFFLINE_CASES)))


def test_cases_and_snapshots_are_one_to_one():
    """孤儿闸：`cases/*.json` 与 `expected/*.json` 必须一一对应。"""
    case_stems = {path.stem for path in OFFLINE_CASES}
    snapshot_stems = {path.stem for path in runner.EXPECTED_DIR.glob("*.json")}
    missing = sorted(case_stems - snapshot_stems)
    orphan = sorted(snapshot_stems - case_stems)
    assert not missing and not orphan, (
        "用例与 expected/ 快照必须一一对应：缺快照=%s；多余快照=%s"
        % (missing, orphan))


def _live_enabled(config):
    return bool(config.getoption("--live")) or os.getenv("DOCMIND_GOLDEN_LIVE") == "1"


@pytest.mark.parametrize("case_path", LIVE_CASES,
                         ids=[path.stem for path in LIVE_CASES])
def test_live_case_only_when_asked(case_path, request):
    """live 组默认 skip；显式 `--live` 或环境变量才跑。"""
    if not _live_enabled(request.config):
        pytest.skip("live 组默认跳过：加 --live 或设 DOCMIND_GOLDEN_LIVE=1 才跑")
    row, _ = runner.run_case(case_path, run_live=True, timeout=120)
    assert row["status"] == runner._PASS, (
        "记忆黄金 live 用例 %s 未通过：%s" % (case_path.stem, _failure_text(row)))
