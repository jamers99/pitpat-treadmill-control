"""Drives the TUI against a fake treadmill and checks what it sends."""
import asyncio

import pytest

from pitpat import app as app_module, ble, config, protocol
from pitpat.app import PitPatApp
from pitpat.config import Config
from pitpat.protocol import Status

from test_protocol import frame

RUNNING, PAUSED, STOPPED, IMPERIAL = 8, 16, 0, 128


class FakeTreadmill:
    instances = []

    def __init__(self, address, on_status, on_disconnect, variant=None):
        self.address = address
        self.on_status = on_status
        self.on_disconnect = on_disconnect
        self.variant = variant or "fba0"
        self.sent = []
        FakeTreadmill.instances.append(self)

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    def send(self, packet):
        self.sent.append(packet)
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    def feed(self, **kwargs):
        self.on_status(Status.parse(frame(**kwargs)))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.json")
    # Each test feeds frames back to back, faster than the real ~1/s.
    monkeypatch.setattr(app_module, "RESEND_AFTER", 0)
    FakeTreadmill.instances.clear()


def wire(mph):
    return round(mph * protocol.KM_PER_MILE * 1000)


async def test_startup_speed_starts_belt_then_session_controls():
    app = PitPatApp(Config(address="AA:BB"), startup_speed=2.5, make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        t = FakeTreadmill.instances[-1]

        t.feed(flags=IMPERIAL | STOPPED)
        assert t.sent[-1] == protocol.start(wire(2.5), imperial=True)

        # Treadmill ignored the start speed: it should be corrected.
        t.feed(flags=IMPERIAL | RUNNING, target=1000, speed=1000)
        assert t.sent[-1] == protocol.set_speed(wire(2.5), imperial=True)

        t.feed(flags=IMPERIAL | RUNNING, target=wire(2.5), speed=wire(2.5))
        assert app.dashboard.goal is None
        sent = len(t.sent)

        await pilot.press("up")
        assert t.sent[-1] == protocol.set_speed(wire(2.6), imperial=True)
        assert len(t.sent) == sent + 1

        await pilot.press("3")  # Default preset 3 is 2.0
        assert t.sent[-1] == protocol.set_speed(wire(2.0), imperial=True)
        t.feed(flags=IMPERIAL | RUNNING, target=wire(2.0), speed=wire(2.0))

        await pilot.press("space")
        assert t.sent[-1] == protocol.pause(imperial=True)
        t.feed(flags=IMPERIAL | PAUSED)

        await pilot.press("space")  # Resumes at the last speed
        assert t.sent[-1] == protocol.start(wire(2.0), imperial=True)
        t.feed(flags=IMPERIAL | PAUSED)

        await pilot.press("s")
        assert t.sent[-1] == protocol.stop(imperial=True)

    saved = Config.load()
    assert saved.address == "AA:BB" and saved.variant == "fba0"


async def test_stop_while_running_pauses_first():
    app = PitPatApp(Config(address="AA:BB"), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        t = FakeTreadmill.instances[-1]
        t.feed(flags=RUNNING, target=3000)
        await pilot.press("s")
        assert t.sent[-1] == protocol.pause(imperial=False)
        t.feed(flags=PAUSED)
        assert t.sent[-1] == protocol.stop(imperial=False)


async def test_save_preset():
    app = PitPatApp(Config(address="AA:BB"), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        FakeTreadmill.instances[-1].feed(flags=RUNNING, target=3300)
        await pilot.press("p", "9")
    assert Config.load().presets[8] == 3.3


async def test_typed_speed_clamped_to_max():
    app = PitPatApp(Config(address="AA:BB"), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        t = FakeTreadmill.instances[-1]
        t.feed(flags=RUNNING, target=3000)
        await pilot.press("enter", "9", "enter")
        assert t.sent[-1] == protocol.set_speed(6000, imperial=False)


async def test_quit_pauses_running_belt():
    app = PitPatApp(Config(address="AA:BB"), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        t = FakeTreadmill.instances[-1]
        t.feed(flags=RUNNING, target=3000)
        await pilot.press("q")
        await pilot.pause()
    assert t.sent[-1] == protocol.pause(imperial=False)


async def test_first_run_autoconnects_to_only_treadmill(monkeypatch):
    async def discover():
        return [ble.Device("CC:DD", "PitPat-T01", -60)]

    monkeypatch.setattr(ble, "discover", discover)
    app = PitPatApp(Config(), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert FakeTreadmill.instances[-1].address == "CC:DD"
        assert app.dashboard.treadmill is not None


async def test_lost_connection_reconnects(monkeypatch):
    monkeypatch.setattr(app_module, "RECONNECT_DELAY", 0)
    app = PitPatApp(Config(address="AA:BB"), make_treadmill=FakeTreadmill)
    async with app.run_test() as pilot:
        await pilot.pause()
        FakeTreadmill.instances[-1].on_disconnect()
        await pilot.pause()
        await pilot.pause()
        assert len(FakeTreadmill.instances) == 2
        assert app.dashboard.treadmill is FakeTreadmill.instances[-1]


def test_config_tolerates_hand_edits(tmp_path):
    config.CONFIG_PATH.write_text('{"presets": [2, 3], "unknown": 1}')
    presets = Config.load().presets
    assert presets[:3] == [2.0, 3.0, 1.0] and len(presets) == 9
    assert all(isinstance(p, float) for p in presets)
