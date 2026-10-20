"""Finding and talking to a PitPat treadmill over BLE."""
import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from bleak import BleakClient, BleakScanner

from . import protocol
from .protocol import Status

log = logging.getLogger(__name__)

# Vendor services a treadmill advertises: fba0 on the firmware-37 variant, the
# ff00 family on BA04.
VENDOR_SERVICES = {
    "0000fba0-0000-1000-8000-00805f9b34fb",
    "0000ff00-0000-1000-8000-00805f9b34fb",
}
NAME_HINTS = ("pitpat", "t01")


@dataclass
class Device:
    address: str
    name: str
    rssi: Optional[int] = None  # None for a device BlueZ knows but that isn't advertising


def _looks_like_treadmill(name: str, uuids) -> bool:
    if {str(u).lower() for u in uuids or ()} & VENDOR_SERVICES:
        return True
    return any(hint in (name or "").lower() for hint in NAME_HINTS)


async def discover(timeout: float = 6.0) -> list[Device]:
    """Scans for treadmills, strongest signal first.

    On Linux this also lists treadmills BlueZ already knows about, since one
    held by a stale connection stops advertising and would never be seen.
    """
    found: dict[str, Device] = {}
    for device, adv in (await BleakScanner.discover(timeout=timeout, return_adv=True)).values():
        name = adv.local_name or device.name or ""
        if _looks_like_treadmill(name, adv.service_uuids):
            found[device.address.upper()] = Device(device.address, name or "Unknown", adv.rssi)
    for d in await _bluez_devices():
        if d["address"].upper() not in found and _looks_like_treadmill(d["name"], d["uuids"]):
            found[d["address"].upper()] = Device(d["address"], d["name"] or "Unknown")
    return sorted(found.values(), key=lambda d: (d.rssi is None, -(d.rssi or 0)))


# --- BlueZ helpers (Linux only; no-ops elsewhere) ---------------------------

async def _bluez_call(path: str, interface: str, method: str):
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        obj = bus.get_proxy_object("org.bluez", path, await bus.introspect("org.bluez", path))
        return await getattr(obj.get_interface(interface), f"call_{method}")()
    finally:
        bus.disconnect()


async def _bluez_devices() -> list[dict]:
    if sys.platform != "linux":
        return []
    try:
        objects = await _bluez_call("/", "org.freedesktop.DBus.ObjectManager", "get_managed_objects")
    except Exception as e:
        log.debug("Could not list BlueZ devices: %s", e)
        return []
    devices = []
    for path, interfaces in objects.items():
        props = interfaces.get("org.bluez.Device1")
        if props and "Address" in props:
            devices.append({
                "path": path,
                "address": props["Address"].value,
                "name": props["Name"].value if "Name" in props else "",
                "uuids": props["UUIDs"].value if "UUIDs" in props else [],
                "connected": bool(props["Connected"].value) if "Connected" in props else False,
            })
    return devices


async def _release_stale_link(address: str) -> None:
    """Drops an existing BlueZ connection (e.g. from the desktop Bluetooth panel).

    A connected BLE device stops advertising, so bleak would fail with "not found".
    """
    for d in await _bluez_devices():
        if d["address"].upper() == address.upper() and d["connected"]:
            log.warning("%s already connected at the BlueZ level; dropping that link", address)
            await _bluez_call(d["path"], "org.bluez.Device1", "disconnect")
            await asyncio.sleep(2)
            return


# --- Connection --------------------------------------------------------------

class Treadmill:
    """A connection to one treadmill.

    The treadmill sends a status frame about once a second. Each frame is
    answered with one write: the queued command if there is one, otherwise a
    heartbeat. So commands go out within about a second of being queued.
    """

    def __init__(
        self,
        address: str,
        on_status: Callable[[Status], None],
        on_disconnect: Callable[[], None],
        variant: Optional[str] = None,
    ):
        """`variant` is a protocol.VARIANTS key, or None to detect it on connect."""
        self.address = address
        self.variant = variant
        self.on_status = on_status
        self.on_disconnect = on_disconnect
        self.counter = 0
        self.pending: Optional[tuple[bytes, asyncio.Future]] = None
        self.client: Optional[BleakClient] = None
        self.closing = False

    async def connect(self) -> None:
        """Connects and starts listening. Raises on failure."""
        loop = asyncio.get_running_loop()
        await _release_stale_link(self.address)
        self.client = BleakClient(
            self.address,
            timeout=20,
            # Backends may call this off the event loop thread.
            disconnected_callback=lambda _: loop.call_soon_threadsafe(self._disconnected),
        )
        await self.client.connect()
        services = self.client.services
        candidates = [self.variant] if self.variant else list(protocol.VARIANTS)
        for key in candidates:
            config = protocol.VARIANTS[key]
            char = services.get_characteristic(config["write_uuid"])
            if char and services.get_characteristic(config["notify_uuid"]):
                break
        else:
            await self.disconnect()
            raise RuntimeError("This device doesn't look like a supported PitPat treadmill")
        self.variant = key
        self.write_uuid = config["write_uuid"]
        self.wrapped = config["wrapped"]
        self.write_response = "write" in char.properties
        await self.client.start_notify(config["notify_uuid"], self._on_notify)
        log.info("Connected to %s (%s variant)", self.address, key)

    async def disconnect(self) -> None:
        self.closing = True
        if self.client and self.client.is_connected:
            await self.client.disconnect()

    def send(self, packet: bytes) -> asyncio.Future:
        """Queues a command for the next write, replacing any not yet sent."""
        if self.pending:
            self.pending[1].cancel()
        future = asyncio.get_running_loop().create_future()
        # Callers needn't await it; mark a failure as seen so asyncio doesn't warn.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self.pending = (packet, future)
        log.info("Queued %s", packet.hex())
        return future

    def _disconnected(self) -> None:
        if not self.closing:
            log.warning("Lost connection to %s", self.address)
            self.on_disconnect()

    async def _on_notify(self, _char, data: bytearray) -> None:
        log.debug("Status %s", data.hex())
        status = Status.parse(bytes(data[4:] if self.wrapped else data))
        if status:
            self.on_status(status)

        packet, future = self.pending or (protocol.HEARTBEAT, None)
        self.pending = None
        if self.wrapped:
            packet = protocol.wrap(packet, self.counter)
            self.counter = (self.counter + 1) % 256
        try:
            await self.client.write_gatt_char(self.write_uuid, packet, response=self.write_response)
            if future and not future.done():
                log.info("Sent %s", packet.hex())
                future.set_result(None)
        except Exception as e:
            log.error("Write of %s failed: %s", packet.hex(), e)
            if future and not future.done():
                future.set_exception(e)
