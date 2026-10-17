"""The operating mode select."""

from __future__ import annotations

from typing import override

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import FieldName, Mode

from . import AOSmithConfigEntry
from .const import MODE_TO_OPTION, decode_mode
from .coordinator import AOSmithCoordinator
from .entity import KEY_OPERATING_MODE, AOSmithEntity, created_keys, writable_modes


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AOSmithConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator is not None and KEY_OPERATING_MODE in created_keys(coordinator):
        async_add_entities([AOSmithModeSelect(coordinator)])


class AOSmithModeSelect(AOSmithEntity, SelectEntity):
    """The writable modes in the heater's own vocabulary."""

    _attr_translation_key = KEY_OPERATING_MODE

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(coordinator, KEY_OPERATING_MODE, (FieldName.MODE,))
        self._modes: dict[str, Mode] = {
            MODE_TO_OPTION[mode]: mode
            for mode in writable_modes(coordinator)
            if mode in MODE_TO_OPTION
        }
        self._attr_options = list(self._modes)

    @property
    @override
    def current_option(self) -> str | None:
        value = self._value(FieldName.MODE)
        mode = None if value is None else decode_mode(value)
        option = None if mode is None else MODE_TO_OPTION.get(mode)
        # GUEST, an undecodable value, or a mode this heater cannot write.
        return option if option in self._modes else None

    @override
    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_mode(self._modes[option])
