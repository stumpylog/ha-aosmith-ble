"""The problem binary sensor."""

from __future__ import annotations

from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import Fault, FieldName

from . import AOSmithConfigEntry
from .coordinator import AOSmithCoordinator
from .entity import KEY_PROBLEM, AOSmithEntity, created_keys


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AOSmithConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator is not None and KEY_PROBLEM in created_keys(coordinator):
        async_add_entities([AOSmithProblemSensor(coordinator)])


class AOSmithProblemSensor(AOSmithEntity, BinarySensorEntity):
    """On while the heater reports a fault."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = KEY_PROBLEM

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(coordinator, KEY_PROBLEM, (FieldName.FAULT,))

    @property
    @override
    def is_on(self) -> bool | None:
        fault = self._value(FieldName.FAULT)
        return fault.active if isinstance(fault, Fault) else None
