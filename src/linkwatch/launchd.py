"""Manage Link Watch as a per-user macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from .defaults import DEFAULT_INTERVAL, DEFAULT_LOG, DEFAULT_TIMEOUT

LABEL = "com.linkwatch.agent"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
app = typer.Typer(
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Install and manage Link Watch as a per-user macOS LaunchAgent.",
)


class LaunchdError(RuntimeError):
    """A launchctl operation failed or the LaunchAgent is misconfigured."""


def service_target() -> str:
    """Return the current user's GUI launchd service target."""
    return f"gui/{os.getuid()}/{LABEL}"


def service_domain() -> str:
    """Return the current user's GUI launchd domain."""
    return f"gui/{os.getuid()}"


def launchctl(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run launchctl and translate process errors into actionable messages."""
    executable = shutil.which("launchctl")
    if executable is None:
        raise LaunchdError("launchctl was not found; this command is available only on macOS")
    try:
        return subprocess.run(
            [executable, *arguments],
            check=check,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise LaunchdError(f"launchctl {' '.join(arguments)} failed: {detail}") from error
    except OSError as error:
        raise LaunchdError(f"could not run launchctl: {error}") from error


def build_agent_plist(
    *,
    python_executable: str,
    period: float,
    timeout: float,
    log_path: Path,
    home: Path,
) -> dict[str, object]:
    """Build the LaunchAgent plist without writing files or invoking launchctl."""
    if period <= 0:
        raise ValueError("period must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    state_dir = home / ".linkwatch"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python_executable,
            "-m",
            "linkwatch",
            "monitor",
            "--interval",
            str(period),
            "--timeout",
            str(timeout),
            "--log",
            str(log_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(home),
        "StandardOutPath": str(state_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(state_dir / "launchd.stderr.log"),
        "ProcessType": "Background",
    }


def _is_loaded() -> bool:
    return launchctl(["print", service_target()], check=False).returncode == 0


def service_status() -> bool:
    """Return whether the LaunchAgent is loaded for the current user."""
    return _is_loaded()


def _write_plist_atomically(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(encoded)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def install_service(period: float, timeout: float, log_path: Path) -> Path:
    """Write the agent plist and bootstrap it into the current GUI domain."""
    if AGENT_PATH.exists():
        try:
            with AGENT_PATH.open("rb") as existing_file:
                existing_plist = plistlib.load(existing_file)
        except (OSError, plistlib.InvalidFileException, ValueError) as error:
            raise LaunchdError(
                f"refusing to replace an unreadable existing LaunchAgent file: {AGENT_PATH}"
            ) from error
        if not isinstance(existing_plist, dict) or existing_plist.get("Label") != LABEL:
            raise LaunchdError(
                f"refusing to replace an existing LaunchAgent with a different label: {AGENT_PATH}"
            )

    if _is_loaded():
        launchctl(["bootout", service_target()])
    launchctl(["enable", service_target()])
    state_dir = Path.home() / ".linkwatch"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    value = build_agent_plist(
        python_executable=sys.executable,
        period=period,
        timeout=timeout,
        log_path=log_path,
        home=Path.home(),
    )
    _write_plist_atomically(AGENT_PATH, value)
    launchctl(["bootstrap", service_domain(), str(AGENT_PATH)])
    return AGENT_PATH


def start_service() -> None:
    """Enable the installed service and load it, or restart it if already loaded."""
    if not AGENT_PATH.is_file():
        raise LaunchdError(f"LaunchAgent plist not found at {AGENT_PATH}; run 'linkwatch launchd install' first")
    launchctl(["enable", service_target()])
    if _is_loaded():
        launchctl(["kickstart", "-k", service_target()])
    else:
        launchctl(["bootstrap", service_domain(), str(AGENT_PATH)])


def stop_service() -> bool:
    """Disable future auto-starts and unload the service; return whether it was loaded."""
    loaded = _is_loaded()
    launchctl(["disable", service_target()])
    if loaded:
        launchctl(["bootout", service_target()])
    return loaded


def uninstall_service() -> bool:
    """Unload and remove the plist, leaving CSV and diagnostic logs untouched."""
    was_loaded = stop_service()
    AGENT_PATH.unlink(missing_ok=True)
    launchctl(["enable", service_target()])
    return was_loaded


def _report_error(error: Exception) -> NoReturn:
    typer.echo(f"Error: {error}", err=True)
    raise typer.Exit(code=1) from error


@app.command()
def install(
    period: Annotated[
        float,
        typer.Option("--period", "--interval", help="Seconds between checks; fractional seconds are supported.", min=0.000001),
    ] = DEFAULT_INTERVAL,
    timeout: Annotated[
        float,
        typer.Option(help="Timeout per endpoint in seconds.", min=0.000001),
    ] = DEFAULT_TIMEOUT,
    log: Annotated[
        Path,
        typer.Option(help="CSV path (default: ~/.linkwatch/connectivity.csv)."),
    ] = DEFAULT_LOG,
) -> None:
    """Install and start the per-user LaunchAgent (starts at future logins)."""
    try:
        path = install_service(period, timeout, log)
    except (LaunchdError, OSError, ValueError) as error:
        _report_error(error)
    typer.echo(f"Installed and started {LABEL} from {path}")


@app.command()
def start() -> None:
    """Start the installed LaunchAgent, or restart it if already loaded."""
    try:
        start_service()
    except (LaunchdError, OSError) as error:
        _report_error(error)
    typer.echo(f"Started {LABEL}")


@app.command()
def stop() -> None:
    """Disable and unload the LaunchAgent until it is started again."""
    try:
        was_loaded = stop_service()
    except (LaunchdError, OSError) as error:
        _report_error(error)
    typer.echo(f"Stopped {LABEL}" if was_loaded else f"{LABEL} is not loaded")


@app.command()
def status() -> None:
    """Show whether the LaunchAgent is loaded for the current user."""
    try:
        loaded = _is_loaded()
    except (LaunchdError, OSError) as error:
        _report_error(error)
    typer.echo(f"{LABEL}: {'loaded' if loaded else 'not loaded'}")


@app.command()
def uninstall() -> None:
    """Stop the service and remove its plist; keep CSV and diagnostic logs."""
    try:
        was_loaded = uninstall_service()
    except (LaunchdError, OSError) as error:
        _report_error(error)
    status = "stopped and " if was_loaded else ""
    typer.echo(f"{LABEL}: {status}uninstalled")
