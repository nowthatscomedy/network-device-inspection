from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from zipfile import BadZipFile

import pandas as pd
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.cli_input import (
    get_command_filepath_from_cli,
    get_filepath_from_cli,
    get_template_values_filepath_from_cli,
)
from core.command_templates import build_profile_command_payload, is_profile_command_path
from core.custom_exceptions import NetworkInspectorError
from core.file_handler import read_command_file, read_excel_file, save_results_to_excel
from core.i18n import set_locale, t
from core.inspector import NetworkInspector
from core.logging_config import init_logging
from core.menu_i18n import (
    ask_yes_no,
    show_action_menu,
    show_main_menu,
    show_netmiko_device_types,
    show_settings_menu,
)
from core.settings import AppSettings, load_settings, resolve_inspection_column_order
from core.tui_dashboard import TuiDashboard
from core.ui import get_password_from_cli
from core.validator import validate_dataframe

logger = logging.getLogger(__name__)
console = Console()


def _read_excel_with_retry(filepath: str) -> pd.DataFrame | None:
    try:
        return read_excel_file(filepath)
    except (BadZipFile, Exception):
        password = get_password_from_cli()
        if not password:
            logger.warning(t("main.warning.password_not_entered"))
            return None
        try:
            return read_excel_file(filepath, password=password)
        except Exception as exc:
            logger.error(t("main.warning.encrypted_excel_read_failed", error=exc))
            return None


def _create_inspector(
    output_excel: str,
    run_timestamp: str,
    settings: AppSettings,
    *,
    inspection_only: bool = False,
    backup_only: bool = False,
    status_callback: Callable[[dict[str, object]], None] | None = None,
) -> NetworkInspector:
    return NetworkInspector(
        output_excel,
        inspection_only=inspection_only,
        backup_only=backup_only,
        run_timestamp=run_timestamp,
        inspection_excludes=settings.inspection_excludes,
        max_retries=settings.max_retries,
        timeout=settings.timeout,
        max_workers=settings.max_workers,
        column_aliases=settings.column_aliases,
        status_callback=status_callback,
    )


def _init_run(settings: AppSettings) -> tuple[str, str]:
    console_level = getattr(logging, settings.console_log_level, logging.INFO)
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = init_logging(
        run_timestamp=run_timestamp,
        console_level=console_level,
        file_level=logging.DEBUG,
        enable_color=True,
    )
    return run_timestamp, log_file


def _print_run_summary(
    mode: str,
    device_count: int,
    filepath: str,
    settings: AppSettings,
    run_timestamp: str,
    log_file: str,
) -> None:
    table = Table(border_style="dim", expand=False, show_header=False)
    table.add_column(t("main.run_summary.item"), style="cyan")
    table.add_column(t("main.run_summary.value"), style="bold")
    table.add_row("RUN ID", run_timestamp)
    table.add_row(t("main.run_summary.mode"), mode)
    table.add_row(t("main.run_summary.devices"), str(device_count))
    table.add_row(t("main.run_summary.file"), filepath)
    table.add_row(t("main.run_summary.timeout"), str(settings.timeout))
    table.add_row(t("main.run_summary.max_retries"), str(settings.max_retries))
    table.add_row(t("main.run_summary.max_workers"), str(settings.max_workers))
    table.add_row(t("main.run_summary.log_file"), log_file)
    console.print(
        Panel(
            table,
            title=f"[bold cyan]{t('main.run_summary.title')}[/bold cyan]",
            border_style="green",
            expand=False,
        ),
    )


def _print_result_summary(inspector: NetworkInspector, log_file: str) -> None:
    console.print()
    if inspector.results:
        console.print(f"[bold green]{t('main.result.completed')}[/bold green]")
        console.print(f"  {t('main.result.result_file')}: {inspector.output_excel}")
        if not inspector.inspection_only:
            console.print(f"  {t('main.result.backup_dir')}: {inspector.backup_dir}")
        console.print(f"  {t('main.result.session_log')}: {inspector.session_log_dir}")
        console.print(f"  {t('main.result.log_file')}: {log_file}")
    else:
        console.print(f"[yellow]{t('main.result.no_results')}[/yellow]")
        console.print(f"  {t('main.result.session_log')}: {inspector.session_log_dir}")
        console.print(f"  {t('main.result.log_file')}: {log_file}")

    console.print()
    input(t("main.prompts.return_main_menu"))


def _build_mode_label(action_choices: list[str]) -> str:
    selected = set(action_choices)
    if selected == {"inspection", "backup"}:
        return t("main.modes.inspection_backup")

    labels = {
        "inspection": t("main.modes.inspection"),
        "backup": t("main.modes.backup"),
        "custom_commands": t("main.modes.custom_commands"),
    }
    ordered_labels = [
        labels[action]
        for action in ("inspection", "backup", "custom_commands")
        if action in selected
    ]
    return " + ".join(ordered_labels)


def _load_inventory_devices(settings: AppSettings) -> tuple[str, list[dict[str, object]]] | None:
    filepath = get_filepath_from_cli()
    if not filepath:
        logger.warning(t("main.warning.input_path_missing"))
        return
    logger.info("INPUT   : %s", filepath)

    devices_df = _read_excel_with_retry(filepath)
    if devices_df is None:
        return
    devices_df = validate_dataframe(devices_df, settings.input_column_aliases)
    return filepath, devices_df.to_dict("records")


def _load_custom_command_payload(
    devices: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[str] | dict[str, list[str]]] | None:
    vendor_os_pairs = {
        (
            str(device.get("vendor", "")).strip().lower(),
            str(device.get("os", "")).strip().lower(),
        )
        for device in devices
    }
    if len(vendor_os_pairs) > 1:
        if not ask_yes_no(t("main.confirm.mixed_vendor_os")):
            logger.info(t("main.info.custom_commands_cancelled"))
            return None

    command_path = get_command_filepath_from_cli()
    if not command_path:
        logger.warning(t("main.warning.command_path_missing"))
        return None
    logger.info("COMMAND FILE : %s", command_path)

    rendered_commands: list[str] | dict[str, list[str]]
    if is_profile_command_path(command_path):
        template_values_path = get_template_values_filepath_from_cli()
        if template_values_path:
            logger.info("TEMPLATE VALUES FILE : %s", template_values_path)

        try:
            devices, rendered_commands, profile = build_profile_command_payload(
                command_path,
                devices,
                template_values_path=template_values_path,
            )
        except ValueError as exc:
            logger.error(t("main.warning.template_render_failed", error=exc))
            return None
        except Exception as exc:
            logger.error(t("main.warning.command_profile_read_failed", error=exc))
            return None

        logger.info("COMMAND PROFILE ID : %s", profile.id)
    else:
        try:
            rendered_commands = read_command_file(command_path)
        except Exception as exc:
            logger.error(t("main.warning.command_file_read_failed", error=exc))
            return None

        if not rendered_commands:
            logger.warning(t("main.warning.command_list_empty"))
            return None

    return devices, rendered_commands


def _run_selected_actions(settings: AppSettings) -> None:
    action_choices = show_action_menu()
    if not action_choices:
        return

    run_inspection = "inspection" in action_choices
    run_backup = "backup" in action_choices
    run_custom_commands = "custom_commands" in action_choices

    if not any((run_inspection, run_backup, run_custom_commands)):
        return

    mode_label = _build_mode_label(action_choices)

    run_timestamp, log_file = _init_run(settings)
    output_excel = (
        "command_results.xlsx"
        if run_custom_commands and not (run_inspection or run_backup)
        else "inspection_results.xlsx"
    )

    logger.info("RUN ID   : %s", run_timestamp)
    logger.info("LOG FILE : %s", log_file)
    logger.info("-----------------------------------------")
    logger.info(t("main.info.mode_log_prefix", mode=mode_label))

    inventory = _load_inventory_devices(settings)
    if inventory is None:
        return
    filepath, devices = inventory

    rendered_commands: list[str] | dict[str, list[str]] | None = None
    if run_custom_commands:
        command_payload = _load_custom_command_payload(devices)
        if command_payload is None:
            return
        devices, rendered_commands = command_payload

    dashboard = TuiDashboard(mode_label, len(devices))
    inspector = _create_inspector(
        output_excel,
        run_timestamp,
        settings,
        inspection_only=not run_backup,
        backup_only=(run_backup and not run_inspection and not run_custom_commands),
        status_callback=dashboard.handle_event,
    )
    inspector.load_devices(devices)

    column_order: list[str] | None = None
    if run_inspection:
        available_columns = inspector.get_available_inspection_columns(inspector.devices)
        if available_columns:
            profile_keys = inspector.get_device_profile_keys(inspector.devices)
            column_order = resolve_inspection_column_order(
                available_columns,
                profile_keys,
                settings,
            )
            logger.info(
                "COLUMN ORDER APPLIED | profiles=%s | columns=%s",
                profile_keys,
                column_order,
            )
        else:
            logger.info(t("main.warning.no_order_columns"))

    _print_run_summary(mode_label, len(devices), filepath, settings, run_timestamp, log_file)
    if not ask_yes_no(t("main.confirm.run_now"), default=True):
        logger.info(t("main.info.job_cancelled"))
        return

    dashboard.start()
    try:
        inspector.run_selected_actions(
            inspection_mode=run_inspection,
            backup_mode=run_backup,
            custom_commands=rendered_commands,
        )
    finally:
        dashboard.mark_completed(t("main.info.dashboard_completed_note"))
        dashboard.stop()

    if inspector.results:
        save_results_to_excel(
            inspector.results,
            inspector.output_excel,
            column_order=column_order,
            column_aliases=settings.column_aliases,
        )

    _print_result_summary(inspector, log_file)


def main() -> None:
    try:
        while True:
            settings = load_settings()
            set_locale(settings.language, settings.fallback_language)
            menu_choice = show_main_menu()

            if menu_choice == "1":
                _run_selected_actions(settings)
            elif menu_choice == "2":
                show_settings_menu(settings)
            elif menu_choice == "3":
                show_netmiko_device_types()
            elif menu_choice == "4":
                console.print(f"[dim]{t('main.shutdown')}[/dim]")
                return
    except KeyboardInterrupt:
        console.print(f"\n[dim]{t('main.shutdown')}[/dim]")
    except NetworkInspectorError as exc:
        logger.exception(t("main.errors.app_error", error=exc))
    except Exception as exc:
        logger.exception(t("main.errors.unexpected", error=exc))


if __name__ == "__main__":
    main()
