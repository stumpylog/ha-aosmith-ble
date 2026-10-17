"""A fake AOSmithBLEClient built against the real library types."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import Any, ClassVar

from aosmith_ble import (
    DeviceInfo,
    Fault,
    Feature,
    FeatureSet,
    FieldName,
    Mode,
    Profile,
    Trust,
    UnknownDeviceError,
    UnsupportedConfigurationError,
)
from aosmith_ble.codecs import ModeWord
from aosmith_ble.profiles.bundled import BUNDLED_PROFILES

from custom_components.aosmith_ble.const import (
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_PAIRING_CODE,
)

Effects = list["Exception | None"]


def make_profile(**overrides: Any) -> Profile:
    """The bundled HPTS-50 6.3 profile under a placeholder id, with overrides."""
    base = BUNDLED_PROFILES["hpts50-6.3"]
    return dataclasses.replace(base, id="placeholder-profile", **overrides)


def make_feature_set(
    *,
    setpoint: float | None = 120.0,
    mode: ModeWord | None = None,
    fault: int = 0,
    vacation_days: int | None = 0,
    electric_days: int | None = 0,
    guest_days: int | None = None,
) -> FeatureSet:
    """A FeatureSet with the fields a poll returns. Pass None to omit a field."""
    if mode is None:
        mode = ModeWord(duration_days=0, mode=int(Mode.HEAT_PUMP))
    values: dict[FieldName, tuple[object, bool]] = {
        FieldName.SETPOINT: (setpoint, True),
        FieldName.MODE: (mode, True),
        FieldName.FAULT: (Fault(code=fault), False),
        FieldName.VACATION_DAYS_REMAINING: (vacation_days, False),
        FieldName.ELECTRIC_DAYS_REMAINING: (electric_days, False),
        FieldName.GUEST_DAYS_REMAINING: (guest_days, False),
    }
    return {
        name: Feature(id=name, value=value, trust=Trust.OK, writable=writable)
        for name, (value, writable) in values.items()
        if value is not None
    }


class FakeClient:
    """Same API and constructor kwargs as AOSmithBLEClient, scripted by effects.

    Each of connect_effects, read_effects and write_effects is consumed one entry
    per call: an Exception is raised, None (or an exhausted list) succeeds.

    Overlapping connect() calls are recorded in `overlap_detected` as well as
    raised: the integration maps every Exception, which could swallow the
    raise. An autouse fixture fails the test if any instance saw an overlap.
    """

    # Every instance built since the last reset (see the conftest guard).
    instances: ClassVar[list[FakeClient]] = []

    def __init__(
        self,
        device: Any = None,
        pairing_code: bytes | str = PLACEHOLDER_PAIRING_CODE,
        asset_id: bytes | str | None = None,
        timeout: float = 8.0,
        *,
        client_class: Any = None,
        device_resolver: Callable[[], Any] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.connect_effects: Effects = []
        self.read_effects: Effects = []
        self.write_effects: Effects = []
        self.delay: float = 0.0
        self.state: FeatureSet = make_feature_set()
        self.calls: list[tuple] = []
        self.active_connects = 0
        self.peak_connects = 0
        self.overlap_detected = False
        self.diagnose_result: Any = None
        self.diagnose_effects: Effects = []
        self._profile: Profile | None = make_profile()
        # What a session reads; the library clears device_info on disconnect.
        self._session_info = DeviceInfo(
            model="HPTS-50",
            serial="XXXXXX",
            asset_id=PLACEHOLDER_ASSET_ID,
            firmware=(6, 3),
        )
        self._info = self._session_info
        self._connected = False
        self._closed = False
        # Models the library's client lock: reads and writes hold it, connect()
        # does not, and async_close() waits for it.
        self._lock = asyncio.Lock()
        self._bind(
            device,
            pairing_code,
            asset_id,
            timeout,
            client_class,
            device_resolver,
            on_disconnect,
        )
        FakeClient.instances.append(self)

    def _bind(
        self,
        device: Any,
        pairing_code: bytes | str,
        asset_id: bytes | str | None,
        timeout: float,
        client_class: Any,
        device_resolver: Callable[[], Any] | None,
        on_disconnect: Callable[[], None] | None,
    ) -> None:
        self.device = device
        self.pairing_code = pairing_code
        self.timeout = timeout
        self.client_class = client_class
        self.device_resolver = device_resolver
        self.on_disconnect = on_disconnect
        self._asset_id = asset_id.decode() if isinstance(asset_id, bytes) else asset_id

    def rebind(self, *args: Any, **kwargs: Any) -> FakeClient:
        """Re-run the constructor binding, so one fake can stand in for the class."""
        fresh = FakeClient(*args, **kwargs)
        self._bind(
            fresh.device,
            fresh.pairing_code,
            fresh._asset_id,
            fresh.timeout,
            fresh.client_class,
            fresh.device_resolver,
            fresh.on_disconnect,
        )
        self.calls.append(("construct", args, kwargs))
        return self

    @property
    def asset_id(self) -> str | None:
        return self._asset_id

    @property
    def profile(self) -> Profile | None:
        return self._profile if self._connected else None

    @profile.setter
    def profile(self, value: Profile | None) -> None:
        self._profile = value

    @property
    def device_info(self) -> DeviceInfo:
        return self._info

    @device_info.setter
    def device_info(self, value: DeviceInfo) -> None:
        """Script the identity every later session reads."""
        self._session_info = value
        self._info = value

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _next(self, effects: Effects) -> None:
        if effects:
            effect = effects.pop(0)
            if effect is not None:
                raise effect

    async def _pause(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)

    def _refuse_if_closed(self) -> None:
        if self._closed:
            from aosmith_ble import NotConnectedError

            raise NotConnectedError("client is closed")

    async def connect(self) -> None:
        self.calls.append(("connect",))
        self._refuse_if_closed()
        self.active_connects += 1
        self.peak_connects = max(self.peak_connects, self.active_connects)
        try:
            if self.active_connects > 1:
                self.overlap_detected = True
                raise AssertionError("overlapping connect() calls")
            await self._pause()
            # The library re-checks the closed flag after each await and tears
            # down the link it just opened, so a connect() that outlasts
            # async_close() never leaves a live link.
            self._refuse_if_closed()
            # The library reads the assetID during the session, before the
            # profile match that raises these two.
            if (
                self.connect_effects
                and isinstance(
                    self.connect_effects[0],
                    UnknownDeviceError | UnsupportedConfigurationError,
                )
                and self._asset_id is None
            ):
                self._asset_id = PLACEHOLDER_ASSET_ID
            self._next(self.connect_effects)
            self._connected = True
            self._info = self._session_info
            if self._asset_id is None:
                self._asset_id = PLACEHOLDER_ASSET_ID
        finally:
            self.active_connects -= 1

    async def disconnect(self) -> None:
        self.calls.append(("disconnect",))
        self._connected = False
        # As the library does: the identity belongs to the session.
        self._info = DeviceInfo()

    async def async_close(self) -> None:
        """Mark closed first, then wait for the in-flight read or write."""
        self.calls.append(("async_close",))
        self._closed = True
        async with self._lock:
            self._connected = False
            self._info = DeviceInfo()

    async def _locked_op(self, effects: Effects) -> None:
        self._refuse_if_closed()
        async with self._lock:
            self._refuse_if_closed()
            await self._pause()
            self._next(effects)

    async def async_get_state(self, min_trust: Trust = Trust.OK) -> FeatureSet:
        self.calls.append(("async_get_state", min_trust))
        await self._locked_op(self.read_effects)
        return self.state

    async def async_set_setpoint(self, degf: float) -> None:
        self.calls.append(("async_set_setpoint", degf))
        await self._locked_op(self.write_effects)

    async def async_set_mode(self, mode: Mode, duration_days: int = 0) -> None:
        self.calls.append(("async_set_mode", mode, duration_days))
        await self._locked_op(self.write_effects)

    async def async_diagnose(self, max_block: int = 40) -> Any:
        self.calls.append(("async_diagnose", max_block))
        await self._pause()
        self._next(self.diagnose_effects)
        return self.diagnose_result

    def fire_disconnect(self) -> None:
        """Simulate a link drop: mark disconnected and call on_disconnect."""
        self._connected = False
        if self.on_disconnect is not None:
            self.on_disconnect()


class ReplayingRegister:
    """Stands in for bluetooth.async_register_callback with a synchronous replay.

    Like Home Assistant, it replays the last advertisement to the callback
    inside the register call, before returning the unsubscribe. With
    honor_replay it skips the replay when called with replay DISABLED (as the
    real API does); without it, it always replays (a release without the
    argument).
    """

    def __init__(self, *, honor_replay: bool) -> None:
        self.honor_replay = honor_replay
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.replayed = 0
        self.info = object()

    def __call__(self, *args: Any, **kwargs: Any) -> Callable[[], None]:
        from homeassistant.components.bluetooth import (
            BluetoothCallbackReplay,
            BluetoothChange,
        )

        self.calls.append((args, kwargs))
        disabled = kwargs.get("replay") is BluetoothCallbackReplay.DISABLED
        if not (self.honor_replay and disabled):
            self.replayed += 1
            args[1](self.info, BluetoothChange.ADVERTISEMENT)
        return lambda: None
