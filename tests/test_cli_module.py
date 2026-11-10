from typer.testing import CliRunner

from linkwatch.cli import app


def test_cli_module_exposes_the_application_commands():
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0, result.output
    assert "monitor" in result.output
    assert "chart" in result.output
    assert "calendar" in result.output
    assert "service" in result.output
    assert "launchd" in result.output
