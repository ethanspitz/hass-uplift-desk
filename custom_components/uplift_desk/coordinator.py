"""The uplift desk Bluetooth integration."""

from __future__ import annotations

import asyncio
import logging

from uplift import Desk

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

_LOGGER: logging.Logger = logging.getLogger(__name__)

type Uplift_Desk_DeskConfigEntry = ConfigEntry[UpliftDeskBluetoothCoordinator]

# Brief settle delay before reconnect attempts. Reconnecting too fast after
# disconnect can yield empty services on ESPHome BT proxies.
_RECONNECT_SETTLE_SEC = 1.0


class UpliftDeskBluetoothCoordinator(DataUpdateCoordinator):
    """Manage connection lifecycle and updates for an Uplift desk."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: Uplift_Desk_DeskConfigEntry,
        ble_device: BLEDevice,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, name="Uplift Desk", config_entry=config_entry)

        self._ble_device = ble_device
        self._desk = Desk(ble_device.address, config_entry.title)
        self._desk.register_callback(self._async_height_notify_callback)

        self._expected_connected = False
        self._connect_lock = asyncio.Lock()

    @property
    def desk_address(self) -> str:
        return self._desk.address

    @property
    def desk_name(self) -> str:
        return self._desk.name

    @property
    def desk_info(self) -> str:
        return str(self._desk)

    @property
    def is_connected(self) -> bool:
        client = self._desk.bleak_client
        return client is not None and client.is_connected

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Replace the cached BLEDevice from a fresh advertisement."""
        self._ble_device = ble_device

    async def async_connect(self) -> None:
        """Connect to the desk and prepare it for use."""
        async with self._connect_lock:
            self._expected_connected = True
            if self.is_connected:
                return

            ble_device = (
                bluetooth.async_ble_device_from_address(
                    self.hass, self.desk_address, connectable=True
                )
                or self._ble_device
            )

            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.name or self.desk_name or "Unknown",
                disconnected_callback=self._async_handle_disconnect,
                max_attempts=3,
            )
            self._desk.bleak_client = client

            try:
                await self._desk.start_notify()
                await self._desk.read_height()
            except BleakError:
                await self._async_disconnect_silently()
                raise

            self.async_set_updated_data(self._desk)

    async def async_disconnect(self) -> None:
        """User-/HA-initiated disconnect; suppresses the auto-reconnect path."""
        self._expected_connected = False
        await self._async_disconnect_silently()

    async def _async_disconnect_silently(self) -> None:
        """Tear down the BleakClient without changing _expected_connected."""
        client = self._desk.bleak_client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error disconnecting %s: %s", self.desk_info, err)
        finally:
            self._desk.bleak_client = None

    async def async_connect_if_expected(self) -> None:
        """Reconnect (when the device advertises again) if the user expects to be connected."""
        if not self._expected_connected or self.is_connected:
            return
        try:
            await self.async_connect()
        except BleakError as err:
            _LOGGER.debug("Reconnect attempt failed for %s: %s", self.desk_info, err)

    @callback
    def _async_handle_disconnect(self, client: BleakClient) -> None:
        """Bleak disconnect callback; reconnects are driven by the BT advertisement listener."""
        if self._desk.bleak_client is not client:
            # Stale callback for a client we've already replaced.
            return
        _LOGGER.debug("Desk %s disconnected", self.desk_info)
        self._desk.bleak_client = None
        self.async_update_listeners()

    async def async_sit(self) -> None:
        await self._async_run_command("move_to_sitting", self._desk.move_to_sitting)

    async def async_stand(self) -> None:
        await self._async_run_command("move_to_standing", self._desk.move_to_standing)

    async def _async_run_command(self, action_name: str, command) -> None:
        """Run a desk command, reconnecting once if the link or service cache is stale."""
        if not self.is_connected:
            await self.async_connect()

        try:
            await command()
        except BleakError as err:
            _LOGGER.warning(
                "Desk %s failed %s (%s); reconnecting and retrying once",
                self.desk_info, action_name, err,
            )
            await self._async_clear_cache_and_reconnect()
            await command()

    async def _async_clear_cache_and_reconnect(self) -> None:
        client = self._desk.bleak_client
        if isinstance(client, BleakClientWithServiceCache):
            try:
                await client.clear_cache()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error clearing service cache for %s: %s", self.desk_info, err)
        await self._async_disconnect_silently()
        await asyncio.sleep(_RECONNECT_SETTLE_SEC)
        await self.async_connect()

    async def _async_height_notify_callback(self, desk: Desk) -> None:
        self.async_set_updated_data(desk)
