"""Config flow, reauth and options."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback

from aosmith_ble import (
    ConfigurationIssue,
    ConnectionSlotsExhaustedError,
    NotPairedError,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
    pairing_code_from_name,
)

from .connection import (
    FAILURE_RETRYABLE,
    AuthFailed,
    build_client,
    connect_and_classify,
)
from .const import (
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_PAIRING_CODE,
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    POLL_INTERVAL_MAX,
    POLL_INTERVAL_MIN,
)
from .scrub import Scrubber

_LOGGER = logging.getLogger(__name__)

NAME_PREFIX = "iCOMM-"
_PAIRING_CODE_DIGITS = 6


def has_pairing_code(name: str) -> bool:
    """True when the name holds enough digits for pairing_code_from_name."""
    return sum(ch.isdigit() for ch in name) >= _PAIRING_CODE_DIGITS


def _is_supported(name: str | None) -> bool:
    return bool(name) and name.startswith(NAME_PREFIX) and has_pairing_code(name)


def entry_title(model: str | None) -> str:
    """Never the advertised name, which embeds the serial."""
    return f"A. O. Smith {model}" if model else "A. O. Smith water heater"


@dataclass(frozen=True)
class PairResult:
    asset_id: str
    model: str | None
    firmware: tuple[int, int] | None


class PairError(Exception):
    """A pairing failure, carrying its form error key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


_CONFIGURATION_ERRORS: dict[object, str] = {
    ConfigurationIssue.UNITS: "unsupported_units",
    ConfigurationIssue.MTU: "unsupported_mtu",
}


def _error_key(err: Exception) -> str:
    """Map a connect failure to its form error key."""
    if isinstance(err, NotPairedError):
        return "not_paired"
    if isinstance(err, ConnectionSlotsExhaustedError):
        return "no_connection_slots"
    # AuthFailed, or a raw ChallengeRejectedError on the retry after a lost
    # session, or a second lost session.
    if isinstance(err, AuthFailed | SessionError):
        return "session_rejected"
    if isinstance(err, UnsupportedConfigurationError):
        # A reason this release does not know yet still gets a translated error.
        return _CONFIGURATION_ERRORS.get(err.reason, "unsupported_configuration")
    if isinstance(err, FAILURE_RETRYABLE):
        return "cannot_connect"
    return "unknown"


class AOSmithConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discovery, picker, pairing and reauth for one heater."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._candidates: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AOSmithOptionsFlow:
        return AOSmithOptionsFlow()

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        if not _is_supported(discovery_info.name):
            return self.async_abort(reason="not_supported")
        self._address = discovery_info.address
        self._name = discovery_info.name
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            self._address = address
            self._name = self._candidates[address]
            return await self.async_step_confirm()

        configured = self._async_current_ids(include_ignore=False)
        self._candidates = {
            info.address: info.name
            for info in async_discovered_service_info(self.hass, connectable=True)
            if info.address not in configured and _is_supported(info.name)
        }
        if not self._candidates:
            return self.async_abort(reason="no_devices_found")
        # Addresses only: the advertised name embeds the serial.
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS): vol.In(sorted(self._candidates))}
            ),
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._address is not None
        assert self._name is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            pairing_code = pairing_code_from_name(self._name).decode()
            try:
                result = await self._pair(
                    self._address, pairing_code, None, name=self._name
                )
            except PairError as err:
                errors["base"] = err.key
            else:
                return self.async_create_entry(
                    title=entry_title(result.model),
                    data={
                        CONF_ADDRESS: self._address,
                        CONF_PAIRING_CODE: pairing_code,
                        CONF_ASSET_ID: result.asset_id,
                    },
                )
        return self.async_show_form(step_id="confirm", errors=errors)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            address: str = entry.data[CONF_ADDRESS]
            info = async_last_service_info(self.hass, address, connectable=True)
            try:
                # The populated slot hands out a fresh assetID.
                result = await self._pair(
                    address,
                    entry.data[CONF_PAIRING_CODE],
                    entry.data.get(CONF_ASSET_ID),
                    name=info.name if info is not None else None,
                )
            except PairError as err:
                errors["base"] = err.key
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_ASSET_ID: result.asset_id}
                )
        return self.async_show_form(step_id="reauth_confirm", errors=errors)

    async def _pair(
        self,
        address: str,
        pairing_code: str,
        asset_id: str | None,
        *,
        name: str | None,
    ) -> PairResult:
        """Connect once and read the identity; the client is always closed."""
        scrubber = Scrubber.for_entry(address, name, pairing_code, asset_id, None)
        client = build_client(self.hass, address, pairing_code, asset_id, None)
        if client is None:
            raise PairError("cannot_connect")
        try:
            await connect_and_classify(client)
            info = client.device_info
            model, firmware = info.model, info.firmware
        except UnknownDeviceError as err:
            # The session succeeded before the profile match.
            model, firmware = err.model, err.firmware
        except Exception as err:
            key = _error_key(err)
            if key == "unknown":
                _LOGGER.warning(
                    "Unexpected error while pairing: %s",
                    scrubber.scrub_exception(err),
                )
            else:
                _LOGGER.debug("Pairing failed: %s", scrubber.scrub_exception(err))
            raise PairError(key) from None
        finally:
            await client.async_close()
        if client.asset_id is None:
            _LOGGER.warning("Pairing succeeded without an asset ID")
            raise PairError("unknown")
        return PairResult(client.asset_id, model, firmware)


class AOSmithOptionsFlow(OptionsFlowWithReload):
    """Poll interval; saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=POLL_INTERVAL_MIN, max=POLL_INTERVAL_MAX),
                    )
                }
            ),
        )
