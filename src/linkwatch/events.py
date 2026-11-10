"""CSV persistence and schema migration for Link Watch events."""

from __future__ import annotations

import csv
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .defaults import CSV_FIELDS


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
