"""Scrubbed config entry diagnostics."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from homeassistant.components.bluetooth import async_last_service_info
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

import aosmith_ble
from aosmith_ble import (
    ChallengeRejectedError,
    DiagnosticReport,
    FeatureSet,
    NotPairedError,
    Profile,
)

from . import AOSmithConfigEntry
from .connection import build_client
from .const import (
    CLOSE_TIMEOUT,
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    DIAGNOSE_TIMEOUT,
)
from .coordinator import AOSmithCoordinator
from .scrub import Scrubber

_LOGGER = logging.getLogger(__name__)

TO_REDACT = {"address", "pairing_code", "asset_id", "serial", "name", "ble_address"}

BUTTON_INSTRUCTION = (
    "Press the Bluetooth button on the heater's panel once so its LED flashes "
    "(or turn Bluetooth off and on at the panel if it was already flashing), "
    "close the A. O. Smith app, then download diagnostics again."
)

RAW_ENTRY_NOTE = (
    "zeroed by the integration: raw entries carry no block number, so the "
    "identity and nameplate blocks cannot be told apart from the others"
)

# Error class reported when the heater is not visible to Bluetooth.
NOT_ADVERTISING = "not_advertising"


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AOSmithConfigEntry
) -> dict[str, Any]:
    """Diagnostics for one entry: coordinator state, or a fresh diagnose run."""
    coordinator: AOSmithCoordinator | None = getattr(entry, "runtime_data", None)
    diagnostics_only = coordinator is None and entry.state is ConfigEntryState.LOADED
    output: dict[str, Any] = {
        "entry": {"options": dict(entry.options), "state": entry.state.value},
        "library_version": getattr(aosmith_ble, "__version__", None),
        "diagnostics_only": diagnostics_only,
    }
    if coordinator is not None:
        scrubber = coordinator.scrubber
        output.update(_coordinator_state(coordinator))
    else:
        scrubber = _entry_scrubber(hass, entry)
        if diagnostics_only:
            output.update(await _async_diagnose(hass, entry, scrubber))
    return _scrub_all(async_redact_data(_plain(output), TO_REDACT), scrubber)


def _entry_scrubber(hass: HomeAssistant, entry: AOSmithConfigEntry) -> Scrubber:
    address: str = entry.data[CONF_ADDRESS]
    # Read only for its name; service info is never output.
    service_info = async_last_service_info(hass, address, connectable=True)
    return Scrubber.for_entry(
        address,
        service_info.name if service_info is not None else None,
        entry.data.get(CONF_PAIRING_CODE),
        entry.data.get(CONF_ASSET_ID),
        None,
    )


def _coordinator_state(coordinator: AOSmithCoordinator) -> dict[str, Any]:
    """Normal entries report what the coordinator already knows; no I/O."""
    info = coordinator.device_info
    return {
        "model": info.model,
        "firmware": info.firmware,
        "profile": _profile(coordinator.profile),
        "features": _features(coordinator.data),
        "health": {
            "last_success": coordinator.last_success,
            "consecutive_failures": coordinator.failure_count,
            "flags": {
                "closing": coordinator.closing,
                "auth_failed": coordinator.auth_failed,
                "drop_pending": coordinator.drop_pending,
                "connecting": coordinator.connecting,
                "connected": coordinator.client.is_connected,
                "last_update_success": coordinator.last_update_success,
            },
            "last_error_class": coordinator.last_error_class,
            "last_error": coordinator.last_error,
        },
    }


def _profile(profile: Profile | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    return {
        "id": profile.id,
        "fields": {
            name: {"read": spec.read, "write": spec.write}
            for name, spec in profile.fields
        },
    }


def _features(features: FeatureSet | None) -> dict[str, Any] | None:
    if features is None:
        return None
    return {
        name: {"value": feature.value, "trust": feature.trust}
        for name, feature in features.items()
    }


async def _async_diagnose(
    hass: HomeAssistant, entry: AOSmithConfigEntry, scrubber: Scrubber
) -> dict[str, Any]:
    """Build a client, run one async_diagnose and always async_close it."""
    client = build_client(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data[CONF_PAIRING_CODE],
        entry.data.get(CONF_ASSET_ID),
        None,
    )
    if client is None:
        return {
            "report": None,
            "error": _error(NOT_ADVERTISING, "heater not advertising", True),
        }
    failure: Exception | None = None
    try:
        async with asyncio.timeout(DIAGNOSE_TIMEOUT):
            report = await client.async_diagnose()
    except Exception as err:  # partial output on any failure
        failure = err
    finally:
        # The run may have read a fresh asset ID, which may sit under any key
        # of the report: scrub it by value, not by key name.
        scrubber.learn_asset_id(client.asset_id)
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await client.async_close()
        except Exception as err:  # closing must not fail the report
            _LOGGER.debug(
                "Closing the diagnose client failed: %s", scrubber.scrub_exception(err)
            )
    if failure is not None:
        press_button = isinstance(failure, NotPairedError | ChallengeRejectedError)
        text = scrubber.scrub_exception(failure)
        return {
            "report": None,
            "error": _error(type(failure).__name__, text, press_button),
        }
    return {"report": _report(report), "error": None}


def _error(error_class: str, text: str, press_button: bool) -> dict[str, Any]:
    return {
        "class": error_class,
        "text": text,
        "instruction": BUTTON_INSTRUCTION if press_button else None,
    }


def _report(report: DiagnosticReport) -> dict[str, Any]:
    """The report's own fields; raw frames are zeroed, model bytes become hex."""
    return {
        "model": report.model.hex() if report.model is not None else None,
        "firmware": report.firmware,
        "block_presence": report.block_presence,
        "decoded_values": report.decoded_values,
        "raw_entries": [
            {
                **entry,
                "hex": "0" * len(entry.get("hex", "")),
                "note": RAW_ENTRY_NOTE,
            }
            for entry in report.raw_entries
        ],
    }


def _plain(value: Any) -> Any:
    """A JSON-serialisable copy: enums by value, bytes as hex, dataclasses as dicts."""
    if isinstance(value, Enum):
        return _plain(value.value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(_plain(k)): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(item) for item in value]
    return repr(value)


def _scrub_all(value: Any, scrubber: Scrubber) -> Any:
    """scrub() over every string, keys included."""
    if isinstance(value, str):
        return scrubber.scrub(value)
    if isinstance(value, dict):
        return {
            _scrub_all(k, scrubber): _scrub_all(v, scrubber) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_all(item, scrubber) for item in value]
    return value
