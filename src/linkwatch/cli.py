"""Typer command-line application and handlers for Link Watch."""

from __future__ import annotations

import signal
from datetime import date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from .calendar_chart import generate_calendar_chart
from .chart import generate_chart
from .config import ConfigError, ConfigSettings, load_config, redact_url
from .defaults import (
    DEFAULT_CHART,
    DEFAULT_CONFIG,
    DEFAULT_INTERVAL,
    DEFAULT_LOG,
    DEFAULT_TIMEOUT,
)
from .events import append_event
from .launchd import app as launchd_app
from .monitor import monitor
from .service import app as service_app

app = typer.Typer(
    name="linkwatch",
    add_completion=False,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Monitor internet reachability using built-in Google, Apple, and Microsoft "
        "checks or user-configured targets. Log connectivity transitions to CSV."
    ),
)
config_app = typer.Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Inspect and validate probe-target configuration.",
)
app.add_typer(config_app, name="config")
app.add_typer(launchd_app, name="launchd")
app.add_typer(service_app, name="service")


def version_callback(value: bool) -> None:
    """Print the installed package version and exit."""
    if value:
        typer.echo(f"linkwatch {version('linkwatch')}")
        raise typer.Exit()


@app.callback()
def root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            is_eager=True,
            callback=version_callback,
            help="Show the version and exit.",
        ),
    ] = False,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Probe-target TOML file (default: ~/.config/linkwatch/config.toml)."),
    ] = None,
) -> None:
    """Monitor internet reachability and log outages."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


def _config_path_from_context(ctx: typer.Context) -> Path | None:
    """Return the root --config option, if explicitly supplied."""
    root_context = ctx.find_root()
    return (root_context.obj or {}).get("config_path")


def _load_config_from_context(ctx: typer.Context) -> ConfigSettings:
    """Load configuration selected for this invocation."""
    return load_config(_config_path_from_context(ctx))


def _report_config_error(error: ConfigError) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1) from error


@config_app.command("path")
def config_path_command(ctx: typer.Context) -> None:
    """Show the selected configuration path and whether a file exists there."""
    selected = _config_path_from_context(ctx)
    path = (selected if selected is not None else DEFAULT_CONFIG).expanduser().resolve()
    source = "command line" if selected is not None else "default"
    typer.echo(f"Configuration path: {path}")
    typer.echo(f"Selected by: {source}")
    typer.echo(f"File exists: {'yes' if path.is_file() else 'no'}")


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    """Show the effective target labels and URLs, redacting query values."""
    try:
        settings = _load_config_from_context(ctx)
    except ConfigError as error:
        _report_config_error(error)
    typer.echo(f"Configuration source: {settings.source}")
    typer.echo(f"Configuration path: {settings.path}")
    typer.echo(f"Targets ({len(settings.targets)}):")
    for target in settings.targets:
        typer.echo(f"- {target.name}: {redact_url(target.url)}")


@config_app.command("validate")
def config_validate(ctx: typer.Context) -> None:
    """Validate the selected configuration without making network requests."""
    try:
        settings = _load_config_from_context(ctx)
    except ConfigError as error:
        _report_config_error(error)
    count = len(settings.targets)
    label = "target" if count == 1 else "targets"
    typer.echo(f"Configuration valid: {count} {label} ({settings.source})")


def _handle_sigterm(_signum: int, _frame: object) -> None:
    """Turn service-manager termination into the monitor's graceful interrupt path."""
    raise KeyboardInterrupt


@app.command("monitor")
def monitor_cli(
    ctx: typer.Context,
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
    """Monitor internet reachability; set root --config PATH before monitor to choose targets."""
    try:
        settings = _load_config_from_context(ctx)
    except ConfigError as error:
        _report_config_error(error)
    previous_sigterm_handler = signal.signal(signal.SIGTERM, _handle_sigterm)
    started = False
    stopped_gracefully = False
    try:
        append_event(log, "monitor_started")
        started = True
        try:
            monitor(interval, timeout, log, endpoints=settings.targets)
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
    end_time: Annotated[
        str | None,
        typer.Option(help="Fixed chart endpoint as a timezone-aware ISO 8601 timestamp."),
    ] = None,
) -> None:
    """Generate an SVG timeline with connectivity and monitor lifecycle history."""
    try:
        fixed_end_time = None
        if end_time is not None:
            fixed_end_time = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            if fixed_end_time.tzinfo is None or fixed_end_time.utcoffset() is None:
                raise ValueError("--end-time must include a timezone offset")
        generate_chart(log, output, end_time=fixed_end_time)
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
