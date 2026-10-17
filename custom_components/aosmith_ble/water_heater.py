"""The water heater entity."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from aosmith_ble import FieldName, Mode

from . import AOSmithConfigEntry
from .const import DOMAIN, MODE_TO_OPERATION, decode_mode
from .coordinator import AOSmithCoordinator
from .entity import (
    KEY_WATER_HEATER,
    AOSmithEntity,
    created_keys,
    is_writable,
    whole_days,
    writable_modes,
    writable_permanent_modes,
)

# The set_timed_mode service's mode field.
TIMED_MODES: dict[str, Mode] = {"electric": Mode.ELECTRIC, "vacation": Mode.VACATION}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AOSmithConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator is not None and KEY_WATER_HEATER in created_keys(coordinator):
        async_add_entities([AOSmithWaterHeater(coordinator)])


class AOSmithWaterHeater(AOSmithEntity, WaterHeaterEntity):
    """Setpoint, operating mode and away (Vacation) mode."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1
    _attr_precision = PRECISION_WHOLE
    _attr_current_temperature = None

    def __init__(self, coordinator: AOSmithCoordinator) -> None:
        super().__init__(
            coordinator, KEY_WATER_HEATER, (FieldName.SETPOINT, FieldName.MODE)
        )
        profile = coordinator.profile
        assert profile is not None
        self._attr_min_temp = profile.min_setpoint_f
        self._attr_max_temp = profile.max_setpoint_f
        modes = writable_modes(coordinator)
        self._operations = {
            MODE_TO_OPERATION[mode]: mode for mode in modes if mode is not Mode.VACATION
        }
        self._attr_operation_list = list(self._operations)
        features = WaterHeaterEntityFeature(0)
        if is_writable(coordinator, FieldName.SETPOINT):
            features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if len(self._operations) > 1:
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        if Mode.VACATION in modes:
            features |= WaterHeaterEntityFeature.AWAY_MODE
        self._attr_supported_features = features

    def _mode(self) -> Mode | None:
        value = self._value(FieldName.MODE)
        return None if value is None else decode_mode(value)

    @property
    @override
    def target_temperature(self) -> float | None:
        value = self._value(FieldName.SETPOINT)
        return float(value) if isinstance(value, int | float) else None

    @property
    @override
    def current_operation(self) -> str | None:
        mode = self._mode()
        return None if mode is None else MODE_TO_OPERATION.get(mode)

    @property
    @override
    def is_away_mode_on(self) -> bool | None:
        mode = self._mode()
        return None if mode is None else mode is Mode.VACATION

    @override
    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_setpoint(temperature)

    @override
    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self.coordinator.async_set_mode(self._operations[operation_mode])

    @override
    async def async_turn_away_mode_on(self) -> None:
        await self.coordinator.async_set_mode(Mode.VACATION)

    @override
    async def async_turn_away_mode_off(self) -> None:
        if self._mode() is not Mode.VACATION:
            return
        permanent = writable_permanent_modes(self.coordinator)
        last = self.coordinator.last_permanent_mode
        if last in permanent:
            target = last
        elif Mode.HYBRID in permanent:
            target = Mode.HYBRID
        elif permanent:
            target = permanent[0]
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_permanent_mode"
            )
        await self.coordinator.async_set_mode(target)

    async def async_set_timed_mode(self, mode: str, days: float) -> None:
        """The aosmith_ble.set_timed_mode entity service."""
        timed = TIMED_MODES[mode]
        await self.coordinator.async_set_timed_mode(timed, whole_days(timed, days))
