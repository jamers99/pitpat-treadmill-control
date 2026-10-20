"""PitPat wire protocol: hardware variants, command packets and status frames.

See docs/FIRMWARE-VARIANT.md for the byte layouts.
"""
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

# Known hardware revisions. "wrapped" means writes need the
# "4d00 <counter> <length>" transport header and status frames carry 4 extra
# leading bytes; the fba0 variant takes everything bare. Order is the order
# they are tried in when detecting which one a treadmill is.
VARIANTS = {
    "fba0": {
        "label": "T01, firmware 37 (fba0)",
        "notify_uuid": "0000fba2-0000-1000-8000-00805f9b34fb",
        "write_uuid": "0000fba1-0000-1000-8000-00805f9b34fb",
        "wrapped": False,
    },
    "ba04": {
        "label": "T01 (BA04)",
        "notify_uuid": "0000ff02-0000-1000-8000-00805f9b34fb",
        "write_uuid": "0000ff01-0000-1000-8000-00805f9b34fb",
        "wrapped": True,
    },
}

# Status poll sent whenever there is no command to send.
HEARTBEAT = bytes.fromhex("6a05fdf843")

# Speeds on the wire are thousandths of a kph and distances are metres, whatever
# the treadmill's display unit is set to.
KM_PER_MILE = 1.609344

USER_ID = 58965456623  # Not validated by the treadmill; any value works.
WEIGHT_KG = 80


class State(IntEnum):
    STARTING = 0
    RUNNING = 1
    PAUSED = 2
    STOPPED = 3


class Command(IntEnum):
    STOP = 0
    PAUSE = 2
    START_OR_SET = 4


def _xor(data: bytes) -> int:
    result = 0
    for byte in data:
        result ^= byte
    return result


def command(cmd: Command, speed: int = 0, imperial: bool = False, ramp: int = 1) -> bytes:
    """Builds a 23-byte command frame.

    `speed` is in thousandths of a kph and `ramp` is the acceleration time in
    seconds (1 or 5). The unit flag must match the treadmill's display unit.
    """
    if not 0 <= speed <= 0xFFFF:
        raise ValueError(f"speed out of range: {speed}")
    packet = bytearray(23)
    packet[0] = 0x6A
    packet[1] = 0x17
    packet[6:8] = speed.to_bytes(2, "big")
    packet[8] = ramp
    packet[10] = WEIGHT_KG
    packet[12] = cmd | 0x08 if imperial else cmd
    packet[13:21] = USER_ID.to_bytes(8, "big")
    packet[21] = _xor(packet[1:21])
    packet[22] = 0x43
    return bytes(packet)


def start(speed: int, imperial: bool) -> bytes:
    return command(Command.START_OR_SET, speed, imperial)


def set_speed(speed: int, imperial: bool) -> bytes:
    return command(Command.START_OR_SET, speed, imperial, ramp=5)


def pause(imperial: bool) -> bytes:
    return command(Command.PAUSE, imperial=imperial)


def stop(imperial: bool) -> bytes:
    return command(Command.STOP, imperial=imperial)


def wrap(body: bytes, counter: int) -> bytes:
    """Adds the BA04 transport header."""
    return bytes([0x4D, 0x00, counter & 0xFF, len(body) & 0xFF]) + body


@dataclass
class Status:
    """One decoded status frame. Speeds are wire units (thousandths of a kph)."""
    speed: int
    target_speed: int
    max_speed: int
    distance_m: int
    incline: int
    target_incline: int
    max_incline: int
    heart_rate: int
    steps: int
    calories: int
    duration_s: float
    firmware: int
    device_type: int
    imperial: bool
    state: State
    serial: Optional[str] = None

    @classmethod
    def parse(cls, frame: bytes) -> Optional["Status"]:
        """Decodes an unwrapped frame, or returns None if it is short or corrupt."""
        if len(frame) < 31 or _xor(frame[1:-2]) != frame[-2]:
            return None

        def u16(i):
            return int.from_bytes(frame[i:i + 2], "big")

        def u32(i):
            return int.from_bytes(frame[i:i + 4], "big")

        firmware = frame[25]
        flags = frame[26]
        state = {24: State.STARTING, 8: State.RUNNING, 16: State.PAUSED}.get(flags & 24, State.STOPPED)
        steps = u32(14)
        # Frames 45-51 bytes long from newer firmware carry a more reliable
        # motor-derived step count at offset 43.
        if firmware >= 25 and 45 <= len(frame) < 52 and frame[1] > 44:
            steps = u16(43)
        serial = None
        if firmware >= 25 and len(frame) >= 52:
            serial = frame[32:48].decode("ascii", errors="ignore").strip("\x00 ") or None
        duration = u32(20)

        return cls(
            speed=u16(3),
            target_speed=u16(5),
            max_speed=u16(27),
            distance_m=u32(7),
            incline=frame[11],
            target_incline=min(frame[12], 192),
            max_incline=frame[29],
            heart_rate=frame[13],
            steps=steps,
            calories=u16(18),
            duration_s=duration / 1000 if firmware > 19 else float(duration),
            firmware=firmware,
            device_type=frame[30] & 31,
            imperial=bool(flags & 128),
            state=state,
            serial=serial,
        )

    @property
    def speed_unit(self) -> str:
        return "mph" if self.imperial else "km/h"

    @property
    def distance_unit(self) -> str:
        return "mi" if self.imperial else "km"

    def to_display(self, wire_speed: int) -> float:
        """Wire speed to the treadmill's display unit."""
        kph = wire_speed / 1000
        return kph / KM_PER_MILE if self.imperial else kph

    def to_wire(self, display_speed: float) -> int:
        """Display-unit speed to wire units, clamped to what the treadmill allows."""
        kph = display_speed * KM_PER_MILE if self.imperial else display_speed
        wire = max(0, round(kph * 1000))
        return min(wire, self.max_speed) if self.max_speed else wire

    @property
    def distance(self) -> float:
        km = self.distance_m / 1000
        return km / KM_PER_MILE if self.imperial else km
