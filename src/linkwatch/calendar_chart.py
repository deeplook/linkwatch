"""Calendar SVG rendering for connectivity outage events."""

from __future__ import annotations

import csv
import html
import os
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _read_events(log_path: Path) -> list[tuple[datetime, str]]:
    if not log_path.is_file():
        raise FileNotFoundError(f"log file not found: {log_path}")
    events: list[tuple[datetime, str]] = []
    accepted = {"connected", "disconnected", "monitor_started", "monitor_stopped"}
    with log_path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            try:
                timestamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
                event = row["event"]
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid event row in {log_path}: {row}") from error
            if timestamp.tzinfo is None or event not in accepted:
                raise ValueError(f"invalid event row in {log_path}: {row}")
            events.append((timestamp.astimezone(timezone.utc), event))
    if not events:
        raise ValueError(f"no connectivity events in log: {log_path}")
    return sorted(events)


def _make_intervals(events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime | None]]:
    intervals: list[tuple[datetime, datetime | None]] = []
    open_start: datetime | None = None
    for timestamp, event in events:
        if event in {"monitor_started", "monitor_stopped"}:
            if open_start is not None:
                intervals[-1] = (open_start, timestamp)
                open_start = None
        elif event == "disconnected" and open_start is None:
            open_start = timestamp
            intervals.append((timestamp, None))
        elif event == "connected" and open_start is not None:
            intervals[-1] = (open_start, timestamp)
            open_start = None
    return intervals


def _make_monitoring_intervals(
    events: list[tuple[datetime, str]], observed_until: datetime
) -> list[tuple[datetime, datetime]]:
    """Return logged monitor sessions; cap an unmatched start at the last observed event."""
    intervals: list[tuple[datetime, datetime]] = []
    open_start: datetime | None = None
    for timestamp, event in events:
        if event == "monitor_started" and open_start is None:
            open_start = timestamp
        elif event == "monitor_stopped" and open_start is not None:
            if open_start < timestamp:
                intervals.append((open_start, timestamp))
            open_start = None
    latest_observation = max(
        (timestamp for timestamp, _ in events if timestamp <= observed_until),
        default=observed_until,
    )
    if open_start is not None and open_start < latest_observation:
        intervals.append((open_start, latest_observation))
    return intervals


def _covered_dates(
    events: list[tuple[datetime, str]],
    zone: tzinfo,
    first_date: date,
    last_date: date,
    observed_until: datetime,
) -> set[date]:
    """Mark event dates and local dates overlapped by recorded monitor sessions."""
    covered = {
        timestamp.astimezone(zone).date()
        for timestamp, _ in events
        if timestamp <= observed_until
        and first_date <= timestamp.astimezone(zone).date() <= last_date
    }
    sessions = _make_monitoring_intervals(events, observed_until)
    for offset in range((last_date - first_date).days + 1):
        day = first_date + timedelta(days=offset)
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        if any(start < day_end and end > day_start for start, end in sessions):
            covered.add(day)
    return covered


def _plural_suffix(count: int) -> str:
    return "" if count == 1 else "s"


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    parts = [f"{days}d"] if days else []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and (not days):
        parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


def _has_open_outage(events: list[tuple[datetime, str]]) -> bool:
    intervals = _make_intervals(events)
    return bool(intervals and intervals[-1][1] is None)


def _period_range(
    events: list[tuple[datetime, str]], zone: tzinfo, start_date: date | None, end_date: date | None, period: str | None,
    now: datetime,
) -> tuple[date, date]:
    if period:
        if start_date is not None or end_date is not None:
            raise ValueError("--period cannot be combined with --from/--to")
        anchor = max(timestamp.astimezone(zone).date() for timestamp, _ in events)
        if _has_open_outage(events):
            anchor = max(anchor, now.date())
        if period == "day":
            return anchor, anchor
        if period == "week":
            monday = anchor - timedelta(days=anchor.weekday())
            return monday, monday + timedelta(days=6)
        if period in {"month", "3-months"}:
            month_index = anchor.year * 12 + anchor.month - 1
            start_index = month_index - (2 if period == "3-months" else 0)
            year, month0 = divmod(start_index, 12)
            first = date(year, month0 + 1, 1)
            next_index = month_index + 1
            end_year, end_month0 = divmod(next_index, 12)
            last = date(end_year, end_month0 + 1, 1) - timedelta(days=1)
            return first, last
        if period == "year":
            return date(anchor.year, 1, 1), date(anchor.year, 12, 31)
        raise ValueError("--period must be day, week, month, 3-months, or year")
    if (start_date is None) != (end_date is None):
        raise ValueError("--from and --to must be supplied together")
    if start_date and end_date:
        if end_date < start_date:
            raise ValueError("--to must be on or after --from")
        return start_date, end_date
    local_dates = [timestamp.astimezone(zone).date() for timestamp, _ in events]
    if _has_open_outage(events):
        local_dates.append(now.date())
    return min(local_dates), max(local_dates)


def _local_timezone() -> tzinfo:
    """Load the host's full local timezone rules, including DST when available."""
    configured = os.environ.get("TZ", "").lstrip(":")
    if configured:
        try:
            return ZoneInfo(configured)
        except (KeyError, ValueError):
            pass
    try:
        with Path("/etc/localtime").open("rb") as timezone_file:
            return ZoneInfo.from_file(timezone_file, key="local")
    except OSError:
        return datetime.now().astimezone().tzinfo or timezone.utc


def generate_calendar_chart(
    log_path: Path,
    output_path: Path,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    period: str | None = None,
    timezone_name: str = "local",
) -> None:
    """Render daily outage intervals and start counts as a week-grid SVG."""
    if timezone_name.lower() == "local":
        zone = _local_timezone()
        zone_label = "Local time"
    elif timezone_name.upper() == "UTC":
        zone = timezone.utc
        zone_label = "UTC"
    else:
        try:
            zone = ZoneInfo(timezone_name)
        except (KeyError, ValueError) as error:
            raise ValueError(f"unknown timezone: {timezone_name}") from error
        zone_label = timezone_name

    generated_at = datetime.now(zone)
    generated_utc = generated_at.astimezone(timezone.utc)
    events = _read_events(log_path)
    start_date, end_date = _period_range(events, zone, start_date, end_date, period, generated_at)
    range_start = datetime.combine(start_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    range_end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
    known_range_end = min(range_end, generated_utc)
    intervals = _make_intervals(events)
    starts = Counter(
        local_start for start, _ in intervals
        if start <= generated_utc and start_date <= (local_start := start.astimezone(zone).date()) <= end_date
    )
    rows = (end_date - start_date).days + 1
    daily_durations: dict[date, float] = {}
    for offset in range(rows):
        day = start_date + timedelta(days=offset)
        day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
        day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        for outage_start, outage_end in intervals:
            clipped_start = max(outage_start, day_start, range_start)
            clipped_end = min(outage_end or known_range_end, day_end, range_end, known_range_end)
            if clipped_start < clipped_end:
                daily_durations[day] = daily_durations.get(day, 0.0) + (clipped_end - clipped_start).total_seconds()
    total_count = sum(starts.values())
    total_duration = sum(daily_durations.values())
    first_monday = start_date - timedelta(days=start_date.weekday())
    last_sunday = end_date + timedelta(days=6 - end_date.weekday())
    covered_dates = _covered_dates(events, zone, first_monday, last_sunday, generated_utc)
    week_rows = ((last_sunday - first_monday).days + 1) // 7

    month_counts = Counter()
    year_counts = Counter()
    month_durations: dict[tuple[int, int], float] = {}
    year_durations: dict[int, float] = {}
    for day, count in starts.items():
        month_counts[(day.year, day.month)] += count
        year_counts[day.year] += count
    for day, duration in daily_durations.items():
        key = (day.year, day.month)
        month_durations[key] = month_durations.get(key, 0.0) + duration
        year_durations[day.year] = year_durations.get(day.year, 0.0) + duration
    month_cursor = date(start_date.year, start_date.month, 1)
    final_month = date(end_date.year, end_date.month, 1)
    while month_cursor <= final_month:
        month_counts.setdefault((month_cursor.year, month_cursor.month), 0)
        month_cursor = date(month_cursor.year + (month_cursor.month == 12), month_cursor.month % 12 + 1, 1)
    for year in range(start_date.year, end_date.year + 1):
        year_counts.setdefault(year, 0)

    month_items = sorted(month_counts.items())
    year_summary = " · ".join(
        f"{year}: {count} outage{_plural_suffix(count)} · {_format_duration(year_durations.get(year, 0.0))}"
        for year, count in sorted(year_counts.items())
    )
    one_month = len(month_items) == 1
    month_summary_rows = (len(month_items) + 2) // 3
    grid_top = 150 if one_month else 128 + month_summary_rows * 20
    margin, cell_w, cell_h = 70, 122, 94
    height = grid_top + week_rows * (cell_h + 20) + 10

    body: list[str] = []
    for weekday, label in enumerate(WEEKDAYS):
        x = margin + weekday * cell_w
        body.append(f'<text x="{x + 4}" y="{grid_top - 12}" class="weekday">{label}</text>')

    weeks: list[int] = []
    for week_index in range(week_rows):
        week_start = first_monday + timedelta(days=week_index * 7)
        week_count = sum(
            starts[week_start + timedelta(days=i)]
            for i in range(7)
            if start_date <= week_start + timedelta(days=i) <= end_date
        )
        week_duration = sum(
            daily_durations.get(week_start + timedelta(days=i), 0.0)
            for i in range(7)
            if start_date <= week_start + timedelta(days=i) <= end_date
        )
        weeks.append(week_count)
        for weekday in range(7):
            day = week_start + timedelta(days=weekday)
            if period in {"month", "3-months", "year"} and not start_date <= day <= end_date:
                continue
            x = margin + weekday * cell_w
            y = grid_top + week_index * (cell_h + 20)
            day_count = starts[day]
            outage_duration_seconds = daily_durations.get(day, 0.0)
            future = day > generated_at.date()
            outside_range = not start_date <= day <= end_date
            cell_class = "future" if future else "outside-range" if outside_range else ""
            cell_fill = "#eeeeee" if future else "#fafafa" if outside_range else "#ffffff"
            covered = day in covered_dates and not future
            day_number = (
                f'<rect x="{x + 3}" y="{y + 2}" width="25" height="22" rx="2" fill="#000000" class="coverage-badge"/>'
                f'<text x="{x + 15.5}" y="{y + 18}" text-anchor="middle" class="covered-day-number">{day.day}</text>'
                if covered else f'<text x="{x + 5}" y="{y + 15}" class="day">{day.day}</text>'
            )
            border_color = "#c62828" if outage_duration_seconds > 0 else "#c8c8c8"
            border_width = "2" if outage_duration_seconds > 0 else "1"
            count_label = (
                f'<text x="{x + cell_w - 12}" y="{y + 17}" text-anchor="end" class="count">{day_count}</text>'
                if day_count > 0 else ""
            )
            duration_label = (
                f'<text x="{x + 5}" y="{y + 82}" class="duration">Duration: {_format_duration(outage_duration_seconds)}</text>'
                if outage_duration_seconds > 0 else ""
            )
            body.append(
                f'<g data-date="{day.isoformat()}" data-covered="{str(covered).lower()}" data-outage-count="{day_count}" '
                f'data-outage-duration-seconds="{outage_duration_seconds:.3f}" class="{cell_class}">'
                f'<rect x="{x}" y="{y}" width="{cell_w - 5}" height="{cell_h - 5}" '
                f'rx="3" fill="{cell_fill}" stroke="{border_color}" stroke-width="{border_width}"/>'
                f'{day_number}'
                f'{count_label}'
                f'<line x1="{x + 5}" y1="{y + 31}" x2="{x + cell_w - 10}" y2="{y + 31}" stroke="#aaa"/>'
                f'<text x="{x + 5}" y="{y + 44}" class="tick">00</text>'
                f'<text x="{x + cell_w / 2}" y="{y + 44}" text-anchor="middle" class="tick">12</text>'
                f'<text x="{x + cell_w - 10}" y="{y + 44}" text-anchor="end" class="tick">24</text>'
            )
            day_start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
            day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
            day_duration = (day_end - day_start).total_seconds()
            track_left, track_width = x + 5, cell_w - 15
            for outage_start, outage_end in intervals:
                clipped_start = max(outage_start, day_start, range_start)
                clipped_end = min(outage_end or known_range_end, day_end, range_end, known_range_end)
                if clipped_start >= clipped_end:
                    continue
                offset = (clipped_start - day_start).total_seconds() / day_duration
                fraction = (clipped_end - clipped_start).total_seconds() / day_duration
                stripe_x = track_left + offset * track_width
                stripe_w = max(1.0, fraction * track_width)
                ongoing = " ongoing" if outage_end is None else ""
                body.append(
                    f'<rect x="{stripe_x:.2f}" y="{y + 55}" width="{stripe_w:.2f}" height="12" '
                    f'rx="2" fill="#d64b4b" data-outage="true" class="stripe{ongoing}"/>'
                )
            body.append(duration_label)
            body.append("</g>")
        row_y = grid_top + week_index * (cell_h + 20) + cell_h + 14
        week_total_class = "week-total" if week_count or week_duration else "week-total empty"
        body.append(
            f'<text x="{margin + 7 * cell_w}" y="{row_y}" text-anchor="end" class="{week_total_class}">'
            f'W{week_start.isocalendar().week}: {week_count} outage{_plural_suffix(week_count)} · '
            f'{_format_duration(week_duration)}</text>'
        )

    generated_label = f'Generated: {generated_at.isoformat(timespec="seconds")} ({html.escape(zone_label)})'
    if one_month:
        (year, month), total = month_items[0]
        first_day = date(year, month, 1)
        next_month = date(year + (month == 12), month % 12 + 1, 1)
        total_label = "this month" if start_date == first_day and end_date == next_month - timedelta(days=1) else "in selected range"
        month_header = (
            f'<text x="{margin}" y="72" class="month-title">{first_day.strftime("%B %Y")}</text>'
            f'<text x="930" y="72" text-anchor="end" class="month-total">'
            f'{total} outage{_plural_suffix(total)} · {_format_duration(month_durations.get((year, month), 0.0))} {total_label}</text>'
            f'<text x="{margin}" y="96" class="small">{start_date.isoformat()} — {end_date.isoformat()} · '
            f'{html.escape(zone_label)}</text>'
            f'<text x="{margin}" y="116" class="small">{generated_label}</text>'
        )
    else:
        summary_elements = []
        for index, ((year, month), total) in enumerate(month_items):
            column, row = index % 3, index // 3
            x = margin + column * 300
            y = 78 + row * 20
            summary_elements.append(
                f'<text x="{x}" y="{y}" class="month-label">{date(year, month, 1).strftime("%B %Y")}</text>'
                f'<text x="{x + 270}" y="{y}" text-anchor="end" class="month-total-small">'
                f'{total} outage{_plural_suffix(total)} · {_format_duration(month_durations.get((year, month), 0.0))}</text>'
            )
        month_header = (
            f'<text x="{margin}" y="52" class="small">{start_date.isoformat()} — {end_date.isoformat()} · {html.escape(zone_label)}</text>'
            f'<text x="990" y="52" text-anchor="end" class="range-total">Selected total: '
            f'{total_count} outage{_plural_suffix(total_count)} · {_format_duration(total_duration)}</text>'
            + "".join(summary_elements)
            + f'<text x="{margin}" y="{78 + month_summary_rows * 20}" class="small">Year subtotals (selected range): {html.escape(year_summary)}</text>'
            + f'<text x="{margin}" y="{98 + month_summary_rows * 20}" class="small">{generated_label}</text>'
        )

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="{height}" viewBox="0 0 1000 {height}">
  <title>Connectivity outage calendar</title>
  <style>text {{ font-family: sans-serif; fill: #222; }} .title {{ font-size: 20px; font-weight: 700; }} .small {{ font-size: 12px; }} .month-title {{ font-size: 24px; font-weight: 700; }} .month-total {{ fill: #c62828; font-size: 20px; font-weight: 700; }} .month-label {{ font-size: 14px; font-weight: 700; }} .month-total-small {{ fill: #c62828; font-size: 14px; font-weight: 700; }} .range-total {{ fill: #c62828; font-size: 12px; font-weight: 700; }} .weekday {{ font-size: 13px; font-weight: bold; }} .day {{ font-size: 14px; font-weight: bold; }} .covered-day-number {{ fill: #ffffff; font-size: 14px; font-weight: 700; }} .future .covered-day-number, .outside-range .covered-day-number {{ fill: #ffffff; }} .duration {{ fill: #444; font-size: 10px; }} .future text, .outside-range text {{ fill: #888; }} .count {{ fill: #c62828; font-size: 14px; font-weight: 700; }} .tick {{ font-size: 10px; }} .week-total {{ fill: #c62828; font-size: 12px; font-weight: 700; }} .week-total.empty {{ fill: #888; font-weight: 400; }} .stripe.ongoing {{ stroke: #8c1616; stroke-width: 1.5; stroke-dasharray: 3 2; }}</style>
  <rect x="0" y="0" width="1000" height="{height}" fill="white"/>
  <text x="{margin}" y="32" class="title">Connectivity outage calendar</text>
  {month_header}
  {''.join(body)}
</svg>
'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
