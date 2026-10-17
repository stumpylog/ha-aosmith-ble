"""The fault and days remaining sensors."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import Fault, FieldName, Mode

from . import AOSmithConfigEntry
from .const import FAULT_KEYS, decode_mode
from .coordinator import AOSmithCoordinator
from .entity import (
    KEY_ELECTRIC_DAYS_REMAINING,
    KEY_FAULT,
    KEY_VACATION_DAYS_REMAINING,
    AOSmithEntity,
    created_keys,
)

FAULT_NONE = "none"
# Any code without a key, including codes a newer library adds.
FAULT_UNKNOWN = "unknown"
FAULT_OPTIONS = [FAULT_NONE, *FAULT_KEYS.values(), FAULT_UNKNOWN]

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
