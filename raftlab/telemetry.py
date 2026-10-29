"""Telemetry: node status (Status RPC) + process resources -> metrics.jsonl."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .cluster import Cluster, iso_now
from .paths import bin_path, hint

CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _read_cpu_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    # comm 字段可能包含空格和括号，取最后一个 ')' 之后的部分
    rest = text[text.rindex(")") + 2 :].split()
    # 字段 14/15（utime/stime）在 comm 之后的下标是 11/12
    return int(rest[11]) + int(rest[12])


def _read_rss_mb(pid: int) -> float:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    except (OSError, ValueError):
        return 0.0
    return 0.0


def status_binary() -> Path:
    return bin_path("kvstatus")


def query_status(workdir: str | Path, binary: str | Path | None = None, timeout_ms: int = 500) -> list[dict]:
    """Call kvstatus once; returns one dict per node (ok=False when unreachable)."""
    workdir = Path(workdir).resolve()
    executable = Path(binary) if binary else status_binary()
    if not executable.exists():
        raise RuntimeError(f"kvstatus not found: {executable}\nbuild it with: cmake --build build -j\n{hint()}")
    if not (workdir / "test.conf").exists():
        raise RuntimeError(f"no test.conf in {workdir}")

    completed = subprocess.run(
        [str(executable), "--config", "test.conf", "--timeout-ms", str(timeout_ms)],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=max(5.0, timeout_ms / 1000.0 * 12),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"kvstatus failed: {(completed.stderr or completed.stdout).strip()}")

    statuses = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        # kvstatus 内部 RPC 会往 stdout 打 DPrintf 日志，只取 JSON 行
        if not line.startswith("{"):
            continue
        try:
            statuses.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return statuses


class Telemetry:
    """Background sampler writing metrics.jsonl (one line per node per tick)."""

    def __init__(
        self,
        workdir: str | Path,
        interval: float = 1.0,
        binary: str | Path | None = None,
        events=None,
    ) -> None:
        self.workdir = Path(workdir).resolve()
        self.cluster = Cluster.attach(self.workdir)
        self.interval = interval
        self.binary = Path(binary) if binary else status_binary()
        if not self.binary.exists():
            raise RuntimeError(f"kvstatus not found: {self.binary}\nbuild it with: cmake --build build -j\n{hint()}")
        self.events = events
        self.path = self.workdir / "metrics.jsonl"

        self.samples = 0
        self.leader_changes = 0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._prev: dict[int, tuple[int, float]] = {}
        self._t0 = time.monotonic()
        self._leader: int | None = None

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def sample_once(self) -> list[dict]:
        now = time.monotonic()
        t_rel = int((now - self._t0) * 1000)

        try:
            statuses = query_status(self.workdir, self.binary)
        except Exception:  # 采样失败不应中断实验，标记 sampled=false 即可
            statuses = []

        by_index = {item.get("index"): item for item in statuses}
        lines = []
        for node in self.cluster.info.nodes:
            status = by_index.get(node.index, {})
            lines.append(
                {
                    "ts": iso_now(),
                    "tRelMs": t_rel,
                    "node": node.index,
                    "cpuPct": round(self._cpu_percent(node.pid, now), 1),
                    "rssMB": round(_read_rss_mb(node.pid), 1),
                    "term": status.get("term"),
                    "isLeader": status.get("isLeader"),
                    "commitIndex": status.get("commitIndex"),
                    "logSize": status.get("logSize"),
                    "snapshotIndex": status.get("snapshotIndex"),
                    "sampled": bool(status.get("ok")),
                }
            )

        self._track_leader(statuses)

        with self.path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")

        self.samples += 1
        return lines

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None

    def __enter__(self) -> "Telemetry":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:  # 采样失败不应中断整个实验
                pass
            self._stop.wait(self.interval)

    def _cpu_percent(self, pid: int, now: float) -> float:
        ticks = _read_cpu_ticks(pid)
        if ticks is None:
            self._prev.pop(pid, None)
            return 0.0

        previous = self._prev.get(pid)
        self._prev[pid] = (ticks, now)
        if previous is None:
            return 0.0

        previous_ticks, previous_time = previous
        delta = now - previous_time
        if delta <= 0:
            return 0.0
        return max(0.0, (ticks - previous_ticks) / CLOCK_TICKS / delta * 100.0)

    def _track_leader(self, statuses: list[dict]) -> None:
        leaders = [item for item in statuses if item.get("ok") and item.get("isLeader")]
        if not leaders:
            return
        current = leaders[0].get("index")
        if current == self._leader:
            return

        previous = self._leader
        self._leader = current
        if previous is not None:
            self.leader_changes += 1
            if self.events is not None:
                self.events.add(
                    "telemetry",
                    "leader.change",
                    f"node{current}",
                    **{"from": previous, "to": current, "term": leaders[0].get("term")},
                )
                self.events.save()
