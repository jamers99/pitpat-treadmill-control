import asyncio
import logging
from contextlib import asynccontextmanager
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from threading import Thread, Lock, Event

from .treadmill_data import TreadmillData

# Known hardware revisions (docs/FIRMWARE-VARIANT.md). "wrapped" means writes
# need the "4d00 <counter> <length>" transport wrapper and status frames need
# their leading 4 bytes stripped; the fba0 variant takes everything bare.
VARIANTS = {
    "ba04": {
        "label": "PitPat T01 (BA04) — original",
        "notify_uuid": "0000ff02-0000-1000-8000-00805f9b34fb",
        "write_uuid": "0000ff01-0000-1000-8000-00805f9b34fb",
        "wrapped": True,
    },
    "fba0": {
        "label": "PitPat T01, firmware 37 (fba0 variant)",
        "notify_uuid": "0000fba2-0000-1000-8000-00805f9b34fb",
        "write_uuid": "0000fba1-0000-1000-8000-00805f9b34fb",
        "wrapped": False,
    },
}
DEFAULT_VARIANT = "fba0"
# Unwrapped status-poll ("heartbeat") packet; wrapped variants add the
# transport header around this same body in BluetoothManager._wrap().
HEARTBEAT_PACKET = "6a05fdf843"

# Vendor GATT services a PitPat treadmill advertises: `fba0` on the firmware-37
# variant, the `ff00` family on the BA04 revision (docs/FIRMWARE-VARIANT.md §1).
# `1910` is advertised too but is a generic-looking service, so it only counts
# as a hint alongside the name.
VENDOR_SERVICE_UUIDS = {
    "0000fba0-0000-1000-8000-00805f9b34fb",
    "0000ff00-0000-1000-8000-00805f9b34fb",
}
NAME_HINTS = ("pitpat", "t01")
DISCOVERY_TIMEOUT = 8.0

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def _looks_like_treadmill(name: str, service_uuids) -> bool:
    """True if the advertisement matches a known service or name hint."""
    uuids = {str(u).lower() for u in (service_uuids or [])}
    if uuids & VENDOR_SERVICE_UUIDS:
        return True
    lowered = (name or "").lower()
    return any(hint in lowered for hint in NAME_HINTS)


async def _scan_for_treadmills(timeout: float):
    """Scans for advertising treadmills. Returns (address, name, rssi) tuples."""
    found = []
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for device, adv in devices.values():
        name = adv.local_name or device.name or ""
        if _looks_like_treadmill(name, adv.service_uuids):
            found.append((device.address, name or "Unknown", adv.rssi))
    return found


@asynccontextmanager
async def _bluez_interface(path: str, interface: str):
    """
    Yields the named D-Bus interface of a BlueZ object, closing the bus after.

    dbus_fast is imported lazily because it is a Linux-only dependency.
    """
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import BusType

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect("org.bluez", path)
        yield bus.get_proxy_object("org.bluez", path, introspection).get_interface(interface)
    finally:
        bus.disconnect()


async def _bluez_devices():
    """BlueZ's known devices as {path, address, name, uuids, connected} dicts."""
    async with _bluez_interface("/", "org.freedesktop.DBus.ObjectManager") as manager:
        objects = await manager.call_get_managed_objects()

    devices = []
    for path, interfaces in objects.items():
        props = interfaces.get("org.bluez.Device1")
        if not props or "Address" not in props:
            continue
        devices.append({
            "path": path,
            "address": props["Address"].value,
            "name": props["Name"].value if "Name" in props else "",
            "uuids": props["UUIDs"].value if "UUIDs" in props else [],
            "connected": bool(props["Connected"].value) if "Connected" in props else False,
        })
    return devices


async def _known_bluez_treadmills():
    """
    Treadmills BlueZ already knows about, advertising or not.

    A BLE peripheral goes silent while something holds a connection to it, so a
    treadmill left connected by the desktop Bluetooth panel never shows up in a
    scan. BlueZ still has the object, and connect() drops the stale link
    afterwards, so surface those here too.
    """
    try:
        devices = await _bluez_devices()
    except Exception as e:
        logging.debug(f"Could not list known BlueZ devices: {e}")
        return []
    return [
        (d["address"], d["name"] or "Unknown", None)
        for d in devices
        if _looks_like_treadmill(d["name"], d["uuids"])
    ]


async def _discover(timeout: float):
    """Collects treadmills from a fresh scan plus BlueZ's known devices."""
    results = {}
    for address, name, rssi in await _scan_for_treadmills(timeout) + await _known_bluez_treadmills():
        key = address.upper()
        # A scan result carries an RSSI, so it wins over the cached BlueZ entry.
        if key not in results or (rssi is not None and results[key][2] is None):
            results[key] = (address, name, rssi)
    return sorted(results.values(), key=lambda d: (d[2] is None, -(d[2] or 0)))


def discover_treadmills(timeout: float = DISCOVERY_TIMEOUT):
    """
    Finds nearby PitPat treadmills, so the address never has to be typed.

    Blocks for up to `timeout` seconds and runs its own event loop, so it is
    safe to call before a BluetoothManager exists.
    """
    try:
        devices = asyncio.run(_discover(timeout))
        logging.info(f"Discovery found {len(devices)} treadmill(s): {devices}")
        return devices
    except Exception as e:
        logging.error(f"Error during device discovery: {e}")
        return []


class BluetoothManager:
    """Manages Bluetooth connection, notifications, and heartbeats for the treadmill."""

    class SendDataRequest:
        """Encapsulates a send_data request."""

        def __init__(self, data: bytes):
            self.data = data
            self.event = Event()
            self.success = False

    def __init__(self, device_address: str, variant: str = DEFAULT_VARIANT, on_disconnect=None, on_receive=None):
        """
        Initializes the BluetoothManager.

        :param variant: Key into VARIANTS selecting the GATT UUIDs and wrapper mode.
        """
        self.device_address = device_address
        config = VARIANTS[variant]
        self.notify_uuid = config["notify_uuid"]
        self.write_uuid = config["write_uuid"]
        self.wrapped = config["wrapped"]
        self.on_disconnect = on_disconnect
        self.on_receive = on_receive

        # Transport wrapper counter, only used when self.wrapped.
        self.heartbeat_counter = 0
        self.counter_lock = Lock()

        # Pending data to be sent via send_data()
        self.pending_request = None
        self.pending_lock = Lock()

        # Initialize BleakClient with a disconnection callback
        self.client = BleakClient(self.device_address, disconnected_callback=self._handle_disconnection)

        # Start the asyncio event loop in a separate thread
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._start_event_loop, daemon=True)
        self.thread.start()

    def _start_event_loop(self):
        """Starts the asyncio event loop in a separate thread."""
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        except Exception as e:
            logging.error(f"Event loop stopped with exception: {e}")

    def run_coroutine(self, coro):
        """Schedules a coroutine to be run on the event loop."""
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _release_stale_link(self) -> bool:
        """
        Disconnects any pre-existing BlueZ connection to the device.

        A BLE peripheral stops advertising while connected, so if something else
        (the system Bluetooth panel, a previous session) holds the link, bleak's
        scan cannot find it and connect() fails with "was not found". Dropping
        that link makes the device advertise again.
        """
        try:
            devices = await _bluez_devices()
        except Exception as e:
            logging.warning(f"Could not check for a stale BlueZ link: {e}")
            return False

        target = self.device_address.upper()
        device = next((d for d in devices if d["address"].upper() == target), None)
        if not device or not device["connected"]:
            return False

        logging.warning(
            f"{self.device_address} is already connected at the BlueZ level; "
            "dropping that link so it advertises again."
        )
        async with _bluez_interface(device["path"], "org.bluez.Device1") as dev:
            await dev.call_disconnect()
        await asyncio.sleep(2)
        return True

    def connect(self) -> bool:
        """Connects to the Bluetooth device and starts notifications."""
        try:
            asyncio.run_coroutine_threadsafe(self._release_stale_link(), self.loop).result()
            future = asyncio.run_coroutine_threadsafe(self.client.connect(), self.loop)
            success = future.result()
            if success and self.client.is_connected:
                logging.info(f"Connected to {self.device_address}")
                self.run_coroutine(self.client.start_notify(self.notify_uuid, self._notification_handler))
            else:
                logging.error(f"Failed to connect to {self.device_address}")
            return self.client.is_connected
        except BleakError as e:
            logging.error(f"BleakError during connection: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error during connection: {e}")
            return False

    def disconnect(self) -> bool:
        """Disconnects from the Bluetooth device."""
        try:
            if self.client.is_connected:
                self.run_coroutine(self.client.stop_notify(self.notify_uuid))
                future = asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)
                success = future.result()
                if success:
                    logging.info(f"Disconnected from {self.device_address}")
                else:
                    logging.error(f"Failed to disconnect from {self.device_address}")
                return not self.client.is_connected
            else:
                logging.info(f"Already disconnected from {self.device_address}")
                return True
        except BleakError as e:
            logging.error(f"BleakError during disconnection: {e}")
            return False
        except Exception as e:
            logging.error(f"Unexpected error during disconnection: {e}")
            return False

    def _handle_disconnection(self, client):
        """Handles unexpected disconnections."""
        logging.warning(f"Device {self.device_address} disconnected unexpectedly.")
        if self.on_disconnect:
            self.loop.call_soon_threadsafe(self.on_disconnect, self.device_address)

    def _notification_handler(self, sender: str, data: bytearray):
        """Handles incoming notifications from the BLE device."""
        logging.info(f"Notification from {sender}: {data.hex()}")
        parsed_data = TreadmillData(data[4:] if self.wrapped else data)
        if self.on_receive:
            self.loop.call_soon_threadsafe(self.on_receive, parsed_data)
        self.send_heartbeat()

    def _wrap(self, body: bytes) -> bytes:
        """Adds the "4d00 <counter> <length>" transport header, if this variant needs one."""
        if not self.wrapped:
            return body
        with self.counter_lock:
            counter = self.heartbeat_counter
            self.heartbeat_counter = (self.heartbeat_counter + 1) % 256
        return bytes.fromhex("4d00") + bytes([counter, len(body) & 0xFF]) + body

    def send_heartbeat(self):
        """Sends the queued command if there is one, otherwise a plain status poll."""
        try:
            with self.pending_lock:
                request, self.pending_request = self.pending_request, None
            data = self._wrap(request.data if request else bytes.fromhex(HEARTBEAT_PACKET))
            self.run_coroutine(self._write(data, request))
        except Exception as e:
            logging.error(f"Error during heartbeat preparation: {e}")

    def send_data(self, data: bytes, timeout: float = 10.0) -> bool:
        """
        Sends data to the BLE device via the next heartbeat.

        Queued rather than sent immediately, and only the latest queued data is
        sent if several calls land before the next heartbeat.
        """
        request = self.SendDataRequest(data)
        with self.pending_lock:
            if self.pending_request and not self.pending_request.event.is_set():
                # Invalidate the previous request
                logging.warning("A send_data() operation is already in progress. Overwriting with new data.")
                self.pending_request.success = False
                self.pending_request.event.set()

            # Set the new request as pending
            self.pending_request = request
            logging.info(f"Data queued for sending: {data.hex()}")

        # Wait for the event to be set by send_heartbeat()
        success = request.event.wait(timeout=timeout)
        if success:
            return request.success
        else:
            logging.error("Timeout waiting for data to be sent.")
            return False

    async def _write(self, data: bytes, request=None):
        """Writes to the control characteristic, completing `request` if one is waiting on it."""
        try:
            await self.client.write_gatt_char(self.write_uuid, data)
            logging.info(f"Sent: {data.hex()}")
            if request:
                request.success = True
        except Exception as e:
            logging.error(f"Error while sending {data.hex()}: {e}")
        finally:
            if request:
                request.event.set()

    def shutdown(self):
        """Shuts down the BluetoothManager, ensuring all resources are cleaned up."""
        try:
            if self.client.is_connected:
                self.disconnect()
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join()
            logging.info("BluetoothManager shutdown successfully.")
        except Exception as e:
            logging.error(f"Error during shutdown: {e}")
