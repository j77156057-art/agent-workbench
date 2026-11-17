"""sqlite lane 分片并行的接线守卫（2026-11-17 挂门）。

分片把 ~25 分钟的串行全量套件拆成 4 个并行 job。这类改动最容易出的事故不是
「跑不起来」，而是**悄悄漏跑**：某个文件没被任何一片认领、两片认领了同一个文件、
或者覆盖率门只看到一片的数据就盖章。所以这里钉四件事：

1. matrix 形状：sqlite 恰好 4 片（片号 1..4），postgres 不分片；
2. 两条测试 lane 用同一套确定性分配（体积降序轮转取模），本地重算一遍：
   全量文件恰好被认领一次（不重不漏），每片非空；
3. YAML 里的分配表达式与本地重算的算法逐字对得上（防算法漂移）；
4. 覆盖率数据带片号上传、门 job 收齐合并——门不许只看半边数据。

退出码口径：本文件是 unittest TestCase，CI 的 unittest lane 自己就会收到它。
"""
import ast
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CI_FILE = ROOT / ".github" / "workflows" / "harness.yml"


def _doc():
    return yaml.safe_load(CI_FILE.read_text(encoding="utf-8"))


def _steps():
    return [s for s in _doc()["jobs"]["test"]["steps"] if isinstance(s, dict)]


def _step(name):
    hits = [s for s in _steps() if s.get("name") == name]
    if len(hits) != 1:
        raise AssertionError("步骤 %r 应恰好出现一次，实际 %d" % (name, len(hits)))
    return hits[0]


def _partition(files, shard, total):
    """与 harness.yml 两条 lane 内嵌 Python 相同的分配算法。"""
    ordered = sorted(files, key=lambda p: (-p.stat().st_size, p.as_posix()))
    return [p for i, p in enumerate(ordered) if i % total == shard - 1]


class MatrixShapeTests(unittest.TestCase):
    def test_sqlite_is_four_shards_postgres_is_not_sharded(self):
        entries = _doc()["jobs"]["test"]["strategy"]["matrix"]["include"]
        sqlite = [e for e in entries if e["backend"] == "sqlite"]
        postgres = [e for e in entries if e["backend"] == "postgres"]
        self.assertEqual(sorted(e["shard"] for e in sqlite), [1, 2, 3, 4])
        for entry in sqlite:
            self.assertEqual(entry["shards"], 4)
        self.assertEqual(len(postgres), 1)
        self.assertEqual((postgres[0]["shard"], postgres[0]["shards"]), (1, 1))

    def test_fail_fast_stays_off(self):
        # 一片红不许把其它片的红名单掐掉——红名单聚合靠每片各自打完。
        self.assertIs(_doc()["jobs"]["test"]["strategy"]["fail-fast"], False)


class PartitionCoverageTests(unittest.TestCase):
    """本地重算分配：全量文件不重不漏，每片非空。"""

    def test_unittest_lane_covers_every_test_file_exactly_once(self):
        files = list(pathlib.Path(ROOT / "tests").glob("test*.py"))
        self.assertTrue(files)
        seen = []
        for shard in range(1, 5):
            mine = _partition(files, shard, 4)
            self.assertTrue(mine, "unittest 第 %d 片是空的" % shard)
            seen.extend(mine)
        self.assertEqual(sorted(p.as_posix() for p in seen),
                         sorted(p.as_posix() for p in files),
                         "分片不重不漏被破坏：有文件没人跑或跑了两遍")

    def test_pytest_lane_covers_every_pytest_style_file_exactly_once(self):
        paths = []
        for p in sorted(pathlib.Path(ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            if any(isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
                   for n in tree.body):
                paths.append(p)
        self.assertTrue(paths)
        seen = []
        for shard in range(1, 5):
            mine = _partition(paths, shard, 4)
            self.assertTrue(mine, "pytest 第 %d 片是空的" % shard)
            seen.extend(mine)
        self.assertEqual(sorted(p.as_posix() for p in seen),
                         sorted(p.as_posix() for p in paths))

    def test_yaml_partition_expression_matches_the_recomputed_algorithm(self):
        # 字符串钉住算法形状：改了取模/排序而本地重算没跟上时，这里先红。
        for name in ("Run pytest-style lane", "Run full test suite"):
            run = str(_step(name).get("run") or "")
            self.assertIn("i % total == shard - 1", run,
                          "%s 的分配表达式变了" % name)
            self.assertIn("-p.stat().st_size", run,
                          "%s 不再按体积降序轮转" % name)
            self.assertIn("CI_SHARD", run)
            self.assertIn("CI_SHARDS", run)

    def test_lane_steps_receive_the_matrix_shard_env(self):
        for name in ("Run pytest-style lane", "Run full test suite"):
            env = _step(name).get("env") or {}
            self.assertEqual(str(env.get("CI_SHARD")), "${{ matrix.shard }}")
            self.assertEqual(str(env.get("CI_SHARDS")), "${{ matrix.shards }}")


class CoverageAggregationTests(unittest.TestCase):
    def test_upload_is_per_shard_and_sqlite_only(self):
        up = [s for s in _steps() if "upload-artifact" in str(s.get("uses") or "")]
        self.assertEqual(len(up), 1)
        step = up[0]
        self.assertIn("sqlite", str(step.get("if") or ""))
        self.assertIn("always()", str(step.get("if") or ""),
                      "红片的数据也要上传，失败现场同样要能诊断")
        with_ = step.get("with") or {}
        self.assertEqual(with_.get("name"), "ci-cov-shard-${{ matrix.shard }}")
        self.assertEqual(with_.get("if-no-files-found"), "error",
                         "没数据不许静默传个空 artifact")

    def test_staged_files_carry_the_shard_suffix(self):
        # 同名文件在 merge-multiple 下载时会互相覆盖——片号后缀是防覆盖的关键。
        stage = [s for s in _steps() if "ci-cov-upload" in str(s.get("run") or "")
                 and "upload-artifact" not in str(s.get("uses") or "")]
        self.assertTrue(stage, "找不到带片号落盘覆盖率数据的步骤")
        self.assertIn(".shard-${{ matrix.shard }}", str(stage[0].get("run")))

    def test_gate_job_downloads_all_shards_and_combines(self):
        gate = _doc()["jobs"]["coverage-gate"]
        self.assertIn("test", gate.get("needs") or [])
        dl = [s for s in gate["steps"] if "download-artifact" in str(s.get("uses") or "")]
        self.assertEqual(len(dl), 1)
        with_ = dl[0].get("with") or {}
        self.assertEqual(with_.get("pattern"), "ci-cov-shard-*")
        self.assertIs(with_.get("merge-multiple"), True)
        gate_run = "\n".join(str(s.get("run") or "") for s in gate["steps"])
        self.assertIn("coverage combine", gate_run)
        self.assertIn("coverage_eval", gate_run)

    def test_artifact_actions_are_pinned_to_node24_tags(self):
        # 实测 action.yml：upload-artifact v7.0.2 / download-artifact v8.0.2
        # 均声明 using: node24（2026-11-17 经 api.github.com 逐 tag 核对）。
        doc = _doc()
        uses = [str(s.get("uses") or "") for s in _steps()]
        uses.extend(str(s.get("uses") or "")
                    for s in doc["jobs"]["coverage-gate"]["steps"])
        self.assertIn("actions/upload-artifact@v7.0.2", uses)
        self.assertIn("actions/download-artifact@v8.0.2", uses)


if __name__ == "__main__":
    unittest.main()