import csv
import re
from pathlib import Path
from xml.etree import ElementTree as ET

from typer.testing import CliRunner

from linkwatch.cli import app

runner = CliRunner()
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain_output(result):
    return ANSI_ESCAPE_RE.sub("", result.output)


def write_events(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp_utc", "event", "endpoint"])
        writer.writerows((timestamp, event, "") for timestamp, event in rows)


def test_linear_chart_svg_has_white_background(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2026-01-01T00:00:00+00:00", "disconnected")])
    output = tmp_path / "timeline.svg"

    result = runner.invoke(app, ["chart", "--log", str(log), "--output", str(output)])

    assert result.exit_code == 0, result.output
    svg = output.read_text(encoding="utf-8")
    assert '<rect x="0" y="0" width="1000" height="190" fill="white"' in svg


def test_calendar_svg_clips_midnight_but_counts_outage_only_on_start_date(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(
        log,
        [
            ("2026-01-05T23:30:00+00:00", "disconnected"),
            ("2026-01-06T01:15:00+00:00", "connected"),
        ],
    )
    output = tmp_path / "calendar.svg"

    result = runner.invoke(
        app,
        ["calendar", "--from", "2026-01-05", "--to", "2026-01-06", "--timezone", "UTC", "--log", str(log), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    svg = output.read_text(encoding="utf-8")
    assert "2026-01-05" in svg and "2026-01-06" in svg
    assert 'data-outage-count="1"' in svg
    assert 'data-outage-count="0"' in svg
    assert svg.count('fill="#d64b4b"') >= 2
    assert 'fill="white"' in svg
    assert 'class="month-title">January 2026</text>' in svg
    assert 'class="month-total">1 outage · 1h 45m in selected range</text>' in svg
    assert 'class="count">1</text>' in svg
    assert '.count { fill: #c62828; font-size: 14px; font-weight: 700; }' in svg

    root = ET.fromstring(svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    cells = {cell.attrib["data-date"]: cell for cell in root.findall('.//s:g[@data-date]', ns)}
    outage_day = cells["2026-01-05"]
    clear_day = cells["2026-01-06"]
    outage_count = outage_day.find('s:text[@class="count"]', ns)
    clear_count = clear_day.find('s:text[@class="count"]', ns)
    outage_border = outage_day.find("s:rect", ns)
    clear_border = clear_day.find("s:rect", ns)
    assert outage_count is not None and outage_count.text == "1"
    assert clear_count is None
    assert outage_border is not None and outage_border.attrib["stroke"] == "#c62828"
    assert clear_border is not None and clear_border.attrib["stroke"] == "#c62828"
    outage_duration = outage_day.find('s:text[@class="duration"]', ns)
    continuation_duration = clear_day.find('s:text[@class="duration"]', ns)
    assert outage_duration is not None and outage_duration.text == "Duration: 30m"
    assert continuation_duration is not None and continuation_duration.text == "Duration: 1h 15m"
    assert "1 outage · 1h 45m in selected range" in svg
    assert "W2: 1 outage · 1h 45m" in svg


def test_calendar_marks_monitored_days_without_outages_as_covered(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(
        log,
        [
            ("2001-01-05T10:00:00+00:00", "monitor_started"),
            ("2001-01-07T10:00:00+00:00", "monitor_stopped"),
        ],
    )
    output = tmp_path / "coverage.svg"
    result = runner.invoke(
        app,
        ["calendar", "--from", "2001-01-01", "--to", "2001-01-08", "--timezone", "UTC", "--log", str(log), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    root = ET.parse(output).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    cells = {cell.attrib["data-date"]: cell for cell in root.findall('.//s:g[@data-date]', ns)}
    for day in ("2001-01-05", "2001-01-06", "2001-01-07"):
        cell = cells[day]
        assert cell.attrib["data-covered"] == "true"
        badge = cell.find('s:rect[@class="coverage-badge"]', ns)
        number = cell.find('s:text[@class="covered-day-number"]', ns)
        assert badge is not None and badge.attrib["fill"] == "#000000"
        assert number is not None and number.text == str(int(day[-2:]))
        assert cell.find('s:text[@class="count"]', ns) is None
    for day in ("2001-01-04", "2001-01-08"):
        cell = cells[day]
        assert cell.attrib["data-covered"] == "false"
        assert cell.find('s:rect[@class="coverage-badge"]', ns) is None
        number = cell.find('s:text[@class="day"]', ns)
        assert number is not None and number.text == str(int(day[-2:]))


def test_calendar_caps_unmatched_monitor_session_at_last_log_event(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(
        log,
        [
            ("2001-01-05T10:00:00+00:00", "monitor_started"),
            ("2001-01-06T10:00:00+00:00", "connected"),
        ],
    )
    output = tmp_path / "open-session.svg"
    result = runner.invoke(
        app,
        ["calendar", "--from", "2001-01-05", "--to", "2001-01-07", "--timezone", "UTC", "--log", str(log), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    root = ET.parse(output).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    cells = {cell.attrib["data-date"]: cell for cell in root.findall('.//s:g[@data-date]', ns)}
    assert cells["2001-01-05"].attrib["data-covered"] == "true"
    assert cells["2001-01-06"].attrib["data-covered"] == "true"
    assert cells["2001-01-07"].attrib["data-covered"] == "false"


def test_calendar_command_help_exposes_timezone_and_date_range():
    result = runner.invoke(app, ["calendar", "--help"])
    assert result.exit_code == 0
    output = _plain_output(result)
    assert "--timezone" in output
    assert "local" in output.lower()
    assert "UTC" in output
    assert "--from" in output and "--to" in output


def test_calendar_pads_week_and_marks_future_cells_with_generation_time(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2099-04-08T10:00:00+00:00", "disconnected"), ("2099-04-08T11:00:00+00:00", "connected")])
    output = tmp_path / "future.svg"
    result = runner.invoke(
        app,
        ["calendar", "--from", "2099-04-08", "--to", "2099-04-08", "--timezone", "UTC", "--log", str(log), "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    root = ET.parse(output).getroot()
    ns = {"s": "http://www.w3.org/2000/svg"}
    cells = root.findall('.//s:g[@data-date]', ns)
    assert len(cells) == 7
    assert all("future" in cell.attrib["class"].split() for cell in cells)
    assert all(cell.find('s:text[@class="count"]', ns) is None for cell in cells)
    assert all(
        (rect := cell.find("s:rect", ns)) is not None and rect.attrib["fill"] == "#eeeeee"
        for cell in cells
    )
    assert any(text.text and text.text.startswith("Generated:") for text in root.findall('.//s:text', ns))


def test_calendar_closes_observed_outage_at_monitor_stop(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(
        log,
        [
            ("2026-01-05T23:30:00+00:00", "disconnected"),
            ("2026-01-06T00:00:00+00:00", "monitor_stopped"),
            ("2026-01-07T00:00:00+00:00", "monitor_started"),
        ],
    )
    output = tmp_path / "calendar.svg"

    result = runner.invoke(app, ["calendar", "--timezone", "UTC", "--log", str(log), "--output", str(output)])

    assert result.exit_code == 0, result.output
    svg = output.read_text(encoding="utf-8")
    assert 'class="stripe"' in svg
    assert 'class="stripe ongoing"' not in svg


def test_calendar_rejects_one_sided_or_reversed_date_ranges(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2026-01-01T00:00:00+00:00", "disconnected")])
    one_sided = runner.invoke(app, ["calendar", "--from", "2026-01-01", "--log", str(log)])
    reversed_range = runner.invoke(app, ["calendar", "--from", "2026-01-02", "--to", "2026-01-01", "--log", str(log)])

    assert one_sided.exit_code == 1
    assert "supplied together" in one_sided.output
    assert reversed_range.exit_code == 1
    assert "on or after" in reversed_range.output


def test_calendar_period_month_uses_latest_event_month(tmp_path: Path):
    log = tmp_path / "events.csv"
    write_events(log, [("2026-02-10T12:00:00+00:00", "disconnected"), ("2026-02-10T12:15:00+00:00", "connected")])
    result = runner.invoke(app, ["calendar", "--period", "month", "--timezone", "UTC", "--log", str(log), "--output", str(tmp_path / "month.svg")])

    assert result.exit_code == 0, result.output
    svg = (tmp_path / "month.svg").read_text(encoding="utf-8")
    assert "2026-02-01 — 2026-02-28" in svg
    root = ET.fromstring(svg)
    ns = {"s": "http://www.w3.org/2000/svg"}
    month_cells = root.findall('.//s:g[@data-date]', ns)
    month_days = {
        cell.attrib["data-date"]
        for cell in month_cells
        if cell.attrib["data-date"].startswith("2026-02-")
    }
    assert month_days == {f"2026-02-{day:02d}" for day in range(1, 29)}
    assert all(cell.attrib["data-date"].startswith("2026-02-") for cell in month_cells)
