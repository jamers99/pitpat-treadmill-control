from typing import List, Tuple, Optional, Any
from dash import Dash, html, dcc, Output, Input, State, no_update
import dash_bootstrap_components as dbc
from datetime import timedelta
from .bluetooth_manager import BluetoothManager, discover_treadmills
from .treadmill_controller import TreadmillController
from .logger import setup_logger

# The protocol is metric whatever the treadmill's display is set to: speeds are
# thousandths of a kph and distances are metres. Confirmed on firmware 37 — a
# 3000 target reads 1.9 on a unit displaying mph (3.0 / 1.609), and the status
# frames come back in the same metric units.
KM_PER_MILE = 1.609344

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
                        dbc.Input(
                            id="text-input",
                            type="text",
                            placeholder="Device Address",
                            list="device-list",
                            autoComplete="off",
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
                dcc.Interval(id="interval-component", interval=1000, n_intervals=0, disabled=True),
                # Fires once shortly after page load to scan without a click.
                dcc.Interval(id="interval-scan", interval=500, n_intervals=0, max_intervals=1),
            ]
        )

    def _is_imperial(self) -> bool:
        """Whether the treadmill's own display is set to miles."""
        return bool(self.treadmill_data and self.treadmill_data.unit_mode == 1)

    def _to_display_speed(self, wire_speed: int) -> float:
        """
        Converts a wire speed to the unit the treadmill's display uses.

        :param wire_speed: Speed in thousandths of a kph, as sent and reported.
        :return: Speed in kph, or mph when the treadmill is in imperial mode.
        """
        speed = wire_speed / 1000
        return speed / KM_PER_MILE if self._is_imperial() else speed

    def _to_wire_speed(self, display_speed: float) -> int:
        """
        Converts a speed the user sees into the value the treadmill expects.

        :param display_speed: Speed in kph, or mph in imperial mode.
        :return: Speed in thousandths of a kph.
        """
        if self._is_imperial():
            display_speed = display_speed * KM_PER_MILE
        return int(round(display_speed * 1000))

    def _to_display_distance(self, wire_distance: int) -> float:
        """
        Converts a wire distance to the unit the treadmill's display uses.

        :param wire_distance: Distance in metres, as reported.
        :return: Distance in km, or miles when the treadmill is in imperial mode.
        """
        distance = wire_distance / 1000
        return distance / KM_PER_MILE if self._is_imperial() else distance

    def _is_kph(self) -> bool:
        """
        Unit flag to stamp on outgoing commands.

        The command's unit bit has to agree with the mode the treadmill is
        actually reporting, so drive it off the last status frame rather than
        assuming metric.
        """
        return not self._is_imperial()

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
        defaults = {
            "btn_connect_text": no_update,
            "btn_connect_color": no_update,
            "input_disabled": no_update,
            "speed": no_update,
            "distance": no_update,
            "calories": no_update,
            "steps": no_update,
            "duration": no_update,
            "status": no_update,
            "alert_open": no_update,
            "alert_text": no_update,
            "alert_color": no_update,
            "btn_start_text": no_update,
            "btn_start_disabled": no_update,
            "btn_stop_disabled": no_update,
            "btn_speed_down_disabled": no_update,
            "btn_speed_up_disabled": no_update,
            "btn_set_speed_disabled": no_update,
            "speed_input_disabled": no_update,
            "speed_input_unit": no_update,
            "table_body": no_update,
            "connection_state": no_update,
            "running_state": no_update,
            "interval_disabled": no_update,
        }
        defaults.update(kwargs)
        return tuple(defaults.values())

    def _register_callbacks(self) -> None:
        """Register all Dash callbacks."""
        outputs = [
            Output("button-connect", "children", allow_duplicate=True),
            Output("button-connect", "color", allow_duplicate=True),
            Output("text-input", "disabled", allow_duplicate=True),
            Output("speed", "children", allow_duplicate=True),
            Output("distance", "children", allow_duplicate=True),
            Output("calories", "children", allow_duplicate=True),
            Output("steps", "children", allow_duplicate=True),
            Output("duration", "children", allow_duplicate=True),
            Output("status", "children", allow_duplicate=True),
            Output("alert", "is_open", allow_duplicate=True),
            Output("alert", "children", allow_duplicate=True),
            Output("alert", "color", allow_duplicate=True),
            Output("button-start", "children", allow_duplicate=True),
            Output("button-start", "disabled", allow_duplicate=True),
            Output("button-stop", "disabled", allow_duplicate=True),
            Output("button-speed-down", "disabled", allow_duplicate=True),
            Output("button-speed-up", "disabled", allow_duplicate=True),
            Output("button-set-speed", "disabled", allow_duplicate=True),
            Output("speed-input", "disabled", allow_duplicate=True),
            Output("speed-input-unit", "children", allow_duplicate=True),
            Output("table-data", "children", allow_duplicate=True),
            Output("connection-state", "data", allow_duplicate=True),
            Output("running-state", "data", allow_duplicate=True),
            Output("interval-component", "disabled", allow_duplicate=True),
        ]

        @self.app.callback(
            [
                Output("device-list", "children"),
                Output("text-input", "value"),
                Output("alert", "is_open", allow_duplicate=True),
                Output("alert", "children", allow_duplicate=True),
                Output("alert", "color", allow_duplicate=True),
            ],
            [Input("button-scan", "n_clicks"), Input("interval-scan", "n_intervals")],
            [State("connection-state", "data"), State("text-input", "value")],
            running=[
                [Output("button-scan", "children"), "Scanning...", "Scan"],
                [Output("button-scan", "disabled"), True, False],
                [Output("button-connect", "disabled"), True, False],
            ],
            prevent_initial_call=True
        )
        def handle_scan(n_clicks: int, n_intervals: int, connection_state: str, device_address: str) -> Tuple:
            """Scan for nearby treadmills and offer them as address suggestions."""
            if connection_state == "connected":
                return no_update, no_update, no_update, no_update, no_update

            self.logger.info("Scanning for treadmills")
            devices = discover_treadmills()

            if not devices:
                self.logger.warning("No treadmills found")
                return (
                    [],
                    no_update,
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

            # One hit and nothing typed yet: fill it in so Connect just works.
            value = devices[0][0] if len(devices) == 1 and not (device_address or "").strip() else no_update
            self.logger.info(f"Found {len(devices)} treadmill(s)")
            return options, value, False, no_update, no_update

        @self.app.callback(
            outputs,
            [Input("button-connect", "n_clicks")],
            [State("connection-state", "data"), State("text-input", "value")],
            running=[
                [Output("button-connect", "disabled"), True, False],
                [Output("button-scan", "disabled"), True, False],
                [Output("text-input", "disabled"), True, False],
                [Output("button-start", "disabled"), True, no_update],
                [Output("button-stop", "disabled"), True, no_update],
                [Output("button-speed-down", "disabled"), True, no_update],
                [Output("button-speed-up", "disabled"), True, no_update],
                [Output("button-set-speed", "disabled"), True, no_update],
                [Output("speed-input", "disabled"), True, no_update],
            ],
            prevent_initial_call=True
        )
        def handle_connect(n_clicks: int, connection_state: str, device_address: str) -> Tuple:
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
                
                self.manager = BluetoothManager(device_address, on_disconnect=self._on_disconnect, on_receive=self._on_receive)
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
                    btn_start_disabled=True,
                    btn_stop_disabled=True,
                    btn_speed_down_disabled=True,
                    btn_speed_up_disabled=True,
                    btn_set_speed_disabled=True,
                    speed_input_disabled=True,
                    alert_open=False,
                    interval_disabled=False
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
                    btn_start_disabled=True,
                    btn_stop_disabled=True,
                    btn_speed_down_disabled=True,
                    btn_speed_up_disabled=True,
                    btn_set_speed_disabled=True,
                    speed_input_disabled=True,
                    alert_open=False,
                    interval_disabled=True
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
                    btn_start_disabled=True,
                    btn_speed_down_disabled=True,
                    btn_speed_up_disabled=True,
                    btn_set_speed_disabled=True,
                    speed_input_disabled=True,
                    interval_disabled=True
                )
            
            if self.treadmill_data:
                unit_mode = {0: "Metric", 1: "Imperial"}.get(self.treadmill_data.unit_mode, "N/A")
                speed_unit = {0: "kph", 1: "mph"}.get(self.treadmill_data.unit_mode, "kph")
                distance_unit = {0: "km", 1: "mi"}.get(self.treadmill_data.unit_mode, "km")
                
                speed = f"{self._to_display_speed(self.treadmill_data.current_speed):.1f} {speed_unit}"
                distance = f"{self._to_display_distance(self.treadmill_data.distance):.2f} {distance_unit}"
                calories = f"{self.treadmill_data.calories} kcal"
                steps = f"{self.treadmill_data.real_electricity_steps}"
                duration = f"{self.treadmill_data.duration_seconds:.1f} s"
                status = {0: "Starting", 1: "Running", 2: "Paused", 3: "Stopped"}.get(self.treadmill_data.running_state, "N/A")

                table_data = [
                    ("Cycle ID", f"{self.treadmill_data.cycle_id}"),
                    ("Running State", f"{status}"),
                    ("Current Speed", f"{self._to_display_speed(self.treadmill_data.current_speed):.1f} {speed_unit}"),
                    ("Target Speed", f"{self._to_display_speed(self.treadmill_data.target_speed):.1f} {speed_unit}"),
                    ("Maximum Speed", f"{self._to_display_speed(self.treadmill_data.max_speed):.1f} {speed_unit}"),
                    ("Current Incline", f"{self.treadmill_data.current_incline:.1f} °"),
                    ("Target Incline", f"{self.treadmill_data.target_incline:.1f} °"),
                    ("Maximum Incline", f"{self.treadmill_data.max_incline:.1f} °"),
                    ("Heart Rate", f"{self.treadmill_data.heart_rate} bpm"),
                    ("Distance", f"{self._to_display_distance(self.treadmill_data.distance):.2f} {distance_unit}"),
                    ("Steps", f"{self.treadmill_data.real_electricity_steps}"),
                    ("Calories", f"{self.treadmill_data.calories} kcal"),
                    ("Duration", f"{timedelta(seconds=self.treadmill_data.duration_seconds)}"),
                    ("WIFI Connected", f"{self.treadmill_data.is_connected}"),
                    ("Motor Rotation Speed", f"{self.treadmill_data.real_rotate} rpm"),
                    ("Motor Load", f"{self.treadmill_data.real_electricity} %"),
                    ("Serial Number", f"{self.treadmill_data.serial_number}"),
                    ("Firmware Version", f"{self.treadmill_data.firmware_version}"),
                    ("Device Type", f"{self.treadmill_data.device_type}"),
                    ("Unit Mode", f"{unit_mode}"),
                ]
                
                new_running_state = self.treadmill_data.running_state if self.treadmill_data.running_state != running_state else no_update
                
                return self._create_output(
                    speed_input_unit=speed_unit,
                    speed=speed,
                    distance=distance,
                    calories=calories,
                    steps=steps,
                    duration=duration,
                    status=status,
                    running_state=new_running_state,
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
            if not self.manager:
                return "Start", True, True, True, True, True, True

            states = {
                0: ("Start", True, True, True, True, True, True),  # Starting
                1: ("Pause", False, True, False, False, False, False),  # Running
                2: ("Start", False, False, True, True, True, True),  # Paused
                3: ("Start", False, True, True, True, True, True)  # Stopped
            }
            return states.get(running_state, ("Start", True, True, True, True, True, True))

        @self.app.callback(
            [],
            [Input("button-start", "n_clicks")],
            [State("running-state", "data")],
            running=[
                [Output("button-start", "disabled"), True, True],
                [Output("button-stop", "disabled"), True, True],
                [Output("button-speed-down", "disabled"), True, True],
                [Output("button-speed-up", "disabled"), True, True],
                [Output("button-set-speed", "disabled"), True, True],
                [Output("speed-input", "disabled"), True, True],
            ],
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
            running=[
                [Output("button-start", "disabled"), True, True],
                [Output("button-stop", "disabled"), True, True],
                [Output("button-speed-down", "disabled"), True, True],
                [Output("button-speed-up", "disabled"), True, True],
                [Output("button-set-speed", "disabled"), True, True],
                [Output("speed-input", "disabled"), True, True],
            ],
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
            running=[
                [Output("button-start", "disabled"), True, False],
                [Output("button-speed-down", "disabled"), True, False],
                [Output("button-speed-up", "disabled"), True, False],
                [Output("button-set-speed", "disabled"), True, False],
                [Output("speed-input", "disabled"), True, False],
            ],
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
            running=[
                [Output("button-start", "disabled"), True, False],
                [Output("button-speed-down", "disabled"), True, False],
                [Output("button-speed-up", "disabled"), True, False],
                [Output("button-set-speed", "disabled"), True, False],
                [Output("speed-input", "disabled"), True, False],
            ],
            prevent_initial_call=True
        )
        def handle_speed_up(n_clicks: int, running_state: int) -> None:
            """Handle speed up button clicks."""
            self.logger.info("Speed Up button clicked")
            if self.manager and self.treadmill_data and running_state == 1:
                # A 0.1 step in whatever unit the display is using.
                target_speed = self._to_wire_speed(self._to_display_speed(self.treadmill_data.current_speed) + 0.1)
                self.manager.send_data(TreadmillController.set_speed(target_speed, is_kph=self._is_kph()))

        @self.app.callback(
            [],
            [Input("button-speed-down", "n_clicks")],
            [State("running-state", "data")],
            running=[
                [Output("button-start", "disabled"), True, False],
                [Output("button-speed-down", "disabled"), True, False],
                [Output("button-speed-up", "disabled"), True, False],
                [Output("button-set-speed", "disabled"), True, False],
                [Output("speed-input", "disabled"), True, False],
            ],
            prevent_initial_call=True
        )
        def handle_speed_down(n_clicks: int, running_state: int) -> None:
            """Handle speed down button clicks."""
            self.logger.info("Speed Down button clicked")
            if self.manager and self.treadmill_data and running_state == 1:
                # A 0.1 step in whatever unit the display is using.
                target_speed = self._to_wire_speed(max(self._to_display_speed(self.treadmill_data.current_speed) - 0.1, 0))
                self.manager.send_data(TreadmillController.set_speed(target_speed, is_kph=self._is_kph()))

    def run(self) -> None:
        """Run the Dash application."""
        self.logger.info(f"Starting application on {self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=self.debug)