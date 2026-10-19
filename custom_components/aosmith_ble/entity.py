"""The base entity and the profile-driven creation rules."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from aosmith_ble import FeatureSet, FieldName, Mode, Trust
from aosmith_ble.profiles import trust_at_least

from .const import CONF_ADDRESS, DAYS_RANGE, DOMAIN, PERMANENT_MODES
from .coordinator import AOSmithCoordinator

KEY_WATER_HEATER = "water_heater"
KEY_OPERATING_MODE = "operating_mode"
KEY_FAULT = "fault"
KEY_PROBLEM = "problem"
KEY_ELECTRIC_DAYS_REMAINING = "electric_days_remaining"
KEY_VACATION_DAYS_REMAINING = "vacation_days_remaining"
KEY_ELECTRIC_DAYS = "electric_days"
KEY_VACATION_DAYS = "vacation_days"
KEY_POWER = "power"
KEY_ENERGY = "energy"
KEY_HOT_WATER_AVAILABILITY = "hot_water_availability"


def _data(coordinator: AOSmithCoordinator) -> FeatureSet:
    return coordinator.data or {}


def is_readable(
    coordinator: AOSmithCoordinator, name: FieldName, min_trust: Trust = Trust.OK
) -> bool:
    """The field is in the FeatureSet with read trust at least min_trust.

    The poll reads down to trust PARTIAL, so an entity that needs OK must ask
    for it here rather than rely on the field being absent.
    """
    feature = _data(coordinator).get(name)
    return feature is not None and trust_at_least(feature.trust, min_trust)


def is_writable(coordinator: AOSmithCoordinator, name: FieldName) -> bool:
    """The matched profile grades the field's write trust OK."""
    profile = coordinator.profile
    if profile is None:
        return False
    spec = profile.field_map.get(name)
    return spec is not None and spec.write is Trust.OK


def writable_modes(coordinator: AOSmithCoordinator) -> tuple[Mode, ...]:
    """The profile's writable modes, in profile order. GUEST is never written."""
    profile = coordinator.profile
    if profile is None or not is_writable(coordinator, FieldName.MODE):
        return ()
    return tuple(
        Mode(mode) for mode in profile.writable_modes if Mode(mode) is not Mode.GUEST
    )


def writable_permanent_modes(coordinator: AOSmithCoordinator) -> tuple[Mode, ...]:
    return tuple(m for m in writable_modes(coordinator) if int(m) in PERMANENT_MODES)


def created_keys(coordinator: AOSmithCoordinator) -> set[str]:
    """The entity keys this heater gets, from the first FeatureSet and profile."""
    modes = writable_modes(coordinator)
    keys: set[str] = set()
    if modes or is_writable(coordinator, FieldName.SETPOINT):
        keys.add(KEY_WATER_HEATER)
    if modes:
        keys.add(KEY_OPERATING_MODE)
    if is_readable(coordinator, FieldName.FAULT):
        keys |= {KEY_FAULT, KEY_PROBLEM}
    if is_readable(coordinator, FieldName.ELECTRIC_DAYS_REMAINING):
        keys.add(KEY_ELECTRIC_DAYS_REMAINING)
    if is_readable(coordinator, FieldName.VACATION_DAYS_REMAINING):
        keys.add(KEY_VACATION_DAYS_REMAINING)
    if is_readable(coordinator, FieldName.POWER_W):
        keys.add(KEY_POWER)
    if is_readable(coordinator, FieldName.ENERGY_WH):
        keys.add(KEY_ENERGY)
    if is_readable(coordinator, FieldName.HOT_WATER_AVAILABILITY, Trust.PARTIAL):
        keys.add(KEY_HOT_WATER_AVAILABILITY)
    if Mode.ELECTRIC in modes:
        keys.add(KEY_ELECTRIC_DAYS)
    if Mode.VACATION in modes:
        keys.add(KEY_VACATION_DAYS)
    return keys


def unique_id(coordinator: AOSmithCoordinator, key: str) -> str:
    return f"{coordinator.entry.data[CONF_ADDRESS]}_{key}"


def created_unique_ids(coordinator: AOSmithCoordinator) -> set[str]:
    """Unique IDs setup creates; every other registry entry is stale."""
    return {unique_id(coordinator, key) for key in created_keys(coordinator)}


def whole_days(mode: Mode, value: float) -> int:
    """A day count as int. Home Assistant passes floats; fractions are refused."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    low, high = DAYS_RANGE.get(mode, (0, 0))
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_duration",
        translation_placeholders={
            "mode": mode.name.lower(),
            "min": str(low),
            "max": str(high),
        },
    )


def device_info(coordinator: AOSmithCoordinator) -> DeviceInfo:
    """Device registry info. Never the advertised name: it embeds the serial."""
    info = coordinator.device_info
    firmware = (
        f"{info.firmware[0]}.{info.firmware[1]}" if info.firmware is not None else None
    )
    named = info.model is not None
    return DeviceInfo(
        connections={(CONNECTION_BLUETOOTH, coordinator.entry.data[CONF_ADDRESS])},
        manufacturer="A. O. Smith",
        model=info.model,
        serial_number=info.serial,
        sw_version=firmware,
        translation_key="water_heater" if named else "water_heater_unknown_model",
        translation_placeholders={"model": info.model} if named else None,
    )


class AOSmithEntity(CoordinatorEntity[AOSmithCoordinator]):
    """One heater entity: unique ID, device info, availability per field."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AOSmithCoordinator,
        key: str,
        fields: Iterable[FieldName] = (),
        min_trust: Trust = Trust.OK,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id(coordinator, key)
        self._attr_device_info = device_info(coordinator)
        # Only the fields present at creation: a later FeatureSet missing one
        # makes the entity unavailable instead of raising KeyError.
        self._fields = tuple(
            f for f in fields if is_readable(coordinator, f, min_trust)
        )

    @property
    def available(self) -> bool:
        data = _data(self.coordinator)
        return super().available and all(f in data for f in self._fields)

    def _value(self, name: FieldName) -> object:
        feature = _data(self.coordinator).get(name)
        return feature.value if feature is not None else None
