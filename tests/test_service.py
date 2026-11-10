from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from linkwatch.cli import app
from linkwatch import launchd, service, systemd

runner = CliRunner()


@pytest.mark.parametrize(
    ("system_name", "expected"),
    [("Darwin", launchd), ("Linux", systemd)],
)
def test_service_dispatches_to_native_backend(system_name, expected, monkeypatch):
    monkeypatch.setattr(service.platform, "system", lambda: system_name)
    assert service.get_backend() is expected


def test_service_rejects_unsupported_platform():
    with pytest.raises(service.UnsupportedPlatformError, match="supported on macOS and Linux"):
        service.get_backend("FreeBSD")


def test_public_service_command_reports_unsupported_platform(monkeypatch):
    monkeypatch.setattr(service.platform, "system", lambda: "FreeBSD")

    result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 1
    assert "supported on macOS and Linux" in result.output


def test_service_cli_uses_backend_for_detected_platform(monkeypatch):
    calls = []
    backend = type("Backend", (), {"install_service": staticmethod(lambda *args: calls.append(args) or Path("/unit"))})
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service, "systemd", backend)

    result = runner.invoke(app, ["service", "install", "--period", "0.75"])

    assert result.exit_code == 0, result.output
    assert calls
    assert calls[0][0] == 0.75


def test_service_install_forwards_explicit_config_to_backend(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "probes.toml"
    config_path.write_text(
        '[[targets]]\nname = "Regional"\nurl = "https://regional.example/"\n',
        encoding="utf-8",
    )
    calls = []
    backend = type("Backend", (), {"install_service": staticmethod(lambda *args: calls.append(args) or Path("/unit"))})
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service, "systemd", backend)

    result = runner.invoke(app, ["--config", str(config_path), "service", "install"])

    assert result.exit_code == 0, result.output
    assert calls[0][-1] == config_path.resolve()


def test_service_install_rejects_invalid_config_before_backend_install(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "bad.toml"
    config_path.write_text("targets = []\n", encoding="utf-8")
    calls = []
    backend = type("Backend", (), {"install_service": staticmethod(lambda *args: calls.append(args) or Path("/unit"))})
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(service, "systemd", backend)

    result = runner.invoke(app, ["--config", str(config_path), "service", "install"])

    assert result.exit_code == 1
    assert str(config_path) in result.output
    assert calls == []


def test_root_help_includes_platform_neutral_service_group():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "service" in result.output


def test_service_install_help_mentions_root_config_option(plain_cli_output):
    result = runner.invoke(app, ["service", "install", "--help"])

    assert result.exit_code == 0
    assert "--config PATH" in plain_cli_output(result)
