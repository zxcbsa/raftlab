"""Command line entry point for raftlab."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .chaos import Chaos, ChaosError
from .cluster import Cluster, ClusterError
from .schema import RUN_FILES, ValidationError, load_and_validate_path
from .workload import BenchError, run_bench


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _print_schema_result(result) -> None:
    if result.ok:
        print(f"OK   {result.target}  ({result.files} file(s), semantic checks passed)")
        return
    print(f"FAIL {result.target}")
    for issue in result.issues:
        print(f"  {issue}")


def _print_cluster(info) -> None:
    for node in info.nodes:
        print(f"node{node.index}  pid={node.pid:<7} {node.ip}:{node.port}")
    if info.ready_at:
        print(f"ready at {info.ready_at}")


def _run_id(workdir: Path) -> str:
    manifest = workdir / "manifest.json"
    if manifest.exists():
        try:
            return json.loads(manifest.read_text(encoding="utf-8")).get("runId", workdir.name)
        except (ValueError, OSError):
            pass
    return workdir.name


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="raftlab",
        description="Reproducible performance and chaos experiments for the Raft KV cluster.",
    )
    parser.add_argument("--version", action="version", version=f"raftlab {__version__}")
    parser.add_argument(
        "--project",
        metavar="PATH",
        help="KV project root containing bin/ (or set RAFTLAB_TARGET)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # schema ---------------------------------------------------------------
    schema = sub.add_parser("schema", help="validate scenarios and run artifacts")
    group = schema.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", metavar="PATH", help="scenario file or run directory")
    group.add_argument("--list", action="store_true", help="list artifact files of a run directory")
    schema.add_argument("--json", action="store_true", help="machine readable output")
    schema.add_argument(
        "--allow-missing",
        action="store_true",
        help="do not fail when a run directory is missing some artifacts",
    )

    # cluster --------------------------------------------------------------
    cluster = sub.add_parser("cluster", help="start / inspect / stop the cluster")
    cluster_sub = cluster.add_subparsers(dest="action", required=True)

    up = cluster_sub.add_parser("up", help="start a cluster and wait until it is ready")
    up.add_argument("--nodes", type=int, default=3)
    up.add_argument("--workdir", required=True)
    up.add_argument("--timeout", type=float, default=30.0)
    up.add_argument("--mode", choices=["fork", "per-node"], default="fork")
    up.add_argument("--max-raft-state", type=int, default=500)
    up.add_argument("--binary", help="path to raftCoreRun (default: <project>/bin/raftCoreRun)")
    up.add_argument("--json", action="store_true")

    status = cluster_sub.add_parser("status", help="show node liveness")
    status.add_argument("--workdir", required=True)
    status.add_argument("--json", action="store_true")

    down = cluster_sub.add_parser("down", help="stop the cluster")
    down.add_argument("--workdir", required=True)
    down.add_argument("--timeout", type=float, default=5.0)

    restart = cluster_sub.add_parser("restart", help="stop and start the whole cluster")
    restart.add_argument("--workdir", required=True)
    restart.add_argument("--binary")
    restart.add_argument("--timeout", type=float, default=5.0)

    # bench ----------------------------------------------------------------
    bench = sub.add_parser("bench", help="run a workload against a running cluster")
    bench.add_argument("--workdir", required=True)
    bench.add_argument("--duration", type=int, default=10, help="seconds, including warmup")
    bench.add_argument("--warmup", type=int, default=0)
    bench.add_argument("--concurrency", type=int, default=4)
    bench.add_argument("--mix", default="20,70,10", help="put,get,append percentages")
    bench.add_argument("--key-space", type=int, default=1000)
    bench.add_argument("--value-size", type=int, default=128)
    bench.add_argument("--timeout-ms", type=int, default=1000)
    bench.add_argument("--load-id", default="L1")
    bench.add_argument("--binary")
    bench.add_argument("--json", action="store_true")

    # chaos ----------------------------------------------------------------
    chaos = sub.add_parser("chaos", help="inject faults into a running cluster")
    chaos_sub = chaos.add_subparsers(dest="fault", required=True)

    for name in ("kill", "stop", "resume", "restart", "partition"):
        fault = chaos_sub.add_parser(name)
        fault.add_argument("--workdir", required=True)
        fault.add_argument("--target", required=True, help="node<N> or leader")
        if name in ("stop", "partition"):
            fault.add_argument("--duration-ms", type=int, default=3000)
        if name == "partition":
            fault.add_argument("--direction", default="both", choices=["in", "out", "both"])

    # telemetry ------------------------------------------------------------
    telemetry = sub.add_parser("telemetry", help="sample node status and resources")
    telemetry.add_argument("--workdir", required=True)
    telemetry.add_argument("--duration", type=float, default=10.0)
    telemetry.add_argument("--interval", type=float, default=1.0)
    telemetry.add_argument("--binary", help="path to kvstatus")

    # run ------------------------------------------------------------------
    run = sub.add_parser("run", help="execute a scenario end to end")
    run.add_argument("scenario", help="path to scenario yaml")
    run.add_argument("--run-dir", help="output directory (default: runs/<timestamp>-<name>)")
    run.add_argument("--seed", type=int, help="override spec.seed")
    run.add_argument("--baseline", help="baseline run directory for regression checks")
    run.add_argument("--json", action="store_true")

    # report ---------------------------------------------------------------
    report = sub.add_parser("report", help="render a single-file HTML report")
    report.add_argument("--run", required=True, help="run directory")
    report.add_argument("--baseline", help="optional baseline run directory")
    report.add_argument("--out", help="output html path (default: <run>/report.html)")

    return parser


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #

def _cmd_schema(args) -> int:
    if args.list:
        for name in RUN_FILES:
            print(name)
        return 0

    result = load_and_validate_path(Path(args.check), allow_missing=args.allow_missing)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_schema_result(result)
    return 0 if result.ok else 1


def _cmd_cluster(args) -> int:
    workdir = Path(args.workdir)

    if args.action == "up":
        started = time.monotonic()
        cluster = Cluster(
            nodes=args.nodes,
            workdir=workdir,
            binary=args.binary,
            startup_timeout=args.timeout,
            mode=args.mode,
            max_raft_state=args.max_raft_state,
        )
        info = cluster.start()
        if args.json:
            print(json.dumps(info.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_cluster(info)
            print(f"started in {time.monotonic() - started:.1f}s")
        return 0

    if args.action in ("status", "down", "restart") and not (workdir / "cluster.json").exists():
        print(f"ERROR no cluster.json in {workdir.resolve()}; run 'cluster up' first", file=sys.stderr)
        return 2

    if args.action == "status":
        cluster = Cluster.attach(workdir)
        status = cluster.status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print(f"alive {status['alive']}/{status['total']}  mode={status.get('mode', 'fork')}")
            for node in status["nodes"]:
                print(f"node{node['index']}  pid={node['pid']:<7} {node['ip']}:{node['port']}")
        return 0 if status["running"] else 1

    if args.action == "down":
        cluster = Cluster.attach(workdir)
        cluster.stop(timeout=args.timeout)
        print(f"stopped {workdir}")
        return 0

    if args.action == "restart":
        cluster = Cluster.attach(workdir)
        nodes = len(cluster.info.nodes)
        cluster.stop(timeout=args.timeout)
        time.sleep(0.5)
        fresh = Cluster(nodes=nodes, workdir=workdir, binary=args.binary, mode=cluster.mode)
        info = fresh.start()
        _print_cluster(info)
        return 0

    return 2


def _cmd_bench(args) -> int:
    workdir = Path(args.workdir).resolve()
    parts = [int(item) for item in args.mix.split(",")]
    if len(parts) != 3:
        raise BenchError("--mix expects three comma separated integers, e.g. 20,70,10")

    result = run_bench(
        workdir=workdir,
        run_id=_run_id(workdir),
        load_id=args.load_id,
        duration=args.duration,
        warmup=args.warmup,
        concurrency=args.concurrency,
        mix=(parts[0], parts[1], parts[2]),
        key_space=args.key_space,
        value_size=args.value_size,
        timeout_ms=args.timeout_ms,
        binary=args.binary,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        latency = result["latencyMs"]["all"]
        print(
            f"ops={result['counts']['total']} ok={result['counts']['ok']} err={result['counts']['error']} "
            f"throughput={result['throughput']['total']:.1f} ops/s "
            f"p50={latency['p50']}ms p99={latency['p99']}ms -> {workdir / 'result.json'}"
        )
    return 0


def _cmd_chaos(args) -> int:
    chaos = Chaos(args.workdir)
    if args.fault == "kill":
        node = chaos.kill(args.target)
        print(f"killed node{node.index} (pid {node.pid})")
    elif args.fault == "stop":
        node = chaos.stop(args.target)
        print(f"stopped node{node.index}")
        if args.duration_ms > 0:
            time.sleep(args.duration_ms / 1000.0)
            chaos.resume(node.index)
            print(f"resumed node{node.index} after {args.duration_ms}ms")
    elif args.fault == "resume":
        node = chaos.resume(args.target)
        print(f"resumed node{node.index}")
    elif args.fault == "restart":
        node = chaos.restart(args.target)
        print(f"restarted node{node.index}")
    elif args.fault == "partition":
        node = chaos.partition(args.target, direction=args.direction)
        print(f"partitioned node{node.index} ({args.direction})")
        if args.duration_ms > 0:
            time.sleep(args.duration_ms / 1000.0)
            chaos.restore_partition(node.index, direction=args.direction)
            print(f"restored node{node.index} after {args.duration_ms}ms")
    else:
        return 2
    return 0


def _cmd_telemetry(args) -> int:
    from .events import EventLog
    from .telemetry import Telemetry

    workdir = Path(args.workdir).resolve()
    events = EventLog(_run_id(workdir), workdir / "events.json")
    sampler = Telemetry(workdir, interval=args.interval, binary=args.binary, events=events)

    sampler.start()
    try:
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        sampler.stop()

    print(
        f"samples={sampler.samples} leader_changes={sampler.leader_changes} "
        f"-> {workdir / 'metrics.jsonl'}"
    )
    return 0


def _cmd_run(args) -> int:
    from .orchestrator import Runner
    from .report import build_report

    runner = Runner(args.scenario, run_dir=args.run_dir, seed=args.seed, baseline=args.baseline)
    verdict = runner.run()

    report_path = None
    try:
        report_path = build_report(runner.run_dir, baseline_dir=args.baseline)
    except Exception as exc:  # 报告失败不应影响实验结论
        print(f"WARN report failed: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        print(f"{verdict['status'].upper():5} {runner.run_id}  -> {runner.run_dir}")
        for check in verdict["checks"]:
            mark = "ok  " if check["passed"] else "FAIL"
            print(f"  {mark} {check['metric']}: {check['actual']:.2f} {check['op']} {check['threshold']:.2f}")
        if runner.error:
            print(f"  error: {runner.error}")
        for issue in runner.issues:
            print(f"  artifact: {issue}")
        if report_path is not None:
            print(f"  report: {report_path}")

    if runner.issues:
        return 2
    return 0 if verdict["status"] == "pass" else 1


def _cmd_report(args) -> int:
    from .report import build_report

    path = build_report(Path(args.run), baseline_dir=args.baseline, out=args.out)
    print(path)
    return 0


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if getattr(args, "project", None):
        os.environ["RAFTLAB_TARGET"] = args.project

    try:
        if args.command == "schema":
            return _cmd_schema(args)
        if args.command == "cluster":
            return _cmd_cluster(args)
        if args.command == "bench":
            return _cmd_bench(args)
        if args.command == "chaos":
            return _cmd_chaos(args)
        if args.command == "telemetry":
            return _cmd_telemetry(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "report":
            return _cmd_report(args)
    except ValidationError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    except (ClusterError, BenchError, ChaosError) as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1

    return 2
