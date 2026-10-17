"""The timed mode day counts.

A view of the coordinator's stored value; setting one never writes the heater.
"""

from __future__ import annotations

from typing import override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import Mode

from . import AOSmithConfigEntry
from .const import DAYS_RANGE
from .coordinator import AOSmithCoordinator
from .entity import (
    KEY_ELECTRIC_DAYS,
    KEY_VACATION_DAYS,
    AOSmithEntity,
    created_keys,
    whole_days,
)

_NUMBERS: dict[str, Mode] = {
    KEY_ELECTRIC_DAYS: Mode.ELECTRIC,
    KEY_VACATION_DAYS: Mode.VACATION,
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
    async_add_entities(
        AOSmithDaysNumber(coordinator, key, mode)
        for key, mode in _NUMBERS.items()
        if key in keys
    )


class AOSmithDaysNumber(AOSmithEntity, NumberEntity):
    """The day count used when the timed mode is selected."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(self, coordinator: AOSmithCoordinator, key: str, mode: Mode) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._mode = mode
        self._attr_native_min_value, self._attr_native_max_value = DAYS_RANGE[mode]

    @property
    @override
    def native_value(self) -> int:
        if self._mode is Mode.ELECTRIC:
            return self.coordinator.electric_days
        return self.coordinator.vacation_days

    @override
    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_user_days(
            self._mode, whole_days(self._mode, value)
        )
