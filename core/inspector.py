import re
from netmiko import ConnectHandler
import os
from datetime import datetime
import threading
import socket
import time
import logging
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys

from core.settings import canonicalize_column_name, make_profile_key
from vendors import (
    INSPECTION_COMMANDS,
    BACKUP_COMMANDS,
    PARSING_RULES,
    get_custom_handler,
    CUSTOM_PARSERS,
    CONNECTION_OVERRIDES,
    HANDLER_OVERRIDES,
    is_custom_rule_pair
)

class NetworkInspector:
    def __init__(
        self,
        output_excel: str,
        backup_only: bool = False,
        inspection_only: bool = False,
        run_timestamp: str | None = None,
        inspection_excludes: dict[str, dict[str, list[str]]] | None = None,
        max_retries: int = 3,
        timeout: int = 10,
        max_workers: int = 10,
        column_aliases: dict[str, str] | None = None,
        status_callback: Callable[[dict[str, object]], None] | None = None,
    ):
        file_name, file_ext = os.path.splitext(output_excel)
        timestamp = run_timestamp or datetime.now().strftime('%Y%m%d_%H%M%S')
        self.output_dir = "results"
        os.makedirs(self.output_dir, exist_ok=True)
        self.output_excel = os.path.join(self.output_dir, f"{file_name}_{timestamp}{file_ext}")
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_workers = max_workers
        self.logger = logging.getLogger(__name__)
        self.backup_dir = os.path.join("backup", timestamp)
        self.session_log_dir = os.path.join("session_logs", timestamp)
        
        os.makedirs("session_logs", exist_ok=True)
        os.makedirs(self.session_log_dir, exist_ok=True)
        
        if not inspection_only:
            os.makedirs("backup", exist_ok=True)
            os.makedirs(self.backup_dir, exist_ok=True)
            
        self.backup_only = backup_only
        self.inspection_only = inspection_only
        
        self.devices = []
        self.results = []
        self.results_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.cli_lock = threading.Lock()
        self.inspection_excludes = inspection_excludes or {}
        self.reconnect_cooldown = 0.5
        self.column_aliases = dict(column_aliases or {})
        self.status_callback = status_callback

    def _canonicalize_result_columns(self, raw: dict) -> dict:
        canonical_result: dict = {}
        for key, value in raw.items():
            canonical_key = canonicalize_column_name(key, self.column_aliases)
            if not canonical_key:
                continue

            if canonical_key not in canonical_result:
                canonical_result[canonical_key] = value
                continue

            existing = canonical_result[canonical_key]
            if existing in (None, "", []):
                canonical_result[canonical_key] = value
                continue
            if value in (None, "", []):
                continue

            existing_text = str(existing)
            incoming_text = str(value)
            if existing_text != incoming_text:
                canonical_result[canonical_key] = f"{existing_text}, {incoming_text}"

        return canonical_result

    def _get_device_commands(self, vendor: str, model: str) -> list[str]:
        """장비별 점검 명령어를 가져옵니다."""
        try:
            self.logger.debug("장비 명령어 조회 시작: %s %s", vendor, model)
            v = str(vendor).strip().lower()
            m = str(model).strip().lower()
            cmds = INSPECTION_COMMANDS.get(v, {}).get(m, [])
            excludes = set(self.inspection_excludes.get(v, {}).get(m, []))
            if excludes:
                filtered_cmds: list[str] = []
                for cmd in cmds:
                    if cmd in excludes:
                        continue
                    parse_ids = self._get_parse_ids_for_command(v, m, cmd)
                    if parse_ids and parse_ids.issubset(excludes):
                        continue
                    filtered_cmds.append(cmd)
                cmds = filtered_cmds
            if not cmds:
                self.logger.warning("점검 명령어를 찾을 수 없음: %s %s", v, m)
            else:
                self.logger.debug("점검 명령어 목록: %s", cmds)
            return cmds
        except Exception as e:
            self.logger.error("장비 명령어 조회 중 오류 발생: %s", e)
            return []
    
    def _get_backup_command(self, vendor: str, model: str) -> str:
        """장비별 백업 명령어를 가져옵니다."""
        try:
            self.logger.debug("백업 명령어 조회 시작: %s %s", vendor, model)
            v = str(vendor).strip().lower()
            m = str(model).strip().lower()
            cmd = BACKUP_COMMANDS.get(v, {}).get(m, '')
            if not cmd:
                self.logger.warning("백업 명령어를 찾을 수 없음: %s %s", v, m)
            else:
                self.logger.debug("백업 명령어: %s", cmd)
            return cmd
        except Exception as e:
            self.logger.error("백업 명령어 조회 중 오류 발생: %s", e)
            return ""
    
    def _parse_command_output(self, vendor: str, model: str, command: str, output: str) -> dict:
        """명령어 출력을 파싱합니다."""
        self.logger.debug("명령어 출력 파싱 시작: %s", command)
        result = {}
        vendor_lower = str(vendor).lower()
        model_lower = str(model).lower()
        excludes = set(self.inspection_excludes.get(vendor_lower, {}).get(model_lower, []))
        if command in excludes:
            self.logger.debug("파싱 제외(명령어 단위): %s", command)
            return result
        
        if vendor_lower not in PARSING_RULES:
            self.logger.debug("파싱 규칙 없음 (벤더): %s", vendor)
            return result
            
        if model_lower not in PARSING_RULES[vendor_lower]:
            self.logger.debug("파싱 규칙 없음 (모델): %s", model)
            return result
            
        if command not in PARSING_RULES[vendor_lower][model_lower]:
            self.logger.debug("파싱 규칙 없음 (명령어): %s", command)
            return result
        
        try:
            rules = PARSING_RULES[vendor_lower][model_lower][command]
            
            if 'custom_parser' in rules:
                parser_name = rules['custom_parser']
                
                if parser_name in CUSTOM_PARSERS:
                    parser_func = CUSTOM_PARSERS[parser_name]
                    parsed_value = parser_func(output)

                    if isinstance(parsed_value, dict):
                        result.update(parsed_value)
                    elif 'output_column' in rules:
                        column = rules['output_column']
                        result[column] = parsed_value
                else:
                    self.logger.error("커스텀 파서 함수 '%s'를 찾을 수 없습니다.", parser_name)

            elif 'pattern' in rules:
                pattern = rules['pattern']
                column = rules['output_column']
                matches = re.finditer(pattern, output, re.MULTILINE)
                values = [match.group(1) for match in matches]
                
                if rules.get('first_match_only', False) and values:
                    result[column] = values[0]
                else:
                    result[column] = ', '.join(values)
            elif 'patterns' in rules:
                for pattern_rule in rules['patterns']:
                    if 'custom_parser' in pattern_rule:
                        parser_name = pattern_rule['custom_parser']
                        column = pattern_rule['output_column']
                        
                        if parser_name in CUSTOM_PARSERS:
                            parser_func = CUSTOM_PARSERS[parser_name]
                            result[column] = parser_func(output)
                        else:
                            self.logger.error("커스텀 파서 함수 '%s'를 찾을 수 없습니다.", parser_name)
                        continue
                        
                    pattern = pattern_rule['pattern']
                    matches = list(re.finditer(pattern, output, re.MULTILINE))
                    
                    if not matches:
                        continue
                    
                    if 'output_columns' in pattern_rule and matches:
                        columns = pattern_rule['output_columns']
                        for i, col in enumerate(columns):
                            group_idx = i + 1
                            if group_idx < len(matches[0].groups()) + 1:
                                result[col] = matches[0].group(group_idx)
                        
                        if 'process' in pattern_rule:
                            process_info = pattern_rule['process']
                            
                            if process_info['type'] == 'percentage':
                                if 'inputs' in process_info and all(col in result for col in process_info['inputs']):
                                    inputs = process_info['inputs']
                                    try:
                                        numerator = float(result[inputs[0]])
                                        denominator = float(result[inputs[1]])
                                        if denominator > 0:
                                            percentage = round((numerator / denominator) * 100, 2)
                                            result[process_info['output_column']] = f"{percentage}%"
                                        else:
                                            self.logger.warning("분모가 0입니다: %s (명령어: %s)", inputs[1], command)
                                    except (ValueError, TypeError) as e:
                                        self.logger.warning("백분율 계산 실패: %s (명령어: %s)", e, command)
                                else:
                                    self.logger.warning(
                                        "'percentage' process: 'inputs' 키가 없거나, result에 해당 컬럼이 없습니다. (명령어: %s)",
                                        command
                                    )

                            elif process_info['type'] == 'calculate_usage_from_available':
                                if 'input_column' in process_info:
                                    input_col = process_info['input_column']
                                    output_col = process_info['output_column']
                                    if input_col in result:
                                        try:
                                            available_percent_str = result[input_col].replace('%', '')
                                            available_percent = float(available_percent_str)
                                            usage_percent = round(100.0 - available_percent, 2)
                                            result[output_col] = f"{usage_percent}%"
                                            if input_col in result:
                                                del result[input_col]
                                        except ValueError:
                                            self.logger.warning(
                                                "사용 가능한 메모리 백분율 계산 실패: %s (명령어: %s)",
                                                result[input_col], command
                                            )
                                    else:
                                        self.logger.warning(
                                            "'calculate_usage_from_available' process: 입력 컬럼 '%s'을 result에서 찾을 수 없습니다. (명령어: %s)",
                                            input_col, command
                                        )
                                else:
                                    self.logger.warning(
                                        "'calculate_usage_from_available' process: 'input_column' 키가 없습니다. (명령어: %s)",
                                        command
                                    )
                    elif 'output_column' in pattern_rule and matches:
                        column = pattern_rule['output_column']
                        values = [match.group(1) for match in matches]
                        
                        if pattern_rule.get('first_match_only', False) and values:
                            result[column] = values[0]
                        else:
                            result[column] = ', '.join(values)
            else:
                if 'output_column' in rules:
                    result[rules['output_column']] = output.strip()
                
            self.logger.debug("파싱 결과: %s", result)
        except (KeyError, AttributeError) as e:
            self.logger.warning("파싱 실패: %s", e)
            self.logger.debug("파싱 실패 예외 상세: %s", traceback.format_exc())
        if excludes:
            filtered = {}
            for key, value in result.items():
                parse_id = f"{command}::{key}"
                if parse_id in excludes:
                    continue
                filtered[key] = value
            result = filtered

        return self._canonicalize_result_columns(result)

    def _get_parse_ids_for_command(self, vendor: str, model: str, command: str) -> set[str]:
        rules = PARSING_RULES.get(vendor, {}).get(model, {}).get(command, {})
        if not isinstance(rules, dict):
            return set()

        parse_ids: set[str] = set()

        def add_column(column: str) -> None:
            if column:
                parse_ids.add(f"{command}::{column}")

        if "custom_parser" in rules:
            add_column(str(rules.get("output_column", "")).strip())
        elif "pattern" in rules:
            add_column(str(rules.get("output_column", "")).strip())
            process = rules.get("process", {})
            if isinstance(process, dict):
                add_column(str(process.get("output_column", "")).strip())
        elif "patterns" in rules:
            for pattern_rule in rules.get("patterns", []):
                if not isinstance(pattern_rule, dict):
                    continue
                if "custom_parser" in pattern_rule:
                    add_column(str(pattern_rule.get("output_column", "")).strip())
                output_columns = pattern_rule.get("output_columns", [])
                if isinstance(output_columns, list):
                    for col in output_columns:
                        if isinstance(col, str):
                            add_column(col.strip())
                add_column(str(pattern_rule.get("output_column", "")).strip())
                process = pattern_rule.get("process", {})
                if isinstance(process, dict):
                    add_column(str(process.get("output_column", "")).strip())
        else:
            add_column(str(rules.get("output_column", "")).strip())

        parse_ids.discard(f"{command}::")
        return parse_ids

    def _get_output_columns_for_command(self, vendor: str, model: str, command: str) -> list[str]:
        """명령어에 매핑되는 출력 컬럼 목록을 순서대로 반환합니다."""
        vendor_key = str(vendor).strip().lower()
        model_key = str(model).strip().lower()
        rules = PARSING_RULES.get(vendor_key, {}).get(model_key, {}).get(command, {})
        if not isinstance(rules, dict):
            return []

        columns: list[str] = []

        def add_column(column: str | None) -> None:
            if not column:
                return
            cleaned = str(column).strip()
            if cleaned and cleaned not in columns:
                columns.append(cleaned)

        if "custom_parser" in rules:
            add_column(rules.get("output_column"))
        elif "pattern" in rules:
            add_column(rules.get("output_column"))
            process = rules.get("process", {})
            if isinstance(process, dict):
                add_column(process.get("output_column"))
        elif "patterns" in rules:
            for pattern_rule in rules.get("patterns", []):
                if not isinstance(pattern_rule, dict):
                    continue
                if "custom_parser" in pattern_rule:
                    add_column(pattern_rule.get("output_column"))
                output_columns = pattern_rule.get("output_columns", [])
                if isinstance(output_columns, list):
                    for col in output_columns:
                        add_column(col)
                add_column(pattern_rule.get("output_column"))
                process = pattern_rule.get("process", {})
                if isinstance(process, dict):
                    add_column(process.get("output_column"))
        else:
            add_column(rules.get("output_column"))

        return columns

    def get_available_inspection_columns(self, devices: list[dict]) -> list[str]:
        """장비 목록 기준으로 점검 결과 컬럼을 순서대로 수집합니다."""
        ordered_columns: list[str] = []
        seen: set[str] = set()

        for device in devices:
            vendor = str(device.get("vendor", "")).strip()
            model = str(device.get("os", "")).strip()
            vendor_key = vendor.lower()
            model_key = model.lower()
            excludes = set(self.inspection_excludes.get(vendor_key, {}).get(model_key, []))

            commands = self._get_device_commands(vendor, model)
            for cmd in commands:
                for col in self._get_output_columns_for_command(vendor, model, cmd):
                    parse_id = f"{cmd}::{col}"
                    if cmd in excludes or parse_id in excludes:
                        continue
                    canonical_col = canonicalize_column_name(col, self.column_aliases)
                    if canonical_col and canonical_col not in seen:
                        seen.add(canonical_col)
                        ordered_columns.append(canonical_col)

        return ordered_columns

    def get_device_profile_keys(self, devices: list[dict] | None = None) -> list[str]:
        source = devices if devices is not None else self.devices
        profiles: list[str] = []
        seen: set[str] = set()

        for device in source:
            profile_key = make_profile_key(device.get("vendor", ""), device.get("os", ""))
            if not profile_key or profile_key in seen:
                continue
            seen.add(profile_key)
            profiles.append(profile_key)

        return profiles
    
    def _test_tcping(self, ip: str, port: int, timeout: int = 5) -> bool:
        """TCP 연결 테스트를 수행합니다."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, port))
                return result == 0
        except Exception as e:
            self.logger.error("TCP 연결 테스트 실패 (%s:%s): %s", ip, port, e)
            return False

    def _connect_to_device(
        self,
        device: dict,
        inspection_mode: bool = True,
        backup_mode: bool = True,
        session_log_suffix: str | None = None,
        custom_commands: list[str] | None = None,
        on_phase_complete: Callable[[str], None] | None = None,
    ) -> tuple[dict, dict]:
        """장비에 연결하고 명령어를 실행합니다."""
        retry_count = 0
        last_error = None
        self._print_cli_status(f"[{device['ip']}] 연결 테스트 시작 (TCP {device['port']})")
        
        if not self._test_tcping(device['ip'], device['port']):
            self.logger.error("TCP 연결 테스트 실패 (%s:%s)", device['ip'], device['port'])
            self._print_cli_status(f"[{device['ip']}] TCP 연결 테스트 실패")
            return device, {"error": "TCP 연결 테스트 실패"}
        self._print_cli_status(f"[{device['ip']}] TCP 연결 확인 완료")
        
        session_log_filename = f"{device['ip']}_{device['vendor']}_{device['os']}"
        if session_log_suffix:
            session_log_filename = f"{session_log_filename}_{session_log_suffix}"
        session_log_file = os.path.join(self.session_log_dir, f"{session_log_filename}.log")
        
        while retry_count < self.max_retries:
            try:
                with open(session_log_file, 'a', encoding='utf-8') as log:
                    log.write(f"\n{'='*50}\n")
                    log.write(f"연결 시도 {retry_count + 1} - {datetime.now()}\n")
                    log.write(f"장비: {device['ip']} ({device['vendor']} {device['os']})\n")
                    log.write(f"{'='*50}\n\n")

                custom_handler = get_custom_handler(device, self.timeout, session_log_file)
                if not custom_handler and is_custom_rule_pair(device.get("vendor", ""), device.get("os", "")):
                    if device.get("connection_type", "").lower() == "ssh":
                        from vendors.base import GenericParamikoHandler
                        vendor_key = device.get("vendor", "").strip().lower()
                        os_key = device.get("os", "").strip().lower()
                        handler_config = HANDLER_OVERRIDES.get(vendor_key, {}).get(os_key, {})
                        custom_handler = GenericParamikoHandler(
                            device, self.timeout, session_log_file,
                            handler_config=handler_config if handler_config else None
                        )
                    else:
                        self.logger.warning(
                            "커스텀 벤더/OS는 SSH만 Paramiko 공용 핸들러를 사용합니다: %s %s (%s)",
                            device.get('vendor'), device.get('os'), device.get('connection_type')
                        )
                if custom_handler:
                    self.logger.debug("커스텀 핸들러 사용: %s %s", device['vendor'], device['os'])
                    handler_connected = False
                    try:
                        self._print_cli_status(f"[{device['ip']}] 커스텀 핸들러 연결 시작")
                        custom_handler.connect()
                        handler_connected = True
                        custom_handler.enable()
                        self._print_cli_status(f"[{device['ip']}] 커스텀 핸들러 연결 완료")
                        
                        inspection_results = {}
                        
                        if inspection_mode:
                            commands = self._get_device_commands(
                                device['vendor'],
                                device['os']
                            )
                            self._print_cli_status(f"[{device['ip']}] 점검 명령 {len(commands)}개 실행 시작")
                            
                            for idx, cmd in enumerate(commands, start=1):
                                try:
                                    self._print_cli_status(f"[{device['ip']}] 점검 명령 실행 {idx}/{len(commands)}: {cmd}")
                                    output = custom_handler.send_command(cmd)
                                    
                                    parsed = self._parse_command_output(
                                        device['vendor'],
                                        device['os'],
                                        cmd,
                                        output
                                    )
                                    inspection_results.update(parsed)
                                except Exception as e:
                                    self.logger.error("명령어 실행 실패 (%s - %s): %s", cmd, device['ip'], e)
                                    inspection_results[f"error_{cmd}"] = str(e)

                        if on_phase_complete and inspection_mode:
                            on_phase_complete('inspection')

                        if custom_commands:
                            self._print_cli_status(f"[{device['ip']}] 사용자 명령 {len(custom_commands)}개 실행 시작")
                            for idx, cmd in enumerate(custom_commands, start=1):
                                try:
                                    self._print_cli_status(
                                        f"[{device['ip']}] 사용자 명령 실행 {idx}/{len(custom_commands)}: {cmd}"
                                    )
                                    custom_handler.send_command(cmd)
                                except Exception as e:
                                    self.logger.error("사용자 명령 실행 실패 (%s - %s): %s", cmd, device['ip'], e)
                                    return device, {"error": f"사용자 명령 실행 실패: {str(e)}"}

                        if backup_mode:
                            backup_cmd = self._get_backup_command(
                                device['vendor'],
                                device['os']
                            )
                            if backup_cmd:
                                try:
                                    self._print_cli_status(f"[{device['ip']}] 백업 명령 실행: {backup_cmd}")
                                    backup_output = custom_handler.send_command(backup_cmd, timeout=10)
                                    
                                    backup_filename = os.path.join(
                                        self.backup_dir,
                                        f"{device['ip']}_{device['vendor']}_{device['os']}.txt"
                                    )
                                    with open(backup_filename, 'w', encoding='utf-8') as f:
                                        f.write(backup_output)
                                    self.logger.info("백업 파일 저장 완료: %s", backup_filename)
                                    self._print_cli_status(f"[{device['ip']}] 백업 파일 저장 완료: {backup_filename}")
                                    inspection_results["backup_file"] = backup_filename
                                except Exception as e:
                                    self.logger.error("백업 실패 (%s): %s", device['ip'], e)
                                    inspection_results["backup_error"] = str(e)

                        if on_phase_complete and backup_mode:
                            on_phase_complete('backup')
                        
                        if custom_commands:
                            inspection_results["custom_commands_executed"] = len(custom_commands)
                        return device, inspection_results
                    except Exception as e:
                        self.logger.error("커스텀 핸들러 실행 실패 (%s): %s", device['ip'], e)
                        retry_count += 1
                        last_error = e
                        
                        with open(session_log_file, 'a', encoding='utf-8') as log:
                            log.write(f"\n{'='*50}\n")
                            log.write(f"커스텀 핸들러 실행 실패 ({retry_count}) - {datetime.now()}\n")
                            log.write(f"오류: {str(e)}\n")
                            log.write(f"{'='*50}\n\n")
                        
                        if retry_count < self.max_retries:
                            time.sleep(2 ** retry_count)
                            continue
                        else:
                            return device, {"error": f"커스텀 핸들러 실행 실패: {str(e)}"}
                    finally:
                        if handler_connected:
                            try:
                                custom_handler.disconnect()
                                time.sleep(self.reconnect_cooldown)
                            except Exception as disconnect_error:
                                self.logger.debug(
                                    "커스텀 핸들러 종료 중 경고 (%s): %s",
                                    device['ip'],
                                    disconnect_error,
                                )
                else:
                    vendor_key = str(device['vendor']).lower()
                    os_key = str(device['os']).lower()
                    override_map = CONNECTION_OVERRIDES.get(vendor_key, {}).get(os_key, {})
                    override_device_type = ""
                    if isinstance(override_map, dict):
                        conn_key = str(device['connection_type']).lower()
                        override_device_type = (
                            override_map.get(conn_key)
                            or override_map.get("default")
                            or override_map.get("any")
                            or ""
                        )
                    elif isinstance(override_map, str):
                        override_device_type = override_map.strip()

                    override_used = bool(override_device_type)
                    if override_used:
                        device_type = override_device_type
                    elif device['connection_type'].lower() == 'telnet':
                        device_type = f"{vendor_key}_{os_key}_telnet"
                    else:
                        device_type = f"{vendor_key}_{os_key}"
                    
                    if device['vendor'].lower() == 'juniper':
                        device_type = 'juniper_junos'

                    if override_used:
                        try:
                            from netmiko.ssh_dispatcher import CLASS_MAPPER
                            if device_type not in CLASS_MAPPER:
                                self.logger.warning(
                                    "Netmiko device_type 미지원 가능성: %s (custom override)", device_type
                                )
                        except Exception:
                            pass

                    safe_device = {
                        'ip': str(device['ip']),
                        'vendor': str(device['vendor']),
                        'os': str(device['os']),
                        'username': str(device['username']),
                        'password': str(device['password']),
                        'port': int(device['port']),
                        'connection_type': str(device['connection_type'])
                    }
                    
                    if 'enable_password' in device and device['enable_password']:
                        safe_device['enable_password'] = str(device['enable_password'])
                    
                    connection_params = {
                        'device_type': str(device_type),
                        'host': str(safe_device['ip']),
                        'username': str(safe_device['username']),
                        'password': str(safe_device['password']),
                        'port': int(safe_device['port']),
                        'secret': str(safe_device.get('enable_password', '')),
                        'timeout': int(self.timeout),
                        'session_log': str(session_log_file),
                        'fast_cli': False
                    }
                    try:
                        with ConnectHandler(**connection_params) as conn:
                            self._print_cli_status(f"[{device['ip']}] Netmiko 연결 완료 ({device_type})")
                            conn.enable()
                            try:
                                if not conn.check_enable_mode():
                                    enable_secret = safe_device.get('enable_password') or safe_device.get('password')
                                    self.logger.warning(
                                        "enable 모드 미진입 감지: %s (%s)", device['ip'], device_type
                                    )
                                    if enable_secret:
                                        output = conn.send_command_timing("enable")
                                        if "Password" in output or "password" in output:
                                            conn.send_command_timing(enable_secret)
                                    if not conn.check_enable_mode():
                                        self.logger.warning(
                                            "enable 모드 진입 실패: %s (%s)", device['ip'], device_type
                                        )
                            except Exception as e:
                                self.logger.warning(
                                    "enable 모드 확인/재시도 실패: %s (%s) - %s", device['ip'], device_type, e
                                )
                            if not (device['vendor'].lower() == 'axgate' and device['os'].lower() == 'axgate'):
                                conn.send_command_timing('terminal length 0')
                            
                            inspection_results = {}
                            if inspection_mode:
                                commands = self._get_device_commands(device['vendor'], device['os'])
                                self._print_cli_status(f"[{device['ip']}] 점검 명령 {len(commands)}개 실행 시작")
                                for idx, cmd in enumerate(commands, start=1):
                                    self._print_cli_status(f"[{device['ip']}] 점검 명령 실행 {idx}/{len(commands)}: {cmd}")
                                    output = conn.send_command(cmd, read_timeout=30)
                                    parsed = self._parse_command_output(device['vendor'], device['os'], cmd, output)
                                    inspection_results.update(parsed)

                            if on_phase_complete and inspection_mode:
                                on_phase_complete('inspection')

                            if custom_commands:
                                self._print_cli_status(f"[{device['ip']}] 사용자 명령 {len(custom_commands)}개 실행 시작")
                                for idx, cmd in enumerate(custom_commands, start=1):
                                    self._print_cli_status(
                                        f"[{device['ip']}] 사용자 명령 실행 {idx}/{len(custom_commands)}: {cmd}"
                                    )
                                    conn.send_command(cmd, read_timeout=30)
                            
                            if backup_mode:
                                backup_cmd = self._get_backup_command(device['vendor'], device['os'])
                                if backup_cmd:
                                    self._print_cli_status(f"[{device['ip']}] 백업 명령 실행: {backup_cmd}")
                                    backup_output = conn.send_command(backup_cmd, read_timeout=60)
                                    backup_filename = os.path.join(self.backup_dir, f"{device['ip']}_{device['vendor']}_{device['os']}.txt")
                                    with open(backup_filename, 'w', encoding='utf-8') as f:
                                        f.write(backup_output)
                                    self._print_cli_status(f"[{device['ip']}] 백업 파일 저장 완료: {backup_filename}")
                                    inspection_results["backup_file"] = backup_filename

                            if on_phase_complete and backup_mode:
                                on_phase_complete('backup')

                            if custom_commands:
                                inspection_results["custom_commands_executed"] = len(custom_commands)
                            return device, inspection_results
                    except Exception as e:
                        last_error = e
                        retry_count += 1
                        self.logger.warning("Netmiko 연결 시도 %d 실패 (%s): %s", retry_count, device['ip'], e)
                        if retry_count >= self.max_retries:
                            return device, {"error": f"Netmiko 연결 실패: {str(e)}"}
                        time.sleep(2 ** retry_count)
                        continue
            except Exception as e:
                last_error = e
                retry_count += 1
                self.logger.warning("연결 시도 %d 실패 (%s): %s", retry_count, device['ip'], e)
                
                with open(session_log_file, 'a', encoding='utf-8') as log:
                    log.write(f"\n{'='*50}\n")
                    log.write(f"연결 시도 {retry_count} 실패 - {datetime.now()}\n")
                    log.write(f"오류: {str(e)}\n")
                    log.write(f"{'='*50}\n\n")
                
                if retry_count < self.max_retries:
                    time.sleep(2 ** retry_count)
                    continue
                else:
                    return device, {"error": f"최종 연결 실패: {str(e)}"}
    
    def load_devices(self, devices: list[dict]):
        for idx, device in enumerate(devices, start=1):
            device['device_index'] = idx
        self.devices = devices

    def get_device_profiles(self) -> list[dict[str, object]]:
        """대시보드에 전달할 장비 프로필(IP, 벤더, OS, 명령어 수)을 반환합니다."""
        profiles: list[dict[str, object]] = []
        for device in self.devices:
            vendor = str(device.get("vendor", "")).strip()
            os_name = str(device.get("os", "")).strip()
            cmd_count = len(self._get_device_commands(vendor, os_name))
            has_backup = bool(self._get_backup_command(vendor, os_name))
            profiles.append({
                "ip": device["ip"],
                "vendor": vendor,
                "os": os_name,
                "command_count": cmd_count,
                "has_backup": has_backup,
            })
        return profiles

    def _format_progress_bar(self, completed: int, total: int, width: int = 24) -> str:
        """진행률 표시를 ASCII 바 형태로 생성합니다."""
        if total <= 0:
            return f"[{'-' * width}] 0/0 (0%)"
        filled = int(width * completed / total)
        bar = "#" * filled + "-" * (width - filled)
        percent = int((completed / total) * 100)
        return f"[{bar}] {completed}/{total} ({percent}%)"

    def _print_cli_status(self, message: str) -> None:
        """로그 레벨과 무관하게 CLI에 진행 상황을 출력합니다."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        thread_name = threading.current_thread().name
        self._emit_status_event(
            "log",
            message=message,
            timestamp=timestamp,
            thread=thread_name,
        )

        if self.status_callback:
            return

        with self.cli_lock:
            sys.stdout.write(f"{timestamp} [{thread_name}] {message}\n")
            sys.stdout.flush()

    def _emit_status_event(self, event_type: str, **payload: object) -> None:
        """상태 콜백으로 이벤트를 전달합니다."""
        if not self.status_callback:
            return
        try:
            event: dict[str, object] = {"type": event_type}
            event.update(payload)
            self.status_callback(event)
        except Exception as e:
            self.logger.debug("상태 콜백 전달 실패: %s", e)

    def _print_pipeline_progress(
        self,
        insp_done: int,
        insp_total: int,
        bkup_done: int,
        bkup_total: int,
        stage: str,
        device_ip: str,
        status_msg: str,
    ) -> None:
        """파이프라인 진행률을 점검/백업 분리하여 표시합니다."""
        insp_bar = self._format_progress_bar(insp_done, insp_total, width=20)
        bkup_bar = self._format_progress_bar(bkup_done, bkup_total, width=20)
        self._print_cli_status(
            f"점검: {insp_bar} | 백업: {bkup_bar} | [{stage}] {device_ip}: {status_msg}"
        )

    def inspect_devices(self, backup_only: bool = False):
        """네트워크 장비를 점검하고 결과를 저장합니다."""
        if backup_only:
            self.logger.info("장비 백업 시작")
            self._print_cli_status("장비 백업을 시작합니다.")
        else:
            self.logger.info("장비 점검 시작")
            self._print_cli_status("장비 점검을 시작합니다.")
            
        total_devices = len(self.devices)
        completed_devices = 0
        success_count = 0
        fail_count = 0
        self._print_cli_status(f"총 장비 수: {total_devices}대")
        
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_device = {}
            for device in self.devices:
                if backup_only:
                    future = executor.submit(self._backup_device, device)
                else:
                    future = executor.submit(self._inspect_device, device)
                future_to_device[future] = device
            
            for future in as_completed(future_to_device):
                device = future_to_device[future]
                status_message = "성공"
                try:
                    result = future.result()
                    with self.results_lock:
                        self.results.append(result)
                    if result.get('status') == 'error':
                        status_message = f"실패 - 오류: {result.get('error_message', '알 수 없는 오류')}"
                except Exception as e:
                    self.logger.error("장비 처리 중 오류 발생: %s - %s", device['ip'], e)
                    with self.results_lock:
                        self.results.append({
                            'ip': device['ip'],
                            'vendor': device['vendor'],
                            'os': device['os'],
                            'status': 'error',
                            'error_message': str(e)
                        })
                    status_message = f"실패 - 오류: {str(e)}"
                finally:
                    completed_devices += 1
                    is_success = status_message.startswith("성공")
                    if is_success:
                        success_count += 1
                    else:
                        fail_count += 1
                    elapsed_sec = result.get('_elapsed_seconds', 0) if isinstance(result, dict) else 0
                    self._emit_status_event(
                        "device_complete",
                        success=is_success,
                        ip=device['ip'],
                        vendor=device.get('vendor', ''),
                        os=device.get('os', ''),
                        elapsed_seconds=elapsed_sec,
                    )
                    progress = self._format_progress_bar(completed_devices, total_devices)
                    self.logger.info("진행 상황: %s | IP: %s | 상태: %s", progress, device['ip'], status_message)
                    self._print_cli_status(
                        f"진행: {progress} | IP: {device['ip']} | 상태: {status_message} | 성공 {success_count} / 실패 {fail_count}"
                    )
        
        with self.results_lock:
            device_order = {device['ip']: i for i, device in enumerate(self.devices)}
            self.results.sort(key=lambda r: device_order.get(r.get('ip'), float('inf')))
        
        if backup_only:
            self.logger.info("장비 백업 완료")
            self._print_cli_status(f"장비 백업 완료 (성공 {success_count} / 실패 {fail_count})")
        else:
            self.logger.info("장비 점검 완료")
            self._print_cli_status(f"장비 점검 완료 (성공 {success_count} / 실패 {fail_count})")

    def _attach_custom_command_metadata(self, result: dict, device: dict) -> None:
        inspection_results = result.setdefault('inspection_results', {})
        if device.get('_custom_command_profile_id'):
            inspection_results['Command Profile ID'] = str(
                device['_custom_command_profile_id'],
            )
        if device.get('_custom_command_rendered_count') is not None:
            inspection_results['Rendered Command Count'] = device[
                '_custom_command_rendered_count'
            ]
        if device.get('_custom_command_values_path'):
            inspection_results['Template Values File'] = str(
                device['_custom_command_values_path'],
            )

    def run_selected_actions(
        self,
        inspection_mode: bool = False,
        backup_mode: bool = False,
        custom_commands: list[str] | dict[str, list[str]] | None = None,
    ) -> None:
        """Run the selected action set using a single session per device."""
        if not inspection_mode and not backup_mode and custom_commands is None:
            self.logger.info("No actions selected. Skipping run.")
            self.results = []
            return

        selected_actions: list[str] = []
        if inspection_mode:
            selected_actions.append("inspection")
        if backup_mode:
            selected_actions.append("backup")
        if custom_commands is not None:
            selected_actions.append("custom_commands")
        action_label = "+".join(selected_actions)

        self.logger.info("Selected action run started: %s", action_label)
        self._print_cli_status(f"Selected action run started: {action_label}")

        total_devices = len(self.devices)
        completed_devices = 0
        success_count = 0
        fail_count = 0
        self._print_cli_status(f"Total devices: {total_devices}")

        with self.results_lock:
            self.results = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_device = {}
            for device in self.devices:
                device_commands = custom_commands
                if isinstance(custom_commands, dict):
                    device_commands = custom_commands.get(str(device.get('ip', '')).strip(), [])
                future = executor.submit(
                    self._run_selected_actions_device,
                    device,
                    inspection_mode=inspection_mode,
                    backup_mode=backup_mode,
                    custom_commands=device_commands,
                )
                future_to_device[future] = device

            for future in as_completed(future_to_device):
                device = future_to_device[future]
                result = {}
                is_success = False
                status_message = "failed"
                try:
                    result = future.result()
                    is_success = result.get('status') != 'error'
                    status_message = "success" if is_success else (
                        f"failed - error: {result.get('error_message', 'unknown error')}"
                    )
                    with self.results_lock:
                        self.results.append(result)
                except Exception as e:
                    self.logger.error("Selected action device run failed: %s - %s", device['ip'], e)
                    with self.results_lock:
                        self.results.append({
                            'ip': device['ip'],
                            'vendor': device['vendor'],
                            'os': device['os'],
                            'status': 'error',
                            'error_message': str(e),
                            'inspection_results': {},
                            'backup_file': '',
                        })
                    status_message = f"failed - error: {str(e)}"
                finally:
                    completed_devices += 1
                    if is_success:
                        success_count += 1
                    else:
                        fail_count += 1
                    elapsed_sec = result.get('_elapsed_seconds', 0) if isinstance(result, dict) else 0
                    self._emit_status_event(
                        "device_complete",
                        success=is_success,
                        ip=device['ip'],
                        vendor=device.get('vendor', ''),
                        os=device.get('os', ''),
                        elapsed_seconds=elapsed_sec,
                    )
                    progress = self._format_progress_bar(completed_devices, total_devices)
                    self.logger.info(
                        "Selected action progress: %s | IP: %s | status: %s",
                        progress,
                        device['ip'],
                        status_message,
                    )
                    self._print_cli_status(
                        f"Progress: {progress} | IP: {device['ip']} | "
                        f"Status: {status_message} | Success {success_count} / Fail {fail_count}"
                    )

        with self.results_lock:
            device_order = {device['ip']: i for i, device in enumerate(self.devices)}
            self.results.sort(key=lambda r: device_order.get(r.get('ip'), float('inf')))

        self.logger.info(
            "Selected action run completed: %s (success %s / fail %s)",
            action_label,
            success_count,
            fail_count,
        )
        self._print_cli_status(
            f"Selected action run completed ({action_label}) "
            f"(success {success_count} / fail {fail_count})"
        )

    def run_custom_commands(self, commands: list[str] | dict[str, list[str]]):
        """사용자 명령어 목록을 장비에 순차 실행합니다."""
        self.logger.info("사용자 명령 실행 시작")
        self._print_cli_status("사용자 명령 실행을 시작합니다.")

        total_devices = len(self.devices)
        completed_devices = 0
        success_count = 0
        fail_count = 0
        self._print_cli_status(f"총 장비 수: {total_devices}대")

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_device = {}
            for device in self.devices:
                device_commands = commands
                if isinstance(commands, dict):
                    device_commands = commands.get(str(device.get('ip', '')).strip(), [])
                future = executor.submit(self._run_custom_commands_device, device, device_commands)
                future_to_device[future] = device

            for future in as_completed(future_to_device):
                device = future_to_device[future]
                status_message = "성공"
                result = {}
                try:
                    result = future.result()
                    with self.results_lock:
                        self.results.append(result)
                    if result.get('status') == 'error':
                        status_message = f"실패 - 오류: {result.get('error_message', '알 수 없는 오류')}"
                except Exception as e:
                    self.logger.error("장비 처리 중 오류 발생: %s - %s", device['ip'], e)
                    with self.results_lock:
                        self.results.append({
                            'ip': device['ip'],
                            'vendor': device['vendor'],
                            'os': device['os'],
                            'status': 'error',
                            'error_message': str(e)
                        })
                    status_message = f"실패 - 오류: {str(e)}"
                finally:
                    completed_devices += 1
                    is_success = status_message.startswith("성공")
                    if is_success:
                        success_count += 1
                    else:
                        fail_count += 1
                    elapsed_sec = result.get('_elapsed_seconds', 0) if isinstance(result, dict) else 0
                    self._emit_status_event(
                        "device_complete",
                        success=is_success,
                        ip=device['ip'],
                        vendor=device.get('vendor', ''),
                        os=device.get('os', ''),
                        elapsed_seconds=elapsed_sec,
                    )
                    progress = self._format_progress_bar(completed_devices, total_devices)
                    self.logger.info("진행 상황: %s | IP: %s | 상태: %s", progress, device['ip'], status_message)
                    self._print_cli_status(
                        f"진행: {progress} | IP: {device['ip']} | 상태: {status_message} | 성공 {success_count} / 실패 {fail_count}"
                    )

        with self.results_lock:
            device_order = {device['ip']: i for i, device in enumerate(self.devices)}
            self.results.sort(key=lambda r: device_order.get(r.get('ip'), float('inf')))

        self.logger.info("사용자 명령 실행 완료")
        self._print_cli_status(f"사용자 명령 실행 완료 (성공 {success_count} / 실패 {fail_count})")
            
    def inspect_and_backup_devices(self):
        """네트워크 장비를 단일 연결로 점검 후 백업합니다."""
        self.logger.info("장비 점검 및 백업 시작")
        self._print_cli_status("장비 점검 및 백업을 시작합니다.")
        total_devices = len(self.devices)
        self._print_cli_status(f"총 장비 수: {total_devices}대")

        inspection_done = 0
        backup_total = 0
        backup_done = 0
        counter_lock = threading.Lock()
        combined_results: dict[str, dict] = {}

        def on_inspection_done(ip: str, success: bool) -> None:
            nonlocal inspection_done, backup_total
            with counter_lock:
                inspection_done += 1
                if success:
                    backup_total += 1
                self._print_pipeline_progress(
                    inspection_done, total_devices,
                    backup_done, backup_total,
                    "점검완료" if success else "점검실패", ip,
                    "백업 진행 중" if success else "점검 실패"
                )

        def on_backup_done(ip: str, success: bool) -> None:
            nonlocal backup_done
            with counter_lock:
                backup_done += 1
                self._print_pipeline_progress(
                    inspection_done, total_devices,
                    backup_done, backup_total,
                    "백업완료" if success else "백업실패", ip,
                    "성공" if success else "실패"
                )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._inspect_and_backup_device,
                    device,
                    on_inspection_done,
                    on_backup_done,
                ): device
                for device in self.devices
            }

            for future in as_completed(futures):
                device = futures[future]
                ip = device['ip']
                try:
                    result = future.result()
                except Exception as e:
                    self.logger.error("점검+백업 처리 중 오류: %s - %s", ip, e)
                    result = {
                        'ip': ip,
                        'vendor': device['vendor'],
                        'os': device['os'],
                        'status': 'error',
                        'error_message': str(e),
                        'inspection_results': {},
                        'backup_file': '',
                        '_elapsed_seconds': 0,
                    }

                combined_results[ip] = {
                    'ip': ip,
                    'vendor': result.get('vendor', device['vendor']),
                    'os': result.get('os', device['os']),
                    'status': result.get('status', 'success'),
                    'error_message': result.get('error_message', ''),
                    'inspection_results': result.get('inspection_results', {}),
                    'backup_file': result.get('backup_file', ''),
                }

                self._emit_status_event(
                    "device_complete",
                    success=result.get('status') != 'error',
                    ip=ip,
                    vendor=device.get('vendor', ''),
                    os=device.get('os', ''),
                    elapsed_seconds=result.get('_elapsed_seconds', 0),
                )

        with self.results_lock:
            device_order = {d['ip']: i for i, d in enumerate(self.devices)}
            self.results = list(combined_results.values())
            self.results.sort(key=lambda r: device_order.get(r.get('ip'), float('inf')))

        success_count = sum(1 for result in self.results if result.get('status') != 'error')
        fail_count = len(self.results) - success_count

        self.logger.info("장비 점검 및 백업 완료")
        self._print_cli_status(
            f"장비 점검 및 백업 완료 (성공 {success_count} / 실패 {fail_count})"
        )

    def _inspect_and_backup_device(
        self,
        device: dict,
        on_inspection_done: Callable[[str, bool], None] | None = None,
        on_backup_done: Callable[[str, bool], None] | None = None,
    ) -> dict:
        """단일 SSH 연결로 점검 후 백업을 수행합니다."""
        _start = time.monotonic()
        device_index = device.get('device_index', 'NA')
        threading.current_thread().name = f"Device-{device_index}"
        ip = device['ip']
        self.logger.info("장비 점검+백업 시작: %s", ip)
        self._print_cli_status(f"[{ip}] 점검+백업 시작")

        result: dict = {
            'ip': ip,
            'vendor': device['vendor'],
            'os': device['os'],
            'status': 'success',
            'error_message': '',
            'inspection_results': {},
            'backup_file': ''
        }

        inspection_reported = False

        def phase_callback(phase: str) -> None:
            nonlocal inspection_reported
            if phase == 'inspection' and not inspection_reported:
                inspection_reported = True
                if on_inspection_done:
                    on_inspection_done(ip, True)

        try:
            device, connection_results = self._connect_to_device(
                device,
                inspection_mode=True,
                backup_mode=True,
                on_phase_complete=phase_callback,
            )

            if 'error' in connection_results:
                result['status'] = 'error'
                result['error_message'] = connection_results['error']
                if not inspection_reported and on_inspection_done:
                    on_inspection_done(ip, False)
                return result

            result['inspection_results'] = {
                k: v for k, v in connection_results.items()
                if k not in ('backup_file', 'backup_error')
            }

            if 'backup_file' in connection_results:
                result['backup_file'] = connection_results['backup_file']
                if on_backup_done:
                    on_backup_done(ip, True)
            elif 'backup_error' in connection_results:
                result['status'] = 'error'
                result['error_message'] = f"백업: {connection_results['backup_error']}"
                if on_backup_done:
                    on_backup_done(ip, False)
            else:
                if on_backup_done:
                    on_backup_done(ip, True)

            self.logger.info("장비 점검+백업 완료: %s", ip)
            self._print_cli_status(f"[{ip}] 점검+백업 완료")
            return result

        except Exception as e:
            self.logger.error("장비 점검+백업 중 오류: %s - %s", ip, e)
            result['status'] = 'error'
            result['error_message'] = str(e)
            if not inspection_reported and on_inspection_done:
                on_inspection_done(ip, False)
            return result
        finally:
            result['_elapsed_seconds'] = time.monotonic() - _start

    def _inspect_device(self, device: dict, session_log_suffix: str | None = None) -> dict:
        """단일 장비를 점검합니다."""
        _start = time.monotonic()
        device_index = device.get('device_index', 'NA')
        threading.current_thread().name = f"Device-{device_index}:Inspect"
        self.logger.info("장비 점검 시작: %s", device['ip'])
        self._print_cli_status(f"[{device['ip']}] 점검 시작")
        result = {
            'ip': device['ip'],
            'vendor': device['vendor'],
            'os': device['os'],
            'status': 'success',
            'error_message': '',
            'inspection_results': {}
        }
        
        try:
            device, inspection_results = self._connect_to_device(
                device,
                inspection_mode=True,
                backup_mode=False,
                session_log_suffix=session_log_suffix
            )
            
            if 'error' in inspection_results:
                result['status'] = 'error'
                result['error_message'] = inspection_results['error']
                return result
                
            result['inspection_results'] = inspection_results
            
            self.logger.info("장비 점검 완료: %s", device['ip'])
            self._print_cli_status(f"[{device['ip']}] 점검 완료")
            return result

        except Exception as e:
            self.logger.error("장비 점검 중 오류 발생: %s - %s", device['ip'], e)
            result['status'] = 'error'
            result['error_message'] = str(e)
            return result
        finally:
            result['_elapsed_seconds'] = time.monotonic() - _start

    def _run_custom_commands_device(
        self,
        device: dict,
        commands: list[str],
        session_log_suffix: str | None = None
    ) -> dict:
        """단일 장비에 사용자 명령어를 실행합니다."""
        _start = time.monotonic()
        device_index = device.get('device_index', 'NA')
        threading.current_thread().name = f"Device-{device_index}:Cmds"
        self.logger.info("사용자 명령 실행 시작: %s", device['ip'])
        self._print_cli_status(f"[{device['ip']}] 사용자 명령 실행 시작")
        result = {
            'ip': device['ip'],
            'vendor': device['vendor'],
            'os': device['os'],
            'status': 'success',
            'error_message': '',
            'inspection_results': {},
        }

        if not commands:
            result['status'] = 'error'
            result['error_message'] = 'No commands were assigned to this device.'
            return result

        try:
            device, command_results = self._connect_to_device(
                device,
                inspection_mode=False,
                backup_mode=False,
                session_log_suffix=session_log_suffix,
                custom_commands=commands
            )

            if 'error' in command_results:
                result['status'] = 'error'
                result['error_message'] = command_results['error']
                return result

            result['inspection_results'] = {
                key: value
                for key, value in command_results.items()
                if key not in {'error', 'backup_file', 'backup_error'}
            }
            self._attach_custom_command_metadata(result, device)

            self.logger.info("사용자 명령 실행 완료: %s", device['ip'])
            self._print_cli_status(f"[{device['ip']}] 사용자 명령 실행 완료")
            return result

        except Exception as e:
            self.logger.error("사용자 명령 실행 중 오류 발생: %s - %s", device['ip'], e)
            result['status'] = 'error'
            result['error_message'] = str(e)
            return result
        finally:
            result['_elapsed_seconds'] = time.monotonic() - _start

    def _run_selected_actions_device(
        self,
        device: dict,
        *,
        inspection_mode: bool,
        backup_mode: bool,
        custom_commands: list[str] | None = None,
        session_log_suffix: str | None = None,
    ) -> dict:
        """Run the selected action combination in a single device session."""
        _start = time.monotonic()
        device_index = device.get('device_index', 'NA')
        threading.current_thread().name = f"Device-{device_index}:Selected"
        result = {
            'ip': device['ip'],
            'vendor': device['vendor'],
            'os': device['os'],
            'status': 'success',
            'error_message': '',
            'inspection_results': {},
            'backup_file': '',
        }

        if custom_commands is not None and not custom_commands:
            result['status'] = 'error'
            result['error_message'] = 'No commands were assigned to this device.'
            return result

        try:
            device, connection_results = self._connect_to_device(
                device,
                inspection_mode=inspection_mode,
                backup_mode=backup_mode,
                session_log_suffix=session_log_suffix,
                custom_commands=custom_commands,
            )

            if 'error' in connection_results:
                result['status'] = 'error'
                result['error_message'] = connection_results['error']
                return result

            result['inspection_results'] = {
                key: value
                for key, value in connection_results.items()
                if key not in {'error', 'backup_file', 'backup_error'}
            }

            if custom_commands is not None:
                self._attach_custom_command_metadata(result, device)

            if backup_mode:
                if 'backup_file' in connection_results:
                    result['backup_file'] = connection_results['backup_file']
                elif 'backup_error' in connection_results:
                    result['status'] = 'error'
                    result['error_message'] = f"Backup: {connection_results['backup_error']}"

            return result

        except Exception as e:
            self.logger.error("Selected action execution failed: %s - %s", device['ip'], e)
            result['status'] = 'error'
            result['error_message'] = str(e)
            return result
        finally:
            result['_elapsed_seconds'] = time.monotonic() - _start

    def _backup_device(self, device: dict, session_log_suffix: str | None = None) -> dict:
        """단일 장비를 백업합니다."""
        _start = time.monotonic()
        device_index = device.get('device_index', 'NA')
        threading.current_thread().name = f"Device-{device_index}:Backup"
        self.logger.info("장비 백업 시작: %s", device['ip'])
        self._print_cli_status(f"[{device['ip']}] 백업 시작")
        result = {
            'ip': device['ip'],
            'vendor': device['vendor'],
            'os': device['os'],
            'status': 'success',
            'error_message': '',
            'backup_file': ''
        }
        
        try:
            device, connection_results = self._connect_to_device(
                device,
                inspection_mode=False,
                backup_mode=True,
                session_log_suffix=session_log_suffix
            )
            
            if 'error' in connection_results:
                result['status'] = 'error'
                result['error_message'] = connection_results['error']
                return result
            
            if 'backup_error' in connection_results:
                result['status'] = 'error'
                result['error_message'] = connection_results['backup_error']
                return result
                
            if 'backup_file' in connection_results:
                result['backup_file'] = connection_results['backup_file']
            
            self.logger.info("장비 백업 완료: %s", device['ip'])
            self._print_cli_status(f"[{device['ip']}] 백업 완료")
            return result

        except Exception as e:
            self.logger.error("장비 백업 중 오류 발생: %s - %s", device['ip'], e)
            result['status'] = 'error'
            result['error_message'] = str(e)
            return result
        finally:
            result['_elapsed_seconds'] = time.monotonic() - _start
