from pitpat import protocol
from pitpat.protocol import State, Status

# Status frame captured from a firmware-27 treadmill (stopped, metric).
SAMPLE = bytes.fromhex(
    "6834000000000000000000000000000000000000000000002a1b001770000500"
    "746c4b613139316655705473733630330e004043"
)
USER_ID = "0000000dba9d76ef"


def frame(speed=0, target=0, distance=0, duration_ms=0, flags=0, max_speed=6000) -> bytes:
    """SAMPLE with the given fields set and the checksum fixed up."""
    f = bytearray(SAMPLE)
    f[3:5] = speed.to_bytes(2, "big")
    f[5:7] = target.to_bytes(2, "big")
    f[7:11] = distance.to_bytes(4, "big")
    f[20:24] = duration_ms.to_bytes(4, "big")
    f[26] = flags
    f[27:29] = max_speed.to_bytes(2, "big")
    f[-2] = protocol._xor(f[1:-2])
    return bytes(f)


def test_commands_match_documented_packets():
    # docs/FIRMWARE-VARIANT.md, captured on an imperial treadmill.
    assert protocol.start(1000, imperial=True).hex() == f"6a170000000003e8010050000c{USER_ID}1243"
    assert protocol.set_speed(1100, imperial=True).hex() == f"6a1700000000044c050050000c{USER_ID}b543"
    assert protocol.pause(imperial=True).hex() == f"6a17000000000000010050000a{USER_ID}ff43"
    assert protocol.stop(imperial=True).hex() == f"6a170000000000000100500008{USER_ID}fd43"


def test_metric_command_clears_unit_bit():
    assert protocol.start(2500, imperial=False)[12] == 0x04


def test_wrap():
    assert protocol.wrap(protocol.HEARTBEAT, 7) == bytes.fromhex("4d0007056a05fdf843")


def test_parse_sample():
    s = Status.parse(SAMPLE)
    assert s.state == State.STOPPED
    assert s.max_speed == 6000
    assert s.firmware == 27
    assert s.device_type == 5
    assert s.serial == "tlKa191fUpTss603"
    assert not s.imperial


def test_parse_running_imperial():
    s = Status.parse(frame(speed=4023, target=4023, distance=1609, duration_ms=754_000, flags=128 | 8))
    assert s.state == State.RUNNING
    assert s.imperial
    assert round(s.to_display(s.speed), 2) == 2.5
    assert round(s.distance, 3) == 1.0
    assert s.duration_s == 754
    assert (s.speed_unit, s.distance_unit) == ("mph", "mi")


def test_parse_states():
    assert Status.parse(frame(flags=24)).state == State.STARTING
    assert Status.parse(frame(flags=16)).state == State.PAUSED


def test_parse_rejects_bad_checksum_and_short_frames():
    corrupt = bytearray(SAMPLE)
    corrupt[5] ^= 1
    assert Status.parse(bytes(corrupt)) is None
    assert Status.parse(SAMPLE[:20]) is None


def test_to_wire_clamps_to_max():
    s = Status.parse(frame(flags=128))
    assert s.to_wire(2.5) == 4023
    assert s.to_wire(10) == 6000
    assert s.to_wire(-1) == 0
