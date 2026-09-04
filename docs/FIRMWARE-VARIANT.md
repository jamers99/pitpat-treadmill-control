# PitPat T01 — firmware 37 ("fba0") variant

Upstream targets the **T01 (BA04)** revision. This variant speaks the same
application protocol over different GATT characteristics and without the
4-byte transport wrapper. Confirmed on real hardware: firmware `37`,
`device_type` 5, max speed 6000, imperial units.

## Do I have this variant?

```
svc 0000fba0-…      ← vendor service (this variant, vs. ff00-family upstream)
    0000fba2-…  read, notify      ← status stream
    0000fba1-…  read, write       ← commands
```

Enumerate a device's own services/characteristics with:

```python
import asyncio
from bleak import BleakClient
async def m():
    async with BleakClient("AA:BB:CC:DD:EE:FF") as c:
        for s in c.services:
            print("svc", s.uuid)
            for ch in s.characteristics:
                print("   ", ch.uuid, ch.properties)
asyncio.run(m())
```

## Deviations from upstream

| | Upstream (BA04) | This variant |
| --- | --- | --- |
| notify char | `0000ff02-…` | `0000fba2-…` |
| write char  | `0000ff01-…` | `0000fba1-…` |
| inbound wrapper | strips `data[4:]` | none — parse raw |
| outbound wrapper | `4d00 <counter> <length>` + packet | none — write the bare packet |

A wrapped write on this variant is accepted at the ATT layer with no error and
then silently discarded — the belt never moves and nothing looks wrong.
Heartbeat payload is `6a05fdf843` (upstream's leading `05` was the wrapper's
length byte, not part of the packet).

**Unit bit must match the device.** Derive it from the last status frame, not
an assumption of metric: `packet[12] = cmd & 0xF7 if is_kph else cmd | 0x08`.

**Speed/distance are metric on the wire regardless of the unit bit or the
treadmill's own display.** `target_speed` is thousandths of a kph, `distance`
is metres, both directions. Convert at the UI boundary
(`_to_wire_speed`/`_to_display_speed` in `src/app.py`), not in the packet.

Both changes are currently unconditional in `src/bluetooth_manager.py` —
running this checkout against a BA04 unit will not work. Supporting both
means probing for `fba0` vs. `ff00` at connect time and switching UUIDs/wrapper
mode from that.

### Adapting this for BA04 (or another variant)

This checkout hardcodes the fba0 constants. To point it at a different
device, hand a coding agent this file plus:

1. `src/bluetooth_manager.py` — `NOTIFY_CHAR_UUID`, `WRITE_CHAR_UUID`,
   `HEARTBEAT_PACKET`, and the `TreadmillData(data)` call in
   `_notification_handler` (upstream needs `data[4:]`; add back the
   `4d00 <counter> <length>` wrapper in `_write_data_and_set_request` /
   `_write_heartbeat` if the target also needs it).
2. Enumerate the device's GATT services/characteristics (§1 above shows the
   `bleak` snippet) and diff against the tables in this doc to find what
   actually differs.
3. Confirm with the XOR checksum: a correctly-unwrapped status frame has
   `checksum_valid = True`; a wrong offset or leftover wrapper byte flips it
   to `False` immediately, so there's no guessing.

## Status frame (notify on `fba2`, 53 bytes, ~1/s, unprompted)

Same layout as upstream `TreadmillData`. Notable offsets:

| Offset | Field |
| --- | --- |
| 0 | frame type — `0x66` here, `0x68` on BA04 |
| 3–4 / 5–6 | current / target speed, BE |
| 26 | flags: bit 0 connected, bits 3–4 running state, bit 7 unit mode (1=imperial) |
| 51 | XOR checksum over bytes 1–50 |

## Command frame (write to `fba1`, 23 bytes, sent bare)

| Command | Packet |
| --- | --- |
| start 1.00 | `6a17 00000000 03e8 01 00 50 00 0c <userid> 12 43` |
| set 1.10 | `6a17 00000000 044c 05 00 50 00 0c <userid> b5 43` |
| pause | `6a17 00000000 0000 01 00 50 00 0a <userid> ff 43` |
| stop | `6a17 00000000 0000 01 00 50 00 08 <userid> fd 43` |

Byte 12 is the command (0 stop, 2 pause, 4 start/set, `|0x08` imperial).
User id is unvalidated — upstream's hardcoded `DEFAULT_USER_ID` works fine,
no pairing/bonding required. Heartbeat/status-poll is the same shape at
length 5: `6a 05 fd f8 43`.

## Docker on Linux

`--device=/dev/rfcomm0` is wrong here — this is BLE GATT, not Bluetooth
Classic serial. bleak talks to `bluetoothd` over D-Bus only, so the container
needs just the system bus socket, no `--privileged`/`--net=host`/`CAP_NET_ADMIN`:

```bash
docker run -d -p 8050:8050 \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  pitpat-treadmill-control
```

## Troubleshooting

**`Device with address … was not found`** — usually a stale link, not a
missing device: something else (desktop Bluetooth panel, a previous session)
already holds the connection, so it stops advertising. Check with
`bluetoothctl info <MAC> | grep Connected`; fix with
`bluetoothctl disconnect <MAC>`. `_release_stale_link()` in
`bluetooth_manager.py` now does this automatically before each connect.
Never connect the treadmill from the desktop Bluetooth panel — let the app
own the link. Pairing is neither needed nor wanted.

**Connects, dashboard stays empty** — wrong notify UUID, or the `data[4:]`
strip is still present.

**Connects and shows data, buttons do nothing** — outbound wrapper still
present; check logged bytes start with `6a`, not `4d00`.

**`2b10`/`2b11` (service `1910`)** — advertised but inert on this firmware,
not a control channel.
