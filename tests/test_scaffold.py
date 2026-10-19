"""Scaffold checks: manifest, hacs.json, fault keys and the identifier script."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from aosmith_ble.const import FAULT_CODES

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "aosmith_ble"
SCRIPT = ROOT / "scripts" / "check_identifiers.py"


def _run_check(*paths: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, paths)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def test_manifest_matches_spec() -> None:
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    assert manifest["domain"] == "aosmith_ble"
    assert manifest["name"] == "A. O. Smith BLE"
    assert manifest["codeowners"] == ["@stumpylog"]
    assert manifest["requirements"] == ["aosmith-ble~=0.2.0"]
    assert manifest["dependencies"] == ["bluetooth_adapters"]
    assert manifest["bluetooth"] == [{"local_name": "iCOMM-*", "connectable": True}]
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "device"
    assert manifest["iot_class"] == "local_polling"
    assert manifest["loggers"] == ["aosmith_ble"]
    assert "version" in manifest
    assert "documentation" in manifest
    assert "issue_tracker" in manifest


def test_hacs_json() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["homeassistant"] == "2026.3.0"
    assert hacs["name"] == "A. O. Smith BLE"


def test_fault_keys_cover_library() -> None:
    from custom_components.aosmith_ble.const import FAULT_KEYS

    assert set(FAULT_CODES) <= set(FAULT_KEYS)
    values = list(FAULT_KEYS.values())
    assert len(values) == len(set(values))
    for value in values:
        assert re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)*", value), value


def test_check_identifiers_clean_on_repo() -> None:
    result = _run_check()
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_identifiers_flags_em_dash_mac_and_name(tmp_path: Path) -> None:
    dash = tmp_path / "dash.txt"
    dash.write_text("bad " + chr(0x2014) + " text\n")
    mac = tmp_path / "mac.txt"
    mac.write_text("addr " + ":".join(["12", "34", "56", "78", "9A", "BC"]) + "\n")
    name = tmp_path / "name.txt"
    name.write_text("name iCOMM-AC000W037" + "123456\n")
    for bad in (dash, mac, name):
        assert _run_check(bad).returncode != 0, bad.name

    ok = tmp_path / "ok.txt"
    ok.write_text(
        "AA:BB:CC:DD:EE:FF iCOMM-AC000W037XXXXXX glob iCOMM-* 02iQk000000EXAMPLE\n"
    )
    assert _run_check(ok).returncode == 0
