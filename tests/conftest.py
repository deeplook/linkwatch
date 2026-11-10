import re

import pytest

from linkwatch import cli, launchd, service

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@pytest.fixture
def plain_cli_output():
    """Return CLI output with terminal styling removed for stable assertions."""
    return lambda result: ANSI_ESCAPE_RE.sub("", result.output)


@pytest.fixture(autouse=True)
def isolate_default_probe_config(monkeypatch, tmp_path):
    """Keep CLI/service tests independent of the invoking user's config file."""
    default_path = tmp_path / "missing-linkwatch-config.toml"
    for module in (cli, launchd, service):
        original_loader = module.load_config
        monkeypatch.setattr(
            module,
            "load_config",
            lambda config_path=None, _loader=original_loader: _loader(
                config_path,
                default_path=default_path,
            ),
        )
