"""Manage Link Watch as a per-user systemd service on Linux."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

UNIT_NAME = "linkwatch.service"
UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
MANAGED_DESCRIPTION = "Description=Link Watch connectivity monitor"


class SystemdError(RuntimeError):
    """A systemctl operation failed or the user unit is misconfigured."""


def systemctl(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a user-scoped systemctl operation and translate process failures."""
    executable = shutil.which("systemctl")
    if executable is None:
        raise SystemdError("systemctl was not found; Linux service management requires systemd")
    try:
        return subprocess.run(
            [executable, "--user", *arguments],
            check=check,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise SystemdError(f"systemctl --user {' '.join(arguments)} failed: {detail}") from error
    except OSError as error:
        raise SystemdError(f"could not run systemctl: {error}") from error


def _quote_exec_argument(argument: str) -> str:
    """Quote one systemd ExecStart word, including unit specifier/variable escapes."""
    escaped = (
        argument.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def build_unit_file(
    *,
    python_executable: str,
    period: float,
    timeout: float,
    log_path: Path,
    home: Path,
    config_path: Path | None = None,
) -> str:
    """Build a per-user systemd unit without writing files or invoking systemctl."""
    if period <= 0:
        raise ValueError("period must be greater than zero")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    arguments = [
        python_executable,
        "-m",
        "linkwatch",
    ]
    if config_path is not None:
        arguments.extend(("--config", str(config_path)))
    arguments.extend((
        "monitor",
        "--interval",
        str(period),
        "--timeout",
        str(timeout),
        "--log",
        str(log_path),
    ))
    exec_start = " ".join(_quote_exec_argument(argument) for argument in arguments)
    working_directory = _quote_exec_argument(str(home))
    return f"""[Unit]
{MANAGED_DESCRIPTION}

[Service]
Type=simple
WorkingDirectory={working_directory}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _write_unit_atomically(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(contents)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def service_status() -> bool:
    """Return whether the user service is active; distinguish expected inactive states."""
    result = systemctl(["is-active", UNIT_NAME], check=False)
    if result.returncode == 0:
        return True
    if result.returncode in {3, 4}:
        return False
    detail = (result.stderr or result.stdout or "unexpected systemctl exit status").strip()
    raise SystemdError(f"could not determine {UNIT_NAME} status: {detail}")


def install_service(
    period: float,
    timeout: float,
    log_path: Path,
    config_path: Path | None = None,
) -> Path:
    """Install/refresh the managed unit and enable it in the current user manager."""
    was_active = False
    if UNIT_PATH.exists():
        try:
            existing = UNIT_PATH.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise SystemdError(f"refusing to replace an unreadable existing unit: {UNIT_PATH}") from error
        if MANAGED_DESCRIPTION not in existing:
            raise SystemdError(f"refusing to replace an unmanaged systemd unit: {UNIT_PATH}")
        was_active = service_status()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    contents = build_unit_file(
        python_executable=sys.executable,
        period=period,
        timeout=timeout,
        log_path=log_path,
        home=Path.home(),
        config_path=config_path,
    )
    _write_unit_atomically(UNIT_PATH, contents)
    systemctl(["daemon-reload"])
    systemctl(["enable", "--now", UNIT_NAME])
    if was_active:
        systemctl(["restart", UNIT_NAME])
    return UNIT_PATH


def start_service() -> None:
    """Enable and start the installed unit in the current user's systemd manager."""
    if not UNIT_PATH.is_file():
        raise SystemdError(f"systemd unit not found at {UNIT_PATH}; run 'linkwatch service install' first")
    systemctl(["enable", "--now", UNIT_NAME])


def stop_service() -> bool:
    """Disable and stop the unit; return whether it was active."""
    if not UNIT_PATH.is_file():
        raise SystemdError(f"systemd unit not found at {UNIT_PATH}")
    was_active = service_status()
    systemctl(["disable", "--now", UNIT_NAME])
    return was_active


def uninstall_service() -> bool:
    """Stop and remove the managed unit, preserving the connectivity CSV."""
    if not UNIT_PATH.exists():
        return False
    try:
        existing = UNIT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SystemdError(f"refusing to remove an unreadable unit: {UNIT_PATH}") from error
    if MANAGED_DESCRIPTION not in existing:
        raise SystemdError(f"refusing to remove an unmanaged systemd unit: {UNIT_PATH}")
    was_active = stop_service()
    UNIT_PATH.unlink()
    systemctl(["daemon-reload"])
    return was_active
