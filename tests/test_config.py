from pathlib import Path

import pytest
from typer.testing import CliRunner

from linkwatch.cli import app
from linkwatch.config import ConfigError, load_config, redact_url
from linkwatch.monitor import ENDPOINTS, Endpoint

runner = CliRunner()


def test_missing_default_config_uses_builtin_targets(tmp_path: Path):
    path = tmp_path / "config.toml"

    config = load_config(default_path=path)

    assert config.targets == ENDPOINTS
    assert config.path == path.resolve()
    assert config.source == "built-in defaults"
    assert config.explicit is False


def test_missing_explicit_config_is_an_error(tmp_path: Path):
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match="not found"):
        load_config(path, default_path=tmp_path / "default.toml")


def test_configured_targets_replace_defaults_and_preserve_order(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[[targets]]\nname = "First"\nurl = "https://one.example/check"\n\n'
        '[[targets]]\nname = "Second"\nurl = "https://two.example/check"\n',
        encoding="utf-8",
    )

    config = load_config(path, default_path=tmp_path / "unused.toml")

    assert config.targets == (
        Endpoint("First", "https://one.example/check"),
        Endpoint("Second", "https://two.example/check"),
    )
    assert config.targets != ENDPOINTS
    assert config.path == path.resolve()
    assert config.source == "configuration file"
    assert config.explicit is True


@pytest.mark.parametrize(
    "contents",
    [
        "not valid TOML =",
        "",
        "other = true\n",
        "targets = []\n",
        '[[targets]]\nname = ""\nurl = "https://one.example/"\n',
        '[[targets]]\nname = "One"\nurl = "ftp://one.example/"\n',
        '[[targets]]\nname = "One"\nurl = "https:///missing-host"\n',
        '[[targets]]\nname = "One"\nurl = "https://user:pass@one.example/"\n',
        '[[targets]]\nname = "One"\nurl = "https://one.example/"\nextra = true\n',
    ],
)
def test_invalid_config_is_rejected_with_path_context(tmp_path: Path, contents: str):
    path = tmp_path / "bad.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_config(path, default_path=tmp_path / "unused.toml")

    assert str(path) in str(error.value)


def test_target_names_must_be_unique_case_insensitively(tmp_path: Path):
    path = tmp_path / "duplicate.toml"
    path.write_text(
        '[[targets]]\nname = "Office"\nurl = "https://one.example/"\n\n'
        '[[targets]]\nname = "office"\nurl = "https://two.example/"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unique"):
        load_config(path, default_path=tmp_path / "unused.toml")


def test_config_path_reports_explicit_selection_and_file_existence(tmp_path: Path, plain_cli_output):
    path = tmp_path / "probes.toml"
    path.write_text('[[targets]]\nname = "Office"\nurl = "https://office.example/"\n', encoding="utf-8")

    result = runner.invoke(app, ["--config", str(path), "config", "path"])

    assert result.exit_code == 0, result.output
    output = plain_cli_output(result)
    assert str(path.resolve()) in output
    assert "command line" in output.lower()
    assert "exists" in output.lower()


def test_config_show_lists_custom_targets_and_redacts_query_values(tmp_path: Path, plain_cli_output):
    path = tmp_path / "probes.toml"
    path.write_text(
        '[[targets]]\nname = "Office"\nurl = "https://office.example/check?token=secret#signature=fragment-secret"\n',
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--config", str(path), "config", "show"])

    assert result.exit_code == 0, result.output
    output = plain_cli_output(result)
    assert "Office" in output
    assert "https://office.example/check?[redacted]#[redacted]" in output
    assert "secret" not in output
    assert "fragment-secret" not in output


def test_redact_url_preserves_url_without_query_or_fragment():
    assert redact_url("https://office.example/check") == "https://office.example/check"


def test_config_show_uses_builtin_targets_when_default_file_is_absent(plain_cli_output):
    result = runner.invoke(app, ["config", "show"])

    assert result.exit_code == 0, result.output
    output = plain_cli_output(result)
    assert "built-in defaults" in output
    assert "Google" in output
    assert "Apple" in output
    assert "Microsoft" in output


def test_config_validate_is_read_only_and_never_probes(monkeypatch, tmp_path: Path, plain_cli_output):
    path = tmp_path / "probes.toml"
    path.write_text('[[targets]]\nname = "Office"\nurl = "https://office.example/"\n', encoding="utf-8")

    def no_probe(*_args, **_kwargs):
        raise AssertionError("configuration validation must not make network requests")

    monkeypatch.setattr("linkwatch.monitor.probe", no_probe)
    result = runner.invoke(app, ["--config", str(path), "config", "validate"])

    assert result.exit_code == 0, result.output
    output = plain_cli_output(result)
    assert "valid" in output.lower()
    assert "1 target" in output.lower()


def test_config_validate_reports_invalid_explicit_file(tmp_path: Path, plain_cli_output):
    path = tmp_path / "bad.toml"
    path.write_text("targets = []\n", encoding="utf-8")

    result = runner.invoke(app, ["--config", str(path), "config", "validate"])

    assert result.exit_code == 1
    output = plain_cli_output(result)
    assert str(path) in output
    assert "Error" in output
