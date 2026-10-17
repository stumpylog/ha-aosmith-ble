"""The A. O. Smith BLE integration: setup, unload and removal."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_last_service_info,
    async_process_advertisements,
)
from homeassistant.components.water_heater import DOMAIN as WATER_HEATER_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import service
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from aosmith_ble import (
    AOSmithBLEClient,
    ConnectionSlotsExhaustedError,
    NotPairedError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
)

from .connection import (
    AuthFailed,
    async_register_advertisement_callback,
    build_client,
)
from .const import (
    BACKOFF_MAX,
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    DOMAIN,
    PLATFORMS,
    RECONNECT_DELAY,
    SETUP_ADVERT_WAIT,
)
from .coordinator import AOSmithCoordinator, DropHandlerHolder
from .entity import created_unique_ids
from .issues import (
    ISSUE_CONNECTIVITY,
    ISSUE_UNSUPPORTED_CONFIGURATION,
    ISSUE_UNSUPPORTED_DEVICE,
    VARIANT_NO_CONNECTION_SLOTS,
    VARIANT_NOT_ADVERTISING,
    clear_issue,
    configuration_variant,
    delete_all_issues,
    raise_issue,
)
from .scrub import Scrubber
from .store import StateStore

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# runtime_data is None in diagnostics-only mode.
type AOSmithConfigEntry = ConfigEntry[AOSmithCoordinator | None]


class _RetryReload:
    """One-shot: the first advertisement after a not-ready setup reloads the entry.

    Not registered through entry.async_on_unload: Home Assistant runs those as
    soon as a setup raises ConfigEntryNotReady, which would remove the listener
    before it could fire. It removes itself instead, on its advertisement or
    when the entry leaves SETUP_RETRY (unloading a SETUP_RETRY entry does not
    call async_unload_entry).

    Advertisements within `holdoff` seconds of arming are ignored. A heater
    that advertises while connects fail (slots exhausted, a flaky proxy) would
    otherwise reload on every advertisement; the holdoff doubles per
    consecutive failed setup, capped at BACKOFF_MAX.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, failures: int) -> None:
        self._hass = hass
        self._entry = entry
        self.holdoff = retry_holdoff(failures)
        self._armed_at = hass.loop.time()
        # Set before registering: a fallback listener (before 2026.10) may
        # replay the last advertisement inside the register call, which
        # _async_on_advert ignores while _unsub_advert is still None.
        self._unsub_advert: CALLBACK_TYPE | None = None
        self._unsub_state: CALLBACK_TYPE | None = None
        self._unsub_advert = async_register_advertisement_callback(
            hass, self._async_on_advert, entry.data[CONF_ADDRESS]
        )
        self._unsub_state = entry.async_on_state_change(self._async_on_state)

    @callback
    def cancel(self) -> None:
        if self._unsub_advert is not None:
            self._unsub_advert()
            self._unsub_advert = None
        if self._unsub_state is not None:
            # Home Assistant may be iterating its state listeners right now.
            self._hass.loop.call_soon(self._unsub_state)
            self._unsub_state = None

    @callback
    def _async_on_state(self) -> None:
        if self._entry.state is not ConfigEntryState.SETUP_RETRY:
            self.cancel()

    @callback
    def _async_on_advert(self, _info: BluetoothServiceInfoBleak) -> None:
        if self._unsub_advert is None:
            return
        if self._hass.loop.time() - self._armed_at < self.holdoff:
            return
        self.cancel()
        if self._entry.state is ConfigEntryState.SETUP_RETRY:
            self._hass.config_entries.async_schedule_reload(self._entry.entry_id)


def retry_holdoff(failures: int) -> float:
    """Seconds the reload listener ignores advertisements after failure N."""
    return min(RECONNECT_DELAY * 2 ** max(failures - 1, 0), BACKOFF_MAX)


@dataclass
class _SetupState:
    """Per-entry state that outlives one setup attempt."""

    failures: int = 0
    retry: _RetryReload | None = None


_DATA: HassKey[dict[str, _SetupState]] = HassKey(DOMAIN)


def _setup_state(hass: HomeAssistant, entry: ConfigEntry) -> _SetupState:
    return hass.data.setdefault(_DATA, {}).setdefault(entry.entry_id, _SetupState())


SERVICE_SET_TIMED_MODE = "set_timed_mode"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the set_timed_mode entity service."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_TIMED_MODE,
        entity_domain=WATER_HEATER_DOMAIN,
        schema={
            vol.Required("mode"): vol.In(["electric", "vacation"]),
            # The range per mode is checked by the coordinator; fractions are
            # refused by the entity (whole_days).
            vol.Required("days"): vol.Coerce(float),
        },
        func="async_set_timed_mode",
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: AOSmithConfigEntry) -> bool:
    """Set up one heater, scheduling a retry reload when it is not ready."""
    state = _setup_state(hass, entry)
    if state.retry is not None:
        state.retry.cancel()
        state.retry = None
    try:
        await _async_setup_entry(hass, entry, state)
    except ConfigEntryNotReady:
        state.failures += 1
        state.retry = _RetryReload(hass, entry, state.failures)
        raise
    except BaseException:
        state.failures += 1
        raise
    state.failures = 0
    return True


async def _async_setup_entry(
    hass: HomeAssistant, entry: AOSmithConfigEntry, state: _SetupState
) -> None:
    # A reauth flow is the only central allowed; a new coordinator would compete.
    if any(entry.async_get_active_flows(hass, {SOURCE_REAUTH})):
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="reauth_in_progress"
        )

    address: str = entry.data[CONF_ADDRESS]
    store = StateStore(hass, entry.entry_id)
    user_state = await store.async_load()
    holder = DropHandlerHolder()
    client = _build(hass, entry, holder)
    if client is None:
        # After a long held link the manager has forgotten the heater, which
        # advertises again only moments after unload.
        await _async_wait_for_advertisement(hass, address)
        client = _build(hass, entry, holder)
    if client is None:
        if state.failures >= 1:
            raise_issue(hass, entry, ISSUE_CONNECTIVITY, VARIANT_NOT_ADVERTISING)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="not_advertising"
        )

    service_info = async_last_service_info(hass, address, connectable=True)
    scrubber = Scrubber.for_entry(
        address,
        service_info.name if service_info is not None else None,
        entry.data[CONF_PAIRING_CODE],
        entry.data.get(CONF_ASSET_ID),
        None,
    )
    coordinator = AOSmithCoordinator(hass, entry, client, store, user_state, scrubber)
    holder.bind(coordinator.handle_drop)

    # Registered before connecting: Home Assistant does not call
    # async_unload_entry for a failed setup, but it does run these.
    entry.async_on_unload(coordinator.async_close)

    async def _async_on_stop(_event: Event) -> None:
        await coordinator.async_close()

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    )

    try:
        await coordinator.async_connect()
    except BaseException as err:
        await coordinator.async_close()
        if isinstance(err, UnknownDeviceError):
            _enter_diagnostics_only(hass, entry, err)
            return
        if isinstance(err, Exception):
            raise _setup_error(hass, entry, scrubber, err) from None
        raise

    try:
        for key in (
            ISSUE_CONNECTIVITY,
            ISSUE_UNSUPPORTED_CONFIGURATION,
            ISSUE_UNSUPPORTED_DEVICE,
        ):
            clear_issue(hass, entry, key)
        coordinator.write_back_asset_id()
        await coordinator.async_config_entry_first_refresh()
        remove_stale_entities(hass, entry, created_unique_ids(coordinator))
        # Platforms read runtime_data while they are forwarded.
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await coordinator.async_start()
    except BaseException:
        # Never leave runtime_data pointing at a closed coordinator.
        entry.runtime_data = None
        # A link left open would block every retry.
        await coordinator.async_close()
        raise


def _build(
    hass: HomeAssistant, entry: ConfigEntry, holder: DropHandlerHolder
) -> AOSmithBLEClient | None:
    return build_client(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data[CONF_PAIRING_CODE],
        entry.data.get(CONF_ASSET_ID),
        holder,
    )


async def _async_wait_for_advertisement(hass: HomeAssistant, address: str) -> None:
    matcher: BluetoothCallbackMatcher = {"address": address, "connectable": True}
    try:
        await async_process_advertisements(
            hass,
            lambda _info: True,
            matcher,
            BluetoothScanningMode.ACTIVE,
            SETUP_ADVERT_WAIT,
        )
    except TimeoutError:
        _LOGGER.debug("No advertisement within %ss", SETUP_ADVERT_WAIT)


def _setup_error(
    hass: HomeAssistant, entry: ConfigEntry, scrubber: Scrubber, err: Exception
) -> Exception:
    """Map a setup connect failure to the exception to raise, raising any issue."""
    if isinstance(err, AuthFailed | NotPairedError):
        return ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        )
    if isinstance(err, ConnectionSlotsExhaustedError):
        raise_issue(hass, entry, ISSUE_CONNECTIVITY, VARIANT_NO_CONNECTION_SLOTS)
        return ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="no_connection_slots"
        )
    if isinstance(err, UnsupportedConfigurationError):
        variant = configuration_variant(err.reason)
        raise_issue(hass, entry, ISSUE_UNSUPPORTED_CONFIGURATION, variant)
        return ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key=f"unsupported_configuration_{variant}",
        )
    # bleak, TimeoutError (CONNECT_TIMEOUT), NotConnectedError, ProtocolError, a
    # raw ChallengeRejectedError after a lost session, and anything else.
    return ConfigEntryNotReady(
        translation_domain=DOMAIN,
        translation_key="cannot_connect",
        translation_placeholders={"error": scrubber.scrub_exception(err)},
    )


def _enter_diagnostics_only(
    hass: HomeAssistant, entry: AOSmithConfigEntry, err: UnknownDeviceError
) -> None:
    """Diagnostics-only mode: loaded, no platforms, link closed, issue raised."""
    clear_issue(hass, entry, ISSUE_CONNECTIVITY)
    clear_issue(hass, entry, ISSUE_UNSUPPORTED_CONFIGURATION)
    firmware = (
        f"{err.firmware[0]}.{err.firmware[1]}"
        if err.firmware is not None
        else "unknown"
    )
    raise_issue(
        hass,
        entry,
        ISSUE_UNSUPPORTED_DEVICE,
        None,
        model=err.model or "unknown",
        firmware=firmware,
    )
    entry.runtime_data = None


def remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, keep: set[str]
) -> None:
    """Remove registry entries of this entry whose unique ID is not kept."""
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id not in keep:
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, entry: AOSmithConfigEntry) -> bool:
    """Unload platforms and release the link before returning."""
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None:
        return True
    try:
        return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    finally:
        # Always release the link, so the heater advertises again.
        await coordinator.async_close()


async def async_remove_entry(hass: HomeAssistant, entry: AOSmithConfigEntry) -> None:
    """Delete the entry's Store and every issue it raised."""
    state = hass.data.get(_DATA, {}).pop(entry.entry_id, None)
    if state is not None and state.retry is not None:
        state.retry.cancel()
    await StateStore(hass, entry.entry_id).async_remove()
    delete_all_issues(hass, entry)
