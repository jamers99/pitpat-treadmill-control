from typing import List, Tuple, Optional, Any
from dash import Dash, html, dcc, Output, Input, State, no_update
import dash_bootstrap_components as dbc
from datetime import timedelta
from .bluetooth_manager import BluetoothManager, VARIANTS, DEFAULT_VARIANT, discover_treadmills
from .treadmill_controller import TreadmillController
from .logger import setup_logger

# The protocol is metric whatever the treadmill's display is set to: speeds are
# thousandths of a kph and distances are metres. Confirmed on firmware 37 — a
# 3000 target reads 1.9 on a unit displaying mph (3.0 / 1.609), and the status
# frames come back in the same metric units.
KM_PER_MILE = 1.609344

# Outputs shared by the connect and interval callbacks, as
# keyword -> (component id, prop). The Output list and _create_output()'s
# no_update defaults are both derived from this, so they cannot drift apart.
SHARED_OUTPUTS = {
    "btn_connect_text": ("button-connect", "children"),
    "btn_connect_color": ("button-connect", "color"),
    "input_disabled": ("text-input", "disabled"),
    "variant_select_disabled": ("variant-select", "disabled"),
    "speed": ("speed", "children"),
    "distance": ("distance", "children"),
    "calories": ("calories", "children"),
    "steps": ("steps", "children"),
    "duration": ("duration", "children"),
    "status": ("status", "children"),
    "alert_open": ("alert", "is_open"),
    "alert_text": ("alert", "children"),
    "alert_color": ("alert", "color"),
    "btn_start_text": ("button-start", "children"),
    "btn_start_disabled": ("button-start", "disabled"),
    "btn_stop_disabled": ("button-stop", "disabled"),
    "btn_speed_down_disabled": ("button-speed-down", "disabled"),
    "btn_speed_up_disabled": ("button-speed-up", "disabled"),
    "btn_set_speed_disabled": ("button-set-speed", "disabled"),
    "speed_input_disabled": ("speed-input", "disabled"),
    "speed_input_unit": ("speed-input-unit", "children"),
    "table_body": ("table-data", "children"),
    "connection_state": ("connection-state", "data"),
    "running_state": ("running-state", "data"),
    "interval_disabled": ("interval-component", "disabled"),
    "last_address": ("last-address", "data"),
}

# _create_output() kwargs that switch every run control off.
CONTROLS_OFF = {
    "btn_start_disabled": True,
    "btn_stop_disabled": True,
    "btn_speed_down_disabled": True,
    "btn_speed_up_disabled": True,
    "btn_set_speed_disabled": True,
    "speed_input_disabled": True,
}

# The run controls, in the order they appear in the layout.
RUN_CONTROLS = (
    "button-start", "button-stop", "button-speed-down",
    "button-speed-up", "button-set-speed", "speed-input",
)


def _busy(after, controls=RUN_CONTROLS):
    """`running=` spec disabling `controls` while a callback works, then setting them to `after`."""
    return [[Output(cid, "disabled"), True, after] for cid in controls]


class TreadmillApp:
    """Main application class for PitPat Treadmill Control."""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 8050, debug: bool = False):
        self.logger = setup_logger("TreadmillApp")
        self.app = Dash(__name__, title="PitPat Treadmill Control", external_stylesheets=[dbc.themes.BOOTSTRAP])
        self.manager: Optional[BluetoothManager] = None
        self.treadmill_data = None
        self.host = host
        self.port = port
        self.debug = debug
        self._setup_layout()
        self._register_callbacks()

    def _create_card(self, id: str, title: str, init_value: str = "-") -> dbc.Col:
        """Create a dashboard card."""
        return dbc.Col(
            dbc.Card(
                [
                    dbc.CardHeader(title),
                    dbc.CardBody(
                        html.H4(init_value, id=id, className="card-title text-center")
                    ),
                ]
            ),
            md=6,
            lg=4,
            className="mb-3"
        )

    def _create_table_body(self, data: List[Tuple[str, str]]) -> List[Any]:
        """Create table body content."""
        return [
            html.Thead(html.Tr([html.Th("Attribute"), html.Th("Value")])),
            html.Tbody([html.Tr([html.Td(k), html.Td(v)]) for k, v in data]),
        ]

    def _setup_layout(self) -> None:
        """Setup the Dash application layout."""
        self.app.layout = dbc.Container(
            [
                html.H1("PitPat Treadmill Control", className="my-4"),
                dbc.Alert(id="alert", color="danger", dismissable=True, is_open=False),
                dbc.InputGroup(
                    [
                        dbc.Select(
                            id="variant-select",
                            options=[{"label": cfg["label"], "value": key} for key, cfg in VARIANTS.items()],
                            value=DEFAULT_VARIANT,
                            style={"maxWidth": "18rem"},
                            persistence=True,
                            persistence_type="local",
                        ),
                        dbc.Input(
                            id="text-input",
                            type="text",
                            placeholder="Device Address",
                            list="device-list",
                            autoComplete="off",
                            persistence=True,
                            persistence_type="local",
                        ),
                        dbc.Button("Scan", id="button-scan", color="secondary", n_clicks=0),
                        dbc.Button("Connect", id="button-connect", color="primary", n_clicks=0),
                    ],
                    className="mb-3"
                ),
                html.Datalist(id="device-list"),
                dbc.Row(
                    [
                        self._create_card("speed", "Speed"),
                        self._create_card("distance", "Distance"),
                        self._create_card("calories", "Calories"),
                        self._create_card("steps", "Steps"),
                        self._create_card("duration", "Duration"),
                        self._create_card("status", "Running State"),
                    ]
                ),
                dbc.Row(
                    dbc.Col(
                        dbc.Stack(
                            [
                                dbc.Button("Start", id="button-start", color="primary", n_clicks=0, disabled=True),
                                dbc.Button("Stop", id="button-stop", color="primary", n_clicks=0, disabled=True),
                                dbc.Button("Speed Up", id="button-speed-up", color="primary", n_clicks=0, disabled=True),
                                dbc.Button("Speed Down", id="button-speed-down", color="primary", n_clicks=0, disabled=True),
                                dbc.InputGroup(
                                    [
                                        dbc.Input(
                                            id="speed-input",
                                            type="number",
                                            min=0,
                                            step=0.1,
                                            placeholder="Speed",
                                            disabled=True,
                                            style={"maxWidth": "7rem"},
                                        ),
                                        dbc.InputGroupText("kph", id="speed-input-unit"),
                                        dbc.Button("Set", id="button-set-speed", color="primary", n_clicks=0, disabled=True),
                                    ],
                                ),
                            ],
                            direction="horizontal",
                            gap=2,
                        ),
                        width="auto",
                        className="mb-3"
                    ),
                    justify="center"
                ),
                dbc.Table(id="table-data", bordered=True, responsive=True, style={"tableLayout": "fixed"}),
                dcc.Store(id="connection-state", data="disconnected"),
                dcc.Store(id="running-state", data=-1),
                # Address of the last treadmill connected to. Unlike the `persistence`
                # prop on text-input, dcc.Store's storage_type survives that field's
                # value also being a callback Output (see handle_scan/handle_connect).
                dcc.Store(id="last-address", storage_type="local"),
                dcc.Interval(id="interval-component", interval=1000, n_intervals=0, disabled=True),
                # Fires once shortly after page load to scan without a click.
                dcc.Interval(id="interval-scan", interval=500, n_intervals=0, max_intervals=1),
            ]
        )

    def _is_imperial(self) -> bool:
        """Whether the treadmill's own display is set to miles."""
        return bool(self.treadmill_data and self.treadmill_data.unit_mode == 1)

    def _to_display_speed(self, wire_speed: int) -> float:
        """Converts a wire speed (thousandths of a kph) to the display unit."""
        speed = wire_speed / 1000
        return speed / KM_PER_MILE if self._is_imperial() else speed

    def _to_wire_speed(self, display_speed: float) -> int:
        """Converts a displayed speed to thousandths of a kph for the wire."""
        if self._is_imperial():
            display_speed = display_speed * KM_PER_MILE
        return int(round(display_speed * 1000))

    def _to_display_distance(self, wire_distance: int) -> float:
        """Converts a wire distance (metres) to the display unit."""
        distance = wire_distance / 1000
        return distance / KM_PER_MILE if self._is_imperial() else distance

    def _is_kph(self) -> bool:
        """Unit flag for outgoing commands, driven off the last status frame."""
        return not self._is_imperial()

    def _nudge_speed(self, step: float, running_state: int) -> None:
        """Steps the target speed by `step` in whatever unit the display is using."""
        if self.manager and self.treadmill_data and running_state == 1:
            current = self._to_display_speed(self.treadmill_data.current_speed)
            target_speed = self._to_wire_speed(max(current + step, 0))
            self.manager.send_data(TreadmillController.set_speed(target_speed, is_kph=self._is_kph()))

    def _on_disconnect(self, device_address: str) -> None:
        """Handle Bluetooth disconnection."""
        self.logger.info(f"Disconnected from {device_address}")
        if self.manager:
            self.manager.shutdown()
            self.manager = None
            self.treadmill_data = None

    def _on_receive(self, data: Any) -> None:
        """Handle received treadmill data."""
        self.treadmill_data = data
        self.logger.debug(f"Received data: {data}")

    def _create_output(self, **kwargs) -> Tuple:
        """Create callback output tuple with default no_update values."""
        unknown = set(kwargs) - set(SHARED_OUTPUTS)
        if unknown:
            raise KeyError(f"Unknown callback output(s): {sorted(unknown)}")
        return tuple(kwargs.get(key, no_update) for key in SHARED_OUTPUTS)

    def _register_callbacks(self) -> None:
        """Register all Dash callbacks."""
        outputs = [
            Output(cid, prop, allow_duplicate=True) for cid, prop in SHARED_OUTPUTS.values()
        ]
        # Stop is left alone by the speed callbacks: they only run while the
        # treadmill is running, and Stop is not offered in that state anyway.
        speed_busy = _busy(False, [c for c in RUN_CONTROLS if c != "button-stop"])

        @self.app.callback(
            [
                Output("device-list", "children"),
                Output("text-input", "value"),
                Output("alert", "is_open", allow_duplicate=True),
                Output("alert", "children", allow_duplicate=True),
                Output("alert", "color", allow_duplicate=True),
            ],
            [Input("button-scan", "n_clicks"), Input("interval-scan", "n_intervals")],
            [State("connection-state", "data"), State("text-input", "value"), State("last-address", "data")],
            running=[
                [Output("button-scan", "children"), "Scanning...", "Scan"],
                [Output("button-scan", "disabled"), True, False],
                [Output("button-connect", "disabled"), True, False],
            ],
            prevent_initial_call=True
        )
        def handle_scan(
            n_clicks: int, n_intervals: int, connection_state: str, device_address: str, last_address: Optional[str]
        ) -> Tuple:
            """Scan for nearby treadmills and offer them as address suggestions."""
            if connection_state == "connected":
                return no_update, no_update, no_update, no_update, no_update

            # Nothing typed yet: prefill the last treadmill connected to, so a
            # reload doesn't lose it even though a scan may still overwrite the
            # suggestion list below.
            value = no_update
            if not (device_address or "").strip() and last_address:
                value = last_address

            self.logger.info("Scanning for treadmills")
            devices = discover_treadmills()

            if not devices:
                self.logger.warning("No treadmills found")
                return (
                    [],
                    value,
                    True,
                    "No treadmill found. Make sure it is powered on and not connected "
                    "elsewhere, then scan again.",
                    "warning",
                )

            options = [
                html.Option(
                    value=address,
                    label=f"{name} ({rssi} dBm)" if rssi is not None else name,
                )
                for address, name, rssi in devices
            ]

            # One hit and nothing filled in yet: fill it in so Connect just works.
            if value is no_update and len(devices) == 1 and not (device_address or "").strip():
                value = devices[0][0]
            self.logger.info(f"Found {len(devices)} treadmill(s)")
            return options, value, False, no_update, no_update

        @self.app.callback(
            outputs,
            [Input("button-connect", "n_clicks")],
            [State("connection-state", "data"), State("text-input", "value"), State("variant-select", "value")],
            running=[
                [Output("button-connect", "disabled"), True, False],
                [Output("button-scan", "disabled"), True, False],
                [Output("text-input", "disabled"), True, False],
                [Output("variant-select", "disabled"), True, False],
            ] + _busy(no_update),
            prevent_initial_call=True
        )
        def handle_connect(n_clicks: int, connection_state: str, device_address: str, variant: str) -> Tuple:
            """Handle connect/disconnect button clicks."""
            self.logger.info("Connect/Disconnect button clicked")

            if connection_state == "disconnected":
                device_address = (device_address or "").strip()
                if not device_address:
                    self.logger.error("No device address provided")
                    return self._create_output(
                        alert_open=True,
                        alert_text="Please provide a correct device address.",
                        alert_color="danger"
                    )

                self.manager = BluetoothManager(
                    device_address,
                    variant=variant or DEFAULT_VARIANT,
                    on_disconnect=self._on_disconnect,
                    on_receive=self._on_receive,
                )
                success = self.manager.connect()

                if not success:
                    self.logger.error(f"Failed to connect to {device_address}")
                    return self._create_output(
                        alert_open=True,
                        alert_text=f"Failed to connect to {device_address}. Check if the device address is correct!",
                        alert_color="danger"
                    )

                self.logger.info(f"Successfully connected to {device_address}")
                return self._create_output(
                    connection_state="connected",
                    btn_connect_text="Disconnect",
                    btn_connect_color="danger",
                    input_disabled=True,
                    variant_select_disabled=True,
                    alert_open=False,
                    interval_disabled=False,
                    last_address=device_address,
                    **CONTROLS_OFF
                )

            elif connection_state == "connected" and self.manager:
                success = self.manager.disconnect()

                if not success:
                    self.logger.error(f"Failed to disconnect from {device_address}")
                    return self._create_output(
                        alert_open=True,
                        alert_text=f"Failed to disconnect from {device_address}",
                        alert_color="danger"
                    )

                self.logger.info(f"Successfully disconnected from {device_address}")
                return self._create_output(
                    connection_state="disconnected",
                    btn_connect_text="Connect",
                    btn_connect_color="primary",
                    input_disabled=False,
                    variant_select_disabled=False,
                    alert_open=False,
                    interval_disabled=True,
                    **CONTROLS_OFF
                )

            return self._create_output()

        @self.app.callback(
            outputs,
            [Input("interval-component", "n_intervals")],
            [State("running-state", "data"), State("text-input", "value")],
            prevent_initial_call=True
        )
        def handle_interval(n_intervals: int, running_state: int, device_address: str) -> Tuple:
            """Handle periodic updates."""
            self.logger.debug("Performing interval check")
            
            if not self.manager:
                self.logger.warning(f"Lost connection to {device_address}")
                return self._create_output(
                    connection_state="disconnected",
                    btn_connect_text="Connect",
                    btn_connect_color="primary",
                    alert_open=True,
                    alert_text=f"Disconnected from {device_address}. Please try to reconnect!",
                    alert_color="danger",
                    input_disabled=False,
                    variant_select_disabled=False,
                    interval_disabled=True,
                    **CONTROLS_OFF
                )

            data = self.treadmill_data
            if data:
                imperial = data.unit_mode == 1
                speed_unit = "mph" if imperial else "kph"
                distance_unit = "mi" if imperial else "km"

                speed = f"{self._to_display_speed(data.current_speed):.1f} {speed_unit}"
                distance = f"{self._to_display_distance(data.distance):.2f} {distance_unit}"
                calories = f"{data.calories} kcal"
                steps = str(data.real_electricity_steps)
                duration = f"{data.duration_seconds:.1f} s"
                status = {0: "Starting", 1: "Running", 2: "Paused", 3: "Stopped"}.get(data.running_state, "N/A")

                table_data = [
                    ("Cycle ID", str(data.cycle_id)),
                    ("Running State", status),
                    ("Current Speed", speed),
                    ("Target Speed", f"{self._to_display_speed(data.target_speed):.1f} {speed_unit}"),
                    ("Maximum Speed", f"{self._to_display_speed(data.max_speed):.1f} {speed_unit}"),
                    ("Current Incline", f"{data.current_incline:.1f} °"),
                    ("Target Incline", f"{data.target_incline:.1f} °"),
                    ("Maximum Incline", f"{data.max_incline:.1f} °"),
                    ("Heart Rate", f"{data.heart_rate} bpm"),
                    ("Distance", distance),
                    ("Steps", steps),
                    ("Calories", calories),
                    ("Duration", f"{timedelta(seconds=data.duration_seconds)}"),
                    ("WIFI Connected", str(data.is_connected)),
                    ("Motor Rotation Speed", f"{data.real_rotate} rpm"),
                    ("Motor Load", f"{data.real_electricity} %"),
                    ("Serial Number", str(data.serial_number)),
                    ("Firmware Version", str(data.firmware_version)),
                    ("Device Type", str(data.device_type)),
                    ("Unit Mode", "Imperial" if imperial else "Metric"),
                ]

                return self._create_output(
                    speed_input_unit=speed_unit,
                    speed=speed,
                    distance=distance,
                    calories=calories,
                    steps=steps,
                    duration=duration,
                    status=status,
                    running_state=data.running_state if data.running_state != running_state else no_update,
                    table_body=self._create_table_body(table_data)
                )

            return self._create_output()

        @self.app.callback(
            [
                Output("button-start", "children"),
                Output("button-start", "disabled"),
                Output("button-stop", "disabled"),
                Output("button-speed-down", "disabled"),
                Output("button-speed-up", "disabled"),
                Output("button-set-speed", "disabled"),
                Output("speed-input", "disabled")
            ],
            [Input("running-state", "data")],
            prevent_initial_call=True
        )
        def update_controls(running_state: int) -> Tuple[str, bool, bool, bool, bool, bool, bool]:
            """Update control button states based on running state."""
            # The speed entry follows the same rule as the +/- buttons: the
            # treadmill only accepts a new target while it is running.
            all_off = ("Start", True, True, True, True, True, True)
            states = {
                0: all_off,  # Starting
                1: ("Pause", False, True, False, False, False, False),  # Running
                2: ("Start", False, False, True, True, True, True),  # Paused
                3: ("Start", False, True, True, True, True, True),  # Stopped
            }
            return states.get(running_state, all_off) if self.manager else all_off

        @self.app.callback(
            [],
            [Input("button-start", "n_clicks")],
            [State("running-state", "data")],
            running=_busy(True),
            prevent_initial_call=True
        )
        def handle_start(n_clicks: int, running_state: int) -> None:
            """Handle start/pause button clicks."""
            self.logger.info("Start/Pause button clicked")
            if self.manager:
                if running_state == 1:
                    self.manager.send_data(TreadmillController.pause(is_kph=self._is_kph()))
                elif running_state in (2, 3):
                    self.manager.send_data(TreadmillController.start(is_kph=self._is_kph()))

        @self.app.callback(
            [],
            [Input("button-stop", "n_clicks")],
            [State("running-state", "data")],
            running=_busy(True),
            prevent_initial_call=True
        )
        def handle_stop(n_clicks: int, running_state: int) -> None:
            """Handle stop button clicks."""
            self.logger.info("Stop button clicked")
            if self.manager and running_state == 2:
                self.manager.send_data(TreadmillController.stop(is_kph=self._is_kph()))

        @self.app.callback(
            [],
            [Input("button-set-speed", "n_clicks")],
            [State("speed-input", "value"), State("running-state", "data")],
            running=speed_busy,
            prevent_initial_call=True
        )
        def handle_set_speed(n_clicks: int, speed: Optional[float], running_state: int) -> None:
            """Handle an absolute speed entered in the speed field."""
            self.logger.info(f"Set Speed clicked with value {speed}")
            if not (self.manager and self.treadmill_data and running_state == 1):
                return
            if speed is None:
                self.logger.warning("No speed entered")
                return

            try:
                target_speed = self._to_wire_speed(float(speed))
            except (TypeError, ValueError):
                self.logger.warning(f"Ignoring unparseable speed: {speed!r}")
                return

            # Clamp to what the treadmill says it will accept, so a typo cannot
            # send an out-of-range target.
            max_speed = self.treadmill_data.max_speed or 0
            if max_speed > 0:
                target_speed = min(target_speed, max_speed)
            target_speed = max(target_speed, 0)

            self.logger.info(f"Setting speed to {target_speed} (wire) / {self._to_display_speed(target_speed):.1f} (display)")
            self.manager.send_data(TreadmillController.set_speed(target_speed, is_kph=self._is_kph()))

        @self.app.callback(
            [],
            [Input("button-speed-up", "n_clicks")],
            [State("running-state", "data")],
            running=speed_busy,
            prevent_initial_call=True
        )
        def handle_speed_up(n_clicks: int, running_state: int) -> None:
            """Handle speed up button clicks."""
            self.logger.info("Speed Up button clicked")
            self._nudge_speed(0.1, running_state)

        @self.app.callback(
            [],
            [Input("button-speed-down", "n_clicks")],
            [State("running-state", "data")],
            running=speed_busy,
            prevent_initial_call=True
        )
        def handle_speed_down(n_clicks: int, running_state: int) -> None:
            """Handle speed down button clicks."""
            self.logger.info("Speed Down button clicked")
            self._nudge_speed(-0.1, running_state)

    def run(self) -> None:
        """Run the Dash application."""
        self.logger.info(f"Starting application on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=self.debug)