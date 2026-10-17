"""The fake client must behave like the real one where tests rely on it."""

from __future__ import annotations

import asyncio
import contextlib

from aosmith_ble import DeviceInfo

from .conftest import overlapping_clients
from .fakes import FakeClient


async def test_overlapping_connects_are_recorded_even_if_swallowed() -> None:
    client = FakeClient()
    client.delay = 0.01

    async def swallowed() -> None:
        # The integration maps every Exception, which would hide an assert.
        with contextlib.suppress(Exception):
            await client.connect()

    await asyncio.gather(swallowed(), swallowed())
    assert client.overlap_detected is True
    assert client.peak_connects == 2
    # Clear it: this overlap is the test's own doing, not a finding.
    client.overlap_detected = False


async def test_sequential_connects_are_not_an_overlap() -> None:
    client = FakeClient()
    await client.connect()
    await client.disconnect()
    await client.connect()
    assert client.overlap_detected is False
    assert client.peak_connects == 1


async def test_disconnect_resets_device_info_like_the_library() -> None:
    client = FakeClient()
    await client.connect()
    assert client.device_info.model == "HPTS-50"
    await client.disconnect()
    assert client.device_info == DeviceInfo()
    # A new session reads the identity again.
    await client.connect()
    assert client.device_info.model == "HPTS-50"


async def test_close_resets_device_info_like_the_library() -> None:
    client = FakeClient()
    await client.connect()
    await client.async_close()
    assert client.device_info == DeviceInfo()


async def test_scripted_device_info_is_what_connect_reads() -> None:
    client = FakeClient()
    client.device_info = DeviceInfo(model="HPTS-80", firmware=(6, 3))
    await client.connect()
    await client.disconnect()
    await client.connect()
    assert client.device_info.model == "HPTS-80"


def test_overlap_guard_reports_overlapping_clients() -> None:
    clean, overlapped = FakeClient(), FakeClient()
    overlapped.overlap_detected = True
    assert overlapping_clients([clean, overlapped]) == [overlapped]
    overlapped.overlap_detected = False
