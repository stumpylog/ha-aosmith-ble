"""The water heater entity and the set_timed_mode service."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
import voluptuous as vol
from aosmith_ble import Mode
from aosmith_ble.codecs import ModeWord
from homeassistant.components.water_heater import (
    ATTR_OPERATION_LIST,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.aosmith_ble.const import DOMAIN, decode_mode
from custom_components.aosmith_ble.store import StateStore, UserState

from .conftest import Heater
from .fakes import make_feature_set, make_profile

Load = Callable[..., Any]


def _mode(mode: Mode | int, days: int = 0) -> ModeWord:
    return ModeWord(duration_days=days, mode=int(mode))


@pytest.fixture(autouse=True)
def _fahrenheit(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM


async def _call(heater: Heater, service: str, **data: Any) -> None:
    await heater.hass.services.async_call(
        "water_heater",
        service,
        {ATTR_ENTITY_ID: heater.entity_id("water_heater", "water_heater"), **data},
        blocking=True,
    )
    await heater.hass.async_block_till_done()


async def _timed(heater: Heater, mode: str, days: Any) -> None:
    await heater.hass.services.async_call(
        DOMAIN,
        "set_timed_mode",
        {
            ATTR_ENTITY_ID: heater.entity_id("water_heater", "water_heater"),
            "mode": mode,
            "days": days,
        },
        blocking=True,
    )
    await heater.hass.async_block_till_done()


async def test_limits_unit_and_temperatures_from_profile(load_heater: Load) -> None:
    heater = await load_heater(
        profile=make_profile(min_setpoint_f=100, max_setpoint_f=125)
    )
    state = heater.state("water_heater", "water_heater")
    assert state.attributes["min_temp"] == 100
    assert state.attributes["max_temp"] == 125
    assert state.attributes["target_temp_step"] == 1
    assert state.attributes["temperature"] == 120
    assert state.attributes["current_temperature"] is None


async def test_operation_list_excludes_vacation(load_heater: Load) -> None:
    heater = await load_heater()
    state = heater.state("water_heater", "water_heater")
    assert state.attributes[ATTR_OPERATION_LIST] == ["eco", "heat_pump", "electric"]
    assert state.state == "heat_pump"


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        (
            (Mode.HYBRID, Mode.HEAT_PUMP, Mode.ELECTRIC, Mode.VACATION),
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE
            | WaterHeaterEntityFeature.AWAY_MODE,
        ),
        (
            (Mode.HYBRID, Mode.VACATION),
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.AWAY_MODE,
        ),
        (
            (Mode.HYBRID, Mode.HEAT_PUMP),
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.OPERATION_MODE,
        ),
    ],
)
async def test_features_follow_writable_modes(
    load_heater: Load, modes: tuple[Mode, ...], expected: int
) -> None:
    heater = await load_heater(profile=make_profile(writable_modes=modes))
    state = heater.state("water_heater", "water_heater")
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == expected


async def test_set_temperature_writes_setpoint(load_heater: Load) -> None:
    heater = await load_heater()
    await _call(heater, "set_temperature", temperature=125)
    assert heater.writes() == [("async_set_setpoint", 125)]


async def test_set_operation_mode_writes_mode(load_heater: Load) -> None:
    heater = await load_heater()
    await _call(heater, "set_operation_mode", operation_mode="electric")
    assert heater.writes() == [("async_set_mode", Mode.ELECTRIC, 3)]


async def test_away_mode_on_writes_vacation(load_heater: Load) -> None:
    heater = await load_heater()
    await _call(heater, "set_away_mode", away_mode=True)
    assert heater.writes() == [("async_set_mode", Mode.VACATION, 7)]


async def test_away_mode_off_is_noop_unless_vacation(load_heater: Load) -> None:
    heater = await load_heater(state=make_feature_set(mode=_mode(Mode.ELECTRIC, 2)))
    assert heater.state("water_heater", "water_heater").attributes["away_mode"] == "off"
    await _call(heater, "set_away_mode", away_mode=False)
    assert heater.writes() == []


async def test_away_mode_off_returns_to_stored_last_permanent_mode(
    hass: HomeAssistant, config_entry: Any, load_heater: Load
) -> None:
    # Persisted by an earlier run; a VACATION poll does not overwrite it.
    await StateStore(hass, config_entry.entry_id).async_save(
        UserState(last_permanent_mode=int(Mode.HEAT_PUMP))
    )
    heater = await load_heater(state=make_feature_set(mode=_mode(Mode.VACATION, 5)))
    state = heater.state("water_heater", "water_heater")
    assert state.state == "off"
    assert state.attributes["away_mode"] == "on"
    await _call(heater, "set_away_mode", away_mode=False)
    assert heater.writes() == [("async_set_mode", Mode.HEAT_PUMP, 0)]


@pytest.mark.parametrize(
    ("modes", "expected"),
    [
        ((Mode.HEAT_PUMP, Mode.HYBRID, Mode.VACATION), Mode.HYBRID),
        ((Mode.ELECTRIC, Mode.HEAT_PUMP, Mode.VACATION), Mode.HEAT_PUMP),
    ],
    ids=["hybrid", "first_writable_permanent"],
)
async def test_away_mode_off_fallbacks(
    load_heater: Load, modes: tuple[Mode, ...], expected: Mode
) -> None:
    heater = await load_heater(
        state=make_feature_set(mode=_mode(Mode.VACATION, 5)),
        profile=make_profile(writable_modes=modes),
    )
    assert heater.coordinator.last_permanent_mode is None
    await _call(heater, "set_away_mode", away_mode=False)
    assert heater.writes() == [("async_set_mode", expected, 0)]


async def test_guest_shows_high_demand_and_writes_cleanly(load_heater: Load) -> None:
    heater = await load_heater(state=make_feature_set(mode=_mode(Mode.GUEST, 1)))
    state = heater.state("water_heater", "water_heater")
    assert state.state == "high_demand"
    assert state.attributes["away_mode"] == "off"
    await _call(heater, "set_operation_mode", operation_mode="eco")
    assert heater.writes() == [("async_set_mode", Mode.HYBRID, 0)]


async def test_undecodable_mode_is_none_for_water_heater_and_select(
    load_heater: Load,
) -> None:
    heater = await load_heater(state=make_feature_set(mode=ModeWord(0, 9)))
    assert heater.state("water_heater", "water_heater").state == STATE_UNKNOWN
    assert heater.state("select", "operating_mode").state == STATE_UNKNOWN


def test_decode_mode_logs_undecodable_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.aosmith_ble")
    assert decode_mode(ModeWord(0, 42)) is None
    assert decode_mode(ModeWord(0, 42)) is None
    assert decode_mode("garbage") is None
    assert decode_mode(ModeWord(0, int(Mode.HYBRID))) is Mode.HYBRID
    records = [r for r in caplog.records if "42" in r.getMessage()]
    assert len(records) == 1


async def test_set_timed_mode_writes_days_and_leaves_stored_counts(
    load_heater: Load, hass_storage: dict[str, Any]
) -> None:
    heater = await load_heater()
    await _timed(heater, "electric", 5)
    await _timed(heater, "vacation", 30)
    assert heater.writes() == [
        ("async_set_mode", Mode.ELECTRIC, 5),
        ("async_set_mode", Mode.VACATION, 30),
    ]
    assert heater.coordinator.electric_days == 3
    assert heater.coordinator.vacation_days == 7
    stored = hass_storage[f"{DOMAIN}.{heater.entry.entry_id}"]["data"]
    assert (stored["electric_days"], stored["vacation_days"]) == (3, 7)


@pytest.mark.parametrize(
    ("mode", "days"),
    [("electric", 0), ("electric", 8), ("vacation", 0), ("vacation", 100)],
)
async def test_set_timed_mode_validates_range_per_mode(
    load_heater: Load, mode: str, days: int
) -> None:
    heater = await load_heater()
    with pytest.raises(ServiceValidationError) as info:
        await _timed(heater, mode, days)
    assert info.value.translation_key == "invalid_duration"
    assert heater.writes() == []


async def test_set_timed_mode_rejects_unknown_mode(load_heater: Load) -> None:
    heater = await load_heater()
    with pytest.raises(vol.Invalid, match="mode"):
        await _timed(heater, "guest", 1)
    assert heater.writes() == []


async def test_set_timed_mode_converts_whole_float_and_rejects_fraction(
    load_heater: Load,
) -> None:
    heater = await load_heater()
    await _timed(heater, "vacation", 2.0)
    ((_, mode, days),) = heater.writes()
    assert mode is Mode.VACATION
    assert days == 2
    assert type(days) is int
    with pytest.raises(ServiceValidationError) as info:
        await _timed(heater, "vacation", 2.5)
    assert info.value.translation_key == "invalid_duration"
    assert len(heater.writes()) == 1
