"""Workload runner: launches bin/kvbench and validates result.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .paths import bin_path, hint
from .schema import load_and_validate_path


class BenchError(RuntimeError):
    """kvbench failed, or produced an invalid result.json."""


def run_bench(
    workdir: str | Path,
    run_id: str,
    load_id: str = "L1",
    duration: int = 10,
    warmup: int = 0,
    concurrency: int = 4,
    mix: tuple[int, int, int] = (20, 70, 10),
    key_space: int = 1000,
    value_size: int = 128,
    timeout_ms: int = 1000,
    binary: str | Path | None = None,
) -> dict:
    workdir = Path(workdir).resolve()
    bench = Path(binary) if binary else bin_path("kvbench")
    if not bench.exists():
        raise BenchError(f"kvbench not found: {bench}\nbuild it with: cmake --build build -j\n{hint()}")
    if not (workdir / "test.conf").exists():
        raise BenchError(f"no test.conf in {workdir}; start the cluster first")

    command = [
        str(bench),
        "--config",
        "test.conf",
        "--out",
        "result.json",
        "--run-id",
        run_id,
        "--load-id",
        load_id,
        "--duration",
        str(duration),
        "--warmup",
        str(warmup),
        "--concurrency",
        str(concurrency),
        "--mix",
        f"{mix[0]},{mix[1]},{mix[2]}",
        "--key-space",
        str(key_space),
        "--value-size",
        str(value_size),
        "--timeout-ms",
        str(timeout_ms),
    ]

    completed = subprocess.run(
        command,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=duration + 60,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise BenchError(f"kvbench failed (exit {completed.returncode}):\n{detail}")

    result_path = workdir / "result.json"
    if not result_path.exists():
        raise BenchError("kvbench finished but result.json is missing")

    check = load_and_validate_path(result_path)
    if not check.ok:
        details = "\n".join(f"  {issue}" for issue in check.issues)
        raise BenchError(f"result.json violates the contract:\n{details}")

    return json.loads(result_path.read_text(encoding="utf-8"))
