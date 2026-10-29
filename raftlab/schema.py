"""Schema registry, JSON validation and cross-file semantic checks.

Every artifact produced by raftlab has a single source of truth: the JSON Schemas
under ``raftlab/schemas``.  This module validates documents against them and adds
the checks that JSON Schema cannot express (sums, ordering, cross-file invariants).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

RUN_FILES = [
    "manifest.json",
    "cluster.json",
    "result.json",
    "events.json",
    "verdict.json",
    "metrics.jsonl",
]

SINGLE_FILES = {
    "manifest.json": "manifest",
    "cluster.json": "cluster",
    "result.json": "result",
    "events.json": "events",
    "verdict.json": "verdict",
    "metrics.jsonl": "metrics",
}


class ValidationError(Exception):
    """Raised when validation cannot even start (bad path, missing dependency)."""


@dataclass
class Issue:
    file: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.file}: {self.path}: {self.message}"

    def to_dict(self) -> dict:
        return {"file": self.file, "path": self.path, "message": self.message}


@dataclass
class Result:
    target: str
    files: int = 0
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "ok": self.ok,
            "files": self.files,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _require(module: str, hint: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - environment specific
        raise ValidationError(f"missing python package '{module}': {hint}") from exc


_SCHEMA_CACHE: dict[str, dict] = {}


def _schema(kind: str) -> dict:
    if kind not in _SCHEMA_CACHE:
        path = SCHEMA_DIR / f"{kind}.schema.json"
        if not path.exists():
            raise ValidationError(f"schema not found: {path}")
        _SCHEMA_CACHE[kind] = json.loads(path.read_text(encoding="utf-8"))
    return _SCHEMA_CACHE[kind]


def _json_path(parts) -> str:
    out = "$"
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def _validate(kind: str, data: Any, source: str, issues: list[Issue]) -> None:
    jsonschema = _require("jsonschema", "sudo apt install python3-jsonschema")
    validator = jsonschema.Draft7Validator(_schema(kind), format_checker=jsonschema.FormatChecker())
    for error in sorted(validator.iter_errors(data), key=lambda err: list(err.absolute_path)):
        issues.append(Issue(source, _json_path(list(error.absolute_path)), error.message))


# --------------------------------------------------------------------------- #
# semantic checks
# --------------------------------------------------------------------------- #

def _semantic_scenario(data: dict, source: str, issues: list[Issue]) -> None:
    steps = data["spec"]["steps"]
    ids = [step["id"] for step in steps]

    duplicates = sorted({step_id for step_id in ids if ids.count(step_id) > 1})
    if duplicates:
        issues.append(Issue(source, "$.spec.steps", f"duplicate step id(s): {duplicates}"))

    if steps[0]["action"] != "cluster.up":
        issues.append(Issue(source, "$.spec.steps[0].action", "first step must be 'cluster.up'"))

    background: set[str] = set()
    for index, step in enumerate(steps):
        mix = step.get("mix")
        if mix is not None:
            total = sum(mix.values())
            if total != 100:
                issues.append(Issue(source, f"$.spec.steps[{index}].mix", f"mix must sum to 100, got {total}"))

        after = step.get("after")
        if isinstance(after, dict):
            ref = after.get("ref")
            if ref not in background:
                issues.append(
                    Issue(
                        source,
                        f"$.spec.steps[{index}].after.ref",
                        f"'{ref}' must reference an earlier step with background: true",
                    )
                )

        if step.get("background") and step["action"] != "workload.run":
            issues.append(
                Issue(source, f"$.spec.steps[{index}].background", "only workload.run may run in background")
            )
        if step.get("background"):
            background.add(step["id"])


def _check_counts(result: dict, source: str, issues: list[Issue]) -> None:
    counts = result["counts"]
    if counts["ok"] + counts["error"] != counts["total"]:
        issues.append(
            Issue(
                source,
                "$.counts",
                f"ok({counts['ok']}) + error({counts['error']}) != total({counts['total']})",
            )
        )


def _check_latency(result: dict, source: str, issues: list[Issue]) -> None:
    order = ["p50", "p90", "p95", "p99", "p999", "max"]
    for name, bucket in result["latencyMs"].items():
        values = [bucket[key] for key in order]
        if any(values[i] > values[i + 1] for i in range(len(values) - 1)):
            issues.append(
                Issue(source, f"$.latencyMs.{name}", f"percentiles must be non-decreasing, got {values}")
            )


def _check_verdict(verdict: dict, source: str, issues: list[Issue]) -> None:
    failed = sum(1 for check in verdict["checks"] if not check["passed"])
    if verdict["summary"]["failed"] != failed:
        issues.append(
            Issue(
                source,
                "$.summary.failed",
                f"summary says {verdict['summary']['failed']} failed, checks contain {failed}",
            )
        )
    if verdict["status"] == "pass" and failed:
        issues.append(Issue(source, "$.status", f"status is 'pass' but {failed} check(s) failed"))
    if verdict["status"] == "fail" and not failed:
        issues.append(Issue(source, "$.status", "status is 'fail' but no check failed"))


def _semantic_run(docs: dict[str, Any], source: str, issues: list[Issue]) -> None:
    run_ids = {
        name: doc.get("runId")
        for name, doc in docs.items()
        if isinstance(doc, dict) and "runId" in doc
    }
    if len(set(run_ids.values())) > 1:
        issues.append(Issue(source, "$", f"runId mismatch across artifacts: {run_ids}"))

    if isinstance(docs.get("result.json"), dict):
        result = docs["result.json"]
        _check_counts(result, "result.json", issues)
        _check_latency(result, "result.json", issues)

    if isinstance(docs.get("verdict.json"), dict):
        _check_verdict(docs["verdict.json"], "verdict.json", issues)

    if isinstance(docs.get("events.json"), dict):
        times = [event["tRelMs"] for event in docs["events.json"]["events"]]
        if any(times[i] > times[i + 1] for i in range(len(times) - 1)):
            issues.append(Issue("events.json", "$.events", "events must be sorted by tRelMs"))

    samples = docs.get("metrics.jsonl")
    manifest = docs.get("manifest.json")
    if isinstance(samples, list) and isinstance(manifest, dict):
        nodes = manifest["cluster"]["nodes"]
        for index, sample in enumerate(samples):
            if not 0 <= sample["node"] < nodes:
                issues.append(
                    Issue(
                        f"metrics.jsonl:{index + 1}",
                        "$.node",
                        f"node index {sample['node']} out of range 0..{nodes - 1}",
                    )
                )


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[Any]:
    samples = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path.name}:{line_no}: invalid JSON: {exc}") from exc
    return samples


def _validate_single(path: Path) -> Result:
    result = Result(target=str(path))
    issues: list[Issue] = []

    if path.suffix in (".yaml", ".yml"):
        yaml = _require("yaml", "sudo apt install python3-yaml")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValidationError(f"{path.name}: invalid YAML: {exc}") from exc
        _validate("scenario", data, path.name, issues)
        if not issues:
            _semantic_scenario(data, path.name, issues)
        result.files = 1
        result.issues = issues
        return result

    if path.name not in SINGLE_FILES:
        raise ValidationError(
            f"cannot infer artifact type from '{path.name}'; "
            f"expected a scenario (*.yaml) or one of {sorted(SINGLE_FILES)}"
        )

    kind = SINGLE_FILES[path.name]
    if kind == "metrics":
        samples = _read_jsonl(path)
        for index, sample in enumerate(samples):
            _validate("metrics", sample, f"{path.name}:{index + 1}", issues)
    else:
        try:
            data = _read_json(path)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{path.name}: invalid JSON: {exc}") from exc
        _validate(kind, data, path.name, issues)
        if kind == "result" and not issues:
            _check_counts(data, path.name, issues)
            _check_latency(data, path.name, issues)
        if kind == "verdict" and not issues:
            _check_verdict(data, path.name, issues)

    result.files = 1
    result.issues = issues
    return result


def _validate_run(run_dir: Path, allow_missing: bool) -> Result:
    result = Result(target=str(run_dir))
    docs: dict[str, Any] = {}

    for name in RUN_FILES:
        path = run_dir / name
        if not path.exists():
            if not allow_missing:
                result.issues.append(Issue(name, "$", "missing artifact (use --allow-missing to ignore)"))
            continue

        result.files += 1
        try:
            if name == "metrics.jsonl":
                samples = _read_jsonl(path)
                docs[name] = samples
                for index, sample in enumerate(samples):
                    _validate("metrics", sample, f"{name}:{index + 1}", result.issues)
            else:
                data = _read_json(path)
                docs[name] = data
                _validate(SINGLE_FILES[name], data, name, result.issues)
        except json.JSONDecodeError as exc:
            result.issues.append(Issue(name, "$", f"invalid JSON: {exc}"))

    _semantic_run(docs, str(run_dir), result.issues)
    return result


def load_and_validate_path(path: Path, allow_missing: bool = False) -> Result:
    if not path.exists():
        raise ValidationError(f"path not found: {path}")
    return _validate_run(path, allow_missing) if path.is_dir() else _validate_single(path)
