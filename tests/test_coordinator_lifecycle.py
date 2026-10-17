"""Coordinator drops, advertisements and close."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aosmith_ble import NotConnectedError
from bleak.exc import BleakError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.bluetooth import (
    BluetoothCallbackReplay,
    BluetoothChange,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.aosmith_ble.const import (
    ADVERT_DEBOUNCE_COOLDOWN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
    RECONNECT_DELAY,
)
from custom_components.aosmith_ble.coordinator import (
    AOSmithCoordinator,
    DropHandlerHolder,
)
from custom_components.aosmith_ble.scrub import Scrubber
from custom_components.aosmith_ble.store import StateStore, UserState

from .fakes import FakeClient, ReplayingRegister, make_feature_set

REGISTER = (
    "custom_components.aosmith_ble.coordinator.async_register_advertisement_callback"
)


@pytest.fixture
def holder() -> DropHandlerHolder:
    return DropHandlerHolder()


@pytest.fixture
def client(holder: DropHandlerHolder) -> FakeClient:
    return FakeClient(asset_id=PLACEHOLDER_ASSET_ID, on_disconnect=holder)


@pytest.fixture
def unregister() -> MagicMock:
    return MagicMock(name="unregister")


@pytest.fixture
def register(unregister: MagicMock) -> Generator[MagicMock]:
    with patch(REGISTER, return_value=unregister) as patched:
        yield patched


@pytest.fixture
async def coordinator(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client: FakeClient,
    holder: DropHandlerHolder,
) -> AsyncGenerator[AOSmithCoordinator]:
    config_entry.add_to_hass(hass)
    scrubber = Scrubber.for_entry(
        PLACEHOLDER_ADDRESS, PLACEHOLDER_NAME, PLACEHOLDER_PAIRING_CODE, None, None
    )
    coord = AOSmithCoordinator(
        hass,
        config_entry,
        client,
        StateStore(hass, config_entry.entry_id),
        UserState(),
        scrubber,
    )
    holder.bind(coord.handle_drop)
    yield coord
    await coord.async_close()


@pytest.fixture
def refresh(coordinator: AOSmithCoordinator) -> AsyncMock:
    """Replace async_refresh so a test can count the refreshes it triggers."""
    mock = AsyncMock()
    coordinator.async_refresh = mock  # type: ignore[method-assign]
    return mock


@pytest.fixture
def now(freezer: FrozenDateTimeFactory) -> Callable[[HomeAssistant, float], None]:
    """Freeze the clock (loop time included); advance it and fire due timers.

    Frozen time also stops FakeClient delays, so tests using this keep delay 0.
    """

    def advance(hass: HomeAssistant, seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)

    return advance


async def _until(predicate: Callable[[], bool]) -> None:
    """Let the loop run until predicate holds (nested tasks need a few turns)."""
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


# ---------------------------------------------------------------------------
# Drops
# ---------------------------------------------------------------------------


async def test_drop_schedules_refresh_after_reconnect_delay(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    refresh: AsyncMock,
    now: Any,
) -> None:
    # on_disconnect may run on a non-loop thread.
    await hass.async_add_executor_job(client.fire_disconnect)
    await hass.async_block_till_done()
    assert coordinator.drop_pending is True

    now(hass, RECONNECT_DELAY - 1)
    await hass.async_block_till_done()
    refresh.assert_not_awaited()

    now(hass, 1)
    await hass.async_block_till_done()
    refresh.assert_awaited_once()


async def test_second_drop_replaces_timer(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    refresh: AsyncMock,
    now: Any,
) -> None:
    client.fire_disconnect()
    await hass.async_block_till_done()
    now(hass, RECONNECT_DELAY / 2)
    client.fire_disconnect()
    await hass.async_block_till_done()

    now(hass, RECONNECT_DELAY / 2)
    await hass.async_block_till_done()
    refresh.assert_not_awaited()

    now(hass, RECONNECT_DELAY / 2)
    await hass.async_block_till_done()
    refresh.assert_awaited_once()

    now(hass, RECONNECT_DELAY * 3)
    await hass.async_block_till_done()
    refresh.assert_awaited_once()


@pytest.mark.parametrize("flag", ["closing", "auth_failed", "connecting"])
async def test_drop_ignored_while_flag_set(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    refresh: AsyncMock,
    now: Any,
    flag: str,
) -> None:
    setattr(coordinator, flag, True)
    client.fire_disconnect()
    await hass.async_block_till_done()
    assert coordinator.drop_pending is False

    now(hass, RECONNECT_DELAY * 2)
    await hass.async_block_till_done()
    refresh.assert_not_awaited()


async def test_drop_during_real_connect_schedules_nothing(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    refresh: AsyncMock,
) -> None:
    client.delay = 0.05
    connect = asyncio.create_task(coordinator._connect())
    await _until(lambda: coordinator.connecting)
    client.fire_disconnect()
    await connect
    await hass.async_block_till_done()
    assert coordinator.drop_pending is False
    assert coordinator._reconnect_unsub is None
    refresh.assert_not_awaited()


async def test_drop_before_holder_filled_is_ignored(hass: HomeAssistant) -> None:
    holder = DropHandlerHolder()
    client = FakeClient(asset_id=PLACEHOLDER_ASSET_ID, on_disconnect=holder)
    client.fire_disconnect()  # nothing bound yet: no error, nothing scheduled
    await hass.async_block_till_done()

    handler = MagicMock()
    holder.bind(handler)
    client.fire_disconnect()
    handler.assert_called_once_with()


async def test_scheduled_refresh_clears_drop_pending(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    now: Any,
) -> None:
    seen: list[bool] = []

    async def record() -> None:
        seen.append(coordinator.drop_pending)

    coordinator.async_refresh = record  # type: ignore[method-assign]
    client.fire_disconnect()
    await hass.async_block_till_done()
    assert coordinator.drop_pending is True

    now(hass, RECONNECT_DELAY)
    await hass.async_block_till_done()
    assert seen == [False]
    assert coordinator.drop_pending is False


async def test_scheduled_refresh_reconnects(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    now: Any,
) -> None:
    client._connected = True
    client.fire_disconnect()
    await hass.async_block_till_done()
    now(hass, RECONNECT_DELAY)
    await hass.async_block_till_done()
    assert [c[0] for c in client.calls] == ["connect", "async_get_state"]
    assert client.is_connected
    assert coordinator.last_update_success is True


# ---------------------------------------------------------------------------
# Advertisements
# ---------------------------------------------------------------------------


async def _started(
    coordinator: AOSmithCoordinator, register: MagicMock
) -> Callable[[Any], None]:
    await coordinator.async_start()
    return register.call_args.args[1]


async def test_start_registers_advertisement_callback(
    hass: HomeAssistant, coordinator: AOSmithCoordinator, register: MagicMock
) -> None:
    await coordinator.async_start()
    register.assert_called_once()
    assert register.call_args.args[0] is hass
    assert register.call_args.args[2] == PLACEHOLDER_ADDRESS


async def test_advertisement_after_failed_refresh_requests_refresh(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    register: MagicMock,
) -> None:
    callback = await _started(coordinator, register)
    client.connect_effects = [BleakError("unreachable")]
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False

    callback(MagicMock())
    await hass.async_block_till_done()
    assert coordinator.last_update_success is True
    assert client.is_connected


def _drop_pending(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    coordinator.drop_pending = True


def _connecting(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    coordinator.connecting = True


def _connected(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    client._connected = True


def _succeeded(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    coordinator.last_update_success = True


def _closing(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    coordinator.closing = True


def _auth_failed(coordinator: AOSmithCoordinator, client: FakeClient) -> None:
    coordinator.auth_failed = True


@pytest.mark.parametrize(
    "condition",
    [_drop_pending, _connecting, _connected, _succeeded, _closing, _auth_failed],
    ids=[
        "drop_pending",
        "connecting",
        "connected",
        "last_refresh_succeeded",
        "closing",
        "auth_failed",
    ],
)
async def test_advertisement_ignored(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    register: MagicMock,
    refresh: AsyncMock,
    condition: Callable[[AOSmithCoordinator, FakeClient], None],
) -> None:
    callback = await _started(coordinator, register)
    coordinator.last_update_success = False
    condition(coordinator, client)
    callback(MagicMock())
    await hass.async_block_till_done()
    refresh.assert_not_awaited()


async def test_advertisement_burst_one_immediate_and_one_trailing(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    register: MagicMock,
    refresh: AsyncMock,
    now: Any,
) -> None:
    callback = await _started(coordinator, register)
    coordinator.last_update_success = False
    for _ in range(10):
        callback(MagicMock())
    await hass.async_block_till_done()
    assert refresh.await_count == 1

    now(hass, ADVERT_DEBOUNCE_COOLDOWN)
    await hass.async_block_till_done()
    assert refresh.await_count == 2

    now(hass, ADVERT_DEBOUNCE_COOLDOWN * 4)
    await hass.async_block_till_done()
    assert refresh.await_count == 2


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def test_close_order(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    register: MagicMock,
    unregister: MagicMock,
) -> None:
    order: list[str] = []
    await coordinator.async_start()
    client.fire_disconnect()
    await hass.async_block_till_done()
    assert coordinator.drop_pending is True

    # An in-flight refresh blocked in its read.
    client._connected = True
    client.delay = 30
    poll = asyncio.create_task(coordinator._async_update_data())
    await _until(client._lock.locked)
    in_flight = list(coordinator._refresh_tasks)
    assert len(in_flight) == 1

    def cancel_requested() -> bool:
        return all(task.cancelling() > 0 for task in in_flight)

    timer_cancel = coordinator._reconnect_unsub
    assert timer_cancel is not None

    def on_timer_cancel() -> None:
        assert coordinator.closing is True
        assert not cancel_requested()
        order.append("timer")
        timer_cancel()

    coordinator._reconnect_unsub = on_timer_cancel

    def on_unregister() -> None:
        assert coordinator.closing is True
        assert cancel_requested()
        order.append("unregister")

    unregister.side_effect = on_unregister
    real_close = client.async_close

    async def on_close() -> None:
        assert cancel_requested()
        order.append("async_close")
        await real_close()
        order.append("async_close_done")

    client.async_close = on_close  # type: ignore[method-assign]

    await coordinator.async_close()
    order.append("returned")
    assert order == [
        "timer",
        "unregister",
        "async_close",
        "async_close_done",
        "returned",
    ]
    assert await poll is None  # quiet: last data, no exception
    assert not client.is_connected


async def test_close_waits_for_in_flight_poll_then_no_connect(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    previous = make_feature_set(setpoint=130.0)
    coordinator.data = previous
    client._connected = True
    client.delay = 30
    in_flight = asyncio.create_task(coordinator._async_update_data())
    await _until(client._lock.locked)
    queued = asyncio.create_task(coordinator._async_update_data())
    await _until(lambda: len(coordinator._refresh_tasks) == 2)
    assert coordinator.io_lock.locked()

    await coordinator.async_close()
    # The library close took the client lock, so the read had ended.
    assert not client._lock.locked()
    assert not client.is_connected
    assert await in_flight is previous
    assert await queued is previous
    assert await coordinator._async_update_data() is previous
    close_at = [c[0] for c in client.calls].index("async_close")
    assert "connect" not in [c[0] for c in client.calls[close_at:]]


async def test_close_with_connect_in_flight_leaves_no_live_link(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    client.delay = 0.05
    connect = asyncio.create_task(coordinator._connect())
    await _until(lambda: client.active_connects == 1)

    await coordinator.async_close()
    # connect() holds no lock, so close does not wait for it.
    assert not connect.done()
    with pytest.raises(NotConnectedError):
        await connect
    assert not client.is_connected
    assert client.active_connects == 0
    assert coordinator.connecting is False


async def test_close_timeout_falls_back_to_disconnect(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client._connected = True

    async def hang() -> None:
        client.calls.append(("async_close",))
        await asyncio.sleep(3600)

    client.async_close = hang  # type: ignore[method-assign]
    with patch("custom_components.aosmith_ble.coordinator.CLOSE_TIMEOUT", 0.01):
        await coordinator.async_close()
    assert [c[0] for c in client.calls] == ["async_close", "disconnect"]
    assert not client.is_connected
    assert [r for r in caplog.records if r.levelname == "WARNING"]


async def test_close_idempotent(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    register: MagicMock,
    unregister: MagicMock,
) -> None:
    await coordinator.async_start()
    await coordinator.async_close()
    await coordinator.async_close()
    await asyncio.gather(coordinator.async_close(), coordinator.async_close())
    assert [c[0] for c in client.calls] == ["async_close"]
    unregister.assert_called_once_with()


async def test_shutdown_closes_client(
    hass: HomeAssistant, coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    await coordinator.async_shutdown()
    assert coordinator.closing is True
    assert [c[0] for c in client.calls] == ["async_close"]


async def test_close_error_falls_back_to_disconnect(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client._connected = True

    async def broken() -> None:
        client.calls.append(("async_close",))
        raise BleakError("link gone")

    client.async_close = broken  # type: ignore[method-assign]
    await coordinator.async_close()
    assert [c[0] for c in client.calls] == ["async_close", "disconnect"]
    assert [r for r in caplog.records if r.levelname == "WARNING"]


async def test_start_after_close_registers_nothing(
    hass: HomeAssistant, coordinator: AOSmithCoordinator, register: MagicMock
) -> None:
    await coordinator.async_close()
    await coordinator.async_start()
    register.assert_not_called()


async def test_start_twice_replaces_callback(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    register: MagicMock,
    unregister: MagicMock,
) -> None:
    await coordinator.async_start()
    await coordinator.async_start()
    assert register.call_count == 2
    unregister.assert_called_once_with()
    await coordinator.async_close()
    assert unregister.call_count == 2


async def test_close_unschedules_interval_refresh(
    hass: HomeAssistant, coordinator: AOSmithCoordinator
) -> None:
    coordinator.async_add_listener(MagicMock())
    assert coordinator._unsub_refresh is not None
    await coordinator.async_close()
    assert coordinator._unsub_refresh is None


async def test_outer_cancel_of_update_reraises_and_cancels_poll(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    client._connected = True
    client.delay = 30
    outer = asyncio.create_task(coordinator._async_update_data())
    await _until(client._lock.locked)
    inner = next(iter(coordinator._refresh_tasks))
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert inner.cancelled()
    assert coordinator._refresh_tasks == set()
    assert not client._lock.locked()


async def test_start_listener_ignores_a_synchronous_replay(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    refresh: AsyncMock,
    pre_2026_10_bluetooth: ReplayingRegister,
) -> None:
    # Even with a failed last refresh, a replay delivered inside the register
    # call (fallback before 2026.10) is not an advertisement to act on.
    coordinator.last_update_success = False
    await coordinator.async_start()
    await hass.async_block_till_done()
    ((args, kwargs),) = pre_2026_10_bluetooth.calls
    assert args[2] == {"address": PLACEHOLDER_ADDRESS, "connectable": True}
    assert kwargs["replay"] is BluetoothCallbackReplay.DISABLED
    refresh.assert_not_awaited()

    # A later advertisement still triggers the refresh.
    args[1](MagicMock(), BluetoothChange.ADVERTISEMENT)
    await hass.async_block_till_done()
    refresh.assert_awaited_once()
