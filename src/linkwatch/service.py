"""Platform-neutral service commands for macOS and Linux."""

from __future__ import annotations

import platform
from pathlib import Path
from types import ModuleType
from typing import Annotated, NoReturn

import typer

from . import launchd, systemd
from .defaults import DEFAULT_INTERVAL, DEFAULT_LOG, DEFAULT_TIMEOUT

app = typer.Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Install and manage the per-user Link Watch service for this platform.",
)


class UnsupportedPlatformError(RuntimeError):
    """Raised when no supported user-service backend exists for this operating system."""


def get_backend(system_name: str | None = None) -> ModuleType:
    """Choose the native service manager for macOS or Linux."""
    detected = system_name or platform.system()
    if detected == "Darwin":
        return launchd
    if detected == "Linux":
        return systemd
    raise UnsupportedPlatformError(
        f"service management is supported on macOS and Linux, not {detected}"
    )


def _report_error(error: Exception) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1) from error


@app.command()
def install(
    period: Annotated[
        float,
        typer.Option("--period", "--interval", help="Seconds between checks.", min=0.000001),
    ] = DEFAULT_INTERVAL,
    timeout: Annotated[
        float,
        typer.Option(help="Timeout per endpoint in seconds.", min=0.000001),
    ] = DEFAULT_TIMEOUT,
    log: Annotated[
        Path,
        typer.Option(help="CSV log path."),
    ] = DEFAULT_LOG,
) -> None:
    """Install and start the native per-user service."""
    try:
        path = get_backend().install_service(period, timeout, log)
    except (RuntimeError, OSError, ValueError) as error:
        _report_error(error)
    typer.echo(f"Installed and started Link Watch service from {path}")


@app.command()
def start() -> None:
    """Start the installed service and enable it at future logins."""
    try:
        get_backend().start_service()
    except (RuntimeError, OSError) as error:
        _report_error(error)
    typer.echo("Started Link Watch service")


@app.command()
def stop() -> None:
    """Stop the service and disable its automatic start."""
    try:
        was_active = get_backend().stop_service()
    except (RuntimeError, OSError) as error:
        _report_error(error)
    typer.echo("Stopped Link Watch service" if was_active else "Link Watch service is not active")


@app.command()
def status() -> None:
    """Show whether the service is active."""
    try:
        backend = get_backend()
        active = backend.service_status()
    except (RuntimeError, OSError) as error:
        _report_error(error)
    typer.echo(f"Link Watch service: {'active' if active else 'inactive'}")


@app.command()
def uninstall() -> None:
    """Stop and remove the service configuration, preserving connectivity logs."""
    try:
        was_active = get_backend().uninstall_service()
    except (RuntimeError, OSError) as error:
        _report_error(error)
    status = "stopped and " if was_active else ""
    typer.echo(f"Link Watch service: {status}uninstalled")
