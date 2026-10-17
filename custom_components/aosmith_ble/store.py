"""Per-entry persisted user state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_ELECTRIC_DAYS,
    DEFAULT_VACATION_DAYS,
    DOMAIN,
    ELECTRIC_DAYS_MAX,
    ELECTRIC_DAYS_MIN,
    PERMANENT_MODES,
    VACATION_DAYS_MAX,
    VACATION_DAYS_MIN,
)

STORAGE_VERSION = 1


@dataclass
class UserState:
    """User-chosen values owned by the coordinator."""

    electric_days: int = DEFAULT_ELECTRIC_DAYS
    vacation_days: int = DEFAULT_VACATION_DAYS
    last_permanent_mode: int | None = None


def _days(value: Any, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(low, min(high, value))


class StateStore:
    """Load, validate, save and remove one entry's UserState."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )

    async def async_load(self) -> UserState:
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return UserState()
        mode = data.get("last_permanent_mode")
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode not in PERMANENT_MODES
        ):
            mode = None
        return UserState(
            electric_days=_days(
                data.get("electric_days"),
                ELECTRIC_DAYS_MIN,
                ELECTRIC_DAYS_MAX,
                DEFAULT_ELECTRIC_DAYS,
            ),
            vacation_days=_days(
                data.get("vacation_days"),
                VACATION_DAYS_MIN,
                VACATION_DAYS_MAX,
                DEFAULT_VACATION_DAYS,
            ),
            last_permanent_mode=mode,
        )

    async def async_save(self, state: UserState) -> None:
        await self._store.async_save(asdict(state))

    async def async_remove(self) -> None:
        await self._store.async_remove()
