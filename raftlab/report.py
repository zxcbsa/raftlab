"""Single-file HTML report (no external assets)."""

from __future__ import annotations

import html
import json
from pathlib import Path

from .evaluator import metrics_from_run_dir


def _json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def _esc(value) -> str:
    return html.escape(str(value))


def _status_class(status: str) -> str:
    return {"pass": "ok", "fail": "bad", "error": "warn"}.get(status, "warn")


def _cards(result: dict, verdict: dict, events: list[dict]) -> str:
    latency = result.get("latencyMs", {}).get("all", {})
    gap = next(
        (check["actual"] for check in verdict.get("checks", []) if check["metric"] == "availability.maxGapMs"), None
    )
    leader_changes = sum(1 for event in events if event.get("type") == "leader.change")
    items = [
        ("吞吐", f"{result.get('throughput', {}).get('total', 0):.0f} ops/s"),
        ("P50", f"{latency.get('p50', 0):.1f} ms"),
        ("P99", f"{latency.get('p99', 0):.1f} ms"),
        ("错误率", f"{result.get('errors', {}).get('rate', 0) * 100:.2f}%"),
        ("故障窗口", "—" if gap is None else f"{float(gap):.0f} ms"),
        ("换主次数", leader_changes),
    ]
    return "".join(
        f'<div class="card"><div class="label">{_esc(label)}</div><div class="value">{_esc(value)}</div></div>'
        for label, value in items
    )


def _latency_bars(result: dict) -> str:
    latency = result.get("latencyMs", {}).get("all")
    if not latency:
        return '<p class="muted">没有延迟数据</p>'
    keys = ["p50", "p90", "p95", "p99", "p999", "max"]
    peak = max(float(latency[key]) for key in keys) or 1.0
    rows = []
    for key in keys:
        value = float(latency[key])
        width = value / peak * 100.0
        rows.append(
            f'<div class="bar"><span class="k">{key}</span>'
            f'<div class="track"><div class="fill" style="width:{width:.1f}%"></div></div>'
            f'<b>{value:.1f} ms</b></div>'
        )
    return "".join(rows)


def _cpu_chart(samples: list[dict]) -> str:
    nodes = sorted({int(item["node"]) for item in samples if item.get("cpuPct") is not None})
    if not nodes:
        return '<p class="muted">没有 metrics 采样</p>'

    width, height, left, top, bottom = 760, 220, 44, 12, 28
    plot_w = width - left - 12
    plot_h = height - top - bottom
    max_t = max((item["tRelMs"] for item in samples), default=1) or 1
    max_v = max([item["cpuPct"] for item in samples] + [10.0])
    max_v = min(100.0, max(max_v, 10.0))

    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c"]
    lines = []
    for index, node in enumerate(nodes):
        points = []
        for item in samples:
            if int(item["node"]) != node:
                continue
            x = left + plot_w * item["tRelMs"] / max_t
            y = top + plot_h * (1.0 - min(float(item["cpuPct"]), max_v) / max_v)
            points.append(f"{x:.1f},{y:.1f}")
        if len(points) >= 2:
            lines.append(
                f'<polyline fill="none" stroke="{colors[index % len(colors)]}" stroke-width="1.6" '
                f'points="{" ".join(points)}"/>'
            )
        elif len(points) == 1:
            x, y = points[0].split(",")
            lines.append(f'<circle cx="{x}" cy="{y}" r="3" fill="{colors[index % len(colors)]}"/>')

    axis = (
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#d4d4d8"/>'
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#d4d4d8"/>'
        f'<text x="{left - 6}" y="{top + 4}" text-anchor="end" class="axis">{max_v:.0f}%</text>'
        f'<text x="{left - 6}" y="{top + plot_h}" text-anchor="end" class="axis">0</text>'
        f'<text x="{left}" y="{top + plot_h + 18}" class="axis">0s</text>'
        f'<text x="{left + plot_w}" y="{top + plot_h + 18}" text-anchor="end" class="axis">'
        f"{max_t / 1000:.0f}s</text>"
    )
    legend = "".join(
        f'<span class="dot" style="background:{colors[index % len(colors)]}"></span>node{node}&nbsp;&nbsp;'
        for index, node in enumerate(nodes)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="CPU usage">'
        f"{axis}{''.join(lines)}</svg><div class=\"legend\">{legend}</div>"
    )


def _checks_table(verdict: dict) -> str:
    checks = verdict.get("checks", [])
    if not checks:
        return '<p class="muted">没有断言</p>'
    rows = []
    for check in checks:
        mark = "✅" if check["passed"] else "❌"
        rows.append(
            "<tr>"
            f"<td>{mark}</td><td>{_esc(check['metric'])}</td>"
            f"<td class='num'>{check['actual']:.2f}</td><td>{_esc(check['op'])}</td>"
            f"<td class='num'>{check['threshold']:.2f}</td><td>{_esc(check['type'])}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th></th><th>指标</th><th>实际</th><th>比较</th><th>阈值</th><th>类型</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _events_table(events: list[dict]) -> str:
    if not events:
        return '<p class="muted">没有事件</p>'
    rows = []
    for event in events[-30:]:
        detail = json.dumps(event.get("detail", {}), ensure_ascii=False) if event.get("detail") else ""
        rows.append(
            "<tr>"
            f"<td class='num'>{(event.get('tRelMs', 0)) / 1000:.1f}s</td>"
            f"<td>{_esc(event.get('source', ''))}</td>"
            f"<td><code>{_esc(event.get('type', ''))}</code></td>"
            f"<td>{_esc(event.get('target', ''))}</td>"
            f"<td class='muted'>{_esc(detail)}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>时间</th><th>来源</th><th>事件</th><th>对象</th><th>detail</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _baseline_table(current: dict, baseline: dict, baseline_id: str) -> str:
    keys = ["throughput.total", "latency.p99", "errors.rate", "availability.maxGapMs"]
    rows = []
    for key in keys:
        if key not in current or key not in baseline:
            continue
        old = float(baseline[key])
        new = float(current[key])
        delta = ((new - old) / old * 100.0) if old else 0.0
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        rows.append(
            "<tr>"
            f"<td>{_esc(key)}</td><td class='num'>{old:.2f}</td><td class='num'>{new:.2f}</td>"
            f"<td class='num'>{arrow} {delta:+.1f}%</td></tr>"
        )
    if not rows:
        return '<p class="muted">基线缺少可比指标</p>'
    return (
        f"<p class='muted'>对比基线：{_esc(baseline_id)}</p>"
        "<table><thead><tr><th>指标</th><th>基线</th><th>本次</th><th>变化</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def build_report(run_dir: str | Path, baseline_dir: str | Path | None = None, out: str | Path | None = None) -> Path:
    run_dir = Path(run_dir).resolve()
    manifest = _json(run_dir / "manifest.json", {})
    verdict = _json(run_dir / "verdict.json", {})
    result = _json(run_dir / "result.json", {})
    events = _json(run_dir / "events.json", {}).get("events", [])
    samples = _jsonl(run_dir / "metrics.jsonl")

    baseline_metrics = None
    baseline_id = ""
    if baseline_dir is not None:
        baseline_metrics = metrics_from_run_dir(Path(baseline_dir))
        baseline_manifest = _json(Path(baseline_dir) / "manifest.json", {})
        baseline_id = baseline_manifest.get("runId", str(baseline_dir))

    status = verdict.get("status", "unknown")
    scenario = manifest.get("scenario", {}).get("name", "")
    build = manifest.get("build", {})
    cluster = manifest.get("cluster", {})

    baseline_section = ""
    if baseline_metrics is not None:
        baseline_section = (
            f"<section><h2>基线对比</h2>{_baseline_table(metrics_from_run_dir(run_dir), baseline_metrics, baseline_id)}</section>"
        )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(manifest.get('runId', run_dir.name))}</title>
<style>
:root {{ color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 32px; font: 14px/1.6 -apple-system, "Segoe UI", "Noto Sans SC", sans-serif;
       color: #18181b; background: #fafafa; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }}
h2 {{ font-size: 16px; margin: 0 0 12px; }}
section {{ background: #fff; border: 1px solid #e4e4e7; border-radius: 10px; padding: 18px 20px; margin-bottom: 16px; }}
.meta {{ color: #71717a; margin-bottom: 20px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-weight: 600; font-size: 12px; }}
.badge.ok {{ background: #dcfce7; color: #166534; }}
.badge.bad {{ background: #fee2e2; color: #991b1b; }}
.badge.warn {{ background: #fef3c7; color: #92400e; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }}
.card {{ background: #fafafa; border: 1px solid #e4e4e7; border-radius: 8px; padding: 12px; }}
.card .label {{ color: #71717a; font-size: 12px; }}
.card .value {{ font-size: 20px; font-weight: 600; margin-top: 4px; }}
.bar {{ display: grid; grid-template-columns: 56px 1fr 90px; gap: 10px; align-items: center; margin: 6px 0; }}
.bar .k {{ color: #52525b; font-size: 12px; }}
.track {{ background: #f4f4f5; border-radius: 4px; height: 14px; overflow: hidden; }}
.fill {{ background: #2563eb; height: 100%; }}
.bar b {{ text-align: right; font-variant-numeric: tabular-nums; font-weight: 500; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #f1f1f3; }}
th {{ color: #71717a; font-weight: 500; }}
td.num {{ font-variant-numeric: tabular-nums; }}
.muted {{ color: #a1a1aa; }}
.axis {{ fill: #a1a1aa; font-size: 11px; }}
.legend {{ margin-top: 6px; color: #52525b; font-size: 12px; }}
.dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }}
svg {{ width: 100%; height: auto; }}
code {{ background: #f4f4f5; padding: 1px 4px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<h1>{_esc(manifest.get('runId', run_dir.name))}
  <span class="badge {_status_class(status)}">{_esc(status)}</span></h1>
<div class="meta">
  场景 {_esc(scenario)} · seed {_esc(manifest.get('seed', '?'))} ·
  构建 {_esc(build.get('type', '?'))} ({_esc(build.get('gitCommit', '?'))}) ·
  节点 {_esc(cluster.get('nodes', '?'))} · maxRaftState {_esc(cluster.get('maxRaftState', '?'))}
</div>

<section><h2>关键指标</h2><div class="cards">{_cards(result, verdict, events)}</div></section>

<section><h2>延迟分布</h2>{_latency_bars(result)}</section>

<section><h2>CPU 使用率</h2>{_cpu_chart(samples)}</section>

<section><h2>断言</h2>{_checks_table(verdict)}</section>

{baseline_section}

<section><h2>事件时间线</h2>{_events_table(events)}</section>

<section><h2>产物</h2>
<p class="muted">manifest.json · cluster.json · result.json · events.json · metrics.jsonl · verdict.json</p>
</section>
</body>
</html>
"""
    path = Path(out) if out else run_dir / "report.html"
    path.write_text(page, encoding="utf-8")
    return path
