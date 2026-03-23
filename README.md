# NetOps Inspector

Language: [English](README.md) | [한국어](docs/README.ko.md) | [日本語](docs/README.ja.md) | [Español](docs/README.es.md) | [Português (Brasil)](docs/README.pt-BR.md) | [简体中文](docs/README.zh-CN.md)

NetOps Inspector is a CLI tool for multi-vendor network device inspection and configuration backup.
It reads device inventories from Excel files, connects via SSH/Telnet, runs inspection commands, parses outputs, and writes result workbooks.

## Key Features

- Multi-vendor architecture (`vendors/` modules)
- Inspection / Backup / Batch command input can be combined in one run
- Batch command input from TXT/XLSX files or `switch-config-builder` style YAML profiles
- Per-device command rendering from `switch-config-builder` compatible YAML profiles and CSV/XLSX values
- Excel input validation (required fields, duplicate IP, vendor/OS compatibility)
- Retry and timeout controls for network I/O
- Real-time terminal dashboard during execution
- Session log files per device
- Result workbook generation with configurable column alias/order
- User-defined parsing and command extensions via `custom_rules.yaml`
- i18n-ready UI/messages (`en`, `ko`, `ja`, `es`, `pt-BR`, `zh-CN`)

## Supported Vendors (Current Modules)

- `alcatel-lucent`
- `aruba`
- `axgate`
- `cisco`
- `dayou`
- `handreamnet`
- `juniper`
- `nexg`
- `piolink`
- `ruckus`
- `ubiquoss`

Supported OS values depend on each vendor module and `vendors/__init__.py` command maps.

## Requirements

- Python 3.10+
- Network reachability to target devices
- Dependencies in `requirements.txt`

Install:

```bash
pip install -r requirements.txt
```

## Quick Start

Run:

```bash
python main.py
```

Main menu:

1. Start inspection/backup
2. Change settings
3. Show Netmiko `device_type` list
4. Exit

## Excel Input Schema

Required columns:

- `ip`
- `vendor`
- `os`
- `connection_type` (`ssh` or `telnet`)
- `port`
- `password`

Optional columns:

- `username`
- `enable_password`

Example:

| ip | vendor | os | connection_type | port | username | password | enable_password |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 192.168.1.10 | cisco | ios | ssh | 22 | admin | ****** | ****** |
| 192.168.1.20 | ruckus | icx | ssh | 22 | super | ****** | |

## Settings (`settings.yaml`)

The file is auto-created in the app directory when missing.

Common keys:

- `console_log_level`: `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`
- `max_retries`: max connect retries
- `timeout`: connect timeout (seconds)
- `max_workers`: parallel worker count
- `inspection_excludes`: per vendor/OS parse exclusion map

Inspection output keys:

- `column_aliases`: normalize inspection column names
- `inspection_column_order_global`
- `inspection_column_order_by_profile`

i18n keys:

- `language`
- `fallback_language`
- `input_column_aliases`

Example:

```yaml
language: en
fallback_language: en
console_log_level: WARNING
max_retries: 3
timeout: 10
max_workers: 10

input_column_aliases:
  "ip address": ip
  "vendor name": vendor
  "connection type": connection_type

column_aliases:
  "host name": Hostname
  "cpu usage": CPU Usage
```

## i18n

Language codes currently accepted:

- `en`
- `ko`
- `ja`
- `es`
- `pt-BR`
- `zh-CN`

Translation files currently shipped:

- `locales/en.yaml`
- `locales/ko.yaml`
- `locales/ja.yaml`
- `locales/es.yaml`
- `locales/pt-BR.yaml`
- `locales/zh-CN.yaml`

Unsupported language codes are normalized to `en`.
If a translation key is missing, messages fall back to `fallback_language`, then to English.

## Multilingual README

- Korean: `docs/README.ko.md`
- Japanese: `docs/README.ja.md`
- Spanish: `docs/README.es.md`
- Portuguese (Brazil): `docs/README.pt-BR.md`
- Simplified Chinese: `docs/README.zh-CN.md`

## Custom Rules (`custom_rules.yaml`)

You can extend commands/parsers without changing Python code.

Top-level sections:

- `inspection_commands`
- `backup_commands`
- `parsing_rules`
- `connection_overrides`
- `handler_overrides`

Template file:

- `custom_rules.example.yaml`

## Profile-Based Custom Commands

The `Tasks` menu now supports multi-select task execution.

You can combine:

- Inspection
- Backup
- Batch command input

When multiple tasks are selected, each device is processed in a single session so
inspection, command execution, and backup can run without reconnecting between steps.

After selecting tasks, the CLI lets you choose the execution order explicitly.
It also shows an order warning before the run, including keyword-based guidance for
cases such as management IP changes, routing changes, or other commands that may
disconnect the session or change what later inspection/backup captures.
Batch command input supports two input types:

- Plain command files: `.txt`, `.xlsx`, `.xls`, `.xlsm`
- Profile templates: `.yaml`, `.yml`

When a YAML profile is selected, the app can optionally load a separate template-values
file (`.csv`, `.xlsx`, `.xls`, `.xlsm`) that follows the same header style used by
`switch-config-builder`. Matching between inventory rows and template values is attempted
with `device_id`, then `hostname`, then `ip`.

Profile templates use `switch-config-builder` style variables and blocks:

```yaml
id: SAMPLE_PROFILE
vendor: CISCO
model: Catalyst
firmware: IOS-XE 17.x
variables:
  new_hostname:
    required: true
    type: string
  new_ip:
    required: true
    type: ipv4
  new_mask:
    required: true
    type: ipv4
blocks:
  - name: base
    lines:
      - "configure terminal"
      - "hostname {{ new_hostname }}"
      - "interface vlan 99"
      - " ip address {{ new_ip }} {{ new_mask }}"
      - "end"
```

If a block has a matching boolean variable like `enable_voice_vlan: false`, the CLI
runner skips the block named `voice_vlan` automatically.

Example files in this repository:

- `examples/batch_command_input/profile_access_switch.yaml`
- `examples/batch_command_input/profile_access_switch_values.csv`

User workflow:

1. Prepare your inventory Excel with login information. If possible, include
   `device_id` because profile value matching prefers `device_id`, then `hostname`,
   then `ip`.
2. In the CLI, open `Tasks`, select `Batch command input`, and choose the YAML
   profile file.
3. When prompted for template values, select the CSV/XLSX file if device-specific
   values are stored separately. Press Enter to skip it when the inventory file
   already contains the variable columns.
4. The CLI merges values per device and renders commands separately for each device.
5. If `enable_<block_name>` is `false`, that block is skipped automatically.

With the example files above, `SW-01` renders commands such as:

```text
configure terminal
hostname BR-1F-01
interface vlan 99
 ip address 10.10.99.11 255.255.255.0
 no shutdown
ip default-gateway 10.10.99.1
end
write memory
```

`SW-02` renders the same base commands but also includes the `voice_vlan` block
because `enable_voice_vlan=true`.

## Outputs

Generated paths (timestamped):

- Inspection results: `results/inspection_results_YYYYMMDD_HHMMSS.xlsx`
- Custom command results: `results/command_results_YYYYMMDD_HHMMSS.xlsx`
- Backup files: `backup/YYYYMMDD_HHMMSS/[IP]_[vendor]_[os].txt`
- Run logs: `logs/netops_inspector_YYYYMMDD_HHMMSS.log`
- Session logs: `session_logs/YYYYMMDD_HHMMSS/[IP]_[vendor]_[os].log`

When backup is included, each device uses a single connection for all selected steps to reduce reconnect-related failures.

## Testing

```bash
python -m pytest
```

## Build (Windows)

Use:

```bat
build.bat
```

The script expects `NetOpsInspector.spec` in the repository root.

## Security Notes

- Do not hardcode credentials in source files.
- Prefer environment variables or secured secret delivery for runtime credentials.
- Treat exported logs and result files as sensitive operational data.

## License

MIT License
