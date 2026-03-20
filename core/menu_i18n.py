from __future__ import annotations

import logging

from InquirerPy import inquirer
from InquirerPy.separator import Separator
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.i18n import list_supported_languages, set_locale, t
from core.settings import AppSettings, save_settings

logger = logging.getLogger(__name__)
console = Console()

_LANGUAGE_LABELS: dict[str, str] = {
    "en": "English (en)",
    "ko": "Korean (ko)",
    "ja": "Japanese (ja)",
    "es": "Spanish (es)",
    "pt-BR": "Portuguese (Brazil) (pt-BR)",
    "zh-CN": "Chinese (Simplified) (zh-CN)",
}


def _clear() -> None:
    console.clear()


def _show_banner() -> None:
    console.print(
        Panel(
            t("app.title"),
            title="[bold cyan]CLI[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()


def show_main_menu() -> str:
    _clear()
    _show_banner()
    choices = [
        {"name": t("menu.main.start"), "value": "1"},
        {"name": t("menu.main.settings"), "value": "2"},
        {"name": t("menu.main.netmiko_types"), "value": "3"},
        Separator(),
        {"name": t("menu.main.exit"), "value": "4"},
    ]
    return inquirer.select(
        message=t("menu.main.prompt"),
        choices=choices,
        default="1",
        pointer=">",
    ).execute()


def show_netmiko_device_types() -> None:
    try:
        from netmiko.ssh_dispatcher import CLASS_MAPPER

        device_types = sorted(CLASS_MAPPER.keys())
    except Exception as exc:
        console.print(f"[red]{t('menu.netmiko.list_fetch_failed', error=exc)}[/red]")
        input(t("menu.prompts.press_enter_back"))
        return

    if not device_types:
        console.print(f"[yellow]{t('menu.netmiko.no_device_types')}[/yellow]")
        input(t("menu.prompts.press_enter_back"))
        return

    _clear()
    result = inquirer.fuzzy(
        message=t("menu.netmiko.search_prompt"),
        choices=device_types,
        pointer=">",
        info=True,
        mandatory=False,
    ).execute()

    if result:
        console.print(
            f"\n[bold green]{t('menu.netmiko.selected', device_type=result)}[/bold green]",
        )
        input(t("menu.prompts.press_enter_back"))


def show_action_menu() -> str | None:
    _clear()
    console.print(
        Panel(
            t("menu.action.description"),
            title=f"[bold cyan]{t('menu.action.title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()

    choices = [
        {"name": t("menu.action.inspect_only"), "value": "1"},
        {"name": t("menu.action.backup_only"), "value": "2"},
        {"name": t("menu.action.inspect_and_backup"), "value": "3"},
        {"name": t("menu.action.batch_command_input"), "value": "4"},
        Separator(),
        {"name": t("menu.action.back"), "value": None},
    ]
    return inquirer.select(
        message=t("menu.action.prompt"),
        choices=choices,
        default="1",
        pointer=">",
    ).execute()


def select_console_log_level(current_level: str) -> str:
    _clear()
    console.print(
        Panel(
            t(
                "menu.settings.select_log_level_desc",
                current_level=current_level,
            ),
            title=f"[bold cyan]{t('menu.settings.select_log_level_title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()

    level_choices = [
        {"name": "CRITICAL", "value": "CRITICAL"},
        {"name": "ERROR", "value": "ERROR"},
        {"name": "WARNING", "value": "WARNING"},
        {"name": "INFO", "value": "INFO"},
        {"name": "DEBUG", "value": "DEBUG"},
        Separator(),
        {"name": t("menu.action.back"), "value": None},
    ]
    selected = inquirer.select(
        message=t("menu.settings.console_log_level"),
        choices=level_choices,
        default=current_level,
        pointer=">",
    ).execute()
    return selected if selected is not None else current_level


def _input_int(prompt: str, current: int, min_val: int, max_val: int) -> int:
    while True:
        raw = input(
            f"{prompt} ({min_val}-{max_val}, current: {current}, {t('menu.settings.keep_current_hint')}): ",
        ).strip()
        if not raw:
            return current
        try:
            value = int(raw)
        except ValueError:
            console.print(f"[red]{t('menu.settings.enter_number')}[/red]")
            continue
        if value < min_val or value > max_val:
            console.print(
                f"[red]{t('menu.settings.enter_range', min_val=min_val, max_val=max_val)}[/red]",
            )
            continue
        return value


def select_max_retries(current: int) -> int:
    _clear()
    console.print(
        Panel(
            t("menu.settings.retries_desc", current=current),
            title=f"[bold cyan]{t('menu.settings.retries_title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()
    return _input_int(t("menu.settings.max_retries"), current, 1, 10)


def select_timeout(current: int) -> int:
    _clear()
    console.print(
        Panel(
            t("menu.settings.timeout_desc", current=current),
            title=f"[bold cyan]{t('menu.settings.timeout_title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()
    return _input_int(t("menu.settings.timeout"), current, 1, 300)


def select_max_workers(current: int) -> int:
    _clear()
    console.print(
        Panel(
            t("menu.settings.workers_desc", current=current),
            title=f"[bold cyan]{t('menu.settings.workers_title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()
    return _input_int(t("menu.settings.max_workers"), current, 1, 50)


def select_language(current: str) -> str:
    _clear()
    console.print(
        Panel(
            t("menu.settings.language_desc", current=current),
            title=f"[bold cyan]{t('menu.settings.language_title')}[/bold cyan]",
            border_style="cyan",
            expand=False,
        ),
    )
    console.print()

    choices = [
        {"name": _LANGUAGE_LABELS.get(code, code), "value": code}
        for code in list_supported_languages()
    ]
    choices.append(Separator())
    choices.append({"name": t("menu.action.back"), "value": None})
    selected = inquirer.select(
        message=t("menu.settings.language"),
        choices=choices,
        default=current,
        pointer=">",
    ).execute()
    return selected if selected is not None else current


def show_settings_menu(settings: AppSettings) -> None:
    while True:
        _clear()
        table = Table(title=t("menu.settings.title"), border_style="dim", expand=False)
        table.add_column(t("menu.settings.item"), style="cyan")
        table.add_column(t("menu.settings.value"), style="bold")
        table.add_row(t("menu.settings.console_log_level"), settings.console_log_level)
        table.add_row(t("menu.settings.max_retries"), str(settings.max_retries))
        table.add_row(t("menu.settings.timeout"), f"{settings.timeout}")
        table.add_row(t("menu.settings.max_workers"), f"{settings.max_workers}")
        table.add_row(t("menu.settings.language"), settings.language)
        exclude_count = sum(
            len(commands)
            for os_map in settings.inspection_excludes.values()
            for commands in os_map.values()
        )
        table.add_row(t("menu.settings.excludes"), str(exclude_count))
        console.print(table)
        console.print()

        choices = [
            {"name": t("menu.settings.change_log_level"), "value": "log_level"},
            {"name": t("menu.settings.change_retries"), "value": "retries"},
            {"name": t("menu.settings.change_timeout"), "value": "timeout"},
            {"name": t("menu.settings.change_workers"), "value": "workers"},
            {"name": t("menu.settings.change_language"), "value": "language"},
            {"name": t("menu.settings.manage_excludes"), "value": "excludes"},
            Separator(),
            {"name": t("menu.settings.back"), "value": None},
        ]
        choice = inquirer.select(
            message=t("menu.settings.options_prompt"),
            choices=choices,
            pointer=">",
        ).execute()

        if choice is None:
            return
        if choice == "log_level":
            settings.console_log_level = select_console_log_level(settings.console_log_level)
            save_settings(settings)
        elif choice == "retries":
            settings.max_retries = select_max_retries(settings.max_retries)
            save_settings(settings)
        elif choice == "timeout":
            settings.timeout = select_timeout(settings.timeout)
            save_settings(settings)
        elif choice == "workers":
            settings.max_workers = select_max_workers(settings.max_workers)
            save_settings(settings)
        elif choice == "language":
            settings.language = select_language(settings.language)
            save_settings(settings)
            set_locale(settings.language, settings.fallback_language)
        elif choice == "excludes":
            # Keep existing advanced exclude workflow from the legacy menu.
            from core import menu as legacy_menu

            legacy_menu.show_inspection_exclude_menu(settings)


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    return inquirer.confirm(
        message=prompt,
        default=default,
    ).execute()
