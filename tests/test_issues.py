"""Repair issue helpers."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.aosmith_ble.const import DOMAIN
from custom_components.aosmith_ble.issues import (
    ISSUE_CONNECTIVITY,
    ISSUE_UNSUPPORTED_CONFIGURATION,
    ISSUE_UNSUPPORTED_DEVICE,
    clear_issue,
    delete_all_issues,
    raise_issue,
)


def _entry(hass: HomeAssistant, title: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=title)
    entry.add_to_hass(hass)
    return entry


def _issue(hass: HomeAssistant, key: str, entry: MockConfigEntry) -> ir.IssueEntry:
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")
    assert issue is not None
    return issue


async def test_raise_issue_fields(hass: HomeAssistant) -> None:
    entry = _entry(hass, "Heater")
    raise_issue(hass, entry, ISSUE_CONNECTIVITY, "unreachable")
    issue = _issue(hass, ISSUE_CONNECTIVITY, entry)
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "connectivity_unreachable"
    assert issue.translation_placeholders == {"title": "Heater"}
    assert issue.data == {"entry_id": entry.entry_id}
    assert issue.is_fixable is False


async def test_unsupported_issues_are_fixable(hass: HomeAssistant) -> None:
    entry = _entry(hass, "Heater")
    raise_issue(hass, entry, ISSUE_UNSUPPORTED_CONFIGURATION, "units")
    raise_issue(
        hass, entry, ISSUE_UNSUPPORTED_DEVICE, None, model="HPTS-50", firmware="6.3"
    )
    config = _issue(hass, ISSUE_UNSUPPORTED_CONFIGURATION, entry)
    assert config.is_fixable is True
    assert config.translation_key == "unsupported_configuration_units"
    device = _issue(hass, ISSUE_UNSUPPORTED_DEVICE, entry)
    assert device.is_fixable is True
    assert device.translation_key == "unsupported_device"
    assert device.translation_placeholders == {
        "title": "Heater",
        "model": "HPTS-50",
        "firmware": "6.3",
    }
    assert device.data == {"entry_id": entry.entry_id}


async def test_reraise_with_other_variant_replaces(hass: HomeAssistant) -> None:
    entry = _entry(hass, "Heater")
    raise_issue(hass, entry, ISSUE_CONNECTIVITY, "unreachable")
    raise_issue(hass, entry, ISSUE_CONNECTIVITY, "no_connection_slots")
    assert (
        _issue(hass, ISSUE_CONNECTIVITY, entry).translation_key
        == "connectivity_no_connection_slots"
    )
    assert len(ir.async_get(hass).issues) == 1


async def test_clear_issue(hass: HomeAssistant) -> None:
    entry = _entry(hass, "Heater")
    raise_issue(hass, entry, ISSUE_CONNECTIVITY, "unreachable")
    clear_issue(hass, entry, ISSUE_CONNECTIVITY)
    clear_issue(hass, entry, ISSUE_CONNECTIVITY)  # idempotent
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"connectivity_{entry.entry_id}")
        is None
    )


async def test_two_entries_do_not_collide(hass: HomeAssistant) -> None:
    first = _entry(hass, "First")
    second = _entry(hass, "Second")
    raise_issue(hass, first, ISSUE_CONNECTIVITY, "unreachable")
    raise_issue(hass, second, ISSUE_CONNECTIVITY, "no_connection_slots")
    assert _issue(hass, ISSUE_CONNECTIVITY, first).translation_placeholders == {
        "title": "First"
    }
    assert _issue(hass, ISSUE_CONNECTIVITY, second).translation_placeholders == {
        "title": "Second"
    }
    clear_issue(hass, first, ISSUE_CONNECTIVITY)
    assert _issue(hass, ISSUE_CONNECTIVITY, second) is not None


async def test_delete_all_issues_only_that_entry(hass: HomeAssistant) -> None:
    first = _entry(hass, "First")
    second = _entry(hass, "Second")
    for entry in (first, second):
        raise_issue(hass, entry, ISSUE_CONNECTIVITY, "unreachable")
        raise_issue(hass, entry, ISSUE_UNSUPPORTED_CONFIGURATION, "mtu")
        raise_issue(hass, entry, ISSUE_UNSUPPORTED_DEVICE, None)
    delete_all_issues(hass, first)
    registry = ir.async_get(hass)
    assert {issue_id for _, issue_id in registry.issues} == {
        f"connectivity_{second.entry_id}",
        f"unsupported_configuration_{second.entry_id}",
        f"unsupported_device_{second.entry_id}",
    }
