"""Repair fix flows: a confirm step whose submit reloads the entry."""

from __future__ import annotations

from typing import cast

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir


class RetryRepairFlow(ConfirmRepairFlow):
    """Confirm, then reload the entry so setup tries again.

    Home Assistant deletes the issue when a fix flow ends in anything but an
    abort. A reload whose setup raised the issue again therefore aborts, so
    the instruction stays visible.
    """

    def __init__(self, entry_id: str | None) -> None:
        self._entry_id = entry_id

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> FlowResult:
        """Reload the entry on submit."""
        if (
            user_input is not None
            and self._entry_id is not None
            and self.hass.config_entries.async_get_entry(self._entry_id) is not None
        ):
            await self.hass.config_entries.async_reload(self._entry_id)
            if ir.async_get(self.hass).async_get_issue(self.handler, self.issue_id):
                return self.async_abort(reason="still_failing")
        return cast("FlowResult", await super().async_step_confirm(user_input))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the Retry flow for a fixable issue."""
    entry_id = (data or {}).get("entry_id")
    return RetryRepairFlow(None if entry_id is None else str(entry_id))
