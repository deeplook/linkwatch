import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from typer.testing import CliRunner

from linkwatch.chart import read_chart_events, render_chart
from linkwatch.cli import app


def write_events(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp_utc", "event", "endpoint"])
        writer.writerows((timestamp, event, "") for timestamp, event in rows)


def test_read_chart_events_sorts_and_normalizes_timestamps(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(
        log,
        [
            ("2026-01-01T01:00:00+02:00", "connected"),
            ("2025-12-31T22:30:00+00:00", "disconnected"),
        ],
    )

    events = read_chart_events(log)

    assert events == [
        (datetime(2025, 12, 31, 22, 30, tzinfo=timezone.utc), "disconnected"),
        (datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc), "connected"),
    ]


def test_fixed_end_time_makes_rendered_timeline_reproducible(tmp_path: Path):
    events = [
        (datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc), "disconnected"),
        (datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc), "connected"),
    ]
    end_time = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
    first = tmp_path / "first.svg"
    second = tmp_path / "second.svg"

    render_chart(events, first, end_time=end_time)
    render_chart(events, second, end_time=end_time)

    assert first.read_bytes() == second.read_bytes()
    assert "2026-01-01 01:00:00 UTC" in first.read_text(encoding="utf-8")


def make_seeded_events(seed: int) -> tuple[list[tuple[datetime, str]], datetime]:
    rng = random.Random(seed)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events: list[tuple[datetime, str]] = []

    for _ in range(3):
        now += timedelta(minutes=rng.randint(5, 90))
        events.append((now, "monitor_started"))
        online = bool(rng.getrandbits(1))
        for _ in range(8):
            now += timedelta(seconds=rng.randint(15, 900))
            online = not online
            events.append((now, "connected" if online else "disconnected"))
        now += timedelta(minutes=rng.randint(1, 30))
        events.append((now, "monitor_stopped"))

    now += timedelta(hours=2)
    return events, now


@pytest.mark.parametrize("seed", [7, 23, 104])
def test_seeded_synthetic_events_render_without_input_log(tmp_path: Path, seed: int):
    events, end_time = make_seeded_events(seed)
    output = tmp_path / f"synthetic-{seed}.svg"

    render_chart(events, output, end_time=end_time)

    svg = output.read_text(encoding="utf-8")
    root = ET.fromstring(svg)
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    title = root.find("svg:title", namespace)
    assert title is not None and title.text == "Internet connectivity history"
    assert svg.count('aria-label="Monitoring started"') == 3
    assert svg.count('aria-label="Graceful stop"') == 3
    assert "#2e9d59" in svg
    assert "#d64b4b" in svg
    assert end_time.strftime("%Y-%m-%d %H:%M:%S UTC") in svg


def test_render_chart_rejects_naive_end_time(tmp_path: Path):
    events = [(datetime(2026, 1, 1, tzinfo=timezone.utc), "connected")]

    with pytest.raises(ValueError, match="timezone-aware"):
        render_chart(events, tmp_path / "chart.svg", end_time=datetime(2026, 1, 1))


def test_chart_cli_accepts_fixed_end_time(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2025-12-31T22:00:00+00:00", "connected")])
    output = tmp_path / "timeline.svg"

    result = CliRunner().invoke(
        app,
        [
            "chart",
            "--log",
            str(log),
            "--output",
            str(output),
            "--end-time",
            "2026-01-01T01:00:00+02:00",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "2025-12-31 23:00:00 UTC" in output.read_text(encoding="utf-8")


def test_chart_cli_rejects_end_time_without_timezone(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2025-12-31T22:00:00+00:00", "connected")])
    output = tmp_path / "timeline.svg"

    result = CliRunner().invoke(
        app,
        ["chart", "--log", str(log), "--output", str(output), "--end-time", "2026-01-01T01:00:00"],
    )

    assert result.exit_code == 1
    assert "must include a timezone offset" in result.output
    assert not output.exists()
