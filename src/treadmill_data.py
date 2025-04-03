from dataclasses import dataclass
from typing import Optional

@dataclass
class TreadmillData:
    """Data class to store and process treadmill-related information."""
    current_speed: int = 0             # Raw value (16-bit)
    target_speed: int = 0              # Raw value (16-bit)
    distance: int = 0                  # In meters or miles
    current_incline: int = 0           # In degrees
    target_incline: int = 0            # In degrees
    heart_rate: int = 0                # In bpm
    steps: int = 0                     # Total steps
    calories: int = 0                  # In kcal
    duration: int = 0                  # In milliseconds
    duration_seconds: float = 0.0      # In seconds (adjusted by firmware version)
    cycle_id: int = 0                  # Movement cycle ID
    firmware_version: int = 0          # Firmware version number
    max_speed: int = 0                 # Raw value (16-bit)
    max_incline: int = 0               # Maximum incline in degrees
    device_type: int = 0               # Device type (lower 5 bits)
    run_walk_state: int = 0            # Run/walk state (bits 6-7 of byte 12)
    serial_number: Optional[str] = None  # Device serial number
    sensor_status: int = 0             # Sensor status
    carrying_idler: int = 0            # Carrying idler value
    battery: Optional[int] = None      # Battery level (0-255)
    remote_control_connect_states: Optional[int] = None  # Remote control states
    buzzer_control: Optional[int] = None  # Buzzer control (0 or 1)
    factory_rc_status: Optional[int] = None  # Factory remote control status
    device_activate: Optional[int] = None  # Device activation status
    unit_mode: int = 0                 # 0 = metric (kph), 1 = imperial (mph)
    is_connected: int = 0              # 0 = no, 1 = yes
    running_state: int = 3             # 0-3 (stopped, running, etc.)
    has_bracelet: bool = False         # Whether a bracelet is present
    bracelet_power: Optional[bool] = None  # Bracelet power status
    binary: int = 0                    # Binary flag from bits 1-2 of flags
    peak: Optional[int] = None         # Peak value (extended data)
    grain: Optional[int] = None        # Grain value (extended data)
    sum_steps: Optional[int] = None    # Sum of steps (extended data)
    real_electricity: Optional[int] = None  # Real-time electricity (extended data)
    real_rotate: Optional[int] = None  # Real-time rotation speed (extended data)
    real_electricity_steps: Optional[int] = None  # Electricity-based steps (extended data)
    ble_model: Optional[int] = None    # BLE model (extended data)
    ble_brand: Optional[int] = None    # BLE brand (extended data)
    checksum_valid: bool = False       # Whether checksum passed
    payload_hex: str = ""              # Hex representation of payload

    def __init__(self, payload: bytes, is_first: bool = False):
        """Initialize and process treadmill data from payload."""
        if len(payload) < 31:
            self.payload_hex = self._bytes_to_hex_string(payload)
            return

        # Store hex string and validate checksum
        self.payload_hex = self._bytes_to_hex_string(payload)
        self.checksum_valid = self._calc_checksum(payload, len(payload) - 2) == payload[-2]
        if not self.checksum_valid:
            return

        # Extract basic data with bit shifting
        length = payload[1]
        self.current_speed = ((payload[3] << 8) | payload[4]) & 0xFFFF
        self.target_speed = ((payload[5] << 8) | payload[6]) & 0xFFFF
        self.distance = (payload[7] << 24 | payload[8] << 16 | payload[9] << 8 | payload[10]) & 0xFFFFFFFF
        self.current_incline = payload[11] & 0xFF
        self.target_incline = min(payload[12] & 0xFF, 192)  # Cap at 192
        self.heart_rate = payload[13] & 0xFF
        self.steps = (payload[14] << 24 | payload[15] << 16 | payload[16] << 8 | payload[17]) & 0xFFFFFFFF
        self.calories = (payload[18] << 8 | payload[19]) & 0xFFFF
        self.duration = (payload[20] << 24 | payload[21] << 16 | payload[22] << 8 | payload[23]) & 0xFFFFFFFF
        self.cycle_id = payload[24] & 0xFF
        self.firmware_version = payload[25] & 0xFF
        flags = payload[26] & 0xFF
        self.max_speed = ((payload[27] << 8) | payload[28]) & 0xFFFF
        self.max_incline = payload[29] & 0xFF
        self.device_type = payload[30] & 31
        self.run_walk_state = (payload[12] >> 6) & 3

        # Extract flags
        self.unit_mode = 1 if (flags & 128) == 128 else 0
        self.is_connected = 1 if (flags & 1) == 1 else 0
        self.binary = (flags & 6) >> 1
        state_bits = flags & 24
        if state_bits == 24:
            self.running_state = 0
        elif state_bits == 8:
            self.running_state = 1
        elif state_bits == 16:
            self.running_state = 2
        else:
            self.running_state = 3
        self.has_bracelet = ((flags & 96) >> 5) != 3

        # Handle firmware version and first payload logic
        if self.firmware_version < 25 or is_first:
            min_length = 48 if self.firmware_version > 5 else 46
            start = 32 if self.firmware_version > 5 else 30
            if len(payload) >= min_length and is_first:
                sn_bytes = payload[start:start + 16]
                self.serial_number = sn_bytes.decode('ascii', errors='ignore')
            self.sensor_status = 0
        else:
            if len(payload) >= 52:
                self.ble_model = payload[48] & 0xFF
                self.ble_brand = payload[49] & 0xFF
                sn_bytes = payload[32:48]
                self.serial_number = sn_bytes.decode('ascii', errors='ignore')
            else:
                if length > 32:
                    self.carrying_idler = payload[32] & 0xFF
                if length > 35:
                    self.sensor_status = payload[33] & 0xFF
                if length > 39:
                    peak_raw = (payload[34] << 8 | payload[35]) & 0xFFFF
                    self.peak = 65535 - peak_raw if bin(peak_raw).startswith('0b1') else peak_raw
                    grain_raw = (payload[36] << 8 | payload[37]) & 0xFFFF
                    self.grain = 65535 - grain_raw if bin(grain_raw).startswith('0b1') else grain_raw
                    self.sum_steps = (payload[38] << 8 | payload[39]) & 0xFFFF
                if length > 42:
                    self.real_electricity = payload[40] & 0xFF
                    self.real_rotate = (payload[41] << 8 | payload[42]) & 0xFFFF
                if length > 44:
                    self.real_electricity_steps = (payload[43] << 8 | payload[44]) & 0xFFFF
                if length > 46:
                    self.battery = payload[45] & 0xFF
                    self.remote_control_connect_states = payload[46] & 0xFF
                if len(payload) > 47:
                    b10 = payload[47] & 0xFF
                    self.buzzer_control = b10 % 2
                    self.factory_rc_status = (b10 >> 1) % 2
                    self.device_activate = self._extract_bit_field(b10, 2, 2)

        # Additional logic based on firmware version
        if self.firmware_version > 5:
            self.bracelet_power = (payload[31] & 0xFF) <= 15
        self.duration_seconds = self.duration / 1000 if self.firmware_version > 19 else self.duration

    def _calc_checksum(self, payload: bytes, length: int) -> int:
        """Calculate XOR checksum over payload bytes from index 1 to length."""
        checksum = payload[1]
        for i in range(2, length):
            checksum ^= payload[i] & 0xFF
        return checksum

    def _bytes_to_hex_string(self, payload: bytes) -> str:
        """Convert byte array to hex string (e.g., '68 2F 00 ...')."""
        return " ".join(f"{byte:02X}" for byte in payload)

    def _extract_bit_field(self, value: int, start: int, length: int) -> int:
        """Extract a bit field from a value (start index, length in bits). Placeholder for m1."""
        mask = (1 << length) - 1
        return (value >> start) & mask

    def __repr__(self) -> str:
        """Return a formatted string representation of the treadmill data."""
        if not self.checksum_valid:
            return f"Invalid payload: {self.payload_hex}\nChecksum validation failed"
        if len(self.payload_hex) < 31 * 3:  # Approx length of hex string for 31 bytes
            return f"Invalid payload: {self.payload_hex}\nLength too short (< 31 bytes)"

        speed_unit = 'mph' if self.unit_mode == 1 else 'kph'
        distance_unit = 'mi' if self.unit_mode == 1 else 'km'
        lines = [
            f"Payload: {self.payload_hex}",
            f"Current Speed: {self.current_speed / 1000:.2f} {speed_unit}",
            f"Target Speed: {self.target_speed / 1000:.2f} {speed_unit}",
            f"Max Speed: {self.max_speed / 1000:.2f} {speed_unit}",
            f"Distance: {self.distance / 1000} {distance_unit}",
            f"Current Incline: {self.current_incline}°",
            f"Target Incline: {self.target_incline}°",
            f"Max Incline: {self.max_incline}°",
            f"Heart Rate: {self.heart_rate} bpm",
            f"Steps: {self.steps}",
            f"Calories: {self.calories} kcal",
            f"Duration: {self.duration_seconds:.1f} s",
            f"Cycle ID: {self.cycle_id}",
            f"Firmware Version: {self.firmware_version}",
            f"Device Type: {self.device_type}",
            f"Run/Walk State: {self.run_walk_state}",
            f"Serial Number: {self.serial_number}",
            f"Sensor Status: {self.sensor_status}",
            f"Carrying Idler: {self.carrying_idler}",
            f"Battery: {self.battery}%",
            f"Remote Control Connect States: {self.remote_control_connect_states}",
            f"Buzzer Control: {self.buzzer_control}",
            f"Factory RC Status: {self.factory_rc_status}",
            f"Device Activate: {self.device_activate}",
            f"Unit Mode: {'imperial' if self.unit_mode == 1 else 'metric'}",
            f"Wifi Connected: {'yes' if self.is_connected == 1 else 'no'}",
            f"Running State: {self.running_state}",
            f"Has Bracelet: {self.has_bracelet}",
            f"Bracelet Power: {self.bracelet_power}",
            f"Binary Flag: {self.binary}",
            f"Peak: {self.peak}",
            f"Grain: {self.grain}",
            f"Sum Steps: {self.sum_steps}",
            f"Real Electricity: {self.real_electricity}",
            f"Real Rotate: {self.real_rotate}",
            f"Real Electricity Steps: {self.real_electricity_steps}",
            f"BLE Model: {self.ble_model}",
            f"BLE Brand: {self.ble_brand}"
        ]
        return "\n".join(lines)

# Example usage
if __name__ == "__main__":
    payload = bytes.fromhex("6834000000000000000000000000000000000000000000002a1b001770000500746c4b613139316655705473733630330e004043")
    data = TreadmillData(payload, is_first=False)
    print(data)