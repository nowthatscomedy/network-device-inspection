from __future__ import annotations

from pathlib import Path

import pytest

from core.command_templates import build_profile_command_payload, is_profile_command_path


PROFILE_TEXT = """
id: CISCO_SWITCH_PROFILE
vendor: CISCO
model: Catalyst
firmware: IOS-XE 17.x
variables:
  hostname:
    required: true
    type: string
  mgmt_ip:
    required: true
    type: ipv4
  mgmt_mask:
    required: true
    type: ipv4
  enable_voice_vlan:
    required: false
    type: bool
    default: false
  voice_vlan:
    required: false
    type: int
    default: 20
blocks:
  - name: base
    lines:
      - "hostname {{ hostname }}"
      - "interface vlan 99"
      - " ip address {{ mgmt_ip }} {{ mgmt_mask }}"
  - name: voice_vlan
    lines:
      - "switchport voice vlan {{ voice_vlan }}"
"""


def make_inventory() -> list[dict[str, object]]:
    return [
        {
            "ip": "192.0.2.10",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "admin",
            "password": "pw",
            "device_id": "SW-01",
        },
        {
            "ip": "192.0.2.11",
            "vendor": "cisco",
            "os": "ios",
            "connection_type": "ssh",
            "port": 22,
            "username": "admin",
            "password": "pw",
            "device_id": "SW-02",
        },
    ]


def test_is_profile_command_path_detects_yaml() -> None:
    assert is_profile_command_path("commands.yaml") is True
    assert is_profile_command_path("commands.txt") is False


def test_build_profile_command_payload_merges_template_values_by_device_id(tmp_path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE_TEXT, encoding="utf-8")
    values_path = tmp_path / "values.csv"
    values_path.write_text(
        "\n".join(
            [
                "device_id,hostname,mgmt_ip,mgmt_mask,enable_voice_vlan,voice_vlan",
                "SW-01,EDGE-01,10.0.0.11,255.255.255.0,false,20",
                "SW-02,EDGE-02,10.0.0.12,255.255.255.0,true,30",
            ]
        ),
        encoding="utf-8",
    )

    devices, command_map, profile = build_profile_command_payload(
        str(profile_path),
        make_inventory(),
        template_values_path=str(values_path),
    )

    assert profile.id == "CISCO_SWITCH_PROFILE"
    assert devices[0]["_custom_command_profile_id"] == "CISCO_SWITCH_PROFILE"
    assert command_map["192.0.2.10"][0] == "hostname EDGE-01"
    assert "switchport voice vlan 20" not in command_map["192.0.2.10"]
    assert "switchport voice vlan 30" in command_map["192.0.2.11"]


def test_build_profile_command_payload_uses_inventory_values_without_external_file(tmp_path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE_TEXT, encoding="utf-8")
    inventory = [
        {
            **make_inventory()[0],
            "hostname": "EDGE-11",
            "mgmt_ip": "10.10.10.11",
            "mgmt_mask": "255.255.255.0",
            "enable_voice_vlan": "true",
            "voice_vlan": "110",
        }
    ]

    devices, command_map, _ = build_profile_command_payload(str(profile_path), inventory)

    assert devices[0]["_custom_command_rendered_count"] == len(command_map["192.0.2.10"])
    assert command_map["192.0.2.10"][0] == "hostname EDGE-11"
    assert "switchport voice vlan 110" in command_map["192.0.2.10"]


def test_build_profile_command_payload_rejects_duplicate_match_values(tmp_path) -> None:
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(PROFILE_TEXT, encoding="utf-8")
    values_path = tmp_path / "values.csv"
    values_path.write_text(
        "\n".join(
            [
                "device_id,hostname,mgmt_ip,mgmt_mask",
                "SW-01,EDGE-01,10.0.0.11,255.255.255.0",
                "SW-01,EDGE-02,10.0.0.12,255.255.255.0",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        build_profile_command_payload(
            str(profile_path),
            make_inventory(),
            template_values_path=str(values_path),
        )

    assert "Duplicate 'device_id'" in str(exc_info.value)


def test_example_profile_files_render_per_device_commands() -> None:
    examples_dir = Path(__file__).resolve().parents[1] / "examples" / "batch_command_input"
    profile_path = examples_dir / "profile_access_switch.yaml"
    values_path = examples_dir / "profile_access_switch_values.csv"

    devices, command_map, profile = build_profile_command_payload(
        str(profile_path),
        make_inventory(),
        template_values_path=str(values_path),
    )

    assert profile.id == "ACCESS_SWITCH_INITIAL_SETUP"
    assert devices[0]["_custom_command_profile_id"] == "ACCESS_SWITCH_INITIAL_SETUP"
    assert command_map["192.0.2.10"][1] == "hostname BR-1F-01"
    assert not any("switchport voice vlan 20" in line for line in command_map["192.0.2.10"])
    assert any("switchport voice vlan 30" in line for line in command_map["192.0.2.11"])
