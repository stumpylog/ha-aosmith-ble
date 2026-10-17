"""Translation keys the integration raises must exist in en.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components import aosmith_ble

STRINGS = json.loads(
    (Path(aosmith_ble.__file__).parent / "translations" / "en.json").read_text()
)


@pytest.mark.parametrize(
    "path",
    [
        ("exceptions", "not_advertising", "message"),
        ("exceptions", "no_connection_slots", "message"),
        ("config", "error", "no_connection_slots"),
    ],
    ids=".".join,
)
def test_translation_exists(path: tuple[str, ...]) -> None:
    node = STRINGS
    for key in path:
        assert key in node, ".".join(path)
        node = node[key]
    assert isinstance(node, str)
    assert node


@pytest.mark.parametrize(
    "path",
    [
        ("issues", "connectivity_no_connection_slots", "description"),
        ("exceptions", "no_connection_slots", "message"),
        ("config", "error", "no_connection_slots"),
    ],
    ids=".".join,
)
def test_no_connection_slots_covers_an_offline_proxy(path: tuple[str, ...]) -> None:
    # A proxy that went offline raises the same slots error as a full one.
    node = STRINGS
    for key in path:
        node = node[key]
    assert "offline" in node
    assert "slot" in node


def test_no_connection_slots_title_does_not_blame_slots() -> None:
    title = STRINGS["issues"]["connectivity_no_connection_slots"]["title"]
    assert title == "{title}: no Bluetooth connection available"
