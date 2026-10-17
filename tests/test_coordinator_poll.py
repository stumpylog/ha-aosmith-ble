"""Coordinator poll, backoff and connectivity issue."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from aosmith_ble import (
    ChallengeRejectedError,
    ConfigurationIssue,
    ConnectionSlotsExhaustedError,
    Mode,
    NotConnectedError,
    NotPairedError,
    ProtocolError,
    ReadRefusedError,
    SessionError,
    UnknownDeviceError,
    UnsupportedConfigurationError,
    ValidationError,
)
from aosmith_ble.codecs import ModeWord
from bleak.exc import BleakError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble.connection import AuthFailed
from custom_components.aosmith_ble.const import (
    CONF_ASSET_ID,
    CONF_POLL_INTERVAL,
    DOMAIN,
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)
from custom_components.aosmith_ble.coordinator import AOSmithCoordinator
from custom_components.aosmith_ble.issues import ISSUE_CONNECTIVITY, raise_issue
from custom_components.aosmith_ble.scrub import Scrubber
from custom_components.aosmith_ble.store import StateStore, UserState

from .fakes import FakeClient, make_feature_set
from .test_repairs import assert_issue_translated

# A non-placeholder token the scrubber is told about, so scrubbing is observable.
SECRET = "SECRETTOKEN42"
CHANGED_ASSET_ID = "02iQk000000CHANGED"


def _build(
    hass: HomeAssistant, entry: MockConfigEntry, client: FakeClient | None = None
) -> AOSmithCoordinator:
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries()}:
        entry.add_to_hass(hass)
    scrubber = Scrubber.for_entry(
        PLACEHOLDER_ADDRESS, PLACEHOLDER_NAME, PLACEHOLDER_PAIRING_CODE, SECRET, None
    )
    return AOSmithCoordinator(
        hass,
        entry,
        client or FakeClient(asset_id=PLACEHOLDER_ASSET_ID),
        StateStore(hass, entry.entry_id),
        UserState(),
        scrubber,
    )


@pytest.fixture
def client() -> FakeClient:
    return FakeClient(asset_id=PLACEHOLDER_ASSET_ID)


@pytest.fixture
def coordinator(
    hass: HomeAssistant, config_entry: MockConfigEntry, client: FakeClient
) -> AOSmithCoordinator:
    return _build(hass, config_entry, client)


def _names(client: FakeClient) -> list[str]:
    return [call[0] for call in client.calls]


def _connectivity(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_CONNECTIVITY}_{entry.entry_id}"
    )


async def _fail(coordinator: AOSmithCoordinator, client: FakeClient, err: Exception):
    client.connect_effects = [err]
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_poll_connects_when_disconnected(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    data = await coordinator._async_update_data()
    assert data == client.state
    assert _names(client) == ["connect", "async_get_state"]


async def test_poll_connected_reads_only(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client._connected = True
    await coordinator._async_update_data()
    assert _names(client) == ["async_get_state"]
    assert coordinator.connecting is False


async def test_success_resets_counter_backoff_and_issue(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
) -> None:
    coordinator.failure_count = 5
    coordinator.update_interval = timedelta(seconds=600)
    raise_issue(hass, config_entry, ISSUE_CONNECTIVITY, "unreachable")
    await coordinator._async_update_data()
    assert coordinator.failure_count == 0
    assert coordinator.update_interval == timedelta(seconds=60)
    assert _connectivity(hass, config_entry) is None
    assert coordinator.last_success is not None


async def test_asset_id_written_back_without_reload(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
) -> None:
    client._asset_id = CHANGED_ASSET_ID
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await coordinator._async_update_data()
    assert config_entry.data[CONF_ASSET_ID] == CHANGED_ASSET_ID
    reload.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Mode.HYBRID, int(Mode.HYBRID)),
        (Mode.HEAT_PUMP, int(Mode.HEAT_PUMP)),
        (Mode.ELECTRIC, int(Mode.HYBRID)),
        (Mode.VACATION, int(Mode.HYBRID)),
        (Mode.GUEST, int(Mode.HYBRID)),
    ],
)
async def test_last_permanent_mode_tracked_and_persisted(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    mode: Mode,
    expected: int,
) -> None:
    client.state = make_feature_set(mode=ModeWord(0, int(Mode.HYBRID)))
    await coordinator._async_update_data()
    client.state = make_feature_set(mode=ModeWord(2, int(mode)))
    await coordinator._async_update_data()
    assert coordinator.user_state.last_permanent_mode == expected
    stored = await StateStore(hass, config_entry.entry_id).async_load()
    assert stored.last_permanent_mode == expected


@pytest.mark.parametrize(
    "err", [AuthFailed("two rejections"), NotPairedError("not paired")]
)
async def test_auth_failure(
    coordinator: AOSmithCoordinator, client: FakeClient, err: Exception
) -> None:
    with (
        patch(
            "custom_components.aosmith_ble.coordinator.connect_and_classify",
            side_effect=err,
        ),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coordinator._async_update_data()
    assert coordinator.auth_failed is True
    assert "disconnect" in _names(client)
    assert "async_close" not in _names(client)
    assert coordinator.failure_count == 0


async def test_two_connect_rejections_auth_failure(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client.connect_effects = [ChallengeRejectedError("1"), ChallengeRejectedError("2")]
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.auth_failed is True
    assert _names(client)[-1] == "disconnect"


@pytest.mark.parametrize(
    "err",
    [
        BleakError(f"bleak {SECRET}"),
        TimeoutError(f"timeout {SECRET}"),
        NotConnectedError(f"gone {SECRET}"),
        ProtocolError(f"bad frame {SECRET}"),
        ReadRefusedError(f"refused {SECRET}"),
        ConnectionSlotsExhaustedError(f"slots {SECRET}"),
        ValidationError(f"invalid {SECRET}"),
        OSError(f"os {SECRET}"),
        KeyError(f"key {SECRET}"),
        ChallengeRejectedError(f"first rejection {SECRET}"),
    ],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.parametrize("side", ["connect", "read"])
async def test_transient_errors_update_failed_scrubbed(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    err: Exception,
    side: str,
) -> None:
    if side == "connect":
        if isinstance(err, ChallengeRejectedError):
            # A raw rejection escapes connect_and_classify after a plain SessionError.
            client.connect_effects = [SessionError("lost"), err]
        else:
            client.connect_effects = [err]
    else:
        client._connected = True
        # A read-side session error reconnects once; failing again is transient.
        client.read_effects = [err, err] if isinstance(err, SessionError) else [err]
    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()
    message = str(caught.value)
    assert SECRET not in message
    assert PLACEHOLDER_ASSET_ID in message
    assert coordinator.failure_count == 1
    assert coordinator.auth_failed is False
    assert coordinator.last_error is not None
    assert SECRET not in coordinator.last_error
    assert _connectivity(hass, config_entry) is None
    assert _names(client)[-1] == "disconnect"
    if side == "connect":
        assert coordinator.connecting is False


async def test_unexpected_exception_logged_at_warning(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _fail(coordinator, client, KeyError(f"key {SECRET}"))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert SECRET not in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.parametrize(
    ("last", "variant"),
    [
        (ConnectionSlotsExhaustedError("slots"), "no_connection_slots"),
        (BleakError("bleak"), "unreachable"),
        (TimeoutError(), "unreachable"),
        (KeyError("x"), "unreachable"),
    ],
)
async def test_threshold_raises_connectivity_issue(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    last: Exception,
    variant: str,
) -> None:
    await _fail(coordinator, client, ConnectionSlotsExhaustedError("slots"))
    await _fail(coordinator, client, BleakError("bleak"))
    assert _connectivity(hass, config_entry) is None
    await _fail(coordinator, client, last)
    issue = _connectivity(hass, config_entry)
    assert issue is not None
    assert issue.translation_key == f"connectivity_{variant}"
    assert_issue_translated(issue)
    assert issue.translation_placeholders == {"title": config_entry.title}


async def test_issue_variant_follows_last_error_past_threshold(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
) -> None:
    for _ in range(3):
        await _fail(coordinator, client, BleakError("bleak"))
    await _fail(coordinator, client, ConnectionSlotsExhaustedError("slots"))
    issue = _connectivity(hass, config_entry)
    assert issue is not None
    assert issue.translation_key == "connectivity_no_connection_slots"
    assert_issue_translated(issue)


@pytest.mark.parametrize(
    "err",
    [
        UnknownDeviceError("unknown", model="HPTS-50", firmware=(9, 9)),
        UnsupportedConfigurationError("units", reason=ConfigurationIssue.UNITS),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_unknown_or_unsupported_schedules_reload(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
    err: Exception,
) -> None:
    client.connect_effects = [err]
    with (
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    reload.assert_called_once_with(config_entry.entry_id)
    assert coordinator.failure_count == 0
    assert _connectivity(hass, config_entry) is None


async def test_read_session_error_reconnects_once(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client._connected = True
    client.read_effects = [SessionError("lost")]
    data = await coordinator._async_update_data()
    assert data == client.state
    assert _names(client) == [
        "async_get_state",
        "disconnect",
        "connect",
        "async_get_state",
    ]


async def test_read_session_error_twice_is_transient(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client._connected = True
    client.read_effects = [SessionError("lost"), SessionError("lost again")]
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert coordinator.failure_count == 1
    assert _names(client).count("connect") == 1


async def test_read_challenge_rejection_then_rejection_is_auth_failure(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client._connected = True
    client.read_effects = [ChallengeRejectedError("read-side")]
    client.connect_effects = [ChallengeRejectedError("again")]
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    assert coordinator.auth_failed is True
    names = _names(client)
    assert names[:3] == ["async_get_state", "disconnect", "connect"]
    assert names.count("connect") == 1
    assert names[-1] == "disconnect"


async def test_read_challenge_rejection_then_success_recovers(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    client._connected = True
    client.read_effects = [ChallengeRejectedError("read-side")]
    await coordinator._async_update_data()
    assert coordinator.auth_failed is False


async def test_poll_timeout(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
) -> None:
    client.delay = 5
    with (
        patch("custom_components.aosmith_ble.coordinator.POLL_TIMEOUT", 0.01),
        pytest.raises(UpdateFailed),
    ):
        await coordinator._async_update_data()
    assert coordinator.failure_count == 1
    assert _names(client)[-1] == "disconnect"
    assert coordinator.connecting is False
    assert not coordinator.io_lock.locked()


async def test_closing_returns_last_data_without_client(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    previous = make_feature_set(setpoint=130.0)
    coordinator.data = previous
    coordinator.closing = True
    assert await coordinator._async_update_data() is previous
    assert client.calls == []


async def test_exception_while_closing_not_counted(
    hass: HomeAssistant,
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    config_entry: MockConfigEntry,
) -> None:
    previous = make_feature_set(setpoint=130.0)
    coordinator.data = previous
    coordinator.failure_count = 2
    client._connected = True

    async def closing_read(*_args: object, **_kwargs: object) -> None:
        coordinator.closing = True
        raise NotConnectedError("client is closed")

    client.async_get_state = closing_read  # type: ignore[method-assign]
    assert await coordinator._async_update_data() is previous
    assert coordinator.failure_count == 2
    assert _connectivity(hass, config_entry) is None


async def test_auth_failed_poll_raises_update_failed(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    coordinator.data = make_feature_set()
    coordinator.auth_failed = True
    with pytest.raises(UpdateFailed, match="authentication required"):
        await coordinator._async_update_data()
    assert client.calls == []


async def test_backoff_doubles_caps_and_resets(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    seen = []
    for _ in range(7):
        await _fail(coordinator, client, BleakError("bleak"))
        seen.append(coordinator.update_interval.total_seconds())
    assert seen == [60, 60, 120, 240, 480, 600, 600]
    await coordinator._async_update_data()
    assert coordinator.update_interval == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("configured", "expected"), [(5, 15), (9999, 300), (45, 45), (None, 60)]
)
async def test_poll_interval_clamped(
    hass: HomeAssistant, configured: int | None, expected: int
) -> None:
    options = {} if configured is None else {CONF_POLL_INTERVAL: configured}
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=PLACEHOLDER_NAME,
        data={CONF_ASSET_ID: PLACEHOLDER_ASSET_ID},
        options=options,
    )
    coordinator = _build(hass, entry)
    assert coordinator.update_interval == timedelta(seconds=expected)


async def test_backoff_uses_clamped_interval(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=PLACEHOLDER_NAME,
        data={CONF_ASSET_ID: PLACEHOLDER_ASSET_ID},
        options={CONF_POLL_INTERVAL: 5},
    )
    client = FakeClient(asset_id=PLACEHOLDER_ASSET_ID)
    coordinator = _build(hass, entry, client)
    for _ in range(3):
        await _fail(coordinator, client, BleakError("bleak"))
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_two_entries_connectivity_issues_do_not_collide(
    hass: HomeAssistant,
) -> None:
    first = MockConfigEntry(domain=DOMAIN, title="First", data={})
    second = MockConfigEntry(domain=DOMAIN, title="Second", data={})
    first_client, second_client = FakeClient(), FakeClient()
    first_coord = _build(hass, first, first_client)
    _build(hass, second, second_client)
    for _ in range(3):
        await _fail(first_coord, first_client, BleakError("bleak"))
    assert _connectivity(hass, first) is not None
    assert _connectivity(hass, second) is None


async def test_poll_runs_under_io_lock(
    coordinator: AOSmithCoordinator, client: FakeClient
) -> None:
    seen: list[bool] = []
    original = client.connect

    async def connect() -> None:
        seen.append(coordinator.io_lock.locked())
        seen.append(coordinator.connecting)
        await original()

    client.connect = connect  # type: ignore[method-assign]
    await coordinator._async_update_data()
    assert seen == [True, True]
    assert coordinator.connecting is False


async def test_flags_initial_state(coordinator: AOSmithCoordinator) -> None:
    assert coordinator.closing is False
    assert coordinator.auth_failed is False
    assert coordinator.drop_pending is False
    assert coordinator.connecting is False
    assert coordinator.failure_count == 0
    assert coordinator.last_error is None
    assert coordinator.last_success is None


async def test_unexpected_exception_logged_once_per_failure_run(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for _ in range(3):
        await _fail(coordinator, client, KeyError("key"))
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1

    await coordinator._async_update_data()
    client._connected = False
    await _fail(coordinator, client, KeyError("key"))
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2


async def test_success_bookkeeping_error_is_scrubbed_and_contained(
    coordinator: AOSmithCoordinator,
    client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client.state = make_feature_set(mode=ModeWord(0, int(Mode.HYBRID)))
    with patch.object(
        coordinator.store, "async_save", side_effect=OSError(f"disk {SECRET}")
    ):
        state = await coordinator._async_update_data()
    assert state is client.state
    assert coordinator.failure_count == 0
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert SECRET not in warnings[0].getMessage()
    assert PLACEHOLDER_ASSET_ID in warnings[0].getMessage()
