"""Constants for the A. O. Smith BLE integration."""

from __future__ import annotations

import logging
from typing import Final

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_ELECTRIC,
    STATE_HEAT_PUMP,
    STATE_HIGH_DEMAND,
)
from homeassistant.const import STATE_OFF, Platform

from aosmith_ble.codecs import ModeWord
from aosmith_ble.const import Mode

_LOGGER = logging.getLogger(__name__)

DOMAIN: Final = "aosmith_ble"

# Config entry data and options keys.
CONF_ADDRESS: Final = "address"
CONF_PAIRING_CODE: Final = "pairing_code"
CONF_ASSET_ID: Final = "asset_id"
CONF_POLL_INTERVAL: Final = "poll_interval"

# Placeholder identifiers, safe for tests, fixtures and documentation.
PLACEHOLDER_ADDRESS: Final = "AA:BB:CC:DD:EE:FF"
PLACEHOLDER_NAME: Final = "iCOMM-AC000W037XXXXXX"
PLACEHOLDER_ASSET_ID: Final = "02iQk000000EXAMPLE"
PLACEHOLDER_PAIRING_CODE: Final = "XXXXXX"

# Timing and range constants. All durations are in seconds.
DEFAULT_POLL_INTERVAL: Final = 60
POLL_INTERVAL_MIN: Final = 15
POLL_INTERVAL_MAX: Final = 300
RECONNECT_DELAY: Final = 10
SETUP_ADVERT_WAIT: Final = 15
BACKOFF_MAX: Final = 600
ADVERT_DEBOUNCE_COOLDOWN: Final = 5
# Cooldown of the refresh a write requests (Home Assistant's default is 10 s).
WRITE_REFRESH_COOLDOWN: Final = 1
CONNECT_TIMEOUT: Final = 90
POLL_TIMEOUT: Final = 120
CLOSE_TIMEOUT: Final = 30
DIAGNOSE_TIMEOUT: Final = 120
UNREACHABLE_THRESHOLD: Final = 3

ELECTRIC_DAYS_MIN: Final = 1
ELECTRIC_DAYS_MAX: Final = 7
DEFAULT_ELECTRIC_DAYS: Final = 3
VACATION_DAYS_MIN: Final = 1
VACATION_DAYS_MAX: Final = 99
DEFAULT_VACATION_DAYS: Final = 7

# The user-chosen day range per timed mode the integration writes.
DAYS_RANGE: Final[dict[Mode, tuple[int, int]]] = {
    Mode.ELECTRIC: (ELECTRIC_DAYS_MIN, ELECTRIC_DAYS_MAX),
    Mode.VACATION: (VACATION_DAYS_MIN, VACATION_DAYS_MAX),
}

# Modes a timed mode reverts to; tracked as last_permanent_mode.
PERMANENT_MODES: Final = frozenset({int(Mode.HYBRID), int(Mode.HEAT_PUMP)})

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.WATER_HEATER,
]

# Mode mapping, matching the core cloud aosmith integration.
MODE_TO_OPERATION: Final[dict[Mode, str]] = {
    Mode.HYBRID: STATE_ECO,
    Mode.HEAT_PUMP: STATE_HEAT_PUMP,
    Mode.ELECTRIC: STATE_ELECTRIC,
    Mode.VACATION: STATE_OFF,
    # Display only: GUEST is never written.
    Mode.GUEST: STATE_HIGH_DEMAND,
}
MODE_TO_OPTION: Final[dict[Mode, str]] = {
    Mode.HYBRID: "hybrid",
    Mode.HEAT_PUMP: "heat_pump",
    Mode.ELECTRIC: "electric",
    Mode.VACATION: "vacation",
}

# Mode values already reported as undecodable, so each is logged once.
_UNDECODABLE_LOGGED: set[object] = set()


def decode_mode(value: object) -> Mode | None:
    """The Mode in a mode field's ModeWord, or None for anything else."""
    raw: object = value
    if isinstance(value, ModeWord):
        try:
            return Mode(value.mode)
        except ValueError:
            raw = value.mode
    key = raw if isinstance(raw, int | str) else type(raw).__name__
    if key not in _UNDECODABLE_LOGGED:
        _UNDECODABLE_LOGGED.add(key)
        _LOGGER.debug("Undecodable heater mode %r", raw)
    return None


# Translation key per fault code in aosmith_ble.const.FAULT_CODES.
FAULT_KEYS: Final[dict[int, str]] = {
    1: "dry_fire",
    2: "high_water_temperature",
    3: "upper_thermistor_sensor_failure",
    4: "lower_thermistor_sensor_failure",
    6: "internal_processor_error",
    9: "power_supply_voltage_error",
    21: "upper_element_circuit_failure",
    22: "lower_element_circuit_failure",
    25: "coil_temperature_sensor_failure",
    26: "suction_temperature_sensor_failure",
    27: "discharge_temperature_sensor_failure",
    28: "ambient_temperature_sensor_failure",
    29: "outlet_temperature_sensor_failure",
    31: "water_leak",
    44: "anode_is_depleted",
    46: "water_shut_off_valve_error",
    47: "smart_valve_failure",
    48: "battery_low_br2032",
    80: "air_filter_is_dirty",
    81: "condensate_management_error",
    83: "compressor_low_pressure",
    84: "compressor_error",
    85: "high_discharge_temperature",
    86: "fan_error",
    101: "upper_thermostat_error",
    102: "lower_thermostat_error",
}
