"""Test fixtures for the Uplift Desk integration."""

from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stand-in for the upstream `uplift` library.
#
# The real package isn't a Python-typed library and pulls in bleak transitively.
# For tests we only need the surface the integration uses: a `Desk` class with
# the same attributes and the same async methods. Behaviour is whatever the
# tests configure via AsyncMocks.
# ---------------------------------------------------------------------------
def _install_fake_uplift_module() -> None:
    if "uplift" in sys.modules:
        return

    module = types.ModuleType("uplift")

    class Desk:  # noqa: D401  (test stand-in, not docstring-shaped)
        """Test stand-in for uplift.Desk."""

        def __init__(self, address: str, name: str, bleak_client=None) -> None:
            self.address = address
            self.name = name
            self.bleak_client = bleak_client
            self._height: float = 0.0
            self._moving = False
            self._callbacks: list[Callable[["Desk"], Awaitable[None]]] = []

            # Pre-bound AsyncMocks let tests assert on calls.
            self.start_notify = AsyncMock(name="Desk.start_notify")
            self.stop_notify = AsyncMock(name="Desk.stop_notify")
            self.read_height = AsyncMock(name="Desk.read_height")
            self.move_to_sitting = AsyncMock(name="Desk.move_to_sitting")
            self.move_to_standing = AsyncMock(name="Desk.move_to_standing")

        @property
        def height(self) -> float:
            return self._height

        @property
        def moving(self) -> bool:
            return self._moving

        def register_callback(self, cb: Callable[["Desk"], Awaitable[None]]) -> None:
            self._callbacks.append(cb)

        def __str__(self) -> str:
            return f"{self.name} - {self.address}"

    module.Desk = Desk
    sys.modules["uplift"] = module


_install_fake_uplift_module()


# ---------------------------------------------------------------------------
# Bleak / bleak-retry-connector helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def make_ble_device():
    """Factory for fake BLEDevice objects with the attributes the coordinator reads."""
    from bleak.backends.device import BLEDevice

    def _make(address: str = "AA:BB:CC:DD:EE:FF", name: str = "Test Desk") -> BLEDevice:
        return BLEDevice(address=address, name=name, details=None)  # type: ignore[call-arg]

    return _make


@pytest.fixture(autouse=True)
def stub_async_ble_device_from_address(monkeypatch):
    """Default-stub HA's BluetoothManager lookup so the coordinator's fallback to its
    cached BLEDevice runs. Tests that need a different value can override the patch."""
    from custom_components.uplift_desk import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod.bluetooth, "async_ble_device_from_address", lambda *a, **kw: None
    )


@pytest.fixture
def fake_bleak_client():
    """Build a fake BleakClient-ish object backing the connection."""

    def _make(connected: bool = True) -> MagicMock:
        client = MagicMock(name="FakeBleakClient")
        client.is_connected = connected
        client.disconnect = AsyncMock(name="disconnect")
        client.clear_cache = AsyncMock(name="clear_cache", return_value=True)
        client.pair = AsyncMock(name="pair", return_value=True)
        return client

    return _make


@pytest.fixture
def patch_establish_connection(monkeypatch, fake_bleak_client):
    """Patch coordinator.establish_connection to return our fake client.

    Returns a tuple of (mock, last_kwargs_holder); tests can assert on the calls.
    """
    from custom_components.uplift_desk import coordinator as coord_mod

    holder = {"calls": []}

    async def _fake_establish_connection(client_cls, device, name, **kwargs):
        holder["calls"].append({"client_cls": client_cls, "device": device, "name": name, **kwargs})
        client = fake_bleak_client(connected=True)
        # Make BleakClientWithServiceCache isinstance check pass so clear_cache path runs.
        client.__class__ = coord_mod.BleakClientWithServiceCache  # type: ignore[assignment]
        return client

    monkeypatch.setattr(coord_mod, "establish_connection", _fake_establish_connection)
    return holder
