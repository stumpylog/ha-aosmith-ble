"""Client construction and the shared connect-and-classify rule."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback

from aosmith_ble import (
    AOSmithBLEClient,
    ChallengeRejectedError,
    NotConnectedError,
    ProtocolError,
    SessionError,
)

from .const import CONNECT_TIMEOUT

# Failures that mean "the heater is unreachable right now", worth a retry later.
FAILURE_RETRYABLE: tuple[type[BaseException], ...] = (
    BleakError,
    TimeoutError,
    NotConnectedError,
    ProtocolError,
)


@callback
def async_register_advertisement_callback(
    hass: HomeAssistant,
    advert_callback: Callable[[BluetoothServiceInfoBleak], None],
    address: str,
) -> CALLBACK_TYPE:
    """Call advert_callback for advertisements from address.

    Uses Home Assistant's every-advertisement listener (added in 2026.10) when
    present, else the changed-advertisement fallback. Looked up per call, so the
    choice follows the running Home Assistant.
    """
    native = getattr(bluetooth, "async_register_advertisement_callback", None)
    if native is not None:
        return native(hass, advert_callback, address)
    return async_register_changed_advertisement_callback(hass, advert_callback, address)


@callback
def async_register_changed_advertisement_callback(
    hass: HomeAssistant,
    advert_callback: Callable[[BluetoothServiceInfoBleak], None],
    address: str,
) -> CALLBACK_TYPE:
    """Fallback for Home Assistant before 2026.10.

    Fires only when the advertisement differs from the last one, or when the
    heater comes back after the manager marked it unavailable. The poll and Home
    Assistant's own setup retry still recover, so an identical advertisement
    missed here only delays recovery.

    The history replay is disabled where the API allows it (the 2026.10
    listener does not replay). Where it does not, the replay arrives inside the
    register call, and every caller ignores a callback before its registration
    returned.

    PASSIVE, as the 2026.10 listener registers no active-scan request either:
    matching a connectable advertisement by address needs no scan response, and
    ACTIVE would keep an active-scan request open for the listener's lifetime.
    """

    @callback
    def _on_change(info: BluetoothServiceInfoBleak, _change: BluetoothChange) -> None:
        advert_callback(info)

    replay = getattr(bluetooth, "BluetoothCallbackReplay", None)
    kwargs = {} if replay is None else {"replay": replay.DISABLED}
    return async_register_callback(
        hass,
        _on_change,
        {"address": address, "connectable": True},
        BluetoothScanningMode.PASSIVE,
        **kwargs,
    )


class AuthFailed(Exception):
    """Two challenge rejections on two fresh links."""


def build_client(
    hass: HomeAssistant,
    address: str,
    pairing_code: str,
    asset_id: str | None,
    on_disconnect: Callable[[], None] | None,
) -> AOSmithBLEClient | None:
    """Build a client from a BLEDevice, or None when the heater is not visible."""
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        return None
    return AOSmithBLEClient(
        device=ble_device,
        pairing_code=pairing_code,
        asset_id=asset_id,
        client_class=BleakClientWithServiceCache,
        device_resolver=lambda: async_ble_device_from_address(
            hass, address, connectable=True
        ),
        on_disconnect=on_disconnect,
    )


async def connect_and_classify(
    client: AOSmithBLEClient, *, prior_rejection: bool = False
) -> None:
    """Connect under the retry rule; stateless between calls.

    A challenge rejection closes the link and connects once more; a second one (or
    a first one when prior_rejection is set) raises AuthFailed. Any other
    SessionError closes and connects once more, and a second failure propagates.
    NotPairedError and everything else propagate unchanged. A
    ChallengeRejectedError raised on the retry after a plain SessionError is only
    the first rejection, so it propagates unchanged rather than as AuthFailed.

    The caller must close/disconnect the client on TimeoutError or any propagated
    exception. This helper does not, because a timeout may cancel connect()
    mid-flight.
    """
    async with asyncio.timeout(CONNECT_TIMEOUT):
        try:
            await client.connect()
            return
        except ChallengeRejectedError as err:
            if prior_rejection:
                raise AuthFailed from err
            rejected = True
        except SessionError:
            rejected = False
        await client.disconnect()
        if not rejected:
            await client.connect()
            return
        try:
            await client.connect()
        except ChallengeRejectedError as err:
            raise AuthFailed from err
