"""Unit tests for UpliftDeskBluetoothCoordinator."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplift_desk.const import DOMAIN
from custom_components.uplift_desk.coordinator import UpliftDeskBluetoothCoordinator


@pytest.fixture
def config_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Desk",
        data={"address": "AA:BB:CC:DD:EE:FF"},
        unique_id="AA:BB:CC:DD:EE:FF",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def coordinator(hass, config_entry, make_ble_device) -> UpliftDeskBluetoothCoordinator:
    ble_device = make_ble_device()
    return UpliftDeskBluetoothCoordinator(hass, config_entry, ble_device)


# ---------------------------------------------------------------------------
# async_connect
# ---------------------------------------------------------------------------
async def test_async_connect_establishes_and_starts_notify(
    coordinator, patch_establish_connection
):
    """A clean connect calls start_notify and read_height, sets _expected_connected."""
    await coordinator.async_connect()

    assert coordinator._expected_connected is True
    assert coordinator.is_connected is True
    coordinator._desk.start_notify.assert_awaited_once()
    coordinator._desk.read_height.assert_awaited_once()
    assert len(patch_establish_connection["calls"]) == 1
    call = patch_establish_connection["calls"][0]
    assert call["max_attempts"] == 3
    # Bound methods aren't `is`-equal across accesses; compare by self+func instead.
    cb = call["disconnected_callback"]
    assert cb.__self__ is coordinator and cb.__func__ is type(coordinator)._async_handle_disconnect


async def test_async_connect_is_noop_when_already_connected(
    coordinator, patch_establish_connection
):
    """Second connect on a healthy link does nothing."""
    await coordinator.async_connect()
    coordinator._desk.start_notify.reset_mock()
    coordinator._desk.read_height.reset_mock()

    await coordinator.async_connect()

    coordinator._desk.start_notify.assert_not_awaited()
    coordinator._desk.read_height.assert_not_awaited()
    assert len(patch_establish_connection["calls"]) == 1  # not called again


async def test_async_connect_propagates_start_notify_failure_and_disconnects(
    coordinator, patch_establish_connection
):
    """start_notify failure tears down the just-established client and re-raises."""
    coordinator._desk.start_notify.side_effect = BleakError("notify failed")

    with pytest.raises(BleakError, match="notify failed"):
        await coordinator.async_connect()

    # Client was torn down so the next connect attempt starts fresh.
    assert coordinator._desk.bleak_client is None


async def test_async_connect_calls_pair_before_start_notify(
    coordinator, patch_establish_connection
):
    """Pairing must happen between establish_connection and start_notify so the
    bonded link is in place before any GATT work on Linak-style desks."""
    await coordinator.async_connect()
    client = coordinator._desk.bleak_client
    client.pair.assert_awaited_once()


async def test_async_connect_swallows_pair_failure(
    coordinator, patch_establish_connection
):
    """If pair() raises (backend doesn't support it, or not needed), connect proceeds."""
    # We need to patch pair on the client created by establish_connection. Easiest
    # way: hook into the establish_connection mock so the new client's pair raises.
    from custom_components.uplift_desk import coordinator as coord_mod

    original = coord_mod.establish_connection

    async def _establish_with_failing_pair(*args, **kwargs):
        client = await original(*args, **kwargs)
        client.pair.side_effect = BleakError("pair not supported")
        return client

    import unittest.mock as _mock
    with _mock.patch.object(coord_mod, "establish_connection", _establish_with_failing_pair):
        await coordinator.async_connect()

    # start_notify must still have been called.
    coordinator._desk.start_notify.assert_awaited_once()
    assert coordinator.is_connected is True


async def test_async_connect_swallows_initial_read_height_failure(
    coordinator, patch_establish_connection
):
    """A best-effort initial read_height failure must not abort connect."""
    coordinator._desk.read_height.side_effect = BleakError(
        "Bluetooth GATT Error address=... handle=15 error=133 description=Error"
    )

    # Must not raise.
    await coordinator.async_connect()

    # Connection still up; subsequent commands can proceed.
    assert coordinator.is_connected is True
    assert coordinator._desk.bleak_client is not None
    coordinator._desk.start_notify.assert_awaited_once()


async def test_async_connect_lock_serializes_concurrent_calls(
    coordinator, patch_establish_connection
):
    """Two concurrent async_connect callers don't both establish a connection."""
    await asyncio.gather(coordinator.async_connect(), coordinator.async_connect())
    assert len(patch_establish_connection["calls"]) == 1


# ---------------------------------------------------------------------------
# Disconnect callback
# ---------------------------------------------------------------------------
async def test_disconnect_callback_does_not_null_bleak_client(
    coordinator, patch_establish_connection
):
    """Disconnect callback must keep the client reference so retry surfaces BleakError."""
    await coordinator.async_connect()
    client = coordinator._desk.bleak_client
    assert client is not None

    coordinator._async_handle_disconnect(client)

    # PR #5: reference is preserved; only client.is_connected goes False.
    assert coordinator._desk.bleak_client is client


async def test_disconnect_callback_ignores_stale_client(
    coordinator, patch_establish_connection
):
    """A late disconnect for an already-replaced client must not affect live state."""
    await coordinator.async_connect()
    current = coordinator._desk.bleak_client
    stale = MagicMock(name="stale_client")

    coordinator._async_handle_disconnect(stale)

    assert coordinator._desk.bleak_client is current  # untouched


async def test_is_connected_reflects_underlying_client_after_disconnect(
    coordinator, patch_establish_connection
):
    """Once the client reports disconnected, coordinator.is_connected is False."""
    await coordinator.async_connect()
    coordinator._desk.bleak_client.is_connected = False

    assert coordinator.is_connected is False


# ---------------------------------------------------------------------------
# async_disconnect / async_connect_if_expected
# ---------------------------------------------------------------------------
async def test_async_disconnect_clears_expected_connected(
    coordinator, patch_establish_connection
):
    await coordinator.async_connect()
    assert coordinator._expected_connected is True

    await coordinator.async_disconnect()

    assert coordinator._expected_connected is False
    assert coordinator._desk.bleak_client is None


async def test_async_connect_if_expected_no_op_when_not_expected(coordinator):
    """If user has explicitly disconnected, advertisement should not reconnect."""
    coordinator._expected_connected = False

    with patch.object(coordinator, "async_connect", new=AsyncMock()) as connect_mock:
        await coordinator.async_connect_if_expected()
        connect_mock.assert_not_awaited()


async def test_async_connect_if_expected_no_op_when_already_connected(
    coordinator, patch_establish_connection
):
    await coordinator.async_connect()

    with patch.object(coordinator, "async_connect", new=AsyncMock()) as connect_mock:
        await coordinator.async_connect_if_expected()
        connect_mock.assert_not_awaited()


async def test_async_connect_if_expected_swallows_bleak_errors(
    coordinator, patch_establish_connection
):
    """An advertisement-driven reconnect attempt that fails must not raise."""
    coordinator._expected_connected = True

    with patch.object(
        coordinator, "async_connect", new=AsyncMock(side_effect=BleakError("nope"))
    ):
        await coordinator.async_connect_if_expected()


# ---------------------------------------------------------------------------
# Commands and retry
# ---------------------------------------------------------------------------
async def test_async_sit_runs_command_when_connected(
    coordinator, patch_establish_connection
):
    await coordinator.async_connect()
    await coordinator.async_sit()
    coordinator._desk.move_to_sitting.assert_awaited_once()


async def test_async_stand_runs_command_when_connected(
    coordinator, patch_establish_connection
):
    await coordinator.async_connect()
    await coordinator.async_stand()
    coordinator._desk.move_to_standing.assert_awaited_once()


async def test_command_connects_first_when_not_connected(
    coordinator, patch_establish_connection
):
    """Pressing a button while disconnected triggers a connect before the command."""
    await coordinator.async_sit()

    coordinator._desk.move_to_sitting.assert_awaited_once()
    assert len(patch_establish_connection["calls"]) == 1


async def test_command_retries_once_on_bleak_error(
    coordinator, patch_establish_connection, monkeypatch
):
    """A BleakError on the command triggers a single reconnect+retry."""
    await coordinator.async_connect()

    # First call fails, second call succeeds.
    coordinator._desk.move_to_sitting.side_effect = [BleakError("transient"), None]

    # Replace the sleep so the test isn't slow.
    from custom_components.uplift_desk import coordinator as coord_mod

    monkeypatch.setattr(coord_mod.asyncio, "sleep", AsyncMock())

    await coordinator.async_sit()

    assert coordinator._desk.move_to_sitting.await_count == 2
    # Reconnect path went through clear_cache and a fresh establish_connection.
    assert len(patch_establish_connection["calls"]) == 2


async def test_command_propagates_when_retry_also_fails(
    coordinator, patch_establish_connection, monkeypatch
):
    """A second BleakError on the retry attempt is propagated to the caller."""
    await coordinator.async_connect()
    coordinator._desk.move_to_sitting.side_effect = BleakError("still broken")

    from custom_components.uplift_desk import coordinator as coord_mod
    monkeypatch.setattr(coord_mod.asyncio, "sleep", AsyncMock())

    with pytest.raises(BleakError, match="still broken"):
        await coordinator.async_sit()
    assert coordinator._desk.move_to_sitting.await_count == 2


async def test_clear_cache_is_called_during_retry(
    coordinator, patch_establish_connection, monkeypatch
):
    """The retry path invalidates the BleakClientWithServiceCache cache."""
    await coordinator.async_connect()
    cached_client = coordinator._desk.bleak_client
    assert isinstance(cached_client, BleakClientWithServiceCache)

    coordinator._desk.move_to_standing.side_effect = [BleakError("stale"), None]

    from custom_components.uplift_desk import coordinator as coord_mod
    monkeypatch.setattr(coord_mod.asyncio, "sleep", AsyncMock())

    await coordinator.async_stand()

    cached_client.clear_cache.assert_awaited_once()


# ---------------------------------------------------------------------------
# update_ble_device
# ---------------------------------------------------------------------------
def test_update_ble_device_replaces_cached_device(coordinator, make_ble_device):
    fresh = make_ble_device(name="Refreshed")
    coordinator.update_ble_device(fresh)
    assert coordinator._ble_device is fresh
