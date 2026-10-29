"""Unit tests that do not require a running cluster (schema, parsing, artifacts)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from raftlab.cluster import ClusterInfo, NodeInfo, parse_test_conf
from raftlab.evaluator import (
    EvalError,
    aggregate_results,
    build_verdict,
    checks_from_assertions,
    compare,
    metrics_from_run_dir,
    parse_duration,
    write_verdict,
)
from raftlab.events import EventLog
from raftlab.orchestrator import Runner
from raftlab.report import build_report
from raftlab.schema import load_and_validate_path

SCENARIO = Path(__file__).resolve().parent.parent / "scenarios" / "baseline-kill-leader.yaml"
VALID_RUN = Path(__file__).resolve().parent / "fixtures" / "valid-run"


class ParseTestConfTest(unittest.TestCase):
    def test_out_of_order_and_extra_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "test.conf"
            conf.write_text(
                "node1ip=127.0.1.1\nnode1port=29017\n"
                "# comment\n"
                "node0ip=127.0.1.1\nnode0port=29016\n",
                encoding="utf-8",
            )
            self.assertEqual(
                parse_test_conf(conf),
                [(0, "127.0.1.1", 29016), (1, "127.0.1.1", 29017)],
            )

    def test_incomplete_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "test.conf"
            conf.write_text("node0ip=127.0.1.1\nnode0port=29016\nnode1ip=127.0.1.1\n", encoding="utf-8")
            self.assertEqual(parse_test_conf(conf), [(0, "127.0.1.1", 29016)])


class ArtifactContractTest(unittest.TestCase):
    def test_cluster_json_matches_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = ClusterInfo(
                config_file="test.conf",
                started_at="2026-10-27T01:10:01.000Z",
                nodes=[
                    NodeInfo(index=0, pid=4101, ip="127.0.1.1", port=29016),
                    NodeInfo(index=1, pid=4102, ip="127.0.1.1", port=29017),
                ],
                ready_at="2026-10-27T01:10:12.400Z",
            )
            path = Path(tmp) / "cluster.json"
            path.write_text(json.dumps(info.to_dict()), encoding="utf-8")

            result = load_and_validate_path(path)
            self.assertTrue(result.ok, [str(issue) for issue in result.issues])

    def test_events_json_matches_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = EventLog(run_id="20261027-0110-t2", path=Path(tmp) / "events.json")
            log.add("chaos", "fault.inject", "node1", fault="kill", pid=4102)
            log.add("cluster", "node.down", "node1")
            path = log.save()

            result = load_and_validate_path(path)
            self.assertTrue(result.ok, [str(issue) for issue in result.issues])

    def test_missing_artifact_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = load_and_validate_path(Path(tmp))
            self.assertFalse(result.ok)
            self.assertTrue(any("missing" in issue.message for issue in result.issues))


class DurationTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(parse_duration("100ms"), 100.0)
        self.assertEqual(parse_duration("2s"), 2000.0)
        self.assertEqual(parse_duration("1m"), 60000.0)
        self.assertEqual(parse_duration(42), 42.0)

    def test_invalid(self) -> None:
        with self.assertRaises(EvalError):
            parse_duration("10 seconds")


class EvaluatorTest(unittest.TestCase):
    def test_compare(self) -> None:
        self.assertTrue(compare(1, "<=", 2))
        self.assertFalse(compare(3, "<", 3))
        self.assertTrue(compare(3, ">=", 3))

    def test_checks_from_assertions(self) -> None:
        checks = checks_from_assertions(
            [{"metric": "latency.p99", "op": "<=", "value": "100ms"}],
            {"latency.p99": 132.4},
        )
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["passed"])
        self.assertEqual(checks[0]["threshold"], 100.0)

    def test_unknown_metric(self) -> None:
        with self.assertRaises(EvalError):
            checks_from_assertions([{"metric": "nope", "op": "<=", "value": 1}], {})

    def test_aggregate(self) -> None:
        def result(ok: int, error: int, p99: float) -> dict:
            bucket = {"p50": 1.0, "p90": 2.0, "p95": 3.0, "p99": p99, "p999": 40.0, "max": 100.0}
            return {
                "durationMs": 10000,
                "warmupMs": 0,
                "counts": {"total": ok + error, "ok": ok, "error": error, "byOp": {}},
                "latencyMs": {"all": bucket},
            }

        metrics = aggregate_results([result(90, 10, 12.0), result(100, 0, 8.0)])
        self.assertEqual(metrics["latency.p99"], 12.0)
        self.assertAlmostEqual(metrics["errors.rate"], 0.05)
        self.assertAlmostEqual(metrics["throughput.total"], 9.5)

    def test_verdict_contract(self) -> None:
        checks = checks_from_assertions([{"metric": "errors.rate", "op": "<=", "value": 0.02}], {"errors.rate": 0.01})
        verdict = build_verdict("20261027-1010-demo", checks)
        self.assertEqual(verdict["status"], "pass")

        with tempfile.TemporaryDirectory() as tmp:
            write_verdict(tmp, verdict)
            result = load_and_validate_path(Path(tmp) / "verdict.json")
            self.assertTrue(result.ok, [str(issue) for issue in result.issues])


class RunnerTest(unittest.TestCase):
    def test_loads_scenario_and_overrides_seed(self) -> None:
        runner = Runner(SCENARIO, seed=7)
        self.assertEqual(runner.name, "baseline-kill-leader")
        self.assertEqual(runner.seed, 7)
        self.assertRegex(runner.run_id, r"^\d{8}-\d{4}-baseline-kill-leader$")

    def test_manifest_matches_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(SCENARIO, run_dir=tmp)
            runner._write_manifest()
            result = load_and_validate_path(Path(tmp) / "manifest.json")
            self.assertTrue(result.ok, [str(issue) for issue in result.issues])


class ClusterModeTest(unittest.TestCase):
    def test_mode_is_written_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            info = ClusterInfo(
                config_file="test.conf",
                started_at="2026-10-27T01:10:01.000Z",
                nodes=[
                    NodeInfo(index=0, pid=4101, ip="127.0.1.1", port=29016, log_file="logs/node0.log"),
                    NodeInfo(index=1, pid=4102, ip="127.0.1.1", port=29017, log_file="logs/node1.log"),
                ],
                mode="per-node",
            )
            path = Path(tmp) / "cluster.json"
            path.write_text(json.dumps(info.to_dict()), encoding="utf-8")

            result = load_and_validate_path(path)
            self.assertTrue(result.ok, [str(issue) for issue in result.issues])
            self.assertEqual(json.loads(path.read_text())["mode"], "per-node")

    def test_unknown_mode_rejected(self) -> None:
        from raftlab.cluster import Cluster, ClusterError

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ClusterError):
                Cluster(3, tmp, mode="nope")

    def test_restart_requires_per_node(self) -> None:
        from raftlab.cluster import Cluster, ClusterError

        with tempfile.TemporaryDirectory() as tmp:
            cluster = Cluster(3, tmp, mode="fork")
            cluster.info = ClusterInfo(
                config_file="test.conf",
                started_at="2026-10-27T01:10:01.000Z",
                nodes=[NodeInfo(index=0, pid=1, ip="127.0.0.1", port=1)],
            )
            with self.assertRaises(ClusterError):
                cluster.restart(0)


class ReportTest(unittest.TestCase):
    def test_metrics_from_run_dir(self) -> None:
        metrics = metrics_from_run_dir(VALID_RUN)
        self.assertAlmostEqual(metrics["throughput.total"], 4115.2)
        self.assertAlmostEqual(metrics["latency.p99"], 12.3)
        self.assertAlmostEqual(metrics["availability.maxGapMs"], 812.0)

    def test_build_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_report(VALID_RUN, out=Path(tmp) / "report.html")
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("20261026-2110-baseline-kill-leader", text)
            self.assertIn("延迟分布", text)
            self.assertIn("CPU 使用率", text)

    def test_build_report_with_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_report(VALID_RUN, baseline_dir=VALID_RUN, out=Path(tmp) / "report.html")
            text = path.read_text(encoding="utf-8")
            self.assertIn("基线对比", text)

    def test_regression_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = Runner(SCENARIO, run_dir=tmp, baseline=VALID_RUN)
            checks: list[dict] = []
            meta = runner._baseline_checks({"throughput.total": 3000.0, "latency.p99": 20.0}, checks)

            self.assertIsNotNone(meta)
            self.assertEqual(len(checks), 2)
            self.assertTrue(all(check["type"] == "regression" for check in checks))
            self.assertTrue(all(not check["passed"] for check in checks))
            self.assertLess(meta["deltas"]["throughput.total"], -20)


if __name__ == "__main__":
    unittest.main()
