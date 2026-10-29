"""Locate the target KV project's binaries (raftCoreRun / kvbench / kvstatus).

Resolution order:
1. ``--target <path>`` (sets ``RAFTLAB_TARGET``)
2. ``RAFTLAB_TARGET`` environment variable
3. current working directory (run raftlab from the KV project root)
"""

from __future__ import annotations

import os
from pathlib import Path

ENV = "RAFTLAB_TARGET"


def target_root() -> Path | None:
    value = os.environ.get(ENV)
    return Path(value).expanduser().resolve() if value else None


def bin_path(name: str) -> Path:
    root = target_root()
    return (root / "bin" / name) if root is not None else (Path.cwd() / "bin" / name)


def hint() -> str:
    return "set --target <KV project> or RAFTLAB_TARGET, and build with: cmake --build build -j"
