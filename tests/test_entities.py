"""Select, sensor, binary sensor and number entities, creation and device info."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aosmith_ble import (
    DeviceInfo,
    FieldName,
    HotWaterAvailability,
    Mode,
    ReadRefusedError,
    Trust,
)
from aosmith_ble.codecs import ModeWord
from aosmith_ble.const import FAULT_CODES
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH

from custom_components.aosmith_ble.const import (
    DOMAIN,
    FAULT_KEYS,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_NAME,
)
from custom_components.aosmith_ble.entity import created_unique_ids
from custom_components.aosmith_ble.store import StateStore, UserState

from .conftest import Heater
from .fakes import make_feature_set, make_profile

Load = Callable[..., Any]

TRANSLATIONS = json.loads(
    (
        Path(__file__).parent.parent
        / "custom_components/aosmith_ble/translations/en.json"
    ).read_text()
)

ALL_KEYS = {
    "water_heater": "water_heater",
    "operating_mode": "select",
    "fault": "sensor",
    "problem": "binary_sensor",
    "electric_days_remaining": "sensor",
    "vacation_days_remaining": "sensor",
    "electric_days": "number",
    "vacation_days": "number",
    "power": "sensor",
    "energy": "sensor",
    "hot_water_availability": "sensor",
}


def _mode(mode: Mode, days: int = 0) -> ModeWord:
    return ModeWord(duration_days=days, mode=int(mode))


def _registry_ids(hass: HomeAssistant, heater: Heater) -> set[str]:
    registry = er.async_get(hass)
    return {
        e.unique_id
        for e in er.async_entries_for_config_entry(registry, heater.entry.entry_id)
    }


# Select


async def test_select_options_follow_writable_modes(load_heater: Load) -> None:
    heater = await load_heater(
        profile=make_profile(writable_modes=(Mode.HEAT_PUMP, Mode.HYBRID, Mode.GUEST))
    )
    state = heater.state("select", "operating_mode")
    assert state.attributes["options"] == ["heat_pump", "hybrid"]
    assert state.state == "heat_pump"


async def test_select_writes_mode(hass: HomeAssistant, load_heater: Load) -> None:
    heater = await load_heater()
    await hass.services.async_call(
        "select",
        "select_option",
        {
            ATTR_ENTITY_ID: heater.entity_id("select", "operating_mode"),
            "option": "vacation",
        },
        blocking=True,
    )
    assert heater.writes() == [("async_set_mode", Mode.VACATION, 7)]


async def test_select_shows_none_for_guest(load_heater: Load) -> None:
    heater = await load_heater(state=make_feature_set(mode=_mode(Mode.GUEST, 1)))
    assert heater.state("select", "operating_mode").state == STATE_UNKNOWN


# Fault sensor and problem binary sensor


async def test_fault_sensor_options() -> None:
    from custom_components.aosmith_ble.sensor import FAULT_OPTIONS

    assert ["none", *FAULT_KEYS.values(), "unknown"] == FAULT_OPTIONS
    assert set(FAULT_CODES) <= set(FAULT_KEYS)


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "none"), (31, "water_leak"), (77, "unknown")],
)
async def test_fault_sensor_state_and_code(
    load_heater: Load, code: int, expected: str
) -> None:
    heater = await load_heater(state=make_feature_set(fault=code))
    state = heater.state("sensor", "fault")
    assert state.state == expected
    assert state.attributes["code"] == code
    assert state.attributes["device_class"] == "enum"
    assert expected in state.attributes["options"]


def test_every_option_and_entity_is_translated() -> None:
    from custom_components.aosmith_ble.sensor import FAULT_OPTIONS

    entity = TRANSLATIONS["entity"]
    assert set(entity["sensor"]["fault"]["state"]) == set(FAULT_OPTIONS)
    assert set(entity["select"]["operating_mode"]["state"]) == {
        "hybrid",
        "heat_pump",
        "electric",
        "vacation",
    }
    for key, platform in ALL_KEYS.items():
        if key != "water_heater":
            assert entity[platform][key]["name"]
    for code, key in FAULT_KEYS.items():
        assert entity["sensor"]["fault"]["state"][key] == FAULT_CODES[code]
    from custom_components.aosmith_ble.sensor import AVAILABILITY_OPTIONS

    availability = entity["sensor"]["hot_water_availability"]
    assert set(availability["state"]) == set(AVAILABILITY_OPTIONS)


@pytest.mark.parametrize(("code", "expected"), [(0, STATE_OFF), (31, STATE_ON)])
async def test_problem_binary_sensor(
    load_heater: Load, code: int, expected: str
) -> None:
    heater = await load_heater(state=make_feature_set(fault=code))
    state = heater.state("binary_sensor", "problem")
    assert state.state == expected
    assert state.attributes["device_class"] == "problem"


# Counters (0 when not active)


@pytest.mark.parametrize(
    ("mode", "electric", "vacation"),
    [(Mode.ELECTRIC, "4", "0"), (Mode.VACATION, "0", "5"), (Mode.HYBRID, "0", "0")],
)
async def test_counters_read_zero_unless_their_mode_is_active(
    load_heater: Load, mode: Mode, electric: str, vacation: str
) -> None:
    heater = await load_heater(
        state=make_feature_set(mode=_mode(mode), electric_days=4, vacation_days=5)
    )
    electric_state = heater.state("sensor", "electric_days_remaining")
    assert electric_state.state == electric
    assert electric_state.attributes["unit_of_measurement"] == "d"
    assert heater.state("sensor", "vacation_days_remaining").state == vacation


# Power, energy and hot water availability


async def test_power_and_energy_sensors(load_heater: Load) -> None:
    heater = await load_heater(state=make_feature_set(power=4500, energy=123_456))
    power = heater.state("sensor", "power")
    assert power.state == "4500"
    assert power.attributes["device_class"] == "power"
    assert power.attributes["unit_of_measurement"] == "W"
    assert power.attributes["state_class"] == "measurement"
    energy = heater.state("sensor", "energy")
    # Native Wh, shown in the suggested kWh.
    assert float(energy.state) == pytest.approx(123.456)
    assert energy.attributes["device_class"] == "energy"
    assert energy.attributes["unit_of_measurement"] == "kWh"
    assert energy.attributes["state_class"] == "total_increasing"


async def test_energy_none_is_unknown_not_zero(load_heater: Load) -> None:
    heater = await load_heater()
    later = make_feature_set()
    later[FieldName.ENERGY_WH] = dataclasses.replace(
        later[FieldName.ENERGY_WH], value=None
    )
    await heater.refresh(later)
    assert heater.state("sensor", "energy").state == STATE_UNKNOWN


def _enable_availability(hass: HomeAssistant, config_entry: Any) -> None:
    """Register the disabled-by-default sensor as enabled before setup."""
    config_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{PLACEHOLDER_ADDRESS}_hot_water_availability",
        config_entry=config_entry,
    )


async def test_availability_disabled_by_default(
    hass: HomeAssistant, load_heater: Load
) -> None:
    heater = await load_heater()
    entity_id = heater.entity_id("sensor", "hot_water_availability")
    assert entity_id is not None
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


@pytest.mark.parametrize(
    ("value", "expected", "raw"),
    [
        (HotWaterAvailability.LOW, "low", 0),
        (HotWaterAvailability.MEDIUM, "medium", 5),
        (HotWaterAvailability.HIGH, "high", 10),
        (7, "unknown", 7),
    ],
)
async def test_availability_state_and_raw_word(
    hass: HomeAssistant,
    config_entry: Any,
    load_heater: Load,
    value: HotWaterAvailability | int,
    expected: str,
    raw: int,
) -> None:
    _enable_availability(hass, config_entry)
    heater = await load_heater(state=make_feature_set(availability=value))
    state = heater.state("sensor", "hot_water_availability")
    assert state.state == expected
    assert state.attributes["raw"] == raw
    assert state.attributes["device_class"] == "enum"
    assert expected in state.attributes["options"]


async def test_entities_need_trust_ok_except_availability(
    hass: HomeAssistant, load_heater: Load
) -> None:
    """A field the PARTIAL poll returns gets no entity unless the entity allows it."""
    heater = await load_heater(
        state=make_feature_set(
            trusts={
                FieldName.POWER_W: Trust.PARTIAL,
                FieldName.FAULT: Trust.PARTIAL,
            }
        )
    )
    ids = _registry_ids(hass, heater)
    for key in ("power", "fault", "problem"):
        assert f"{PLACEHOLDER_ADDRESS}_{key}" not in ids
    assert f"{PLACEHOLDER_ADDRESS}_energy" in ids
    assert f"{PLACEHOLDER_ADDRESS}_hot_water_availability" in ids


# Number entities


async def test_numbers_show_stored_value_and_never_write_to_heater(
    hass: HomeAssistant,
    config_entry: Any,
    load_heater: Load,
    hass_storage: dict[str, Any],
) -> None:
    await StateStore(hass, config_entry.entry_id).async_save(
        UserState(electric_days=5, vacation_days=10)
    )
    heater = await load_heater()
    electric = heater.state("number", "electric_days")
    assert float(electric.state) == 5
    assert (electric.attributes["min"], electric.attributes["max"]) == (1, 7)
    vacation = heater.state("number", "vacation_days")
    assert float(vacation.state) == 10
    assert (vacation.attributes["min"], vacation.attributes["max"]) == (1, 99)

    await hass.services.async_call(
        "number",
        "set_value",
        {ATTR_ENTITY_ID: heater.entity_id("number", "electric_days"), "value": 2},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert float(heater.state("number", "electric_days").state) == 2
    assert heater.coordinator.electric_days == 2
    assert (
        hass_storage[f"{DOMAIN}.{config_entry.entry_id}"]["data"]["electric_days"] == 2
    )
    assert heater.writes() == []


async def test_number_converts_whole_float_and_rejects_fraction(
    hass: HomeAssistant, load_heater: Load
) -> None:
    heater = await load_heater()
    entity_id = heater.entity_id("number", "vacation_days")
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: entity_id, "value": 12.0}, blocking=True
    )
    assert heater.coordinator.vacation_days == 12
    assert type(heater.coordinator.vacation_days) is int
    with pytest.raises(ServiceValidationError) as info:
        await hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": 12.5},
            blocking=True,
        )
    assert info.value.translation_key == "invalid_duration"
    assert heater.coordinator.vacation_days == 12
    assert heater.writes() == []


# Availability


async def test_missing_field_makes_entity_unavailable(
    load_heater: Load, caplog: pytest.LogCaptureFixture
) -> None:
    heater = await load_heater()
    later = make_feature_set()
    del later[FieldName.FAULT]
    del later[FieldName.MODE]
    await heater.refresh(later)

    for platform, key in (
        ("sensor", "fault"),
        ("binary_sensor", "problem"),
        ("select", "operating_mode"),
        ("water_heater", "water_heater"),
        ("sensor", "electric_days_remaining"),
    ):
        assert heater.state(platform, key).state == STATE_UNAVAILABLE, key
    assert heater.state("number", "electric_days").state != STATE_UNAVAILABLE
    assert "KeyError" not in caplog.text
    assert "Traceback" not in caplog.text

    await heater.refresh(make_feature_set())
    assert heater.state("sensor", "fault").state == "none"


async def test_entities_unavailable_when_update_fails(load_heater: Load) -> None:
    heater = await load_heater()
    heater.client.read_effects = [ReadRefusedError("refused")]
    await heater.refresh()
    assert heater.state("sensor", "fault").state == STATE_UNAVAILABLE
    assert heater.state("water_heater", "water_heater").state == STATE_UNAVAILABLE


# Creation (profile-driven)


@pytest.mark.parametrize(
    ("profile_kwargs", "state_kwargs", "missing"),
    [
        ({}, {"guest_days": 2}, set()),
        (
            {"writable_modes": (Mode.HYBRID, Mode.HEAT_PUMP, Mode.ELECTRIC)},
            {},
            {"vacation_days"},
        ),
        (
            {"writable_modes": (Mode.HYBRID, Mode.HEAT_PUMP, Mode.VACATION)},
            {"electric_days": None},
            {"electric_days", "electric_days_remaining"},
        ),
        (
            {"writable_modes": ()},
            {"fault": 0},
            {"operating_mode", "electric_days", "vacation_days"},
        ),
        (
            {},
            {"power": None, "energy": None, "availability": None},
            {"power", "energy", "hot_water_availability"},
        ),
    ],
    ids=["full", "no_vacation_write", "no_electric", "no_writable_modes", "no_b27"],
)
async def test_entities_created_per_profile(
    hass: HomeAssistant,
    load_heater: Load,
    profile_kwargs: dict[str, Any],
    state_kwargs: dict[str, Any],
    missing: set[str],
) -> None:
    heater = await load_heater(
        state=make_feature_set(**state_kwargs), profile=make_profile(**profile_kwargs)
    )
    expected = {f"{PLACEHOLDER_ADDRESS}_{key}" for key in set(ALL_KEYS) - missing}
    assert _registry_ids(hass, heater) == expected
    assert created_unique_ids(heater.coordinator) == expected
    assert not any("guest" in uid for uid in expected)


async def test_no_controls_when_mode_field_not_writable(
    hass: HomeAssistant, load_heater: Load
) -> None:
    base = make_profile()
    fields = tuple(
        (name, dataclasses.replace(spec, write=Trust.UNSUPPORTED))
        if name in {FieldName.MODE, FieldName.SETPOINT}
        else (name, spec)
        for name, spec in base.fields
    )
    heater = await load_heater(profile=make_profile(fields=fields))
    ids = _registry_ids(hass, heater)
    for key in ("water_heater", "operating_mode", "electric_days", "vacation_days"):
        assert f"{PLACEHOLDER_ADDRESS}_{key}" not in ids
    assert f"{PLACEHOLDER_ADDRESS}_fault" in ids


async def test_stale_entities_removed_on_setup(
    hass: HomeAssistant, config_entry: Any, load_heater: Load
) -> None:
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{PLACEHOLDER_ADDRESS}_guest_days_remaining",
        config_entry=config_entry,
    )
    heater = await load_heater()
    assert registry.async_get(stale.entity_id) is None
    assert heater.entity_id("sensor", "fault") is not None


# Device registry


@pytest.mark.parametrize(
    ("info", "name", "sw_version"),
    [
        (
            DeviceInfo(model="HPTS-50", serial="XXXXXX", firmware=(6, 3)),
            "A. O. Smith HPTS-50",
            "6.3",
        ),
        (DeviceInfo(), "A. O. Smith water heater", None),
    ],
    ids=["known_model", "unknown_model"],
)
async def test_device_info_never_uses_advertised_name(
    hass: HomeAssistant,
    config_entry: Any,
    load_heater: Load,
    info: DeviceInfo,
    name: str,
    sw_version: str | None,
) -> None:
    # A title holding the advertised name must still not reach the device.
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(config_entry, title=PLACEHOLDER_NAME)
    heater = await load_heater(device_info=info)
    assert heater.entry.title == PLACEHOLDER_NAME
    (device,) = dr.async_entries_for_config_entry(
        dr.async_get(hass), heater.entry.entry_id
    )
    assert device.connections == {(CONNECTION_BLUETOOTH, PLACEHOLDER_ADDRESS)}
    assert device.name == name
    assert device.manufacturer == "A. O. Smith"
    assert device.model == info.model
    assert device.serial_number == info.serial
    assert device.sw_version == sw_version
    assert PLACEHOLDER_NAME not in repr(device)
    assert heater.state("water_heater", "water_heater").name == name
