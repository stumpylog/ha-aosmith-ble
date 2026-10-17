"""Identifier scrubbing for every string the integration emits."""

from __future__ import annotations

import re

from aosmith_ble import pairing_code_from_name

from .const import (
    PLACEHOLDER_ADDRESS,
    PLACEHOLDER_ASSET_ID,
    PLACEHOLDER_NAME,
    PLACEHOLDER_PAIRING_CODE,
)


def _swap_pairs(text: str) -> str:
    """Swap adjacent character pairs (a trailing odd character stays)."""
    chars = list(text)
    for i in range(0, len(chars) - 1, 2):
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


def _address_variants(address: str) -> set[str]:
    octets = (
        re.split(r"[:\-_.]", address)
        if re.search(r"[:\-_.]", address)
        else [address[i : i + 2] for i in range(0, len(address), 2)]
    )
    variants = {address, address.encode().hex()}
    # "_" covers slugified forms such as entity IDs.
    for sep in (":", "-", "", " ", "_", "."):
        variants.add(sep.join(octets))
    return variants


def _serial_variants(serial: str) -> set[str]:
    return {
        serial,
        serial.encode().hex(),
        _swap_pairs(serial).encode().hex(),
    }


class Scrubber:
    """Replaces one entry's identifiers with the documented placeholders."""

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None

    @classmethod
    def for_entry(
        cls,
        address: str | None,
        name: str | None,
        pairing_code: str | None,
        asset_id: str | None,
        serial: str | None,
    ) -> Scrubber:
        self = cls()
        if address:
            self._add(_address_variants(address), PLACEHOLDER_ADDRESS)
        if name:
            self._add({name}, PLACEHOLDER_NAME)
        if asset_id:
            self._add({asset_id}, PLACEHOLDER_ASSET_ID)
        if not pairing_code and name:
            # The code is the last 6 digits of the advertised name.
            try:
                pairing_code = pairing_code_from_name(name).decode()
            except ValueError:
                pairing_code = None
        if pairing_code:
            self._add({pairing_code}, PLACEHOLDER_PAIRING_CODE)
        if serial:
            self.learn_serial(serial)
        self._compile()
        return self

    def _add(self, variants: set[str], replacement: str) -> None:
        for variant in variants:
            if variant:
                self._map.setdefault(variant.lower(), replacement)

    def _compile(self) -> None:
        if not self._map:
            self._pattern = None
            return
        # Longest first so a name wins over a code embedded in it.
        keys = sorted(self._map, key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(k) for k in keys), re.IGNORECASE)

    def learn_serial(self, serial: str) -> None:
        """Add a serial learned after setup (text, hex and byte-swapped hex)."""
        if not serial:
            return
        self._add(_serial_variants(serial), PLACEHOLDER_PAIRING_CODE)
        self._compile()

    def learn_asset_id(self, asset_id: str | None) -> None:
        """Add an asset ID the library learned after the scrubber was built."""
        if not asset_id:
            return
        self._add({asset_id}, PLACEHOLDER_ASSET_ID)
        self._compile()

    def scrub(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self._map[m.group(0).lower()], text)

    def scrub_exception(self, err: BaseException) -> str:
        return self.scrub(f"{type(err).__name__}: {err}")


def scrub(text: str, scrubber: Scrubber | None) -> str:
    """Scrub text, passing it through when there is no scrubber."""
    return text if scrubber is None else scrubber.scrub(text)
