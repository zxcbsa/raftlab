"""Checks, metrics and verdict.json."""

from __future__ import annotations

import json
import re
from pathlib import Path

DURATION_RE = re.compile(r"^(\d+)(ms|s|m)$")
UNIT_MS = {"ms": 1.0, "s": 1000.0, "m": 60000.0}


class EvalError(ValueError):
    """Scenario assertion or metric cannot be evaluated."""


def parse_duration(value) -> float:
    """'100ms' / '2s' / '1m' / number -> milliseconds."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise EvalError(f"invalid duration: {value!r}")
    match = DURATION_RE.match(value.strip())
    if not match:
        raise EvalError(f"invalid duration: {value!r}")
    return float(match.group(1)) * UNIT_MS[match.group(2)]


def compare(actual: float, op: str, threshold: float) -> bool:
    if op == "<=":
        return actual <= threshold
    if op == ">=":
        return actual >= threshold
    if op == "<":
        return actual < threshold
    if op == ">":
        return actual > threshold
    if op == "==":
        return actual == threshold
    if op == "!=":
        return actual != threshold
    raise EvalError(f"unknown operator: {op}")


def make_check(check_id: str, kind: str, metric: str, actual: float, op: str, threshold: float) -> dict:
    return {
        "id": check_id,
        "type": kind,
        "metric": metric,
        "op": op,
        "threshold": float(threshold),
        "actual": float(actual),
        "passed": bool(compare(actual, op, threshold)),
    }


def checks_from_assertions(assertions: list[dict], metrics: dict[str, float]) -> list[dict]:
    checks = []
    for index, item in enumerate(assertions):
        metric = item["metric"]
        if metric not in metrics:
            raise EvalError(f"unknown metric in assertion: {metric}")
        threshold = parse_duration(item["value"]) if isinstance(item["value"], str) else float(item["value"])
        checks.append(
            make_check(
                check_id=f"slo.{metric}#{index}",
                kind="slo",
                metric=metric,
                actual=float(metrics[metric]),
                op=item["op"],
                threshold=threshold,
            )
        )
    return checks


def build_verdict(run_id: str, checks: list[dict], baseline: dict | None = None) -> dict:
    failed = sum(1 for check in checks if not check["passed"])
    return {
        "schemaVersion": "1.0",
        "runId": run_id,
        "status": "fail" if failed else "pass",
        "checks": checks,
        "summary": {"passed": len(checks) - failed, "failed": failed},
        "baseline": baseline,
    }


def write_verdict(run_dir: str | Path, verdict: dict) -> Path:
    run_dir = Path(run_dir)
    path = run_dir / "verdict.json"
    tmp = run_dir / "verdict.json.tmp"
    tmp.write_text(json.dumps(verdict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def aggregate_results(results: list[dict]) -> dict[str, float]:
    """Combine result.json documents into flat metrics."""
    if not results:
        raise EvalError("no workload result to aggregate")

    if len(results) == 1:
        item = results[0]
        return {
            "latency.p50": float(item["latencyMs"]["all"]["p50"]),
            "latency.p99": float(item["latencyMs"]["all"]["p99"]),
            "latency.p999": float(item["latencyMs"]["all"]["p999"]),
            "errors.rate": float(item["errors"]["rate"]),
            "throughput.total": float(item["throughput"]["total"]),
        }

    ok = sum(item["counts"]["ok"] for item in results)
    error = sum(item["counts"]["error"] for item in results)
    total = ok + error
    seconds = sum(max(1.0, item["durationMs"] - item["warmupMs"]) for item in results) / 1000.0

    return {
        "latency.p50": max(item["latencyMs"]["all"]["p50"] for item in results),
        "latency.p99": max(item["latencyMs"]["all"]["p99"] for item in results),
        "latency.p999": max(item["latencyMs"]["all"]["p999"] for item in results),
        "errors.rate": (error / total) if total else 0.0,
        "throughput.total": ok / seconds,
    }


def metrics_from_run_dir(run_dir: str | Path) -> dict[str, float]:
    """Read a finished run directory and flatten it to comparable metrics."""
    run_dir = Path(run_dir)
    metrics: dict[str, float] = {}

    result_path = run_dir / "result.json"
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            metrics["latency.p50"] = float(result["latencyMs"]["all"]["p50"])
            metrics["latency.p99"] = float(result["latencyMs"]["all"]["p99"])
            # 优先使用 kvbench 自己报的口径，保证与 result.json 一致
            metrics["throughput.total"] = float(result["throughput"]["total"])
            metrics["errors.rate"] = float(result["errors"]["rate"])
        except (ValueError, KeyError, ZeroDivisionError):
            pass

    verdict_path = run_dir / "verdict.json"
    if verdict_path.exists():
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            for check in verdict.get("checks", []):
                metric = check.get("metric", "")
                if metric == "availability.maxGapMs":
                    metrics.setdefault(metric, float(check["actual"]))
        except (ValueError, KeyError):
            pass

    return metrics
