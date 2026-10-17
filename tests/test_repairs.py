"""Repair fix flows and issue translations."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aosmith_ble import (
    ConfigurationIssue,
    UnknownDeviceError,
    UnsupportedConfigurationError,
)
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble import issues
from custom_components.aosmith_ble.const import DOMAIN
from custom_components.aosmith_ble.repairs import async_create_fix_flow

from .conftest import ClientFactory, assert_no_advertised_name

PACKAGE = Path(issues.__file__).parent
ISSUES = json.loads((PACKAGE / "translations" / "en.json").read_text())["issues"]


def _possible_translation_keys() -> set[str]:
    keys = {issues.ISSUE_UNSUPPORTED_DEVICE}
    keys |= {
        f"{issues.ISSUE_UNSUPPORTED_CONFIGURATION}_{variant}"
        for variant in issues.CONFIGURATION_VARIANTS
    }
    keys |= {
        f"{issues.ISSUE_CONNECTIVITY}_{variant}"
        for variant in issues.CONNECTIVITY_VARIANTS
    }
    return keys


@pytest.mark.parametrize("reason", list(ConfigurationIssue))
def test_every_library_reason_has_its_own_variant(reason: ConfigurationIssue) -> None:
    assert issues.configuration_variant(reason) == reason.value
    assert reason.value in issues.CONFIGURATION_VARIANTS


def test_unknown_reason_maps_to_the_generic_variant() -> None:
    assert issues.configuration_variant("future") == issues.VARIANT_OTHER
    assert issues.VARIANT_OTHER in issues.CONFIGURATION_VARIANTS


def test_variant_constants_are_the_connectivity_set() -> None:
    assert set(issues.CONNECTIVITY_VARIANTS) == {
        issues.VARIANT_NOT_ADVERTISING,
        issues.VARIANT_NO_CONNECTION_SLOTS,
        issues.VARIANT_UNREACHABLE,
    }


FIXABLE_KEYS = frozenset(
    {
        issues.ISSUE_UNSUPPORTED_DEVICE,
        *(
            f"{issues.ISSUE_UNSUPPORTED_CONFIGURATION}_{variant}"
            for variant in issues.CONFIGURATION_VARIANTS
        ),
    }
)


def assert_issue_text(key: str, *, fixable: bool) -> None:
    """The issue has a title and its text where the frontend shows it.

    A fixable issue's text is the fix flow's confirm step description and
    hassfest rejects an issue-level description next to fix_flow. A
    non-fixable issue's text is its issue-level description.
    """
    entry = ISSUES[key]
    assert entry["title"]
    if fixable:
        assert "description" not in entry
        assert entry["fix_flow"]["step"]["confirm"]["description"]
    else:
        assert "fix_flow" not in entry
        assert entry["description"]


def assert_issue_translated(issue: ir.IssueEntry) -> None:
    """A raised issue is translated in en.json and shows no advertised name."""
    assert issue.translation_key in ISSUES
    assert_no_advertised_name(issue)
    assert_issue_text(issue.translation_key, fixable=issue.is_fixable)


@pytest.mark.parametrize("key", sorted(_possible_translation_keys()))
def test_every_raisable_key_is_translated(key: str) -> None:
    assert_issue_text(key, fixable=key in FIXABLE_KEYS)


@pytest.mark.parametrize("key", sorted(ISSUES))
def test_no_issue_has_both_description_and_fix_flow(key: str) -> None:
    # hassfest: description and fix_flow are in the exclusion group 'fixable'.
    assert ("description" in ISSUES[key]) != ("fix_flow" in ISSUES[key])


def test_no_unreachable_translation_is_orphaned() -> None:
    assert set(ISSUES) == _possible_translation_keys()


@pytest.mark.parametrize("key", sorted(FIXABLE_KEYS))
def test_fixable_issues_have_fix_flow_strings(key: str) -> None:
    confirm = ISSUES[key]["fix_flow"]["step"]["confirm"]
    assert confirm["title"]
    assert confirm["description"]
    assert confirm["submit"] == "Retry"


def test_unsupported_device_copy() -> None:
    entry = ISSUES["unsupported_device"]
    assert "{model}" in entry["title"]
    assert "{firmware}" in entry["title"]
    assert "description" not in entry
    confirm = entry["fix_flow"]["step"]["confirm"]
    assert confirm["description"].startswith(
        "This model or firmware is not supported yet. Press the Bluetooth button"
    )
    assert "download diagnostics" in confirm["description"]
    assert confirm["submit"] == "Retry"


def test_translations_have_no_em_dashes() -> None:
    assert chr(0x2014) not in json.dumps(ISSUES, ensure_ascii=False)


@pytest.mark.parametrize(
    "key",
    [issues.ISSUE_UNSUPPORTED_CONFIGURATION, issues.ISSUE_UNSUPPORTED_DEVICE],
)
async def test_retry_reloads_the_entry(hass: HomeAssistant, key: str) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Heater")
    entry.add_to_hass(hass)
    variant = "units" if key == issues.ISSUE_UNSUPPORTED_CONFIGURATION else None
    issues.raise_issue(hass, entry, key, variant)
    issue_id = f"{key}_{entry.entry_id}"
    flow = await async_create_fix_flow(hass, issue_id, {"entry_id": entry.entry_id})
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = issue_id

    shown = await flow.async_step_init()
    assert shown["type"] is FlowResultType.FORM
    assert shown["step_id"] == "confirm"

    async def resolved(_entry_id: str) -> bool:
        # A setup that succeeds clears the issue.
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return True

    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(side_effect=resolved)
    ) as reload:
        done = await flow.async_step_confirm({})

    reload.assert_awaited_once_with(entry.entry_id)
    assert done["type"] is FlowResultType.CREATE_ENTRY


async def test_unsupported_device_links_to_the_issue_tracker(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, title="Heater")
    entry.add_to_hass(hass)
    issues.raise_issue(hass, entry, issues.ISSUE_UNSUPPORTED_DEVICE, None)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{issues.ISSUE_UNSUPPORTED_DEVICE}_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.learn_more_url == issues.ISSUE_TRACKER_URL


async def test_fix_flow_tolerates_missing_data(hass: HomeAssistant) -> None:
    for data in (None, {}, {"other": 1}):
        flow = await async_create_fix_flow(hass, "x", data)
        flow.hass = hass
        flow.handler = DOMAIN
        flow.issue_id = "x"
        with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
            done = await flow.async_step_confirm({})
        reload.assert_not_awaited()
        assert done["type"] is FlowResultType.CREATE_ENTRY


async def test_fix_flow_skips_reload_for_a_deleted_entry(hass: HomeAssistant) -> None:
    flow = await async_create_fix_flow(hass, "x", {"entry_id": "gone"})
    flow.hass = hass
    flow.handler = DOMAIN
    flow.issue_id = "x"
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        done = await flow.async_step_confirm({})
    reload.assert_not_awaited()
    assert done["type"] is FlowResultType.CREATE_ENTRY


# ---------------------------------------------------------------------------
# Unpatched: the real repairs flow manager and a real reload
# ---------------------------------------------------------------------------


def _unsupported_units() -> Exception:
    return UnsupportedConfigurationError("celsius", reason=ConfigurationIssue.UNITS)


def _unknown_device() -> Exception:
    return UnknownDeviceError("no profile", model="HPTS-99", firmware=(9, 9))


async def _retry(hass: HomeAssistant, issue_id: str) -> Any:
    assert await async_setup_component(hass, "repairs", {})
    manager = repairs_flow_manager(hass)
    assert manager is not None
    shown = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert shown["type"] is FlowResultType.FORM
    done = await manager.async_configure(shown["flow_id"], {})
    await hass.async_block_till_done()
    return done


@pytest.mark.parametrize(
    ("make_error", "key", "state"),
    [
        (
            _unsupported_units,
            issues.ISSUE_UNSUPPORTED_CONFIGURATION,
            ConfigEntryState.SETUP_ERROR,
        ),
        (_unknown_device, issues.ISSUE_UNSUPPORTED_DEVICE, ConfigEntryState.LOADED),
    ],
    ids=["units", "unsupported_device"],
)
async def test_failed_retry_keeps_the_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    enable_bluetooth: None,
    make_error: Callable[[], Exception],
    key: str,
    state: ConfigEntryState,
) -> None:
    # The setup and the Retry reload both fail the same way.
    for _ in range(2):
        client_factory.prepare.append(
            lambda client: client.connect_effects.append(make_error())
        )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is state
    issue_id = f"{key}_{config_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert_issue_translated(issue)

    done = await _retry(hass, issue_id)

    assert len(client_factory.built) == 2
    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "still_failing"
    assert config_entry.state is state
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


@pytest.mark.parametrize(
    ("make_error", "key"),
    [
        (_unsupported_units, issues.ISSUE_UNSUPPORTED_CONFIGURATION),
        (_unknown_device, issues.ISSUE_UNSUPPORTED_DEVICE),
    ],
    ids=["units", "unsupported_device"],
)
async def test_successful_retry_resolves_the_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    client_factory: ClientFactory,
    mock_ble_device: MagicMock,
    enable_bluetooth: None,
    make_error: Callable[[], Exception],
    key: str,
) -> None:
    client_factory.prepare.append(
        lambda client: client.connect_effects.append(make_error())
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"{key}_{config_entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    done = await _retry(hass, issue_id)

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


@pytest.mark.parametrize(
    "key", sorted(key for key in ISSUES if key.startswith("unsupported_"))
)
def test_fixable_issues_have_still_failing_abort(key: str) -> None:
    assert ISSUES[key]["fix_flow"]["abort"]["still_failing"]
