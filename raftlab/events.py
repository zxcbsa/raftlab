"""Event timeline (events.json)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .cluster import iso_now


class EventLog:
    def __init__(self, run_id: str, path: str | Path, start_monotonic: float | None = None) -> None:
        self.run_id = run_id
        self.path = Path(path)
        self.t0 = time.monotonic() if start_monotonic is None else start_monotonic
        self.events: list[dict] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.events = data.get("events", [])
            except (json.JSONDecodeError, OSError):
                self.events = []

    def add(self, source: str, type: str, target: str, **detail) -> dict:
        event = {
            "ts": iso_now(),
            "tRelMs": int((time.monotonic() - self.t0) * 1000),
            "source": source,
            "type": type,
            "target": target,
        }
        if detail:
            event["detail"] = detail
        self.events.append(event)
        return event

    def save(self) -> Path:
        payload = {"schemaVersion": "1.0", "runId": self.run_id, "events": self.events}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)
        return self.path
