import csv
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from linkwatch.chart import generate_chart
from linkwatch.cli import app
from linkwatch.defaults import DEFAULT_LOG
from linkwatch.events import append_event
from linkwatch.monitor import (
    ENDPOINTS,
    PUBLIC_IP_ENDPOINTS,
    find_reachable_endpoint,
    lookup_public_ip,
    monitor,
    probe,
)

runner = CliRunner()


def test_default_log_uses_hidden_home_directory():
    assert DEFAULT_LOG == Path.home() / ".linkwatch" / "connectivity.csv"


def test_cli_help_uses_linkwatch_and_lists_timing_and_log_defaults():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "monitor" in result.output
    assert "chart" in result.output
    assert "launchd" in result.output
    assert "service" in result.output


def test_cli_monitor_help_lists_timing_and_log_options():
    result = runner.invoke(app, ["monitor", "--help"])
    assert result.exit_code == 0
    assert "--interval" in result.output
    assert "--period" in result.output
    assert ".linkwatch" in result.output


def test_cli_chart_help_lists_log_and_output_options():
    result = runner.invoke(app, ["chart", "--help"])
    assert result.exit_code == 0
    assert "--log" in result.output
    assert "--output" in result.output
    assert "--end-time" in result.output
    assert "lifecycle" in result.output.lower()


def test_cli_period_accepts_fractional_seconds_and_defaults_to_canonical_log(monkeypatch):
    received = {}

    def stop_monitor(interval, timeout, log_path):
        received.update(interval=interval, timeout=timeout, log_path=log_path)
        raise KeyboardInterrupt

    monkeypatch.setattr("linkwatch.cli.monitor", stop_monitor)
    monkeypatch.setattr("linkwatch.cli.append_event", lambda *_args, **_kwargs: None)
    result = runner.invoke(app, ["monitor", "--period", "0.25", "--timeout", "1.5"])

    assert result.exit_code == 0
    assert received == {
        "interval": 0.25,
        "timeout": 1.5,
        "log_path": Path.home() / ".linkwatch" / "connectivity.csv",
    }


def test_monitor_cli_logs_start_and_graceful_keyboard_interrupt(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "events.csv"

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("linkwatch.cli.monitor", interrupt)
    result = runner.invoke(app, ["monitor", "--log", str(log_path)])

    assert result.exit_code == 0
    with log_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
    assert reader.fieldnames == [
        "timestamp_utc",
        "event",
        "endpoint",
        "public_ipv4",
        "public_ipv6",
    ]
    assert [row["event"] for row in rows] == ["monitor_started", "monitor_stopped"]
    assert [row["endpoint"] for row in rows] == ["", ""]


def test_monitor_cli_does_not_claim_graceful_stop_after_failure(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "events.csv"

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated monitor failure")

    monkeypatch.setattr("linkwatch.cli.monitor", fail)
    result = runner.invoke(app, ["monitor", "--log", str(log_path)])

    assert result.exit_code == 1
    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["event"] for row in rows] == ["monitor_started"]


def test_monitor_cli_logs_graceful_sigterm_and_restores_handler(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "events.csv"
    original_handler = signal.getsignal(signal.SIGTERM)

    def terminate(*_args, **_kwargs):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr("linkwatch.cli.monitor", terminate)
    result = runner.invoke(app, ["monitor", "--log", str(log_path)])

    assert result.exit_code == 0
    assert signal.getsignal(signal.SIGTERM) is original_handler
    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["event"] for row in rows] == ["monitor_started", "monitor_stopped"]


def test_monitor_cli_does_not_claim_graceful_stop_after_unhandled_error(monkeypatch, tmp_path: Path):
    log_path = tmp_path / "events.csv"

    def fail(*_args, **_kwargs):
        raise RuntimeError("unexpected monitor failure")

    monkeypatch.setattr("linkwatch.cli.monitor", fail)
    result = runner.invoke(app, ["monitor", "--log", str(log_path)])

    assert result.exit_code == 1
    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["event"] for row in rows] == ["monitor_started"]


def test_cli_chart_mode_writes_requested_svg(tmp_path: Path):
    log_path = tmp_path / "events.csv"
    append_event(log_path, "disconnected")
    chart_path = tmp_path / "out" / "history.svg"

    result = runner.invoke(app, ["chart", "--log", str(log_path), "--output", str(chart_path)])

    assert result.exit_code == 0
    assert "Chart written" in result.output
    assert chart_path.read_text(encoding="utf-8").startswith("<svg")


def test_cli_rejects_nonpositive_interval():
    result = runner.invoke(app, ["monitor", "--interval", "0"])
    assert result.exit_code != 0
    assert "Invalid value" in result.output
    assert "--interval" in result.output

def test_probe_treats_transport_failure_as_offline(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr("linkwatch.monitor.urlopen", fail)
    assert probe("https://example.test", timeout=0.1) is False


def test_fallback_tries_providers_in_order_and_stops_on_success():
    attempted = []

    def check(url, _timeout):
        attempted.append(url)
        return url == ENDPOINTS[1].url

    assert find_reachable_endpoint(check=check, timeout=0.1) == ENDPOINTS[1]
    assert attempted == [ENDPOINTS[0].url, ENDPOINTS[1].url]


def test_fallback_returns_none_when_all_providers_fail():
    attempted = []
    assert find_reachable_endpoint(
        check=lambda url, _timeout: attempted.append(url) or False
    ) is None
    assert attempted == [endpoint.url for endpoint in ENDPOINTS]


def test_csv_contains_provider_label_and_only_one_header(tmp_path: Path):
    log_path = tmp_path / "events.csv"
    append_event(log_path, "connected", "Apple")
    append_event(log_path, "disconnected")

    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    assert [row["endpoint"] for row in rows] == ["Apple", ""]
    assert [row["event"] for row in rows] == ["connected", "disconnected"]
    assert all(row["public_ipv4"] == row["public_ipv6"] == "" for row in rows)


def test_csv_migrates_legacy_rows_and_only_keeps_ips_on_connected_rows(tmp_path: Path):
    log_path = tmp_path / "legacy.csv"
    log_path.write_text(
        "timestamp_utc,event,endpoint\n"
        "2026-01-01T00:00:00+00:00,disconnected,\n"
        "2026-01-01T00:01:00+00:00,connected,Apple\n",
        encoding="utf-8",
    )

    append_event(
        log_path,
        "connected",
        "Google",
        public_ipv4="203.0.113.9",
        public_ipv6="2001:db8::9",
    )
    append_event(
        log_path,
        "disconnected",
        public_ipv4="203.0.113.10",
        public_ipv6="2001:db8::10",
    )

    with log_path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        rows = list(reader)

    assert reader.fieldnames == [
        "timestamp_utc",
        "event",
        "endpoint",
        "public_ipv4",
        "public_ipv6",
    ]
    assert [row["event"] for row in rows] == ["disconnected", "connected", "connected", "disconnected"]
    assert rows[0]["public_ipv4"] == rows[0]["public_ipv6"] == ""
    assert rows[1]["public_ipv4"] == rows[1]["public_ipv6"] == ""
    assert rows[2]["public_ipv4"] == "203.0.113.9"
    assert rows[2]["public_ipv6"] == "2001:db8::9"
    assert rows[3]["public_ipv4"] == rows[3]["public_ipv6"] == ""


def test_public_ip_lookup_falls_back_in_order_and_validates_family(monkeypatch):
    attempted = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"2001:4860:4860::8888\n"

    def open_url(request, timeout):
        attempted.append((request.full_url, timeout))
        if len(attempted) == 1:
            raise OSError("primary unavailable")
        return Response()

    monkeypatch.setattr("linkwatch.monitor.urlopen", open_url)

    assert lookup_public_ip(6, timeout=0.75) == "2001:4860:4860::8888"
    assert [url for url, _ in attempted] == list(PUBLIC_IP_ENDPOINTS[6])
    assert all(timeout == 0.75 for _, timeout in attempted)


def test_public_ip_lookup_rejects_wrong_family_and_tries_fallback(monkeypatch):
    attempted = []

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    def open_url(request, timeout):
        attempted.append(request.full_url)
        return Response(b"2001:4860:4860::8888\n" if len(attempted) == 1 else b"8.8.8.8\n")

    monkeypatch.setattr("linkwatch.monitor.urlopen", open_url)

    assert lookup_public_ip(4, timeout=1) == "8.8.8.8"
    assert attempted == list(PUBLIC_IP_ENDPOINTS[4])


def test_public_ip_lookup_returns_none_when_all_endpoints_fail(monkeypatch):
    attempted = []

    def fail(request, timeout):
        attempted.append(request.full_url)
        raise OSError("unavailable")

    monkeypatch.setattr("linkwatch.monitor.urlopen", fail)

    assert lookup_public_ip(4, timeout=1) is None
    assert attempted == list(PUBLIC_IP_ENDPOINTS[4])


def test_monitor_logs_only_connectivity_transitions(monkeypatch, tmp_path: Path):
    outcomes = iter([None, ENDPOINTS[2], ENDPOINTS[2], None, KeyboardInterrupt()])

    def lookup(**_kwargs):
        result = next(outcomes)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("linkwatch.monitor.find_reachable_endpoint", lookup)
    log_path = tmp_path / "events.csv"
    with pytest.raises(KeyboardInterrupt):
        monitor(
            interval=1,
            timeout=1,
            log_path=log_path,
            sleep=lambda _seconds: None,
            lookup_ip=lambda version, _timeout: "203.0.113.1" if version == 4 else "2001:db8::1",
        )

    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["event"] for row in rows] == ["disconnected", "connected", "disconnected"]
    assert [row["endpoint"] for row in rows] == ["", "Microsoft", ""]


def test_monitor_looks_up_public_ips_only_on_reconnection(monkeypatch, tmp_path: Path):
    outcomes = iter([None, ENDPOINTS[0], ENDPOINTS[0], None, ENDPOINTS[1], KeyboardInterrupt()])
    calls = []

    def check(**_kwargs):
        result = next(outcomes)
        if isinstance(result, BaseException):
            raise result
        return result

    def lookup(version, timeout):
        calls.append((version, timeout))
        return "203.0.113.5" if version == 4 else "2001:db8::5"

    monkeypatch.setattr("linkwatch.monitor.find_reachable_endpoint", check)
    log_path = tmp_path / "events.csv"
    with pytest.raises(KeyboardInterrupt):
        monitor(
            interval=1,
            timeout=1.25,
            log_path=log_path,
            sleep=lambda _seconds: None,
            lookup_ip=lookup,
        )

    with log_path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["event"] for row in rows] == ["disconnected", "connected", "disconnected", "connected"]
    assert [row["public_ipv4"] for row in rows] == ["", "203.0.113.5", "", "203.0.113.5"]
    assert [row["public_ipv6"] for row in rows] == ["", "2001:db8::5", "", "2001:db8::5"]
    assert calls == [(4, 1.25), (6, 1.25), (4, 1.25), (6, 1.25)]


def test_chart_writes_svg_connectivity_timeline(tmp_path: Path):
    log_path = tmp_path / "events.csv"
    chart_path = tmp_path / "charts" / "history.svg"
    start = datetime.now(timezone.utc) - timedelta(minutes=2)
    with log_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("timestamp_utc", "event", "endpoint"))
        writer.writeheader()
        writer.writerows(
            [
                {"timestamp_utc": start.isoformat(), "event": "disconnected", "endpoint": ""},
                {"timestamp_utc": (start + timedelta(minutes=1)).isoformat(), "event": "connected", "endpoint": "Apple"},
            ]
        )

    generate_chart(log_path, chart_path)

    svg = chart_path.read_text(encoding="utf-8")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"')
    assert "#d64b4b" in svg
    assert "#2e9d59" in svg
    assert "Internet connectivity" in svg
    assert "Monitor start" not in svg
    assert "Graceful stop" not in svg


def test_chart_marks_monitor_runs_and_leaves_stopped_gaps_unknown(tmp_path: Path):
    log_path = tmp_path / "events.csv"
    chart_path = tmp_path / "history.svg"
    start = datetime.now(timezone.utc) - timedelta(minutes=6)
    timeline = [
        (start, "monitor_started", ""),
        (start + timedelta(minutes=1), "disconnected", ""),
        (start + timedelta(minutes=2), "connected", "Apple"),
        (start + timedelta(minutes=3), "monitor_stopped", ""),
        (start + timedelta(minutes=4), "monitor_started", ""),
        (start + timedelta(minutes=5), "disconnected", ""),
    ]
    with log_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("timestamp_utc", "event", "endpoint"))
        writer.writeheader()
        writer.writerows(
            {"timestamp_utc": timestamp.isoformat(), "event": event, "endpoint": endpoint}
            for timestamp, event, endpoint in timeline
        )

    generate_chart(log_path, chart_path)
    svg = chart_path.read_text(encoding="utf-8")

    assert "Monitoring started" in svg
    assert "Graceful stop" in svg
    assert "Not monitored / unknown" in svg
    assert svg.count('fill="#d64b4b"') == 2
    assert svg.count('fill="#2e9d59"') == 1


def test_chart_renders_lifecycle_only_log(tmp_path: Path):
    log_path = tmp_path / "events.csv"
    chart_path = tmp_path / "history.svg"
    started = datetime.now(timezone.utc) - timedelta(minutes=2)
    with log_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=("timestamp_utc", "event", "endpoint"))
        writer.writeheader()
        writer.writerows(
            [
                {"timestamp_utc": started.isoformat(), "event": "monitor_started", "endpoint": ""},
                {
                    "timestamp_utc": (started + timedelta(minutes=1)).isoformat(),
                    "event": "monitor_stopped",
                    "endpoint": "",
                },
            ]
        )

    generate_chart(log_path, chart_path)
    svg = chart_path.read_text(encoding="utf-8")
    assert "Monitoring started" in svg
    assert "Graceful stop" in svg
    assert 'fill="#2e9d59"' not in svg
    assert 'fill="#d64b4b"' not in svg


def test_chart_rejects_empty_log(tmp_path: Path):
    log_path = tmp_path / "empty.csv"
    log_path.write_text("timestamp_utc,event,endpoint\\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no connectivity events"):
        generate_chart(log_path, tmp_path / "history.svg")


def test_monitor_rejects_nonpositive_timing():
    with pytest.raises(ValueError, match="interval"):
        monitor(interval=0)
    with pytest.raises(ValueError, match="timeout"):
        monitor(timeout=0)
