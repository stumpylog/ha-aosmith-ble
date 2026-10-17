"""Tests for identifier scrubbing. Fake identifiers are built at runtime."""

from __future__ import annotations

from custom_components.aosmith_ble.const import (
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)
from custom_components.aosmith_ble.scrub import Scrubber, scrub

_OCTETS = ["1a", "2b", "3c", "4d", "5e", "6f"]
ADDRESS = ":".join(_OCTETS).upper()
SERIAL = "Q7" + "k9" + "Z3m" + "L2"
NAME = "iCOMM-" + "ZZ" + "9" * 3 + "Y" + "0" * 3 + SERIAL[:6]
CODE = "4" + "8" * 2 + "1" + "93"
ASSET = "02" + "zq" + "1" * 6 + "FAKE" + "9"


def _swap(text: str) -> str:
    chars = list(text)
    for i in range(0, len(chars) - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def _scrubber() -> Scrubber:
    return Scrubber.for_entry(ADDRESS, NAME, CODE, ASSET, SERIAL)


def test_scrubs_address_any_case_and_separators() -> None:
    s = _scrubber()
    for variant in (
        ADDRESS,
        ADDRESS.lower(),
        ADDRESS.replace(":", "-"),
        ADDRESS.replace(":", ""),
        ADDRESS.replace(":", "").lower(),
    ):
        assert s.scrub(f"dev {variant} up") == f"dev {PLACEHOLDER_ADDRESS} up"


def test_scrubs_address_as_hex_bytes() -> None:
    s = _scrubber()
    spaced = ADDRESS.replace(":", " ").lower()
    assert s.scrub(f"raw {spaced}") == f"raw {PLACEHOLDER_ADDRESS}"
    assert s.scrub("raw " + ADDRESS.encode().hex()) == f"raw {PLACEHOLDER_ADDRESS}"


def test_scrubs_name_code_asset_id() -> None:
    out = _scrubber().scrub(f"{NAME} {CODE} {ASSET}")
    assert out == (
        f"{PLACEHOLDER_NAME} {PLACEHOLDER_PAIRING_CODE} {PLACEHOLDER_ASSET_ID}"
    )


def test_scrubs_serial_text_hex_and_byte_swapped_hex() -> None:
    s = _scrubber()
    swapped = _swap(SERIAL).encode().hex()
    text = f"{SERIAL} {SERIAL.encode().hex()} {swapped.upper()}"
    assert s.scrub(text) == " ".join([PLACEHOLDER_PAIRING_CODE] * 3)


def test_learn_serial() -> None:
    s = Scrubber.for_entry(ADDRESS, NAME, CODE, ASSET, None)
    late = "W" + "p4" * 3 + "Qx"
    assert late in s.scrub(late)
    s.learn_serial(late)
    assert s.scrub(f"sn {late}") == f"sn {PLACEHOLDER_PAIRING_CODE}"


def test_learn_asset_id() -> None:
    s = Scrubber.for_entry(ADDRESS, NAME, CODE, None, None)
    late = "02" + "lt" + "6" * 6 + "LATE" + "3"
    assert late in s.scrub(late)
    s.learn_asset_id(late)
    assert s.scrub(f"asset {late}") == f"asset {PLACEHOLDER_ASSET_ID}"
    s.learn_asset_id(None)  # nothing learned: unchanged
    s.learn_asset_id("")
    assert s.scrub("plain") == "plain"


def test_unrelated_text_untouched() -> None:
    text = "Heater at 120 F, mode HEAT_PUMP, 2 failures"
    assert _scrubber().scrub(text) == text


def test_none_scrubber_passthrough() -> None:
    assert scrub(f"{ADDRESS} {CODE}", None) == f"{ADDRESS} {CODE}"
    assert scrub(f"{CODE}", _scrubber()) == PLACEHOLDER_PAIRING_CODE


def test_scrubs_exception_text() -> None:
    err = TimeoutError(f"no reply from {ADDRESS} for {NAME}")
    assert _scrubber().scrub_exception(err) == (
        f"TimeoutError: no reply from {PLACEHOLDER_ADDRESS} for {PLACEHOLDER_NAME}"
    )


def test_name_replaced_before_embedded_code() -> None:
    s = Scrubber.for_entry(ADDRESS, NAME, NAME[-6:], ASSET, SERIAL)
    assert s.scrub(f"adv {NAME}") == f"adv {PLACEHOLDER_NAME}"


def test_scrubs_address_with_underscore_and_dot_separators() -> None:
    s = _scrubber()
    for sep in ("_", "."):
        for variant in (ADDRESS.replace(":", sep), ADDRESS.replace(":", sep).lower()):
            assert s.scrub(f"sensor.x_{variant}_y") == (
                f"sensor.x_{PLACEHOLDER_ADDRESS}_y"
            )


def test_pairing_code_derived_from_name_when_missing() -> None:
    s = Scrubber.for_entry(ADDRESS, NAME, None, ASSET, SERIAL)
    derived = "".join(ch for ch in NAME if ch.isdigit())[-6:]
    assert s.scrub(f"code {derived}") == f"code {PLACEHOLDER_PAIRING_CODE}"


def test_name_without_enough_digits_derives_no_code() -> None:
    s = Scrubber.for_entry(ADDRESS, "iCOMM-" + "ab", None, None, None)
    assert s.scrub("12") == "12"
