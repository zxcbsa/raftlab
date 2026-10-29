"""Scenario orchestrator: execute a YAML scenario and produce a run directory."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path

import yaml

from . import __version__
from .chaos import Chaos
from .cluster import Cluster, iso_now
from .evaluator import (
    EvalError,
    aggregate_results,
    build_verdict,
    checks_from_assertions,
    make_check,
    metrics_from_run_dir,
    parse_duration,
    write_verdict,
)
from .events import EventLog
from .schema import load_and_validate_path
from .telemetry import Telemetry, query_status
from .workload import run_bench


class RunError(RuntimeError):
    """Scenario cannot be executed (bad step, missing artifact, timeout)."""


class Runner:
    def __init__(
        self,
        scenario_path: str | Path,
        run_dir: str | Path | None = None,
        seed: int | None = None,
        baseline: str | Path | None = None,
        verbose: bool = True,
    ) -> None:
        self.path = Path(scenario_path).resolve()
        self.scenario = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        self.spec = self.scenario["spec"]
        self.name = self.scenario["metadata"]["name"]
        self.seed = int(seed if seed is not None else self.spec["seed"])
        self.baseline = Path(baseline).resolve() if baseline else None
        self.verbose = verbose

        stamp = time.strftime("%Y%m%d-%H%M")
        self.run_id = f"{stamp}-{self.name}"
        self.run_dir = Path(run_dir).resolve() if run_dir else (Path("runs") / self.run_id).resolve()

        self.events: EventLog | None = None
        self.cluster: Cluster | None = None
        self.telemetry: Telemetry | None = None
        self.workloads: dict[str, dict] = {}
        self.results: list[dict] = []
        self.checks: list[dict] = []
        self.availability = {"maxGapMs": 0.0, "detectMs": None, "recoveryMs": None}
        self.error: str | None = None
        self.steps: list[dict] = []
        self.issues: list[str] = []
        self.verdict: dict = {}

    # ------------------------------------------------------------------ #
    # public
    # ------------------------------------------------------------------ #

    def run(self) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "metrics.jsonl").touch()
        self.events = EventLog(self.run_id, self.run_dir / "events.json")
        self.events.add("orchestrator", "run.start", self.name, seed=self.seed)
        self._write_manifest()
        self._log(f"run {self.run_id} -> {self.run_dir}")

        try:
            for step in self.spec["steps"]:
                self._run_step(step)
        except Exception as exc:  # noqa: BLE001 - 记录后统一收尾
            self.error = str(exc)
            self._log(f"aborted: {exc}")
        finally:
            self._shutdown()

        self._finalize()
        return self.verdict

    # ------------------------------------------------------------------ #
    # steps
    # ------------------------------------------------------------------ #

    def _run_step(self, step: dict) -> None:
        action = step["action"]
        self._wait_after(step)
        started = time.monotonic()

        try:
            handler = getattr(self, "_action_" + action.replace(".", "_"))
            handler(step)
        except Exception as exc:  # noqa: BLE001
            self._log(f"step {step['id']} ({action}) failed: {exc}")
            self.events.add("orchestrator", "step.failed", step["id"], action=action, error=str(exc))
            if step.get("onError", self._defaults("onError", "abort")) == "continue":
                return
            raise
        finally:
            self.steps.append({"id": step["id"], "action": action, "ms": int((time.monotonic() - started) * 1000)})

    def _wait_after(self, step: dict) -> None:
        after = step.get("after")
        if after is None:
            return
        if isinstance(after, str):
            time.sleep(parse_duration(after) / 1000.0)
            return

        entry = self.workloads.get(after["ref"])
        if entry is None:
            raise RunError(f"after.ref '{after['ref']}' is not a started workload")
        deadline = entry["started"] + parse_duration(after["elapsed"]) / 1000.0
        while time.monotonic() < deadline:
            if entry["thread"] is not None and not entry["thread"].is_alive():
                return
            time.sleep(0.05)

    # -- actions ------------------------------------------------------- #

    def _action_cluster_up(self, step: dict) -> None:
        cfg = self.spec["cluster"]
        self.cluster = Cluster(
            nodes=cfg["nodes"],
            workdir=self.run_dir,
            startup_timeout=parse_duration(step.get("timeout", self._defaults("timeout", "30s"))) / 1000.0,
            mode=cfg.get("mode", "fork"),
            max_raft_state=cfg.get("maxRaftState", 500),
        )
        self.cluster.start()
        self._start_telemetry()

    def _action_cluster_down(self, step: dict) -> None:
        self._stop_telemetry()
        if self.cluster is not None:
            self.cluster.stop()
            self.cluster = None

    def _action_cluster_restart(self, step: dict) -> None:
        if self.cluster is None:
            raise RunError("cluster.restart before cluster.up")
        self._stop_telemetry()
        self.cluster.restart()
        self._start_telemetry()

    def _action_wait_ready(self, step: dict) -> None:
        timeout = parse_duration(step.get("timeout", self._defaults("timeout", "30s"))) / 1000.0
        if step.get("require", "all_nodes") == "leader":
            self._wait_leader(timeout)
            return
        if self.cluster is None:
            raise RunError("wait.ready before cluster.up")
        self.cluster.wait_ready(timeout)

    def _action_workload_run(self, step: dict) -> None:
        mix = step["mix"]
        params = {
            "load_id": step["loadId"],
            "duration": max(1, int(round(parse_duration(step["duration"]) / 1000.0))),
            "warmup": int(round(parse_duration(step.get("warmup", 0)) / 1000.0)),
            "concurrency": step["concurrency"],
            "mix": (mix["put"], mix["get"], mix["append"]),
            "key_space": step.get("keySpace", 1000),
            "value_size": step.get("valueSize", 128),
        }
        entry = {"thread": None, "result": None, "error": None, "started": time.monotonic()}
        self.workloads[step["id"]] = entry

        if step.get("background"):
            thread = threading.Thread(target=self._bench_job, args=(entry, params), daemon=True)
            entry["thread"] = thread
            thread.start()
        else:
            self._bench_job(entry, params)

    def _action_workload_wait(self, step: dict) -> None:
        entry = self.workloads.get(step["ref"])
        if entry is None:
            raise RunError(f"workload.wait references unknown load '{step['ref']}'")
        timeout = parse_duration(step.get("timeout", self._defaults("timeout", "60s"))) / 1000.0
        if entry["thread"] is not None:
            entry["thread"].join(timeout)
            if entry["thread"].is_alive():
                raise RunError(f"workload {step['ref']} did not finish within {timeout:.0f}s")
        if entry["error"] is not None:
            raise RunError(f"workload {step['ref']} failed: {entry['error']}")

    def _action_chaos_inject(self, step: dict) -> None:
        if self.cluster is None:
            raise RunError("chaos.inject before cluster.up")

        chaos = Chaos(self.run_dir, events=self.events)
        fault = step["fault"]
        duration_ms = int(parse_duration(step["duration"])) if "duration" in step else None

        for index in self._resolve_targets(step["target"]):
            target = f"node{index}"
            if fault == "kill":
                chaos.kill(target)
            elif fault == "stop":
                chaos.stop(target, duration_ms)
            elif fault == "resume":
                chaos.resume(target)
            elif fault == "restart":
                chaos.restart(f"node{index}")
            elif fault == "partition":
                chaos.partition(target, step.get("direction", "both"), duration_ms)
            else:
                raise RunError(f"fault '{fault}' is not implemented yet")

    def _action_assert_availability(self, step: dict) -> None:
        self._compute_availability()
        error_rate = self._error_rate()
        self.checks.append(
            make_check(
                "availability.maxGapMs",
                "availability",
                "availability.maxGapMs",
                self.availability["maxGapMs"],
                "<=",
                float(step["maxGapMs"]),
            )
        )
        self.checks.append(
            make_check("availability.errorRate", "availability", "errors.rate", error_rate, "<=", float(step["maxErrorRate"]))
        )

    def _action_sleep(self, step: dict) -> None:
        time.sleep(parse_duration(step["duration"]) / 1000.0)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _bench_job(self, entry: dict, params: dict) -> None:
        try:
            result = run_bench(self.run_dir, run_id=self.run_id, **params)
            entry["result"] = result
            self.results.append(result)
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)

    def _wait_leader(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                statuses = query_status(self.run_dir)
            except RuntimeError as exc:
                raise RunError(f"wait.ready(leader) needs kvstatus: {exc}") from exc
            leaders = [item for item in statuses if item.get("ok") and item.get("isLeader")]
            if leaders:
                self.events.add("orchestrator", "leader.ready", f"node{leaders[0]['index']}", term=leaders[0].get("term"))
                return
            time.sleep(0.2)

        raise RunError(f"no leader within {timeout:.0f}s {last_error}")

    def _resolve_targets(self, target: str) -> list[int]:
        if target == "all":
            return [node.index for node in self.cluster.info.nodes]
        match = re.match(r"^node(\d+)$", str(target))
        if match:
            return [int(match.group(1))]

        statuses = query_status(self.run_dir)
        alive = [item for item in statuses if item.get("ok")]
        if target == "leader":
            leaders = [item for item in alive if item.get("isLeader")]
            if not leaders:
                raise RunError("no leader found for chaos target 'leader'")
            return [int(leaders[0]["index"])]
        if target == "follower":
            followers = [item for item in alive if not item.get("isLeader")]
            if not followers:
                raise RunError("no follower found for chaos target 'follower'")
            return [int(followers[0]["index"])]
        raise RunError(f"unknown chaos target: {target}")

    def _start_telemetry(self) -> None:
        try:
            self.telemetry = Telemetry(self.run_dir, interval=1.0, events=self.events)
            self.telemetry.start()
        except RuntimeError as exc:
            self.telemetry = None
            self._log(f"telemetry disabled: {exc}")

    def _stop_telemetry(self) -> None:
        if self.telemetry is not None:
            self.telemetry.stop()
            self.telemetry = None

    def _compute_availability(self) -> None:
        assert self.events is not None
        events = self.events.events
        injects = [
            item
            for item in events
            if item["type"] == "fault.inject"
            and item.get("detail", {}).get("fault") in ("kill", "stop", "partition")
        ]
        if not injects:
            self.availability = {"maxGapMs": 0.0, "detectMs": None, "recoveryMs": None}
            return

        start = min(item["tRelMs"] for item in injects)
        recovery = None
        for item in events:
            if item["tRelMs"] <= start:
                continue
            if item["type"] in ("leader.change", "fault.recover"):
                recovery = item["tRelMs"]
                break

        if recovery is None:
            end = max(item["tRelMs"] for item in events)
        else:
            end = recovery
        self.availability = {
            "maxGapMs": float(max(0, end - start)),
            "detectMs": None,
            "recoveryMs": float(recovery - start) if recovery is not None else None,
        }

    def _error_rate(self) -> float:
        ok = sum(item["counts"]["ok"] for item in self.results)
        error = sum(item["counts"]["error"] for item in self.results)
        total = ok + error
        return error / total if total else 0.0

    def _shutdown(self) -> None:
        for entry in self.workloads.values():
            if entry["thread"] is not None and entry["thread"].is_alive():
                entry["thread"].join(5)
        self._stop_telemetry()
        if self.cluster is not None:
            self.cluster.stop()
            self.cluster = None

    def _finalize(self) -> None:
        assert self.events is not None
        self._compute_availability()
        self.events.add("orchestrator", "run.end", self.name, error=self.error or "")
        self.events.save()

        metrics: dict[str, float] = {}
        if self.results:
            try:
                metrics = aggregate_results(self.results)
            except EvalError:
                metrics = {}
            metrics["availability.maxGapMs"] = self.availability["maxGapMs"]

        checks = list(self.checks)
        try:
            checks += checks_from_assertions(self.spec.get("assertions", []), metrics)
        except EvalError as exc:
            self._log(f"assertions skipped: {exc}")

        baseline_meta = self._baseline_checks(metrics, checks)
        verdict = build_verdict(self.run_id, checks, baseline_meta)
        if self.error:
            verdict["status"] = "error"
        write_verdict(self.run_dir, verdict)
        self.verdict = verdict

        check = load_and_validate_path(self.run_dir, allow_missing=True)
        self.issues = [str(issue) for issue in check.issues]

    def _baseline_checks(self, metrics: dict, checks: list) -> dict | None:
        """Compare with a baseline run: >+20% p99 or <-20% throughput fails."""
        if self.baseline is None or not metrics:
            return None

        base = metrics_from_run_dir(self.baseline)
        if not base:
            self._log(f"baseline has no metrics: {self.baseline}")
            return None

        deltas = {}
        for key in ("throughput.total", "latency.p99", "errors.rate", "availability.maxGapMs"):
            if key in base and key in metrics and base[key]:
                deltas[key] = round((metrics[key] - base[key]) / base[key] * 100.0, 1)
        if not deltas:
            return None

        checks.append(
            make_check(
                "regression.latency.p99",
                "regression",
                "latency.p99.deltaPct",
                deltas.get("latency.p99", 0.0),
                "<=",
                20.0,
            )
        )
        checks.append(
            make_check(
                "regression.throughput",
                "regression",
                "throughput.total.deltaPct",
                deltas.get("throughput.total", 0.0),
                ">=",
                -20.0,
            )
        )

        baseline_run_id = self.baseline.name
        try:
            manifest = json.loads((self.baseline / "manifest.json").read_text(encoding="utf-8"))
            baseline_run_id = manifest.get("runId", baseline_run_id)
        except (OSError, ValueError):
            pass

        return {"runId": baseline_run_id, "deltas": deltas}

    def _defaults(self, key: str, fallback):
        return self.spec.get("defaults", {}).get(key, fallback)

    def _write_manifest(self) -> None:
        cluster = self.spec["cluster"]
        data = {
            "schemaVersion": "1.0",
            "runId": self.run_id,
            "createdAt": iso_now(),
            "scenario": {
                "name": self.name,
                "path": str(self.path),
                "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            },
            "seed": self.seed,
            "host": _host_info(),
            "build": _build_info(cluster.get("build", "Debug")),
            "cluster": {
                "nodes": cluster["nodes"],
                "maxRaftState": cluster.get("maxRaftState", 500),
                "logLevel": cluster.get("logLevel", "off"),
            },
            "tool": {"name": "raftlab", "version": __version__},
        }
        (self.run_dir / "manifest.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[raftlab] {message}", flush=True)


# --------------------------------------------------------------------------- #
# environment info
# --------------------------------------------------------------------------- #

def _host_info() -> dict:
    mem_mb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_mb = int(int(line.split()[1]) / 1024)
                break
    except OSError:
        mem_mb = 0
    return {
        "hostname": platform.node() or "unknown",
        "os": platform.platform(),
        "cpuCores": os.cpu_count() or 1,
        "memTotalMB": max(1, mem_mb),
    }


def _build_info(declared_type: str = "Debug") -> dict:
    root = Path(__file__).resolve().parent.parent
    commit = ""
    dirty = False
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        pass

    build_type = _detect_build_type() or declared_type
    if build_type not in ("Release", "Debug"):
        build_type = "Debug"

    return {
        "type": build_type,
        "gitCommit": commit or "unknown",
        "gitDirty": dirty,
        "compiler": _first_line(["g++", "--version"]),
        "protobuf": _first_line(["protoc", "--version"]),
    }


def _detect_build_type() -> str | None:
    """Read CMAKE_BUILD_TYPE from build/CMakeCache.txt when available."""
    cache = Path(__file__).resolve().parent.parent / "build" / "CMakeCache.txt"
    try:
        for line in cache.read_text(encoding="utf-8").splitlines():
            if line.startswith("CMAKE_BUILD_TYPE:"):
                value = line.split("=", 1)[1].strip()
                if value in ("Release", "Debug"):
                    return value
    except OSError:
        return None
    return None


def _first_line(command: list[str]) -> str:
    try:
        output = subprocess.run(command, capture_output=True, text=True, timeout=5).stdout.strip()
        return output.splitlines()[0] if output else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
