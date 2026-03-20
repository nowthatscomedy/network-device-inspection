from __future__ import annotations

import time
from typing import Any

import pytest

from core.inspector import NetworkInspector


@pytest.fixture()
def inspector(tmp_path, monkeypatch) -> NetworkInspector:
    monkeypatch.chdir(tmp_path)
    return NetworkInspector(
        output_excel="inspection_results.xlsx",
        run_timestamp="20260101_010101",
        max_retries=2,
        timeout=1,
        max_workers=2,
        column_aliases={"host name": "Hostname", "hostname": "Hostname"},
    )


def test_canonicalize_result_columns_merges_alias(inspector: NetworkInspector) -> None:
    merged = inspector._canonicalize_result_columns(
        {
            "host name": "edge-sw-1",
            "Hostname": "edge-sw-1-alt",
            "CPU Usage": "10%",
        }
    )
    assert merged["Hostname"] == "edge-sw-1, edge-sw-1-alt"
    assert merged["CPU Usage"] == "10%"


def test_get_output_columns_for_known_command(inspector: NetworkInspector) -> None:
    columns = inspector._get_output_columns_for_command("cisco", "ios", "show env all")
    assert columns == ["Fan Status", "System Temperature"]


def test_get_available_inspection_columns_respects_excludes(
    inspector: NetworkInspector,
) -> None:
    inspector.inspection_excludes = {
        "cisco": {"ios": ["show env all::Fan Status", "show version | include uptime"]}
    }
    devices = [{"vendor": "cisco", "os": "ios"}]

    columns = inspector.get_available_inspection_columns(devices)
    assert "Hostname" in columns
    assert "System Temperature" in columns
    assert "CPU Usage" in columns
    assert "Memory Usage" in columns
    assert "Fan Status" not in columns
    assert "Uptime" not in columns


def test_parse_command_output_custom_parser_and_parse_id_exclude(
    inspector: NetworkInspector,
) -> None:
    parsed_hostname = inspector._parse_command_output(
        "cisco",
        "ios",
        "show running-config",
        'hostname "dist-sw-1"',
    )
    assert parsed_hostname["Hostname"].strip('"') == "dist-sw-1"

    inspector.inspection_excludes = {"cisco": {"ios": ["show env all::Fan Status"]}}
    parsed_env = inspector._parse_command_output(
        "cisco",
        "ios",
        "show env all",
        "FAN 1 is OK\nTEMPERATURE is GREEN\n",
    )
    assert "Fan Status" not in parsed_env
    assert parsed_env["System Temperature"] == "GREEN"


def test_connect_to_device_returns_error_when_tcp_test_fails(
    inspector: NetworkInspector, sample_device: dict[str, Any], monkeypatch
) -> None:
    monkeypatch.setattr(inspector, "_test_tcping", lambda ip, port: False)
    _, result = inspector._connect_to_device(
        dict(sample_device),
        inspection_mode=True,
        backup_mode=False,
    )
    assert "error" in result
    assert "TCP" in str(result["error"])


def test_inspect_device_marks_status_error_on_connection_failure(
    inspector: NetworkInspector, sample_device: dict[str, Any], monkeypatch
) -> None:
    def fake_connect(device: dict[str, Any], *args, **kwargs):
        return device, {"error": "auth failed"}

    monkeypatch.setattr(inspector, "_connect_to_device", fake_connect)
    result = inspector._inspect_device(dict(sample_device))

    assert result["status"] == "error"
    assert result["error_message"] == "auth failed"
    assert result["_elapsed_seconds"] >= 0


def test_backup_device_propagates_backup_error(
    inspector: NetworkInspector, sample_device: dict[str, Any], monkeypatch
) -> None:
    def fake_connect(device: dict[str, Any], *args, **kwargs):
        return device, {"backup_error": "disk full"}

    monkeypatch.setattr(inspector, "_connect_to_device", fake_connect)
    result = inspector._backup_device(dict(sample_device))

    assert result["status"] == "error"
    assert result["error_message"] == "disk full"


def test_inspect_devices_sorts_results_by_input_order(
    inspector: NetworkInspector, monkeypatch
) -> None:
    devices = [
        {
            "ip": "192.0.2.11",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
        {
            "ip": "192.0.2.10",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
    ]
    inspector.load_devices(devices)

    def fake_inspect(device: dict[str, Any]) -> dict[str, Any]:
        if device["ip"] == "192.0.2.11":
            time.sleep(0.05)
        return {
            "ip": device["ip"],
            "vendor": device["vendor"],
            "os": device["os"],
            "status": "success",
            "error_message": "",
            "inspection_results": {},
            "_elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(inspector, "_inspect_device", fake_inspect)
    monkeypatch.setattr(inspector, "_print_cli_status", lambda message: None)
    monkeypatch.setattr(inspector, "_emit_status_event", lambda *args, **kwargs: None)

    inspector.inspect_devices()
    assert [row["ip"] for row in inspector.results] == ["192.0.2.11", "192.0.2.10"]


def test_inspect_and_backup_devices_uses_single_session_path_for_all_devices(
    inspector: NetworkInspector, monkeypatch
) -> None:
    device = {
        "ip": "192.0.2.21",
        "vendor": "cisco",
        "os": "ios",
        "connection_type": "ssh",
        "port": 22,
        "username": "u",
        "password": "p",
    }
    inspector.load_devices([device])

    calls = {"combined": 0, "inspect": 0, "backup": 0}

    def fake_combined(target: dict[str, Any], on_inspection_done=None, on_backup_done=None):
        calls["combined"] += 1
        if on_inspection_done:
            on_inspection_done(target["ip"], True)
        if on_backup_done:
            on_backup_done(target["ip"], True)
        return {
            "ip": target["ip"],
            "vendor": target["vendor"],
            "os": target["os"],
            "status": "success",
            "error_message": "",
            "inspection_results": {"Hostname": "sw1"},
            "backup_file": "backup/sw1.txt",
            "_elapsed_seconds": 0.25,
        }

    def fail_inspect(*args, **kwargs):
        calls["inspect"] += 1
        raise AssertionError("single-session device should not use split inspect path")

    def fail_backup(*args, **kwargs):
        calls["backup"] += 1
        raise AssertionError("inspect_and_backup should not use split backup path")

    monkeypatch.setattr(inspector, "_inspect_and_backup_device", fake_combined)
    monkeypatch.setattr(inspector, "_inspect_device", fail_inspect)
    monkeypatch.setattr(inspector, "_backup_device", fail_backup)
    monkeypatch.setattr(inspector, "_print_cli_status", lambda message: None)
    monkeypatch.setattr(inspector, "_emit_status_event", lambda *args, **kwargs: None)

    inspector.inspect_and_backup_devices()

    assert calls == {"combined": 1, "inspect": 0, "backup": 0}
    assert inspector.results[0]["status"] == "success"
    assert inspector.results[0]["backup_file"] == "backup/sw1.txt"


def test_run_custom_commands_accepts_per_device_command_map(
    inspector: NetworkInspector,
    monkeypatch,
) -> None:
    devices = [
        {
            "ip": "192.0.2.11",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
        {
            "ip": "192.0.2.10",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
    ]
    inspector.load_devices(devices)
    captured: dict[str, list[str]] = {}

    def fake_run_device(device: dict[str, Any], commands: list[str], session_log_suffix=None):
        captured[device["ip"]] = list(commands)
        return {
            "ip": device["ip"],
            "vendor": device["vendor"],
            "os": device["os"],
            "status": "success",
            "error_message": "",
            "inspection_results": {"custom_commands_executed": len(commands)},
            "_elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(inspector, "_run_custom_commands_device", fake_run_device)
    monkeypatch.setattr(inspector, "_print_cli_status", lambda message: None)
    monkeypatch.setattr(inspector, "_emit_status_event", lambda *args, **kwargs: None)

    inspector.run_custom_commands(
        {
            "192.0.2.11": ["show version"],
            "192.0.2.10": ["show ip interface brief", "show inventory"],
        }
    )

    assert captured["192.0.2.11"] == ["show version"]
    assert captured["192.0.2.10"] == ["show ip interface brief", "show inventory"]


def test_run_selected_actions_accepts_per_device_command_map(
    inspector: NetworkInspector,
    monkeypatch,
) -> None:
    devices = [
        {
            "ip": "192.0.2.11",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
        {
            "ip": "192.0.2.10",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "u",
            "password": "p",
        },
    ]
    inspector.load_devices(devices)
    captured: dict[str, list[str]] = {}

    def fake_run_device(
        device: dict[str, Any],
        *,
        inspection_mode: bool,
        backup_mode: bool,
        custom_commands: list[str] | None = None,
        session_log_suffix=None,
    ):
        assert inspection_mode is True
        assert backup_mode is True
        captured[device["ip"]] = list(custom_commands or [])
        return {
            "ip": device["ip"],
            "vendor": device["vendor"],
            "os": device["os"],
            "status": "success",
            "error_message": "",
            "inspection_results": {"custom_commands_executed": len(custom_commands or [])},
            "backup_file": "backup/test.txt",
            "_elapsed_seconds": 0.01,
        }

    monkeypatch.setattr(inspector, "_run_selected_actions_device", fake_run_device)
    monkeypatch.setattr(inspector, "_print_cli_status", lambda message: None)
    monkeypatch.setattr(inspector, "_emit_status_event", lambda *args, **kwargs: None)

    inspector.run_selected_actions(
        inspection_mode=True,
        backup_mode=True,
        custom_commands={
            "192.0.2.11": ["show version"],
            "192.0.2.10": ["show ip interface brief", "show inventory"],
        },
    )

    assert captured["192.0.2.11"] == ["show version"]
    assert captured["192.0.2.10"] == ["show ip interface brief", "show inventory"]


def test_run_selected_actions_device_collects_backup_and_command_metadata(
    inspector: NetworkInspector,
    sample_device: dict[str, Any],
    monkeypatch,
) -> None:
    device = dict(sample_device)
    device["_custom_command_profile_id"] = "PROFILE_A"
    device["_custom_command_rendered_count"] = 2
    device["_custom_command_values_path"] = "values.csv"

    def fake_connect(target: dict[str, Any], *args, **kwargs):
        assert kwargs["inspection_mode"] is True
        assert kwargs["backup_mode"] is True
        assert kwargs["custom_commands"] == ["cmd1", "cmd2"]
        return target, {
            "Hostname": "sw1",
            "custom_commands_executed": 2,
            "backup_file": "backup/sw1.txt",
        }

    monkeypatch.setattr(inspector, "_connect_to_device", fake_connect)

    result = inspector._run_selected_actions_device(
        device,
        inspection_mode=True,
        backup_mode=True,
        custom_commands=["cmd1", "cmd2"],
    )

    assert result["status"] == "success"
    assert result["backup_file"] == "backup/sw1.txt"
    assert result["inspection_results"]["Hostname"] == "sw1"
    assert result["inspection_results"]["custom_commands_executed"] == 2
    assert result["inspection_results"]["Command Profile ID"] == "PROFILE_A"
    assert result["inspection_results"]["Rendered Command Count"] == 2
    assert result["inspection_results"]["Template Values File"] == "values.csv"


def test_run_custom_commands_device_includes_profile_metadata(
    inspector: NetworkInspector,
    sample_device: dict[str, Any],
    monkeypatch,
) -> None:
    device = dict(sample_device)
    device["_custom_command_profile_id"] = "PROFILE_A"
    device["_custom_command_rendered_count"] = 2
    device["_custom_command_values_path"] = "values.csv"

    def fake_connect(target: dict[str, Any], *args, **kwargs):
        assert kwargs["custom_commands"] == ["cmd1", "cmd2"]
        return target, {"custom_commands_executed": 2}

    monkeypatch.setattr(inspector, "_connect_to_device", fake_connect)

    result = inspector._run_custom_commands_device(device, ["cmd1", "cmd2"])

    assert result["status"] == "success"
    assert result["inspection_results"]["custom_commands_executed"] == 2
    assert result["inspection_results"]["Command Profile ID"] == "PROFILE_A"
    assert result["inspection_results"]["Rendered Command Count"] == 2
    assert result["inspection_results"]["Template Values File"] == "values.csv"
