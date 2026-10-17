"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aosmith_ble import DeviceInfo, FeatureSet, Profile
from bleak.backends.device import BLEDevice
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble.config_flow import entry_title
from custom_components.aosmith_ble.const import (
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    DOMAIN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)

from .fakes import FakeClient, ReplayingRegister


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components in every test."""


def overlapping_clients(clients: list[FakeClient]) -> list[FakeClient]:
    """The fakes that ever saw two connect() calls at once."""
    return [client for client in clients if client.overlap_detected]


@pytest.fixture(autouse=True)
def fail_on_overlapping_connects() -> Generator[None]:
    """Fail any test in which a fake saw overlapping connects.

    The fake also raises, but the integration maps every Exception, so the
    raise alone could be swallowed.
    """
    FakeClient.instances.clear()
    yield
    overlapped = overlapping_clients(FakeClient.instances)
    FakeClient.instances.clear()
    assert not overlapped, f"overlapping connect() calls on {len(overlapped)} fake(s)"


@pytest.fixture
def mock_client() -> Generator[FakeClient]:
    """Patch AOSmithBLEClient in connection.py; every build returns this fake."""
    fake = FakeClient()
    with patch(
        "custom_components.aosmith_ble.connection.AOSmithBLEClient",
        side_effect=fake.rebind,
    ):
        yield fake


@pytest.fixture(params=[True, False], ids=["replay_disabled", "replay_forced"])
def pre_2026_10_bluetooth(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> ReplayingRegister:
    """Bluetooth as before Home Assistant 2026.10: no every-advertisement API.

    The fallback's register call replays synchronously, honouring replay
    DISABLED (replay_disabled) or ignoring it (replay_forced).
    """
    from homeassistant.components import bluetooth

    from custom_components.aosmith_ble import connection

    monkeypatch.delattr(
        bluetooth, "async_register_advertisement_callback", raising=False
    )
    fake = ReplayingRegister(honor_replay=request.param)
    monkeypatch.setattr(connection, "async_register_callback", fake)
    return fake


class ClientFactory:
    """Stands in for AOSmithBLEClient: every build is a fresh FakeClient.

    `prepare` entries are applied to the next builds in order, so a test can
    script a client before setup constructs it.
    """

    def __init__(self) -> None:
        self.built: list[FakeClient] = []
        self.prepare: list[Callable[[FakeClient], None]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> FakeClient:
        client = FakeClient(*args, **kwargs)
        client.calls.append(("construct", args, kwargs))
        if self.prepare:
            self.prepare.pop(0)(client)
        self.built.append(client)
        return client


@pytest.fixture
def client_factory() -> Generator[ClientFactory]:
    """Patch AOSmithBLEClient in connection.py; every build is a new fake."""
    factory = ClientFactory()
    with patch(
        "custom_components.aosmith_ble.connection.AOSmithBLEClient",
        side_effect=factory,
    ):
        yield factory


@pytest.fixture
def ble_device() -> BLEDevice:
    return BLEDevice(PLACEHOLDER_ADDRESS, PLACEHOLDER_NAME, {})


@pytest.fixture
def mock_ble_device(ble_device: BLEDevice) -> Generator[MagicMock]:
    """Patch async_ble_device_from_address so the heater looks present."""
    with patch(
        "custom_components.aosmith_ble.connection.async_ble_device_from_address",
        return_value=ble_device,
    ) as patched:
        yield patched


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A config entry with the data pairing stores and the title setup builds.

    Never the advertised name: issue tests check it does not leak.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        title=entry_title("HPTS-50"),
        unique_id=PLACEHOLDER_ADDRESS,
        data={
            CONF_ADDRESS: PLACEHOLDER_ADDRESS,
            CONF_PAIRING_CODE: PLACEHOLDER_PAIRING_CODE,
            CONF_ASSET_ID: PLACEHOLDER_ASSET_ID,
        },
    )


def assert_no_advertised_name(issue: ir.IssueEntry) -> None:
    """An issue never shows the advertised name (it has the serial)."""
    for value in (issue.translation_placeholders or {}).values():
        assert PLACEHOLDER_NAME not in str(value)


class Heater:
    """A loaded entry plus its fake client, for entity tests."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, client: FakeClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client

    @property
    def coordinator(self) -> Any:
        return self.entry.runtime_data

    def entity_id(self, platform: str, key: str) -> str | None:
        registry = er.async_get(self.hass)
        return registry.async_get_entity_id(
            platform, DOMAIN, f"{self.entry.data[CONF_ADDRESS]}_{key}"
        )

    def state(self, platform: str, key: str) -> State:
        entity_id = self.entity_id(platform, key)
        assert entity_id is not None, f"no {platform} entity {key}"
        state = self.hass.states.get(entity_id)
        assert state is not None
        return state

    def writes(self) -> list[tuple]:
        return [
            call
            for call in self.client.calls
            if call[0] in {"async_set_mode", "async_set_setpoint"}
        ]

    async def refresh(self, state: FeatureSet | None = None) -> None:
        if state is not None:
            self.client.state = state
        await self.coordinator.async_refresh()
        await self.hass.async_block_till_done()


@pytest.fixture
async def load_heater(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    enable_bluetooth: None,
) -> Callable[..., Any]:
    """Load the entry with a scripted client; returns a Heater."""

    async def load(
        state: FeatureSet | None = None,
        profile: Profile | None = None,
        device_info: DeviceInfo | None = None,
    ) -> Heater:
        def prepare(client: FakeClient) -> None:
            if state is not None:
                client.state = state
            if profile is not None:
                client.profile = profile
            if device_info is not None:
                client.device_info = device_info

        client_factory.prepare.append(prepare)
        if hass.config_entries.async_get_entry(config_entry.entry_id) is None:
            config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED
        return Heater(hass, config_entry, client_factory.built[-1])

    return load
