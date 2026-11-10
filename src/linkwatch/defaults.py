"""Built-in paths and timing defaults shared across Link Watch commands."""

from pathlib import Path

DEFAULT_INTERVAL = 5.0
DEFAULT_TIMEOUT = 2.0
DEFAULT_LOG = Path.home() / ".linkwatch" / "connectivity.csv"
DEFAULT_CONFIG = Path.home() / ".config" / "linkwatch" / "config.toml"
DEFAULT_CHART = Path("linkwatch.svg")
CSV_FIELDS = ("timestamp_utc", "event", "endpoint", "public_ipv4", "public_ipv6")
