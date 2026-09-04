import asyncio
import logging
from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError
from threading import Thread, Lock, Event

from .treadmill_data import TreadmillData

# Constants
NOTIFY_CHAR_UUID = "0000fba2-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000fba1-0000-1000-8000-00805f9b34fb"
# This firmware takes bare packets: no "4d00 <counter> <length>" transport
# wrapper in either direction. Wrapped writes are accepted at the ATT layer and
# then silently discarded. Status-poll packet, unwrapped:
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
    """
    Decides whether an advertisement belongs to a PitPat treadmill.

    :param name: Advertised (or cached) device name, may be empty.
    :param service_uuids: Iterable of advertised service UUID strings.
    :return: True if the device matches a known service or name hint.
    """
    uuids = {str(u).lower() for u in (service_uuids or [])}
    if uuids & VENDOR_SERVICE_UUIDS:
        return True
    lowered = (name or "").lower()
    return any(hint in lowered for hint in NAME_HINTS)


async def _scan_for_treadmills(timeout: float):
    """
    Scans for advertising treadmills.

    :param timeout: Scan duration in seconds.
    :return: List of (address, name, rssi) tuples.
    """
    found = []
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for device, adv in devices.values():
        name = adv.local_name or device.name or ""
        if _looks_like_treadmill(name, adv.service_uuids):
            found.append((device.address, name or "Unknown", adv.rssi))
    return found


async def _known_bluez_treadmills():
    """
    Lists treadmills BlueZ already knows about, advertising or not.

    A BLE peripheral goes silent while something holds a connection to it, so a
    treadmill left connected by the desktop Bluetooth panel never shows up in a
    scan. BlueZ still has the object, and connect() drops the stale link
    afterwards, so surface those here too.

    :return: List of (address, name, None) tuples; empty if BlueZ is unavailable.
    """
    try:
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            introspection = await bus.introspect("org.bluez", "/")
            proxy = bus.get_proxy_object("org.bluez", "/", introspection)
            manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await manager.call_get_managed_objects()

            found = []
            for interfaces in objects.values():
                props = interfaces.get("org.bluez.Device1")
                if not props or "Address" not in props:
                    continue
                name = props["Name"].value if "Name" in props else ""
                uuids = props["UUIDs"].value if "UUIDs" in props else []
                if _looks_like_treadmill(name, uuids):
                    found.append((props["Address"].value, name or "Unknown", None))
            return found
        finally:
            bus.disconnect()
    except Exception as e:
        logging.debug(f"Could not list known BlueZ devices: {e}")
        return []


async def _discover(timeout: float):
    """
    Collects treadmills from a fresh scan plus BlueZ's known devices.

    :param timeout: Scan duration in seconds.
    :return: List of (address, name, rssi) tuples, strongest signal first.
    """
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

    Blocks for up to `timeout` seconds and runs its own event loop, so it is safe
    to call before a BluetoothManager exists.

    :param timeout: Scan duration in seconds.
    :return: List of (address, name, rssi) tuples, strongest signal first.
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

    def __init__(self, device_address: str, on_disconnect=None, on_receive=None):
        """
        Initializes the BluetoothManager.

        :param device_address: Address of the Bluetooth device.
        :param on_disconnect: Callback function invoked upon disconnection.
        :param on_receive: Callback function invoked upon receiving data.
        """
        self.device_address = device_address
        self.on_disconnect = on_disconnect
        self.on_receive = on_receive

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
        """
        Schedules a coroutine to be run on the event loop.

        :param coro: The coroutine to execute.
        """
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _release_stale_link(self) -> bool:
        """
        Disconnects any pre-existing BlueZ connection to the device.

        A BLE peripheral stops advertising while connected, so if something else
        (the system Bluetooth panel, a previous session) holds the link, bleak's
        scan cannot find it and connect() fails with "was not found". Dropping
        that link makes the device advertise again.

        :return: True if an existing connection was dropped, False otherwise.
        """
        try:
            from dbus_fast.aio import MessageBus
            from dbus_fast.constants import BusType

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            try:
                introspection = await bus.introspect("org.bluez", "/")
                proxy = bus.get_proxy_object("org.bluez", "/", introspection)
                manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
                objects = await manager.call_get_managed_objects()

                target = self.device_address.upper()
                for path, interfaces in objects.items():
                    props = interfaces.get("org.bluez.Device1")
                    if not props:
                        continue
                    address = props.get("Address")
                    connected = props.get("Connected")
                    if not address or address.value.upper() != target:
                        continue
                    if not (connected and connected.value):
                        return False

                    logging.warning(
                        f"{self.device_address} is already connected at the BlueZ level; "
                        "dropping that link so it advertises again."
                    )
                    dev_introspection = await bus.introspect("org.bluez", path)
                    device = bus.get_proxy_object(
                        "org.bluez", path, dev_introspection
                    ).get_interface("org.bluez.Device1")
                    await device.call_disconnect()
                    await asyncio.sleep(2)
                    return True
                return False
            finally:
                bus.disconnect()
        except Exception as e:
            logging.warning(f"Could not check for a stale BlueZ link: {e}")
            return False

    def connect(self) -> bool:
        """
        Connects to the Bluetooth device and starts notifications.

        :return: True if connected successfully, False otherwise.
        """
        try:
            asyncio.run_coroutine_threadsafe(self._release_stale_link(), self.loop).result()
            future = asyncio.run_coroutine_threadsafe(self.client.connect(), self.loop)
            success = future.result()
            if success and self.client.is_connected:
                logging.info(f"Connected to {self.device_address}")
                self.run_coroutine(self.client.start_notify(NOTIFY_CHAR_UUID, self._notification_handler))
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
        """
        Disconnects from the Bluetooth device.

        :return: True if disconnected successfully, False otherwise.
        """
        try:
            if self.client.is_connected:
                self.run_coroutine(self.client.stop_notify(NOTIFY_CHAR_UUID))
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

    def is_connected(self) -> bool:
        """
        Checks if the Bluetooth device is connected.

        :return: True if connected, False otherwise.
        """
        return self.client.is_connected

    def _handle_disconnection(self, client):
        """
        Handles unexpected disconnections.

        :param client: The BleakClient instance.
        """
        logging.warning(f"Device {self.device_address} disconnected unexpectedly.")
        if self.on_disconnect:
            self.loop.call_soon_threadsafe(self.on_disconnect, self.device_address)

    def _notification_handler(self, sender: str, data: bytearray):
        """
        Handles incoming notifications from the BLE device.

        :param sender: The UUID of the characteristic sending the notification.
        :param data: The data received.
        """
        logging.info(f"Notification from {sender}: {data.hex()}")
        parsed_data = TreadmillData(data)
        if self.on_receive:
            self.loop.call_soon_threadsafe(self.on_receive, parsed_data)
        self.send_heartbeat()

    def send_heartbeat(self):
        """Sends a heartbeat signal to the BLE device."""
        try:
            with self.pending_lock:
                request = self.pending_request
                self.pending_request = None
                if request:
                    data_to_send = request.data
                    logging.info(f"Preparing to send pending data: {data_to_send.hex()}")
                    self.run_coroutine(self._write_data_and_set_request(data_to_send, request))
                else:
                    heartbeat_data = bytes.fromhex(HEARTBEAT_PACKET)
                    logging.debug(f"Preparing to send heartbeat: {heartbeat_data.hex()}")
                    self.run_coroutine(self._write_heartbeat(heartbeat_data))
        except Exception as e:
            logging.error(f"Error during heartbeat preparation: {e}")

    def send_data(self, data: bytes, timeout: float = 10.0) -> bool:
        """
        Sends data to the BLE device via the next heartbeat.

        Instead of sending data immediately, it queues the data to be sent when a heartbeat occurs.
        If multiple send_data() calls are made before the heartbeat sends data, only the latest data is sent.

        :param data: The data bytes to send.
        :param timeout: Maximum time to wait for the data to be sent.
        :return: True if data was sent successfully, False otherwise.
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

    async def _write_data_and_set_request(self, data: bytes, request):
        """
        Asynchronously writes the specified data to the BLE characteristic and sets the request's status.

        :param data: Data bytes to send.
        :param request: The SendDataRequest instance associated with this send.
        """
        try:
            await self.client.write_gatt_char(WRITE_CHAR_UUID, data)
            logging.info(f"Data sent: {data.hex()}")
            request.success = True
        except BleakError as e:
            logging.error(f"BleakError while sending data: {e}")
            request.success = False
        except Exception as e:
            logging.error(f"Unexpected error while sending data: {e}")
            request.success = False
        finally:
            request.event.set()

    async def _write_heartbeat(self, data: bytes):
        """
        Asynchronously writes the heartbeat data to the BLE characteristic.

        :param data: Heartbeat data as bytes.
        """
        try:
            await self.client.write_gatt_char(WRITE_CHAR_UUID, data)
            logging.info(f"Heartbeat sent: {data.hex()}")
        except BleakError as e:
            logging.error(f"BleakError while sending heartbeat: {e}")
        except Exception as e:
            logging.error(f"Unexpected error while sending heartbeat: {e}")

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
