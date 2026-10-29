"""Cluster lifecycle: fork mode and per-node mode.

fork       : 一个 raftCoreRun 进程 fork 出 N 个节点（原始行为，一个日志）
per-node   : 每个节点一个独立进程（-i/-p），可单独 kill / 重启，每节点独立日志
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .paths import bin_path, hint

CONF_RE = re.compile(r"^node(\d+)(ip|port)=(.+)$")
SS_PORT_RE = re.compile(r":(\d+)\s+\S+\s+users:")
SS_PID_RE = re.compile(r"pid=(\d+)")

MODES = ("fork", "per-node")


class ClusterError(RuntimeError):
    """Runtime failure: cluster cannot start, is not ready, or died."""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        # ProcessLookupError on POSIX, WinError 87 on Windows
        return False


def pid_alive(pid: int) -> bool:
    """Public helper used by status / chaos modules."""
    return _alive(pid)


def parse_test_conf(path: Path) -> list[tuple[int, str, int]]:
    """Return [(index, ip, port)] parsed from the nodes' test.conf."""
    ips: dict[int, str] = {}
    ports: dict[int, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = CONF_RE.match(line.strip())
        if not match:
            continue
        index = int(match.group(1))
        kind = match.group(2)
        value = match.group(3).strip()
        if kind == "ip":
            ips[index] = value
        else:
            ports[index] = int(value)
    return [(index, ips[index], ports[index]) for index in sorted(ips) if index in ports]


def _local_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def _allocate_ports(ip: str, count: int) -> list[int]:
    """Pick free ports below 32768: KvServer/RpcProvider take a signed short."""
    import random

    rng = random.SystemRandom()
    ports: list[int] = []
    while len(ports) < count:
        port = rng.randint(20000, 32000)
        if port in ports:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((ip, port))
            except OSError:
                continue
        ports.append(port)
    return ports


@dataclass
class NodeInfo:
    index: int
    pid: int
    ip: str
    port: int
    log_file: str = "cluster.log"

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "pid": self.pid,
            "ip": self.ip,
            "port": self.port,
            "logFile": self.log_file,
        }


@dataclass
class ClusterInfo:
    config_file: str
    started_at: str
    nodes: list[NodeInfo]
    ready_at: str | None = None
    parent_pid: int | None = None
    mode: str = "fork"
    max_raft_state: int = 500

    def to_dict(self) -> dict:
        data = {
            "schemaVersion": "1.0",
            "configFile": self.config_file,
            "startedAt": self.started_at,
            "mode": self.mode,
            "maxRaftState": self.max_raft_state,
            "nodes": [node.to_dict() for node in self.nodes],
        }
        if self.ready_at:
            data["readyAt"] = self.ready_at
        return data


class Cluster:
    def __init__(
        self,
        nodes: int,
        workdir: str | Path,
        binary: str | Path | None = None,
        max_raft_state: int = 500,
        startup_timeout: float = 30.0,
        startup_grace: float | None = None,
        mode: str = "fork",
    ) -> None:
        if nodes < 2:
            raise ClusterError("nodes must be >= 2: a single node can never win an election in this codebase")
        if mode not in MODES:
            raise ClusterError(f"unknown cluster mode: {mode}")
        self.nodes = nodes
        self.workdir = Path(workdir).resolve()
        self.binary = Path(binary) if binary else bin_path("raftCoreRun")
        self.max_raft_state = max_raft_state
        self.startup_timeout = startup_timeout
        self.startup_grace = startup_grace
        self.mode = mode
        self.proc: subprocess.Popen | None = None
        self.procs: dict[int, subprocess.Popen] = {}
        self.info: ClusterInfo | None = None
        self._log = None
        self._t0 = 0.0

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> ClusterInfo:
        if not self.binary.exists():
            raise ClusterError(
                f"binary not found: {self.binary}\n"
                f"build it with: cmake -S . -B build && cmake --build build -j\n"
                f"{hint()}"
            )

        self.workdir.mkdir(parents=True, exist_ok=True)
        self._guard_running()

        conf = self.workdir / "test.conf"
        if conf.exists():
            conf.unlink()

        self._t0 = time.monotonic()
        try:
            if self.mode == "per-node":
                self._start_per_node(conf)
            else:
                self._start_fork(conf)
            self.wait_ready()
        except Exception:
            self.stop(timeout=2.0)
            raise

        return self.info

    def _start_fork(self, conf: Path) -> None:
        self._log = (self.workdir / "cluster.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(self.binary), "-n", str(self.nodes), "-m", str(self.max_raft_state), "-f", "test.conf"],
            cwd=self.workdir,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
        )
        self._write_pid(self.proc.pid)

        entries = self._wait_config(conf)
        children = self._children(self.proc.pid)
        port_to_pid = self._pids_by_port([port for _, _, port in entries])
        nodes: list[NodeInfo] = []
        for position, (index, ip, port) in enumerate(entries):
            pid = port_to_pid.get(port)
            if pid is None and position < len(children):
                pid = children[position]
            if not pid:
                raise ClusterError(f"cannot map node{index} to a pid (is 'ps'/'ss' available?)")
            nodes.append(NodeInfo(index=index, pid=pid, ip=ip, port=port))

        self.info = ClusterInfo(
            config_file="test.conf",
            started_at=iso_now(),
            nodes=nodes,
            parent_pid=self.proc.pid,
            mode="fork",
            max_raft_state=self.max_raft_state,
        )
        self._write_info()

    def _start_per_node(self, conf: Path) -> None:
        ip = _local_ip()
        ports = _allocate_ports(ip, self.nodes)
        (self.workdir / "logs").mkdir(exist_ok=True)

        for index in range(self.nodes):
            self._launch_node(index, ip, ports[index])
            # 与 fork 模式一致：错开 1s，使各节点 sleep(6)+(N-i) 后几乎同时 init
            time.sleep(1.0)

        self._wait_config(conf)

        self.info = ClusterInfo(
            config_file="test.conf",
            started_at=iso_now(),
            nodes=[
                NodeInfo(
                    index=index,
                    pid=self.procs[index].pid,
                    ip=ip,
                    port=ports[index],
                    log_file=f"logs/node{index}.log",
                )
                for index in range(self.nodes)
            ],
            parent_pid=None,
            mode="per-node",
            max_raft_state=self.max_raft_state,
        )
        self._write_info()

    def _launch_node(self, index: int, ip: str, port: int) -> None:
        log_path = self.workdir / "logs" / f"node{index}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("a", encoding="utf-8")
        try:
            proc = subprocess.Popen(
                [
                    str(self.binary),
                    "-n",
                    str(self.nodes),
                    "-i",
                    str(index),
                    "-p",
                    str(port),
                    "-m",
                    str(self.max_raft_state),
                    "-f",
                    "test.conf",
                ],
                cwd=self.workdir,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
            )
        finally:
            handle.close()
        self.procs[index] = proc

    def wait_ready(self, timeout: float | None = None) -> None:
        if self.info is None:
            self.info = self.load(self.workdir)
        timeout = self.startup_timeout if timeout is None else timeout
        grace = self.startup_grace if self.startup_grace is not None else 8.0 + self.nodes
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            if self._any_exited():
                raise ClusterError(f"a node exited before the cluster became ready\n{self._tail()}")
            if self._alive_any() is False:
                raise ClusterError("all nodes are gone before the cluster became ready\n" + self._tail())
            if time.monotonic() - self._t0 >= grace and self._ports_open():
                self.info.ready_at = iso_now()
                self._write_info()
                return
            time.sleep(0.2)

        raise ClusterError(f"cluster not ready within {timeout:.0f}s\n{self._tail()}")

    def stop(self, timeout: float = 5.0) -> None:
        if self.mode == "per-node":
            self._stop_per_node(timeout)
        else:
            self._stop_fork(timeout)

        pid_file = self.workdir / "cluster.pid"
        if pid_file.exists():
            pid_file.unlink()
        if self._log is not None:
            self._log.close()
            self._log = None

    def _stop_fork(self, timeout: float) -> None:
        parent = self._parent_pid()
        if parent and _alive(parent):
            self._signal_group(parent, signal.SIGTERM)
            end = time.monotonic() + timeout
            while time.monotonic() < end and _alive(parent):
                time.sleep(0.1)
            if _alive(parent):
                self._signal_group(parent, signal.SIGKILL)

        if self.proc is not None:
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        for pid in self._children(parent) if parent else []:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _stop_per_node(self, timeout: float) -> None:
        for proc in self.procs.values():
            if proc.poll() is None:
                self._signal_group(proc.pid, signal.SIGTERM)

        deadline = time.monotonic() + timeout
        for proc in self.procs.values():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._signal_group(proc.pid, signal.SIGKILL)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self.procs.clear()

    def restart(self, index: int | None = None, timeout: float = 5.0) -> ClusterInfo:
        if index is None:
            self.stop(timeout=timeout)
            time.sleep(0.5)
            return self.start()

        if self.mode != "per-node":
            raise ClusterError("single-node restart requires mode='per-node'")
        info = self.info or self.load(self.workdir)
        self.max_raft_state = info.max_raft_state
        node = next((item for item in info.nodes if item.index == index), None)
        if node is None:
            raise ClusterError(f"node{index} not found in cluster.json")

        self._stop_node(index)
        self._launch_node(index, node.ip, node.port)
        self._wait_port(node, timeout=self.startup_timeout)
        self._wait_node_status(index)

        node.pid = self.procs[index].pid
        self.info = info
        self._write_info()
        return info

    def _stop_node(self, index: int) -> None:
        proc = self.procs.get(index)
        pid = proc.pid if proc is not None else None
        if pid is None and self.info is not None:
            node = next((item for item in self.info.nodes if item.index == index), None)
            pid = node.pid if node is not None else None

        if pid and _alive(pid):
            self._signal_group(pid, signal.SIGTERM)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and _alive(pid):
                time.sleep(0.1)
            if _alive(pid):
                self._signal_group(pid, signal.SIGKILL)
        self.procs.pop(index, None)

    # ------------------------------------------------------------------ #
    # inspection
    # ------------------------------------------------------------------ #

    def status(self) -> dict:
        info = self.info or self.load(self.workdir)
        alive = [node for node in info.nodes if _alive(node.pid)]
        return {
            "workdir": str(self.workdir),
            "mode": info.mode,
            "running": len(alive) == len(info.nodes),
            "alive": len(alive),
            "total": len(info.nodes),
            "nodes": [node.to_dict() for node in info.nodes],
            "readyAt": info.ready_at,
        }

    @classmethod
    def load(cls, workdir: str | Path) -> ClusterInfo:
        workdir = Path(workdir).resolve()
        path = workdir / "cluster.json"
        if not path.exists():
            raise ClusterError(f"no cluster.json in {workdir}; run 'cluster up' first")
        data = json.loads(path.read_text(encoding="utf-8"))
        pid_file = workdir / "cluster.pid"
        parent = int(pid_file.read_text().strip()) if pid_file.exists() else None
        return ClusterInfo(
            config_file=data.get("configFile", "test.conf"),
            started_at=data["startedAt"],
            ready_at=data.get("readyAt"),
            mode=data.get("mode", "fork"),
            max_raft_state=data.get("maxRaftState", 500),
            nodes=[
                NodeInfo(
                    index=node["index"],
                    pid=node["pid"],
                    ip=node["ip"],
                    port=node["port"],
                    log_file=node.get("logFile", "cluster.log"),
                )
                for node in data["nodes"]
            ],
            parent_pid=parent,
        )

    @classmethod
    def attach(cls, workdir: str | Path) -> "Cluster":
        """Load an existing run without owning the process."""
        info = cls.load(workdir)
        obj = cls.__new__(cls)
        obj.nodes = len(info.nodes)
        obj.workdir = Path(workdir).resolve()
        obj.binary = bin_path("raftCoreRun")
        obj.max_raft_state = info.max_raft_state
        obj.startup_timeout = 30.0
        obj.startup_grace = None
        obj.mode = info.mode
        obj.proc = None
        obj.procs = {}
        obj.info = info
        obj._log = None
        obj._t0 = 0.0
        return obj

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _guard_running(self) -> None:
        info_path = self.workdir / "cluster.json"
        if info_path.exists():
            try:
                data = json.loads(info_path.read_text(encoding="utf-8"))
                alive = [node for node in data.get("nodes", []) if _alive(int(node.get("pid", 0)))]
            except (ValueError, OSError):
                alive = []
            if alive:
                raise ClusterError(
                    f"cluster already running in {self.workdir} ({len(alive)} node(s) alive); run 'cluster down' first"
                )

    def _wait_config(self, conf: Path) -> list[tuple[int, str, int]]:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if self._any_exited():
                raise ClusterError(f"a node exited during startup\n{self._tail()}")
            if conf.exists():
                entries = parse_test_conf(conf)
                if len(entries) >= self.nodes:
                    return entries[: self.nodes]
            time.sleep(0.1)
        raise ClusterError(f"nodes did not register in test.conf within {self.startup_timeout:.0f}s\n{self._tail()}")

    def _any_exited(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is not None
        return any(proc.poll() is not None for proc in self.procs.values())

    def _ports_open(self) -> bool:
        assert self.info is not None
        return all(self._port_open(node) for node in self.info.nodes)

    @staticmethod
    def _port_open(node: NodeInfo, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((node.ip, node.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_port(self, node: NodeInfo, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._port_open(node):
                return
            time.sleep(0.1)
        raise ClusterError(f"node{node.index} port {node.port} not listening within {timeout:.0f}s")

    def _wait_node_status(self, index: int) -> None:
        """Wait until the restarted node answers Status; tolerates missing kvstatus."""
        try:
            from .telemetry import query_status
        except ImportError:
            return

        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            try:
                statuses = query_status(self.workdir)
            except RuntimeError:
                return  # kvstatus 不可用时只保证端口
            for item in statuses:
                if item.get("index") == index and item.get("ok"):
                    return
            time.sleep(0.3)

    def _alive_any(self) -> bool:
        assert self.info is not None
        pids = [node.pid for node in self.info.nodes if node.pid > 0]
        if not pids:
            return True  # pid 未映射成功时不做误判
        return any(_alive(pid) for pid in pids)

    def _parent_pid(self) -> int | None:
        if self.info and self.info.parent_pid:
            return self.info.parent_pid
        pid_file = self.workdir / "cluster.pid"
        if pid_file.exists():
            try:
                return int(pid_file.read_text().strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _children(parent: int | None) -> list[int]:
        if not parent:
            return []
        try:
            output = subprocess.run(
                ["ps", "-o", "pid=", "--ppid", str(parent)],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return sorted(int(item) for item in output.split() if item.strip().isdigit())

    @staticmethod
    def _pids_by_port(ports: list[int]) -> dict[int, int]:
        mapping: dict[int, int] = {}
        try:
            output = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return mapping
        for line in output.splitlines():
            port_match = SS_PORT_RE.search(line)
            pid_match = SS_PID_RE.search(line)
            if port_match and pid_match:
                mapping.setdefault(int(port_match.group(1)), int(pid_match.group(1)))
        return {port: mapping[port] for port in ports if port in mapping}

    def _write_info(self) -> None:
        assert self.info is not None
        path = self.workdir / "cluster.json"
        tmp = self.workdir / "cluster.json.tmp"
        tmp.write_text(json.dumps(self.info.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _write_pid(self, pid: int) -> None:
        (self.workdir / "cluster.pid").write_text(f"{pid}\n", encoding="utf-8")

    def _tail(self, lines: int = 20) -> str:
        log_path = self.workdir / "cluster.log"
        if log_path.exists():
            content = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(content[-lines:])

        parts = []
        for node_log in sorted((self.workdir / "logs").glob("node*.log")):
            content = node_log.read_text(encoding="utf-8", errors="replace").splitlines()
            parts.append(f"--- {node_log.name} ---")
            parts.extend(content[-lines:])
        return "\n".join(parts)
