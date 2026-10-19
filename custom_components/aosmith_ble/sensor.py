"""The fault, days remaining, power, energy and hot water availability sensors."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import Fault, FieldName, HotWaterAvailability, Mode, Trust

from . import AOSmithConfigEntry
from .const import FAULT_KEYS, decode_mode
from .coordinator import AOSmithCoordinator
from .entity import (
    KEY_ELECTRIC_DAYS_REMAINING,
    KEY_ENERGY,
    KEY_FAULT,
    KEY_HOT_WATER_AVAILABILITY,
    KEY_POWER,
    KEY_VACATION_DAYS_REMAINING,
    AOSmithEntity,
    created_keys,
)

FAULT_NONE = "none"
# Any code without a key, including codes a newer library adds.
FAULT_UNKNOWN = "unknown"
FAULT_OPTIONS = [FAULT_NONE, *FAULT_KEYS.values(), FAULT_UNKNOWN]

# Any word outside the known levels, kept raw in the attribute.
AVAILABILITY_UNKNOWN = "unknown"
AVAILABILITY_OPTIONS = [
    *(level.name.lower() for level in HotWaterAvailability),
    AVAILABILITY_UNKNOWN,
]

# Counter key -> (counter field, the mode during which it counts).
_COUNTERS: dict[str, tuple[FieldName, Mode]] = {
    KEY_ELECTRIC_DAYS_REMAINING: (FieldName.ELECTRIC_DAYS_REMAINING, Mode.ELECTRIC),
    KEY_VACATION_DAYS_REMAINING: (FieldName.VACATION_DAYS_REMAINING, Mode.VACATION),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AOSmithConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator is None:
        return
    keys = created_keys(coordinator)
    entities: list[SensorEntity] = []
    if KEY_FAULT in keys:
        entities.append(AOSmithFaultSensor(coordinator))
    entities.extend(
        AOSmithDaysRemainingSensor(coordinator, key, field, mode)
        for key, (field, mode) in _COUNTERS.items()
        if key in keys
    )
    if KEY_POWER in keys:
        entities.append(AOSmithPowerSensor(coordinator))
    if KEY_ENERGY in keys:
        entities.append(AOSmithEnergySensor(coordinator))
    if KEY_HOT_WATER_AVAILABILITY in keys:
        entities.append(AOSmithHotWaterAvailabilitySensor(coordinator))
    async_add_entities(entities)


class AOSmithFaultSensor(AOSmithEntity, SensorEntity):
    """The active fault as an enum, with the raw code as an attribute."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = FAULT_OPTIONS
    _attr_translation_key = KEY_FAULT

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(coordinator, KEY_FAULT, (FieldName.FAULT,))

    def _fault(self) -> Fault | None:
        fault = self._value(FieldName.FAULT)
        return fault if isinstance(fault, Fault) else None

    @property
    @override
    def native_value(self) -> str | None:
        fault = self._fault()
        if fault is None:
            return None
        if not fault.active:
            return FAULT_NONE
        return FAULT_KEYS.get(fault.code, FAULT_UNKNOWN)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fault = self._fault()
        return None if fault is None else {"code": fault.code}


class AOSmithDaysRemainingSensor(AOSmithEntity, SensorEntity):
    """A timed mode's counter. The library reports raw values; 0 when inactive."""

    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(
        self,
        coordinator: AOSmithCoordinator,
        key: str,
        field: FieldName,
        mode: Mode,
    ) -> None:
        super().__init__(coordinator, key, (field, FieldName.MODE))
        self._attr_translation_key = key
        self._field = field
        self._mode = mode

    @property
    @override
    def native_value(self) -> int | None:
        value = self._value(self._field)
        if not isinstance(value, int):
            return None
        mode = self._value(FieldName.MODE)
        if mode is None or decode_mode(mode) is not self._mode:
            return 0
        return value


class AOSmithPowerSensor(AOSmithEntity, SensorEntity):
    """The heater's own figure for what is running (compressor or element).

    Not a meter: it reports fixed values per component, 0 when idle.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = KEY_POWER

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(coordinator, KEY_POWER, (FieldName.POWER_W,))

    @property
    @override
    def native_value(self) -> int | None:
        value = self._value(FieldName.POWER_W)
        return value if isinstance(value, int) else None


class AOSmithEnergySensor(AOSmithEntity, SensorEntity):
    """The heater's running total of its reported power; not a meter.

    Whether the counter ever resets is unknown, which TOTAL_INCREASING handles.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = KEY_ENERGY

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(coordinator, KEY_ENERGY, (FieldName.ENERGY_WH,))

    @property
    @override
    def native_value(self) -> int | None:
        # The library reports None when the heater sends all ones.
        value = self._value(FieldName.ENERGY_WH)
        return value if isinstance(value, int) else None


class AOSmithHotWaterAvailabilitySensor(AOSmithEntity, SensorEntity):
    """Experimental: the heater's coarse availability level, read at trust PARTIAL.

    Disabled by default. Its history is the evidence for promoting the field, so
    the raw word stays an attribute: a value outside the known levels is exactly
    what needs to be seen.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AVAILABILITY_OPTIONS
    _attr_translation_key = KEY_HOT_WATER_AVAILABILITY
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(
            coordinator,
            KEY_HOT_WATER_AVAILABILITY,
            (FieldName.HOT_WATER_AVAILABILITY,),
            Trust.PARTIAL,
        )

    def _raw(self) -> int | None:
        value = self._value(FieldName.HOT_WATER_AVAILABILITY)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return int(value)

    @property
    @override
    def native_value(self) -> str | None:
        value = self._value(FieldName.HOT_WATER_AVAILABILITY)
        if isinstance(value, HotWaterAvailability):
            return value.name.lower()
        return None if self._raw() is None else AVAILABILITY_UNKNOWN

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        raw = self._raw()
        return None if raw is None else {"raw": raw}
