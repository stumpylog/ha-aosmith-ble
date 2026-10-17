"""Repair issues raised and cleared by the integration."""

from __future__ import annotations

from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_CONNECTIVITY: Final = "connectivity"
ISSUE_UNSUPPORTED_CONFIGURATION: Final = "unsupported_configuration"
ISSUE_UNSUPPORTED_DEVICE: Final = "unsupported_device"

ALL_ISSUES: Final = (
    ISSUE_CONNECTIVITY,
    ISSUE_UNSUPPORTED_CONFIGURATION,
    ISSUE_UNSUPPORTED_DEVICE,
)
VARIANT_NOT_ADVERTISING: Final = "not_advertising"
VARIANT_NO_CONNECTION_SLOTS: Final = "no_connection_slots"
VARIANT_UNREACHABLE: Final = "unreachable"
CONNECTIVITY_VARIANTS: Final = (
    VARIANT_NOT_ADVERTISING,
    VARIANT_NO_CONNECTION_SLOTS,
    VARIANT_UNREACHABLE,
)
VARIANT_UNITS: Final = "units"
VARIANT_MTU: Final = "mtu"
# A ConfigurationIssue this release does not know yet.
VARIANT_OTHER: Final = "other"
CONFIGURATION_VARIANTS: Final = (VARIANT_UNITS, VARIANT_MTU, VARIANT_OTHER)
ISSUE_TRACKER_URL: Final = "https://github.com/stumpylog/ha-aosmith-ble/issues"
_FIXABLE: Final = frozenset({ISSUE_UNSUPPORTED_CONFIGURATION, ISSUE_UNSUPPORTED_DEVICE})


def configuration_variant(reason: object) -> str:
    """The unsupported_configuration variant for a library ConfigurationIssue.

    An unknown reason gets the generic variant, so it is always translated.
    """
    value = str(reason)
    return value if value in (VARIANT_UNITS, VARIANT_MTU) else VARIANT_OTHER


def _issue_id(entry: ConfigEntry, key: str) -> str:
    return f"{key}_{entry.entry_id}"


def raise_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    key: str,
    variant: str | None,
    **placeholders: object,
) -> None:
    """Create or replace an entry's issue; a new variant replaces the old one."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(entry, key),
        data={"entry_id": entry.entry_id},
        is_fixable=key in _FIXABLE,
        learn_more_url=ISSUE_TRACKER_URL if key == ISSUE_UNSUPPORTED_DEVICE else None,
        severity=ir.IssueSeverity.WARNING,
        translation_key=key if variant is None else f"{key}_{variant}",
        translation_placeholders={
            "title": entry.title,
            **{name: str(value) for name, value in placeholders.items()},
        },
    )


def clear_issue(hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
    """Delete an entry's issue if it exists."""
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry, key))


def delete_all_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete every issue of one entry (entry removal)."""
    for key in ALL_ISSUES:
        clear_issue(hass, entry, key)
