import random
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from linkwatch.calendar_chart import render_calendar_chart

START_DATE = date(2026, 2, 1)
END_DATE = date(2026, 2, 21)
GENERATED_AT = datetime(2026, 2, 22, tzinfo=timezone.utc)


def make_seeded_calendar_events(seed: int) -> list[tuple[datetime, str]]:
    rng = random.Random(seed)
    events: list[tuple[datetime, str]] = []

    for offset in range((END_DATE - START_DATE).days + 1):
        day = START_DATE + timedelta(days=offset)
        cursor = datetime.combine(day, time.min, tzinfo=timezone.utc) + timedelta(
            minutes=rng.randint(15, 120)
        )
        events.append((cursor, "monitor_started"))
        outage_count = rng.randint(1, 3)
        for _ in range(outage_count):
            cursor += timedelta(minutes=rng.randint(5, 45))
            events.append((cursor, "disconnected"))
            cursor += timedelta(minutes=rng.randint(5, 90))
            events.append((cursor, "connected"))
        cursor += timedelta(minutes=rng.randint(1, 20))
        events.append((cursor, "monitor_stopped"))

    return events


@pytest.mark.parametrize("seed", [11, 37, 84])
def test_seeded_calendar_data_renders_reproducibly_without_csv(tmp_path: Path, seed: int):
    events = make_seeded_calendar_events(seed)
    first = tmp_path / f"calendar-{seed}-a.svg"
    second = tmp_path / f"calendar-{seed}-b.svg"

    render_calendar_chart(
        events,
        first,
        start_date=START_DATE,
        end_date=END_DATE,
        timezone_name="UTC",
        generated_at=GENERATED_AT,
    )
    render_calendar_chart(
        events,
        second,
        start_date=START_DATE,
        end_date=END_DATE,
        timezone_name="UTC",
        generated_at=GENERATED_AT,
    )

    assert first.read_bytes() == second.read_bytes()
    root = ET.parse(first).getroot()
    namespace = {"svg": "http://www.w3.org/2000/svg"}
    cells = {
        cell.attrib["data-date"]: cell
        for cell in root.findall('.//svg:g[@data-date]', namespace)
        if START_DATE.isoformat() <= cell.attrib["data-date"] <= END_DATE.isoformat()
    }
    expected_days = {
        (START_DATE + timedelta(days=offset)).isoformat()
        for offset in range((END_DATE - START_DATE).days + 1)
    }
    assert set(cells) == expected_days

    expected_outages = Counter(
        timestamp.date().isoformat()
        for timestamp, event in events
        if event == "disconnected"
    )
    for day, cell in cells.items():
        assert cell.attrib["data-covered"] == "true"
        assert int(cell.attrib["data-outage-count"]) == expected_outages[day]
        assert float(cell.attrib["data-outage-duration-seconds"]) > 0
    assert root.find("svg:title", namespace) is not None
    assert b"Generated: 2026-02-22T00:00:00+00:00 (UTC)" in first.read_bytes()
