"""Fault injection: kill / stop / resume / partition."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path

from .cluster import Cluster, ClusterError, NodeInfo, pid_alive
from .events import EventLog

NODE_RE = re.compile(r"^node(\d+)$")


class ChaosError(RuntimeError):
    """Fault cannot be injected (unknown target, no permission, node already dead)."""


class Chaos:
    def __init__(self, workdir: str | Path, events: EventLog | None = None) -> None:
        self.workdir = Path(workdir).resolve()
        try:
            self.cluster = Cluster.attach(self.workdir)
        except ClusterError as exc:
            raise ChaosError(str(exc)) from exc
        self.run_id = self._run_id()
        self.events = events if events is not None else EventLog(self.run_id, self.workdir / "events.json")

    # ------------------------------------------------------------------ #
    # faults
    # ------------------------------------------------------------------ #

    def kill(self, target) -> NodeInfo:
        node = self._node(target)
        if not pid_alive(node.pid):
            raise ChaosError(f"node{node.index} is not running")
        os.kill(node.pid, signal.SIGKILL)
        self.events.add("chaos", "fault.inject", f"node{node.index}", fault="kill", pid=node.pid)
        self.events.add("cluster", "node.down", f"node{node.index}")
        self.events.save()
        return node

    def stop(self, target, duration_ms: int | None = None) -> NodeInfo:
        node = self._node(target)
        if not pid_alive(node.pid):
            raise ChaosError(f"node{node.index} is not running")
        os.kill(node.pid, signal.SIGSTOP)
        self.events.add("chaos", "fault.inject", f"node{node.index}", fault="stop", pid=node.pid)
        self.events.save()
        if duration_ms:
            self._schedule(lambda: self.resume(node.index), duration_ms)
        return node

    def resume(self, target) -> NodeInfo:
        node = self._node(target)
        os.kill(node.pid, signal.SIGCONT)
        self.events.add("chaos", "fault.recover", f"node{node.index}", fault="stop")
        self.events.save()
        return node

    def partition(self, target, direction: str = "both", duration_ms: int | None = None) -> NodeInfo:
        node = self._node(target)
        if shutil.which("iptables") is None:
            raise ChaosError("iptables not found on this host")
        self._iptables("-I", node.port, direction)
        self.events.add(
            "chaos", "fault.inject", f"node{node.index}", fault="partition", direction=direction, port=node.port
        )
        self.events.save()
        if duration_ms:
            self._schedule(lambda: self._restore_partition(node.index, node.port, direction), duration_ms)
        return node

    def restore_partition(self, target, direction: str = "both") -> NodeInfo:
        node = self._node(target)
        self._iptables("-D", node.port, direction)
        self.events.add("chaos", "fault.recover", f"node{node.index}", fault="partition")
        self.events.save()
        return node

    def restart(self, target) -> NodeInfo:
        node = self._node(target)
        self.events.add("chaos", "fault.inject", f"node{node.index}", fault="restart")
        self.events.save()
        self.cluster.restart(node.index)
        self.events.add("chaos", "fault.recover", f"node{node.index}", fault="restart")
        self.events.save()
        return node

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _node(self, target) -> NodeInfo:
        if isinstance(target, int):
            index = target
        else:
            match = NODE_RE.match(str(target))
            if match:
                index = int(match.group(1))
            elif str(target) == "leader":
                from .telemetry import query_status

                statuses = query_status(self.workdir)
                leaders = [item for item in statuses if item.get("ok") and item.get("isLeader")]
                if not leaders:
                    raise ChaosError("no leader found right now (election in progress?)")
                index = int(leaders[0]["index"])
            else:
                raise ChaosError(f"unknown target: {target}")

        for node in self.cluster.info.nodes:
            if node.index == index:
                return node
        raise ChaosError(f"node{index} not found in cluster.json")

    def _iptables(self, action: str, port: int, direction: str) -> None:
        chains = []
        if direction in ("in", "both"):
            chains.append("INPUT")
        if direction in ("out", "both"):
            chains.append("OUTPUT")
        if not chains:
            raise ChaosError("direction must be one of: in / out / both")

        for chain in chains:
            command = [
                "sudo",
                "-n",
                "iptables",
                action,
                chain,
                "-p",
                "tcp",
                "--dport",
                str(port),
                "-j",
                "DROP",
            ]
            completed = subprocess.run(command, capture_output=True, text=True)
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise ChaosError(
                    "iptables needs root and non-interactive sudo.\n"
                    "hint: configure NOPASSWD for iptables, or run raftlab as root.\n"
                    f"detail: {detail}"
                )

    def _restore_partition(self, index: int, port: int, direction: str) -> None:
        try:
            self._iptables("-D", port, direction)
        finally:
            self.events.add("chaos", "fault.recover", f"node{index}", fault="partition")
            self.events.save()

    def _schedule(self, fn, delay_ms: int) -> threading.Timer:
        timer = threading.Timer(delay_ms / 1000.0, fn)
        timer.daemon = True
        timer.start()
        return timer

    def _run_id(self) -> str:
        manifest = self.workdir / "manifest.json"
        if manifest.exists():
            try:
                import json

                return json.loads(manifest.read_text(encoding="utf-8")).get("runId", self.workdir.name)
            except (ValueError, OSError):
                pass
        return self.workdir.name
