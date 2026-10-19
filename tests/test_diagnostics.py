"""Scrubbed diagnostics. Seeded identifiers are built at runtime."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aosmith_ble import (
    ChallengeRejectedError,
    DeviceInfo,
    DiagnosticReport,
    NotPairedError,
    UnknownDeviceError,
)
from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble import diagnostics
from custom_components.aosmith_ble.const import (
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    DOMAIN,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
)
from custom_components.aosmith_ble.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import ClientFactory
from .fakes import FakeClient

# Seeded identifiers, built so no tracked file holds a realistic value.
ADDRESS = ":".join(f"{n:02X}" for n in (0x3A, 0x4B, 0x5C, 0x6D, 0x7E, 0x8F))
SERIAL = "S" + "e" + "4" * 2 + "R" + "d" + "7" * 2 + "Q"
NAME = "iCOMM-" + "QQ" + "8" * 3 + "Z" + "1" * 3 + "5" + "2" * 2 + "6" * 3
CODE = "".join(ch for ch in NAME if ch.isdigit())[-6:]
ASSET = "02" + "xy" + "3" * 6 + "SEED" + "7"
# An asset ID the library learns during the diagnose run (a fresh slot).
NEW_ASSET = "02" + "nw" + "5" * 6 + "NEWA" + "9"


def _swap(text: str) -> str:
    chars = list(text)
    for i in range(0, len(chars) - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def _forms(value: str) -> set[str]:
    return {
        value.lower(),
        value.encode().hex(),
        _swap(value).encode().hex(),
    }


SEEDED_FORMS = (
    _forms(ADDRESS)
    | _forms(ADDRESS.replace(":", ""))
    | _forms(SERIAL)
    | _forms(NAME)
    | _forms(CODE)
    | _forms(ASSET)
    | _forms(NEW_ASSET)
)


def assert_no_identifiers(output: dict[str, Any]) -> None:
    """Plain JSON, and no seeded identifier as text, hex or byte-swapped hex."""
    dumped = json.dumps(output).lower()
    leaked = sorted(form for form in SEEDED_FORMS if form in dumped)
    assert leaked == []


def _raw_hex(text: str) -> str:
    return ("a55a" + text.encode().hex() + "00ff").upper()


def _seeded_report() -> DiagnosticReport:
    """A real report whose raw frames and decoded values carry the identifiers."""
    return DiagnosticReport(
        model=b"HPTS-99\x00",
        firmware=(9, 4),
        block_presence=((0, 30), (11, 20), (40, 0)),
        decoded_values={
            "asset_id": ASSET,
            "matched_profile": None,
            # Not a redacted key: only the scrub over every string catches it.
            "extra": f"seen {NAME} at {ADDRESS.lower()} code {CODE}",
        },
        raw_entries=(
            {
                "id": "init_0_request",
                "hex": "00" * 12,
                "source": "capture",
                "note": "redacted: init request is assetID-derived",
            },
            {
                "id": "_1_response",
                "hex": _raw_hex(_swap(SERIAL)),
                "source": "capture",
                "note": "captured during async_diagnose():  response",
            },
            {
                "id": "diagnose_identity_2_response",
                "hex": _raw_hex(ADDRESS.replace(":", "") + NAME + CODE),
                "source": "capture",
                "note": "captured during async_diagnose(): diagnose_identity response",
            },
        ),
    )


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """An entry holding the seeded identifiers (overrides conftest)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="A. O. Smith HPTS-99",
        unique_id=ADDRESS,
        data={
            CONF_ADDRESS: ADDRESS,
            CONF_PAIRING_CODE: CODE,
            CONF_ASSET_ID: ASSET,
        },
        options={CONF_POLL_INTERVAL: 60},
    )


@pytest.fixture
def service_info() -> Generator[MagicMock]:
    """The last advertisement, with manufacturer data equal to a marker."""
    info = MagicMock()
    info.name = NAME
    info.address = ADDRESS
    info.manufacturer_data = {0x1234: b"advert-marker"}
    info.advertisement = SimpleNamespace(local_name=NAME)
    with (
        patch(
            "custom_components.aosmith_ble.async_last_service_info",
            return_value=info,
        ),
        patch(
            "custom_components.aosmith_ble.diagnostics.async_last_service_info",
            return_value=info,
        ),
    ):
        yield info


async def _load_diagnostics_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    prepare: Callable[[FakeClient], None],
) -> None:
    def unknown(client: FakeClient) -> None:
        client.connect_effects = [
            UnknownDeviceError("no profile", model="HPTS-99", firmware=(9, 4))
        ]

    client_factory.prepare.extend([unknown, prepare])
    config_entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_forward_entry_setups"):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is None


def _with_report(report: DiagnosticReport | None = None) -> Callable[..., None]:
    def prepare(client: FakeClient) -> None:
        client.diagnose_result = report if report is not None else _seeded_report()

    return prepare


def _diagnose_calls(client: FakeClient) -> list[tuple]:
    return [call for call in client.calls if call[0] == "async_diagnose"]


async def test_seeded_identifiers_never_appear(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    service_info: MagicMock,
) -> None:
    await _load_diagnostics_only(hass, config_entry, client_factory, _with_report())
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert_no_identifiers(output)


async def test_every_raw_entry_is_zeros_of_the_original_length(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    report = _seeded_report()
    await _load_diagnostics_only(hass, config_entry, client_factory, _with_report())
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    entries = output["report"]["raw_entries"]
    assert len(entries) == len(report.raw_entries)
    for original, entry in zip(report.raw_entries, entries, strict=True):
        assert entry["id"] == original["id"]
        assert len(entry["hex"]) == len(original["hex"])
        assert set(entry["hex"]) == {"0"}
        assert "zeroed" in entry["note"]


async def test_no_service_info_or_advertisement_data(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    service_info: MagicMock,
) -> None:
    await _load_diagnostics_only(hass, config_entry, client_factory, _with_report())
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    dumped = json.dumps(output).lower()
    assert b"advert-marker".hex() not in dumped
    assert "advert-marker" not in dumped
    for word in ("service_info", "advertisement", "manufacturer", "rssi"):
        assert word not in dumped


async def test_diagnostics_only_diagnoses_once_and_closes(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    await _load_diagnostics_only(hass, config_entry, client_factory, _with_report())
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    setup_client, client = client_factory.built
    assert _diagnose_calls(setup_client) == []
    assert len(_diagnose_calls(client)) == 1
    # build_client, not connect_and_classify: no connect() before diagnose.
    names = [call[0] for call in client.calls]
    assert "connect" not in names
    assert names[-1] == "async_close"
    assert client.on_disconnect is None
    assert output["diagnostics_only"] is True
    assert output["error"] is None


async def test_diagnose_failure_returns_partial_scrubbed_output(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    service_info: MagicMock,
) -> None:
    def fail(client: FakeClient) -> None:
        client.diagnose_effects = [BleakError(f"lost {ADDRESS} {NAME} {CODE}")]

    await _load_diagnostics_only(hass, config_entry, client_factory, fail)
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert output["report"] is None
    assert output["error"]["class"] == "BleakError"
    assert PLACEHOLDER_NAME in output["error"]["text"]
    assert output["entry"]["options"] == {CONF_POLL_INTERVAL: 60}
    assert output["library_version"]
    assert [call[0] for call in client_factory.built[1].calls][-1] == "async_close"
    assert_no_identifiers(output)


@pytest.mark.parametrize("fails", [False, True], ids=["report", "error"])
async def test_asset_id_learned_by_the_diagnose_run_is_scrubbed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    service_info: MagicMock,
    fails: bool,
) -> None:
    report = dataclasses.replace(
        _seeded_report(),
        # Not a key the redaction knows: only the learned asset ID catches it.
        decoded_values={"unexpected_library_key": f"slot {NEW_ASSET}"},
    )

    def learns(client: FakeClient) -> None:
        client._asset_id = NEW_ASSET
        client.diagnose_result = report
        if fails:
            client.diagnose_effects = [BleakError(f"lost after {NEW_ASSET}")]

    await _load_diagnostics_only(hass, config_entry, client_factory, learns)
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    if fails:
        assert PLACEHOLDER_ASSET_ID in output["error"]["text"]
    else:
        assert output["report"]["decoded_values"] == {
            "unexpected_library_key": f"slot {PLACEHOLDER_ASSET_ID}"
        }
    assert_no_identifiers(output)


async def test_diagnose_timeout_returns_partial_output(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    def slow(client: FakeClient) -> None:
        client.delay = 5.0
        client.diagnose_result = _seeded_report()

    await _load_diagnostics_only(hass, config_entry, client_factory, slow)
    with patch.object(diagnostics, "DIAGNOSE_TIMEOUT", 0.01):
        output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert output["report"] is None
    assert output["error"]["class"] == "TimeoutError"
    assert output["entry"]["options"] == {CONF_POLL_INTERVAL: 60}
    assert [call[0] for call in client_factory.built[1].calls][-1] == "async_close"
    assert_no_identifiers(output)


@pytest.mark.parametrize(
    "error",
    [NotPairedError(f"slot empty for {ASSET}"), ChallengeRejectedError("rejected")],
    ids=["not_paired", "challenge_rejected"],
)
async def test_auth_errors_reported_with_button_instruction(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    error: Exception,
) -> None:
    def fail(client: FakeClient) -> None:
        client.diagnose_effects = [error]

    await _load_diagnostics_only(hass, config_entry, client_factory, fail)
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert output["report"] is None
    assert output["error"]["class"] == type(error).__name__
    assert "Press the Bluetooth button" in output["error"]["instruction"]
    assert_no_identifiers(output)


async def test_not_advertising_reported_without_a_client(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    await _load_diagnostics_only(hass, config_entry, client_factory, _with_report())
    mock_ble_device.return_value = None
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert len(client_factory.built) == 1
    assert output["report"] is None
    assert output["error"]["class"] == "not_advertising"
    assert "Press the Bluetooth button" in output["error"]["instruction"]


async def test_report_shape_matches_real_report(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    report = DiagnosticReport(
        model=b"HPTS-50\x00",
        firmware=(6, 3),
        block_presence=((0, 30), (11, 20)),
        decoded_values={
            "asset_id": PLACEHOLDER_ASSET_ID,
            "matched_profile": "hpts50-6.3",
        },
        raw_entries=(
            {
                "id": "_0_request",
                "hex": "A55A0B00",
                "source": "capture",
                "note": "captured during async_diagnose():  request",
            },
        ),
    )
    await _load_diagnostics_only(
        hass, config_entry, client_factory, _with_report(report)
    )
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    json.dumps(output)
    rendered = output["report"]
    assert set(rendered) == {f.name for f in dataclasses.fields(DiagnosticReport)}
    assert rendered["model"] == b"HPTS-50\x00".hex()
    assert rendered["firmware"] == [6, 3]
    assert rendered["block_presence"] == [[0, 30], [11, 20]]
    assert rendered["decoded_values"]["asset_id"] == "**REDACTED**"
    assert rendered["decoded_values"]["matched_profile"] == "hpts50-6.3"
    (entry,) = rendered["raw_entries"]
    assert set(entry) == set(report.raw_entries[0])
    assert entry["hex"] == "00000000"
    assert entry["source"] == "capture"


async def test_normal_entry_reports_coordinator_state_only(
    hass: HomeAssistant,
    load_heater: Callable[..., Any],
    service_info: MagicMock,
) -> None:
    heater = await load_heater(
        device_info=DeviceInfo(
            model="HPTS-50", serial=SERIAL, asset_id=ASSET, firmware=(6, 3)
        )
    )
    heater.client.read_effects = [BleakError(f"lost {ADDRESS} {SERIAL} {NAME}")]
    await heater.coordinator.async_refresh()
    await hass.async_block_till_done()

    output = await async_get_config_entry_diagnostics(hass, heater.entry)

    assert _diagnose_calls(heater.client) == []
    assert output["diagnostics_only"] is False
    assert output["entry"]["options"] == {CONF_POLL_INTERVAL: 60}
    assert output["model"] == "HPTS-50"
    assert output["firmware"] == [6, 3]
    assert output["profile"]["id"] == "placeholder-profile"
    assert output["profile"]["fields"]["setpoint"] == {"read": "ok", "write": "ok"}
    assert output["features"]["setpoint"] == {"value": 120.0, "trust": "ok"}
    # The raw word, so values outside the known levels show up in reports.
    assert output["features"]["hot_water_availability"] == {
        "value": 10,
        "trust": "partial",
    }
    assert output["profile"]["fields"]["hot_water_availability"]["read"] == "partial"
    health = output["health"]
    assert health["consecutive_failures"] == 1
    assert health["last_error_class"] == "BleakError"
    assert PLACEHOLDER_NAME in health["last_error"]
    assert health["last_success"] is not None
    assert health["flags"]["auth_failed"] is False
    assert "report" not in output
    assert_no_identifiers(output)


async def test_entry_not_loaded_never_diagnoses(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    output = await async_get_config_entry_diagnostics(hass, config_entry)
    assert client_factory.built == []
    assert output["diagnostics_only"] is False
    assert output["entry"]["state"] == ConfigEntryState.NOT_LOADED.value
    assert_no_identifiers(output)
