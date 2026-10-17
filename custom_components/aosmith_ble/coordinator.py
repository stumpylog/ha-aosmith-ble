"""The coordinator that owns the AOSmithBLEClient."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, NoReturn

from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from aosmith_ble import (
    AOSmithBLEClient,
    AOSmithError,
    ChallengeRejectedError,
    ConnectionSlotsExhaustedError,
    DeviceInfo,
    FeatureSet,
    FieldName,
    Mode,
    NotPairedError,
    Profile,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
    ValidationError,
)
from aosmith_ble.codecs import ModeWord

from .connection import (
    AuthFailed,
    async_register_advertisement_callback,
    connect_and_classify,
)
from .const import (
    ADVERT_DEBOUNCE_COOLDOWN,
    BACKOFF_MAX,
    CLOSE_TIMEOUT,
    CONF_ADDRESS,
    CONF_ASSET_ID,
    CONF_POLL_INTERVAL,
    DAYS_RANGE,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    PERMANENT_MODES,
    POLL_INTERVAL_MAX,
    POLL_INTERVAL_MIN,
    POLL_TIMEOUT,
    RECONNECT_DELAY,
    UNREACHABLE_THRESHOLD,
    WRITE_REFRESH_COOLDOWN,
)
from .issues import (
    ISSUE_CONNECTIVITY,
    VARIANT_NO_CONNECTION_SLOTS,
    VARIANT_UNREACHABLE,
    clear_issue,
    raise_issue,
)
from .scrub import Scrubber
from .store import StateStore, UserState

_LOGGER = logging.getLogger(__name__)

# Failure classes the poll expects; anything else is also transient but logged.
_EXPECTED_FAILURES: tuple[type[BaseException], ...] = (
    BleakError,
    AOSmithError,
    OSError,
)

# A translated refusal: (translation key, placeholders).
_Refusal = tuple[str, dict[str, str]]


def _refusal(key: str, **placeholders: Any) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


def _failure(key: str, **placeholders: Any) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={k: str(v) for k, v in placeholders.items()},
    )


def _mode_name(mode: Mode) -> str:
    return mode.name.lower()


def _days_refusal(mode: Mode, days: int) -> _Refusal | None:
    """The integration's own day range, which the library cannot know."""
    if mode not in DAYS_RANGE:
        return "mode_not_writable", {"mode": _mode_name(mode)}
    low, high = DAYS_RANGE[mode]
    if isinstance(days, bool) or not isinstance(days, int) or not low <= days <= high:
        return "invalid_duration", {"mode": _mode_name(mode), "min": low, "max": high}
    return None


def poll_interval_from(entry: ConfigEntry) -> int:
    """The configured poll interval in seconds, clamped to the allowed range."""
    value = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    try:
        seconds = int(value)
    except TypeError, ValueError:
        seconds = DEFAULT_POLL_INTERVAL
    return max(POLL_INTERVAL_MIN, min(POLL_INTERVAL_MAX, seconds))


class DropHandlerHolder:
    """The client's on_disconnect, bound to the coordinator after both exist.

    The client needs the drop handler and the coordinator needs the client, so
    the client gets this holder and the coordinator's handler is bound later. A
    drop reported before that is ignored.
    """

    def __init__(self) -> None:
        self._handler: Callable[[], None] | None = None

    def bind(self, handler: Callable[[], None]) -> None:
        self._handler = handler

    def __call__(self) -> None:
        if self._handler is not None:
            self._handler()


class AOSmithCoordinator(DataUpdateCoordinator[FeatureSet]):
    """Polls one heater and owns its client, lock and flags."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: AOSmithBLEClient,
        store: StateStore,
        user_state: UserState,
        scrubber: Scrubber,
    ) -> None:
        self.poll_interval = poll_interval_from(entry)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.poll_interval),
            # Only writes request a refresh. A write inside the cooldown waits
            # for its end, so a short one keeps the post-write state lag short.
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=WRITE_REFRESH_COOLDOWN,
                immediate=True,
            ),
        )
        self.entry = entry
        self.client = client
        self.store = store
        self.user_state = user_state
        self.scrubber = scrubber
        self.closing = False
        self.auth_failed = False
        self.drop_pending = False
        self.connecting = False
        self.io_lock = asyncio.Lock()
        self.failure_count = 0
        self.last_error: str | None = None
        self.last_error_class: str | None = None
        self.last_success: datetime | None = None
        # Kept from the last connect: the library clears both on disconnect.
        self.profile: Profile | None = None
        self.device_info = DeviceInfo()
        self._reconnect_unsub: CALLBACK_TYPE | None = None
        self._advert_unsub: CALLBACK_TYPE | None = None
        self._advert_debouncer: Debouncer[Any] = Debouncer(
            hass,
            _LOGGER,
            cooldown=ADVERT_DEBOUNCE_COOLDOWN,
            immediate=True,
            function=self._async_advert_refresh,
        )
        self._refresh_tasks: set[asyncio.Task[FeatureSet]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._unexpected_logged = False

    # Drops and advertisements

    def handle_drop(self) -> None:
        """The on_disconnect target; the library may call it from any thread."""
        if self.closing or self.auth_failed or self.connecting:
            # Checked at report time: a drop during a connect is that connect's
            # own failure, and it reports it itself.
            return
        self.hass.loop.call_soon_threadsafe(self._async_handle_drop)

    @callback
    def _async_handle_drop(self) -> None:
        if self.closing or self.auth_failed:
            return
        self.drop_pending = True
        self._cancel_reconnect()
        self._reconnect_unsub = async_call_later(
            self.hass,
            RECONNECT_DELAY,
            HassJob(self._async_reconnect, cancel_on_shutdown=True),
        )

    def _cancel_reconnect(self) -> None:
        if self._reconnect_unsub is not None:
            self._reconnect_unsub()
            self._reconnect_unsub = None

    async def _async_reconnect(self, _now: datetime) -> None:
        self._reconnect_unsub = None
        self.drop_pending = False
        # Not the debounced request, so a write's own refresh cannot delay it.
        await self.async_refresh()

    async def async_start(self) -> None:
        """Listen for advertisements from the heater."""
        if self.closing:
            return
        if self._advert_unsub is not None:
            self._advert_unsub()
            self._advert_unsub = None
        # A replay delivered inside the register call (the fallback before
        # 2026.10) arrives while _advert_unsub is None and is ignored.
        self._advert_unsub = async_register_advertisement_callback(
            self.hass, self._async_on_advertisement, self.entry.data[CONF_ADDRESS]
        )

    @callback
    def _async_on_advertisement(self, _info: BluetoothServiceInfoBleak) -> None:
        if (
            self._advert_unsub is None
            or self.closing
            or self.auth_failed
            or self.drop_pending
            or self.connecting
            or self.client.is_connected
            or self.last_update_success
        ):
            return
        self._advert_debouncer.async_schedule_call()

    async def _async_advert_refresh(self) -> None:
        await self.async_refresh()

    # Close

    async def async_close(self) -> None:
        """Close the client for good. Idempotent; every caller waits for it."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._async_close())
        await asyncio.shield(self._close_task)

    async def _async_close(self) -> None:
        self.closing = True
        self._cancel_reconnect()
        self._unschedule_refresh()
        for task in self._refresh_tasks:
            task.cancel()
        if self._advert_unsub is not None:
            self._advert_unsub()
            self._advert_unsub = None
        self._advert_debouncer.async_shutdown()
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT):
                await self.client.async_close()
        except TimeoutError:
            _LOGGER.warning(
                "Closing the heater connection timed out after %ss; disconnecting",
                CLOSE_TIMEOUT,
            )
            await self._disconnect()
        except Exception as err:
            _LOGGER.warning(
                "Closing the heater connection failed: %s; disconnecting",
                self.scrubber.scrub_exception(err),
            )
            await self._disconnect()

    async def async_shutdown(self) -> None:
        await self.async_close()
        await super().async_shutdown()

    async def _disconnect(self) -> None:
        """Plain disconnect that never masks the error being handled."""
        try:
            await self.client.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnect failed: %s", self.scrubber.scrub_exception(err))

    async def async_connect(self) -> None:
        """The setup connect: connect_and_classify under io_lock.

        Raises whatever connect_and_classify raises; the link is closed first.
        """
        async with self.io_lock:
            await self._connect()
        # The serial is known only once a session exists.
        self.scrubber.learn_serial(self.client.device_info.serial or "")

    async def _connect(self, prior_rejection: bool = False) -> None:
        """Every coordinator connect goes through here, with io_lock held."""
        self.connecting = True
        try:
            await connect_and_classify(self.client, prior_rejection=prior_rejection)
        except Exception:
            await self._disconnect()
            raise
        finally:
            self.connecting = False
        if self.client.profile is not None:
            self.profile = self.client.profile
            self.device_info = self.client.device_info

    async def _read(self) -> FeatureSet:
        if not self.client.is_connected:
            await self._connect()
        try:
            return await self.client.async_get_state()
        except SessionError as err:
            # The library may have connected implicitly inside the read, so a
            # challenge rejection here counts as the first one.
            await self._disconnect()
            await self._connect(prior_rejection=isinstance(err, ChallengeRejectedError))
            return await self.client.async_get_state()

    async def _async_update_data(self) -> FeatureSet:
        if self.closing:
            return self.data
        if self.auth_failed:
            raise UpdateFailed("authentication required")
        # The work runs in its own task so close can cancel it without
        # cancelling whoever awaits the refresh.
        task = asyncio.create_task(self._async_poll())
        self._refresh_tasks.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.closing and (current is None or not current.cancelling()):
                return self.data
            raise
        finally:
            self._refresh_tasks.discard(task)

    async def _async_poll(self) -> FeatureSet:
        async with self.io_lock:
            if self.closing:
                return self.data
            if self.auth_failed:
                raise UpdateFailed("authentication required")
            try:
                async with asyncio.timeout(POLL_TIMEOUT):
                    state = await self._read()
            except Exception as err:
                if self.closing:
                    return self.data
                # A failed read can leave a link whose session is gone (the
                # heater answers every read with a refusal), so always drop it
                # and let the next poll reconnect.
                await self._disconnect()
                await self._raise_for(err)
            try:
                await self._on_success(state)
            except Exception as err:
                _LOGGER.warning(
                    "Recording a successful poll failed: %s",
                    self.scrubber.scrub_exception(err),
                )
        return state

    async def _raise_for(self, err: Exception) -> NoReturn:
        """Map a poll failure to reauth, a reload or UpdateFailed with backoff."""
        message = self.scrubber.scrub_exception(err)
        self.last_error = message
        self.last_error_class = type(err).__name__
        if isinstance(err, AuthFailed | NotPairedError):
            self.auth_failed = True
            await self._disconnect()
            raise ConfigEntryAuthFailed(message) from None
        if isinstance(err, UnknownDeviceError | UnsupportedConfigurationError):
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            raise UpdateFailed(message) from None
        if not isinstance(err, _EXPECTED_FAILURES):
            # Once per failure run; a success re-arms it.
            if self._unexpected_logged:
                _LOGGER.debug("Unexpected error polling the heater: %s", message)
            else:
                self._unexpected_logged = True
                _LOGGER.warning("Unexpected error polling the heater: %s", message)
        self.failure_count += 1
        if self.failure_count >= UNREACHABLE_THRESHOLD:
            variant = (
                VARIANT_NO_CONNECTION_SLOTS
                if isinstance(err, ConnectionSlotsExhaustedError)
                else VARIANT_UNREACHABLE
            )
            raise_issue(self.hass, self.entry, ISSUE_CONNECTIVITY, variant)
            exponent = self.failure_count - UNREACHABLE_THRESHOLD + 1
            self.update_interval = timedelta(
                seconds=min(self.poll_interval * 2**exponent, BACKOFF_MAX)
            )
        raise UpdateFailed(message) from None

    def write_back_asset_id(self) -> None:
        """Store a changed client asset ID in the entry (no reload: no listener)."""
        asset_id = self.client.asset_id
        if asset_id and asset_id != self.entry.data.get(CONF_ASSET_ID):
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_ASSET_ID: asset_id}
            )

    async def _on_success(self, state: FeatureSet) -> None:
        self.failure_count = 0
        self._unexpected_logged = False
        self.update_interval = timedelta(seconds=self.poll_interval)
        self.last_success = dt_util.utcnow()
        clear_issue(self.hass, self.entry, ISSUE_CONNECTIVITY)
        self.write_back_asset_id()
        feature = state.get(FieldName.MODE)
        mode = feature.value if feature is not None else None
        if (
            isinstance(mode, ModeWord)
            and mode.mode in PERMANENT_MODES
            and mode.mode != self.user_state.last_permanent_mode
        ):
            self.user_state.last_permanent_mode = mode.mode
            await self.store.async_save(self.user_state)

    # Writes and user-chosen state

    @property
    def electric_days(self) -> int:
        return self.user_state.electric_days

    @property
    def vacation_days(self) -> int:
        return self.user_state.vacation_days

    @property
    def last_permanent_mode(self) -> Mode | None:
        mode = self.user_state.last_permanent_mode
        return Mode(mode) if mode in PERMANENT_MODES else None

    async def async_set_user_days(self, mode: Mode, days: int) -> None:
        """Persist the stored day count for a timed mode; never writes the heater."""
        if refusal := _days_refusal(mode, days):
            raise _refusal(refusal[0], **refusal[1])
        if mode is Mode.ELECTRIC:
            self.user_state.electric_days = days
        else:
            self.user_state.vacation_days = days
        await self.store.async_save(self.user_state)
        self.async_update_listeners()

    async def async_set_setpoint(self, degf: float) -> None:
        # A Celsius display converts to values like 119.99999.
        value = round(degf)

        def cause() -> _Refusal | None:
            profile = self.client.profile
            if profile is not None and not (
                profile.min_setpoint_f <= value <= profile.max_setpoint_f
            ):
                return "setpoint_out_of_range", {
                    "setpoint": value,
                    "min": round(profile.min_setpoint_f),
                    "max": round(profile.max_setpoint_f),
                }
            return self._fault_cause()

        await self._async_write(lambda: self.client.async_set_setpoint(value), cause)

    async def async_set_mode(self, mode: Mode) -> None:
        """Permanent modes with 0 days, timed modes with the stored day count."""
        days = 0
        if mode is Mode.ELECTRIC:
            days = self.user_state.electric_days
        elif mode is Mode.VACATION:
            days = self.user_state.vacation_days
        await self._async_write_mode(mode, days)

    async def async_set_timed_mode(self, mode: Mode, days: int) -> None:
        """The service path: an explicit day count, stored counts untouched."""
        self._refuse_if_unavailable()
        if mode is not Mode.GUEST and (refusal := _days_refusal(mode, days)):
            raise _refusal(refusal[0], **refusal[1])
        await self._async_write_mode(mode, days)

    async def _async_write_mode(self, mode: Mode, days: int) -> None:
        self._refuse_if_unavailable()
        if mode is Mode.GUEST:
            # Never written, whatever a profile says.
            raise _refusal("mode_not_writable", mode=_mode_name(mode))

        def cause() -> _Refusal | None:
            profile = self.client.profile
            if profile is not None and mode not in profile.writable_modes:
                return "mode_not_writable", {"mode": _mode_name(mode)}
            if mode in DAYS_RANGE and (refusal := _days_refusal(mode, days)):
                return refusal
            return self._fault_cause()

        await self._async_write(lambda: self.client.async_set_mode(mode, days), cause)

    def _fault_cause(self) -> _Refusal | None:
        feature = self.data.get(FieldName.FAULT) if self.data else None
        fault = feature.value if feature is not None else None
        if fault is not None and getattr(fault, "restricts_changes", False):
            return "fault_active", {}
        return None

    def _refuse_if_unavailable(self) -> None:
        if self.closing:
            raise _failure("shutting_down")
        if self.auth_failed:
            raise _failure("reauth_required")

    async def _async_write(
        self,
        write: Callable[[], Awaitable[None]],
        cause: Callable[[], _Refusal | None],
    ) -> None:
        """One library write under io_lock. Never retried, never timed out.

        A cancelled write may already have landed, so no timeout wraps it.
        """
        self._refuse_if_unavailable()
        failure: HomeAssistantError | None = None
        refresh = True
        async with self.io_lock:
            # Close or a reauth may have started while this waited for the lock.
            self._refuse_if_unavailable()
            sent = False
            try:
                if not self.client.is_connected:
                    await self._connect()
                sent = True
                await write()
            except Exception as err:
                failure, may_have_landed = await self._write_failure(err, cause)
                # A write that failed after it was sent may have landed, so the
                # entities must not keep showing the pre-write state.
                refresh = sent and may_have_landed
        if refresh and not self.closing and not self.auth_failed:
            await self.async_request_refresh()
        if failure is not None:
            raise failure

    async def _write_failure(
        self, err: Exception, cause: Callable[[], _Refusal | None]
    ) -> tuple[HomeAssistantError, bool]:
        """Map a write failure to the HomeAssistantError to raise.

        Also says whether the heater may have changed, which calls for a
        refresh. A known refusal changed nothing; a reload or a reauth
        replaces the poll anyway.
        """
        message = self.scrubber.scrub_exception(err)
        if self.closing:
            return _failure("shutting_down"), False
        if isinstance(err, AuthFailed | NotPairedError):
            self.auth_failed = True
            # Before the await: a cancel there must not leave auth_failed set
            # with no reauth flow to clear it.
            self.entry.async_start_reauth(self.hass)
            await self._disconnect()
            return _failure("reauth_required"), False
        # Before ValidationError: UnsupportedConfigurationError subclasses it.
        if isinstance(err, UnknownDeviceError | UnsupportedConfigurationError):
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)
            return _failure("write_failed", error=message), False
        if isinstance(err, ValidationError):
            if known := cause():
                return _refusal(known[0], **known[1]), False
            return _refusal("write_refused", error=message), True
        if not isinstance(err, _EXPECTED_FAILURES):
            _LOGGER.warning("Unexpected error writing to the heater: %s", message)
        return _failure("write_failed", error=message), True
