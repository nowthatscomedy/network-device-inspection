from __future__ import annotations

import logging
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.validator import PathValidator

from core.i18n import t

logger = logging.getLogger(__name__)

EXCEL_EXTENSIONS = (".xlsx", ".xls", ".xlsm")
COMMAND_EXTENSIONS = (".txt", ".xlsx", ".xls", ".xlsm", ".yaml", ".yml")
TABULAR_EXTENSIONS = (".csv", ".xlsx", ".xls", ".xlsm")


def _validate_extension(path_str: str, extensions: tuple[str, ...]) -> bool:
    if not path_str:
        return False
    return Path(path_str).suffix.lower() in extensions


def get_filepath_from_cli() -> str | None:
    result = inquirer.filepath(
        message=t("cli.path.excel_message"),
        long_instruction=t("cli.path.long_instruction"),
        validate=PathValidator(is_file=True, message=t("cli.path.invalid_path")),
        only_files=True,
        mandatory=False,
    ).execute()

    if not result:
        return None

    cleaned = result.strip().strip('"').strip("'")
    path = Path(cleaned).expanduser()

    if not path.exists():
        logger.warning(t("cli.warning.file_not_found", path=path))
        return None

    if path.suffix.lower() not in EXCEL_EXTENSIONS:
        logger.warning(
            t(
                "cli.warning.unsupported_excel_extension",
                suffix=path.suffix,
            ),
        )
        return None

    return str(path)


def get_command_filepath_from_cli() -> str | None:
    result = inquirer.filepath(
        message=t("cli.path.command_message"),
        long_instruction=t("cli.path.long_instruction"),
        validate=PathValidator(is_file=True, message=t("cli.path.invalid_path")),
        only_files=True,
        mandatory=False,
    ).execute()

    if not result:
        return None

    cleaned = result.strip().strip('"').strip("'")
    path = Path(cleaned).expanduser()

    if not path.exists():
        logger.warning(t("cli.warning.file_not_found", path=path))
        return None

    if path.suffix.lower() not in COMMAND_EXTENSIONS:
        logger.warning(
            t(
                "cli.warning.unsupported_command_extension",
                suffix=path.suffix,
            ),
        )
        return None

    return str(path)


def get_template_values_filepath_from_cli() -> str | None:
    result = inquirer.filepath(
        message=t("cli.path.template_values_message"),
        long_instruction=t("cli.path.long_instruction"),
        validate=PathValidator(is_file=True, message=t("cli.path.invalid_path")),
        only_files=True,
        mandatory=False,
    ).execute()

    if not result:
        return None

    cleaned = result.strip().strip('"').strip("'")
    path = Path(cleaned).expanduser()

    if not path.exists():
        logger.warning(t("cli.warning.file_not_found", path=path))
        return None

    if path.suffix.lower() not in TABULAR_EXTENSIONS:
        logger.warning(
            t(
                "cli.warning.unsupported_tabular_extension",
                suffix=path.suffix,
            ),
        )
        return None

    return str(path)
