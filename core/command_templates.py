from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from jinja2 import Environment, StrictUndefined, TemplateSyntaxError, meta


PROFILE_EXTENSIONS = (".yaml", ".yml")
VALUE_FILE_EXTENSIONS = (".csv", ".xlsx", ".xls", ".xlsm")
SUPPORTED_VARIABLE_TYPES = {"string", "ipv4", "bool", "int"}
BUILTIN_VARIABLES = {"profile_id"}
RESERVED_DEVICE_COLUMNS = {
    "ip",
    "vendor",
    "os",
    "connection_type",
    "port",
    "username",
    "password",
    "enable_password",
}


@dataclass(slots=True)
class CommandProfileVariable:
    name: str
    required: bool = False
    type: str = "string"
    default: Any = None
    description: str = ""
    description_ko: str = ""
    auto_increment: str = "none"


@dataclass(slots=True)
class CommandProfileBlock:
    name: str
    lines: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CommandProfile:
    id: str
    vendor: str
    model: str
    firmware: str
    description: str = ""
    description_ko: str = ""
    variables: dict[str, CommandProfileVariable] = field(default_factory=dict)
    blocks: list[CommandProfileBlock] = field(default_factory=list)
    source: str = ""


class CommandTemplateEngine:
    def __init__(self, profile: CommandProfile):
        self.profile = profile
        self.environment = Environment(
            autoescape=False,
            finalize=_finalize_value,
            trim_blocks=True,
            lstrip_blocks=False,
            undefined=StrictUndefined,
        )
        self._profile_errors = self._validate_profile()

    def render_commands(self, values: dict[str, Any]) -> list[str]:
        if self._profile_errors:
            raise ValueError("; ".join(self._profile_errors))

        context = self._resolve_context(values)
        auto_skip_blocks = _collect_auto_skip_blocks(self.profile, context)
        rendered_commands: list[str] = []

        for block in self.profile.blocks:
            if block.name in auto_skip_blocks:
                continue

            for line in block.lines:
                template = self.environment.from_string(line)
                rendered = template.render(**context).rstrip()
                if rendered.strip():
                    rendered_commands.append(rendered)

        if not rendered_commands:
            raise ValueError("No executable commands were rendered from the template.")

        return rendered_commands

    def _validate_profile(self) -> list[str]:
        errors: list[str] = []
        if not self.profile.blocks:
            errors.append("Profile must contain at least one command block.")

        declared_variables = set(self.profile.variables) | BUILTIN_VARIABLES

        for variable_name, spec in self.profile.variables.items():
            if spec.type not in SUPPORTED_VARIABLE_TYPES:
                errors.append(
                    f"Unsupported variable type for '{variable_name}': {spec.type}",
                )

        for block in self.profile.blocks:
            for line in block.lines:
                try:
                    parsed = self.environment.parse(line)
                except TemplateSyntaxError as exc:
                    errors.append(
                        f"Template syntax error in block '{block.name}': {exc.message}",
                    )
                    continue

                undeclared = meta.find_undeclared_variables(parsed) - declared_variables
                for variable_name in sorted(undeclared):
                    errors.append(
                        f"Undefined variable used in block '{block.name}': {variable_name}",
                    )

        return errors

    def _resolve_context(self, values: dict[str, Any]) -> dict[str, Any]:
        resolved = {str(key).strip(): value for key, value in values.items()}

        for variable_name, spec in self.profile.variables.items():
            raw_value = resolved.get(variable_name, "")
            if raw_value == "" or raw_value is None:
                if spec.default is not None:
                    resolved_value = spec.default
                elif spec.required:
                    raise ValueError(f"Missing required variable: {variable_name}")
                else:
                    resolved_value = None
            else:
                resolved_value = raw_value

            resolved[variable_name] = _coerce_value(variable_name, resolved_value, spec.type)

        resolved["profile_id"] = self.profile.id
        return resolved


def is_profile_command_path(path: str) -> bool:
    return Path(path).suffix.lower() in PROFILE_EXTENSIONS


def load_command_profile(filepath: str) -> CommandProfile:
    return parse_command_profile_text(
        Path(filepath).read_text(encoding="utf-8"),
        source=filepath,
    )


def parse_command_profile_text(text: str, *, source: str = "") -> CommandProfile:
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        raise ValueError("Profile YAML root must be a mapping.")

    try:
        profile_id = str(raw["id"]).strip()
        vendor = str(raw["vendor"]).strip()
        model = str(raw["model"]).strip()
        firmware = str(raw["firmware"]).strip()
    except KeyError as exc:
        raise ValueError(f"Missing required profile field: {exc.args[0]}") from exc

    variables_raw = raw.get("variables", {})
    if not isinstance(variables_raw, dict):
        raise ValueError("'variables' must be a mapping.")

    blocks_raw = raw.get("blocks", [])
    if not isinstance(blocks_raw, list):
        raise ValueError("'blocks' must be a list.")

    variables: dict[str, CommandProfileVariable] = {}
    for variable_name, config in variables_raw.items():
        if not isinstance(config, dict):
            raise ValueError(f"variables.{variable_name} must be a mapping.")
        variable_key = str(variable_name).strip()
        variables[variable_key] = CommandProfileVariable(
            name=variable_key,
            required=bool(config.get("required", False)),
            type=str(config.get("type", "string")).strip().lower() or "string",
            default=config.get("default"),
            description=str(config.get("description", "")).strip(),
            description_ko=str(config.get("description_ko", "")).strip(),
            auto_increment=str(config.get("auto_increment", "none")).strip().lower() or "none",
        )

    blocks: list[CommandProfileBlock] = []
    for index, block_raw in enumerate(blocks_raw, start=1):
        if not isinstance(block_raw, dict):
            raise ValueError(f"blocks[{index}] must be a mapping.")
        name = str(block_raw.get("name", f"block_{index}")).strip() or f"block_{index}"
        lines_raw = block_raw.get("lines", [])
        if not isinstance(lines_raw, list) or not all(isinstance(line, str) for line in lines_raw):
            raise ValueError(f"blocks[{index}].lines must be a list of strings.")
        blocks.append(CommandProfileBlock(name=name, lines=list(lines_raw)))

    return CommandProfile(
        id=profile_id,
        vendor=vendor,
        model=model,
        firmware=firmware,
        description=str(raw.get("description", "")).strip(),
        description_ko=str(raw.get("description_ko", "")).strip(),
        variables=variables,
        blocks=blocks,
        source=source,
    )


def load_template_value_rows(filepath: str) -> list[dict[str, str]]:
    suffix = Path(filepath).suffix.lower()
    if suffix == ".csv":
        dataframe = pd.read_csv(filepath, dtype=object).fillna("")
    elif suffix in {".xlsx", ".xls", ".xlsm"}:
        dataframe = pd.read_excel(filepath, dtype=object).fillna("")
    else:
        raise ValueError(f"Unsupported template values file format: {suffix}")

    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    rows: list[dict[str, str]] = []
    for raw_row in dataframe.to_dict("records"):
        cleaned: dict[str, str] = {}
        for key, value in raw_row.items():
            header = str(key).strip()
            if not header:
                continue
            cleaned[header] = _clean_cell_value(value)
        if any(value for value in cleaned.values()):
            rows.append(cleaned)
    return rows


def build_profile_command_payload(
    profile_path: str,
    devices: list[dict[str, Any]],
    *,
    template_values_path: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]], CommandProfile]:
    profile = load_command_profile(profile_path)
    value_rows = load_template_value_rows(template_values_path) if template_values_path else None
    prepared_devices = merge_template_values(devices, value_rows)
    engine = CommandTemplateEngine(profile)

    command_map: dict[str, list[str]] = {}
    enriched_devices: list[dict[str, Any]] = []
    for device in prepared_devices:
        rendered_commands = engine.render_commands(device)
        device_copy = dict(device)
        device_copy["_custom_command_profile_id"] = profile.id
        device_copy["_custom_command_rendered_count"] = len(rendered_commands)
        if template_values_path:
            device_copy["_custom_command_values_path"] = template_values_path
        command_map[str(device_copy.get("ip", "")).strip()] = rendered_commands
        enriched_devices.append(device_copy)

    return enriched_devices, command_map, profile


def merge_template_values(
    devices: list[dict[str, Any]],
    value_rows: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    prepared_devices = [dict(device) for device in devices]
    if not value_rows:
        return prepared_devices

    match_key = _select_match_key(prepared_devices, value_rows)
    if not match_key:
        raise ValueError(
            "Could not find a shared identifier column between inventory and template values. "
            "Use one of: device_id, hostname, ip.",
        )

    lookup: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()
    for row in value_rows:
        raw_match = row.get(match_key, "")
        normalized = _normalize_lookup_value(raw_match)
        if not normalized:
            raise ValueError(f"Template values file has a blank '{match_key}' value.")
        if normalized in lookup:
            duplicates.add(str(raw_match).strip())
            continue
        lookup[normalized] = row

    if duplicates:
        duplicate_text = ", ".join(sorted(duplicates))
        raise ValueError(
            f"Duplicate '{match_key}' values found in template values file: {duplicate_text}",
        )

    merged_devices: list[dict[str, Any]] = []
    unmatched_devices: list[str] = []
    for device in prepared_devices:
        device_label = _device_label(device)
        normalized = _normalize_lookup_value(device.get(match_key, ""))
        if not normalized:
            unmatched_devices.append(device_label)
            continue

        row = lookup.get(normalized)
        if row is None:
            unmatched_devices.append(device_label)
            continue

        combined = dict(device)
        for key, value in row.items():
            cleaned_key = str(key).strip()
            if not cleaned_key:
                continue

            if cleaned_key in RESERVED_DEVICE_COLUMNS and cleaned_key in combined:
                existing = _clean_cell_value(combined.get(cleaned_key, ""))
                incoming = _clean_cell_value(value)
                if existing and incoming and existing != incoming:
                    raise ValueError(
                        f"Template values column '{cleaned_key}' conflicts with inventory data "
                        f"for device '{device_label}'.",
                    )
                continue

            combined[cleaned_key] = _clean_cell_value(value)

        merged_devices.append(combined)

    if unmatched_devices:
        missing = ", ".join(sorted(unmatched_devices))
        raise ValueError(
            f"Missing template values for inventory rows matched by '{match_key}': {missing}",
        )

    return merged_devices


def _collect_auto_skip_blocks(
    profile: CommandProfile,
    context: dict[str, Any],
) -> set[str]:
    auto_skip: set[str] = set()
    for block in profile.blocks:
        flag_name = f"enable_{block.name}"
        flag_value = context.get(flag_name)
        if isinstance(flag_value, bool) and not flag_value:
            auto_skip.add(block.name)
    return auto_skip


def _select_match_key(
    devices: list[dict[str, Any]],
    value_rows: list[dict[str, str]],
) -> str:
    device_columns = {str(key).strip() for device in devices for key in device.keys()}
    value_columns = {str(key).strip() for row in value_rows for key in row.keys()}
    for candidate in ("device_id", "hostname", "ip"):
        if candidate in device_columns and candidate in value_columns:
            return candidate
    return ""


def _normalize_lookup_value(value: object) -> str:
    return str(value).strip().casefold()


def _device_label(device: dict[str, Any]) -> str:
    for key in ("device_id", "hostname", "ip"):
        value = str(device.get(key, "")).strip()
        if value:
            return value
    return "unknown-device"


def _clean_cell_value(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _coerce_value(variable_name: str, value: Any, value_type: str) -> Any:
    if value is None:
        return None

    if value_type == "string":
        return str(value).strip()

    if value_type == "ipv4":
        try:
            parsed = ipaddress.ip_address(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid IPv4 value for '{variable_name}': {value}") from exc
        if parsed.version != 4:
            raise ValueError(f"Invalid IPv4 value for '{variable_name}': {value}")
        return str(parsed)

    if value_type == "bool":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for '{variable_name}': {value}")

    if value_type == "int":
        try:
            return int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"Invalid integer value for '{variable_name}': {value}") from exc

    raise ValueError(f"Unsupported variable type: {value_type}")


def _finalize_value(value: Any) -> Any:
    if value is None:
        return ""
    return value
