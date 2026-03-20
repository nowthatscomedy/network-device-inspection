from __future__ import annotations

import re

import pandas as pd

import main
from core.i18n import set_locale
from core.settings import AppSettings


def test_read_excel_with_retry_returns_dataframe_without_password(monkeypatch) -> None:
    expected = pd.DataFrame([{"ip": "192.0.2.10"}])
    monkeypatch.setattr(main, "read_excel_file", lambda filepath, password=None: expected)

    result = main._read_excel_with_retry("devices.xlsx")
    pd.testing.assert_frame_equal(result, expected)


def test_read_excel_with_retry_uses_password_on_second_try(monkeypatch) -> None:
    expected = pd.DataFrame([{"ip": "192.0.2.11"}])
    call_count = {"count": 0}

    def fake_read(filepath: str, password: str | None = None):
        call_count["count"] += 1
        if password is None:
            raise ValueError("encrypted workbook")
        assert password == "secret"
        return expected

    monkeypatch.setattr(main, "read_excel_file", fake_read)
    monkeypatch.setattr(main, "get_password_from_cli", lambda: "secret")

    result = main._read_excel_with_retry("devices.xlsx")
    pd.testing.assert_frame_equal(result, expected)
    assert call_count["count"] == 2


def test_read_excel_with_retry_returns_none_when_password_not_entered(monkeypatch) -> None:
    monkeypatch.setattr(main, "read_excel_file", lambda filepath, password=None: (_ for _ in ()).throw(ValueError("encrypted workbook")))
    monkeypatch.setattr(main, "get_password_from_cli", lambda: None)

    result = main._read_excel_with_retry("devices.xlsx")
    assert result is None


def test_create_inspector_applies_settings_fields() -> None:
    settings = AppSettings(
        inspection_excludes={"cisco": {"ios": ["show version"]}},
        max_retries=5,
        timeout=15,
        max_workers=4,
        column_aliases={"host name": "Hostname"},
    )

    inspector = main._create_inspector(
        output_excel="inspection_results.xlsx",
        run_timestamp="20260101_120000",
        settings=settings,
        inspection_only=True,
    )

    assert inspector.inspection_only is True
    assert inspector.backup_only is False
    assert inspector.max_retries == 5
    assert inspector.timeout == 15
    assert inspector.max_workers == 4
    assert inspector.inspection_excludes == {"cisco": {"ios": ["show version"]}}
    assert inspector.column_aliases["host name"] == "Hostname"
    assert inspector.output_excel.endswith("inspection_results_20260101_120000.xlsx")


def test_init_run_returns_timestamp_and_logfile(monkeypatch) -> None:
    monkeypatch.setattr(main, "init_logging", lambda **kwargs: "logs/test.log")
    settings = AppSettings(console_log_level="INFO")

    run_timestamp, log_file = main._init_run(settings)
    assert re.fullmatch(r"\d{8}_\d{6}", run_timestamp) is not None
    assert log_file == "logs/test.log"


def test_build_mode_label_joins_selected_actions_in_fixed_order() -> None:
    set_locale("en", "en")

    assert main._build_mode_label(["custom_commands", "inspection"]) == (
        "Inspection + Batch Command Input"
    )
    assert main._build_mode_label(["inspection", "backup"]) == "Inspection+Backup"


def test_analyze_custom_command_payload_detects_config_change_and_session_risk() -> None:
    analysis = main._analyze_custom_command_payload(
        [
            "show version",
            "interface vlan 99",
            " ip address 10.0.0.11 255.255.255.0",
            "ip route 0.0.0.0 0.0.0.0 10.0.0.1",
        ]
    )

    assert analysis["total_commands"] == 4
    assert analysis["config_change_count"] == 3
    assert analysis["session_impact_count"] == 2


def test_recommend_action_order_prefers_post_change_validation_for_safe_changes() -> None:
    order = main._recommend_action_order(
        ["inspection", "backup", "custom_commands"],
        {"config_change_count": 2, "session_impact_count": 0},
    )
    assert order == ["backup", "custom_commands", "inspection"]


def test_build_execution_warning_lines_highlights_pre_change_inspection_and_session_risk() -> None:
    set_locale("en", "en")

    warnings = main._build_execution_warning_lines(
        ["custom_commands", "inspection", "backup"],
        {
            "config_change_count": 2,
            "session_impact_count": 1,
            "session_impact_examples": ["ip address 10.0.0.11 255.255.255.0"],
            "config_change_examples": ["hostname EDGE-01"],
        },
    )

    assert any("session may drop" in warning.lower() for warning in warnings)
    assert any("inspection can verify" in warning.lower() for warning in warnings)
    assert any("backup file will capture" in warning.lower() for warning in warnings)
