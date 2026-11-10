"""Parsing and SVG rendering for connectivity timeline charts."""

from __future__ import annotations

import csv
import html
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

ChartEvent = tuple[datetime, str]


def read_chart_events(log_path: Path) -> list[ChartEvent]:
    """Read, validate, and UTC-sort events from a Link Watch CSV log."""
    if not log_path.is_file():
        raise FileNotFoundError(f"log file not found: {log_path}")

    connectivity_events = {"connected", "disconnected"}
    lifecycle_events = {"monitor_started", "monitor_stopped"}
    accepted_events = connectivity_events | lifecycle_events
    events: list[ChartEvent] = []
    with log_path.open(newline="", encoding="utf-8") as log_file:
        for row in csv.DictReader(log_file):
            try:
                timestamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
                event = row["event"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid event row in {log_path}: {row}") from error
            if timestamp.tzinfo is None or event not in accepted_events:
                raise ValueError(f"invalid event row in {log_path}: {row}")
            events.append((timestamp.astimezone(timezone.utc), event))

    if not events:
        raise ValueError(f"no connectivity events in log: {log_path}")
    events.sort(key=lambda item: item[0])
    return events


def render_chart(
    events: Sequence[ChartEvent],
    output_path: Path,
    *,
    end_time: datetime | None = None,
) -> None:
    """Render validated events as an SVG timeline.

    Pass a fixed, timezone-aware ``end_time`` to make repeated renders of the
    same events byte-for-byte reproducible. When omitted, the current UTC time
    is used, extending to the latest event if that event is in the future.
    """
    if not events:
        raise ValueError("no connectivity events to render")

    normalized_events: list[ChartEvent] = []
    for timestamp, event in events:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("chart event timestamps must be timezone-aware")
        normalized_events.append((timestamp.astimezone(timezone.utc), event))
    normalized_events.sort(key=lambda item: item[0])

    if end_time is not None:
        if end_time.tzinfo is None or end_time.utcoffset() is None:
            raise ValueError("end_time must be timezone-aware")
        requested_end = end_time.astimezone(timezone.utc)
    else:
        requested_end = datetime.now(timezone.utc)

    connectivity_events = {"connected", "disconnected"}
    lifecycle_labels = {
        "monitor_started": "Monitoring started",
        "monitor_stopped": "Graceful stop",
    }
    start = normalized_events[0][0]
    end = max(requested_end, normalized_events[-1][0])
    total_seconds = max((end - start).total_seconds(), 1.0)
    left, width = 90.0, 820.0
    bar_y, bar_height = 82.0, 48.0

    def x_for(timestamp: datetime) -> float:
        return left + (timestamp - start).total_seconds() / total_seconds * width

    current_state: bool | None = None
    monitoring = True
    cursor = start
    segments: list[str] = []
    markers: list[str] = []
    has_lifecycle_events = any(event in lifecycle_labels for _, event in normalized_events)

    for timestamp, event in normalized_events:
        x = x_for(timestamp)
        x0 = x_for(cursor)
        if monitoring and current_state is not None and x > x0:
            color = "#2e9d59" if current_state else "#d64b4b"
            segments.append(
                f'<rect x="{x0:.2f}" y="{bar_y}" width="{x - x0:.2f}" '
                f'height="{bar_height}" fill="{color}"/>'
            )
        cursor = timestamp

        if event in lifecycle_labels:
            label = lifecycle_labels[event]
            marker_color = "#2864dc" if event == "monitor_started" else "#8050a0"
            timestamp_label = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
            icon = (
                f'<path d="M{x - 4:.2f} 76 l8 5 -8 5 Z" fill="{marker_color}"/>'
                if event == "monitor_started"
                else f'<rect x="{x - 4:.2f}" y="77" width="8" height="8" fill="{marker_color}"/>'
            )
            markers.append(
                f'<g aria-label="{label}"><title>{label}: {html.escape(timestamp_label)}</title>'
                f'<line x1="{x:.2f}" y1="74" x2="{x:.2f}" y2="134" '
                f'stroke="{marker_color}" stroke-width="1.5" stroke-dasharray="4 3"/>'
                f'{icon}</g>'
            )
            if event == "monitor_started":
                monitoring = True
            else:
                monitoring = False
            current_state = None
        elif event in connectivity_events:
            current_state = event == "connected"

    x0 = x_for(cursor)
    if monitoring and current_state is not None and left + width > x0:
        color = "#2e9d59" if current_state else "#d64b4b"
        segments.append(
            f'<rect x="{x0:.2f}" y="{bar_y}" width="{left + width - x0:.2f}" '
            f'height="{bar_height}" fill="{color}"/>'
        )

    start_label = start.strftime("%Y-%m-%d %H:%M:%S UTC")
    end_label = end.strftime("%Y-%m-%d %H:%M:%S UTC")
    lifecycle_legend = ""
    unknown_legend = ""
    if has_lifecycle_events:
        lifecycle_legend = (
            '<path d="M540 23 l7 5 -7 5 Z" fill="#2864dc"/>'
            '<text x="552" y="33" class="small">Monitor start</text>'
            '<rect x="675" y="25" width="8" height="8" fill="#8050a0"/>'
            '<text x="690" y="33" class="small">Graceful stop</text>'
        )
        unknown_legend = (
            '<rect x="205" y="142" width="10" height="10" fill="#eeeeee" stroke="#888"/>'
            '<text x="221" y="151" class="small">Not monitored / unknown</text>'
        )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="190" viewBox="0 0 1000 190">
  <title>Internet connectivity history</title>
  <style>text {{ font-family: sans-serif; fill: #222; }} .small {{ font-size: 13px; }}</style>
  <rect x="0" y="0" width="1000" height="190" fill="white"/>
  <text x="90" y="32" font-size="20">Internet connectivity</text>
  {lifecycle_legend}
  <text x="90" y="64" class="small">Online</text><text x="90" y="151" class="small">Offline</text>
  {unknown_legend}
  <rect x="90" y="82" width="820" height="48" fill="#eeeeee"/>
  {''.join(segments)}
  {''.join(markers)}
  <text x="90" y="174" class="small">{html.escape(start_label)}</text>
  <text x="910" y="174" class="small" text-anchor="end">{html.escape(end_label)}</text>
</svg>
'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")


def generate_chart(
    log_path: Path,
    output_path: Path,
    *,
    end_time: datetime | None = None,
) -> None:
    """Read a Link Watch log and render its timeline to an SVG file."""
    render_chart(read_chart_events(log_path), output_path, end_time=end_time)
