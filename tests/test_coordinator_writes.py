"""Coordinator writes, user days and write error translation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aosmith_ble import (
    AOSmithError,
    ChallengeRejectedError,
    ConfigurationIssue,
    Mode,
    NotPairedError,
    ProtocolError,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
    ValidationError,
)
from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import custom_components.aosmith_ble.coordinator as coordinator_module
from custom_components.aosmith_ble.const import (
    DOMAIN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)
from custom_components.aosmith_ble.coordinator import AOSmithCoordinator
from custom_components.aosmith_ble.scrub import Scrubber
from custom_components.aosmith_ble.store import StateStore, UserState

from .fakes import FakeClient, make_feature_set, make_profile

SECRET = "SECRETTOKEN42"
EN_JSON = (
    Path(__file__).parent.parent
    / "custom_components"
    / "aosmith_ble"
    / "translations"
    / "en.json"
)
WRITE_KEYS = {
    "setpoint_out_of_range",
    "fault_active",
    "mode_not_writable",
    "invalid_duration",
    "write_refused",
    "write_failed",
    "shutting_down",
    "reauth_required",
}


@pytest.fixture
def client() -> FakeClient:
    fake = FakeClient(asset_id=PLACEHOLDER_ASSET_ID)
    fake._connected = True
    return fake


@pytest.fixture
async def coordinator(
    hass: HomeAssistant, config_entry: MockConfigEntry, client: FakeClient
) -> Any:
    config_entry.add_to_hass(hass)
    scrubber = Scrubber.for_entry(
        PLACEHOLDER_ADDRESS, PLACEHOLDER_NAME, PLACEHOLDER_PAIRING_CODE, SECRET, None
    )
    coord = AOSmithCoordinator(
        hass,
        config_entry,
        client,
        StateStore(hass, config_entry.entry_id),
        UserState(),
        scrubber,
    )
    coord.data = make_feature_set()
    yield coord
    await coord.async_close()


@pytest.fixture
def request_refresh(coordinator: AOSmithCoordinator) -> AsyncMock:
    mock = AsyncMock()
    coordinator.async_request_refresh = mock  # type: ignore[method-assign]
    return mock


@pytest.fixture
def reauth(config_entry: MockConfigEntry) -> Generator[MagicMock]:
    with patch.object(config_entry, "async_start_reauth") as patched:
        yield patched


@pytest.fixture
def reload(hass: HomeAssistant) -> Generator[MagicMock]:
    with patch.object(hass.config_entries, "async_schedule_reload") as patched:
        yield patched


def _names(client: FakeClient) -> list[str]:
    return [call[0] for call in client.calls]


def _writes(client: FakeClient) -> list[tuple]:
    return [c for c in client.calls if c[0].startswith("async_set_")]


async def _until(predicate: Any) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def _assert_key(err: HomeAssistantError, key: str) -> None:
    assert err.translation_domain == DOMAIN
    assert err.translation_key == key


# ---------------------------------------------------------------------------
# Translations
# ---------------------------------------------------------------------------


def test_write_error_keys_exist_in_translations() -> None:
    exceptions = json.loads(EN_JSON.read_text())["exceptions"]
    assert set(exceptions) >= WRITE_KEYS
    for key in WRITE_KEYS:
        assert exceptions[key]["message"]


# ---------------------------------------------------------------------------
# Setpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "sent"), [(119.99999, 120), (130.4, 130)])
async def test_setpoint_rounds_to_whole_degree_before_range_check(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
    value: float,
    sent: int,
) -> None:
    await coordinator.async_set_setpoint(value)
    assert _writes(client) == [("async_set_setpoint", sent)]


# ---------------------------------------------------------------------------
# Serialization and connect
# ---------------------------------------------------------------------------


async def test_write_waits_for_reconnecting_poll_and_connects_never_overlap(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    client._connected = False
    client.delay = 0.02
    # The poll's connect fails, so the write has to connect too.
    client.connect_effects = [BleakError("unreachable")]
    peak = 0
    events: list[str] = []
    original = client.connect

    async def connect() -> None:
        nonlocal peak
        events.append("connect_start")
        peak = max(peak, client.active_connects + 1)
        try:
            await original()
        finally:
            events.append("connect_end")

    client.connect = connect  # type: ignore[method-assign]
    poll = asyncio.create_task(coordinator.async_refresh())
    await _until(lambda: client.active_connects == 1)
    write = asyncio.create_task(coordinator.async_set_setpoint(125))
    await asyncio.sleep(0.005)
    assert not write.done()
    await poll
    await write
    assert peak == 1
    assert events == ["connect_start", "connect_end", "connect_start", "connect_end"]
    assert _writes(client) == [("async_set_setpoint", 125)]


async def test_write_while_disconnected_connects_through_connect_and_classify(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    client._connected = False
    # One rejection recovers under the retry rule.
    client.connect_effects = [ChallengeRejectedError("once")]
    seen: list[bool] = []
    real = coordinator_module.connect_and_classify

    async def spy(*args: Any, **kwargs: Any) -> None:
        seen.append(coordinator.io_lock.locked() and coordinator.connecting)
        await real(*args, **kwargs)

    with patch.object(coordinator_module, "connect_and_classify", side_effect=spy):
        await coordinator.async_set_setpoint(125)
    assert seen == [True]
    assert _names(client) == [
        "connect",
        "disconnect",
        "connect",
        "async_set_setpoint",
    ]
    assert coordinator.connecting is False


async def test_connected_write_does_not_connect(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    seen: list[bool] = []
    original = client.async_set_setpoint

    async def write(degf: float) -> None:
        seen.append(coordinator.io_lock.locked())
        await original(degf)

    client.async_set_setpoint = write  # type: ignore[method-assign]
    await coordinator.async_set_setpoint(125)
    assert seen == [True]
    assert "connect" not in _names(client)


# ---------------------------------------------------------------------------
# Connect-class errors on a write
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "effects",
    [
        [ChallengeRejectedError("1"), ChallengeRejectedError("2")],
        [NotPairedError("not paired")],
    ],
    ids=["two_rejections", "not_paired"],
)
async def test_auth_error_on_write_connect_starts_reauth(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    reauth: MagicMock,
    request_refresh: AsyncMock,
    effects: list[Exception],
) -> None:
    client._connected = False
    client.connect_effects = effects
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_setpoint(125)
    assert not isinstance(caught.value, ServiceValidationError)
    _assert_key(caught.value, "reauth_required")
    assert coordinator.auth_failed is True
    reauth.assert_called_once_with(hass)
    assert _names(client)[-1] == "disconnect"
    assert not client.is_connected
    assert _writes(client) == []
    request_refresh.assert_not_awaited()


async def test_reauth_started_before_disconnect_on_write_auth_failure(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    reauth: MagicMock,
    request_refresh: AsyncMock,
) -> None:
    # A cancel landing in the auth-failure disconnect must not leave
    # auth_failed set with no reauth flow started.
    client._connected = False
    client.connect_effects = [NotPairedError("not paired")]
    disconnects = 0
    plain_disconnect = client.disconnect

    async def disconnect() -> None:
        nonlocal disconnects
        disconnects += 1
        if disconnects == 2:
            raise asyncio.CancelledError
        await plain_disconnect()

    client.disconnect = disconnect  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await coordinator.async_set_setpoint(125)
    assert coordinator.auth_failed is True
    reauth.assert_called_once_with(hass)


@pytest.mark.parametrize("side", ["connect", "write"])
async def test_unsupported_configuration_on_write_is_write_failed_and_schedules_reload(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    reload: MagicMock,
    request_refresh: AsyncMock,
    side: str,
) -> None:
    err = UnsupportedConfigurationError("units", reason=ConfigurationIssue.UNITS)
    if side == "connect":
        client._connected = False
        client.connect_effects = [err]
    else:
        client.write_effects = [err]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_setpoint(125)
    assert not isinstance(caught.value, ServiceValidationError)
    _assert_key(caught.value, "write_failed")
    reload.assert_called_once_with(config_entry.entry_id)
    assert coordinator.auth_failed is False
    # The scheduled reload replaces the coordinator; no refresh first.
    request_refresh.assert_not_awaited()


async def test_unknown_device_on_write_is_write_failed(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    reload: MagicMock,
    request_refresh: AsyncMock,
) -> None:
    client._connected = False
    client.connect_effects = [
        UnknownDeviceError("unknown", model="HPTS-50", firmware=(9, 9))
    ]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_mode(Mode.HYBRID)
    assert not isinstance(caught.value, ServiceValidationError)
    _assert_key(caught.value, "write_failed")
    reload.assert_called_once_with(config_entry.entry_id)
    assert _writes(client) == []


# ---------------------------------------------------------------------------
# ValidationError translation
# ---------------------------------------------------------------------------


async def _setpoint_out_of_range(coord: AOSmithCoordinator, client: FakeClient):
    client.write_effects = [ValidationError("outside range")]
    await coord.async_set_setpoint(135)


async def _fault_in_last_data(coord: AOSmithCoordinator, client: FakeClient):
    coord.data = make_feature_set(fault=31)
    client.write_effects = [ValidationError("fault active")]
    await coord.async_set_setpoint(125)


async def _fault_on_mode_write(coord: AOSmithCoordinator, client: FakeClient):
    coord.data = make_feature_set(fault=31)
    client.write_effects = [ValidationError("fault active")]
    await coord.async_set_mode(Mode.HYBRID)


async def _mode_not_in_profile(coord: AOSmithCoordinator, client: FakeClient):
    client.profile = make_profile(writable_modes=(Mode.HYBRID, Mode.HEAT_PUMP))
    client.write_effects = [ValidationError("mode not writable")]
    await coord.async_set_mode(Mode.ELECTRIC)


async def _guest(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_mode(Mode.GUEST)


async def _guest_timed(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_timed_mode(Mode.GUEST, 1)


async def _days_too_many(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_timed_mode(Mode.VACATION, 100)


async def _days_zero(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_timed_mode(Mode.ELECTRIC, 0)


async def _electric_days_over_range(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_timed_mode(Mode.ELECTRIC, 8)


async def _timed_permanent(coord: AOSmithCoordinator, client: FakeClient):
    await coord.async_set_timed_mode(Mode.HYBRID, 2)


@pytest.mark.parametrize(
    ("action", "key", "written"),
    [
        (_setpoint_out_of_range, "setpoint_out_of_range", True),
        (_fault_in_last_data, "fault_active", True),
        (_fault_on_mode_write, "fault_active", True),
        (_mode_not_in_profile, "mode_not_writable", True),
        (_guest, "mode_not_writable", False),
        (_guest_timed, "mode_not_writable", False),
        (_days_too_many, "invalid_duration", False),
        (_days_zero, "invalid_duration", False),
        (_electric_days_over_range, "invalid_duration", False),
        (_timed_permanent, "mode_not_writable", False),
    ],
    ids=lambda v: getattr(v, "__name__", None),
)
async def test_validation_error_translated_by_known_cause(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
    action: Any,
    key: str,
    written: bool,
) -> None:
    with pytest.raises(ServiceValidationError) as caught:
        await action(coordinator, client)
    _assert_key(caught.value, key)
    assert bool(_writes(client)) is written
    request_refresh.assert_not_awaited()


async def test_validation_error_with_unknown_cause_is_write_refused_scrubbed(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    client.write_effects = [ValidationError(f"read-back mismatch {SECRET}")]
    with pytest.raises(ServiceValidationError) as caught:
        await coordinator.async_set_setpoint(125)
    _assert_key(caught.value, "write_refused")
    placeholders = caught.value.translation_placeholders or {}
    assert SECRET not in placeholders["error"]
    assert PLACEHOLDER_ASSET_ID in placeholders["error"]
    assert caught.value.__cause__ is None
    # A refused write may have landed (a read-back mismatch): show real state.
    request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Other errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "err"),
    [
        ("write", AOSmithError(f"library {SECRET}")),
        ("write", ProtocolError(f"bad frame {SECRET}")),
        ("write", BleakError(f"bleak {SECRET}")),
        ("write", TimeoutError(f"timeout {SECRET}")),
        ("write", KeyError(f"key {SECRET}")),
        ("connect", BleakError(f"bleak {SECRET}")),
        ("connect", TimeoutError(f"timeout {SECRET}")),
        ("connect", ChallengeRejectedError(f"first rejection {SECRET}")),
    ],
    ids=lambda v: v if isinstance(v, str) else type(v).__name__,
)
async def test_other_errors_become_write_failed(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    reauth: MagicMock,
    reload: MagicMock,
    request_refresh: AsyncMock,
    side: str,
    err: Exception,
) -> None:
    if side == "connect":
        client._connected = False
        if isinstance(err, ChallengeRejectedError):
            # A raw rejection escapes connect_and_classify after a plain SessionError.
            client.connect_effects = [SessionError("lost"), err]
        else:
            client.connect_effects = [err]
    else:
        client.write_effects = [err]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_setpoint(125)
    assert not isinstance(caught.value, ServiceValidationError)
    _assert_key(caught.value, "write_failed")
    placeholders = caught.value.translation_placeholders or {}
    assert SECRET not in placeholders["error"]
    assert caught.value.__cause__ is None
    assert coordinator.auth_failed is False
    reauth.assert_not_called()
    reload.assert_not_called()
    if side == "write":
        # The write may have landed: refresh so the UI is not left stale.
        request_refresh.assert_awaited_once()
    else:
        # Nothing was sent; the failed connect needs no immediate retry.
        request_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "err",
    [BleakError("bleak"), ValidationError("mismatch"), TimeoutError()],
    ids=lambda e: type(e).__name__,
)
async def test_write_not_retried(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
    err: Exception,
) -> None:
    client.write_effects = [err]
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_mode(Mode.HYBRID)
    assert _writes(client) == [("async_set_mode", Mode.HYBRID, 0)]
    assert "connect" not in _names(client)


async def test_no_timeout_around_write(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    client.delay = 0.05
    with (
        patch.object(coordinator_module, "POLL_TIMEOUT", 0.001),
        patch("custom_components.aosmith_ble.connection.CONNECT_TIMEOUT", 0.001),
    ):
        await coordinator.async_set_setpoint(125)
    assert _writes(client) == [("async_set_setpoint", 125)]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def _setpoint(coord: AOSmithCoordinator) -> None:
    await coord.async_set_setpoint(125)


async def _mode(coord: AOSmithCoordinator) -> None:
    await coord.async_set_mode(Mode.HYBRID)


async def _timed(coord: AOSmithCoordinator) -> None:
    await coord.async_set_timed_mode(Mode.ELECTRIC, 2)


@pytest.mark.parametrize("write", [_setpoint, _mode, _timed], ids=lambda f: f.__name__)
@pytest.mark.parametrize(
    ("flag", "key"),
    [("closing", "shutting_down"), ("auth_failed", "reauth_required")],
)
async def test_writes_refused_while_closing_and_while_auth_failed(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
    write: Any,
    flag: str,
    key: str,
) -> None:
    setattr(coordinator, flag, True)
    with pytest.raises(HomeAssistantError) as caught:
        await write(coordinator)
    assert not isinstance(caught.value, ServiceValidationError)
    _assert_key(caught.value, key)
    assert client.calls == []
    assert not coordinator.io_lock.locked()
    request_refresh.assert_not_awaited()


async def test_write_queued_behind_poll_refused_when_close_starts(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    client.delay = 0.05
    poll = asyncio.create_task(coordinator._async_update_data())
    await _until(client._lock.locked)
    write = asyncio.create_task(coordinator.async_set_setpoint(125))
    await asyncio.sleep(0)
    await coordinator.async_close()
    await poll
    with pytest.raises(HomeAssistantError) as caught:
        await write
    _assert_key(caught.value, "shutting_down")
    assert _writes(client) == []


async def test_close_waits_for_in_flight_write_then_no_connect(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    client.delay = 0.05
    write = asyncio.create_task(coordinator.async_set_setpoint(125))
    await _until(client._lock.locked)

    await coordinator.async_close()
    # The library close waited for the client lock, so the write had finished.
    assert write.done()
    assert not client._lock.locked()
    await write
    assert not client.is_connected
    await asyncio.sleep(0.1)
    close_at = _names(client).index("async_close")
    assert "connect" not in _names(client)[close_at:]


# ---------------------------------------------------------------------------
# Modes and stored days
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [Mode.HYBRID, Mode.HEAT_PUMP])
async def test_permanent_mode_sends_zero_days(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
    mode: Mode,
) -> None:
    await coordinator.async_set_mode(mode)
    assert _writes(client) == [("async_set_mode", mode, 0)]


async def test_electric_and_vacation_use_stored_days(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    coordinator.user_state.electric_days = 5
    coordinator.user_state.vacation_days = 12
    await coordinator.async_set_mode(Mode.ELECTRIC)
    await coordinator.async_set_mode(Mode.VACATION)
    assert _writes(client) == [
        ("async_set_mode", Mode.ELECTRIC, 5),
        ("async_set_mode", Mode.VACATION, 12),
    ]


async def test_timed_mode_does_not_change_stored_counts(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    request_refresh: AsyncMock,
) -> None:
    await coordinator.async_set_timed_mode(Mode.ELECTRIC, 2)
    await coordinator.async_set_timed_mode(Mode.VACATION, 30)
    assert _writes(client) == [
        ("async_set_mode", Mode.ELECTRIC, 2),
        ("async_set_mode", Mode.VACATION, 30),
    ]
    assert coordinator.electric_days == 3
    assert coordinator.vacation_days == 7
    stored = await StateStore(hass, config_entry.entry_id).async_load()
    assert (stored.electric_days, stored.vacation_days) == (3, 7)


async def _all_writes(coord: AOSmithCoordinator) -> None:
    await coord.async_set_setpoint(125)
    await coord.async_set_mode(Mode.HYBRID)
    await coord.async_set_timed_mode(Mode.VACATION, 3)


async def test_write_requests_refresh(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    seen: list[bool] = []

    async def record() -> None:
        seen.append(coordinator.io_lock.locked())

    request_refresh.side_effect = record
    await _all_writes(coordinator)
    # Requested after each write, outside the lock.
    assert seen == [False, False, False]


async def test_failed_write_refresh_runs_outside_the_lock(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    request_refresh: AsyncMock,
) -> None:
    seen: list[bool] = []

    async def record() -> None:
        seen.append(coordinator.io_lock.locked())

    request_refresh.side_effect = record
    client.write_effects = [ProtocolError("lost after send")]
    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_mode(Mode.HYBRID)
    assert seen == [False]


async def test_failed_write_polls_the_heater_again(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    # Unpatched: the real debounced refresh reads the heater's actual state.
    client.write_effects = [ProtocolError("lost after send")]
    with pytest.raises(HomeAssistantError) as caught:
        await coordinator.async_set_setpoint(125)
    _assert_key(caught.value, "write_failed")
    await hass.async_block_till_done()
    names = _names(client)
    assert names.index("async_set_setpoint") < names.index("async_get_state")


async def test_back_to_back_writes_refresh_within_seconds(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Unpatched debouncer: a second write inside the cooldown waits for its end,
    # so the cooldown bounds how long an entity shows the pre-write state.
    await coordinator.async_set_setpoint(125)
    await hass.async_block_till_done()
    await coordinator.async_set_setpoint(126)
    await hass.async_block_till_done()
    assert _names(client).count("async_get_state") == 1
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _names(client) == [
        "async_set_setpoint",
        "async_get_state",
        "async_set_setpoint",
        "async_get_state",
    ]


async def test_user_days_persist_through_store(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
) -> None:
    listener = MagicMock()
    unsub = coordinator.async_add_listener(listener)
    await coordinator.async_set_user_days(Mode.ELECTRIC, 6)
    await coordinator.async_set_user_days(Mode.VACATION, 30)
    unsub()
    assert coordinator.electric_days == 6
    assert coordinator.vacation_days == 30
    assert listener.call_count == 2
    stored = await StateStore(hass, config_entry.entry_id).async_load()
    assert (stored.electric_days, stored.vacation_days) == (6, 30)
    # Persisting a count never touches the heater.
    assert client.calls == []

    for mode, days in [(Mode.ELECTRIC, 8), (Mode.VACATION, 0)]:
        with pytest.raises(ServiceValidationError) as caught:
            await coordinator.async_set_user_days(mode, days)
        _assert_key(caught.value, "invalid_duration")
    with pytest.raises(ServiceValidationError) as caught:
        await coordinator.async_set_user_days(Mode.HYBRID, 2)
    _assert_key(caught.value, "mode_not_writable")
    assert (coordinator.electric_days, coordinator.vacation_days) == (6, 30)


async def test_last_permanent_mode_property(
    coordinator: AOSmithCoordinator,
) -> None:
    assert coordinator.last_permanent_mode is None
    coordinator.user_state.last_permanent_mode = int(Mode.HEAT_PUMP)
    assert coordinator.last_permanent_mode is Mode.HEAT_PUMP
