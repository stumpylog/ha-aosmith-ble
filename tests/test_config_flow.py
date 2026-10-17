"""Config flow, reauth and options."""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aosmith_ble import (
    ChallengeRejectedError,
    ConfigurationIssue,
    ConnectionSlotsExhaustedError,
    NotConnectedError,
    NotPairedError,
    ProtocolError,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
)
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble.config_flow import has_pairing_code
from custom_components.aosmith_ble.const import (
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    DOMAIN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)

from .conftest import ClientFactory
from .fakes import FakeClient

# The last 6 digits of PLACEHOLDER_NAME.
NAME_CODE = "000037"
# Built at runtime: only the placeholder address may appear in the repo.
OTHER_ADDRESS = PLACEHOLDER_ADDRESS[:-1] + "0"
THIRD_ADDRESS = PLACEHOLDER_ADDRESS[:-1] + "1"
FOURTH_ADDRESS = PLACEHOLDER_ADDRESS[:-1] + "2"
CHANGED_ASSET_ID = "02iQk000000CHANGED"
CONFIRM_TEXT = (
    "Close the A. O. Smith app. Press the Bluetooth button on the heater's panel "
    "once so its LED flashes. If it was already flashing, turn Bluetooth off and "
    "on at the panel. Then submit."
)
EN_JSON = (
    Path(__file__).parent.parent
    / "custom_components"
    / "aosmith_ble"
    / "translations"
    / "en.json"
)


def service_info(
    name: str = PLACEHOLDER_NAME, address: str = PLACEHOLDER_ADDRESS
) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=AdvertisementData(
            local_name=name,
            manufacturer_data={},
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=0,
        tx_power=None,
    )


@pytest.fixture(autouse=True)
def _bluetooth(enable_bluetooth: None) -> None:
    """A real Bluetooth manager for the integration's dependencies."""


@pytest.fixture(autouse=True)
def setup_entry() -> Generator[AsyncMock]:
    """Entries the flow creates or reloads do not run the real setup."""
    with patch(
        "custom_components.aosmith_ble.async_setup_entry",
        new_callable=AsyncMock,
        return_value=True,
    ) as patched:
        yield patched


@pytest.fixture
def discovered() -> Generator[list[BluetoothServiceInfoBleak]]:
    infos: list[BluetoothServiceInfoBleak] = []
    with patch(
        "custom_components.aosmith_ble.config_flow.async_discovered_service_info",
        return_value=infos,
    ):
        yield infos


async def _discover(hass: HomeAssistant, name: str = PLACEHOLDER_NAME) -> Any:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(name)
    )


def _closed(client: FakeClient) -> bool:
    return ("async_close",) in client.calls


def test_has_pairing_code() -> None:
    assert has_pairing_code(PLACEHOLDER_NAME)
    assert has_pairing_code("iCOMM-123456")
    assert not has_pairing_code("iCOMM-12345")
    assert not has_pairing_code("iCOMM-ABC")


def test_confirm_text_is_the_spec_instruction() -> None:
    strings = json.loads(EN_JSON.read_text(encoding="utf-8"))
    steps = strings["config"]["step"]
    assert steps["confirm"]["description"] == CONFIRM_TEXT
    assert steps["reauth_confirm"]["description"] == CONFIRM_TEXT


async def test_discovery_shows_confirm_form(hass: HomeAssistant) -> None:
    result = await _discover(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] in (None, {})


async def test_discovery_already_configured_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    config_entry.add_to_hass(hass)
    result = await _discover(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("name", ["Other-123456", "iCOMM-ABC12"])
async def test_discovery_not_supported(hass: HomeAssistant, name: str) -> None:
    result = await _discover(hass, name)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_supported"


async def test_picker_lists_only_unconfigured_heaters_with_a_code(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    discovered: list[BluetoothServiceInfoBleak],
) -> None:
    config_entry.add_to_hass(hass)
    discovered.extend(
        [
            service_info(PLACEHOLDER_NAME, PLACEHOLDER_ADDRESS),  # configured
            service_info(PLACEHOLDER_NAME, OTHER_ADDRESS),
            service_info("Other-123456", THIRD_ADDRESS),
            service_info("iCOMM-ABC12", FOURTH_ADDRESS),
        ]
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    (field,) = schema
    assert list(schema[field].container) == [OTHER_ADDRESS]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ADDRESS: OTHER_ADDRESS}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"


async def test_picker_without_devices_aborts(
    hass: HomeAssistant, discovered: list[BluetoothServiceInfoBleak]
) -> None:
    discovered.append(service_info("Other-123456"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_confirm_creates_entry(
    hass: HomeAssistant, client_factory: ClientFactory, mock_ble_device: MagicMock
) -> None:
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "A. O. Smith HPTS-50"
    assert result["data"] == {
        CONF_ADDRESS: PLACEHOLDER_ADDRESS,
        CONF_PAIRING_CODE: NAME_CODE,
        CONF_ASSET_ID: PLACEHOLDER_ASSET_ID,
    }
    assert isinstance(result["data"][CONF_PAIRING_CODE], str)
    assert result["result"].unique_id == PLACEHOLDER_ADDRESS
    (client,) = client_factory.built
    _, _, kwargs = client.calls[0]
    assert kwargs["asset_id"] is None
    assert kwargs["pairing_code"] == NAME_CODE
    assert _closed(client)


def _effects(*effects: Exception) -> list[Exception | None]:
    return list(effects)


ERROR_TABLE = [
    pytest.param(_effects(NotPairedError("empty")), "not_paired", id="not_paired"),
    pytest.param(_effects(BleakError("x")), "cannot_connect", id="bleak"),
    pytest.param(_effects(TimeoutError()), "cannot_connect", id="timeout"),
    pytest.param(_effects(NotConnectedError("x")), "cannot_connect", id="not_conn"),
    pytest.param(_effects(ProtocolError("x")), "cannot_connect", id="protocol"),
    pytest.param(
        _effects(ConnectionSlotsExhaustedError("x")),
        "no_connection_slots",
        id="slots",
    ),
    pytest.param(
        _effects(ChallengeRejectedError("x"), ChallengeRejectedError("x")),
        "session_rejected",
        id="auth_failed",
    ),
    pytest.param(
        _effects(SessionError("x"), SessionError("x")),
        "session_rejected",
        id="session",
    ),
    pytest.param(
        _effects(SessionError("x"), ChallengeRejectedError("x")),
        "session_rejected",
        id="raw_challenge",
    ),
    pytest.param(
        _effects(UnsupportedConfigurationError("x", reason=ConfigurationIssue.UNITS)),
        "unsupported_units",
        id="units",
    ),
    pytest.param(
        _effects(UnsupportedConfigurationError("x", reason=ConfigurationIssue.MTU)),
        "unsupported_mtu",
        id="mtu",
    ),
    pytest.param(
        # A reason a future library release might add.
        _effects(UnsupportedConfigurationError("x", reason="future")),  # type: ignore[arg-type]
        "unsupported_configuration",
        id="future_reason",
    ),
    pytest.param(_effects(RuntimeError("x")), "unknown", id="unknown"),
]


@pytest.mark.parametrize("error", sorted({p.values[1] for p in ERROR_TABLE}))
def test_every_form_error_is_translated(error: str) -> None:
    strings = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "aosmith_ble"
            / "translations"
            / "en.json"
        ).read_text()
    )
    assert strings["config"]["error"][error]


@pytest.mark.parametrize(("effects", "error"), ERROR_TABLE)
async def test_confirm_error_table(
    hass: HomeAssistant,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    effects: list[Exception | None],
    error: str,
) -> None:
    client_factory.prepare.append(lambda client: client.connect_effects.extend(effects))
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": error}
    (client,) = client_factory.built
    assert _closed(client)

    # The form stays open, and a retry after the button press succeeds.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert all(_closed(c) for c in client_factory.built)


async def test_confirm_without_ble_device_is_cannot_connect(
    hass: HomeAssistant, client_factory: ClientFactory
) -> None:
    with patch(
        "custom_components.aosmith_ble.connection.async_ble_device_from_address",
        return_value=None,
    ):
        result = await _discover(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert client_factory.built == []


@pytest.mark.parametrize(
    ("model", "title"),
    [("HPTS-80", "A. O. Smith HPTS-80"), (None, "A. O. Smith water heater")],
)
async def test_unknown_device_creates_entry(
    hass: HomeAssistant,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    model: str | None,
    title: str,
) -> None:
    client_factory.prepare.append(
        lambda client: client.connect_effects.append(
            UnknownDeviceError("no profile", model=model, firmware=(9, 1))
        )
    )
    result = await _discover(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == title
    assert result["data"][CONF_ASSET_ID] == PLACEHOLDER_ASSET_ID
    assert _closed(client_factory.built[0])


async def test_unknown_error_logged_scrubbed(
    hass: HomeAssistant,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client_factory.prepare.append(
        lambda client: client.connect_effects.append(RuntimeError(f"boom {NAME_CODE}"))
    )
    caplog.set_level(logging.DEBUG)
    result = await _discover(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert "boom" in caplog.text
    assert NAME_CODE not in caplog.text
    assert PLACEHOLDER_PAIRING_CODE in caplog.text


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    return result


async def test_reauth_updates_asset_id_and_reloads(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    def prepare(client: FakeClient) -> None:
        # The populated slot hands out a fresh assetID on connect.
        client._asset_id = CHANGED_ASSET_ID

    client_factory.prepare.append(prepare)
    result = await _start_reauth(hass, config_entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_ASSET_ID] == CHANGED_ASSET_ID
    assert config_entry.data[CONF_PAIRING_CODE] == PLACEHOLDER_PAIRING_CODE
    reload.assert_called_once_with(config_entry.entry_id)
    (client,) = client_factory.built
    _, _, kwargs = client.calls[0]
    assert kwargs["asset_id"] == PLACEHOLDER_ASSET_ID
    assert kwargs["pairing_code"] == PLACEHOLDER_PAIRING_CODE
    assert _closed(client)


@pytest.mark.parametrize(("effects", "error"), ERROR_TABLE)
async def test_reauth_error_table(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    effects: list[Exception | None],
    error: str,
) -> None:
    client_factory.prepare.append(lambda client: client.connect_effects.extend(effects))
    result = await _start_reauth(hass, config_entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": error}
    assert _closed(client_factory.built[0])
    assert config_entry.data[CONF_ASSET_ID] == PLACEHOLDER_ASSET_ID


async def test_options_flow_saves_and_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry, setup_entry: AsyncMock
) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.call_count == 1

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 120}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_POLL_INTERVAL: 120}
    assert setup_entry.call_count == 2


@pytest.mark.parametrize("value", [14, 301])
async def test_options_flow_rejects_out_of_range(
    hass: HomeAssistant, config_entry: MockConfigEntry, value: int
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: value}
        )
