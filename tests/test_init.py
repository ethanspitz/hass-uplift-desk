"""Setup/unload tests for the Uplift Desk integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakError
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uplift_desk import async_setup_entry, async_unload_entry
from custom_components.uplift_desk.const import DOMAIN


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


async def test_setup_raises_config_entry_not_ready_when_no_ble_device(
    hass, config_entry
):
    """If the Bluetooth manager has no BLEDevice for the address, setup must defer."""
    with patch(
        "custom_components.uplift_desk.bluetooth.async_ble_device_from_address",
        return_value=None,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, config_entry)


async def test_setup_converts_bleak_error_to_config_entry_not_ready(
    hass, config_entry, make_ble_device
):
    """Transient BleakError during connect must surface as ConfigEntryNotReady."""
    ble_device = make_ble_device()

    with (
        patch(
            "custom_components.uplift_desk.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_connect",
            new=AsyncMock(side_effect=BleakError("GATT 133")),
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_disconnect",
            new=AsyncMock(),
        ),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, config_entry)


async def test_setup_registers_bluetooth_callback_and_stop_listener(
    hass, config_entry, make_ble_device
):
    """Successful setup must register the advertisement callback and HA-stop hook."""
    ble_device = make_ble_device()
    register_callback = MagicMock(return_value=lambda: None)

    with (
        patch(
            "custom_components.uplift_desk.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_connect",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.uplift_desk.bluetooth.async_register_callback",
            new=register_callback,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        result = await async_setup_entry(hass, config_entry)

    assert result is True
    register_callback.assert_called_once()


async def test_unload_disconnects_and_rediscovers(hass, config_entry, make_ble_device):
    """async_unload_entry should disconnect the coordinator and re-trigger discovery."""
    ble_device = make_ble_device()
    coordinator_disconnect = AsyncMock()
    rediscover = MagicMock()

    with (
        patch(
            "custom_components.uplift_desk.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_connect",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_disconnect",
            new=coordinator_disconnect,
        ),
        patch(
            "custom_components.uplift_desk.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.uplift_desk.bluetooth.async_rediscover_address",
            new=rediscover,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", new=AsyncMock(return_value=True)
        ),
    ):
        await async_setup_entry(hass, config_entry)
        unloaded = await async_unload_entry(hass, config_entry)

    assert unloaded is True
    coordinator_disconnect.assert_awaited()
    rediscover.assert_called_once_with(hass, "AA:BB:CC:DD:EE:FF")


async def test_ha_stop_event_disconnects_coordinator(hass, config_entry, make_ble_device):
    """Firing EVENT_HOMEASSISTANT_STOP must disconnect the coordinator cleanly."""
    ble_device = make_ble_device()
    coordinator_disconnect = AsyncMock()

    with (
        patch(
            "custom_components.uplift_desk.bluetooth.async_ble_device_from_address",
            return_value=ble_device,
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_connect",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.uplift_desk.UpliftDeskBluetoothCoordinator.async_disconnect",
            new=coordinator_disconnect,
        ),
        patch(
            "custom_components.uplift_desk.bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
    ):
        await async_setup_entry(hass, config_entry)

        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    coordinator_disconnect.assert_awaited()
