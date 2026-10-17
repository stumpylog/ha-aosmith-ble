"""Tests for the per-entry user state store."""

from __future__ import annotations

from aosmith_ble.const import Mode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.aosmith_ble.store import StateStore, UserState


async def test_defaults_when_missing(hass: HomeAssistant) -> None:
    state = await StateStore(hass, "e1").async_load()
    assert state == UserState(3, 7, None)


async def test_round_trip(hass: HomeAssistant, hass_storage: dict) -> None:
    store = StateStore(hass, "e1")
    await store.async_save(UserState(5, 20, int(Mode.HEAT_PUMP)))
    assert hass_storage["aosmith_ble.e1"]["version"] == 1
    assert await StateStore(hass, "e1").async_load() == UserState(
        5, 20, int(Mode.HEAT_PUMP)
    )


async def test_corrupt_values_fall_back_and_clamp(hass: HomeAssistant) -> None:
    raw = Store(hass, 1, "aosmith_ble.e1")
    await raw.async_save(
        {"electric_days": "x", "vacation_days": 100, "last_permanent_mode": 99}
    )
    assert await StateStore(hass, "e1").async_load() == UserState(3, 99, None)
    await raw.async_save(
        {"electric_days": 8, "vacation_days": 0, "last_permanent_mode": int(Mode.GUEST)}
    )
    assert await StateStore(hass, "e1").async_load() == UserState(7, 1, None)
    await raw.async_save({"electric_days": 0, "vacation_days": True})
    assert await StateStore(hass, "e1").async_load() == UserState(1, 7, None)
    await raw.async_save(["junk"])
    assert await StateStore(hass, "e1").async_load() == UserState()


async def test_remove_deletes_file(hass: HomeAssistant, hass_storage: dict) -> None:
    store = StateStore(hass, "e1")
    await store.async_save(UserState())
    assert "aosmith_ble.e1" in hass_storage
    await store.async_remove()
    assert "aosmith_ble.e1" not in hass_storage
