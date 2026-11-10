from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from linkwatch.cli import app
from linkwatch import service, systemd

runner = CliRunner()


def test_build_unit_quotes_arguments_and_configures_user_service(tmp_path: Path):
    unit = systemd.build_unit_file(
        python_executable="/opt/link watch/bin/python",
        period=0.75,
        timeout=1.25,
        log_path=tmp_path / "logs" / "50% $ event.csv",
        home=tmp_path / "home dir",
    )

    assert "Description=Link Watch connectivity monitor" in unit
    assert "After=network-online.target" not in unit
    assert 'WorkingDirectory="' + str(tmp_path / "home dir") + '"' in unit
    assert 'ExecStart="/opt/link watch/bin/python" "-m" "linkwatch" "monitor"' in unit
    assert '"--interval" "0.75" "--timeout" "1.25" "--log"' in unit
    assert "50%% $$ event.csv" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit


def test_build_unit_persists_explicit_config_path(tmp_path: Path):
    config_path = tmp_path / "config dir" / "probes.toml"

    unit = systemd.build_unit_file(
        python_executable="/opt/linkwatch/bin/python",
        period=5,
        timeout=2,
        log_path=tmp_path / "connectivity.csv",
        home=tmp_path,
        config_path=config_path,
    )

    assert f'"--config" "{config_path}" "monitor"' in unit


def test_systemctl_always_targets_the_current_users_manager(monkeypatch):
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(systemd.subprocess, "run", fake_run)

    systemd.systemctl(["daemon-reload"])

    assert calls == [
        (["/usr/bin/systemctl", "--user", "daemon-reload"], {
            "check": True,
            "capture_output": True,
            "text": True,
        })
    ]


def test_install_writes_and_enables_user_unit_without_live_systemctl(tmp_path: Path, monkeypatch):
    unit_path = tmp_path / "systemd" / "user" / "linkwatch.service"
    log_path = tmp_path / "state" / "connectivity.csv"
    calls = []

    def fake_systemctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "UNIT_PATH", unit_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd.sys, "executable", "/opt/linkwatch/bin/python")
    monkeypatch.setattr(systemd, "systemctl", fake_systemctl)

    result = runner.invoke(
        app,
        ["service", "install", "--period", "0.75", "--timeout", "1.25", "--log", str(log_path)],
    )

    assert result.exit_code == 0, result.output
    assert unit_path.is_file()
    contents = unit_path.read_text(encoding="utf-8")
    assert '"/opt/linkwatch/bin/python" "-m" "linkwatch" "monitor"' in contents
    assert f'"--log" "{log_path}"' in contents
    assert calls == [
        (["daemon-reload"], True),
        (["enable", "--now", systemd.UNIT_NAME], True),
    ]


def test_install_refuses_to_replace_an_unmanaged_user_unit(tmp_path: Path, monkeypatch):
    unit_path = tmp_path / "linkwatch.service"
    existing = "[Unit]\nDescription=Some other service\n"
    unit_path.write_text(existing, encoding="utf-8")
    calls = []

    monkeypatch.setattr(systemd, "UNIT_PATH", unit_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "systemctl", lambda *args, **kwargs: calls.append(args))

    result = runner.invoke(app, ["service", "install"])

    assert result.exit_code == 1
    assert "refusing to replace" in result.output.lower()
    assert unit_path.read_text(encoding="utf-8") == existing
    assert calls == []


def test_status_reports_active_state_without_modifying_unit(tmp_path: Path, monkeypatch):
    unit_path = tmp_path / "linkwatch.service"
    unit_path.write_text("managed", encoding="utf-8")
    calls = []

    def fake_systemctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="active", stderr="")

    monkeypatch.setattr(systemd, "UNIT_PATH", unit_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "systemctl", fake_systemctl)

    result = runner.invoke(app, ["service", "status"])

    assert result.exit_code == 0
    assert "active" in result.output.lower()
    assert calls == [(["is-active", systemd.UNIT_NAME], False)]


def test_status_treats_expected_systemd_inactive_code_as_inactive(monkeypatch):
    monkeypatch.setattr(
        systemd,
        "systemctl",
        lambda arguments, check=True: subprocess.CompletedProcess(
            arguments, returncode=3, stdout="inactive", stderr=""
        ),
    )

    assert systemd.service_status() is False


def test_stop_and_uninstall_preserve_connectivity_log(tmp_path: Path, monkeypatch):
    unit_path = tmp_path / "linkwatch.service"
    unit_path.write_text(f"[Unit]\n{systemd.MANAGED_DESCRIPTION}\n", encoding="utf-8")
    log_path = tmp_path / "connectivity.csv"
    log_path.write_text("keep", encoding="utf-8")
    calls = []

    def fake_systemctl(arguments, *, check=True):
        calls.append((arguments, check))
        if arguments[0] == "is-active":
            return subprocess.CompletedProcess(arguments, returncode=0, stdout="active", stderr="")
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(systemd, "UNIT_PATH", unit_path)
    monkeypatch.setattr(service.platform, "system", lambda: "Linux")
    monkeypatch.setattr(systemd, "systemctl", fake_systemctl)

    result = runner.invoke(app, ["service", "uninstall"])

    assert result.exit_code == 0, result.output
    assert not unit_path.exists()
    assert log_path.read_text(encoding="utf-8") == "keep"
    assert calls == [
        (["is-active", systemd.UNIT_NAME], False),
        (["disable", "--now", systemd.UNIT_NAME], True),
        (["daemon-reload"], True),
    ]
