"""The terminal UI."""
import asyncio
import logging
import os
import time
from typing import Callable, Optional, Union

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Digits, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from . import ble, protocol
from .config import Config
from .protocol import State, Status

log = logging.getLogger(__name__)

# How long to keep nudging the treadmill towards a requested speed or stop
# before giving up. Starting from stopped includes a few seconds' countdown.
GOAL_TIMEOUT = 15.0
# Don't repeat an identical command more often than this while waiting for the
# treadmill to act on it (it reports status about once a second).
RESEND_AFTER = 2.5
RECONNECT_DELAY = 3.0

STATE_LABELS = {
    State.STARTING: "STARTING",
    State.RUNNING: "RUNNING",
    State.PAUSED: "PAUSED",
    State.STOPPED: "STOPPED",
}

# Builds the Treadmill; replaced in tests.
TreadmillFactory = Callable[..., ble.Treadmill]


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


class DevicePicker(ModalScreen[Optional[str]]):
    """Scans for treadmills and returns the chosen address (None if cancelled)."""

    BINDINGS = [
        Binding("ctrl+r", "scan", "Rescan"),
        Binding("r", "scan", show=False),  # When the address box isn't focused
        Binding("escape", "dismiss(None)", "Cancel"),
    ]

    def __init__(self, saved: Optional[str], auto_pick: bool):
        super().__init__()
        self.saved = saved
        self.auto_pick = auto_pick

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label("Choose your treadmill", id="picker-title")
            yield Static(id="scan-status")
            yield OptionList(id="devices")
            yield Input(placeholder="…or type an address and press Enter", id="manual")
            yield Footer(compact=True)

    def on_mount(self) -> None:
        self.action_scan()

    @work(exclusive=True)
    async def action_scan(self) -> None:
        devices_list = self.query_one("#devices", OptionList)
        status = self.query_one("#scan-status", Static)
        devices_list.clear_options()
        devices_list.display = False
        status.update("Scanning… make sure the treadmill is on.")
        try:
            devices = await ble.discover()
        except Exception as e:
            log.exception("Scan failed")
            status.update(f"[red]Scan failed:[/] {e}\nIs Bluetooth on? Press [b]Ctrl+R[/] to retry.")
            self.query_one("#manual").focus()
            return

        if not devices:
            status.update("No treadmills found. Check it's on and not connected to another app, then press [b]Ctrl+R[/].")
            self.query_one("#manual").focus()
            return
        if self.auto_pick and len(devices) == 1:
            self.dismiss(devices[0].address)
            return

        for d in devices:
            signal = f"{d.rssi} dBm" if d.rssi is not None else "known, not advertising"
            saved = "  ★ last used" if self.saved and d.address.upper() == self.saved.upper() else ""
            devices_list.add_option(Option(f"{d.name}  [dim]{d.address}  {signal}[/]{saved}", id=d.address))
        status.update(f"Found {len(devices)}. Pick one with ↑↓ and Enter.")
        devices_list.display = True
        devices_list.highlighted = 0
        devices_list.focus()

    @on(OptionList.OptionSelected, "#devices")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    @on(Input.Submitted, "#manual")
    def typed(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())


class SpeedPrompt(ModalScreen[Optional[float]]):
    """Asks for a speed, optionally pre-filled with the digit that opened it."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, initial: str = ""):
        super().__init__()
        self.title_text = title
        self.initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label(self.title_text)
            yield Input(self.initial, placeholder="e.g. 2.5", type="number", id="speed-input")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        try:
            self.dismiss(float(event.value))
        except ValueError:
            self.notify("Enter a number, like 2.5", severity="warning")


class Dashboard(Screen):
    BINDINGS = [
        Binding("space", "toggle", "Start/Pause"),
        Binding("up,plus,equals_sign", "nudge(0.1)", "Speed", key_display="↑↓"),
        Binding("down,minus", "nudge(-0.1)", show=False),
        Binding("1", "preset(1)", "Preset", key_display="1-9"),
        *(Binding(str(n), f"preset({n})", show=False) for n in range(2, 10)),
        Binding("enter", "type_speed", "Exact", key_display="⏎"),
        Binding("s", "stop", "Stop"),
        Binding("p", "save_preset", "Save"),
        Binding("d", "toggle_details", "Details"),
        Binding("c", "pick_device", "Devices"),
        Binding("escape", "cancel", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, config: Config, make_treadmill: TreadmillFactory, startup_speed: Optional[float]):
        super().__init__()
        self.config = config
        self.make_treadmill = make_treadmill
        self.treadmill: Optional[ble.Treadmill] = None
        self.status: Optional[Status] = None
        # What the user asked for and the treadmill hasn't done yet: a speed in
        # display units, "stop", or None. Re-sent on each status until reached.
        self.goal: Union[float, str, None] = startup_speed
        self.goal_deadline: Optional[float] = None
        self.last_sent: tuple[bytes, float] = (b"", 0.0)
        self.resume_speed: Optional[float] = None
        self.saving_preset = False
        self.connecting = False

    def compose(self) -> ComposeResult:
        yield Static("[dim]●[/] Not connected", id="connection")
        with Vertical(id="main"):
            yield Label(id="state")
            yield Digits("--", id="speed")
            yield Label(id="target")
            with Horizontal(id="stats"):
                for key, title in (("time", "Time"), ("distance", "Distance"), ("steps", "Steps"), ("calories", "Calories")):
                    with Vertical(classes="stat"):
                        yield Label(title, classes="stat-title")
                        yield Label("--", id=key, classes="stat-value")
            yield Static(id="presets")
            details = DataTable(id="details", show_header=False, cursor_type="none")
            details.can_focus = False  # Would otherwise swallow Enter and the arrow keys.
            yield details
        yield Footer(compact=True)

    def on_mount(self) -> None:
        self.query_one("#details", DataTable).add_columns("Field", "Value")
        self.refresh_view()
        if self.config.address:
            self.connect(self.config.address)
        else:
            self.action_pick_device(auto_pick=True)

    # --- Connection ---------------------------------------------------------

    @work(exclusive=True, group="connection")
    async def connect(self, address: str, retry: bool = False) -> None:
        """Connects to `address`. With `retry`, keeps trying until it works."""
        await self._drop_treadmill()
        variant = self.config.variant if address.upper() == (self.config.address or "").upper() else None
        self.connecting = True
        while True:
            self.set_connection(
                f"[yellow]●[/] {'Reconnecting' if retry else 'Connecting'} to {address}…  [dim](c to choose another)[/]"
            )
            treadmill = self.make_treadmill(address, self.on_status, self.on_lost, variant)
            try:
                await treadmill.connect()
                break
            except Exception as e:
                log.warning("Connect to %s failed: %s", address, e)
                if not retry:
                    self.connecting = False
                    self.set_connection(f"[red]●[/] Couldn't connect to {address}")
                    self.refresh_view()
                    self.notify(f"{e or type(e).__name__}", title="Couldn't connect", severity="error")
                    self.action_pick_device()
                    return
                await asyncio.sleep(RECONNECT_DELAY)

        self.treadmill = treadmill
        self.connecting = False
        self.set_connection(f"[green]●[/] Connected to {address}")
        self.config.address, self.config.variant = address, treadmill.variant
        self.config.save()

    def on_lost(self) -> None:
        if self.treadmill is None:
            return
        address = self.treadmill.address
        self.treadmill = None
        self.status = None
        self.goal = None
        self.notify("Lost connection, reconnecting…", severity="warning")
        self.connect(address, retry=True)
        self.refresh_view()

    async def _drop_treadmill(self) -> None:
        treadmill, self.treadmill, self.status = self.treadmill, None, None
        if treadmill:
            try:
                await treadmill.disconnect()
            except Exception as e:
                log.warning("Disconnect failed: %s", e)
        self.refresh_view()

    def set_connection(self, text: str) -> None:
        self.query_one("#connection", Static).update(text)

    # --- Driving the treadmill ----------------------------------------------

    def on_status(self, status: Status) -> None:
        self.status = status
        if status.state == State.RUNNING and status.target_speed:
            self.resume_speed = status.to_display(status.target_speed)
        self.drive()
        self.refresh_view()

    def set_goal(self, goal: Union[float, str, None]) -> None:
        self.goal = goal
        self.goal_deadline = None
        self.last_sent = (b"", 0.0)
        self.drive()
        self.refresh_view()

    def send(self, packet: bytes) -> None:
        """Queues `packet`, unless the identical one went out moments ago."""
        if self.treadmill is None:
            return
        last_packet, last_time = self.last_sent
        if packet == last_packet and time.monotonic() - last_time < RESEND_AFTER:
            return
        self.last_sent = (packet, time.monotonic())
        self.treadmill.send(packet)

    def drive(self) -> None:
        """Sends whatever command moves the treadmill towards self.goal."""
        s = self.status
        if self.goal is None or s is None or self.treadmill is None:
            return
        if self.goal_deadline is None:
            self.goal_deadline = time.monotonic() + GOAL_TIMEOUT
        elif time.monotonic() > self.goal_deadline:
            self.notify("The treadmill didn't respond to the last command.", severity="warning")
            self.goal = None
            return

        if self.goal == "stop":
            if s.state == State.RUNNING:
                self.send(protocol.pause(s.imperial))  # It only stops from paused.
            elif s.state == State.PAUSED:
                self.send(protocol.stop(s.imperial))
            elif s.state == State.STOPPED:
                self.goal = None
            return

        wire = s.to_wire(self.goal)
        if s.state in (State.PAUSED, State.STOPPED):
            self.send(protocol.start(wire, s.imperial))
        elif s.state == State.RUNNING:
            if round(s.to_display(s.target_speed), 1) == round(s.to_display(wire), 1):
                self.goal = None
            else:
                self.send(protocol.set_speed(wire, s.imperial))

    def require_status(self) -> Optional[Status]:
        if self.status is None:
            self.notify("Not connected to a treadmill yet.", severity="warning")
        return self.status

    # --- Actions --------------------------------------------------------------

    def action_toggle(self) -> None:
        s = self.require_status()
        if s is None:
            return
        if s.state in (State.RUNNING, State.STARTING) or isinstance(self.goal, float):
            self.goal = None
            self.send(protocol.pause(s.imperial))
            self.refresh_view()
        else:
            self.set_goal(self.resume_speed or self.config.presets[0])

    def action_stop(self) -> None:
        if self.require_status():
            self.set_goal("stop")

    def action_nudge(self, step: float) -> None:
        s = self.require_status()
        if s is None:
            return
        if isinstance(self.goal, float):
            base = self.goal
        elif s.state == State.RUNNING:
            base = s.to_display(s.target_speed)
        else:
            self.notify("Start the belt first (Space or a preset).")
            return
        max_speed = s.to_display(s.max_speed) if s.max_speed else float("inf")
        self.set_goal(round(min(max(base + step, 0.1), max_speed), 1))

    def action_preset(self, n: int) -> None:
        if self.saving_preset:
            self.saving_preset = False
            speed = self.current_speed()
            self.config.presets[n - 1] = speed
            self.config.save()
            self.notify(f"Saved {speed:.1f} to key {n}.")
            self.refresh_view()
        elif self.require_status():
            self.set_goal(self.config.presets[n - 1])

    def action_type_speed(self) -> None:
        s = self.require_status()
        if s is None:
            return

        def chosen(speed: Optional[float]) -> None:
            if speed is not None and speed > 0:
                self.set_goal(speed)

        self.app.push_screen(SpeedPrompt(f"Speed in {s.speed_unit}, then Enter (Esc cancels)"), chosen)

    def action_save_preset(self) -> None:
        if self.current_speed() is None:
            self.notify("Get the belt running at the speed you want first.")
            return
        self.saving_preset = True
        self.notify(f"Press 1–9 to save {self.current_speed():.1f} there (Esc cancels).")

    def action_cancel(self) -> None:
        self.saving_preset = False

    def action_toggle_details(self) -> None:
        self.query_one("#details").toggle_class("shown")

    def action_pick_device(self, auto_pick: bool = False) -> None:
        if self.connecting:
            # Connecting scans for the device too, and BlueZ allows one scan at a time.
            self.workers.cancel_group(self, "connection")
            self.connecting = False
            self.set_connection("[dim]●[/] Not connected")
            self.refresh_view()

        def chosen(address: Optional[str]) -> None:
            if address:
                self.connect(address)

        self.app.push_screen(DevicePicker(self.config.address, auto_pick), chosen)

    async def action_quit(self) -> None:
        """Pauses the belt if it's moving, then exits."""
        self.workers.cancel_group(self, "connection")
        if self.treadmill and self.status and self.status.state in (State.RUNNING, State.STARTING):
            self.notify("Pausing the belt…")
            try:
                await asyncio.wait_for(self.treadmill.send(protocol.pause(self.status.imperial)), 3)
            except Exception as e:
                log.warning("Couldn't pause before quitting: %s", e)
        await self._drop_treadmill()
        self.app.exit()

    def current_speed(self) -> Optional[float]:
        s = self.status
        if s and s.state == State.RUNNING and s.target_speed:
            return round(s.to_display(s.target_speed), 1)
        return None

    # --- Rendering ----------------------------------------------------------

    def refresh_view(self) -> None:
        s = self.status
        state = self.query_one("#state", Label)
        target = self.query_one("#target", Label)
        state.set_classes(STATE_LABELS[s.state].lower() if s else "")

        if s is None:
            idle = self.treadmill is None and not self.connecting
            state.update("NOT CONNECTED" if idle else "WAITING FOR TREADMILL")
            self.query_one("#speed", Digits).update("--")
            target.update("Press [b]c[/] to choose a treadmill" if idle else "")
            for key in ("time", "distance", "steps", "calories"):
                self.query_one(f"#{key}", Label).update("--")
        else:
            state.update(STATE_LABELS[s.state])
            self.query_one("#speed", Digits).update(f"{s.to_display(s.speed):.1f}")
            line = f"{s.speed_unit}  ·  target {s.to_display(s.target_speed):.1f}  ·  max {s.to_display(s.max_speed):.1f}"
            if isinstance(self.goal, float):
                line += f"  ·  [b]→ {self.goal:.1f}[/]"
            elif self.goal == "stop":
                line += "  ·  [b]stopping…[/]"
            target.update(line)
            self.query_one("#time", Label).update(fmt_duration(s.duration_s))
            self.query_one("#distance", Label).update(f"{s.distance:.2f} {s.distance_unit}")
            self.query_one("#steps", Label).update(f"{s.steps:,}")
            self.query_one("#calories", Label).update(f"{s.calories} kcal")

        current = self.current_speed()
        goal = self.goal if isinstance(self.goal, float) else None
        max_speed = s.to_display(s.max_speed) if s and s.max_speed else float("inf")
        chips = []
        for n, speed in enumerate(self.config.presets, 1):
            active = speed == (goal if goal is not None else current)
            chip = f"[b $accent]{n}[/] {speed:.1f}"
            if active:
                chip = f"[reverse] {chip} [/]"
            elif speed > max_speed + 0.05:
                chip = f"[dim] {chip} [/]"  # Faster than this treadmill goes
            else:
                chip = f" {chip} "
            chips.append(chip)
        hint = "  [b]Press 1–9 to save here[/]" if self.saving_preset else ""
        self.query_one("#presets", Static).update("Presets " + " ".join(chips) + hint)

        self.update_details()

    def update_details(self) -> None:
        table = self.query_one("#details", DataTable)
        table.clear()
        s = self.status
        if s is None:
            return
        unit = s.speed_unit
        rows = [
            ("Address", self.treadmill.address if self.treadmill else "-"),
            ("Variant", (self.treadmill.variant or "-") if self.treadmill else "-"),
            ("State", STATE_LABELS[s.state]),
            ("Speed", f"{s.to_display(s.speed):.2f} {unit}"),
            ("Target speed", f"{s.to_display(s.target_speed):.2f} {unit}"),
            ("Max speed", f"{s.to_display(s.max_speed):.2f} {unit}"),
            ("Incline", f"{s.incline}° (target {s.target_incline}°, max {s.max_incline}°)"),
            ("Heart rate", f"{s.heart_rate} bpm" if s.heart_rate else "-"),
            ("Units", "Imperial" if s.imperial else "Metric"),
            ("Firmware", str(s.firmware)),
            ("Device type", str(s.device_type)),
            ("Serial", s.serial or "-"),
        ]
        table.add_rows(rows)


def terminal_is_light() -> bool:
    """Best guess from COLORFGBG ("fg;bg"), which some terminals set; else dark."""
    background = os.environ.get("COLORFGBG", "").split(";")[-1]
    return background in ("7", "15")


class PitPatApp(App):
    TITLE = "PitPat"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding("ctrl+c", "quit", show=False, priority=True)]
    CSS = """
    #connection { padding: 0 1; border-bottom: solid ansi_bright_black; height: 2; }
    #main { align: center top; padding: 1 2; }
    #state { width: 100%; text-align: center; text-style: bold; color: $text-muted; }
    #state.running { color: $success; }
    #state.starting { color: $warning; }
    #state.paused { color: $warning; }
    #speed { width: 100%; text-align: center; margin: 1 0 0 0; }
    #target { width: 100%; text-align: center; color: $text-muted; margin-bottom: 1; }
    #stats { height: auto; width: 100%; align-horizontal: center; }
    .stat { width: 18; height: auto; border: round ansi_bright_black; padding: 0 1; }
    .stat-title { color: $text-muted; }
    .stat-value { text-style: bold; }
    #presets { width: 100%; text-align: center; margin: 1 0; }
    #details { display: none; height: auto; max-height: 16; margin: 0 4; }
    #details.shown { display: block; }
    DevicePicker, SpeedPrompt { align: center middle; }
    #picker { width: 80; max-width: 100%; height: auto; max-height: 90%; border: round $primary; background: $background; padding: 1 2; }
    #picker-title { text-style: bold; margin-bottom: 1; }
    #devices { height: auto; max-height: 12; margin: 1 0; }
    #devices > .option-list--option-highlighted { background: ansi_blue; color: ansi_bright_white; text-style: bold; }
    #prompt { width: 44; height: auto; border: round $primary; background: $background; padding: 1 2; }
    """

    def __init__(self, config: Config, startup_speed: Optional[float] = None, make_treadmill: TreadmillFactory = ble.Treadmill):
        super().__init__()
        self.dashboard = Dashboard(config, make_treadmill, startup_speed)

    def on_mount(self) -> None:
        # Draw with the terminal's own background and palette rather than Textual's.
        self.theme = "ansi-light" if terminal_is_light() else "ansi-dark"
        self.push_screen(self.dashboard)

    async def action_quit(self) -> None:
        await self.dashboard.action_quit()
