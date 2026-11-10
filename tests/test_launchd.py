import plistlib
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from linkwatch.cli import app
from linkwatch import launchd

runner = CliRunner()


def test_root_help_exposes_launchd_group():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "launchd" in result.output


def test_launchd_install_help_mentions_root_config_option(plain_cli_output):
    result = runner.invoke(app, ["launchd", "install", "--help"])

    assert result.exit_code == 0
    assert "--config PATH" in plain_cli_output(result)


def test_python_module_entrypoint_runs_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "linkwatch", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "launchd" in result.stdout


def test_build_agent_plist_persists_explicit_config_path(tmp_path: Path):
    config_path = tmp_path / "config dir" / "probes.toml"

    plist = launchd.build_agent_plist(
        python_executable="/opt/linkwatch/bin/python",
        period=5,
        timeout=2,
        log_path=tmp_path / "connectivity.csv",
        home=tmp_path,
        config_path=config_path,
    )

    assert plist["ProgramArguments"][:7] == [
        "/opt/linkwatch/bin/python",
        "-m",
        "linkwatch",
        "--config",
        str(config_path),
        "monitor",
        "--interval",
    ]


def test_install_writes_per_user_plist_and_bootstraps_without_live_launchctl(
    tmp_path: Path, monkeypatch
):
    agent_path = tmp_path / "Library" / "LaunchAgents" / "com.linkwatch.agent.plist"
    log_path = tmp_path / ".linkwatch" / "custom.csv"
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        if arguments[0] == "print":
            return subprocess.CompletedProcess(arguments, returncode=113, stdout="", stderr="not loaded")
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "AGENT_PATH", agent_path)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchd.sys, "executable", "/opt/linkwatch/bin/python")
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(
        app,
        ["launchd", "install", "--period", "0.75", "--timeout", "1.25", "--log", str(log_path)],
    )

    assert result.exit_code == 0, result.output
    assert agent_path.is_file()
    plist = plistlib.loads(agent_path.read_bytes())
    assert plist["Label"] == launchd.LABEL
    assert plist["ProgramArguments"] == [
        "/opt/linkwatch/bin/python",
        "-m",
        "linkwatch",
        "monitor",
        "--interval",
        "0.75",
        "--timeout",
        "1.25",
        "--log",
        str(log_path),
    ]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert calls == [
        (["print", "gui/501/com.linkwatch.agent"], False),
        (["enable", "gui/501/com.linkwatch.agent"], True),
        (["bootstrap", "gui/501", str(agent_path)], True),
    ]


def test_status_reports_loaded_state_without_modifying_launchd(monkeypatch):
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="loaded", stderr="")

    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(app, ["launchd", "status"])

    assert result.exit_code == 0
    assert "loaded" in result.output.lower()
    assert calls == [(["print", "gui/501/com.linkwatch.agent"], False)]


def test_uninstall_boots_out_and_removes_only_agent_plist(tmp_path: Path, monkeypatch):
    agent_path = tmp_path / "com.linkwatch.agent.plist"
    agent_path.write_text("plist", encoding="utf-8")
    log_path = tmp_path / "connectivity.csv"
    log_path.write_text("preserve", encoding="utf-8")
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="loaded", stderr="")

    monkeypatch.setattr(launchd, "AGENT_PATH", agent_path)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(app, ["launchd", "uninstall"])

    assert result.exit_code == 0, result.output
    assert not agent_path.exists()
    assert log_path.read_text(encoding="utf-8") == "preserve"
    assert calls == [
        (["print", "gui/501/com.linkwatch.agent"], False),
        (["disable", "gui/501/com.linkwatch.agent"], True),
        (["bootout", "gui/501/com.linkwatch.agent"], True),
        (["enable", "gui/501/com.linkwatch.agent"], True),
    ]


def test_start_restarts_loaded_agent(tmp_path: Path, monkeypatch):
    agent_path = tmp_path / "com.linkwatch.agent.plist"
    agent_path.write_text("plist", encoding="utf-8")
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="loaded", stderr="")

    monkeypatch.setattr(launchd, "AGENT_PATH", agent_path)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(app, ["launchd", "start"])

    assert result.exit_code == 0, result.output
    assert calls == [
        (["enable", "gui/501/com.linkwatch.agent"], True),
        (["print", "gui/501/com.linkwatch.agent"], False),
        (["kickstart", "-k", "gui/501/com.linkwatch.agent"], True),
    ]


def test_stop_unloads_a_loaded_agent(monkeypatch):
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="loaded", stderr="")

    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(app, ["launchd", "stop"])

    assert result.exit_code == 0
    assert "Stopped" in result.output
    assert calls == [
        (["print", "gui/501/com.linkwatch.agent"], False),
        (["disable", "gui/501/com.linkwatch.agent"], True),
        (["bootout", "gui/501/com.linkwatch.agent"], True),
    ]


def test_launchd_cli_install_forwards_explicit_config_path(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "probes.toml"
    config_path.write_text(
        '[[targets]]\nname = "Regional"\nurl = "https://regional.example/"\n',
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(launchd, "install_service", lambda *args: calls.append(args) or Path("/agent.plist"))

    result = runner.invoke(app, ["--config", str(config_path), "launchd", "install"])

    assert result.exit_code == 0, result.output
    assert calls[0][-1] == config_path.resolve()


def test_launchd_install_rejects_invalid_config_before_backend_install(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "bad.toml"
    config_path.write_text("targets = []\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        launchd,
        "install_service",
        lambda *args: calls.append(args) or Path("/agent.plist"),
    )

    result = runner.invoke(app, ["--config", str(config_path), "launchd", "install"])

    assert result.exit_code == 1
    assert str(config_path) in result.output
    assert calls == []


def test_install_refuses_to_replace_an_unrelated_existing_plist(tmp_path: Path, monkeypatch):
    agent_path = tmp_path / "com.linkwatch.agent.plist"
    existing = {"Label": "com.someone.else.service", "ProgramArguments": ["other"]}
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_bytes(plistlib.dumps(existing))
    calls = []

    def fake_launchctl(arguments, *, check=True):
        calls.append((arguments, check))
        return subprocess.CompletedProcess(arguments, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd, "AGENT_PATH", agent_path)
    monkeypatch.setattr(launchd, "launchctl", fake_launchctl)

    result = runner.invoke(app, ["launchd", "install"])

    assert result.exit_code == 1
    assert "refusing to replace" in result.output.lower()
    assert plistlib.loads(agent_path.read_bytes()) == existing
    assert calls == []
