import asyncio
import logging
from bleak import BleakClient
from bleak.exc import BleakError
from threading import Thread, Lock, Event

from .treadmill_data import TreadmillData

# Constants
NOTIFY_CHAR_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
HEARTBEAT_HEAD = "4d00"
HEARTBEAT_BODY = "056a05fdf843"

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


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
        """
        Schedules a coroutine to be run on the event loop.

        :param coro: The coroutine to execute.
        """
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def connect(self) -> bool:
        """
        Connects to the Bluetooth device and starts notifications.

        :return: True if connected successfully, False otherwise.
        """
        try:
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
        parsed_data = TreadmillData(data[4:])
        if self.on_receive:
            self.loop.call_soon_threadsafe(self.on_receive, parsed_data)
        self.send_heartbeat()

    def send_heartbeat(self):
        """Sends a heartbeat signal to the BLE device."""
        try:
            with self.counter_lock:
                current_counter = self.heartbeat_counter
                self.heartbeat_counter = (self.heartbeat_counter + 1) % 256  # Ensures it stays within byte range

            with self.pending_lock:
                request = self.pending_request
                self.pending_request = None
                if request:
                    data_to_send = (
                        bytes.fromhex(HEARTBEAT_HEAD) + 
                        bytes([(current_counter & 0xFF)]) + 
                        bytes([(len(request.data) & 0xFF)]) + 
                        request.data
                    )
                    logging.info(f"Preparing to send pending data: {data_to_send.hex()}")
                    self.run_coroutine(self._write_data_and_set_request(data_to_send, request))
                else:
                    heartbeat_data = (
                        bytes.fromhex(HEARTBEAT_HEAD) + 
                        bytes([(current_counter & 0xFF)]) + 
                        bytes.fromhex(HEARTBEAT_BODY)
                    )
                    logging.info(f"Preparing to send heartbeat: {heartbeat_data.hex()}")
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
