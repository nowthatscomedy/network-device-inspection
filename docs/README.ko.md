# NetOps Inspector (한국어)

언어: [English](../README.md) | [한국어](README.ko.md) | [日本語](README.ja.md) | [Español](README.es.md) | [Português (Brasil)](README.pt-BR.md) | [简体中文](README.zh-CN.md)

NetOps Inspector는 멀티 벤더 네트워크 장비 점검 및 설정 백업을 위한 CLI 도구입니다.
엑셀 장비 인벤토리를 읽고 SSH/Telnet으로 접속하여 점검 명령을 실행하고, 출력을 파싱해 결과 워크북을 생성합니다.

## 주요 기능

- 멀티 벤더 아키텍처 (`vendors/` 모듈)
- 점검 / 백업 / 점검+백업 실행 모드
- TXT 또는 Excel 기반 커스텀 명령 배치 실행
- 입력 Excel 유효성 검사 (필수 필드, 중복 IP, 벤더/OS 호환성)
- 네트워크 I/O 재시도 및 타임아웃 제어
- 실행 중 실시간 터미널 대시보드
- 장비별 세션 로그 파일
- 컬럼 별칭/순서 설정 가능한 결과 워크북 생성
- `custom_rules.yaml`을 통한 사용자 정의 파싱/명령 확장
- 다국어 UI/메시지 지원 (`en`, `ko`, `ja`, `es`, `pt-BR`, `zh-CN`)

## 지원 벤더 (현재 모듈)

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

지원 OS 값은 각 벤더 모듈과 `vendors/__init__.py`의 명령 매핑에 따라 달라집니다.

## 요구 사항

- Python 3.10+
- 대상 장비로의 네트워크 접근 가능 상태
- `requirements.txt`의 의존성

설치:

```bash
pip install -r requirements.txt
```

## 빠른 시작

실행:

```bash
python main.py
```

메인 메뉴:

1. 점검/백업 시작
2. 커스텀 명령 파일 실행
3. 설정 변경
4. Netmiko `device_type` 목록 표시
5. 종료

## Excel 입력 스키마

필수 컬럼:

- `ip`
- `vendor`
- `os`
- `connection_type` (`ssh` 또는 `telnet`)
- `port`
- `password`

선택 컬럼:

- `username`
- `enable_password`

예시:

| ip | vendor | os | connection_type | port | username | password | enable_password |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 192.168.1.10 | cisco | ios | ssh | 22 | admin | ****** | ****** |
| 192.168.1.20 | ruckus | icx | ssh | 22 | super | ****** | |

## 설정 (`settings.yaml`)

파일이 없으면 앱 디렉터리에 자동 생성됩니다.

주요 키:

- `console_log_level`: `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`
- `max_retries`: 연결 최대 재시도 횟수
- `timeout`: 연결 타임아웃(초)
- `max_workers`: 병렬 워커 수
- `inspection_excludes`: 벤더/OS별 파싱 제외 맵

점검 출력 관련 키:

- `column_aliases`: 점검 컬럼명 정규화
- `inspection_column_order_global`
- `inspection_column_order_by_profile`

i18n 관련 키:

- `language`
- `fallback_language`
- `input_column_aliases`

예시:

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

현재 허용되는 언어 코드:

- `en`
- `ko`
- `ja`
- `es`
- `pt-BR`
- `zh-CN`

현재 포함된 번역 파일:

- `locales/en.yaml`
- `locales/ko.yaml`
- `locales/ja.yaml`
- `locales/es.yaml`
- `locales/pt-BR.yaml`
- `locales/zh-CN.yaml`

지원되지 않는 언어 코드는 `en`으로 정규화됩니다.
선택한 로케일 파일 또는 키가 없으면 `fallback_language`를 거친 뒤 영어로 폴백됩니다.

## 다국어 README

- Korean: `docs/README.ko.md`
- Japanese: `docs/README.ja.md`
- Spanish: `docs/README.es.md`
- Portuguese (Brazil): `docs/README.pt-BR.md`
- Simplified Chinese: `docs/README.zh-CN.md`

## 커스텀 규칙 (`custom_rules.yaml`)

Python 코드를 수정하지 않고 명령/파서를 확장할 수 있습니다.

최상위 섹션:

- `inspection_commands`
- `backup_commands`
- `parsing_rules`
- `connection_overrides`
- `handler_overrides`

템플릿 파일:

- `custom_rules.example.yaml`

## 변수형 일괄 명령 입력

`작업 > 일괄 명령 입력`에서는 TXT/XLSX 명령 파일뿐 아니라 YAML 프로파일도 사용할 수 있습니다.
YAML 프로파일을 사용하면 장비마다 다른 값(IP, 호스트명, 게이트웨이 등)을 변수로 받아
장비별 명령어를 따로 렌더링해서 실행합니다.

예시 파일:

- `examples/batch_command_input/profile_access_switch.yaml`
- `examples/batch_command_input/profile_access_switch_values.csv`

사용 순서:

1. 인벤토리 Excel에 접속 정보와 가능하면 `device_id`를 준비합니다.
2. `작업` 메뉴에서 `일괄 명령 입력`을 선택합니다.
3. 명령 파일 경로에 YAML 프로파일을 선택합니다.
4. 템플릿 설정값 파일을 묻는 화면에서 장비별 값이 들어 있는 CSV/XLSX를 선택합니다.
   인벤토리 파일에 변수 컬럼이 이미 있으면 Enter로 건너뛸 수 있습니다.
5. 프로그램은 `device_id -> hostname -> ip` 순서로 인벤토리와 값 파일을 매칭합니다.
6. 각 장비마다 다른 명령어가 생성되어 한 세션 안에서 실행됩니다.

예를 들어 예시 CSV에서 `SW-01`은 아래와 같이 렌더링됩니다.

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

`SW-02`는 `enable_voice_vlan=true` 이므로 같은 기본 명령에 더해 `voice_vlan` 블록도 함께 들어갑니다.

알아두면 좋은 점:

- 변수 타입은 `string`, `ipv4`, `int`, `bool`을 지원합니다.
- 블록 이름이 `voice_vlan`이면 `enable_voice_vlan: false`일 때 해당 블록이 자동으로 제외됩니다.
- 별도 값 파일을 쓰지 않아도, 인벤토리 Excel 안에 같은 변수 컬럼이 있으면 그 값을 바로 사용할 수 있습니다.

## 출력 파일

생성 경로 (타임스탬프 포함):

- 점검 결과: `results/inspection_results_YYYYMMDD_HHMMSS.xlsx`
- 커스텀 명령 결과: `results/command_results_YYYYMMDD_HHMMSS.xlsx`
- 백업 파일: `backup/YYYYMMDD_HHMMSS/[IP]_[vendor]_[os].txt`
- 실행 로그: `logs/netops_inspector_YYYYMMDD_HHMMSS.log`
- 세션 로그: `session_logs/YYYYMMDD_HHMMSS/[IP]_[vendor]_[os].log`

## 테스트

```bash
python -m pytest
```

## 빌드 (Windows)

사용:

```bat
build.bat
```

스크립트는 저장소 루트의 `NetOpsInspector.spec` 파일을 필요로 합니다.

## 보안 참고 사항

- 소스 코드에 자격 증명을 하드코딩하지 마세요.
- 런타임 자격 증명은 환경 변수 또는 보안 전달 방식을 권장합니다.
- 로그 및 결과 파일은 민감한 운영 데이터로 취급하세요.

## 라이선스

MIT License
