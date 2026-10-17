"""Setup, unload, removal and diagnostics-only mode."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Generator
from datetime import timedelta
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
from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import (
    BluetoothCallbackReplay,
    BluetoothScanningMode,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.aosmith_ble as integration
from custom_components.aosmith_ble import remove_stale_entities
from custom_components.aosmith_ble.const import (
    BACKOFF_MAX,
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    DOMAIN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
    PLATFORMS,
    RECONNECT_DELAY,
    SETUP_ADVERT_WAIT,
)
from custom_components.aosmith_ble.coordinator import AOSmithCoordinator
from custom_components.aosmith_ble.issues import (
    ISSUE_CONNECTIVITY,
    ISSUE_UNSUPPORTED_CONFIGURATION,
    ISSUE_UNSUPPORTED_DEVICE,
    raise_issue,
)
from custom_components.aosmith_ble.store import StateStore, UserState

from .conftest import ClientFactory
from .fakes import FakeClient, ReplayingRegister
from .test_repairs import assert_issue_translated

# A non-placeholder token used as the entry's asset ID so scrubbing is observable.
SECRET = "SECRETTOKEN42"
CHANGED_ASSET_ID = "02iQk000000CHANGED"


@pytest.fixture(autouse=True)
def _bluetooth(enable_bluetooth: None) -> None:
    """A real Bluetooth manager, so advertisement callbacks register."""


@pytest.fixture
def advert_wait() -> Generator[AsyncMock]:
    with patch(
        "custom_components.aosmith_ble.async_process_advertisements",
        new_callable=AsyncMock,
        side_effect=TimeoutError,
    ) as patched:
        yield patched


class _Adverts:
    """Captures the one-shot reload listener setup registers."""

    def __init__(self) -> None:
        self.callbacks: list[Callable[[Any], None]] = []
        self.addresses: list[str] = []
        self.unsubs: list[MagicMock] = []

    def register(
        self, _hass: HomeAssistant, cb: Callable[[Any], None], address: str
    ) -> MagicMock:
        self.callbacks.append(cb)
        self.addresses.append(address)
        unsub = MagicMock(name="unsub")
        self.unsubs.append(unsub)
        return unsub


@pytest.fixture
def retry_adverts() -> Generator[_Adverts]:
    adverts = _Adverts()
    with patch(
        "custom_components.aosmith_ble.async_register_advertisement_callback",
        side_effect=adverts.register,
    ):
        yield adverts


@pytest.fixture
def secret_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=PLACEHOLDER_NAME,
        unique_id=PLACEHOLDER_ADDRESS,
        data={
            CONF_ADDRESS: PLACEHOLDER_ADDRESS,
            CONF_PAIRING_CODE: PLACEHOLDER_PAIRING_CODE,
            CONF_ASSET_ID: SECRET,
        },
    )


@pytest.fixture
async def loaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> AsyncGenerator[FakeClient]:
    """A normally loaded entry; yields its client."""
    assert await _setup(hass, config_entry) is ConfigEntryState.LOADED
    yield client_factory.built[-1]


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> ConfigEntryState:
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries()}:
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.state


async def _reload(hass: HomeAssistant, entry: MockConfigEntry) -> ConfigEntryState:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    return entry.state


def _issue(hass: HomeAssistant, key: str, entry: MockConfigEntry) -> ir.IssueEntry:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


def _names(client: FakeClient) -> list[str]:
    return [call[0] for call in client.calls]


def _closed(client: FakeClient) -> bool:
    return client._closed and not client.is_connected


def _fail_connect(*effects: Exception) -> Callable[[FakeClient], None]:
    def prepare(client: FakeClient) -> None:
        client.connect_effects = list(effects)

    return prepare


# ---------------------------------------------------------------------------
# No BLEDevice
# ---------------------------------------------------------------------------


async def test_no_ble_device_waits_then_not_ready(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    advert_wait: AsyncMock,
) -> None:
    mock_ble_device.return_value = None
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    advert_wait.assert_awaited_once()
    args = advert_wait.await_args.args
    assert args[2] == {"address": PLACEHOLDER_ADDRESS, "connectable": True}
    assert args[3] is BluetoothScanningMode.ACTIVE
    assert args[4] == SETUP_ADVERT_WAIT
    # No client was built, so no string address ever reached the library.
    assert client_factory.built == []
    assert config_entry.error_reason_translation_key == "not_advertising"
    assert _issue(hass, ISSUE_CONNECTIVITY, config_entry) is None


async def test_not_advertising_issue_only_on_second_consecutive_failed_setup(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    ble_device: BLEDevice,
    advert_wait: AsyncMock,
) -> None:
    mock_ble_device.return_value = None
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, ISSUE_CONNECTIVITY, config_entry) is None

    assert await _reload(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, ISSUE_CONNECTIVITY, config_entry)
    assert issue is not None
    assert issue.translation_key == "connectivity_not_advertising"
    assert_issue_translated(issue)

    # Success clears the issue and resets the counter.
    mock_ble_device.return_value = ble_device
    assert await _reload(hass, config_entry) is ConfigEntryState.LOADED
    assert _issue(hass, ISSUE_CONNECTIVITY, config_entry) is None

    mock_ble_device.return_value = None
    assert await _reload(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, ISSUE_CONNECTIVITY, config_entry) is None


async def test_advertisement_during_wait_lets_setup_proceed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    ble_device: BLEDevice,
    advert_wait: AsyncMock,
) -> None:
    seen: list[bool] = []

    def lookup(*_args: Any, **_kwargs: Any) -> BLEDevice | None:
        # Absent until the wait has seen an advertisement.
        return ble_device if seen else None

    async def wait(_hass: HomeAssistant, cb: Callable[[Any], bool], *_: Any) -> Any:
        info = MagicMock(name="service_info")
        seen.append(cb(info))
        return info

    mock_ble_device.side_effect = lookup
    advert_wait.side_effect = wait
    assert await _setup(hass, config_entry) is ConfigEntryState.LOADED
    assert seen == [True]
    (client,) = client_factory.built
    assert client.device is ble_device
    assert client.is_connected


# ---------------------------------------------------------------------------
# Connect outcomes
# ---------------------------------------------------------------------------


async def test_connected_clears_issues_writes_back_asset_id_and_forwards_platforms(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    ble_device: BLEDevice,
) -> None:
    config_entry.add_to_hass(hass)
    raise_issue(hass, config_entry, ISSUE_CONNECTIVITY, "unreachable")
    raise_issue(hass, config_entry, ISSUE_UNSUPPORTED_CONFIGURATION, "units")
    raise_issue(
        hass, config_entry, ISSUE_UNSUPPORTED_DEVICE, None, model="m", firmware="1.0"
    )

    def prepare(client: FakeClient) -> None:
        plain = client.connect

        async def connect() -> None:
            await plain()
            client._asset_id = CHANGED_ASSET_ID

        client.connect = connect  # type: ignore[method-assign]

    client_factory.prepare.append(prepare)
    forward = AsyncMock()
    with patch.object(hass.config_entries, "async_forward_entry_setups", forward):
        assert await _setup(hass, config_entry) is ConfigEntryState.LOADED

    for key in (
        ISSUE_CONNECTIVITY,
        ISSUE_UNSUPPORTED_CONFIGURATION,
        ISSUE_UNSUPPORTED_DEVICE,
    ):
        assert _issue(hass, key, config_entry) is None
    assert config_entry.data[CONF_ASSET_ID] == CHANGED_ASSET_ID
    forward.assert_awaited_once_with(config_entry, PLATFORMS)
    (client,) = client_factory.built
    assert client.device is ble_device
    assert client.is_connected
    assert isinstance(config_entry.runtime_data, AOSmithCoordinator)
    assert config_entry.runtime_data.client is client


@pytest.mark.parametrize(
    "effects",
    [
        [BleakError(f"bleak {SECRET}")],
        [TimeoutError(f"timeout {SECRET}")],
        [NotConnectedError(f"gone {SECRET}")],
        [ProtocolError(f"garbled {SECRET}")],
        # A raw rejection after a lost session is an ordinary connect failure.
        [SessionError(f"lost {SECRET}"), ChallengeRejectedError(f"rejected {SECRET}")],
    ],
    ids=["bleak", "timeout", "not_connected", "protocol", "raw_challenge_rejected"],
)
async def test_transient_errors_not_ready_scrubbed(
    hass: HomeAssistant,
    secret_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    effects: list[Exception],
) -> None:
    client_factory.prepare.append(_fail_connect(*effects))
    assert await _setup(hass, secret_entry) is ConfigEntryState.SETUP_RETRY
    assert SECRET not in str(secret_entry.reason)
    assert SECRET not in str(secret_entry.error_reason_translation_placeholders)
    assert _closed(client_factory.built[0])


async def test_slots_exhausted_raises_issue_and_not_ready(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    client_factory.prepare.append(_fail_connect(ConnectionSlotsExhaustedError("full")))
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, ISSUE_CONNECTIVITY, config_entry)
    assert issue is not None
    assert issue.translation_key == "connectivity_no_connection_slots"
    assert_issue_translated(issue)
    assert _closed(client_factory.built[0])


@pytest.mark.parametrize(
    "effects",
    [
        [ChallengeRejectedError("1"), ChallengeRejectedError("2")],
        [NotPairedError("not paired")],
    ],
    ids=["auth_failed", "not_paired"],
)
async def test_auth_errors_raise_config_entry_auth_failed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    retry_adverts: _Adverts,
    effects: list[Exception],
) -> None:
    client_factory.prepare.append(_fail_connect(*effects))
    with patch.object(config_entry, "async_start_reauth_if_available") as reauth:
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_ERROR
    # Home Assistant starts reauth only for ConfigEntryAuthFailed.
    reauth.assert_called_once()
    assert _closed(client_factory.built[0])
    assert retry_adverts.callbacks == []
    assert _issue(hass, ISSUE_CONNECTIVITY, config_entry) is None


@pytest.mark.parametrize("reason", list(ConfigurationIssue))
async def test_unsupported_configuration_issue_by_reason_and_config_entry_error(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    retry_adverts: _Adverts,
    reason: ConfigurationIssue,
) -> None:
    client_factory.prepare.append(
        _fail_connect(UnsupportedConfigurationError("bad", reason=reason))
    )
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_ERROR
    issue = _issue(hass, ISSUE_UNSUPPORTED_CONFIGURATION, config_entry)
    assert issue is not None
    assert issue.translation_key == f"unsupported_configuration_{reason.value}"
    assert_issue_translated(issue)
    assert _closed(client_factory.built[0])
    # ConfigEntryError: no automatic retry, no advertisement reload.
    assert retry_adverts.callbacks == []


async def test_unknown_configuration_reason_gets_the_generic_variant(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    retry_adverts: _Adverts,
) -> None:
    # A reason a future library release might add.
    client_factory.prepare.append(
        _fail_connect(UnsupportedConfigurationError("bad", reason="future"))  # type: ignore[arg-type]
    )
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_ERROR
    issue = _issue(hass, ISSUE_UNSUPPORTED_CONFIGURATION, config_entry)
    assert issue is not None
    assert issue.translation_key == "unsupported_configuration_other"
    assert issue.is_fixable is True
    assert_issue_translated(issue)
    assert config_entry.error_reason_translation_key == (
        "unsupported_configuration_other"
    )


async def test_unknown_device_loads_diagnostics_only(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    client_factory.prepare.append(
        _fail_connect(
            UnknownDeviceError("no profile", model="HPTS-99", firmware=(9, 4))
        )
    )
    forward = AsyncMock()
    with patch.object(hass.config_entries, "async_forward_entry_setups", forward):
        assert await _setup(hass, config_entry) is ConfigEntryState.LOADED
    assert config_entry.runtime_data is None
    forward.assert_not_awaited()
    (client,) = client_factory.built
    assert "async_close" in _names(client)
    assert _closed(client)
    issue = _issue(hass, ISSUE_UNSUPPORTED_DEVICE, config_entry)
    assert issue is not None
    assert_issue_translated(issue)
    assert issue.translation_placeholders == {
        "title": config_entry.title,
        "model": "HPTS-99",
        "firmware": "9.4",
    }
    # Unload of a diagnostics-only entry works and keeps the issue.
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert _issue(hass, ISSUE_UNSUPPORTED_DEVICE, config_entry) is not None


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (
            UnknownDeviceError("no profile", model="HPTS-99", firmware=(9, 4)),
            ISSUE_UNSUPPORTED_DEVICE,
        ),
        (
            UnsupportedConfigurationError("celsius", reason=ConfigurationIssue.UNITS),
            ISSUE_UNSUPPORTED_CONFIGURATION,
        ),
    ],
    ids=["unknown_device", "unsupported_configuration"],
)
async def test_unsupported_entry_becomes_normal_on_reload_and_issue_clears(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    error: Exception,
    key: str,
) -> None:
    client_factory.prepare.append(_fail_connect(error))
    await _setup(hass, config_entry)
    assert _issue(hass, key, config_entry) is not None

    assert await _reload(hass, config_entry) is ConfigEntryState.LOADED
    assert _issue(hass, key, config_entry) is None
    assert isinstance(config_entry.runtime_data, AOSmithCoordinator)
    assert config_entry.runtime_data.client is client_factory.built[-1]


async def test_connect_bounded_by_connect_timeout(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    def prepare(client: FakeClient) -> None:
        client.delay = 30

    client_factory.prepare.append(prepare)
    with patch("custom_components.aosmith_ble.connection.CONNECT_TIMEOUT", 0.05):
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    assert _closed(client_factory.built[0])


# ---------------------------------------------------------------------------
# Failures after a successful connect
# ---------------------------------------------------------------------------


async def test_first_refresh_failure_closes_client(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    def prepare(client: FakeClient) -> None:
        client.read_effects = [BleakError("read failed")]

    client_factory.prepare.append(prepare)
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    (client,) = client_factory.built
    assert "connect" in _names(client)
    assert "async_close" in _names(client)
    assert _closed(client)


async def test_platform_forward_failure_closes_client(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    forward = AsyncMock(side_effect=RuntimeError("platform broke"))
    with patch.object(hass.config_entries, "async_forward_entry_setups", forward):
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_ERROR
    (client,) = client_factory.built
    assert "connect" in _names(client)
    assert _closed(client)
    # runtime_data must not point at the closed coordinator.
    assert getattr(config_entry, "runtime_data", None) is None


async def test_setup_with_reauth_flow_open_does_not_connect(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    flows = [
        {
            "flow_id": "reauth",
            "handler": DOMAIN,
            "context": {"source": SOURCE_REAUTH, "entry_id": config_entry.entry_id},
        }
    ]
    with patch.object(
        hass.config_entries.flow, "async_progress_by_handler", return_value=flows
    ):
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_ERROR
    assert client_factory.built == []
    mock_ble_device.assert_not_called()


# ---------------------------------------------------------------------------
# One-shot advertisement reload after a not-ready setup
# ---------------------------------------------------------------------------


async def test_home_assistant_runs_on_unload_callbacks_for_not_ready(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    # Pins the Home Assistant behavior setup depends on: callbacks given
    # to entry.async_on_unload run as soon as a setup raises
    # ConfigEntryNotReady. The client close relies on it; the one-shot reload
    # listener must therefore NOT be registered that way, or it would be gone
    # before the first advertisement.
    ran: list[ConfigEntryState] = []

    async def setup_entry(_hass: HomeAssistant, entry: Any) -> bool:
        entry.async_on_unload(lambda: ran.append(entry.state))
        raise ConfigEntryNotReady("not yet")

    with patch.object(integration, "async_setup_entry", setup_entry):
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    assert ran == [ConfigEntryState.SETUP_RETRY]


async def test_not_ready_setup_registers_one_shot_advertisement_reload(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    advert_wait: AsyncMock,
    retry_adverts: _Adverts,
    freezer: FrozenDateTimeFactory,
) -> None:
    mock_ble_device.return_value = None
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
        assert retry_adverts.addresses == [PLACEHOLDER_ADDRESS]
        (cb,) = retry_adverts.callbacks
        freezer.tick(timedelta(seconds=RECONNECT_DELAY + 1))
        cb(MagicMock(name="service_info"))
        cb(MagicMock(name="service_info"))
    reload.assert_called_once_with(config_entry.entry_id)
    retry_adverts.unsubs[0].assert_called()


async def test_retry_reload_ignores_advertisements_right_after_failure(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    retry_adverts: _Adverts,
    freezer: FrozenDateTimeFactory,
) -> None:
    # A heater that advertises while connects fail (slots exhausted) must not
    # turn every advertisement into a reload.
    client_factory.prepare.append(_fail_connect(ConnectionSlotsExhaustedError("full")))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
        (cb,) = retry_adverts.callbacks
        cb(MagicMock(name="service_info"))
        reload.assert_not_called()
        freezer.tick(timedelta(seconds=RECONNECT_DELAY + 1))
        cb(MagicMock(name="service_info"))
    reload.assert_called_once_with(config_entry.entry_id)


async def test_retry_reload_holdoff_scales_with_consecutive_failures(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    ble_device: BLEDevice,
    advert_wait: AsyncMock,
    retry_adverts: _Adverts,
    freezer: FrozenDateTimeFactory,
) -> None:
    async def fail_and_check(expected: float) -> None:
        state = (
            await _setup(hass, config_entry)
            if config_entry.state is ConfigEntryState.NOT_LOADED
            else await _reload(hass, config_entry)
        )
        assert state is ConfigEntryState.SETUP_RETRY
        cb = retry_adverts.callbacks[-1]
        with patch.object(hass.config_entries, "async_schedule_reload") as reload:
            freezer.tick(timedelta(seconds=expected - 1))
            cb(MagicMock(name="service_info"))
            reload.assert_not_called()
            freezer.tick(timedelta(seconds=2))
            cb(MagicMock(name="service_info"))
            reload.assert_called_once_with(config_entry.entry_id)

    config_entry.add_to_hass(hass)
    mock_ble_device.return_value = None
    for failures in range(1, 9):
        # 10, 20, 40, ... capped at BACKOFF_MAX.
        expected = min(RECONNECT_DELAY * 2 ** (failures - 1), BACKOFF_MAX)
        await fail_and_check(expected)
    assert expected == BACKOFF_MAX

    # A successful setup resets the holdoff.
    mock_ble_device.return_value = ble_device
    assert await _reload(hass, config_entry) is ConfigEntryState.LOADED
    mock_ble_device.return_value = None
    await fail_and_check(RECONNECT_DELAY)


async def test_retry_reload_listener_removed_when_entry_leaves_retry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    advert_wait: AsyncMock,
    retry_adverts: _Adverts,
) -> None:
    # Unloading a SETUP_RETRY entry skips async_unload_entry, so the listener
    # must remove itself on the state change rather than leak.
    mock_ble_device.return_value = None
    assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    retry_adverts.unsubs[0].assert_called()


# ---------------------------------------------------------------------------
# Unload, stop, stale entities, removal, reload
# ---------------------------------------------------------------------------


async def test_unload_closes_client_before_returning(
    hass: HomeAssistant, config_entry: MockConfigEntry, loaded: FakeClient
) -> None:
    assert loaded.is_connected
    assert await integration.async_unload_entry(hass, config_entry)
    assert "async_close" in _names(loaded)
    assert _closed(loaded)


async def test_stop_event_closes_client(
    hass: HomeAssistant, config_entry: MockConfigEntry, loaded: FakeClient
) -> None:
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert _closed(loaded)


async def test_stale_entities_removed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor", DOMAIN, f"{PLACEHOLDER_ADDRESS}_gone", config_entry=config_entry
    )
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, f"{PLACEHOLDER_ADDRESS}_fault", config_entry=config_entry
    )
    with patch.object(
        integration,
        "created_unique_ids",
        return_value={f"{PLACEHOLDER_ADDRESS}_fault"},
    ):
        assert await _setup(hass, config_entry) is ConfigEntryState.LOADED
    assert registry.async_get(stale.entity_id) is None
    assert registry.async_get(kept.entity_id) is not None

    dropped = registry.async_get_or_create(
        "sensor", DOMAIN, f"{PLACEHOLDER_ADDRESS}_old", config_entry=config_entry
    )
    remove_stale_entities(hass, config_entry, {f"{PLACEHOLDER_ADDRESS}_fault"})
    assert registry.async_get(kept.entity_id) is not None
    assert registry.async_get(dropped.entity_id) is None


async def test_setup_keeps_entities_it_creates(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
) -> None:
    # Stale entity removal runs on every setup; a created unique ID is kept.
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    existing = registry.async_get_or_create(
        "sensor", DOMAIN, f"{PLACEHOLDER_ADDRESS}_fault", config_entry=config_entry
    )
    assert await _setup(hass, config_entry) is ConfigEntryState.LOADED
    assert registry.async_get(existing.entity_id) is not None


async def test_remove_deletes_store_and_issues(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    loaded: FakeClient,
    hass_storage: dict[str, Any],
) -> None:
    key = f"{DOMAIN}.{config_entry.entry_id}"
    await StateStore(hass, config_entry.entry_id).async_save(UserState(electric_days=5))
    assert key in hass_storage
    raise_issue(hass, config_entry, ISSUE_CONNECTIVITY, "unreachable")
    raise_issue(hass, config_entry, ISSUE_UNSUPPORTED_CONFIGURATION, "mtu")

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    for key_ in (
        ISSUE_CONNECTIVITY,
        ISSUE_UNSUPPORTED_CONFIGURATION,
        ISSUE_UNSUPPORTED_DEVICE,
    ):
        assert _issue(hass, key_, config_entry) is None
    assert _closed(loaded)


async def test_reload_builds_new_client_while_old_stays_closed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    loaded: FakeClient,
) -> None:
    assert await _reload(hass, config_entry) is ConfigEntryState.LOADED
    old, new = client_factory.built
    assert old is loaded
    assert _closed(old)
    assert _names(old).count("connect") == 1
    assert new.is_connected
    assert config_entry.runtime_data.client is new


async def test_reauth_end_to_end_reloads_to_loaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    load_heater: Callable[..., Any],
) -> None:
    # Unpatched: real async_update_reload_and_abort, real setup on the reload.
    heater = await load_heater()
    client = heater.client
    await client.disconnect()
    client.connect_effects.extend(
        [ChallengeRejectedError("first"), ChallengeRejectedError("second")]
    )
    await heater.coordinator.async_refresh()
    await hass.async_block_till_done()
    flows = list(config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert len(flows) == 1
    built_before = len(client_factory.built)

    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()

    # One client for the reauth pairing, one for the reloaded setup.
    assert len(client_factory.built) == built_before + 2
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is not None
    assert config_entry.runtime_data.auth_failed is False
    assert not list(config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    assert _closed(client)


async def test_retry_listener_ignores_a_synchronous_replay(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    advert_wait: AsyncMock,
    pre_2026_10_bluetooth: ReplayingRegister,
) -> None:
    # Before 2026.10 the fallback's register call may replay the last
    # advertisement before it returns; the retry listener must not crash or
    # reload on it.
    mock_ble_device.return_value = None
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        assert await _setup(hass, config_entry) is ConfigEntryState.SETUP_RETRY
        await hass.async_block_till_done()
    ((args, kwargs),) = pre_2026_10_bluetooth.calls
    assert args[2] == {"address": PLACEHOLDER_ADDRESS, "connectable": True}
    assert kwargs["replay"] is BluetoothCallbackReplay.DISABLED
    reload.assert_not_called()
