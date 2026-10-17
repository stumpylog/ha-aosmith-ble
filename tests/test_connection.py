"""connect_and_classify and build_client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aosmith_ble import (
    ChallengeRejectedError,
    ConfigurationIssue,
    ConnectionSlotsExhaustedError,
    NotPairedError,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
)
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackReplay,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant

from custom_components.aosmith_ble import connection
from custom_components.aosmith_ble.connection import (
    AuthFailed,
    build_client,
    connect_and_classify,
)
from custom_components.aosmith_ble.const import (
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_PAIRING_CODE,
)

from .fakes import FakeClient


def _names(client: FakeClient) -> list[str]:
    return [call[0] for call in client.calls]


async def test_connects_first_try() -> None:
    client = FakeClient()
    await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client) == ["connect"]


async def test_one_challenge_rejection_then_success() -> None:
    client = FakeClient()
    client.connect_effects = [ChallengeRejectedError("rejected")]
    await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client) == ["connect", "disconnect", "connect"]


async def test_two_challenge_rejections_raise_auth_failed() -> None:
    client = FakeClient()
    client.connect_effects = [ChallengeRejectedError("1"), ChallengeRejectedError("2")]
    with pytest.raises(AuthFailed):
        await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client) == ["connect", "disconnect", "connect"]


async def test_prior_rejection_makes_first_rejection_fatal() -> None:
    client = FakeClient()
    client.connect_effects = [ChallengeRejectedError("1")]
    with pytest.raises(AuthFailed):
        await connect_and_classify(client, prior_rejection=True)  # type: ignore[arg-type]
    assert _names(client) == ["connect"]


async def test_prior_rejection_still_allows_success() -> None:
    client = FakeClient()
    await connect_and_classify(client, prior_rejection=True)  # type: ignore[arg-type]
    assert _names(client) == ["connect"]


async def test_session_error_retried_once_then_propagates() -> None:
    client = FakeClient()
    first, second = SessionError("one"), SessionError("two")
    client.connect_effects = [first, second]
    with pytest.raises(SessionError) as err:
        await connect_and_classify(client)  # type: ignore[arg-type]
    assert err.value is second
    assert _names(client) == ["connect", "disconnect", "connect"]


async def test_session_error_then_success() -> None:
    client = FakeClient()
    client.connect_effects = [SessionError("lost")]
    await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client) == ["connect", "disconnect", "connect"]


async def test_session_error_then_challenge_rejection_propagates_unchanged() -> None:
    client = FakeClient()
    rejection = ChallengeRejectedError("rejected")
    client.connect_effects = [SessionError("lost"), rejection]
    with pytest.raises(ChallengeRejectedError) as err:
        await connect_and_classify(client)  # type: ignore[arg-type]
    assert err.value is rejection
    assert _names(client) == ["connect", "disconnect", "connect"]


async def test_not_paired_propagates() -> None:
    client = FakeClient()
    client.connect_effects = [NotPairedError("no")]
    with pytest.raises(NotPairedError):
        await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client) == ["connect"]


@pytest.mark.parametrize(
    "error",
    [
        UnknownDeviceError("x"),
        UnsupportedConfigurationError("x", reason=ConfigurationIssue.UNITS),
        ConnectionSlotsExhaustedError("x"),
        TimeoutError("x"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_other_errors_propagate_unchanged(error: Exception) -> None:
    client = FakeClient()
    client.connect_effects = [error]
    with pytest.raises(type(error)) as err:
        await connect_and_classify(client)  # type: ignore[arg-type]
    assert err.value is error
    assert _names(client) == ["connect"]


async def test_overall_timeout() -> None:
    client = FakeClient()
    client.delay = 5.0
    with (
        patch("custom_components.aosmith_ble.connection.CONNECT_TIMEOUT", 0.05),
        pytest.raises(TimeoutError),
    ):
        await connect_and_classify(client)  # type: ignore[arg-type]


async def test_no_state_between_calls() -> None:
    client = FakeClient()
    client.connect_effects = [
        ChallengeRejectedError("1"),
        None,
        ChallengeRejectedError("2"),
    ]
    await connect_and_classify(client)  # type: ignore[arg-type]
    # The earlier rejection must not make this one fatal.
    await connect_and_classify(client)  # type: ignore[arg-type]
    assert _names(client).count("connect") == 4


def test_build_client_returns_none_without_ble_device(hass) -> None:
    with patch(
        "custom_components.aosmith_ble.connection.async_ble_device_from_address",
        return_value=None,
    ) as resolver:
        assert (
            build_client(
                hass, PLACEHOLDER_ADDRESS, PLACEHOLDER_PAIRING_CODE, None, None
            )
            is None
        )
    resolver.assert_called_once_with(hass, PLACEHOLDER_ADDRESS, connectable=True)


def test_build_client_passes_ble_device_not_string(
    hass, mock_client: FakeClient, mock_ble_device: MagicMock, ble_device: BLEDevice
) -> None:
    drop = MagicMock()
    client = build_client(
        hass, PLACEHOLDER_ADDRESS, PLACEHOLDER_PAIRING_CODE, PLACEHOLDER_ASSET_ID, drop
    )
    assert client is mock_client
    _, _, kwargs = mock_client.calls[-1]
    assert kwargs["device"] is ble_device
    assert isinstance(kwargs["device"], BLEDevice)
    assert kwargs["pairing_code"] == PLACEHOLDER_PAIRING_CODE
    assert kwargs["asset_id"] == PLACEHOLDER_ASSET_ID
    assert kwargs["client_class"] is BleakClientWithServiceCache
    assert kwargs["on_disconnect"] is drop
    mock_ble_device.reset_mock()
    assert kwargs["device_resolver"]() is ble_device
    mock_ble_device.assert_called_once_with(hass, PLACEHOLDER_ADDRESS, connectable=True)


def test_listener_uses_the_native_api_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, callback, unsub = MagicMock(), MagicMock(), MagicMock()
    native = MagicMock(return_value=unsub)
    fallback = MagicMock()
    monkeypatch.setattr(
        bluetooth, "async_register_advertisement_callback", native, raising=False
    )
    monkeypatch.setattr(connection, "async_register_callback", fallback)
    result = connection.async_register_advertisement_callback(
        hass, callback, PLACEHOLDER_ADDRESS
    )
    assert result is unsub
    native.assert_called_once_with(hass, callback, PLACEHOLDER_ADDRESS)
    fallback.assert_not_called()


def test_listener_falls_back_when_the_native_api_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass, unsub = MagicMock(), MagicMock()
    received: list[object] = []
    register = MagicMock(return_value=unsub)
    monkeypatch.delattr(
        bluetooth, "async_register_advertisement_callback", raising=False
    )
    monkeypatch.setattr(connection, "async_register_callback", register)
    result = connection.async_register_advertisement_callback(
        hass, received.append, PLACEHOLDER_ADDRESS
    )
    assert result is unsub
    register.assert_called_once()
    args, kwargs = register.call_args
    assert args[0] is hass
    assert args[2] == {"address": PLACEHOLDER_ADDRESS, "connectable": True}
    assert args[3] is BluetoothScanningMode.PASSIVE
    assert kwargs == {"replay": BluetoothCallbackReplay.DISABLED}
    info = object()
    args[1](info, BluetoothChange.ADVERTISEMENT)
    assert received == [info]


def test_fallback_omits_replay_where_the_api_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = MagicMock()
    monkeypatch.delattr(bluetooth, "BluetoothCallbackReplay")
    monkeypatch.setattr(connection, "async_register_callback", register)
    connection.async_register_changed_advertisement_callback(
        MagicMock(), MagicMock(), PLACEHOLDER_ADDRESS
    )
    assert register.call_args.kwargs == {}


@pytest.mark.skipif(
    hasattr(bluetooth, "async_register_advertisement_callback"),
    reason="exercises the real pre-2026.10 manager (the 2026.9 CI job)",
)
async def test_fallback_disables_the_real_managers_replay(
    hass: HomeAssistant, enable_bluetooth: None
) -> None:
    from bleak.backends.scanner import AdvertisementData
    from habluetooth import get_manager

    device = BLEDevice(PLACEHOLDER_ADDRESS, "placeholder", {})
    info = BluetoothServiceInfoBleak(
        name="placeholder",
        address=PLACEHOLDER_ADDRESS,
        rssi=-60,
        manufacturer_data={0x1234: b"x"},
        service_data={},
        service_uuids=[],
        source="local",
        device=device,
        advertisement=AdvertisementData(
            local_name="placeholder",
            manufacturer_data={0x1234: b"x"},
            service_data={},
            service_uuids=[],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        ),
        connectable=True,
        time=0.0,
        tx_power=None,
    )
    get_manager().scanner_adv_received(info)
    matcher = {"address": PLACEHOLDER_ADDRESS, "connectable": True}

    # Control: the real API replays the history inside the register call.
    replayed: list[object] = []
    unsub = bluetooth.async_register_callback(
        hass,
        lambda i, _c: replayed.append(i),
        matcher,  # type: ignore[arg-type]
        BluetoothScanningMode.PASSIVE,
    )
    unsub()
    assert replayed

    received: list[object] = []
    unsub = connection.async_register_advertisement_callback(
        hass, received.append, PLACEHOLDER_ADDRESS
    )
    unsub()
    assert received == []
