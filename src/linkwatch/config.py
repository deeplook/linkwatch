"""Load and validate human-owned probe-target configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .defaults import DEFAULT_CONFIG
from .monitor import ENDPOINTS, Endpoint


class ConfigError(ValueError):
    """Raised when Link Watch configuration cannot be loaded or validated."""


@dataclass(frozen=True)
class ConfigSettings:
    """Resolved probe settings and the origin of their configuration."""

    targets: tuple[Endpoint, ...]
    path: Path
    source: str
    explicit: bool


def load_config(
    config_path: Path | None = None,
    *,
    default_path: Path = DEFAULT_CONFIG,
) -> ConfigSettings:
    """Load targets from TOML, falling back only when the default file is absent."""
    explicit = config_path is not None
    selected_path = (config_path if explicit else default_path).expanduser().resolve()

    try:
        text = selected_path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if explicit:
            raise ConfigError(f"configuration file not found: {selected_path}") from error
        return ConfigSettings(ENDPOINTS, selected_path, "built-in defaults", False)
    except (OSError, UnicodeError) as error:
        raise ConfigError(f"cannot read configuration file {selected_path}: {error}") from error

    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{selected_path}: invalid TOML: {error}") from error

    if not isinstance(document, dict):
        raise ConfigError(f"{selected_path}: configuration must be a TOML table")
    unknown_keys = set(document) - {"targets"}
    if unknown_keys:
        raise ConfigError(
            f"{selected_path}: unknown configuration key(s): {', '.join(sorted(unknown_keys))}"
        )
    raw_targets = document.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError(f"{selected_path}: 'targets' must contain at least one [[targets]] entry")

    targets: list[Endpoint] = []
    seen_names: set[str] = set()
    for index, raw_target in enumerate(raw_targets, start=1):
        prefix = f"{selected_path}: targets[{index}]"
        if not isinstance(raw_target, dict):
            raise ConfigError(f"{prefix} must be a table")
        missing = {"name", "url"} - set(raw_target)
        unknown = set(raw_target) - {"name", "url"}
        if missing or unknown:
            details = []
            if missing:
                details.append(f"missing {', '.join(sorted(missing))}")
            if unknown:
                details.append(f"unknown key(s) {', '.join(sorted(unknown))}")
            raise ConfigError(f"{prefix}: {'; '.join(details)}")

        name = raw_target["name"]
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{prefix}.name must be a non-empty string")
        name = name.strip()
        folded_name = name.casefold()
        if folded_name in seen_names:
            raise ConfigError(f"{prefix}.name must be unique; duplicate label {name!r}")
        seen_names.add(folded_name)

        url = raw_target["url"]
        if not isinstance(url, str) or not url.strip():
            raise ConfigError(f"{prefix}.url must be a non-empty absolute HTTP(S) URL")
        url = url.strip()
        if any(character.isspace() for character in url):
            raise ConfigError(f"{prefix}.url must not contain whitespace")
        try:
            parsed = urlsplit(url)
            port = parsed.port  # Access validates that a supplied port is numeric and in range.
        except ValueError as error:
            raise ConfigError(f"{prefix}.url is invalid: {error}") from error
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ConfigError(f"{prefix}.url must be an absolute HTTP(S) URL with a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ConfigError(f"{prefix}.url must not contain embedded credentials")
        if port == 0:
            raise ConfigError(f"{prefix}.url port must be between 1 and 65535")
        targets.append(Endpoint(name, url))

    return ConfigSettings(tuple(targets), selected_path, "configuration file", explicit)


def redact_url(url: str) -> str:
    """Hide query and fragment values when displaying a configured target URL."""
    parsed = urlsplit(url)
    return parsed._replace(
        query="[redacted]" if parsed.query else "",
        fragment="[redacted]" if parsed.fragment else "",
    ).geturl()
