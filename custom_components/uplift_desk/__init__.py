"""The Uplift Desk integration."""

from __future__ import annotations

import logging

from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import (
    UpliftDeskBluetoothCoordinator,
    Uplift_Desk_DeskConfigEntry,
)

_PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: Uplift_Desk_DeskConfigEntry) -> bool:
    """Set up Uplift Desk from a config entry."""
    address: str = entry.data[CONF_ADDRESS]

    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="device_not_found_error",
            translation_placeholders={"address": address},
        )

    coordinator = UpliftDeskBluetoothCoordinator(hass, entry, ble_device)
    entry.runtime_data = coordinator

    try:
        await coordinator.async_connect()
    except BleakError as err:
        # Transient errors (e.g. ESP_GATT_ERROR 133 via ESPHome BT proxies) —
        # let HA retry setup with backoff.
        await coordinator.async_disconnect()
        raise ConfigEntryNotReady(
            f"Error setting up desk {entry.title} ({address}): {err}"
        ) from err

    @callback
    def _async_bluetooth_callback(
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Reconnect when the desk advertises again after a drop."""
        coordinator.update_ble_device(service_info.device)
        hass.async_create_task(coordinator.async_connect_if_expected())

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _async_bluetooth_callback,
            BluetoothCallbackMatcher({ADDRESS: address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    async def _async_stop(event: Event) -> None:
        await coordinator.async_disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: Uplift_Desk_DeskConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        coordinator: UpliftDeskBluetoothCoordinator = entry.runtime_data
        await coordinator.async_disconnect()
        bluetooth.async_rediscover_address(hass, coordinator.desk_address)

    return unload_ok
