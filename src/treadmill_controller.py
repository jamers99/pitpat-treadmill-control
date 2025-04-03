from enum import IntEnum

class CommandType(IntEnum):
    STOP = 0
    PAUSE = 2
    START_OR_SET = 4

class TreadmillController:
    """A module for generating control packets for a treadmill."""

    START_BYTE = 0x6A
    LENGTH = 0x17
    END_BYTE = 0x43
    DEFAULT_WEIGHT = 80          # Default weight in kg (0x50)
    DEFAULT_USER_ID = 58965456623  # Default user ID (64-bit)

    @staticmethod
    def _calculate_checksum(packet):
        """Calculate the checksum (XOR) over bytes 1 to 20."""
        checksum = 0
        for byte in packet[1:21]:
            checksum ^= byte
        return checksum

    @staticmethod
    def _create_packet(target_speed, magical_i11, incline, cmd_type, is_kph=True):
        """
        Create a 23-byte packet based on the parameters.
        
        Args:
            target_speed (int): Target speed (e.g., 1100 for 1.10 kph/mph), 0-65535.
            magical_i11 (int): Acceleration ramp time in seconds (1 or 5).
            incline (int): Incline (0-255).
            cmd_type (int): Command type (4=Start/Set Speed, 2=Pause, 0=Stop).
            is_kph (bool): True for kph, False for mph.
        
        Returns:
            bytes: 23-byte packet.
        
        Raises:
            ValueError: If parameters are out of valid range.
        """
        if not 0 <= target_speed <= 65535:
            raise ValueError("target_speed must be between 0 and 65535")
        if magical_i11 not in [1, 5]:
            raise ValueError("magical_i11 must be 1 or 5")
        if not 0 <= incline <= 255:
            raise ValueError("incline must be between 0 and 255")
        if cmd_type not in [CommandType.STOP, CommandType.PAUSE, CommandType.START_OR_SET]:
            raise ValueError("cmd_type must be 0, 2, or 4")

        packet = bytearray(23)
        packet[0] = TreadmillController.START_BYTE
        packet[1] = TreadmillController.LENGTH
        packet[2:6] = [0, 0, 0, 0]
        packet[6] = (target_speed >> 8) & 0xFF
        packet[7] = target_speed & 0xFF
        packet[8] = magical_i11
        packet[9] = incline
        packet[10] = TreadmillController.DEFAULT_WEIGHT
        packet[11] = 0
        cmd_byte = cmd_type
        packet[12] = cmd_byte & 0xF7 if is_kph else cmd_byte | 0x08
        user_id = TreadmillController.DEFAULT_USER_ID
        for i in range(8):
            packet[13 + i] = (user_id >> (56 - i * 8)) & 0xFF
        packet[21] = TreadmillController._calculate_checksum(packet)
        packet[22] = TreadmillController.END_BYTE
        return bytes(packet)

    @staticmethod
    def start(target_speed=1000, incline=0, is_kph=True):
        """Generate a Start packet."""
        return TreadmillController._create_packet(target_speed, 1, incline, CommandType.START_OR_SET, is_kph)

    @staticmethod
    def pause(incline=0, is_kph=True):
        """Generate a Pause packet."""
        return TreadmillController._create_packet(0, 1, incline, CommandType.PAUSE, is_kph)

    @staticmethod
    def stop(is_kph=True):
        """Generate a Stop packet."""
        return TreadmillController._create_packet(0, 1, 0, CommandType.STOP, is_kph)

    @staticmethod
    def set_speed(target_speed, incline=0, is_kph=True):
        """Generate a packet to set the speed."""
        return TreadmillController._create_packet(target_speed, 5, incline, CommandType.START_OR_SET, is_kph)

    @staticmethod
    def to_hex_string(packet):
        """Convert a packet to a hex string."""
        return ' '.join(f'{byte:02X}' for byte in packet)


if __name__ == "__main__":
    print("Start (kph):", TreadmillController.to_hex_string(TreadmillController.start(1100)))
    print("Set Speed (kph):", TreadmillController.to_hex_string(TreadmillController.set_speed(1200)))