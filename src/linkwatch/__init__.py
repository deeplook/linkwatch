"""Monitor internet reachability and record connectivity transitions."""

from __future__ import annotations

import csv
import html
import ipaddress
import os
import signal
import stat
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Annotated, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

import typer

from .calendar_chart import generate_calendar_chart
from .defaults import (
    CSV_FIELDS,
    DEFAULT_CHART,
    DEFAULT_INTERVAL,
    DEFAULT_LOG,
    DEFAULT_TIMEOUT,
)
from .launchd import app as launchd_app
from .service import app as service_app

app = typer.Typer(
    name="linkwatch",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Monitor internet reachability using Google first, then Apple and Microsoft "
        "as fallbacks. Log connectivity transitions to CSV."
    ),
)
app.add_typer(launchd_app, name="launchd")
app.add_typer(service_app, name="service")


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str


ENDPOINTS = (
    Endpoint("Google", "https://connectivitycheck.gstatic.com/generate_204"),
    Endpoint("Apple", "https://captive.apple.com/hotspot-detect.html"),
    Endpoint("Microsoft", "https://www.msftconnecttest.com/connecttest.txt"),
)

PUBLIC_IP_ENDPOINTS = {
    4: ("https://api.ipify.org", "https://ipv4.icanhazip.com/"),
    6: ("https://api6.ipify.org", "https://ipv6.icanhazip.com/"),
}


def probe(endpoint_url: str, timeout: float) -> bool:
    """Return whether an HTTP connectivity check receives a successful response."""
    request = Request(endpoint_url, headers={"User-Agent": "linkwatch/0.1"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (OSError, URLError, TimeoutError, ValueError):
        return False


def find_reachable_endpoint(
    *, check: Callable[[str, float], bool] = probe, timeout: float = DEFAULT_TIMEOUT
) -> Endpoint | None:
    """Try Google, then Apple, then Microsoft; return the first responder."""
    for endpoint in ENDPOINTS:
        if check(endpoint.url, timeout):
            return endpoint
    return None


def lookup_public_ip(version: int, timeout: float) -> str | None:
    """Return the externally observed address for one family, trying a fallback."""
    if version not in PUBLIC_IP_ENDPOINTS:
        raise ValueError("IP version must be 4 or 6")
    for endpoint_url in PUBLIC_IP_ENDPOINTS[version]:
        request = Request(endpoint_url, headers={"User-Agent": "linkwatch/0.1"}, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                text = response.read().decode("ascii").strip()
            address = ipaddress.ip_address(text)
        except (HTTPException, OSError, URLError, TimeoutError, UnicodeError, ValueError):
            continue
        if address.version == version and address.is_global:
            return str(address)
    return None


def migrate_csv_schema(log_path: Path) -> None:
    """Atomically add public-IP columns to a legacy connectivity CSV."""
    if not log_path.is_file() or log_path.stat().st_size == 0:
        return

    with log_path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        old_fields = reader.fieldnames
        if old_fields == list(CSV_FIELDS):
            return
        if old_fields is None or not set(old_fields).issubset(CSV_FIELDS):
            raise ValueError(f"unsupported CSV schema in {log_path}: {old_fields}")
        if not {"timestamp_utc", "event", "endpoint"}.issubset(old_fields):
            raise ValueError(f"unsupported CSV schema in {log_path}: {old_fields}")
        rows = list(reader)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{log_path.name}.", suffix=".tmp", dir=log_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as destination:
            os.chmod(temporary_name, stat.S_IMODE(log_path.stat().st_mode))
            writer = csv.DictWriter(destination, fieldnames=list(CSV_FIELDS))
            writer.writeheader()
            writer.writerows(rows)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_name, log_path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def append_event(
    log_path: Path,
    event: str,
    endpoint_name: str = "",
    *,
    public_ipv4: str = "",
    public_ipv6: str = "",
) -> None:
    """Append a UTC connectivity or monitor lifecycle event to the CSV log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    migrate_csv_schema(log_path)
    needs_header = not log_path.exists() or log_path.stat().st_size == 0
    if event != "connected":
        public_ipv4 = public_ipv6 = ""
    with log_path.open("a", newline="", encoding="utf-8") as log_file:
        writer = csv.DictWriter(log_file, fieldnames=CSV_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "endpoint": endpoint_name,
                "public_ipv4": public_ipv4,
                "public_ipv6": public_ipv6,
            }
        )


def generate_chart(log_path: Path, output_path: Path) -> None:
    """Render connectivity and monitor lifecycle history as a dependency-free SVG."""
    if not log_path.is_file():
        raise FileNotFoundError(f"log file not found: {log_path}")

    connectivity_events = {"connected", "disconnected"}
    lifecycle_labels = {
        "monitor_started": "Monitoring started",
        "monitor_stopped": "Graceful stop",
    }
    accepted_events = connectivity_events | lifecycle_labels.keys()
    events: list[tuple[datetime, str]] = []
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
    start = events[0][0]
    end = max(datetime.now(timezone.utc), events[-1][0])
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
    has_lifecycle_events = any(event in lifecycle_labels for _, event in events)

    for timestamp, event in events:
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


def monitor(
    interval: float = DEFAULT_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    log_path: Path = DEFAULT_LOG,
    *,
    check: Callable[[str, float], bool] = probe,
    sleep: Callable[[float], None] = time.sleep,
    lookup_ip: Callable[[int, float], str | None] = lookup_public_ip,
) -> None:
    """Poll until interrupted, logging only offline/online transitions."""
    if interval <= 0:
        raise ValueError("interval must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    previous_online: bool | None = None
    while True:
        endpoint = find_reachable_endpoint(check=check, timeout=timeout)
        online = endpoint is not None
        if previous_online is None and not online:
            append_event(log_path, "disconnected")
            print(f"disconnected: {datetime.now(timezone.utc).isoformat()} (logged to {log_path})", flush=True)
        elif previous_online is not None and online != previous_online:
            event = "connected" if online else "disconnected"
            endpoint_name = endpoint.name if endpoint is not None else ""
            public_ipv4 = (lookup_ip(4, timeout) or "") if online else ""
            public_ipv6 = (lookup_ip(6, timeout) or "") if online else ""
            append_event(
                log_path,
                event,
                endpoint_name,
                public_ipv4=public_ipv4,
                public_ipv6=public_ipv6,
            )
            detail = f" via {endpoint_name}" if endpoint_name else ""
            print(f"{event}{detail}: {datetime.now(timezone.utc).isoformat()} (logged to {log_path})", flush=True)
        previous_online = online
        sleep(interval)


def _handle_sigterm(_signum: int, _frame: object) -> None:
    """Turn service-manager termination into the monitor's graceful interrupt path."""
    raise KeyboardInterrupt


@app.command("monitor")
def monitor_cli(
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            "--period",
            help="Seconds between probe cycles; accepts fractional seconds.",
            min=0.000001,
        ),
    ] = DEFAULT_INTERVAL,
    timeout: Annotated[float, typer.Option(help="Timeout per endpoint.", min=0.000001)] = DEFAULT_TIMEOUT,
    log: Annotated[
        Path,
        typer.Option(help="CSV log path (default: ~/.linkwatch/connectivity.csv)."),
    ] = DEFAULT_LOG,
) -> None:
    """Monitor internet reachability and record monitor lifecycle events."""
    previous_sigterm_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    started = False
    stopped_gracefully = False
    try:
        append_event(log, "monitor_started")
        started = True
        try:
            monitor(interval, timeout, log)
        except KeyboardInterrupt:
            stopped_gracefully = True
            typer.echo("stopped")
        else:
            stopped_gracefully = True
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
        if started and stopped_gracefully:
            append_event(log, "monitor_stopped")


@app.command("chart")
def chart_cli(
    log: Annotated[
        Path,
        typer.Option(help="CSV event log (default: ~/.linkwatch/connectivity.csv)."),
    ] = DEFAULT_LOG,
    output: Annotated[
        Path,
        typer.Option(help="SVG output path."),
    ] = DEFAULT_CHART,
) -> None:
    """Generate an SVG timeline with connectivity and monitor lifecycle history."""
    try:
        generate_chart(log, output)
    except (OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Chart written to {output}")


@app.command("calendar")
def calendar_cli(
    log: Annotated[Path, typer.Option(help="CSV event log (default: ~/.linkwatch/connectivity.csv).")] = DEFAULT_LOG,
    output: Annotated[Path, typer.Option(help="Calendar SVG output path.")] = Path("linkwatch-calendar.svg"),
    start_date: Annotated[str | None, typer.Option("--from", help="First displayed date (YYYY-MM-DD); requires --to.")] = None,
    end_date: Annotated[str | None, typer.Option("--to", help="Last displayed date, inclusive (YYYY-MM-DD); requires --from.")] = None,
    period: Annotated[str | None, typer.Option(help="Calendar period ending at the latest log date: day, week, month, 3-months, or year.")] = None,
    timezone_name: Annotated[str, typer.Option("--timezone", help="Display timezone: local (default), UTC, or an IANA timezone.")] = "local",
) -> None:
    """Generate an SVG calendar of daily outage intervals and counts."""
    try:
        generate_calendar_chart(
            log,
            output,
            start_date=date.fromisoformat(start_date) if start_date else None,
            end_date=date.fromisoformat(end_date) if end_date else None,
            period=period,
            timezone_name=timezone_name,
        )
    except (OSError, ValueError) as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Calendar chart written to {output}")


def main() -> None:
    """Typer console-script entry point."""
    app()


if __name__ == "__main__":
    main()
